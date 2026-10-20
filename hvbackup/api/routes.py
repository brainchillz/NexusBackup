"""REST API, versioned under /api/v1. Every mutating call returns a task id or the
changed object; the UI and CLI are plain clients of these routes."""
import json
import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import APP_VERSION, config, db
from ..core import auth, ops, scheduler, secrets, tasks
from ..store.chunks import repo
from .deps import COOKIE, current_user, require

r = APIRouter(prefix='/api/v1')


def _rows(c, q, args=()):
    return [db.row_to_dict(x) for x in c.execute(q, args)]


# ── auth ─────────────────────────────────────────────────────────────
class Login(BaseModel):
    username: str
    password: str


@r.post('/auth/login')
def login(body: Login, response: Response):
    u = auth.authenticate_password(body.username, body.password)
    if not u:
        db.audit(body.username, 'login_failed')
        raise HTTPException(401, 'bad username or password')
    response.set_cookie(COOKIE, auth.make_session(u['username']), httponly=True, samesite='lax', max_age=auth.SESSION_TTL,
                        secure=True)
    db.audit(u['username'], 'login')
    return {'username': u['username'], 'role': u['role']}


@r.post('/auth/logout')
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {'ok': True}


@r.get('/auth/me')
def me(user=Depends(current_user)):
    return {'username': user['username'], 'role': user['role'], 'via': user.get('via')}


# ── users & tokens ───────────────────────────────────────────────────
class UserIn(BaseModel):
    username: str = Field(pattern=r'^[a-z0-9][a-z0-9._-]{0,63}$')
    password: Optional[str] = None
    role: str = 'viewer'


@r.get('/users')
def users(user=Depends(require('admin'))):
    return auth.list_users()


@r.post('/users', status_code=201)
def add_user(body: UserIn, user=Depends(require('admin'))):
    try:
        auth.create_user(body.username, body.password, body.role)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    db.audit(user['username'], 'user_add', body.username)
    return auth.get_user(body.username) | {'password_hash': None}


class UserPatch(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None


@r.patch('/users/{username}')
def patch_user(username: str, body: UserPatch, user=Depends(current_user)):
    if username != user['username'] and not auth.role_at_least(user['role'], 'admin'):
        raise HTTPException(403, 'admin only')
    if body.password:
        auth.set_password(username, body.password)
    if body.role:
        if not auth.role_at_least(user['role'], 'admin'):
            raise HTTPException(403, 'admin only')
        auth.set_role(username, body.role)
    db.audit(user['username'], 'user_patch', username)
    return {'ok': True}


@r.delete('/users/{username}')
def del_user(username: str, user=Depends(require('admin'))):
    try:
        auth.delete_user(username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user['username'], 'user_delete', username)
    return {'ok': True}


class TokenIn(BaseModel):
    name: str
    username: Optional[str] = None


@r.get('/tokens')
def tokens(user=Depends(current_user)):
    return auth.list_tokens(None if auth.role_at_least(user['role'], 'admin') else user['id'])


@r.post('/tokens', status_code=201)
def add_token(body: TokenIn, user=Depends(current_user)):
    target = user
    if body.username and body.username != user['username']:
        if not auth.role_at_least(user['role'], 'admin'):
            raise HTTPException(403, 'admin only')
        target = auth.get_user(body.username)
        if not target:
            raise HTTPException(404, 'user not found')
    t = auth.create_token(target['id'], body.name)
    db.audit(user['username'], 'token_add', f'{target["username"]}:{body.name}')
    return {'token': t, 'note': 'shown once'}


@r.delete('/tokens/{token_id}')
def del_token(token_id: int, user=Depends(current_user)):
    mine = [t['id'] for t in auth.list_tokens(user['id'])]
    if token_id not in mine and not auth.role_at_least(user['role'], 'admin'):
        raise HTTPException(403)
    auth.delete_token(token_id)
    return {'ok': True}


# ── connections ──────────────────────────────────────────────────────
class ConnectionIn(BaseModel):
    type: str = Field(pattern='^(vcenter|esxi|proxmox)$')
    name: str
    host: str
    username: str
    password: str
    verify_ssl: bool = False


def _conn_out(c):
    c = dict(c)
    c.pop('secret_enc', None)
    return c


@r.get('/connections')
def connections(user=Depends(current_user)):
    with db.conn() as c:
        out = _rows(c, 'SELECT * FROM connections ORDER BY name')
        for o in out:
            o['vm_count'] = c.execute('SELECT COUNT(*) FROM vms WHERE connection_id=? AND gone=0 AND is_template=0', (o['id'],)).fetchone()[0]
            o['proxies_online'] = len(ops.online_proxies(o['id']))
    return [_conn_out(o) for o in out]


@r.post('/connections', status_code=201)
def add_connection(body: ConnectionIn, user=Depends(require('admin'))):
    if body.type == 'proxmox':
        raise HTTPException(400, 'Proxmox provider arrives in a later phase')
    with db.conn() as c:
        try:
            c.execute('INSERT INTO connections(type,name,host,username,secret_enc,verify_ssl,created) VALUES(?,?,?,?,?,?,?)',
                      (body.type, body.name, body.host, body.username, secrets.encrypt(body.password), int(body.verify_ssl), time.time()))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f'cannot add: {e}')
        cid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    db.audit(user['username'], 'connection_add', body.name)
    tid = tasks.create('inventory', subject=body.name, created_by=user['username'], connection_id=cid)
    return {'id': cid, 'task_id': tid}


class ConnectionPatch(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    verify_ssl: Optional[bool] = None


@r.patch('/connections/{cid}')
def patch_connection(cid: int, body: ConnectionPatch, user=Depends(require('admin'))):
    sets, args = [], []
    for k in ('name', 'host', 'username'):
        if getattr(body, k) is not None:
            sets.append(f'{k}=?'); args.append(getattr(body, k))
    if body.password:
        sets.append('secret_enc=?'); args.append(secrets.encrypt(body.password))
    if body.verify_ssl is not None:
        sets.append('verify_ssl=?'); args.append(int(body.verify_ssl))
    if sets:
        with db.conn() as c:
            c.execute(f'UPDATE connections SET {",".join(sets)} WHERE id=?', (*args, cid))
    return {'ok': True}


@r.delete('/connections/{cid}')
def del_connection(cid: int, user=Depends(require('admin'))):
    with db.conn() as c:
        n = c.execute('SELECT COUNT(*) FROM restore_points rp JOIN vms v ON v.id=rp.vm_id WHERE v.connection_id=? AND rp.deleted=0', (cid,)).fetchone()[0]
        if n:
            # The catalog hangs off the connection's VM rows. A dead vCenter can stay here
            # unreachable for ever; its restore points are still restorable to any other target.
            raise HTTPException(409, f'{n} restore points still belong to VMs of this connection; delete them first, or keep the '
                                     'connection (it does not need to be reachable to restore from them elsewhere)')
        c.execute('DELETE FROM connections WHERE id=?', (cid,))
    db.audit(user['username'], 'connection_delete', str(cid))
    return {'ok': True}


@r.post('/connections/{cid}/test')
def test_connection(cid: int, user=Depends(require('admin'))):
    try:
        return ops.test_connection(ops.connection(cid))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f'{type(e).__name__}: {e}')


class PluginAction(BaseModel):
    action: str = Field(pattern='^(register|update|unregister|status)$')


@r.post('/connections/{cid}/vsphere-plugin')
def vsphere_plugin(cid: int, body: PluginAction, user=Depends(require('admin'))):
    try:
        res = ops.vsphere_plugin(ops.connection(cid), body.action)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f'{type(e).__name__}: {e}')
    if body.action != 'status':
        db.audit(user['username'], 'vsphere_plugin_' + body.action, str(cid))
    return res


@r.get('/connections/{cid}/placement')
def placement(cid: int, user=Depends(current_user)):
    """Hosts, datastores and networks reachable on this connection, for restore dialogs."""
    try:
        return ops.placement(ops.connection(cid))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f'{type(e).__name__}: {e}')


@r.post('/connections/{cid}/refresh')
def refresh(cid: int, user=Depends(require('operator'))):
    cr = ops.connection(cid)
    return {'task_id': tasks.create('inventory', subject=cr['name'], created_by=user['username'], connection_id=cid)}


# ── vms ──────────────────────────────────────────────────────────────
@r.get('/vms')
def vms(connection_id: Optional[int] = None, include_templates: bool = False, user=Depends(current_user)):
    q = 'SELECT v.*, c.name AS connection_name, c.type AS connection_type FROM vms v JOIN connections c ON c.id=v.connection_id WHERE v.gone=0'
    args = []
    if connection_id:
        q += ' AND v.connection_id=?'; args.append(connection_id)
    if not include_templates:
        q += ' AND v.is_template=0'
    q += ' ORDER BY c.name, v.name'
    with db.conn() as c:
        out = _rows(c, q, args)
        jobs = _rows(c, 'SELECT id,name,vm_ids_json,schedule FROM jobs')
        for o in out:
            o.pop('spec', None)
            o['size_bytes'] = sum(d.get('capacity', 0) for d in (o.get('disks') or []))
            last = c.execute('SELECT id,created,kind,allocated_bytes FROM restore_points WHERE vm_id=? AND deleted=0 ORDER BY id DESC LIMIT 1', (o['id'],)).fetchone()
            o['last_restore_point'] = db.row_to_dict(last)
            o['restore_points'] = c.execute('SELECT COUNT(*) FROM restore_points WHERE vm_id=? AND deleted=0', (o['id'],)).fetchone()[0]
            o['jobs'] = [{'id': j['id'], 'name': j['name'], 'schedule': j['schedule']} for j in jobs if o['id'] in (j.get('vm_ids') or [])]
            lr = c.execute('SELECT status,finished,error FROM runs WHERE vm_id=? ORDER BY id DESC LIMIT 1', (o['id'],)).fetchone()
            o['last_run'] = db.row_to_dict(lr)
    return out


@r.get('/vms/{vm_id}')
def vm(vm_id: int, user=Depends(current_user)):
    with db.conn() as c:
        v = db.row_to_dict(c.execute('SELECT * FROM vms WHERE id=?', (vm_id,)).fetchone())
        if not v:
            raise HTTPException(404)
        v['restore_points'] = _rows(c, 'SELECT id,run_id,created,size_bytes,allocated_bytes,kind FROM restore_points WHERE vm_id=? AND deleted=0 ORDER BY id DESC', (vm_id,))
        v['runs'] = _rows(c, 'SELECT * FROM runs WHERE vm_id=? ORDER BY id DESC LIMIT 20', (vm_id,))
    return v


class BackupNow(BaseModel):
    quiesce: bool = True


@r.post('/vms/{vm_id}/backup')
def backup_now(vm_id: int, body: BackupNow = BackupNow(), user=Depends(require('operator'))):
    db.audit(user['username'], 'backup_now', str(vm_id))
    return {'task_id': ops.backup_vm_now(vm_id, created_by=user['username'], quiesce=body.quiesce)}


# ── jobs ─────────────────────────────────────────────────────────────
class JobIn(BaseModel):
    name: str
    connection_id: int
    vm_ids: list[int]
    schedule: Optional[str] = None
    retention: dict = Field(default_factory=lambda: {'keep_last': 14, 'keep_days': 0})
    enabled: bool = True
    quiesce: bool = True


@r.get('/jobs')
def jobs(user=Depends(current_user)):
    with db.conn() as c:
        out = _rows(c, 'SELECT j.*, c.name AS connection_name FROM jobs j JOIN connections c ON c.id=j.connection_id ORDER BY j.name')
        for o in out:
            o['vm_names'] = [x['name'] for x in c.execute(f'SELECT name FROM vms WHERE id IN ({",".join("?" * len(o["vm_ids"]))})', o['vm_ids'])] if o['vm_ids'] else []
    return out


@r.post('/jobs', status_code=201)
def add_job(body: JobIn, user=Depends(require('operator'))):
    if body.schedule and not scheduler.validate(body.schedule):
        raise HTTPException(400, 'bad cron expression')
    with db.conn() as c:
        try:
            c.execute('INSERT INTO jobs(name,connection_id,vm_ids_json,schedule,retention_json,enabled,quiesce,created,next_run) VALUES(?,?,?,?,?,?,?,?,?)',
                      (body.name, body.connection_id, json.dumps(body.vm_ids), body.schedule or None, json.dumps(body.retention),
                       int(body.enabled), int(body.quiesce), time.time(), scheduler.next_run(body.schedule) if body.enabled else None))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        jid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    db.audit(user['username'], 'job_add', body.name)
    return {'id': jid}


class JobPatch(BaseModel):
    name: Optional[str] = None
    clear_schedule: bool = False
    vm_ids: Optional[list[int]] = None
    schedule: Optional[str] = None
    retention: Optional[dict] = None
    enabled: Optional[bool] = None
    quiesce: Optional[bool] = None


@r.patch('/jobs/{jid}')
def patch_job(jid: int, body: JobPatch, user=Depends(require('operator'))):
    if body.schedule and not scheduler.validate(body.schedule):
        raise HTTPException(400, 'bad cron expression')
    sets, args = [], []
    if body.name is not None: sets.append('name=?'); args.append(body.name)
    if body.vm_ids is not None: sets.append('vm_ids_json=?'); args.append(json.dumps(body.vm_ids))
    if body.clear_schedule: sets.append('schedule=NULL'); sets.append('next_run=NULL')
    elif body.schedule is not None: sets.append('schedule=?'); args.append(body.schedule or None); sets.append('next_run=?'); args.append(scheduler.next_run(body.schedule))
    if body.retention is not None: sets.append('retention_json=?'); args.append(json.dumps(body.retention))
    if body.enabled is not None: sets.append('enabled=?'); args.append(int(body.enabled))
    if body.quiesce is not None: sets.append('quiesce=?'); args.append(int(body.quiesce))
    if sets:
        with db.conn() as c:
            c.execute(f'UPDATE jobs SET {",".join(sets)} WHERE id=?', (*args, jid))
    scheduler.refresh_next_runs()
    return {'ok': True}


@r.delete('/jobs/{jid}')
def del_job(jid: int, user=Depends(require('operator'))):
    with db.conn() as c:
        c.execute('DELETE FROM jobs WHERE id=?', (jid,))
    return {'ok': True}


@r.post('/jobs/{jid}/run')
def run_job(jid: int, user=Depends(require('operator'))):
    db.audit(user['username'], 'job_run', str(jid))
    return {'task_id': ops.run_job(jid, created_by=user['username'])}


# ── runs & restore points ────────────────────────────────────────────
@r.get('/runs')
def runs(limit: int = 100, vm_id: Optional[int] = None, user=Depends(current_user)):
    q = 'SELECT r.*, v.name AS vm_name, j.name AS job_name FROM runs r JOIN vms v ON v.id=r.vm_id LEFT JOIN jobs j ON j.id=r.job_id'
    args = []
    if vm_id:
        q += ' WHERE r.vm_id=?'; args.append(vm_id)
    q += ' ORDER BY r.id DESC LIMIT ?'; args.append(limit)
    with db.conn() as c:
        return _rows(c, q, args)


@r.get('/restore-points')
def restore_points(vm_id: Optional[int] = None, user=Depends(current_user)):
    q = 'SELECT rp.id,rp.vm_id,rp.run_id,rp.created,rp.size_bytes,rp.allocated_bytes,rp.kind,v.name AS vm_name, v.connection_id FROM restore_points rp JOIN vms v ON v.id=rp.vm_id WHERE rp.deleted=0'
    args = []
    if vm_id:
        q += ' AND rp.vm_id=?'; args.append(vm_id)
    q += ' ORDER BY rp.id DESC'
    with db.conn() as c:
        return _rows(c, q, args)


@r.get('/restore-points/{rp_id}')
def restore_point(rp_id: int, user=Depends(current_user)):
    with db.conn() as c:
        rp = db.row_to_dict(c.execute('SELECT * FROM restore_points WHERE id=? AND deleted=0', (rp_id,)).fetchone())
    if not rp:
        raise HTTPException(404)
    m = repo().read_manifest(rp['manifest'])
    rp['disks'] = [{k: v for k, v in d.items() if k != 'index'} for d in m['disks']]
    return rp


@r.delete('/restore-points/{rp_id}')
def del_restore_point(rp_id: int, user=Depends(require('operator'))):
    ops.delete_restore_point(rp_id)
    db.audit(user['username'], 'restore_point_delete', str(rp_id))
    return {'ok': True}


class RestoreIn(BaseModel):
    """Target of a restore. connection_id may be any connection, not only the one the backup
    came from (DR to another vCenter or a standalone host). host/datastore/folder/networks
    name things on that target; what is left unset is taken from the restore point and
    remapped where the names do not exist there (see providers.vmware.resolve_target)."""
    name: Optional[str] = None
    power_on: bool = False
    new_identity: bool = True
    connection_id: Optional[int] = None
    host: Optional[str] = None
    datastore: Optional[str] = None
    folder: Optional[str] = None
    networks: Optional[dict[str, str]] = None


@r.post('/restore-points/{rp_id}/restore')
def restore(rp_id: int, body: RestoreIn, user=Depends(require('restore'))):
    db.audit(user['username'], 'restore', f'{rp_id} -> {body.name}')
    return {'task_id': tasks.create('restore', subject=body.name or f'rp {rp_id}', created_by=user['username'],
                                    restore_point_id=rp_id, options=body.model_dump())}


# ── exports (OVF/OVA copies of restore points; read-only on the backup) ──
class ExportIn(BaseModel):
    format: str = Field(default='ova', pattern='^(ova|ovf)$')


@r.post('/restore-points/{rp_id}/export')
def export_rp(rp_id: int, body: ExportIn = ExportIn(), user=Depends(require('restore'))):
    with db.conn() as c:
        rp = c.execute('SELECT id, vm_id FROM restore_points WHERE id=? AND deleted=0', (rp_id,)).fetchone()
        if not rp:
            raise HTTPException(404)
        vm = c.execute('SELECT name FROM vms WHERE id=?', (rp['vm_id'],)).fetchone()
    db.audit(user['username'], 'export', f'{rp_id} {body.format}')
    return {'task_id': tasks.create('export', subject=f'{vm["name"]} rp {rp_id} -> {body.format}', created_by=user['username'],
                                    restore_point_id=rp_id, fmt=body.format)}


@r.get('/exports')
def exports(user=Depends(current_user)):
    with db.conn() as c:
        return _rows(c, 'SELECT * FROM exports ORDER BY id DESC')


@r.get('/exports/{eid}/download')
def download_export(eid: int, user=Depends(current_user)):
    from fastapi.responses import FileResponse
    with db.conn() as c:
        e = db.row_to_dict(c.execute("SELECT * FROM exports WHERE id=? AND status='done'", (eid,)).fetchone())
    if not e or e['format'] != 'ova':
        raise HTTPException(404, 'no downloadable OVA for this export (OVF folders are read on the server)')
    p = os.path.join(repo().path, e['path'])
    if not os.path.exists(p):
        raise HTTPException(404, 'export file missing')
    db.audit(user['username'], 'export_download', str(eid))
    return FileResponse(p, media_type='application/x-tar', filename=os.path.basename(p))


@r.delete('/exports/{eid}')
def delete_export(eid: int, user=Depends(require('operator'))):
    from ..core import export as export_mod
    export_mod.delete_export(eid)
    db.audit(user['username'], 'export_delete', str(eid))
    return {'ok': True}


# ── proxies ──────────────────────────────────────────────────────────
class ProxyIn(BaseModel):
    name: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$')
    connection_id: Optional[int] = None


@r.get('/proxies')
def proxies(user=Depends(current_user)):
    with db.conn() as c:
        out = _rows(c, 'SELECT p.id,p.name,p.connection_id,p.vm_moref,p.bios_uuid,p.prefix,p.created,p.last_seen,p.version,p.address,p.enabled, c.name AS connection_name FROM proxies p LEFT JOIN connections c ON c.id=p.connection_id ORDER BY p.name')
    now = time.time()
    for o in out:
        o['online'] = bool(o['last_seen'] and now - o['last_seen'] < ops.PROXY_FRESH)
    return out


@r.post('/proxies', status_code=201)
def add_proxy(body: ProxyIn, user=Depends(require('admin'))):
    try:
        pid, tok = auth.create_proxy(body.name, body.connection_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    db.audit(user['username'], 'proxy_add', body.name)
    return {'id': pid, 'token': tok, 'note': 'shown once; put it in /etc/hvbackup/agent.conf on the proxy VM'}


@r.delete('/proxies/{pid}')
def del_proxy(pid: int, user=Depends(require('admin'))):
    with db.conn() as c:
        c.execute('DELETE FROM proxies WHERE id=?', (pid,))
    return {'ok': True}


# ── tasks ────────────────────────────────────────────────────────────
@r.get('/tasks')
def list_tasks(limit: int = 50, state: Optional[str] = None, user=Depends(current_user)):
    return tasks.list_tasks(limit, state)


@r.get('/tasks/{tid}')
def get_task(tid: int, user=Depends(current_user)):
    t = tasks.get(tid)
    if not t:
        raise HTTPException(404)
    return t


@r.post('/tasks/{tid}/cancel')
def cancel_task(tid: int, user=Depends(require('operator'))):
    tasks.cancel(tid)
    return {'ok': True}


# ── status & maintenance ─────────────────────────────────────────────
@r.get('/status')
def status(user=Depends(current_user)):
    rep = repo()
    with db.conn() as c:
        counts = {k: c.execute(q).fetchone()[0] for k, q in {
            'connections': 'SELECT COUNT(*) FROM connections',
            'vms': 'SELECT COUNT(*) FROM vms WHERE gone=0 AND is_template=0',
            'protected_vms': 'SELECT COUNT(DISTINCT vm_id) FROM restore_points WHERE deleted=0',
            'restore_points': 'SELECT COUNT(*) FROM restore_points WHERE deleted=0',
            'jobs': 'SELECT COUNT(*) FROM jobs WHERE enabled=1',
            'running_tasks': "SELECT COUNT(*) FROM tasks WHERE state='running'",
            'failed_runs_24h': f"SELECT COUNT(*) FROM runs WHERE status='failed' AND started>{time.time() - 86400}",
        }.items()}
        last_gc = c.execute("SELECT value FROM meta WHERE key='last_gc'").fetchone()
    return {'version': APP_VERSION, 'repository': {'path': rep.path, 'id': rep.id, **rep.stats()},
            'proxies_online': len(ops.online_proxies()), 'last_gc': float(last_gc[0]) if last_gc else None, **counts}


@r.post('/maintenance/gc')
def run_gc(grace_seconds: int = 3600, user=Depends(require('admin'))):
    return {'task_id': tasks.create('gc', subject='manual', created_by=user['username'], grace_seconds=grace_seconds)}


@r.get('/audit')
def audit_log(limit: int = 200, user=Depends(require('admin'))):
    with db.conn() as c:
        return _rows(c, 'SELECT * FROM audit ORDER BY id DESC LIMIT ?', (limit,))
