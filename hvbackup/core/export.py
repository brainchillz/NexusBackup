"""Export a COPY of a restore point as OVF/OVA. Read-only on the repository: the
restore point stays in the catalog and stays restorable; the export is an extra
file set under <repo>/exports/.

Steps: stream each disk's chunks into a sparse temporary raw file, convert it with
qemu-img to a streamOptimized VMDK (the compressed sparse format OVF uses), write
the OVF descriptor from the stored VM spec plus a SHA-256 manifest, then tar the
set into an OVA (PAX format, so disks over 8 GiB are fine)."""
import hashlib
import os
import shutil
import subprocess
import tarfile
import time
from xml.sax.saxutils import escape

from .. import db
from ..store.chunks import ZERO_HASH, repo
from .tasks import Cancelled

# vSphere guestId -> (OVF CIM OS id, description)
OS_IDS = {
    'ubuntu64Guest': (94, 'Ubuntu Linux (64-bit)'), 'ubuntuGuest': (93, 'Ubuntu Linux'),
    'debian10_64Guest': (96, 'Debian GNU/Linux (64-bit)'), 'debian11_64Guest': (96, 'Debian GNU/Linux (64-bit)'), 'debian12_64Guest': (96, 'Debian GNU/Linux (64-bit)'),
    'rhel8_64Guest': (80, 'Red Hat Enterprise Linux (64-bit)'), 'rhel9_64Guest': (80, 'Red Hat Enterprise Linux (64-bit)'), 'rhel10_64Guest': (80, 'Red Hat Enterprise Linux (64-bit)'),
    'centos8_64Guest': (107, 'CentOS (64-bit)'), 'centos9_64Guest': (107, 'CentOS (64-bit)'), 'rockylinux_64Guest': (107, 'Rocky Linux (64-bit)'),
    'other5xLinux64Guest': (101, 'Other Linux (64-bit)'), 'other6xLinux64Guest': (101, 'Other Linux (64-bit)'), 'otherLinux64Guest': (101, 'Other Linux (64-bit)'),
    'windows9Server64Guest': (112, 'Microsoft Windows Server 2016 or later (64-bit)'), 'windows2019srv_64Guest': (112, 'Microsoft Windows Server 2019 (64-bit)'),
    'windows2019srvNext_64Guest': (112, 'Microsoft Windows Server 2022 (64-bit)'), 'windows2022srvNext_64Guest': (112, 'Microsoft Windows Server 2025 (64-bit)'),
    'windows9_64Guest': (105, 'Microsoft Windows 10 (64-bit)'), 'windows11_64Guest': (105, 'Microsoft Windows 11 (64-bit)'),
}
CTL_SUBTYPE = {'pvscsi': (6, 'VirtualSCSI'), 'lsilogic': (6, 'lsilogic'), 'lsilogicsas': (6, 'lsilogicsas'), 'buslogic': (6, 'buslogic'),
               'sata': (20, 'vmware.sata.ahci'), 'nvme': (20, 'vmware.nvme.controller'), 'ide': (5, ''), 'unknown': (6, 'lsilogic')}
NIC_SUBTYPE = {'vmxnet3': 'VmxNet3', 'e1000e': 'E1000e', 'e1000': 'E1000', 'vmxnet2': 'VmxNet2'}


def _safe(name):
    return ''.join(ch if ch.isalnum() or ch in '-_.' else '_' for ch in name)[:64] or 'vm'


def _qemu_img():
    p = shutil.which('qemu-img')
    if not p:
        raise RuntimeError('qemu-img not found: install the qemu-utils package on the backup server')
    return p


def ovf_xml(spec, name, disks, networks):
    """disks: [{'id','file','href','size','capacity','populated','controller','bus','unit'}]"""
    os_id, os_desc = OS_IDS.get(spec.get('guest_id', ''), (101 if '64' in spec.get('guest_id', '') else 1, spec.get('guest_name') or 'Other'))
    hw = spec.get('hw_version') or 'vmx-19'
    x = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1" '
         'xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData" '
         'xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData" '
         'xmlns:vmw="http://www.vmware.com/schema/ovf" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
         '  <References>']
    for d in disks:
        x.append(f'    <File ovf:href="{escape(d["href"])}" ovf:id="{d["file"]}" ovf:size="{d["size"]}"/>')
    x += ['  </References>', '  <DiskSection>', '    <Info>Virtual disk information</Info>']
    for d in disks:
        x.append(f'    <Disk ovf:capacity="{d["capacity"]}" ovf:capacityAllocationUnits="byte" ovf:diskId="{d["id"]}" ovf:fileRef="{d["file"]}" '
                 f'ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized" ovf:populatedSize="{d["populated"]}"/>')
    x += ['  </DiskSection>', '  <NetworkSection>', '    <Info>The list of logical networks</Info>']
    for n in networks:
        x += [f'    <Network ovf:name="{escape(n)}">', f'      <Description>{escape(n)}</Description>', '    </Network>']
    x += ['  </NetworkSection>', f'  <VirtualSystem ovf:id="{escape(name)}">', '    <Info>A virtual machine</Info>', f'    <Name>{escape(name)}</Name>',
          f'    <OperatingSystemSection ovf:id="{os_id}" vmw:osType="{escape(spec.get("guest_id", ""))}">', f'      <Info>The kind of installed guest operating system</Info>',
          f'      <Description>{escape(os_desc)}</Description>', '    </OperatingSystemSection>', '    <VirtualHardwareSection>',
          '      <Info>Virtual hardware requirements</Info>', '      <System>', '        <vssd:ElementName>Virtual Hardware Family</vssd:ElementName>',
          '        <vssd:InstanceID>0</vssd:InstanceID>', f'        <vssd:VirtualSystemIdentifier>{escape(name)}</vssd:VirtualSystemIdentifier>',
          f'        <vssd:VirtualSystemType>{escape(hw)}</vssd:VirtualSystemType>', '      </System>']
    inst = 1

    def item(lines):
        nonlocal inst
        x.append('      <Item>')
        x.extend('        ' + ln for ln in lines)
        x.append('      </Item>')
        inst += 1

    cps = spec.get('cores_per_socket') or 1
    item([f'<rasd:AllocationUnits>hertz * 10^6</rasd:AllocationUnits>', '<rasd:Description>Number of Virtual CPUs</rasd:Description>',
          f'<rasd:ElementName>{spec["cpus"]} virtual CPU(s)</rasd:ElementName>', f'<rasd:InstanceID>{inst}</rasd:InstanceID>',
          '<rasd:ResourceType>3</rasd:ResourceType>', f'<rasd:VirtualQuantity>{spec["cpus"]}</rasd:VirtualQuantity>',
          f'<vmw:CoresPerSocket ovf:required="false">{cps}</vmw:CoresPerSocket>'])
    item(['<rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits>', '<rasd:Description>Memory Size</rasd:Description>',
          f'<rasd:ElementName>{spec["memory_mb"]}MB of memory</rasd:ElementName>', f'<rasd:InstanceID>{inst}</rasd:InstanceID>',
          '<rasd:ResourceType>4</rasd:ResourceType>', f'<rasd:VirtualQuantity>{spec["memory_mb"]}</rasd:VirtualQuantity>'])
    ctl_ids = {}
    for d in disks:
        key = (d['controller'], d['bus'])
        if key in ctl_ids:
            continue
        rtype, sub = CTL_SUBTYPE.get(d['controller'], CTL_SUBTYPE['unknown'])
        ctl_ids[key] = inst
        lines = [f'<rasd:Address>{d["bus"]}</rasd:Address>', f'<rasd:Description>{"SCSI" if rtype == 6 else "IDE" if rtype == 5 else "SATA/NVMe"} Controller</rasd:Description>',
                 f'<rasd:ElementName>Controller {d["bus"]}</rasd:ElementName>', f'<rasd:InstanceID>{inst}</rasd:InstanceID>']
        if sub:
            lines.append(f'<rasd:ResourceSubType>{sub}</rasd:ResourceSubType>')
        lines.append(f'<rasd:ResourceType>{rtype}</rasd:ResourceType>')
        item(lines)
    for n, d in enumerate(disks):
        item([f'<rasd:AddressOnParent>{d["unit"]}</rasd:AddressOnParent>', f'<rasd:ElementName>Hard disk {n + 1}</rasd:ElementName>',
              f'<rasd:HostResource>ovf:/disk/{d["id"]}</rasd:HostResource>', f'<rasd:InstanceID>{inst}</rasd:InstanceID>',
              f'<rasd:Parent>{ctl_ids[(d["controller"], d["bus"])]}</rasd:Parent>', '<rasd:ResourceType>17</rasd:ResourceType>'])
    for n, nic in enumerate(spec.get('nics', [])):
        item([f'<rasd:AddressOnParent>{7 + n}</rasd:AddressOnParent>', '<rasd:AutomaticAllocation>true</rasd:AutomaticAllocation>',
              f'<rasd:Connection>{escape(nic.get("network") or "VM Network")}</rasd:Connection>', f'<rasd:Description>{NIC_SUBTYPE.get(nic.get("model"), "E1000")} ethernet adapter</rasd:Description>',
              f'<rasd:ElementName>Network adapter {n + 1}</rasd:ElementName>', f'<rasd:InstanceID>{inst}</rasd:InstanceID>',
              f'<rasd:ResourceSubType>{NIC_SUBTYPE.get(nic.get("model"), "E1000")}</rasd:ResourceSubType>', '<rasd:ResourceType>10</rasd:ResourceType>'])
    if spec.get('firmware') == 'efi':
        x.append('      <vmw:Config ovf:required="false" vmw:key="firmware" vmw:value="efi"/>')
        if spec.get('secure_boot'):
            x.append('      <vmw:Config ovf:required="false" vmw:key="bootOptions.efiSecureBootEnabled" vmw:value="true"/>')
    x += ['    </VirtualHardwareSection>', f'    <AnnotationSection ovf:required="false">', '      <Info>A human-readable annotation</Info>',
          f'      <Annotation>Exported by hvbackup from a backup of {escape(spec.get("name", name))}. The backup itself is unchanged.</Annotation>',
          '    </AnnotationSection>', '  </VirtualSystem>', '</Envelope>', '']
    return '\n'.join(x)


def export_restore_point(task, restore_point_id, fmt='ova'):
    rep = repo()
    with db.conn() as c:
        rp = db.row_to_dict(c.execute('SELECT * FROM restore_points WHERE id=? AND deleted=0', (restore_point_id,)).fetchone())
        if not rp:
            raise RuntimeError('restore point not found')
        vm_row = db.row_to_dict(c.execute('SELECT name FROM vms WHERE id=?', (rp['vm_id'],)).fetchone())
        c.execute('INSERT INTO exports(restore_point_id,vm_name,format,path,size_bytes,created,task_id,status) VALUES(?,?,?,?,?,?,?,?)',
                  (rp['id'], vm_row['name'], fmt, '', 0, time.time(), task.id, 'running'))
        export_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    try:
        path, size = _do_export(task, rep, rp, vm_row['name'], fmt)
        with db.conn() as c:
            c.execute("UPDATE exports SET path=?, size_bytes=?, status='done' WHERE id=?", (path, size, export_id))
        return {'export_id': export_id, 'path': path, 'size_bytes': size}
    except BaseException as e:
        with db.conn() as c:
            c.execute("UPDATE exports SET status=?, error=? WHERE id=?", ('cancelled' if isinstance(e, Cancelled) else 'failed', str(e)[:500], export_id))
        raise


def _do_export(task, rep, rp, vm_name, fmt):
    qemu = _qemu_img()
    manifest = rep.read_manifest(rp['manifest'])
    spec = manifest['vm']
    cs = manifest['chunk_size']
    name = _safe(vm_name)
    stamp = time.strftime('%Y%m%d-%H%M', time.localtime(rp['created']))
    base = f'{name}-rp{rp["id"]}-{stamp}'
    exports = os.path.join(rep.path, 'exports')
    work = os.path.join(exports, base + ('.ovf.d' if fmt == 'ovf' else '.tmp'))
    os.makedirs(work, exist_ok=True)
    total = sum(d['allocated'] for d in manifest['disks']) or 1
    done = 0
    disks = []
    spec_disks = {d['key']: d for d in spec.get('disks', [])}
    try:
        for n, d in enumerate(manifest['disks']):
            task.check_cancel()
            hashes = rep.read_index(d['index'])
            raw = os.path.join(work, f'disk{n + 1}.raw')
            task.log(f'disk {d["key"]}: materialising {d["allocated"] / 2**30:.1f} GiB of {d["capacity"] / 2**30:.0f} GiB')
            fd = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.ftruncate(fd, d['capacity'])
            written = 0
            for i, h in enumerate(hashes):
                if h == ZERO_HASH:
                    continue
                buf = rep.get(h)
                off = i * cs
                os.pwrite(fd, buf[:max(0, min(cs, d['capacity'] - off))], off)
                written += cs
                if written % (256 * cs) == 0:
                    task.progress((done + written) / total * 0.7, f'disk {d["key"]}: {written / 2**20:.0f} MiB')
                    task.check_cancel()
            os.close(fd)
            done += d['allocated']
            href = f'{name}-disk{n + 1}.vmdk'
            vmdk = os.path.join(work, href)
            sd = spec_disks.get(d['key'], {})
            adapter = 'lsilogic' if sd.get('controller') in (None, 'unknown', 'pvscsi', 'lsilogic', 'lsilogicsas') else 'ide'
            task.log(f'disk {d["key"]}: converting to streamOptimized VMDK')
            task.progress(done / total * 0.7, f'disk {d["key"]}: converting')
            r = subprocess.run([qemu, 'convert', '-f', 'raw', '-O', 'vmdk', '-o', f'subformat=streamOptimized,adapter_type={adapter}', raw, vmdk],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f'qemu-img failed: {r.stderr.strip()[:300]}')
            os.remove(raw)
            disks.append({'id': f'vmdisk{n + 1}', 'file': f'file{n + 1}', 'href': href, 'size': os.path.getsize(vmdk), 'capacity': d['capacity'],
                          'populated': d['allocated'], 'controller': sd.get('controller', 'pvscsi'), 'bus': sd.get('bus', 0), 'unit': sd.get('unit', n)})
        networks = sorted({nic.get('network') or 'VM Network' for nic in spec.get('nics', [])}) or ['VM Network']
        ovf_name = f'{base}.ovf'
        with open(os.path.join(work, ovf_name), 'w') as f:
            f.write(ovf_xml(spec, vm_name, disks, networks))
        mf = []
        for fn in [ovf_name] + [d['href'] for d in disks]:
            h = hashlib.sha256()
            with open(os.path.join(work, fn), 'rb') as f:
                for blk in iter(lambda: f.read(8 * 2**20), b''):
                    h.update(blk)
            mf.append(f'SHA256({fn})= {h.hexdigest()}')
        with open(os.path.join(work, f'{base}.mf'), 'w') as f:
            f.write('\n'.join(mf) + '\n')
        if fmt == 'ovf':
            task.progress(1, 'OVF folder ready')
            size = sum(os.path.getsize(os.path.join(work, fn)) for fn in os.listdir(work))
            return os.path.relpath(work, rep.path), size
        ova = os.path.join(exports, base + '.ova')
        task.log('packing OVA')
        task.progress(0.8, 'packing OVA')
        with tarfile.open(ova + '.part', 'w', format=tarfile.PAX_FORMAT) as tar:
            for fn in [ovf_name, f'{base}.mf'] + [d['href'] for d in disks]:
                tar.add(os.path.join(work, fn), arcname=fn)
        os.replace(ova + '.part', ova)
        shutil.rmtree(work, ignore_errors=True)
        size = os.path.getsize(ova)
        task.progress(1, f'OVA ready: {size / 2**30:.2f} GiB')
        task.log(f'export written: {ova} ({size / 2**30:.2f} GiB); the restore point is unchanged')
        return os.path.relpath(ova, rep.path), size
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def delete_export(export_id):
    rep = repo()
    with db.conn() as c:
        e = db.row_to_dict(c.execute('SELECT * FROM exports WHERE id=?', (export_id,)).fetchone())
        if not e:
            return
        c.execute('DELETE FROM exports WHERE id=?', (export_id,))
    if e['path']:
        p = os.path.join(rep.path, e['path'])
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)
