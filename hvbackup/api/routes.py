"""REST API, versioned under /api/v1. Every mutating call returns a task id or the
changed object; the UI and CLI are plain clients of these routes."""
import json
import os
import time
from urllib.parse import urlparse
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


@r.post('/restore-points/{rp_id}/restore')
def restore(rp_id: int, body: RestoreIn, user=Depends(require('restore'))):
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
        if not c.execute('SELECT 1 FROM connections WHERE id=?', (body.target_connection_id,)).fetchone():
            raise HTTPException(404, 'target connection not found')
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
    url = (body.server_url or config.PUBLIC_URL or str(request.base_url)).rstrip('/')
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
    return {'version': APP_VERSION, 'repository': {'path': rep.path, 'id': rep.id, **rep.stats()},
            'proxies_online': len(ops.online_proxies()), 'last_gc': float(last_gc[0]) if last_gc else None, **counts}


@r.post('/maintenance/gc')
def run_gc(grace_seconds: int = 3600, user=Depends(require('admin'))):
    return {'task_id': tasks.create('gc', subject='manual', created_by=user['username'], grace_seconds=grace_seconds)}


@r.get('/audit')
def audit_log(limit: int = 200, user=Depends(require('admin'))):
    with db.conn() as c:
        return _rows(c, 'SELECT * FROM audit ORDER BY id DESC LIMIT ?', (limit,))
