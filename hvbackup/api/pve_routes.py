"""Endpoints the Proxmox node-side plug-in talks to (hvbackup/pve/). Authenticated with the
proxy token that was put into the cluster's `hvbackup` storage entry. The node streams
chunks through the agent endpoints (/api/v1/agent/chunks, /have); the calls here book
the run, merge the per-volume chunk indexes and write the restore point, and serve the
catalog back as Proxmox "archives" for listing and restore."""
import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from .. import config, db
from ..core import ops, tasks
from ..providers import base, proxmox
from ..store.chunks import ZERO_HASH, repo
from .deps import current_proxy

r = APIRouter(prefix='/api/v1/pve')
PVE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'pve')
NODE_FILES = {'HvbackupPlugin.pm': 'text/x-perl', 'Hvbackup.pm': 'text/x-perl', 'pve_helper.py': 'text/x-python', 'install.sh': 'text/x-shellscript'}


# ── files for the node installer (no secrets) ────────────────────────
@r.get('/files/{name}', include_in_schema=False)
def node_file(name: str):
    if name not in NODE_FILES:
        raise HTTPException(404)
    return FileResponse(os.path.join(PVE_DIR, name), media_type=NODE_FILES[name], filename=name)


@r.get('/install.sh', include_in_schema=False)
def install_script(request: Request):
    base_url = (config.PUBLIC_URL or str(request.base_url)).rstrip('/')
    text = open(os.path.join(PVE_DIR, 'install.sh')).read().replace('__HVB_URL__', base_url)
    return PlainTextResponse(text, media_type='text/x-shellscript')


# ── helpers ──────────────────────────────────────────────────────────
def _pve_conn(proxy):
    if not proxy.get('connection_id'):
        raise HTTPException(403, 'this token is not tied to a Proxmox connection')
    cr = ops.connection(proxy['connection_id'])
    if cr['type'] != 'proxmox':
        raise HTTPException(403, 'this token belongs to a non-Proxmox connection')
    return cr


def _stage_path(run_id):
    d = os.path.join(config.REPO_DIR, 'staging')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f'pve-run-{run_id}.json')


def _load_stage(run_id):
    try:
        with open(_stage_path(run_id)) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save_stage(run_id, stage):
    tmp = _stage_path(run_id) + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(stage, f)
    os.replace(tmp, _stage_path(run_id))


def _run(run_id, cr):
    with db.conn() as c:
        run = db.row_to_dict(c.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
        if not run:
            raise HTTPException(404, 'no such run')
        vm = db.row_to_dict(c.execute('SELECT * FROM vms WHERE id=?', (run['vm_id'],)).fetchone())
    if not vm or vm['connection_id'] != cr['id']:
        raise HTTPException(403, 'run belongs to another connection')
    return run, vm


def _task_of(run):
    return tasks.Task(run['task_id']) if run.get('task_id') else tasks.NullTask()


def _prev_manifest(vm_id):
    with db.conn() as c:
        prev = c.execute('SELECT manifest FROM restore_points WHERE vm_id=? AND deleted=0 AND manifest!="" ORDER BY id DESC LIMIT 1', (vm_id,)).fetchone()
    if not prev:
        return None
    try:
        return repo().read_manifest(prev[0])
    except Exception:  # noqa: BLE001
        return None


# ── status ───────────────────────────────────────────────────────────
@r.get('/status')
def status(proxy=Depends(current_proxy)):
    _pve_conn(proxy)
    rep = repo()
    st = rep.stats()
    return {'total': st.get('total_bytes', 0), 'avail': st.get('free_bytes', 0), 'used': st.get('stored_bytes', 0), 'repo_id': rep.id}


# ── backup transaction ───────────────────────────────────────────────
@r.post('/backup/init')
async def backup_init(request: Request, proxy=Depends(current_proxy)):
    cr = _pve_conn(proxy)
    body = await request.json()
    vmtype, vmid = body.get('vmtype'), int(body.get('vmid'))
    if vmtype not in ('qemu', 'lxc'):
        raise HTTPException(400, 'vmtype must be qemu or lxc')
    moref = f'{vmtype}/{vmid}'
    with db.conn() as c:
        vm = db.row_to_dict(c.execute('SELECT * FROM vms WHERE connection_id=? AND moref=?', (cr['id'], moref)).fetchone())
    if not vm:
        # a guest created after the last inventory refresh: refresh now so the spec is recorded with the backup
        try:
            ops.refresh_inventory(tasks.NullTask(), cr['id'])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f'guest {moref} is not in the inventory and the refresh failed: {e}')
        with db.conn() as c:
            vm = db.row_to_dict(c.execute('SELECT * FROM vms WHERE connection_id=? AND moref=?', (cr['id'], moref)).fetchone())
        if not vm:
            raise HTTPException(404, f'guest {moref} is not in the inventory of {cr["name"]}')
    with db.conn() as c:
        run = db.row_to_dict(c.execute("SELECT * FROM runs WHERE vm_id=? AND status='running' ORDER BY id DESC LIMIT 1", (vm['id'],)).fetchone())
    if run and run.get('task_id'):
        st = tasks.get(run['task_id'])
        if not st or st['state'] != 'running':
            run = None
    if not run:
        t = tasks.create_external('backup_vm', subject=vm['name'], created_by=f'proxmox:{body.get("node") or cr["name"]}')
        with db.conn() as c:
            c.execute('INSERT INTO runs(job_id,vm_id,task_id,started,status) VALUES(?,?,?,?,?)', (None, vm['id'], t.id, time.time(), 'running'))
            run = db.row_to_dict(c.execute('SELECT * FROM runs WHERE id=?', (c.execute('SELECT last_insert_rowid()').fetchone()[0],)).fetchone())
    task = _task_of(run)
    task.log(f'node {body.get("node") or "?"} started a backup of {vm["name"]} ({moref}) through the Proxmox backup job')
    _save_stage(run['id'], {'vmtype': vmtype, 'vmid': vmid, 'node': body.get('node'), 'volumes': {}, 'started': time.time()})
    return {'run_id': run['id'], 'task_id': run['task_id'], 'archive_name': proxmox.volname_for(vmtype, vmid, run['id']), 'vm_name': vm['name']}


@r.post('/backup/{run_id}/incremental')
async def backup_incremental(run_id: int, request: Request, proxy=Depends(current_proxy)):
    cr = _pve_conn(proxy)
    run, vm = _run(run_id, cr)
    body = await request.json()
    prev = _prev_manifest(vm['id'])
    have = {d['key']: d for d in (prev or {}).get('disks', [])}
    out = {}
    for dev, info in (body.get('volumes') or {}).items():
        p = have.get(dev)
        out[dev] = 'use' if p and int(p.get('capacity', -1)) == int(info.get('size', -2)) else 'new'
    _task_of(run).log('bitmaps: ' + ', '.join(f'{d}={m}' for d, m in out.items()))
    return out


@r.put('/backup/{run_id}/volume/{dev}')
async def backup_volume(run_id: int, dev: str, request: Request, size: int, bitmap_mode: str = 'none', proxy=Depends(current_proxy)):
    """Body: packed (chunk index, hash) records for the chunks the node read. With bitmap_mode
    'reuse' the rest of the disk is taken from the previous restore point's index."""
    cr = _pve_conn(proxy)
    run, vm = _run(run_id, cr)
    rep = repo()
    cs = rep.chunk_size
    n = (size + cs - 1) // cs
    hashes = [ZERO_HASH] * n
    if bitmap_mode == 'reuse':
        prev = _prev_manifest(vm['id'])
        pd = next((d for d in (prev or {}).get('disks', []) if d['key'] == dev), None)
        if not pd or int(pd.get('capacity', -1)) != size:
            raise HTTPException(409, f'no previous index for {dev} of size {size}; redo as a full backup')
        old = rep.read_index(pd['index'])
        hashes[:len(old)] = old[:n]
    body = await request.body()
    records = base.unpack_records(body)
    for idx, h in records:
        if idx < n:
            hashes[idx] = h
    missing = [h.hex() for h in set(hashes) if h != ZERO_HASH and not rep.has(h)]
    if missing:
        raise HTTPException(409, f'{len(missing)} chunks referenced by {dev} are not in the repository (first {missing[0][:12]}…)')
    stage = _load_stage(run_id)
    stage.setdefault('volumes', {})[dev] = {'capacity': size, 'bitmap_mode': bitmap_mode, 'records': len(records),
                                            'hashes': [h.hex() for h in hashes]}
    _save_stage(run_id, stage)
    allocated = sum(1 for h in hashes if h != ZERO_HASH) * cs
    _task_of(run).log(f'{dev}: {len(records)} chunks reported ({bitmap_mode}), {allocated / 2**30:.1f} GiB allocated of {size / 2**30:.1f} GiB')
    return {'chunks': len(records), 'allocated': allocated}


@r.get('/backup/{run_id}/prev/{dev}')
def backup_prev_index(run_id: int, dev: str, proxy=Depends(current_proxy)):
    """The previous restore point's chunk index for a device: the node composes partially
    dirty chunks from it."""
    cr = _pve_conn(proxy)
    run, vm = _run(run_id, cr)
    prev = _prev_manifest(vm['id'])
    pd = next((d for d in (prev or {}).get('disks', []) if d['key'] == dev), None)
    if not pd:
        raise HTTPException(404, 'no previous index')
    return {'size': pd['capacity'], 'index': [h.hex() for h in repo().read_index(pd['index'])]}


@r.post('/backup/{run_id}/meta')
async def backup_meta(run_id: int, request: Request, proxy=Depends(current_proxy)):
    """Facts the forked container backup cannot carry back to backup_cleanup in Perl:
    the guest config and the transfer statistics."""
    cr = _pve_conn(proxy)
    _run(run_id, cr)
    body = await request.json()
    stage = _load_stage(run_id)
    stage.setdefault('meta', {}).update({k: v for k, v in body.items() if v is not None})
    _save_stage(run_id, stage)
    return {'ok': True}


@r.post('/backup/{run_id}/log')
async def backup_log(run_id: int, request: Request, proxy=Depends(current_proxy)):
    cr = _pve_conn(proxy)
    run, _ = _run(run_id, cr)
    text = (await request.body()).decode('utf-8', 'replace')
    task = _task_of(run)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for ln in lines[-60:]:
        task.log('pve: ' + ln)
    return {'ok': True, 'lines': len(lines)}


@r.post('/backup/{run_id}/finish')
async def backup_finish(run_id: int, request: Request, proxy=Depends(current_proxy)):
    cr = _pve_conn(proxy)
    run, vm = _run(run_id, cr)
    body = await request.json()
    task = _task_of(run)
    stage = _load_stage(run_id)
    external = bool(run.get('task_id')) and (tasks.get(run['task_id']) or {}).get('created_by', '').startswith('proxmox:')
    if not body.get('success'):
        err = str(body.get('error') or 'failed on the node')[:1000]
        task.log(f'FAILED: {err}')
        with db.conn() as c:
            c.execute("UPDATE runs SET finished=?, status='failed', error=? WHERE id=? AND status='running'", (time.time(), err, run_id))
        if external:
            tasks.finish_external(run['task_id'], 'failed', error=err)
        try:
            os.remove(_stage_path(run_id))
        except OSError:
            pass
        return {'ok': True, 'status': 'failed'}
    vols = stage.get('volumes') or {}
    if not vols:
        raise HTTPException(400, 'no volumes were reported for this run')
    rep = repo()
    cs = rep.chunk_size
    spec = dict(vm.get('spec') or (json.loads(vm['spec_json']) if vm.get('spec_json') else {}))  # row_to_dict parses spec_json into 'spec'
    spec.setdefault('platform', 'proxmox')
    spec.setdefault('name', vm['name'])
    spec.setdefault('placement', {'host': stage.get('node'), 'vmid': stage.get('vmid')})
    spec['vmtype'] = stage.get('vmtype') or spec.get('vmtype')
    spec['vmid'] = stage.get('vmid') or spec.get('vmid')
    meta = stage.get('meta') or {}
    spec['pve_config'] = body.get('guest_config') or meta.get('guest_config') or ''
    spec['pve_firewall'] = body.get('firewall_config') or meta.get('firewall_config') or None
    for k in ('bytes_read', 'bytes_uploaded', 'chunks_new'):
        if not body.get(k) and meta.get(k):
            body[k] = meta[k]
    kind = 'incremental' if any(v.get('bitmap_mode') == 'reuse' for v in vols.values()) else 'full'
    disks = []
    for dev, v in vols.items():
        hashes = [bytes.fromhex(h) for h in v['hashes']]
        disks.append({'key': dev, 'capacity': v['capacity'], 'hashes': hashes, 'label': dev.replace('drive-', ''),
                      'allocated': sum(1 for h in hashes if h != ZERO_HASH) * cs})
    allocated = sum(d['allocated'] for d in disks)
    with db.conn() as c:
        c.execute('INSERT INTO restore_points(vm_id,run_id,repo_id,created,manifest,size_bytes,allocated_bytes,kind,spec_json) VALUES(?,?,?,?,?,?,?,?,?)',
                  (vm['id'], run_id, rep.id, time.time(), '', sum(d['capacity'] for d in disks), allocated, kind, json.dumps(spec)))
        rp_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    vm_uuid = vm.get('uuid') or spec.get('uuid') or f'vm{vm["id"]}'
    manifest = {'version': 1, 'restore_point': rp_id, 'created': time.time(), 'chunk_size': cs, 'platform': 'proxmox',
                'vm': spec, 'kind': kind, 'disks': []}
    for d in disks:
        rel = rep.write_index(vm_uuid, rp_id, d['key'], d['hashes'])
        manifest['disks'].append({'key': d['key'], 'capacity': d['capacity'], 'index': rel, 'label': d['label'], 'allocated': d['allocated']})
    mrel = rep.write_manifest(vm_uuid, rp_id, manifest)
    bytes_read = int(body.get('bytes_read') or sum(v.get('records', 0) * cs for v in vols.values()))
    with db.conn() as c:
        c.execute('UPDATE restore_points SET manifest=? WHERE id=?', (mrel, rp_id))
        c.execute("UPDATE runs SET finished=?, status='done', kind=?, transport='pve', bytes_read=?, bytes_stored=?, chunks_new=?, restore_point_id=? WHERE id=?",
                  (time.time(), kind, bytes_read, int(body.get('bytes_uploaded') or 0), int(body.get('chunks_new') or 0), rp_id, run_id))
    task.log(f'restore point {rp_id} written: {kind}, {bytes_read / 2**20:.0f} MiB read, {allocated / 2**30:.1f} GiB allocated')
    if external:
        tasks.finish_external(run['task_id'], 'done', result={'vm': vm['name'], 'status': 'done', 'restore_point': rp_id, 'kind': kind,
                                                               'transport': 'pve', 'bytes_read': bytes_read})
    try:
        os.remove(_stage_path(run_id))
    except OSError:
        pass
    return {'ok': True, 'restore_point': rp_id, 'archive_size': allocated, 'kind': kind}


# ── archives: the catalog as Proxmox sees it ─────────────────────────
def _archives(vmid=None):
    with db.conn() as c:
        rows = c.execute('''SELECT rp.id, rp.run_id, rp.created, rp.size_bytes, rp.allocated_bytes, rp.kind, rp.platform, rp.spec_json,
                                   v.name, v.moref, c.name AS cname, c.type AS ctype
                            FROM restore_points rp JOIN vms v ON v.id=rp.vm_id JOIN connections c ON c.id=v.connection_id
                            WHERE rp.deleted=0 AND rp.run_id IS NOT NULL AND rp.manifest!=''
                              AND (rp.platform='proxmox' OR (rp.platform IS NULL AND c.type='proxmox')) ORDER BY rp.id''').fetchall()
    out = []
    for x in rows:
        if x['platform'] == 'proxmox' and x['ctype'] != 'proxmox':
            # converted from another platform: the archive's ids live in the spec
            try:
                sp = json.loads(x['spec_json'] or '{}')
            except ValueError:
                sp = {}
            vmtype, vid = sp.get('vmtype') or 'qemu', str(sp.get('vmid') or 0)
            origin = f'converted from {(sp.get("converted_from") or {}).get("platform") or "?"}'
        else:
            if '/' not in (x['moref'] or ''):
                continue
            vmtype, vid = x['moref'].split('/', 1)
            origin = x['cname']
        if vmtype not in ('qemu', 'lxc') or not int(vid) or (vmid is not None and int(vid) != int(vmid)):
            continue
        out.append({'volname': proxmox.volname_for(vmtype, int(vid), x['run_id']), 'vmid': int(vid), 'vmtype': vmtype,
                    'ctime': int(x['created']), 'size': x['allocated_bytes'], 'restore_point': x['id'], 'run_id': x['run_id'],
                    'notes': f'{x["name"]} · {x["kind"]} · hvbackup restore point {x["id"]} ({origin})'})
    return out


@r.get('/archives')
def archives(vmid: int = None, proxy=Depends(current_proxy)):
    _pve_conn(proxy)
    return _archives(vmid)


def _rp_by_run(run_id):
    with db.conn() as c:
        rp = db.row_to_dict(c.execute('SELECT * FROM restore_points WHERE run_id=? AND deleted=0', (run_id,)).fetchone())
    if not rp:
        raise HTTPException(404, 'no restore point for that archive')
    return rp


@r.delete('/archives/{run_id}')
def delete_archive(run_id: int, proxy=Depends(current_proxy)):
    _pve_conn(proxy)
    rp = _rp_by_run(run_id)
    ops.delete_restore_point(rp['id'])
    db.audit(f'proxy:{proxy["name"]}', 'restore_point_delete', f'{rp["id"]} (from Proxmox)')
    return {'ok': True}


@r.get('/archives/{run_id}/config')
def archive_config(run_id: int, proxy=Depends(current_proxy)):
    _pve_conn(proxy)
    rp = _rp_by_run(run_id)
    m = repo().read_manifest(rp['manifest'])
    spec = m.get('vm') or {}
    return {'vmtype': spec.get('vmtype'), 'vmid': spec.get('vmid'), 'guest_config': spec.get('pve_config') or '',
            'firewall_config': spec.get('pve_firewall'), 'name': spec.get('name')}


@r.get('/archives/{run_id}/volumes')
def archive_volumes(run_id: int, proxy=Depends(current_proxy)):
    _pve_conn(proxy)
    rp = _rp_by_run(run_id)
    rep = repo()
    m = rep.read_manifest(rp['manifest'])
    return {'chunk_size': m['chunk_size'], 'vmtype': (m.get('vm') or {}).get('vmtype'),
            'volumes': {d['key']: {'size': d['capacity'], 'allocated': d.get('allocated', 0), 'index': [h.hex() for h in rep.read_index(d['index'])]}
                        for d in m['disks']}}
