"""Proxmox VE provider.

Backups of Proxmox guests are made by Proxmox itself: the server asks a node to run
vzdump against the `hvbackup` storage registered on the cluster (PVE 9 backup-provider
API). The node-side plug-in (hvbackup/pve/) reads the snapshot access, hashes 4 MiB
chunks and uploads the ones the server does not have, then the server writes the
manifest and restore point exactly as for VMware. Restores go the same way round:
the server asks the node to restore the archive `<storage>:backup/vm/<vmid>/<run>`,
and the node-side plug-in serves the disks from the repository over NBD.

The same storage makes Proxmox's own backup jobs land in this catalog, and shows the
catalog's restore points in the Proxmox UI (Storage -> Backups -> Restore).
"""
import json
import logging
import re
import time
import urllib.parse

import requests
import urllib3

from .. import config, db
from ..core.tasks import Cancelled
from . import base

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger('hvbackup.proxmox')

SIZE_UNITS = {'': 1, 'K': 2**10, 'M': 2**20, 'G': 2**30, 'T': 2**40, 'P': 2**50}
DISK_KEY_RE = re.compile(r'^(scsi|virtio|ide|sata|efidisk|tpmstate|rootfs|mp)(\d*)$')
NET_KEY_RE = re.compile(r'^net(\d+)$')
NIC_MODELS = ('virtio', 'e1000', 'e1000e', 'vmxnet3', 'rtl8139', 'ne2k_pci', 'ne2k_isa', 'pcnet', 'i82551', 'i82557b', 'i82559er')
OSTYPES = {'l26': 'Linux 2.6 - 6.x kernel', 'l24': 'Linux 2.4 kernel', 'win11': 'Windows 11/2022/2025', 'win10': 'Windows 10/2016/2019',
           'win8': 'Windows 8/2012', 'win7': 'Windows 7/2008r2', 'wxp': 'Windows XP', 'w2k': 'Windows 2000', 'solaris': 'Solaris', 'other': 'Other'}
STORAGE_TYPE = 'hvbackup'


def parse_size(s):
    m = re.fullmatch(r'(\d+(?:\.\d+)?)([KMGTP]?)', str(s or '0').strip())
    return int(float(m.group(1)) * SIZE_UNITS[m.group(2)]) if m else 0


def parse_kv(value):
    """PVE property string -> (positional first item or '', {key: value})."""
    first, opts = '', {}
    for i, part in enumerate(str(value or '').split(',')):
        if '=' in part:
            k, v = part.split('=', 1)
            opts[k.strip()] = v.strip()
        elif i == 0:
            first = part.strip()
    return first, opts


def volname_parts(volname):
    """'backup/vm/100/12' -> ('qemu', 100, 12); 'backup/ct/101/13' -> ('lxc', 101, 13)."""
    m = re.fullmatch(r'backup/(vm|ct)/(\d+)/(\d+)', volname or '')
    if not m:
        raise ValueError(f'not an hvbackup archive name: {volname!r}')
    return ('qemu' if m.group(1) == 'vm' else 'lxc'), int(m.group(2)), int(m.group(3))


def volname_for(vmtype, vmid, run_id):
    return f'backup/{"vm" if vmtype == "qemu" else "ct"}/{vmid}/{run_id}'


class Proxmox:
    def __init__(self, host, username, password, verify_ssl=False):
        self.host, self.username, self.password, self.verify = host, username, password, verify_ssl
        h = host if '://' not in host else urllib.parse.urlparse(host).netloc
        self.port = 8006
        if ':' in h and not h.startswith('['):
            h, p = h.rsplit(':', 1)
            self.port = int(p)
        self.hostname = h
        self.base = f'https://{self.hostname}:{self.port}/api2/json'
        self.s = None
        self.is_vcenter = False
        self.cluster = ''

    def __enter__(self):
        self.s = requests.Session()
        self.s.verify = bool(self.verify)
        if '!' in self.username:
            self.s.headers['Authorization'] = f'PVEAPIToken={self.username}={self.password}'
        else:
            r = self.s.post(f'{self.base}/access/ticket', data={'username': self.username, 'password': self.password}, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f'Proxmox login failed: HTTP {r.status_code} {r.reason}')
            d = r.json()['data']
            self.s.cookies.set('PVEAuthCookie', d['ticket'])
            self.s.headers['CSRFPreventionToken'] = d['CSRFPreventionToken']
        self.cluster = self._cluster_name()
        return self

    def __exit__(self, *a):
        if self.s:
            self.s.close()

    def req(self, method, path, **params):
        params = {k: (int(v) if isinstance(v, bool) else v) for k, v in params.items() if v is not None}
        kw = {'params': params} if method == 'GET' else {'data': params}
        r = self.s.request(method, self.base + path, timeout=120, **kw)
        if r.status_code >= 400:
            detail = ''
            try:
                j = r.json()
                detail = j.get('message') or ''
                if j.get('errors'):
                    detail += ' ' + json.dumps(j['errors'])
            except Exception:  # noqa: BLE001
                detail = r.text[:300]
            raise RuntimeError(f'Proxmox {method} {path}: HTTP {r.status_code} {r.reason}: {detail.strip()}')
        return r.json().get('data')

    def get(self, path, **p):
        return self.req('GET', path, **p)

    def post(self, path, **p):
        return self.req('POST', path, **p)

    def put(self, path, **p):
        return self.req('PUT', path, **p)

    def delete(self, path, **p):
        return self.req('DELETE', path, **p)

    # ── identity ─────────────────────────────────────────────────────
    def _cluster_name(self):
        for e in self.get('/cluster/status') or []:
            if e.get('type') == 'cluster':
                return e.get('name') or ''
        return self.hostname

    def nodes(self):
        return [{'name': e['name'], 'online': bool(e.get('online')), 'ip': e.get('ip')}
                for e in (self.get('/cluster/status') or []) if e.get('type') == 'node']

    def hvbackup_storages(self):
        return [s for s in (self.get('/storage') or []) if s.get('type') == STORAGE_TYPE]

    def about(self):
        v = self.get('/version') or {}
        stor = self.hvbackup_storages()
        return {'name': self.cluster or self.hostname, 'version': v.get('version', ''), 'build': v.get('repoid', ''),
                'api_type': 'proxmox', 'is_vcenter': False, 'instance_uuid': f'pve:{self.cluster or self.hostname}',
                'nodes': [n['name'] for n in self.nodes()], 'pve_storage': stor[0]['storage'] if stor else None,
                'pve_storages': [s['storage'] for s in stor]}

    # ── inventory ────────────────────────────────────────────────────
    def inventory(self):
        out = []
        for r in self.get('/cluster/resources', type='vm') or []:
            t, vmid, node = r.get('type'), r.get('vmid'), r.get('node')
            if t not in ('qemu', 'lxc') or vmid is None:
                continue
            try:
                cfg = self.get(f'/nodes/{node}/{t}/{vmid}/config')
            except RuntimeError as e:
                log.warning('config of %s/%s unreadable: %s', t, vmid, e)
                continue
            spec = self.spec_from_qemu(vmid, node, cfg, r) if t == 'qemu' else self.spec_from_lxc(vmid, node, cfg, r)
            out.append({'moref': f'{t}/{vmid}', 'name': spec['name'], 'uuid': spec['uuid'],
                        'power_state': 'poweredOn' if r.get('status') == 'running' else 'poweredOff',
                        'guest': spec['guest_name'], 'tools': spec['tools'], 'cbt_enabled': t == 'qemu',
                        'is_template': bool(r.get('template')), 'host': node, 'folder': r.get('pool') or '',
                        'disks': spec['disks'], 'spec': spec, 'ip': None, 'has_snapshots': False})
        return out

    def _disks(self, cfg, vmtype):
        disks, stores = [], []
        for key in sorted(cfg):
            m = DISK_KEY_RE.match(key)
            if not m:
                continue
            first, opts = parse_kv(cfg[key])
            if opts.get('media') == 'cdrom' or first in ('', 'none') or 'cloudinit' in first:
                continue
            kind, num = m.group(1), m.group(2)
            if vmtype == 'lxc' and kind not in ('rootfs', 'mp'):
                continue
            if vmtype == 'qemu' and kind in ('rootfs', 'mp'):
                continue
            controller = {'scsi': cfg.get('scsihw', 'lsi'), 'virtio': 'virtio-blk', 'ide': 'ide', 'sata': 'sata',
                          'efidisk': 'efi', 'tpmstate': 'tpm', 'rootfs': 'rootfs', 'mp': 'mountpoint'}[kind]
            store = first.split(':', 1)[0] if ':' in first and not first.startswith('/') else ''
            if store:
                stores.append(store)
            disks.append({'key': key, 'label': key, 'capacity': parse_size(opts.get('size', '0')), 'controller': controller,
                          'bus': 0, 'unit': int(num or 0), 'thin': True, 'file': first, 'mode': 'persistent', 'datastore': store,
                          'mountpoint': opts.get('mp', '/' if kind == 'rootfs' else '')})
        return disks, stores

    def _nics(self, cfg, vmtype):
        nics = []
        for key in sorted(cfg):
            if not NET_KEY_RE.match(key):
                continue
            first, opts = parse_kv(cfg[key])
            model, mac = 'virtio', ''
            if vmtype == 'qemu':
                for k, v in list(opts.items()):
                    if k in NIC_MODELS:
                        model, mac = k, v
                        break
                if not mac and '=' in first:
                    model, mac = first.split('=', 1)
            else:
                model, mac = 'veth', opts.get('hwaddr', '')
            nics.append({'key': key, 'mac': mac, 'model': model, 'network': opts.get('bridge', ''),
                         'vlan': opts.get('tag', ''), 'connected': opts.get('link_down') != '1', 'name': opts.get('name', '')})
        return nics

    def spec_from_qemu(self, vmid, node, cfg, res):
        spec = base.empty_spec('proxmox')
        cores, sockets = int(cfg.get('cores') or 1), int(cfg.get('sockets') or 1)
        mem = cfg.get('memory', '512')
        if '=' in str(mem):
            mem = parse_kv(mem)[1].get('current', '512')
        _, smbios = parse_kv(cfg.get('smbios1', ''))
        efi = cfg.get('efidisk0', '')
        ostype = cfg.get('ostype', '')
        disks, stores = self._disks(cfg, 'qemu')
        spec.update({'name': cfg.get('name') or f'vm{vmid}', 'cpus': cores * sockets, 'cores_per_socket': cores, 'memory_mb': int(mem),
                     'firmware': 'efi' if cfg.get('bios') == 'ovmf' else 'bios', 'secure_boot': 'pre-enrolled-keys=1' in efi,
                     'tpm': 'tpmstate0' in cfg, 'guest_id': ostype, 'guest_name': OSTYPES.get(ostype, ostype),
                     'uuid': smbios.get('uuid', ''), 'platform_id': f'qemu/{vmid}', 'hw_version': cfg.get('machine', ''),
                     'tools': 'agent' if parse_kv(cfg.get('agent', '0'))[0] in ('1', 'enabled=1') or parse_kv(cfg.get('agent', ''))[1].get('enabled') == '1' else '',
                     'disks': disks, 'nics': self._nics(cfg, 'qemu'), 'vmtype': 'qemu', 'vmid': vmid,
                     'placement': {'host': node, 'host_moref': node, 'datastores': sorted(set(stores)), 'pool': res.get('pool', ''), 'vmid': vmid},
                     'pve': {'cpu': cfg.get('cpu', ''), 'scsihw': cfg.get('scsihw', ''), 'boot': cfg.get('boot', ''), 'machine': cfg.get('machine', ''),
                             'bios': cfg.get('bios', 'seabios'), 'tags': res.get('tags', '')}})
        return spec

    def spec_from_lxc(self, vmid, node, cfg, res):
        spec = base.empty_spec('proxmox')
        disks, stores = self._disks(cfg, 'lxc')
        ostype = cfg.get('ostype', '')
        spec.update({'name': cfg.get('hostname') or f'ct{vmid}', 'cpus': int(cfg.get('cores') or 1), 'cores_per_socket': int(cfg.get('cores') or 1),
                     'memory_mb': int(cfg.get('memory') or 512), 'firmware': 'container', 'guest_id': ostype, 'guest_name': f'{ostype} container',
                     'uuid': f'pve-{self.cluster or self.hostname}-lxc-{vmid}', 'platform_id': f'lxc/{vmid}', 'tools': '',
                     'disks': disks, 'nics': self._nics(cfg, 'lxc'), 'vmtype': 'lxc', 'vmid': vmid,
                     'placement': {'host': node, 'host_moref': node, 'datastores': sorted(set(stores)), 'pool': res.get('pool', ''), 'vmid': vmid},
                     'pve': {'unprivileged': cfg.get('unprivileged', 0), 'features': cfg.get('features', ''), 'tags': res.get('tags', '')}})
        return spec

    # ── placement (restore targets) ──────────────────────────────────
    def placement(self):
        hosts, datastores = [], {}
        for n in self.nodes():
            ds, nets = [], []
            if n['online']:
                for s in self.get(f'/nodes/{n["name"]}/storage', content='images') or []:
                    if not s.get('enabled', 1):
                        continue
                    ds.append(s['storage'])
                    datastores[s['storage']] = {'capacity': s.get('total') or 0, 'free': s.get('avail') or 0, 'type': s.get('type', ''),
                                                'accessible': bool(s.get('active', 1)), 'shared': bool(s.get('shared'))}
                try:
                    nets = [b['iface'] for b in self.get(f'/nodes/{n["name"]}/network', type='any_bridge') or []]
                except RuntimeError:
                    nets = []
            hosts.append({'name': n['name'], 'datastores': ds, 'networks': sorted(nets), 'cluster': self.cluster,
                          'connection_state': 'connected' if n['online'] else 'disconnected', 'maintenance': False})
        pools = [{'moref': p['poolid'], 'name': f'pool {p["poolid"]}'} for p in (self.get('/pools') or [])]
        return {'hosts': hosts, 'datastores': datastores, 'folders': pools, 'is_vcenter': False, 'platform': 'proxmox'}

    # ── tasks (UPIDs) ────────────────────────────────────────────────
    def wait_upid(self, node, upid, task=None, prefix='pve: ', poll=2):
        """Block until the PVE task stops, mirroring its log into ours. Returns the exit status text."""
        start = 0
        while True:
            if task is not None:
                try:
                    for line in self.get(f'/nodes/{node}/tasks/{upid}/log', start=start, limit=200) or []:
                        txt = line.get('t', '')
                        if txt and txt != 'no content':
                            task.log(prefix + txt)
                        start = max(start, int(line.get('n', start)))
                except RuntimeError:
                    pass
                if task.cancelled():
                    try:
                        self.delete(f'/nodes/{node}/tasks/{upid}')
                    except RuntimeError:
                        pass
                    raise Cancelled()
            st = self.get(f'/nodes/{node}/tasks/{upid}/status') or {}
            if st.get('status') == 'stopped':
                if task is not None:
                    try:
                        for line in self.get(f'/nodes/{node}/tasks/{upid}/log', start=start, limit=500) or []:
                            txt = line.get('t', '')
                            if txt and txt != 'no content':
                                task.log(prefix + txt)
                    except RuntimeError:
                        pass
                return st.get('exitstatus', '')
            time.sleep(poll)

    def fleecing_storage(self, node, prefer=()):
        """External backup providers need fleecing; the fleecing image lives on a storage that
        can hold disk images on that node: the guest's own storage when possible, else the
        one with the most free space."""
        stores = {s['storage']: s for s in self.get(f'/nodes/{node}/storage', content='images') or [] if s.get('active', 1) and s.get('enabled', 1)}
        for p in prefer:
            if p in stores:
                return p
        if not stores:
            raise RuntimeError(f'node {node} has no active storage for disk images to hold the fleecing image')
        return max(stores.values(), key=lambda s: s.get('avail') or 0)['storage']

    def vzdump(self, node, vmid, storage, fleecing_store=None, **extra):
        fl = f'enabled=1,storage={fleecing_store}' if fleecing_store else None
        return self.post(f'/nodes/{node}/vzdump', vmid=vmid, storage=storage, mode='snapshot', remove=0, fleecing=fl, **extra)

    def next_vmid(self):
        return int(self.get('/cluster/nextid'))

    def storage_register(self, storeid, server_host, port, token, fingerprint=None):
        existing = [s for s in self.hvbackup_storages() if s['storage'] == storeid]
        fields = {'server': server_host, 'port': port, 'password': token}
        if fingerprint:
            fields['fingerprint'] = fingerprint
        if existing:
            self.put(f'/storage/{storeid}', **fields)
            return 'updated'
        self.post('/storage', storage=storeid, type=STORAGE_TYPE, content='backup', **fields)
        return 'created'


# ═══════════════════════════════════════════════════════════════════════
# server-driven operations
# ═══════════════════════════════════════════════════════════════════════
def storage_id(conn_row, api=None):
    """The hvbackup storage registered on the cluster: from the connection's recorded facts,
    or asked live when they are stale (an inventory refresh can overwrite them)."""
    info = conn_row.get('info')  # row_to_dict parses *_json columns into their bare name
    if info is None and conn_row.get('info_json'):
        info = json.loads(conn_row['info_json'])
    sid = (info or {}).get('pve_storage')
    if not sid and api is not None:
        stor = api.hvbackup_storages()
        sid = stor[0]['storage'] if stor else None
    return sid


def backup_vm(task, api: Proxmox, cr, vm_row, run_id, quiesce=True):
    """Run vzdump on the guest's node against the hvbackup storage and wait. The node-side
    plug-in fills in the run (restore point, bytes) through /api/v1/pve while this waits."""
    storeid = storage_id(cr, api)
    if not storeid:
        raise RuntimeError(f'no hvbackup storage is registered on {cr["name"]}: install the node plug-in and register the storage first (Connections -> Proxmox nodes)')
    vmtype, vmid = vm_row['moref'].split('/')
    node = vm_row.get('host') or ''
    for r in api.get('/cluster/resources', type='vm') or []:
        if r.get('type') == vmtype and str(r.get('vmid')) == vmid:
            node = r['node']
            break
    if not node:
        raise RuntimeError(f'{vmtype} {vmid} is not on the cluster any more')
    fleece = None
    if vmtype == 'qemu':
        spec = json.loads(vm_row['spec_json']) if vm_row.get('spec_json') else (vm_row.get('spec') or {})
        fleece = api.fleecing_storage(node, prefer=(spec.get('placement') or {}).get('datastores') or [])
    task.log(f'backup {vm_row["name"]} ({vmtype} {vmid}) on node {node}: vzdump to storage {storeid}' + (f', fleecing on {fleece}' if fleece else ''))
    upid = api.vzdump(node, vmid, storeid, fleecing_store=fleece)
    task.log(f'PVE task {upid}')
    status = api.wait_upid(node, upid, task)
    with db.conn() as c:
        run = db.row_to_dict(c.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
    if status != 'OK':
        raise RuntimeError(f'vzdump failed: {status}' + (f' ({run["error"]})' if run.get('error') else ''))
    if run['status'] != 'done' or not run.get('restore_point_id'):
        raise RuntimeError('vzdump reported success but the node plug-in never completed the upload on this server; check the node (pvedaemon log) and the storage token')
    stats = {'kind': run['kind'], 'transport': run['transport'] or 'pve', 'bytes_read': run['bytes_read'] or 0,
             'bytes_stored': run['bytes_stored'] or 0, 'chunks_new': run['chunks_new'] or 0}
    return run['restore_point_id'], stats


def restore_vm(task, api: Proxmox, cr, rp, options):
    """Restore a Proxmox-origin restore point onto this cluster through the PVE restore API.
    options: name, vmid, host (node), datastore (storage), networks {src bridge: dst bridge},
    new_identity, power_on."""
    from ..store.chunks import repo
    manifest = repo().read_manifest(rp['manifest'])
    spec = manifest['vm']
    if manifest.get('platform') != 'proxmox' or not spec.get('vmtype'):
        raise RuntimeError('this restore point did not come from Proxmox; restoring it onto Proxmox is a conversion, which arrives in Phase 5')
    storeid = storage_id(cr, api)
    if not storeid:
        raise RuntimeError(f'no hvbackup storage is registered on {cr["name"]}: install the node plug-in and register the storage first')
    placement = spec.get('placement') or {}
    src_vmid = int(spec.get('vmid') or placement.get('vmid') or 0)
    vmtype = spec['vmtype']
    if not src_vmid:
        with db.conn() as c:
            moref = c.execute('SELECT moref FROM vms WHERE id=?', (rp['vm_id'],)).fetchone()[0]
        src_vmid = int(moref.split('/')[1])
    archive = f'{storeid}:{volname_for(vmtype, src_vmid, rp["run_id"])}'
    nodes = {n['name']: n for n in api.nodes()}
    online = [n for n, v in nodes.items() if v['online']]
    want = options.get('host')
    if want:
        if want not in nodes:
            raise RuntimeError(f'node {want!r} is not in this cluster (nodes: {", ".join(sorted(nodes))})')
        node = want
    elif placement.get('host') in online:
        node = placement['host']
    elif len(online) == 1:
        node = online[0]
        task.log(f'source node {placement.get("host")} is not here; using {node}')
    else:
        raise RuntimeError(f'choose a node (one of: {", ".join(sorted(online))})')
    stores = {s['storage']: s for s in api.get(f'/nodes/{node}/storage', content='images') or []}
    storage = options.get('datastore')
    if storage and storage not in stores:
        raise RuntimeError(f'storage {storage!r} is not usable for disk images on node {node} (it has: {", ".join(sorted(stores))})')
    if not storage:
        src = placement.get('datastores') or []
        if len(src) == 1 and src[0] in stores:
            storage = src[0]
        elif stores:
            storage = max(stores.values(), key=lambda s: s.get('avail') or 0)['storage']
            task.log(f'using storage {storage} ({(stores[storage].get("avail") or 0) / 2**30:.0f} GiB free)')
        else:
            raise RuntimeError(f'node {node} has no storage for disk images')
    vmid = int(options.get('vmid') or 0) or api.next_vmid()
    src_name = spec.get('name') or f'{vmtype} {src_vmid}'
    name = options.get('name') or src_name
    task.log(f'restoring {src_name} ({vmtype} {src_vmid}, restore point {rp["id"]}) as {vmtype} {vmid} {name!r} on {node} / {storage} from {archive}')
    unique = bool(options.get('new_identity', True))
    if vmtype == 'qemu':
        upid = api.post(f'/nodes/{node}/qemu', vmid=vmid, archive=archive, storage=storage, unique=unique)
    else:
        upid = api.post(f'/nodes/{node}/lxc', vmid=vmid, ostemplate=archive, restore=True, storage=storage, unique=unique,
                        unprivileged=True)
    task.log(f'PVE task {upid}')
    status = api.wait_upid(node, upid, task)
    if status != 'OK':
        raise RuntimeError(f'restore failed on {node}: {status}')
    # name and network remap
    changes = {}
    if vmtype == 'qemu' and name != src_name:
        changes['name'] = name
    if vmtype == 'lxc' and name != src_name:
        changes['hostname'] = name
    mapping = options.get('networks') or {}
    if mapping:
        cfg = api.get(f'/nodes/{node}/{vmtype}/{vmid}/config')
        for key, val in cfg.items():
            if NET_KEY_RE.match(key):
                first, opts = parse_kv(val)
                src = opts.get('bridge', '')
                if src in mapping and mapping[src] and mapping[src] != src:
                    opts['bridge'] = mapping[src]
                    changes[key] = ','.join(([first] if first else []) + [f'{k}={v}' for k, v in opts.items()])
    if changes:
        api.put(f'/nodes/{node}/{vmtype}/{vmid}/config', **changes)
        task.log('applied: ' + ', '.join(f'{k}={v}' for k, v in changes.items()))
    if options.get('power_on'):
        up = api.post(f'/nodes/{node}/{vmtype}/{vmid}/status/start')
        api.wait_upid(node, up, task)
        task.log('started')
    return {'moref': f'{vmtype}/{vmid}', 'name': name, 'node': node, 'vmid': vmid}
