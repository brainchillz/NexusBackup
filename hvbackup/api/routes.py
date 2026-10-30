"""REST API, versioned under /api/v1. Every mutating call returns a task id or the
changed object; the UI and CLI are plain clients of these routes."""
import json
import os
import threading
import time
from urllib.parse import urlparse
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import APP_VERSION, config, db
from ..core import auth, ops, scheduler, secrets, settings, tasks
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
    response.set_cookie(COOKIE, auth.make_session(u['username']), httponly=True, samesite='lax', max_age=auth.session_ttl(),
                        secure=True)
    db.audit(u['username'], 'login')
    return {'username': u['username'], 'role': u['role']}


class Setup(BaseModel):
    username: str = Field(pattern=r'^[a-z0-9][a-z0-9._-]{0,63}$')
    password: str = Field(min_length=8)


@r.get('/auth/setup')
def setup_state():
    """Whether the server still has no account at all (then /login shows the first-admin form)."""
    return {'needed': auth.count_users() == 0}


_setup_lock = threading.Lock()


@r.post('/auth/setup', status_code=201)
def setup(body: Setup, response: Response):
    """Create the first administrator. Works only while the users table is empty, so it is
    a one-shot door that closes itself; afterwards accounts come from Settings > Users."""
    with _setup_lock:
        if auth.count_users():
            raise HTTPException(409, 'an account exists already; sign in')
        auth.create_user(body.username, body.password, 'admin')
    db.audit(body.username, 'setup', 'first administrator created from the sign-in page')
    response.set_cookie(COOKIE, auth.make_session(body.username), httponly=True, samesite='lax', max_age=auth.session_ttl(), secure=True)
    return {'username': body.username, 'role': 'admin'}


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


@r.get('/connections/{cid}/pve-setup')
def pve_setup(cid: int, user=Depends(require('admin'))):
    """Proxmox only: the node installer one-liner and whether the hvbackup storage is registered."""
    cr = ops.connection(cid)
    if cr['type'] != 'proxmox':
        raise HTTPException(400, 'not a Proxmox connection')
    try:
        return ops.pve_setup(cr)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f'{type(e).__name__}: {e}')


class PveStorageIn(BaseModel):
    storage_id: str = Field(default='hvbackup', pattern=r'^[a-zA-Z][a-zA-Z0-9\-_.]{1,31}$')


@r.post('/connections/{cid}/pve-storage')
def pve_storage(cid: int, body: PveStorageIn = PveStorageIn(), user=Depends(require('admin'))):
    """Proxmox only: register the hvbackup storage on the cluster (after the node plug-in is
    installed on every node), minting the token the nodes use. Re-run to rotate the token."""
    cr = ops.connection(cid)
    if cr['type'] != 'proxmox':
        raise HTTPException(400, 'not a Proxmox connection')
    try:
        res = ops.pve_register_storage(cr, body.storage_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f'{type(e).__name__}: {e}')
    db.audit(user['username'], 'pve_storage', f'{cr["name"]}: {body.storage_id} {res["state"]}')
    return res


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
        replicas = {(r['target_connection_id'], r['moref']): r for r in _rows(c, 'SELECT r.id, r.target_connection_id, r.moref, r.state, r.vm_id, r.synced_at, r.replication_id, v.name AS vm_name FROM replicas r JOIN vms v ON v.id=r.vm_id WHERE r.moref IS NOT NULL')}
        for o in out:
            o['networks'] = sorted({n.get('network') or '' for n in (o.get('spec') or {}).get('nics') or []})
            o.pop('spec', None)
            rep_row = replicas.get((o['connection_id'], o['moref']))
            o['replica_of'] = {'vm_id': rep_row['vm_id'], 'name': rep_row['vm_name'], 'state': rep_row['state'], 'replica_id': rep_row['id'],
                               'synced_at': rep_row['synced_at'], 'replication_id': rep_row['replication_id']} if rep_row else None
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
    try:
        return {'task_id': ops.backup_vm_now(vm_id, created_by=user['username'], quiesce=body.quiesce)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


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
    q = 'SELECT rp.id,rp.vm_id,rp.run_id,rp.created,rp.size_bytes,rp.allocated_bytes,rp.kind,rp.platform,rp.source_rp_id,v.name AS vm_name, v.connection_id FROM restore_points rp JOIN vms v ON v.id=rp.vm_id WHERE rp.deleted=0'
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
    install_tools: bool = False  # cross-platform restores: install the target's guest tools on the copy's first boot


def _conversion_guard(src_platform, target_type):
    """Refuse a cross-platform restore or replication up front when this server cannot convert
    (a native install without libguestfs), instead of failing minutes into the task."""
    from ..core import convert
    target = 'proxmox' if target_type == 'proxmox' else 'vmware'
    if (src_platform or 'vmware') == target:
        return
    missing = convert.missing_tools(target)
    if missing:
        raise HTTPException(400, f'a {src_platform or "vmware"} image restored onto {target} is converted first, and this server lacks '
                                 f'{", ".join(missing)}: {convert.TOOL_PACKAGES}')


@r.post('/restore-points/{rp_id}/restore')
def restore(rp_id: int, body: RestoreIn, user=Depends(require('restore'))):
    if body.connection_id:
        with db.conn() as c:
            rp = c.execute('SELECT platform FROM restore_points WHERE id=? AND deleted=0', (rp_id,)).fetchone()
            cr = c.execute('SELECT type FROM connections WHERE id=?', (body.connection_id,)).fetchone()
        if rp and cr:
            _conversion_guard(rp[0], cr[0])
    db.audit(user['username'], 'restore', f'{rp_id} -> {body.name}')
    return {'task_id': tasks.create('restore', subject=body.name or f'rp {rp_id}', created_by=user['username'],
                                    restore_point_id=rp_id, options=body.model_dump())}


class ConvertIn(BaseModel):
    """Make a restore point of the other platform from this one (Linux guests). The result is a
    normal restore point of the same VM, restorable from here or, for Proxmox, from the
    Proxmox UI. A cross-platform restore does this by itself; this is for converting ahead."""
    target: str = Field(pattern='^(proxmox|vmware)$')
    new_identity: bool = True
    install_tools: bool = False
    name: Optional[str] = None


@r.post('/restore-points/{rp_id}/convert')
def convert_rp(rp_id: int, body: ConvertIn, user=Depends(require('restore'))):
    with db.conn() as c:
        rp = c.execute('SELECT rp.id, v.name FROM restore_points rp JOIN vms v ON v.id=rp.vm_id WHERE rp.id=? AND rp.deleted=0', (rp_id,)).fetchone()
    if not rp:
        raise HTTPException(404)
    from ..core import convert
    try:
        convert.check_tools(body.target)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    db.audit(user['username'], 'convert', f'{rp_id} -> {body.target}')
    return {'task_id': tasks.create('convert', subject=f'{rp["name"]} -> {body.target}', created_by=user['username'],
                                    restore_point_id=rp_id, target=body.target, options=body.model_dump())}


# ── replication: standby copies on another connection ────────────────
class ReplicationOptions(BaseModel):
    """Where the replicas land on the target and how they are named. host/datastore/folder/
    networks work like a restore's: unset means the source's names when they exist on the
    target, one sensible candidate otherwise, a refusal with the list when there are several."""
    suffix: str = '-replica'
    host: Optional[str] = None
    datastore: Optional[str] = None
    folder: Optional[str] = None
    networks: dict[str, str] = Field(default_factory=dict)
    new_identity: bool = True
    install_tools: bool = False


class ReplicationIn(BaseModel):
    name: str
    connection_id: int
    target_connection_id: int
    vm_ids: list[int]
    schedule: Optional[str] = None
    after_backup: bool = True
    options: ReplicationOptions = Field(default_factory=ReplicationOptions)
    enabled: bool = True


def _replication_rows(c, where='', args=()):
    out = _rows(c, f'SELECT r.*, s.name AS connection_name, t.name AS target_connection_name, t.type AS target_type FROM replications r '
                   f'JOIN connections s ON s.id=r.connection_id JOIN connections t ON t.id=r.target_connection_id {where} ORDER BY r.name', args)
    for o in out:
        o['vm_names'] = [x['name'] for x in c.execute(f'SELECT name FROM vms WHERE id IN ({",".join("?" * len(o["vm_ids"]))})', o['vm_ids'])] if o['vm_ids'] else []
        o['replicas'] = _rows(c, 'SELECT r.*, v.name AS vm_name FROM replicas r JOIN vms v ON v.id=r.vm_id WHERE r.replication_id=? ORDER BY v.name', (o['id'],))
    return out


@r.get('/replications')
def replications(user=Depends(current_user)):
    with db.conn() as c:
        return _replication_rows(c)


@r.post('/replications', status_code=201)
def add_replication(body: ReplicationIn, user=Depends(require('operator'))):
    if body.schedule and not scheduler.validate(body.schedule):
        raise HTTPException(400, 'bad cron expression')
    if body.connection_id == body.target_connection_id:
        raise HTTPException(400, 'the target must be a different connection than the source (a copy on the same hypervisor is a restore)')
    if not body.schedule and not body.after_backup:
        raise HTTPException(400, 'give the replication a schedule, let it follow backups, or both')
    with db.conn() as c:
        target = c.execute('SELECT type FROM connections WHERE id=?', (body.target_connection_id,)).fetchone()
        source = c.execute('SELECT type FROM connections WHERE id=?', (body.connection_id,)).fetchone()
        if not target:
            raise HTTPException(404, 'target connection not found')
        if source:
            _conversion_guard('proxmox' if source[0] == 'proxmox' else 'vmware', target[0])
        try:
            c.execute('INSERT INTO replications(name,connection_id,target_connection_id,vm_ids_json,schedule,after_backup,options_json,enabled,created,next_run) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (body.name, body.connection_id, body.target_connection_id, json.dumps(body.vm_ids), body.schedule or None, int(body.after_backup),
                       json.dumps(body.options.model_dump()), int(body.enabled), time.time(), scheduler.next_run(body.schedule) if body.enabled else None))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        rid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    db.audit(user['username'], 'replication_add', body.name)
    return {'id': rid}


class ReplicationPatch(BaseModel):
    name: Optional[str] = None
    vm_ids: Optional[list[int]] = None
    schedule: Optional[str] = None
    clear_schedule: bool = False
    after_backup: Optional[bool] = None
    options: Optional[ReplicationOptions] = None
    enabled: Optional[bool] = None


@r.patch('/replications/{rid}')
def patch_replication(rid: int, body: ReplicationPatch, user=Depends(require('operator'))):
    if body.schedule and not scheduler.validate(body.schedule):
        raise HTTPException(400, 'bad cron expression')
    sets, args = [], []
    if body.name is not None: sets.append('name=?'); args.append(body.name)
    if body.vm_ids is not None: sets.append('vm_ids_json=?'); args.append(json.dumps(body.vm_ids))
    if body.clear_schedule: sets.append('schedule=NULL'); sets.append('next_run=NULL')
    elif body.schedule is not None: sets.append('schedule=?'); args.append(body.schedule or None); sets.append('next_run=?'); args.append(scheduler.next_run(body.schedule))
    if body.after_backup is not None: sets.append('after_backup=?'); args.append(int(body.after_backup))
    if body.options is not None: sets.append('options_json=?'); args.append(json.dumps(body.options.model_dump()))
    if body.enabled is not None: sets.append('enabled=?'); args.append(int(body.enabled))
    if sets:
        with db.conn() as c:
            c.execute(f'UPDATE replications SET {",".join(sets)} WHERE id=?', (*args, rid))
    scheduler.refresh_next_runs()
    db.audit(user['username'], 'replication_patch', str(rid))
    return {'ok': True}


@r.delete('/replications/{rid}')
def del_replication(rid: int, user=Depends(require('operator'))):
    """Removes the replication; its replicas stay (as records and as VMs) until forgotten."""
    with db.conn() as c:
        c.execute('DELETE FROM replications WHERE id=?', (rid,))
    db.audit(user['username'], 'replication_delete', str(rid))
    return {'ok': True}


@r.post('/replications/{rid}/run')
def run_replication(rid: int, user=Depends(require('operator'))):
    from ..core import replication
    db.audit(user['username'], 'replication_run', str(rid))
    try:
        return {'task_id': replication.run(rid, created_by=user['username'])}
    except RuntimeError as e:
        raise HTTPException(404, str(e))


@r.get('/replicas')
def replicas(user=Depends(current_user)):
    with db.conn() as c:
        out = _rows(c, 'SELECT r.*, v.name AS vm_name, v.connection_id AS source_connection_id, c.name AS target_connection_name, c.type AS target_type, p.name AS replication_name '
                       'FROM replicas r JOIN vms v ON v.id=r.vm_id JOIN connections c ON c.id=r.target_connection_id LEFT JOIN replications p ON p.id=r.replication_id ORDER BY v.name')
        for o in out:
            newest = c.execute("SELECT id, created FROM restore_points WHERE vm_id=? AND deleted=0 AND source_rp_id IS NULL ORDER BY id DESC LIMIT 1", (o['vm_id'],)).fetchone()
            o['newest_rp_id'] = newest['id'] if newest else None
            o['behind'] = bool(newest and o['source_rp_id'] != newest['id']) if o['state'] not in ('failed_over', 'test') else False
    return out


class FailoverIn(BaseModel):
    test: bool = False  # power on with the NICs disconnected


@r.post('/replicas/{rid}/failover')
def failover(rid: int, body: FailoverIn = FailoverIn(), user=Depends(require('restore'))):
    from ..core import replication
    try:
        return replication.failover(rid, test=body.test, user=user['username'])
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@r.post('/replicas/{rid}/end-test')
def end_test(rid: int, user=Depends(require('restore'))):
    from ..core import replication
    try:
        return replication.end_test(rid, user=user['username'])
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@r.delete('/replicas/{rid}')
def forget_replica(rid: int, delete_vm: bool = False, user=Depends(require('operator'))):
    from ..core import replication
    try:
        return replication.forget(rid, delete_vm=delete_vm, user=user['username'])
    except RuntimeError as e:
        raise HTTPException(400, str(e))


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
        out = _rows(c, 'SELECT p.id,p.name,p.connection_id,p.vm_moref,p.bios_uuid,p.prefix,p.created,p.last_seen,p.version,p.address,p.enabled,'
                       ' p.deploy_task_id,p.vm_name, c.name AS connection_name, t.state AS deploy_state'
                       ' FROM proxies p LEFT JOIN connections c ON c.id=p.connection_id LEFT JOIN tasks t ON t.id=p.deploy_task_id ORDER BY p.name')
        links = _rows(c, 'SELECT l.proxy_id, l.connection_id, l.vm_moref, c.name AS connection_name FROM proxy_links l JOIN connections c ON c.id=l.connection_id')
    now = time.time()
    for o in out:
        o['online'] = bool(o['last_seen'] and now - o['last_seen'] < ops.PROXY_FRESH)
        o['also_on'] = [{'connection_id': l['connection_id'], 'connection_name': l['connection_name'], 'vm_moref': l['vm_moref']} for l in links if l['proxy_id'] == o['id']]
    return out


class ProxyDeployIn(BaseModel):
    """Deploy a proxy VM from the stock cloud image onto a vSphere connection. Unset host,
    datastore, folder and network are chosen like a restore target: one sensible candidate
    is used, several are refused with the list. The server URL is what the proxy calls back
    to; it defaults to HVB_URL or the URL this request came in on."""
    name: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$')
    connection_id: int
    host: Optional[str] = None
    datastore: Optional[str] = None
    folder: Optional[str] = None
    network: Optional[str] = None
    cpus: int = Field(default=2, ge=1, le=32)
    memory_mb: int = Field(default=2048, ge=1024, le=131072)
    ssh_key: Optional[str] = None
    server_url: Optional[str] = None
    timeout_minutes: int = Field(default=20, ge=3, le=180)


@r.post('/proxies/deploy')
def deploy_proxy(body: ProxyDeployIn, request: Request, user=Depends(require('admin'))):
    cr = ops.connection(body.connection_id)
    if cr['type'] not in ('vcenter', 'esxi'):
        raise HTTPException(400, f'proxy VMs are deployed on vSphere connections, not {cr["type"]}')
    url = (body.server_url or settings.public_url(str(request.base_url))).rstrip('/')
    hostpart = (urlparse(url).hostname or '').lower()
    if hostpart in ('', 'localhost', '127.0.0.1', '::1'):
        raise HTTPException(400, 'the proxy must reach this server by a routable address: set HVB_URL in hvbackup.conf or pass server_url')
    with db.conn() as c:
        if c.execute('SELECT 1 FROM proxies WHERE name=?', (body.name,)).fetchone():
            raise HTTPException(409, f'a proxy named {body.name!r} already exists')
    opts = body.model_dump()
    opts['server_url'] = url
    db.audit(user['username'], 'proxy_deploy', f'{body.name} on {cr["name"]}')
    return {'task_id': tasks.create('deploy_proxy', subject=body.name, created_by=user['username'], options=opts)}


@r.get('/proxies/image')
def proxy_image(user=Depends(current_user)):
    """The cloud image proxy VMs are deployed from and whether it is already cached."""
    from ..core import proxy_deploy
    return proxy_deploy.image_status()


@r.post('/proxies/image', status_code=201)
async def upload_proxy_image(file: UploadFile = File(...), user=Depends(require('admin'))):
    """Upload an OVA for offline sites; it becomes the local proxy image setting."""
    import shutil
    from ..core import proxy_deploy
    name = os.path.basename(file.filename or 'proxy-image.ova')
    if not name.lower().endswith('.ova'):
        raise HTTPException(400, 'the proxy image must be an OVA')
    name = ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in name)
    os.makedirs(config.IMAGE_DIR, exist_ok=True)
    path = os.path.join(config.IMAGE_DIR, 'uploaded-' + name)
    with open(path + '.part', 'wb') as f:
        shutil.copyfileobj(file.file, f, 8 << 20)
    os.replace(path + '.part', path)
    try:
        settings.update({'proxy_image': path}, user=user['username'])
    except ValueError as e:
        os.unlink(path)
        raise HTTPException(400, str(e))
    db.audit(user['username'], 'proxy_image_uploaded', path)
    return proxy_deploy.image_status()


@r.delete('/proxies/image')
def delete_proxy_image(user=Depends(require('admin'))):
    """Forget the local proxy image; an uploaded file is deleted, a path set by hand is only unset."""
    from ..core import proxy_deploy
    st = proxy_deploy.image_status()
    if not st.get('local'):
        raise HTTPException(404, 'no local image is set')
    try:
        settings.update({'proxy_image': None}, user=user['username'])
    except ValueError as e:
        raise HTTPException(400, str(e))
    if st.get('uploaded'):
        try:
            os.unlink(st['path'])
        except FileNotFoundError:
            pass
    return proxy_deploy.image_status()


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
            'vms': 'SELECT COUNT(*) FROM vms v WHERE gone=0 AND is_template=0 AND NOT EXISTS (SELECT 1 FROM replicas r WHERE r.target_connection_id=v.connection_id AND r.moref=v.moref)',
            'protected_vms': 'SELECT COUNT(DISTINCT vm_id) FROM restore_points WHERE deleted=0',
            'replications': 'SELECT COUNT(*) FROM replications WHERE enabled=1',
            'replicas': "SELECT COUNT(*) FROM replicas WHERE moref IS NOT NULL",
            'replicas_stale': "SELECT COUNT(*) FROM replicas r WHERE r.moref IS NOT NULL AND r.state NOT IN ('failed_over','test') AND (r.state IN ('failed','dirty') OR r.source_rp_id IS NOT (SELECT MAX(id) FROM restore_points rp WHERE rp.vm_id=r.vm_id AND rp.deleted=0 AND rp.source_rp_id IS NULL))",
            'restore_points': 'SELECT COUNT(*) FROM restore_points WHERE deleted=0',
            'jobs': 'SELECT COUNT(*) FROM jobs WHERE enabled=1',
            'running_tasks': "SELECT COUNT(*) FROM tasks WHERE state='running'",
            'failed_runs_24h': f"SELECT COUNT(*) FROM runs WHERE status='failed' AND started>{time.time() - 86400}",
        }.items()}
        last_gc = c.execute("SELECT value FROM meta WHERE key='last_gc'").fetchone()
    from ..core import catalog_backup, convert, server, updates
    return {'version': APP_VERSION, 'repository': {'path': rep.path, 'id': rep.id, **rep.stats()},
            'proxies_online': len(ops.online_proxies()), 'last_gc': float(last_gc[0]) if last_gc else None,
            'conversion': convert.availability(), 'timezone': settings.get('timezone'), 'update': updates.status(),
            'restart_pending': server.restart_pending(), 'catalog_restore_pending': catalog_backup.pending() is not None, **counts}


@r.post('/maintenance/gc')
def run_gc(grace_seconds: int = 3600, user=Depends(require('admin'))):
    return {'task_id': tasks.create('gc', subject='manual', created_by=user['username'], grace_seconds=grace_seconds)}


@r.get('/audit')
def audit_log(limit: int = 200, user=Depends(require('admin'))):
    with db.conn() as c:
        return _rows(c, 'SELECT * FROM audit ORDER BY id DESC LIMIT ?', (limit,))


# ── settings ─────────────────────────────────────────────────────────
@r.get('/settings')
def get_settings(user=Depends(require('admin'))):
    """Every runtime setting with its value, where it comes from (default, settings table,
    environment, config file) and whether the UI may change it."""
    from ..core import server
    return {'settings': settings.describe(), 'restart_pending': server.restart_pending()}


@r.patch('/settings')
def patch_settings(body: Dict[str, Any], user=Depends(require('admin'))):
    """Change settings {key: value}; null or the default clears the row. Returns the keys
    that need a restart to take effect."""
    try:
        restart = settings.update(body, user=user['username'])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'settings': settings.describe(), 'restart_needed': restart}


@r.get('/settings/timezones')
def timezones(user=Depends(current_user)):
    import zoneinfo
    return sorted(zoneinfo.available_timezones())


@r.post('/server/restart')
def restart_server(reason: str = '', user=Depends(require('admin'))):
    """Exit once no task runs (10 minutes at most); the supervisor starts the server again."""
    from ..core import server
    return server.request_restart(user['username'], reason)


@r.get('/logs')
def server_logs(limit: int = 200, level: Optional[str] = None, after: Optional[int] = None, q: Optional[str] = None,
                user=Depends(require('admin'))):
    """The last lines of the server log kept in memory (4000 at most); `after` an id for
    incremental polling, `level` a floor, `q` a case-insensitive filter."""
    from ..core import logs
    return logs.tail(limit=min(max(limit, 1), logs.RING), level=level, after=after, grep=q)


# ── catalog backup ───────────────────────────────────────────────────
@r.get('/catalog/backups')
def catalog_backups(user=Depends(require('admin'))):
    from ..core import catalog_backup
    with db.conn() as c:
        last = c.execute("SELECT value FROM meta WHERE key='last_catalog_backup'").fetchone()
    return {'copies': catalog_backup.list_copies(), 'directory': catalog_backup.repo_dir(), 'last': float(last[0]) if last else None,
            'pending': catalog_backup.pending()}


@r.get('/catalog/backup')
def catalog_download(user=Depends(require('admin'))):
    """A backup archive of the catalog, the secret key and the certificate, made now."""
    import io
    from ..core import catalog_backup
    buf = io.BytesIO()
    catalog_backup.write(buf)
    buf.seek(0)
    name = time.strftime('hvbackup-catalog-%Y%m%d-%H%M%S.tar')
    db.audit(user['username'], 'catalog_download', name)
    return StreamingResponse(buf, media_type='application/x-tar', headers={'Content-Disposition': f'attachment; filename="{name}"'})


@r.post('/catalog/backups', status_code=201)
def catalog_copy_now(user=Depends(require('admin'))):
    """Write a copy into the repository now (the daily copy does the same)."""
    from ..core import catalog_backup
    name = catalog_backup.write_copy(settings.get('catalog_backup_keep'))
    db.audit(user['username'], 'catalog_copy', name)
    return {'name': name, 'copies': catalog_backup.list_copies()}


@r.delete('/catalog/backups/{name}')
def catalog_copy_delete(name: str, user=Depends(require('admin'))):
    from ..core import catalog_backup
    if not catalog_backup.NAME.match(name):
        raise HTTPException(400, 'bad name')
    try:
        os.unlink(os.path.join(catalog_backup.repo_dir(), name))
    except FileNotFoundError:
        raise HTTPException(404, 'no such copy')
    return {'ok': True}


class CatalogRestoreIn(BaseModel):
    name: str


@r.post('/catalog/restore')
async def catalog_restore(file: Optional[UploadFile] = File(None), name: Optional[str] = None, user=Depends(require('admin'))):
    """Stage a catalog restore from an uploaded archive or a copy in the repository (`name`).
    It is applied at the next start; nothing changes until then."""
    from ..core import catalog_backup
    try:
        if file is not None:
            summary = catalog_backup.stage(file.file)
            src = file.filename or 'upload'
        elif name:
            summary = catalog_backup.stage_copy(name)
            src = name
        else:
            raise HTTPException(400, 'upload an archive or name a copy')
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user['username'], 'catalog_restore_staged', src)
    return {'staged': True, 'source': src, **summary}


@r.delete('/catalog/restore')
def catalog_restore_discard(user=Depends(require('admin'))):
    from ..core import catalog_backup
    catalog_backup.discard_pending()
    db.audit(user['username'], 'catalog_restore_discarded')
    return {'ok': True}


# ── themes ───────────────────────────────────────────────────────────
@r.get('/themes')
def list_themes(user=Depends(current_user)):
    from ..core import themes
    return themes.list_themes()


@r.post('/themes', status_code=201)
async def upload_theme(file: UploadFile = File(...), user=Depends(require('admin'))):
    """Install a theme from a zip (theme.json, theme.css, optional theme.js and assets)."""
    from ..core import themes
    data = await file.read(themes.MAX_ZIP + 1)
    try:
        t = themes.install_zip(data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user['username'], 'theme_installed', t['id'])
    return t


@r.delete('/themes/{theme_id}')
def delete_theme(theme_id: str, user=Depends(require('admin'))):
    from ..core import themes
    try:
        themes.remove(theme_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    db.audit(user['username'], 'theme_removed', theme_id)
    return {'ok': True}
