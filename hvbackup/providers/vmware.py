"""vSphere provider: vCenter or a standalone ESXi host.

Backup of a running VM = quiesced snapshot + CBT + the snapshot's base disk hot-added
to a proxy VM whose agent streams chunks to us. Powered-off VMs can also be read
directly over the datastore HTTPS endpoint with no proxy. Restore = thin disks
created with VirtualDiskManager, filled through the proxy (hot-add, persistent)
or by HTTPS PUT, then CreateVM from the neutral spec.

All vSphere API calls happen here, on the server; the proxy never sees credentials.
"""
import json
import logging
import os
import ssl
import threading
import time
import urllib.parse

import requests
import urllib3
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

from .. import config, db
from ..core import uuids, work
from ..core.tasks import Cancelled
from ..store.chunks import ZERO_HASH, repo
from . import base, vsphere_plugin as vsp

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger('hvbackup.vmware')
_proxy_locks = {}
_proxy_locks_guard = threading.Lock()

CONTROLLER_TYPES = {
    vim.vm.device.ParaVirtualSCSIController: 'pvscsi',
    vim.vm.device.VirtualLsiLogicSASController: 'lsilogicsas',
    vim.vm.device.VirtualLsiLogicController: 'lsilogic',
    vim.vm.device.VirtualBusLogicController: 'buslogic',
    vim.vm.device.VirtualAHCIController: 'sata',
    vim.vm.device.VirtualNVMEController: 'nvme',
    vim.vm.device.VirtualIDEController: 'ide',
}
CONTROLLER_CLASSES = {v: k for k, v in CONTROLLER_TYPES.items()}
NIC_TYPES = {vim.vm.device.VirtualVmxnet3: 'vmxnet3', vim.vm.device.VirtualE1000e: 'e1000e',
             vim.vm.device.VirtualE1000: 'e1000', vim.vm.device.VirtualVmxnet2: 'vmxnet2'}
NIC_CLASSES = {v: k for k, v in NIC_TYPES.items()}


def proxy_lock(proxy_id):
    with _proxy_locks_guard:
        return _proxy_locks.setdefault(proxy_id, threading.Lock())


def wait_task(task, what, cancel_check=None):
    while task.info.state in (vim.TaskInfo.State.queued, vim.TaskInfo.State.running):
        if cancel_check and cancel_check():
            try:
                task.CancelTask()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.5)
    if task.info.state != vim.TaskInfo.State.success:
        msg = task.info.error.msg if task.info.error else 'unknown error'
        raise RuntimeError(f'{what} failed: {msg}')
    return task.info.result


class VMware:
    def __init__(self, host, username, password, verify_ssl=False):
        self.host, self.username, self.password, self.verify = host, username, password, verify_ssl
        self.si = None

    def __enter__(self):
        kw = {}
        if not self.verify:
            kw['disableSslCertValidation'] = True
        try:
            self.si = SmartConnect(host=self.host, user=self.username, pwd=self.password, **kw)
        except TypeError:
            self.si = SmartConnect(host=self.host, user=self.username, pwd=self.password,
                                   sslContext=ssl._create_unverified_context())
        self.content = self.si.RetrieveContent()
        self.is_vcenter = self.content.about.apiType == 'VirtualCenter'
        return self

    def __exit__(self, *a):
        try:
            Disconnect(self.si)
        except Exception:  # noqa: BLE001
            pass

    # ── generic helpers ──────────────────────────────────────────────
    def about(self):
        a = self.content.about
        return {'name': a.fullName, 'version': a.version, 'build': a.build, 'api_type': a.apiType,
                'is_vcenter': a.apiType == 'VirtualCenter', 'instance_uuid': a.instanceUuid}

    def collect(self, vimtype, props):
        view = self.content.viewManager.CreateContainerView(self.content.rootFolder, [vimtype], True)
        try:
            ts = vmodl.query.PropertyCollector.TraversalSpec(name='tv', path='view', skip=False, type=vim.view.ContainerView)
            os_ = vmodl.query.PropertyCollector.ObjectSpec(obj=view, skip=True, selectSet=[ts])
            ps = vmodl.query.PropertyCollector.PropertySpec(type=vimtype, pathSet=props)
            fs = vmodl.query.PropertyCollector.FilterSpec(objectSet=[os_], propSet=[ps])
            pc = self.content.propertyCollector
            out = []
            res = pc.RetrievePropertiesEx([fs], vmodl.query.PropertyCollector.RetrieveOptions())
            while res:
                for o in res.objects:
                    d = {'obj': o.obj}
                    for p in o.propSet:
                        d[p.name] = p.val
                    out.append(d)
                if res.token:
                    res = pc.ContinueRetrievePropertiesEx(res.token)
                else:
                    break
            return out
        finally:
            view.Destroy()

    def vm_by_moref(self, moref):
        return vim.VirtualMachine(moref, self.si._stub)

    def vm_by_uuid(self, bios_uuid):
        for r in self.collect(vim.VirtualMachine, ['config.uuid']):
            if uuids.same_vm(r.get('config.uuid', ''), bios_uuid):
                return r['obj']
        return None

    def http_host(self, host):
        """Address for datastore HTTP on this host. Through vCenter the host's inventory name
        is what vCenter resolves (an IP or FQDN); a standalone host often calls itself
        'localhost.localdomain', so there we use the address the connection was made to."""
        return host.name if self.is_vcenter else self.host

    def datacenter_of(self, obj):
        while obj and not isinstance(obj, vim.Datacenter):
            obj = obj.parent
        return obj

    def placement(self):
        """Hosts (with their datastores and networks), datastore space and VM folders of this
        connection: everything a restore dialog needs to place a copy, including one that
        came from another vCenter."""
        hosts = []
        for h in self.collect(vim.HostSystem, ['name', 'datastore', 'network', 'parent', 'runtime.connectionState', 'runtime.inMaintenanceMode']):
            hosts.append({'name': h['name'], 'datastores': [d.name for d in (h.get('datastore') or [])],
                          'networks': [n.name for n in (h.get('network') or [])],
                          'cluster': h['parent'].name if h.get('parent') and isinstance(h['parent'], vim.ClusterComputeResource) else '',
                          'connection_state': str(h.get('runtime.connectionState') or 'connected'),
                          'maintenance': bool(h.get('runtime.inMaintenanceMode'))})
        datastores = {}
        for d in self.collect(vim.Datastore, ['name', 'summary.capacity', 'summary.freeSpace', 'summary.type', 'summary.accessible']):
            datastores[d['name']] = {'capacity': d.get('summary.capacity') or 0, 'free': d.get('summary.freeSpace') or 0,
                                     'type': d.get('summary.type') or '', 'accessible': bool(d.get('summary.accessible', True))}
        folders = [{'moref': f['obj']._moId, 'name': f['name']} for f in self.collect(vim.Folder, ['name', 'childType']) if 'VirtualMachine' in (f.get('childType') or [])]
        return {'hosts': hosts, 'datastores': datastores, 'folders': folders, 'is_vcenter': self.is_vcenter}

    # ── inventory ────────────────────────────────────────────────────
    def inventory(self):
        nets = {r['obj']._moId: r.get('name', '') for r in self.collect(vim.Network, ['name'])}
        hosts = {r['obj']._moId: r.get('name', '') for r in self.collect(vim.HostSystem, ['name'])}
        out = []
        for r in self.collect(vim.VirtualMachine, ['name', 'config', 'runtime.powerState', 'runtime.host',
                                                   'guest.toolsRunningStatus', 'guest.guestFullName', 'guest.ipAddress',
                                                   'network', 'parent', 'resourcePool', 'snapshot', 'datastore']):
            cfg = r.get('config')
            if cfg is None or r['name'].startswith('vCLS-'):
                continue
            host = r.get('runtime.host')
            spec = self.spec_from_config(cfg, r, nets, hosts)
            disks = spec['disks']
            out.append({
                'moref': r['obj']._moId, 'name': r['name'], 'uuid': cfg.uuid, 'power_state': str(r.get('runtime.powerState')),
                'guest': r.get('guest.guestFullName') or cfg.guestFullName, 'tools': r.get('guest.toolsRunningStatus'),
                'cbt_enabled': bool(cfg.changeTrackingEnabled), 'is_template': bool(cfg.template),
                'host': hosts.get(host._moId, '') if host else '', 'folder': r['parent']._moId if r.get('parent') else '',
                'disks': disks, 'spec': spec, 'ip': r.get('guest.ipAddress'), 'has_snapshots': r.get('snapshot') is not None,
            })
        return out

    def spec_from_config(self, cfg, r, nets, hosts):
        spec = base.empty_spec('vmware')
        spec.update({'name': cfg.name, 'cpus': cfg.hardware.numCPU, 'cores_per_socket': cfg.hardware.numCoresPerSocket or 1,
                     'memory_mb': cfg.hardware.memoryMB, 'firmware': cfg.firmware or 'bios',
                     'secure_boot': bool(cfg.bootOptions and cfg.bootOptions.efiSecureBootEnabled),
                     'guest_id': cfg.guestId, 'guest_name': cfg.guestFullName, 'uuid': cfg.uuid,
                     'platform_id': r['obj']._moId, 'hw_version': cfg.version, 'tools': str(r.get('guest.toolsRunningStatus') or '')})
        ctls = {}
        for d in cfg.hardware.device:
            for cls, name in CONTROLLER_TYPES.items():
                if isinstance(d, cls):
                    ctls[d.key] = (name, d.busNumber)
                    break
            if isinstance(d, vim.vm.device.VirtualTPM):
                spec['tpm'] = True
        vm_nets = {n._moId: nets.get(n._moId, '') for n in (r.get('network') or [])}
        for d in cfg.hardware.device:
            if isinstance(d, vim.vm.device.VirtualDisk):
                ctype, bus = ctls.get(d.controllerKey, ('unknown', 0))
                b = d.backing
                spec['disks'].append({'key': str(d.key), 'label': d.deviceInfo.label if d.deviceInfo else '',
                                      'capacity': d.capacityInBytes, 'controller': ctype, 'bus': bus, 'unit': d.unitNumber,
                                      'thin': bool(getattr(b, 'thinProvisioned', False)), 'file': getattr(b, 'fileName', ''),
                                      'mode': getattr(b, 'diskMode', 'persistent'),
                                      'datastore': getattr(b, 'fileName', '').split(']')[0].lstrip('[') if getattr(b, 'fileName', '') else ''})
            elif isinstance(d, vim.vm.device.VirtualEthernetCard):
                model = next((n for c, n in NIC_TYPES.items() if isinstance(d, c)), 'e1000')
                b = d.backing
                if isinstance(b, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
                    net = next((nm for mo, nm in vm_nets.items()), '')
                    for mo, nm in vm_nets.items():
                        pg = vim.dvs.DistributedVirtualPortgroup(mo, self.si._stub)
                        try:
                            if pg.key == b.port.portgroupKey:
                                net = nm
                                break
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    net = getattr(b, 'deviceName', '') or ''
                spec['nics'].append({'key': str(d.key), 'mac': d.macAddress, 'model': model, 'network': net,
                                     'connected': bool(d.connectable and d.connectable.startConnected)})
        host = r.get('runtime.host')
        spec['placement'] = {'host': hosts.get(host._moId, '') if host else '', 'host_moref': host._moId if host else '',
                             'resource_pool': r['resourcePool']._moId if r.get('resourcePool') else '',
                             'folder': r['parent']._moId if r.get('parent') else '',
                             'datastores': [ds.name for ds in (r.get('datastore') or [])],
                             'vmx': cfg.files.vmPathName if cfg.files else ''}
        return spec

    # ── snapshots / CBT ──────────────────────────────────────────────
    def ensure_cbt(self, vm, task):
        if not vm.config.changeTrackingEnabled:
            task.log('enabling changed block tracking')
            wait_task(vm.ReconfigVM_Task(vim.vm.ConfigSpec(changeTrackingEnabled=True)), 'enable CBT')

    def changed_areas(self, vm, snap, disk, change_id):
        offset, cap, out = 0, disk.capacityInBytes, []
        while offset < cap:
            info = vm.QueryChangedDiskAreas(snapshot=snap, deviceKey=disk.key, startOffset=offset, changeId=change_id)
            out.extend((a.start, a.length) for a in info.changedArea)
            if info.length == 0:
                break
            offset = info.startOffset + info.length
        return out

    # ── hot-add ──────────────────────────────────────────────────────
    def proxy_vm(self, proxy_row):
        vm = None
        if proxy_row.get('vm_moref'):
            try:
                vm = self.vm_by_moref(proxy_row['vm_moref'])
                vm.name  # noqa: B018  probe existence
            except Exception:  # noqa: BLE001
                vm = None
        if vm is None and proxy_row.get('bios_uuid'):
            vm = self.vm_by_uuid(proxy_row['bios_uuid'])
            if vm is not None:
                with db.conn() as c:
                    c.execute('UPDATE proxies SET vm_moref=? WHERE id=?', (vm._moId, proxy_row['id']))
        if vm is None:
            raise RuntimeError(f'proxy {proxy_row["name"]} is not a VM on this vSphere (bios uuid {proxy_row.get("bios_uuid")})')
        self.cleanup_proxy(vm)
        return vm

    def cleanup_proxy(self, proxy):
        """Detach disks left over from an interrupted run: anything non-persistent, or any
        persistent disk whose file lives outside the proxy's own folder (a restore in flight)."""
        own_dir = proxy.config.files.vmPathName.rsplit('/', 1)[0] if proxy.config.files else ''
        stale = [d for d in proxy.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)
                 and (getattr(d.backing, 'diskMode', '') != 'persistent' or not d.backing.fileName.startswith(own_dir + '/'))]
        for d in stale:
            log.warning('proxy %s: detaching leftover disk %s', proxy.name, d.backing.fileName)
            self.hot_remove(proxy, d)

    def hot_add(self, proxy, descriptor, mode):
        ctl = next(d for d in proxy.config.hardware.device if isinstance(d, vim.vm.device.VirtualSCSIController))
        used = {d.unitNumber for d in proxy.config.hardware.device if getattr(d, 'controllerKey', None) == ctl.key}
        unit = next(u for u in range(16) if u != 7 and u not in used)
        disk = vim.vm.device.VirtualDisk(key=-1, controllerKey=ctl.key, unitNumber=unit,
                                         backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=descriptor, diskMode=mode))
        wait_task(proxy.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=[vim.vm.device.VirtualDeviceSpec(operation='add', device=disk)])), 'hot-add')
        added = next(d for d in proxy.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)
                     and d.controllerKey == ctl.key and d.unitNumber == unit)
        return added, ctl.busNumber, unit

    def hot_remove(self, proxy, added):
        # never with a fileOperation: the file belongs to another VM
        wait_task(proxy.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=[vim.vm.device.VirtualDeviceSpec(operation='remove', device=added)])), 'hot-remove')

    # ── datastore HTTPS ──────────────────────────────────────────────
    def ds_url(self, host, path):
        ds, rel = path.split('] ', 1)
        rel = '/'.join(urllib.parse.quote(p) for p in rel.split('/'))
        dc = 'ha-datacenter'
        return f'https://{host}/folder/{rel}?dcPath={dc}&dsName={urllib.parse.quote(ds.lstrip("["))}'

    def ticket(self, method, url):
        spec = vim.SessionManager.HttpServiceRequestSpec(method=method, url=url)
        return self.content.sessionManager.AcquireGenericServiceTicket(spec).id

    def http_get(self, host, path, start=None, length=None, stream=True):
        url = self.ds_url(host, path)
        headers = {'Range': f'bytes={start}-{start + length - 1}'} if start is not None else {}
        r = requests.get(url, headers=headers, cookies={'vmware_cgi_ticket': self.ticket('httpGet', url)}, stream=stream,
                         verify=False, timeout=300)
        if start is not None and r.status_code != 206:
            raise RuntimeError(f'range read of {path} failed: HTTP {r.status_code} (a running VM holds the lock; use a proxy)')
        r.raise_for_status()
        return r

    def flat_extent(self, host, descriptor):
        text = self.http_get(host, descriptor, stream=False).text
        ds, rel = descriptor.split('] ', 1)
        d = rel.rsplit('/', 1)[0] if '/' in rel else ''
        ext = [ln.split()[3].strip('"') for ln in text.splitlines() if ln.split()[:1] and ln.split()[0] in ('RW', 'RDONLY')]
        if len(ext) != 1:
            raise RuntimeError(f'{descriptor}: expected one extent, found {ext}')
        return f'{ds}] {d + "/" if d else ""}{ext[0]}'


# ═══════════════════════════════════════════════════════════════════════
# backup
# ═══════════════════════════════════════════════════════════════════════
def _prev_state(vm_id):
    with db.conn() as c:
        rp = db.row_to_dict(c.execute('SELECT * FROM restore_points WHERE vm_id=? AND deleted=0 ORDER BY id DESC LIMIT 1', (vm_id,)).fetchone())
        cbt = {r['disk_key']: dict(r) for r in c.execute('SELECT * FROM cbt_state WHERE vm_id=?', (vm_id,))}
    return rp, cbt


def backup_vm(task, api: VMware, vm_row, run_id, proxy_row, quiesce=True):
    """Returns (restore_point_id, stats)."""
    rep = repo()
    cs = rep.chunk_size
    vm = api.vm_by_moref(vm_row['moref'])
    task.log(f'backup {vm.name} ({vm.runtime.powerState}) on {vm.runtime.host.name if vm.runtime.host else "?"}')
    if vm.runtime.consolidationNeeded:
        task.log('WARNING: VM reports consolidation needed; snapshot chain may be dirty')
    if vm.snapshot and any(s.name.startswith('hvbackup') for s in _walk_snaps(vm.snapshot.rootSnapshotList)):
        task.log('removing a stale hvbackup snapshot from a previous run')
        for s in _walk_snaps(vm.snapshot.rootSnapshotList):
            if s.name.startswith('hvbackup'):
                wait_task(s.snapshot.RemoveSnapshot_Task(removeChildren=False, consolidate=True), 'remove stale snapshot')
    api.ensure_cbt(vm, task)
    vct = vsp.VcTask(api, vm, vsp.TASK_BACKUP)
    task.vc_task = vct
    prev_rp, prev_cbt = _prev_state(vm_row['id'])
    prev_manifest = rep.read_manifest(prev_rp['manifest']) if prev_rp else None
    prev_idx = {d['key']: d for d in prev_manifest['disks']} if prev_manifest else {}

    q = quiesce and vm.guest.toolsRunningStatus == 'guestToolsRunning' and vm.runtime.powerState == 'poweredOn'
    task.log(f'creating snapshot (quiesce={q})')
    t0 = time.time()
    snap = wait_task(vm.CreateSnapshot_Task(name='hvbackup', description=f'hvbackup run {run_id}', memory=False, quiesce=q),
                     'snapshot', task.cancelled)
    task.log(f'snapshot ready in {time.time() - t0:.1f}s')
    stats = {'bytes_read': 0, 'bytes_stored': 0, 'chunks_new': 0, 'kind': 'incremental', 'transport': None}
    disks_out = []
    powered_off = vm.runtime.powerState != 'poweredOn'
    try:
        snap_disks = [d for d in snap.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]
        total_chunks = 0
        plans = []
        for d in snap_disks:
            key = str(d.key)
            new_change = getattr(d.backing, 'changeId', None)
            if not new_change:
                raise RuntimeError('snapshot carries no changeId; CBT not active yet, run again')
            since = '*'
            prev = prev_cbt.get(key)
            if prev and prev_rp and prev['restore_point_id'] == prev_rp['id'] and key in prev_idx and prev_idx[key]['capacity'] == d.capacityInBytes:
                since = prev['change_id']
            try:
                areas = api.changed_areas(vm, snap, d, since)
            except Exception as e:  # noqa: BLE001
                if since == '*':
                    raise
                task.log(f'disk {key}: CBT query since {since[:12]}.. failed ({type(e).__name__}); CBT was reset, taking a full')
                since = '*'
                areas = api.changed_areas(vm, snap, d, since)
            ranges = base.extents_to_chunk_ranges(areas, cs, d.capacityInBytes)
            n = base.ranges_chunk_count(ranges)
            total_chunks += n
            if since == '*':
                stats['kind'] = 'full'
            descriptor = d.backing.parent.fileName if d.backing.parent else d.backing.fileName
            task.log(f'disk {key}: {d.capacityInBytes / 2**30:.1f} GiB, {"full" if since == "*" else "incremental"}, {n} chunks ({n * cs / 2**20:.0f} MiB) to read')
            plans.append((d, key, since, new_change, ranges, descriptor))

        done_chunks = 0
        for d, key, since, new_change, ranges, descriptor in plans:
            task.check_cancel()
            n = base.ranges_chunk_count(ranges)
            if prev_idx.get(key) and since != '*':
                hashes = rep.read_index(prev_idx[key]['index'])
                hashes += [ZERO_HASH] * (((d.capacityInBytes + cs - 1) // cs) - len(hashes))
            else:
                hashes = [ZERO_HASH] * ((d.capacityInBytes + cs - 1) // cs)
            records = []
            if n:
                def prog(done_bytes, _base=done_chunks, _n=n):
                    frac = (_base + done_bytes / cs) / max(total_chunks, 1)
                    task.progress(frac, f'disk {key}: {done_bytes / 2**20:.0f} MiB read')
                    vct.progress(frac * 100)
                if proxy_row is not None:
                    stats['transport'] = 'hotadd'
                    records = _read_via_proxy(task, api, proxy_row, descriptor, d.capacityInBytes, ranges, cs, prog)
                elif powered_off:
                    stats['transport'] = 'https'
                    records = _read_via_https(task, api, vm, descriptor, d.capacityInBytes, ranges, cs, prog)
                else:
                    raise RuntimeError('VM is powered on and no proxy is available on this connection; deploy a proxy VM')
            for idx, h in records:
                if idx < len(hashes):
                    hashes[idx] = h
            done_chunks += n
            stats['bytes_read'] += n * cs
            stats['bytes_stored'] = getattr(task, 'stored', 0)
            disks_out.append({'key': key, 'capacity': d.capacityInBytes, 'hashes': hashes, 'change_id': new_change,
                              'label': d.deviceInfo.label if d.deviceInfo else ''})
    finally:
        t1 = time.time()
        try:
            wait_task(snap.RemoveSnapshot_Task(removeChildren=False, consolidate=True), 'remove snapshot')
            for _ in range(120):
                if not vm.runtime.consolidationNeeded:
                    break
                time.sleep(1)
            task.log(f'snapshot removed in {time.time() - t1:.1f}s' + (' (consolidation still pending)' if vm.runtime.consolidationNeeded else ''))
        except Exception as e:  # noqa: BLE001
            task.log(f'WARNING: snapshot removal failed: {e}; it will be retried on the next run')

    # ── write manifest + catalog ─────────────────────────────────────
    spec = json.loads(vm_row['spec_json']) if vm_row.get('spec_json') else vm_row.get('spec') or {}
    allocated = sum(sum(1 for h in d['hashes'] if h != ZERO_HASH) * cs for d in disks_out)
    with db.conn() as c:
        c.execute('INSERT INTO restore_points(vm_id,run_id,repo_id,created,manifest,size_bytes,allocated_bytes,kind,spec_json) VALUES(?,?,?,?,?,?,?,?,?)',
                  (vm_row['id'], run_id, rep.id, time.time(), '', sum(d['capacity'] for d in disks_out), allocated, stats['kind'], json.dumps(spec)))
        rp_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    vm_uuid = vm_row.get('uuid') or spec.get('uuid') or f'vm{vm_row["id"]}'
    manifest = {'version': 1, 'restore_point': rp_id, 'created': time.time(), 'chunk_size': cs, 'platform': 'vmware',
                'vm': spec, 'kind': stats['kind'], 'disks': []}
    for d in disks_out:
        rel = rep.write_index(vm_uuid, rp_id, d['key'], d['hashes'])
        manifest['disks'].append({'key': d['key'], 'capacity': d['capacity'], 'index': rel, 'label': d['label'],
                                  'allocated': sum(1 for h in d['hashes'] if h != ZERO_HASH) * cs})
    mrel = rep.write_manifest(vm_uuid, rp_id, manifest)
    with db.conn() as c:
        c.execute('UPDATE restore_points SET manifest=? WHERE id=?', (mrel, rp_id))
        for d in disks_out:
            c.execute('INSERT OR REPLACE INTO cbt_state(vm_id,disk_key,change_id,restore_point_id) VALUES(?,?,?,?)',
                      (vm_row['id'], d['key'], d['change_id'], rp_id))
    task.log(f'restore point {rp_id} written: {stats["kind"]}, {stats["bytes_read"] / 2**20:.0f} MiB read, {allocated / 2**30:.1f} GiB allocated')
    vct.done()
    vsp.post_event(api, vm, vsp.EVENT_DONE, f'hvbackup backup of {vm.name} completed ({stats["kind"]})', stats['kind'])
    return rp_id, stats


def _walk_snaps(tree):
    for s in tree:
        yield s
        yield from _walk_snaps(s.childSnapshotList)


def _read_via_proxy(task, api, proxy_row, descriptor, size, ranges, cs, prog):
    with proxy_lock(proxy_row['id']):
        proxy = api.proxy_vm(proxy_row)
        task.log(f'hot-adding {descriptor} to proxy {proxy.name}')
        added, bus, unit = api.hot_add(proxy, descriptor, 'independent_nonpersistent')
        try:
            wid = work.enqueue(proxy_row['id'], 'read', {'size': size, 'bus': bus, 'unit': unit, 'chunk_size': cs,
                                                         'ranges': ranges, 'workers': 4}, task_id=task.id)
            w = work.wait(task, wid, base.ranges_bytes(ranges, cs), 'read', on_progress=prog)
            with open(work.records_path(wid), 'rb') as f:
                records = base.unpack_records(f.read())
            os.remove(work.records_path(wid))
            res = w.get('result') or {}
            task.log(f'proxy read {len(records)} chunks in {res.get("seconds", 0):.0f}s, {res.get("uploaded", 0) / 2**20:.0f} MiB uploaded')
            task.stored = getattr(task, 'stored', 0) + int(res.get('uploaded', 0))
            return records
        finally:
            api.hot_remove(proxy, added)


def _read_via_https(task, api, vm, descriptor, size, ranges, cs, prog):
    rep = repo()
    host = api.http_host(vm.runtime.host)
    flat = api.flat_extent(host, descriptor)
    task.log(f'reading {flat} over HTTPS from {host} (powered-off VM, no proxy needed)')
    records = []
    done = 0
    for first, count in ranges:
        idx = first
        while idx < first + count:
            n = min(count - (idx - first), 16)  # 64 MiB per request
            start = idx * cs
            length = min(n * cs, size - start)
            r = api.http_get(host, flat, start, length)
            buf = r.content
            if len(buf) < n * cs:
                buf += bytes(n * cs - len(buf))
            for i in range(n):
                chunk = buf[i * cs:(i + 1) * cs]
                h, stored, _ = rep.put_raw(chunk)
                task.stored = getattr(task, 'stored', 0) + stored
                records.append((idx + i, h))
            idx += n
            done += n * cs
            prog(done)
            task.check_cancel()
    return records


# ═══════════════════════════════════════════════════════════════════════
# restore
# ═══════════════════════════════════════════════════════════════════════
def _usable_host(h):
    try:
        return h.runtime.connectionState == 'connected' and not h.runtime.inMaintenanceMode
    except Exception:  # noqa: BLE001
        return True


def _ds_writable(ds, host):
    """Accessible from this host and mounted read-write (ISO shares are usually read-only)."""
    try:
        if not ds.summary.accessible:
            return False
        for m in ds.host:
            if m.key == host:
                return (m.mountInfo.accessMode or 'readWrite') == 'readWrite' and m.mountInfo.accessible is not False
    except Exception:  # noqa: BLE001
        pass
    return True


def resolve_target(api, spec, manifest, options, log):
    """Where a restore lands on THIS connection: host, datastore, folder and a network per NIC.
    The restore point may come from another vCenter (the DR case): whatever the source
    placement names that is not here is remapped when there is one sensible choice, and
    refused with the list of candidates when there are several, so a copy never silently
    lands on the wrong storage or VLAN. Explicit options always win and must exist."""
    placement = spec.get('placement') or {}
    hosts = {r['name']: r['obj'] for r in api.collect(vim.HostSystem, ['name'])}
    if not hosts:
        raise RuntimeError('this connection has no hosts')
    src_ds = (spec['disks'][0]['datastore'] if spec.get('disks') else '') or (placement.get('datastores') or [''])[0]
    need = sum(d.get('allocated', 0) for d in manifest['disks'])

    # host
    want = options.get('host')
    if want:
        if want not in hosts:
            raise RuntimeError(f'host {want!r} is not on this connection (hosts: {", ".join(sorted(hosts))})')
        host = hosts[want]
    elif placement.get('host') in hosts:
        host = hosts[placement['host']]
    else:
        usable = [h for h in hosts.values() if _usable_host(h)]
        with_ds = [h for h in usable if any(d.name == src_ds for d in h.datastore)]
        pick = with_ds if len(with_ds) == 1 else usable if len(usable) == 1 else []
        if not pick:
            raise RuntimeError(f'the restore point came from host {placement.get("host") or "?"}, which is not on this connection; '
                               f'choose a host (one of: {", ".join(sorted(h.name for h in usable) or sorted(hosts))})')
        host = pick[0]
        log(f'source host {placement.get("host") or "?"} is not on this connection; using {host.name}')
    if not _usable_host(host):
        log(f'WARNING: host {host.name} is {host.runtime.connectionState}{" and in maintenance mode" if host.runtime.inMaintenanceMode else ""}')

    # datastore
    by_name = {d.name: d for d in host.datastore}
    want = options.get('datastore')
    if want:
        if want not in by_name:
            raise RuntimeError(f'datastore {want!r} is not reachable from host {host.name} (it has: {", ".join(sorted(by_name))})')
        ds = by_name[want]
    elif src_ds in by_name and _ds_writable(by_name[src_ds], host):
        ds = by_name[src_ds]
    else:
        cands = [d for d in by_name.values() if _ds_writable(d, host) and d.summary.freeSpace >= need]
        if not cands:
            raise RuntimeError(f'datastore {src_ds!r} is not on host {host.name} and no other datastore there has {need / 2**30:.1f} GiB free '
                               f'(it has: {", ".join(sorted(by_name))})')
        ds = max(cands, key=lambda d: d.summary.freeSpace)
        log(f'datastore {src_ds!r} is not on host {host.name}; using {ds.name} ({ds.summary.freeSpace / 2**30:.0f} GiB free)')
    if ds.summary.freeSpace < need:
        log(f'WARNING: {ds.name} has {ds.summary.freeSpace / 2**30:.1f} GiB free, the restore writes {need / 2**30:.1f} GiB')

    # folder
    dc = api.datacenter_of(host)
    folder = None
    if options.get('folder'):
        try:
            f = vim.Folder(options['folder'], api.si._stub)
            if 'VirtualMachine' in f.childType and api.datacenter_of(f) == dc:
                folder = f
        except Exception:  # noqa: BLE001
            folder = None
        if folder is None:
            log(f'folder {options["folder"]!r} is not a VM folder of this datacenter; using the root VM folder')
    if folder is None:
        folder = dc.vmFolder

    # networks: one target per source network
    host_nets = {n.name: n for n in host.network}
    mapping = dict(options.get('networks') or {})
    nets = {}
    for n in spec.get('nics') or []:
        src = n.get('network') or ''
        if src in nets:
            continue
        dst = mapping.get(src, src)
        if dst in host_nets:
            nets[src] = host_nets[dst]
        elif dst != src:
            raise RuntimeError(f'network {dst!r} is not on host {host.name} (it has: {", ".join(sorted(host_nets))})')
        elif len(host_nets) == 1:
            nets[src] = next(iter(host_nets.values()))
            log(f'network {src!r} is not on host {host.name}; using {nets[src].name}')
        else:
            raise RuntimeError(f'{"network " + repr(src) if src else "a NIC with no recorded network"} is not on host {host.name}; '
                               f'map {src!r} to one of: {", ".join(sorted(host_nets))}')
    return {'host': host, 'datastore': ds, 'folder': folder, 'datacenter': dc, 'pool': host.parent.resourcePool, 'networks': nets}


def restore_vm(task, api: VMware, rp, options, proxy_row):
    """options: name, power_on, new_identity (fresh MACs), host (name), datastore, folder (moref),
    networks ({source network: target network}). Anything unset is taken from the restore
    point and remapped by resolve_target when the target is a different vCenter or host."""
    rep = repo()
    manifest = rep.read_manifest(rp['manifest'])
    spec = manifest['vm']
    cs = manifest['chunk_size']
    name = options.get('name') or spec['name']
    t = resolve_target(api, spec, manifest, options, task.log)
    host, ds, folder, dc, pool = t['host'], t['datastore'], t['folder'], t['datacenter'], t['pool']
    host_name = host.name
    for r in api.collect(vim.VirtualMachine, ['name']):
        if r['name'] == name:
            raise RuntimeError(f'a VM named {name!r} already exists')

    vm_folder = f'[{ds.name}] {name}'
    vct = vsp.VcTask(api, host, vsp.TASK_RESTORE)
    task.vc_task = vct
    api.content.fileManager.MakeDirectory(name=vm_folder, datacenter=dc, createParentDirectories=True)
    task.log(f'restoring {spec["name"]} as {name} on {host_name} / {ds.name}')
    try:
        return _restore_into(task, api, rp, options, proxy_row, rep, manifest, spec, cs, name, host, ds, folder, dc, pool, t, vm_folder, vct)
    except BaseException as e:
        # MakeDirectory above refuses an existing folder, so this one is ours: a cancelled
        # or failed restore leaves no half-written disks behind on the datastore.
        task.log(f'restore {"cancelled" if isinstance(e, Cancelled) else "failed"}; removing {vm_folder}')
        try:
            wait_task(api.content.fileManager.DeleteDatastoreFile_Task(name=vm_folder, datacenter=dc), 'remove partial files')
        except Exception as e2:  # noqa: BLE001
            task.log(f'WARNING: could not remove {vm_folder}: {e2}')
        raise


def _restore_into(task, api, rp, options, proxy_row, rep, manifest, spec, cs, name, host, ds, folder, dc, pool, t, vm_folder, vct):
    host_name = host.name
    total = sum(d['allocated'] for d in manifest['disks'])
    done = 0
    created = []
    for i, d in enumerate(manifest['disks']):
        task.check_cancel()
        path = f'{vm_folder}/{name}{"" if i == 0 else "_" + str(i)}.vmdk'
        adapter = 'lsiLogic'
        dspec = vim.VirtualDiskManager.FileBackedVirtualDiskSpec(diskType='thin', adapterType=adapter, capacityKb=d['capacity'] // 1024)
        wait_task(api.content.virtualDiskManager.CreateVirtualDisk_Task(name=path, datacenter=dc, spec=dspec), 'create disk')
        created.append((d, path))
        hashes = rep.read_index(d['index'])
        records = [(i2, h) for i2, h in enumerate(hashes) if h != ZERO_HASH]

        def prog(b, _base=done):
            task.progress((_base + b) / max(total, 1), f'disk {d["key"]}: {b / 2**20:.0f} MiB written')
            vct.progress((_base + b) / max(total, 1) * 100)
        if proxy_row is not None:
            _write_via_proxy(task, api, proxy_row, path, d['capacity'], records, cs, prog)
        else:
            # the upload sends the whole capacity, zero blocks included; scale it to the
            # allocated bytes the progress bar is measured in
            scale = d['allocated'] / max(d['capacity'], 1)
            _write_via_https(task, api, api.http_host(host), path, d['capacity'], hashes, cs, lambda b: prog(b * scale))
        task.check_cancel()
        done += d['allocated']

    # ── build the VM ─────────────────────────────────────────────────
    dev_changes = []
    ctl_keys = {}
    key = -100
    for d, _ in created:
        sd = next((x for x in spec['disks'] if x['key'] == d['key']), {})
        ctype, bus = sd.get('controller', 'pvscsi'), sd.get('bus', 0)
        if ctype in ('ide', 'unknown'):
            ctype = 'pvscsi'
        if (ctype, bus) not in ctl_keys:
            cls = CONTROLLER_CLASSES[ctype]
            ctl = cls(key=key, busNumber=bus)
            if isinstance(ctl, vim.vm.device.VirtualSCSIController):
                ctl.sharedBus = 'noSharing'
            dev_changes.append(vim.vm.device.VirtualDeviceSpec(operation='add', device=ctl))
            ctl_keys[(ctype, bus)] = key
            key -= 1
    used_units = {}
    for d, path in created:
        sd = next((x for x in spec['disks'] if x['key'] == d['key']), {})
        ctype, bus = sd.get('controller', 'pvscsi'), sd.get('bus', 0)
        if ctype in ('ide', 'unknown'):
            ctype = 'pvscsi'
        ck = ctl_keys[(ctype, bus)]
        unit = sd.get('unit', 0)
        while (ck, unit) in used_units or (ctype not in ('sata', 'nvme') and unit == 7):
            unit += 1
        used_units[(ck, unit)] = True
        disk = vim.vm.device.VirtualDisk(key=key, controllerKey=ck, unitNumber=unit,
                                         backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=path, diskMode='persistent'))
        dev_changes.append(vim.vm.device.VirtualDeviceSpec(operation='add', device=disk))
        key -= 1
    for n in spec['nics']:
        net = t['networks'].get(n.get('network') or '')
        cls = NIC_CLASSES.get(n['model'], vim.vm.device.VirtualVmxnet3)
        nic = cls(key=key)
        if isinstance(net, vim.dvs.DistributedVirtualPortgroup):
            nic.backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
                port=vim.dvs.PortConnection(switchUuid=net.config.distributedVirtualSwitch.uuid, portgroupKey=net.key))
        elif net is not None:
            nic.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=net.name, network=net)
        nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo(startConnected=n.get('connected', True), allowGuestControl=True)
        if options.get('new_identity', True) and not n.get('mac_fixed'):
            nic.addressType = 'generated'
        else:
            nic.addressType = 'manual'
            nic.macAddress = n['mac']
        dev_changes.append(vim.vm.device.VirtualDeviceSpec(operation='add', device=nic))
        key -= 1
    cfg = vim.vm.ConfigSpec(name=name, memoryMB=spec['memory_mb'], numCPUs=spec['cpus'], numCoresPerSocket=spec.get('cores_per_socket', 1),
                            guestId=spec['guest_id'], firmware=spec.get('firmware', 'bios'),
                            files=vim.vm.FileInfo(vmPathName=vm_folder), deviceChange=dev_changes,
                            bootOptions=vim.vm.BootOptions(efiSecureBootEnabled=bool(spec.get('secure_boot'))),
                            annotation=f'Restored by hvbackup from restore point {rp["id"]} of {spec["name"]}')
    if not options.get('new_identity', True) and spec.get('uuid'):
        cfg.uuid = spec['uuid']
    vm = wait_task(folder.CreateVM_Task(config=cfg, pool=pool, host=host), 'create VM')
    task.log(f'created VM {vm.name} ({vm._moId})')
    if options.get('power_on'):
        wait_task(vm.PowerOnVM_Task(), 'power on')
        task.log('powered on')
    vct.done()
    return {'moref': vm._moId, 'name': vm.name}


def _write_via_proxy(task, api, proxy_row, descriptor, size, records, cs, prog):
    with proxy_lock(proxy_row['id']):
        proxy = api.proxy_vm(proxy_row)
        task.log(f'hot-adding {descriptor} to proxy {proxy.name} for writing')
        added, bus, unit = api.hot_add(proxy, descriptor, 'persistent')
        try:
            wid = work.enqueue(proxy_row['id'], 'write', {'size': size, 'bus': bus, 'unit': unit, 'chunk_size': cs, 'workers': 4}, task_id=task.id)
            with open(work.records_path(wid), 'wb') as f:
                f.write(base.pack_records(records))
            work.wait(task, wid, len(records) * cs, 'write', on_progress=prog)
            try:
                os.remove(work.records_path(wid))
            except FileNotFoundError:
                pass
        finally:
            api.hot_remove(proxy, added)


class _ChunkStream:
    """File-like body for the HTTPS PUT: chunks fetched from the repository on demand,
    cancel checked once per chunk so a cancelled restore stops the upload."""

    def __init__(self, rep, hashes, cs, size, prog, task=None):
        self.rep, self.hashes, self.cs, self.size, self.prog, self.task = rep, hashes, cs, size, prog, task
        self.i = 0
        self.buf = b''
        self.sent = 0

    def __len__(self):
        return self.size

    def read(self, n=-1):
        while len(self.buf) < n and self.i < len(self.hashes):
            if self.task is not None and self.i % 8 == 0:
                self.task.check_cancel()
            self.buf += self.rep.get(self.hashes[self.i])
            self.i += 1
        if self.sent + n > self.size:
            n = self.size - self.sent
        out, self.buf = self.buf[:n], self.buf[n:]
        self.sent += len(out)
        self.prog(self.sent)
        return out


def _write_via_https(task, api, host, descriptor, size, hashes, cs, prog):
    flat = api.flat_extent(host, descriptor)
    url = api.ds_url(host, flat)
    task.log(f'uploading {flat} over HTTPS to {host} (no proxy)')
    r = requests.put(url, data=_ChunkStream(repo(), hashes, cs, size, prog, task), headers={'Content-Type': 'application/octet-stream'},
                     cookies={'vmware_cgi_ticket': api.ticket('httpPut', url)}, verify=False, timeout=24 * 3600)
    if r.status_code not in (200, 201):
        raise RuntimeError(f'upload failed: HTTP {r.status_code}')


# ═══════════════════════════════════════════════════════════════════════
# proxy VM deployment (OVA import through OvfManager + HttpNfcLease)
# ═══════════════════════════════════════════════════════════════════════
class _CountingReader:
    """File-like wrapper that reports upload progress and honours task cancellation."""

    def __init__(self, f, size, prog, task):
        self.f, self.size, self.prog, self.task, self.done = f, size, prog, task, 0

    def __len__(self):
        return self.size

    def read(self, n=-1):
        if n is None or n < 0:
            n = 1 << 20
        buf = self.f.read(n)
        self.done += len(buf)
        if self.done % (16 << 20) < len(buf) or not buf:
            self.prog(self.done)
            self.task.check_cancel()
        return buf


def deploy_ova(task, api: VMware, ova_path, name, target, properties, cpus, memory_mb, annotation=''):
    """Import an OVA onto this connection and return the new VM (powered off). `target` is a
    resolve_target() result; `properties` are OVF (vApp) property values keyed by ovf:key.
    All OVF networks are mapped to the single target network. Disks are thin."""
    import re
    import tarfile
    host, ds, folder, pool = target['host'], target['datastore'], target['folder'], target['pool']
    net = next(iter(target['networks'].values()), None)
    tar = tarfile.open(ova_path)
    members = {m.name: m for m in tar.getmembers()}
    ovf_name = next((n for n in members if n.lower().endswith('.ovf')), None)
    if not ovf_name:
        raise RuntimeError(f'{ova_path} has no .ovf descriptor')
    ovf = tar.extractfile(members[ovf_name]).read().decode('utf-8')
    ovf_nets = re.findall(r'<Network\s+ovf:name="([^"]+)"', ovf) or ['VM Network']
    known = set(re.findall(r'<Property[^>]*ovf:key="([^"]+)"', ovf))
    props = [vim.KeyValue(key=k, value=v) for k, v in properties.items() if k in known]
    missing = [k for k in properties if k not in known]
    if missing:
        task.log(f'WARNING: the image has no OVF properties {missing}; cloud-init will not see them')
    cisp = vim.OvfManager.CreateImportSpecParams(
        entityName=name, hostSystem=host, diskProvisioning='thin',
        networkMapping=[vim.OvfManager.NetworkMapping(name=n, network=net) for n in ovf_nets] if net is not None else [],
        propertyMapping=props)
    spec = api.content.ovfManager.CreateImportSpec(ovf, pool, ds, cisp)
    if spec.error:
        raise RuntimeError('OVF import spec: ' + '; '.join(e.msg for e in spec.error))
    for w in spec.warning or []:
        task.log(f'OVF warning: {w.msg}')
    lease = pool.ImportVApp(spec.importSpec, folder, host)
    while lease.state == vim.HttpNfcLease.State.initializing:
        time.sleep(0.5)
    if lease.state != vim.HttpNfcLease.State.ready:
        raise RuntimeError(f'import lease failed: {lease.error.msg if lease.error else lease.state}')
    http = api.http_host(host)
    files = []
    for fi in spec.fileItem:
        m = members.get(fi.path) or next((mm for n, mm in members.items() if n.endswith('/' + fi.path)), None)
        if m is None:
            lease.HttpNfcLeaseAbort()
            raise RuntimeError(f'{ova_path} has no member {fi.path!r} named by the descriptor')
        du = next((d for d in lease.info.deviceUrl if d.importKey == fi.deviceId), None)
        if du is None:
            lease.HttpNfcLeaseAbort()
            raise RuntimeError(f'no upload URL for {fi.path}')
        files.append((fi, m, du.url.replace('*', http)))
    total = sum(m.size for _, m, _ in files) or 1
    sent = [0]
    stop = threading.Event()

    def keepalive():
        while not stop.wait(15):
            try:
                lease.HttpNfcLeaseProgress(min(99, int(sent[0] * 100 / total)))
            except Exception:  # noqa: BLE001
                return

    th = threading.Thread(target=keepalive, name='lease-keepalive', daemon=True)
    th.start()
    try:
        for fi, m, url in files:
            task.log(f'uploading {fi.path} ({m.size / 2**20:.0f} MiB) to {http}')
            base_done = sent[0]

            def prog(b, _b=base_done):
                sent[0] = _b + b
                task.progress(0.1 + 0.6 * sent[0] / total, f'uploading image: {sent[0] / 2**20:.0f} of {total / 2**20:.0f} MiB')

            f = tar.extractfile(m)
            ctype = 'application/x-vnd.vmware-streamVmdk' if fi.path.lower().endswith('.vmdk') else 'application/octet-stream'
            r = requests.post(url, data=_CountingReader(f, m.size, prog, task),
                              headers={'Content-Type': ctype, 'Content-Length': str(m.size)}, verify=False, timeout=600)
            if r.status_code >= 300:
                raise RuntimeError(f'upload of {fi.path} failed: HTTP {r.status_code} {r.text[:200]}')
            sent[0] = base_done + m.size
        stop.set()
        lease.HttpNfcLeaseComplete()
    except BaseException:
        stop.set()
        try:
            lease.HttpNfcLeaseAbort()
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        tar.close()
    vm = lease.info.entity
    task.log(f'imported VM {vm.name} ({vm._moId})')
    cfg = vim.vm.ConfigSpec(numCPUs=cpus, memoryMB=memory_mb, annotation=annotation or None,
                            vAppConfig=vim.vApp.VmConfigSpec(ovfEnvironmentTransport=['com.vmware.guestInfo']))
    try:
        wait_task(vm.ReconfigVM_Task(cfg), 'size the VM')
    except Exception as e:  # noqa: BLE001
        task.log(f'WARNING: could not set CPU/memory/OVF transport: {e}')
    return vm


def wipe_vapp_properties(vm, keys):
    """Blank the given OVF property values (they carried first-boot secrets)."""
    vac = vm.config.vAppConfig
    if not vac:
        return
    edits = []
    for p in vac.property:
        if p.id in keys and p.value:
            p.value = ''
            edits.append(vim.vApp.PropertySpec(operation='edit', info=p))
    if edits:
        wait_task(vm.ReconfigVM_Task(vim.vm.ConfigSpec(vAppConfig=vim.vApp.VmConfigSpec(property=edits))), 'wipe OVF properties')


def guest_state(vm):
    g = vm.guest
    return {'ip': g.ipAddress, 'tools': str(g.toolsRunningStatus), 'power': str(vm.runtime.powerState)}
