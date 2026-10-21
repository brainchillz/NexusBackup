"""Conversion: a restore point of one platform becomes a restore point of the other.

The backup is never copied. Each disk is exported read-only over NBD straight from the
chunk repository, a qcow2 overlay on top of that export gives the guest-fix-up tools a
writable disk, and afterwards only the blocks the tools changed (a few MiB: initramfs,
network and tool packages) are read back and stored as new chunks. The rest of the new
restore point's index is the old index, so a converted point costs almost nothing in the
repository and is a complete image like any other.

Fix-ups are done by libguestfs on this server (virt-v2v-in-place towards KVM/Proxmox,
virt-customize towards VMware): virtio or VMware storage and network drivers in the
initramfs, VMware tools out and the QEMU guest agent in or the reverse (installed on
the guest's first boot), and a systemd .link file per NIC so the guest keeps its
interface names although the PCI layout changes. Linux guests only in this release.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid as uuidlib
import xml.etree.ElementTree as ET

from .. import db
from ..store.chunks import repo, ZERO_HASH
from . import nbd, tasks

TARGETS = ('proxmox', 'vmware')
TOOLS = {'proxmox': ('qemu-img', 'qemu-nbd', 'virt-inspector', 'guestfish', 'virt-v2v-in-place'),
         'vmware': ('qemu-img', 'qemu-nbd', 'virt-inspector', 'guestfish', 'virt-customize')}
TOOL_PACKAGES = 'apt install virt-v2v libguestfs-tools qemu-utils (and give the hvbackup user a readable kernel and the kvm group: the installer does both)'
NIC_NAME_RE = re.compile(r'(?<![\w-])(en[a-z]*\d[\w.]*|eth\d+)(?![\w-])')
DEBIAN_FAMILY = ('debian', 'ubuntu', 'kalilinux', 'linuxmint', 'pop')
RHEL_FAMILY = ('rhel', 'centos', 'rocky', 'almalinux', 'oraclelinux', 'fedora', 'scientificlinux')
SUSE_FAMILY = ('sles', 'opensuse', 'suse-based')
SYNTH_VMID_BASE = 900000000  # Proxmox vmids are < 1e9; converted VMware points carry one so the Proxmox UI can list them


def guest_env():
    env = dict(os.environ)
    env.setdefault('LIBGUESTFS_BACKEND', 'direct')
    env.setdefault('LIBGUESTFS_CACHEDIR', '/var/tmp')
    env.setdefault('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
    return env


def check_tools(target):
    missing = [t for t in TOOLS[target] if not shutil.which(t)]
    if missing:
        raise RuntimeError(f'conversion needs {", ".join(missing)} on the backup server: {TOOL_PACKAGES}')


def guest_is_windows(spec):
    gid = (spec.get('guest_id') or '').lower()
    return 'win' in gid or gid.startswith('w')


def new_mac(target):
    r = secrets.token_bytes(3)
    if target == 'vmware':   # the range vSphere accepts for manually assigned addresses
        return f'00:50:56:{r[0] & 0x3f:02x}:{r[1]:02x}:{r[2]:02x}'
    return f'bc:24:11:{r[0]:02x}:{r[1]:02x}:{r[2]:02x}'  # Proxmox's OUI


def vmware_guest_id(ins):
    """libguestfs inspection -> vSphere guestId."""
    distro, major = ins.get('distro', ''), ins.get('major', 0)
    if distro == 'ubuntu':
        return 'ubuntu64Guest'
    if distro == 'debian':
        return f'debian{min(max(major, 10), 12)}_64Guest'
    if distro in ('rhel', 'centos', 'rocky', 'almalinux', 'oraclelinux', 'scientificlinux'):
        return f'rhel{min(max(major, 7), 9)}_64Guest'
    if distro == 'fedora':
        return 'fedora64Guest'
    if distro in ('sles', 'suse-based'):
        return 'sles15_64Guest'
    if distro == 'opensuse':
        return 'opensuse64Guest'
    if distro in ('archlinux', 'alpine', 'gentoo', 'void', 'nixos'):
        return 'other5xLinux64Guest'
    return 'other5xLinux64Guest'


def run(task, cmd, env=None, cwd=None, prefix=None, check=True):
    """Run a tool, streaming its output lines into the task log."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env or guest_env(), cwd=cwd, text=True, errors='replace')
    lines = []
    for line in p.stdout:
        line = line.rstrip()
        if not line:
            continue
        lines.append(line)
        if prefix is not None and task is not None:
            task.log(f'{prefix}{line[:300]}')
    p.wait()
    if check and p.returncode != 0:
        tail = '\n'.join(lines[-8:])
        raise RuntimeError(f'{os.path.basename(cmd[0])} failed ({p.returncode}): {tail}')
    return '\n'.join(lines)


def inspect_guest(task, overlays):
    cmd = ['virt-inspector', '--format=qcow2', '--no-applications', '--no-icon']
    for ov in overlays:
        cmd += ['-a', ov]
    out = run(task, cmd)
    try:
        root = ET.fromstring(out[out.index('<?xml'):] if '<?xml' in out else out)
    except (ValueError, ET.ParseError) as e:
        raise RuntimeError(f'virt-inspector gave no usable answer: {e}: {out[-300:]}')
    os_el = root.find('operatingsystem')
    if os_el is None:
        raise RuntimeError('no operating system was found on the disks; only installed Linux guests can be converted')
    g = lambda k, d='': (os_el.findtext(k) or d)  # noqa: E731
    ins = {'type': g('name'), 'distro': g('distro'), 'major': int(g('major_version', '0') or 0), 'minor': int(g('minor_version', '0') or 0),
           'arch': g('arch'), 'product': g('product_name'), 'hostname': g('hostname'), 'package_management': g('package_management'),
           'root': g('root')}
    return ins


NET_DIRS = ('/etc/netplan', '/etc/sysconfig/network-scripts', '/etc/sysconfig/network', '/etc/NetworkManager/system-connections',
            '/etc/network', '/etc/systemd/network', '/etc/udev/rules.d')
MAC_RE = re.compile(r'\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b', re.IGNORECASE)


def guest_net_files(task, overlays, workdir):
    """The guest's network configuration files: {path: text}. Read through guestfish in one
    session (each directory as a tar), so the appliance boots once."""
    d = os.path.join(workdir, 'netcfg')
    os.makedirs(d, exist_ok=True)
    script = ''.join(f'-tar-out {p} {d}/{i}.tar\n' for i, p in enumerate(NET_DIRS))
    cmd = ['guestfish', '--ro', '--format=qcow2', '-i']
    for ov in overlays:
        cmd += ['-a', ov]
    p = subprocess.run(cmd, input=script, capture_output=True, text=True, errors='replace', env=guest_env())
    if p.returncode != 0:
        task.log(f'WARNING: could not read the guest network configuration ({p.stderr.strip()[-200:]}); interface names and addresses are left alone')
        return {}
    import tarfile
    out = {}
    for i, base in enumerate(NET_DIRS):
        tp = f'{d}/{i}.tar'
        if not os.path.exists(tp) or os.path.getsize(tp) == 0:
            continue
        try:
            with tarfile.open(tp) as tf:
                for m in tf.getmembers():
                    if not m.isfile() or m.size > 256 * 1024:
                        continue
                    name = m.name.lstrip('./')
                    if base == '/etc/network' and not (name == 'interfaces' or name.startswith('interfaces.d/')):
                        continue
                    if base == '/etc/udev/rules.d' and 'net' not in name:
                        continue
                    if base == '/etc/systemd/network' and not (name.endswith('.network') or name.endswith('.link')):
                        continue
                    out[f'{base}/{name}'] = tf.extractfile(m).read().decode('utf-8', 'replace')
        except (tarfile.TarError, OSError) as e:
            task.log(f'WARNING: {base}: {e}')
    return out


def nic_names_in(files):
    """Interface names the guest's network configuration refers to, in order of appearance."""
    names = []
    for path in sorted(files):
        for m in NIC_NAME_RE.finditer(files[path]):
            n = m.group(1)
            if n not in names and n != 'ethernets':
                names.append(n)
    return names


def rewrite_macs(files, pairs):
    """Config files that pin the old MAC addresses (netplan match, ifcfg HWADDR, NM keyfiles,
    udev rules) rewritten to the new ones: {path: new text} for the files that changed."""
    table = {a.lower(): b.lower() for a, b in pairs if a and b and a.lower() != b.lower()}
    if not table:
        return {}
    out = {}
    for path, text in files.items():
        new = MAC_RE.sub(lambda m: table.get(m.group(1).lower(), m.group(1)), text)
        if new != text:
            out[path] = new
    return out


def guest_nic_names(task, overlays, workdir=None):
    files = guest_net_files(task, overlays, workdir or tempfile.mkdtemp())
    return nic_names_in(files)


def link_files(workdir, nics, names):
    """One systemd .link per NIC: match the (new) MAC, keep the name the guest config uses."""
    d = os.path.join(workdir, 'links')
    os.makedirs(d, exist_ok=True)
    out = []
    for i, n in enumerate(nics):
        if i >= len(names) or not n.get('mac'):
            continue
        p = os.path.join(d, f'70-hvbackup-{names[i]}.link')
        with open(p, 'w') as f:
            f.write(f'# written by hvbackup at conversion: keep the interface name across platforms\n[Match]\nMACAddress={n["mac"]}\n\n[Link]\nName={names[i]}\n')
        out.append(p)
    return out


def modified_chunks(map_json, chunk_size, capacity):
    """Chunks an overlay changed (qemu-img map: depth 0 is the overlay itself), split into
    the ones to read back and the ones the overlay made entirely zero (discarded free space:
    the fix-up tools trim the filesystem), which become zero chunks without a read."""
    touched, zero_runs = set(), []
    last = max((capacity + chunk_size - 1) // chunk_size - 1, 0)
    for e in map_json:
        if e.get('depth', 0) != 0 or not e.get('length'):
            continue
        a, b = e['start'] // chunk_size, min((e['start'] + e['length'] - 1) // chunk_size, last)
        touched.update(range(a, b + 1))
        if e.get('zero') and not e.get('data'):
            zero_runs.append((e['start'], e['start'] + e['length']))
    zero_runs.sort()
    zeroed = []
    for ci in sorted(touched):
        lo, hi = ci * chunk_size, min((ci + 1) * chunk_size, capacity)
        pos = lo
        for s, e in zero_runs:
            if e <= pos:
                continue
            if s > pos:
                break
            pos = min(e, hi)
            if pos >= hi:
                break
        if pos >= hi:
            zeroed.append(ci)
    zset = set(zeroed)
    return [ci for ci in sorted(touched) if ci not in zset], zeroed


def pve_config_text(spec, name, nics, uuid, install_tools):
    efi = spec.get('firmware') == 'efi'
    # one socket with all the vCPUs: VMware's one-core-per-socket default has no value on KVM
    cpus = max(int(spec.get('cpus') or 1), 1)
    lines = [f'name: {pve_name(name)}', f'cores: {cpus}', 'sockets: 1', f'memory: {int(spec.get("memory_mb") or 1024)}',
             'ostype: l26', 'cpu: x86-64-v2-AES', 'scsihw: virtio-scsi-pci', f'agent: {1 if install_tools else 0}',
             f'bios: {"ovmf" if efi else "seabios"}', f'smbios1: uuid={uuid}']
    if efi:
        lines.append('machine: q35')
    # qemu-server restores the drives named in #qmdump#map hints (virtdev:archive volume:storage:format)
    for i in range(len(spec['disks'])):
        lines.insert(0, f'#qmdump#map:scsi{i}:drive-scsi{i}::raw:')
    for i, d in enumerate(spec['disks']):
        size_g = max((int(d['capacity']) + 2**30 - 1) // 2**30, 1)
        lines.append(f'scsi{i}: local:vm-0-disk-{i},size={size_g}G')
    if spec['disks']:
        lines.append('boot: order=scsi0')
    for i, n in enumerate(nics):
        lines.append(f'net{i}: virtio={n["mac"].upper()},bridge=vmbr0' + ('' if n.get('connected', True) else ',link_down=1'))
    lines.append(f'description: converted by hvbackup from {spec.get("platform", "?")} ({spec.get("name", "")})')
    return '\n'.join(lines) + '\n'


def pve_name(name):
    n = re.sub(r'[^A-Za-z0-9.-]+', '-', name or 'vm').strip('-.')
    return n[:63] or 'vm'


def target_spec(spec, target, ins, nics, uuid, install_tools, source_rp, name):
    t = {k: v for k, v in spec.items() if k not in ('pve', 'pve_config', 'pve_firewall', 'vmtype', 'vmid')}
    t.update({'platform': target, 'uuid': uuid, 'placement': {}, 'platform_id': '', 'tools': '',
              'converted_from': {'restore_point': source_rp, 'platform': spec.get('platform'), 'guest': ins}})
    if target == 'proxmox':
        t['guest_id'], t['guest_name'] = 'l26', 'Linux 2.6 - 6.x kernel'
        # qemu-server matches archive volumes to config drives by 'drive-<key>', as its own backups name them
        t['disks'] = [{'key': f'drive-scsi{i}', 'label': d.get('label') or f'scsi{i}', 'capacity': d['capacity'], 'controller': 'virtio-scsi',
                       'bus': 0, 'unit': i, 'thin': True, 'file': '', 'mode': 'persistent', 'datastore': ''} for i, d in enumerate(spec['disks'])]
        t['nics'] = [{'key': f'net{i}', 'mac': n['mac'], 'model': 'virtio', 'network': n.get('network', ''), 'connected': n.get('connected', True)}
                     for i, n in enumerate(nics)]
        t['vmtype'], t['vmid'] = 'qemu', SYNTH_VMID_BASE + source_rp
        t['pve_config'] = pve_config_text(spec, name, nics, uuid, install_tools)
        t['tools'] = 'agent' if install_tools else ''
    else:
        t['guest_id'] = vmware_guest_id(ins)
        t['guest_name'] = ins.get('product') or t['guest_id']
        t['disks'] = [{**d, 'controller': 'pvscsi', 'bus': 0, 'unit': i, 'thin': True, 'file': '', 'datastore': ''} for i, d in enumerate(spec['disks'])]
        t['nics'] = [{'key': n.get('key', str(i)), 'mac': n['mac'], 'mac_fixed': True, 'model': 'vmxnet3', 'network': n.get('network', ''),
                      'connected': n.get('connected', True)} for i, n in enumerate(nics)]
        t['tools'] = 'open-vm-tools (first boot)' if install_tools else ''
    return t


def fixup_command(target, overlays, ins, links, install_tools, copy_ins=()):
    if target == 'proxmox':
        cmd = ['virt-v2v-in-place', '-i', 'disk', '-if', 'qcow2', '--block-driver', 'virtio-scsi']
        cmd += overlays
        for p in links:
            cmd += ['--copy-in', f'{p}:/etc/systemd/network']
        for local, remote_dir in copy_ins:
            cmd += ['--copy-in', f'{local}:{remote_dir}']
        if install_tools:
            cmd += ['--firstboot-install', 'qemu-guest-agent']
        return cmd
    cmd = ['virt-customize', '--format', 'qcow2']
    for ov in overlays:
        cmd += ['-a', ov]
    for p in links:
        cmd += ['--copy-in', f'{p}:/etc/systemd/network']
    for local, remote_dir in copy_ins:
        cmd += ['--copy-in', f'{local}:{remote_dir}']
    distro = ins.get('distro', '')
    if distro in DEBIAN_FAMILY or ins.get('package_management') == 'apt':
        for mod in ('vmw_pvscsi', 'vmxnet3', 'mptspi', 'mptsas'):
            cmd += ['--append-line', f'/etc/initramfs-tools/modules:{mod}']
        cmd += ['--run-command', 'update-initramfs -u -k all']
    elif distro in RHEL_FAMILY or distro in SUSE_FAMILY or ins.get('package_management') in ('dnf', 'yum', 'zypper'):
        cmd += ['--write', '/etc/dracut.conf.d/90-hvbackup.conf:add_drivers+=" vmw_pvscsi vmxnet3 mptspi mptsas "\n',
                '--run-command', 'dracut --regenerate-all --force']
    if install_tools:
        cmd += ['--firstboot-install', 'open-vm-tools']
    return cmd


def convert(task, rp_id, target, options):
    """Make a restore point of `target` platform from restore point rp_id. Returns the new id.
    options: new_identity (fresh MACs and UUID, default True), install_tools (guest agent or
    open-vm-tools at first boot, default False), name (VM name on the target)."""
    if target not in TARGETS:
        raise RuntimeError(f'unknown target platform {target!r}')
    rep = repo()
    with db.conn() as c:
        rp = db.row_to_dict(c.execute('SELECT * FROM restore_points WHERE id=? AND deleted=0', (rp_id,)).fetchone())
        if not rp:
            raise RuntimeError('restore point not found')
        vm_row = db.row_to_dict(c.execute('SELECT * FROM vms WHERE id=?', (rp['vm_id'],)).fetchone())
    manifest = rep.read_manifest(rp['manifest'])
    spec = manifest['vm']
    src_platform = manifest.get('platform') or spec.get('platform') or 'vmware'
    if src_platform == target:
        raise RuntimeError(f'restore point {rp_id} already is a {target} restore point')
    if spec.get('vmtype') == 'lxc' or spec.get('firmware') == 'container':
        raise RuntimeError('containers cannot be converted into VMs')
    if guest_is_windows(spec):
        raise RuntimeError(f'{spec.get("guest_name") or spec.get("guest_id")}: Windows guests are not converted in this release (Linux only)')
    if spec.get('tpm'):
        task.log('WARNING: the source VM has a TPM; it is not carried over')
    if spec.get('secure_boot'):
        task.log('WARNING: secure boot was enabled on the source; the copy boots with secure boot off')
    check_tools(target)
    new_identity = options.get('new_identity', True)
    install_tools = bool(options.get('install_tools'))
    cs = manifest['chunk_size']
    name = options.get('name') or spec.get('name') or vm_row['name']
    task.log(f'converting restore point {rp_id} of {vm_row["name"]} ({src_platform}) to {target}: {len(manifest["disks"])} disk(s), '
             f'{sum(d["capacity"] for d in manifest["disks"]) / 2**30:.0f} GiB')
    work = tempfile.mkdtemp(prefix=f'convert-{rp_id}-', dir=rep.staging)
    exports, overlays, qnbd = [], [], []
    try:
        # 1. export every disk from the repository and put an overlay on it
        disks = []
        for i, d in enumerate(manifest['disks']):
            hashes = rep.read_index(d['index'])
            ex = nbd.Export(nbd.IndexSource(rep, hashes, d['capacity']), os.path.join(work, f'd{i}.sock'), 'disk').start()
            exports.append(ex)
            ov = os.path.join(work, f'd{i}.qcow2')
            run(task, ['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'raw', '-b', ex.uri(), ov])
            overlays.append(ov)
            disks.append((d, hashes, ov))
        task.check_cancel()

        # 2. look inside
        task.progress(0.05, 'inspecting the guest')
        ins = inspect_guest(task, overlays)
        if ins['type'] != 'linux':
            raise RuntimeError(f'the guest is {ins["type"] or "unknown"} ({ins.get("product") or "?"}); only Linux guests are converted in this release')
        task.log(f'guest: {ins["product"] or ins["distro"]} ({ins["distro"]} {ins["major"]}.{ins["minor"]}, {ins["arch"]}, '
                 f'packages via {ins["package_management"] or "?"}, hostname {ins["hostname"] or "?"})')
        net_files = guest_net_files(task, overlays, work)
        names = nic_names_in(net_files)
        nics = []
        for n in spec.get('nics') or []:
            mac = n.get('mac') if not new_identity else ''
            nics.append({**n, 'mac': mac or new_mac(target)})
        if names:
            task.log(f'interface names in the guest configuration: {", ".join(names)}; pinned to the {"new" if new_identity else "original"} MAC addresses')
        elif nics:
            task.log('no interface names found in the guest configuration (DHCP on any name assumed); none pinned')
        links = link_files(work, nics, names)
        # configs that match the NIC by its old MAC address follow the new one
        patched = rewrite_macs(net_files, [(o.get('mac', ''), n['mac']) for o, n in zip(spec.get('nics') or [], nics)])
        copy_ins = []
        for path, text in patched.items():
            local = os.path.join(work, 'patched', path.strip('/'))
            os.makedirs(os.path.dirname(local), exist_ok=True)
            with open(local, 'w') as f:
                f.write(text)
            copy_ins.append((local, os.path.dirname(path)))
            task.log(f'{path}: MAC address updated to the new NIC')
        task.check_cancel()

        # 3. fix the guest up for the target
        task.progress(0.1, f'adapting the guest for {target} (libguestfs)')
        cmd = fixup_command(target, overlays, ins, links, install_tools, copy_ins)
        task.log('running ' + os.path.basename(cmd[0]) + (' with first-boot install of ' + ('qemu-guest-agent' if target == 'proxmox' else 'open-vm-tools') if install_tools else ''))
        t0 = time.time()
        run(task, cmd, prefix='  ' + os.path.basename(cmd[0]) + ': ')
        task.log(f'guest fix-ups done in {time.time() - t0:.0f} s')
        task.check_cancel()

        # 4. read back only what changed and store it as new chunks
        task.progress(0.8, 'storing the changed blocks')
        uuid = str(uuidlib.uuid4()) if new_identity else (spec.get('uuid') or str(uuidlib.uuid4()))
        disks_out, changed_total, stored_total, new_chunks = [], 0, 0, 0
        for i, (d, hashes, ov) in enumerate(disks):
            m = json.loads(run(task, ['qemu-img', 'map', '--output=json', '-f', 'qcow2', ov]))
            mod, zeroed = modified_chunks(m, cs, d['capacity'])
            new_hashes = list(hashes)
            for ci in zeroed:
                new_hashes[ci] = ZERO_HASH
            changed_total += len(zeroed) * cs
            if mod:
                sock = os.path.join(work, f'q{i}.sock')
                pidf = os.path.join(work, f'q{i}.pid')
                run(task, ['qemu-nbd', '--read-only', '--persistent', '--fork', '--pid-file', pidf, '--socket', sock, '--format', 'qcow2', ov])
                qnbd.append(pidf)
                with nbd.Client(sock) as cl:
                    for ci in mod:
                        off = ci * cs
                        data = cl.read(off, min(cs, d['capacity'] - off))
                        h, stored, was_new = rep.put_raw(data)
                        new_hashes[ci] = h
                        stored_total += stored
                        new_chunks += 1 if was_new else 0
                changed_total += len(mod) * cs
            task.log(f'disk {d["key"]}: {len(mod)} chunk(s) rewritten by the fix-ups ({len(mod) * cs / 2**20:.0f} MiB), '
                     f'{len(zeroed)} discarded as free space, the other {len(hashes) - len(mod) - len(zeroed)} reused')
            disks_out.append({'key': (f'drive-scsi{i}' if target == 'proxmox' else d['key']), 'label': d.get('label', ''), 'capacity': d['capacity'],
                              'hashes': new_hashes, 'allocated': sum(1 for h in new_hashes if h != ZERO_HASH) * cs})
        task.check_cancel()

        # 5. the new restore point
        tspec = target_spec(spec, target, ins, nics, uuid, install_tools, rp_id, name)
        now = time.time()
        with db.conn() as c:
            c.execute('INSERT INTO runs(job_id,vm_id,task_id,started,finished,status,kind,transport,bytes_read,bytes_stored,chunks_new) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (None, vm_row['id'], task.id, t0, now, 'done', 'convert', target, changed_total, stored_total, new_chunks))
            run_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute('INSERT INTO restore_points(vm_id,run_id,repo_id,created,manifest,size_bytes,allocated_bytes,kind,spec_json,platform,source_rp_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (vm_row['id'], run_id, rep.id, now, '', sum(d['capacity'] for d in disks_out), sum(d['allocated'] for d in disks_out),
                       'converted', json.dumps(tspec), target, rp_id))
            new_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute('UPDATE runs SET restore_point_id=? WHERE id=?', (new_id, run_id))
        vm_uuid = vm_row.get('uuid') or spec.get('uuid') or f'vm{vm_row["id"]}'
        new_manifest = {'version': 1, 'restore_point': new_id, 'created': now, 'chunk_size': cs, 'platform': target, 'vm': tspec, 'kind': 'converted',
                        'converted_from': {'restore_point': rp_id, 'platform': src_platform, 'options': {'new_identity': new_identity, 'install_tools': install_tools},
                                           'guest': ins, 'nic_names': names, 'mac_rewritten': sorted(patched)}, 'disks': []}
        for d in disks_out:
            rel = rep.write_index(vm_uuid, new_id, d['key'], d['hashes'])
            new_manifest['disks'].append({'key': d['key'], 'capacity': d['capacity'], 'index': rel, 'label': d['label'], 'allocated': d['allocated']})
        mrel = rep.write_manifest(vm_uuid, new_id, new_manifest)
        with db.conn() as c:
            c.execute('UPDATE restore_points SET manifest=? WHERE id=?', (mrel, new_id))
        task.progress(1.0, f'restore point {new_id} written ({target})')
        task.log(f'restore point {new_id} written: {target} image of {name}, {changed_total / 2**20:.0f} MiB changed, {stored_total / 2**20:.1f} MiB new in the repository')
        return new_id
    finally:
        for pidf in qnbd:
            try:
                os.kill(int(open(pidf).read().strip()), 15)
            except (OSError, ValueError):
                pass
        for ex in exports:
            ex.stop()
        shutil.rmtree(work, ignore_errors=True)


def find_converted(rp_id, target, options):
    """An existing, still present conversion of rp_id with the same options, if any."""
    with db.conn() as c:
        rows = c.execute('SELECT id, manifest FROM restore_points WHERE source_rp_id=? AND platform=? AND deleted=0 AND manifest!=\'\' ORDER BY id DESC', (rp_id, target)).fetchall()
    rep = repo()
    want = {'new_identity': options.get('new_identity', True), 'install_tools': bool(options.get('install_tools'))}
    for r in rows:
        try:
            m = rep.read_manifest(r['manifest'])
        except OSError:
            continue
        if (m.get('converted_from') or {}).get('options') == want:
            return r['id']
    return None


def ensure_converted(task, rp, target, options):
    existing = find_converted(rp['id'], target, options)
    if existing:
        task.log(f'restore point {rp["id"]} was converted to {target} before as restore point {existing}; reusing it')
        return existing
    return convert(task, rp['id'], target, options)


def convert_task(task, restore_point_id, target, options):
    new_id = convert(task, restore_point_id, target, options or {})
    return {'restore_point': new_id, 'platform': target}

