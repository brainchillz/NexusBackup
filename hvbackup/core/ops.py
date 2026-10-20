"""Operations behind the API: inventory refresh, backup jobs, restores, retention, GC.
Each is registered as a task kind and runs in a task thread."""
import json
import logging
import time

from .. import db
from ..providers import vmware
from ..store.chunks import repo
from . import scheduler, secrets, tasks, uuids

log = logging.getLogger('hvbackup.ops')
PROXY_FRESH = 120  # seconds since last hello for a proxy to count as online


def connection(conn_id):
    with db.conn() as c:
        r = db.row_to_dict(c.execute('SELECT * FROM connections WHERE id=?', (conn_id,)).fetchone())
    if not r:
        raise RuntimeError(f'connection {conn_id} not found')
    return r


def open_api(conn_row):
    if conn_row['type'] in ('vcenter', 'esxi'):
        return vmware.VMware(conn_row['host'], conn_row['username'], secrets.decrypt(conn_row['secret_enc']), bool(conn_row['verify_ssl']))
    raise RuntimeError(f'provider {conn_row["type"]} not implemented yet')


def test_connection(conn_row):
    with open_api(conn_row) as api:
        return api.about()


def vsphere_plugin(conn_row, action):
    from ..providers import vsphere_plugin as vsp
    from .. import config
    if action in ('register', 'update') and not config.PUBLIC_URL:
        raise RuntimeError('HVB_URL must be set to the public https URL of this server before registering the plug-in')
    with open_api(conn_row) as api:
        if not api.about()['is_vcenter']:
            raise RuntimeError('the vSphere Client plug-in registers with vCenter, not a standalone ESXi host')
        if action in ('register', 'update'):
            return vsp.register(api, config.PUBLIC_URL)
        if action == 'unregister':
            return vsp.unregister(api)
        return vsp.status(api)


def placement(conn_row):
    with open_api(conn_row) as api:
        return api.placement()


def online_proxies(conn_id=None):
    with db.conn() as c:
        rows = c.execute('SELECT * FROM proxies WHERE enabled=1 AND last_seen>=?', (time.time() - PROXY_FRESH,)).fetchall()
    rows = [db.row_to_dict(r) for r in rows]
    if conn_id is not None:
        rows = [r for r in rows if r['connection_id'] == conn_id]
    return rows


def pick_proxy(conn_id):
    ps = online_proxies(conn_id)
    return ps[0] if ps else None


# ── inventory ────────────────────────────────────────────────────────
def refresh_inventory(task, connection_id):
    cr = connection(connection_id)
    task.log(f'refreshing inventory of {cr["name"]} ({cr["host"]})')
    try:
        with open_api(cr) as api:
            about = api.about()
            vms = api.inventory()
    except Exception as e:  # noqa: BLE001
        with db.conn() as c:
            c.execute('UPDATE connections SET last_error=? WHERE id=?', (str(e)[:500], connection_id))
        raise
    now = time.time()
    with db.conn() as c:
        seen = set()
        for v in vms:
            seen.add(v['moref'])
            c.execute('''INSERT INTO vms(connection_id,moref,name,uuid,power_state,guest,tools,cbt_enabled,is_template,host,folder,disks_json,spec_json,updated,gone)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                         ON CONFLICT(connection_id,moref) DO UPDATE SET name=excluded.name,uuid=excluded.uuid,power_state=excluded.power_state,
                         guest=excluded.guest,tools=excluded.tools,cbt_enabled=excluded.cbt_enabled,is_template=excluded.is_template,host=excluded.host,
                         folder=excluded.folder,disks_json=excluded.disks_json,spec_json=excluded.spec_json,updated=excluded.updated,gone=0''',
                      (connection_id, v['moref'], v['name'], v['uuid'], v['power_state'], v['guest'], v['tools'], int(v['cbt_enabled']),
                       int(v['is_template']), v['host'], v['folder'], json.dumps(v['disks']), json.dumps(v['spec']), now))
        for r in c.execute('SELECT id,moref FROM vms WHERE connection_id=? AND gone=0', (connection_id,)).fetchall():
            if r['moref'] not in seen:
                c.execute('UPDATE vms SET gone=1 WHERE id=?', (r['id'],))
        c.execute('UPDATE connections SET last_inventory=?, last_error=NULL, info_json=? WHERE id=?', (now, json.dumps(about), connection_id))
        # proxies that said hello but were never tied to a connection: find their VM here
        for p in c.execute('SELECT id,bios_uuid FROM proxies WHERE bios_uuid IS NOT NULL AND (connection_id IS NULL OR vm_moref IS NULL)').fetchall():
            for v in vms:
                if uuids.same_vm(v['uuid'], p['bios_uuid']):
                    c.execute('UPDATE proxies SET connection_id=?, vm_moref=? WHERE id=?', (connection_id, v['moref'], p['id']))
                    task.log(f'proxy {p["id"]} is VM {v["name"]} on this connection')
    task.log(f'{len(vms)} VMs')
    return {'vms': len(vms)}


# ── backup ───────────────────────────────────────────────────────────
def run_job(job_id, created_by=None, vm_ids=None):
    with db.conn() as c:
        j = db.row_to_dict(c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
    if not j:
        raise RuntimeError('job not found')
    return tasks.create('backup_job', subject=j['name'], created_by=created_by, job_id=job_id, vm_ids=vm_ids)


def backup_job(task, job_id, vm_ids=None):
    with db.conn() as c:
        j = db.row_to_dict(c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        c.execute('UPDATE jobs SET last_run=?, last_status=? WHERE id=?', (time.time(), 'running', job_id))
    ids = vm_ids or j['vm_ids']
    cr = connection(j['connection_id'])
    results = []
    failed = 0
    with open_api(cr) as api:
        for n, vm_id in enumerate(ids):
            task.check_cancel()
            with db.conn() as c:
                vm_row = db.row_to_dict(c.execute('SELECT * FROM vms WHERE id=?', (vm_id,)).fetchone())
            if not vm_row or vm_row['gone']:
                task.log(f'vm {vm_id}: not in inventory, skipped')
                failed += 1
                continue
            if vm_busy(vm_id):
                task.log(f'{vm_row["name"]}: a backup is already running, skipped')
                failed += 1
                continue
            task.log(f'[{n + 1}/{len(ids)}] {vm_row["name"]}')
            r = _backup_one(task, api, cr, vm_row, j)
            results.append(r)
            if r['status'] != 'done':
                failed += 1
    with db.conn() as c:
        c.execute('UPDATE jobs SET last_status=? WHERE id=?', ('failed' if failed else 'done', job_id))
    if failed:
        raise RuntimeError(f'{failed} of {len(ids)} VMs failed')
    return results


def vm_busy(vm_id):
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) FROM runs WHERE vm_id=? AND status='running'", (vm_id,)).fetchone()[0] > 0


def backup_vm_now(vm_id, created_by=None, quiesce=True):
    with db.conn() as c:
        vm_row = db.row_to_dict(c.execute('SELECT * FROM vms WHERE id=?', (vm_id,)).fetchone())
    if not vm_row:
        raise RuntimeError('vm not found')
    if vm_busy(vm_id):
        raise RuntimeError(f'a backup of {vm_row["name"]} is already running')
    return tasks.create('backup_vm', subject=vm_row['name'], created_by=created_by, vm_id=vm_id, quiesce=quiesce)


def backup_vm(task, vm_id, quiesce=True):
    with db.conn() as c:
        vm_row = db.row_to_dict(c.execute('SELECT * FROM vms WHERE id=?', (vm_id,)).fetchone())
    cr = connection(vm_row['connection_id'])
    with open_api(cr) as api:
        r = _backup_one(task, api, cr, vm_row, {'id': None, 'quiesce': int(quiesce), 'retention': None})
    if r['status'] != 'done':
        raise RuntimeError(r.get('error', 'failed'))
    return r


def _backup_one(task, api, cr, vm_row, job):
    with db.conn() as c:
        c.execute('INSERT INTO runs(job_id,vm_id,task_id,started,status) VALUES(?,?,?,?,?)',
                  (job['id'], vm_row['id'], task.id, time.time(), 'running'))
        run_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    try:
        proxy = pick_proxy(cr['id'])
        if proxy is None and vm_row['power_state'] == 'poweredOn':
            # maybe a proxy exists but is tied to no connection yet
            for p in online_proxies():
                if p['connection_id'] is None and p.get('bios_uuid') and api.vm_by_uuid(p['bios_uuid']) is not None:
                    with db.conn() as c:
                        c.execute('UPDATE proxies SET connection_id=? WHERE id=?', (cr['id'], p['id']))
                    proxy = p
                    break
        rp_id, stats = vmware.backup_vm(task, api, vm_row, run_id, proxy, quiesce=bool(job.get('quiesce', 1)))
        with db.conn() as c:
            c.execute('UPDATE runs SET finished=?, status=?, kind=?, transport=?, bytes_read=?, bytes_stored=?, restore_point_id=? WHERE id=?',
                      (time.time(), 'done', stats['kind'], stats['transport'], stats['bytes_read'], stats.get('bytes_stored', 0), rp_id, run_id))
        if job.get('retention'):
            apply_retention(task, vm_row['id'], job['retention'])
        return {'vm': vm_row['name'], 'status': 'done', 'restore_point': rp_id, **stats}
    except tasks.Cancelled:
        with db.conn() as c:
            c.execute('UPDATE runs SET finished=?, status=?, error=? WHERE id=?', (time.time(), 'cancelled', 'cancelled', run_id))
        raise
    except Exception as e:  # noqa: BLE001
        log.exception('backup of %s failed', vm_row['name'])
        task.log(f'{vm_row["name"]}: FAILED: {type(e).__name__}: {e}')
        vct = getattr(task, 'vc_task', None)
        if vct is not None:
            vct.failed(f'{type(e).__name__}: {e}')
        try:
            vmware.vsp.post_event(api, api.vm_by_moref(vm_row['moref']), vmware.vsp.EVENT_FAILED, f'hvbackup backup of {vm_row["name"]} failed: {type(e).__name__}: {e}')
        except Exception:  # noqa: BLE001
            pass
        with db.conn() as c:
            c.execute('UPDATE runs SET finished=?, status=?, error=? WHERE id=?', (time.time(), 'failed', f'{type(e).__name__}: {e}'[:1000], run_id))
        return {'vm': vm_row['name'], 'status': 'failed', 'error': str(e)}


# ── retention ────────────────────────────────────────────────────────
def apply_retention(task, vm_id, policy):
    keep_last = int(policy.get('keep_last') or 0)
    keep_days = float(policy.get('keep_days') or 0)
    with db.conn() as c:
        rps = [db.row_to_dict(r) for r in c.execute('SELECT * FROM restore_points WHERE vm_id=? AND deleted=0 ORDER BY id DESC', (vm_id,))]
    now = time.time()
    doomed = []
    for i, rp in enumerate(rps):
        if i == 0:
            continue  # always keep the newest: it is the base of the next incremental
        by_count = keep_last and i >= keep_last
        by_age = keep_days and now - rp['created'] > keep_days * 86400
        if (keep_last and keep_days and by_count and by_age) or (keep_last and not keep_days and by_count) or (keep_days and not keep_last and by_age):
            doomed.append(rp)
    for rp in doomed:
        delete_restore_point(rp['id'])
        task.log(f'retention: removed restore point {rp["id"]} from {time.strftime("%Y-%m-%d", time.localtime(rp["created"]))}')
    return len(doomed)


def delete_restore_point(rp_id):
    with db.conn() as c:
        rp = db.row_to_dict(c.execute('SELECT * FROM restore_points WHERE id=?', (rp_id,)).fetchone())
        if not rp or rp['deleted']:
            return
        newest = c.execute('SELECT id FROM restore_points WHERE vm_id=? AND deleted=0 ORDER BY id DESC LIMIT 1', (rp['vm_id'],)).fetchone()
        c.execute('UPDATE restore_points SET deleted=1 WHERE id=?', (rp_id,))
        if newest and newest['id'] == rp_id:
            c.execute('DELETE FROM cbt_state WHERE vm_id=?', (rp['vm_id'],))  # next backup must be a full
    try:
        repo().delete_manifest(rp['manifest'])
    except FileNotFoundError:
        pass


def gc(task, grace_seconds=3600):
    task.log('garbage collection: marking live chunks')
    res = repo().gc(grace_seconds=grace_seconds, log=task.log)
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_gc',?)", (str(time.time()),))
    return res


def maybe_gc():
    """Called by the scheduler loop: run GC once a day at ~03:00 local time."""
    with db.conn() as c:
        r = c.execute("SELECT value FROM meta WHERE key='last_gc'").fetchone()
        running = c.execute("SELECT COUNT(*) FROM tasks WHERE kind='gc' AND state IN ('queued','running')").fetchone()[0]
    last = float(r['value']) if r else 0
    lt = time.localtime()
    if not running and lt.tm_hour == 3 and time.time() - last > 20 * 3600:
        tasks.create('gc', subject='nightly', created_by='scheduler')


# ── restore ──────────────────────────────────────────────────────────
def restore(task, restore_point_id, options):
    with db.conn() as c:
        rp = db.row_to_dict(c.execute('SELECT * FROM restore_points WHERE id=?', (restore_point_id,)).fetchone())
        if not rp or rp['deleted']:
            raise RuntimeError('restore point not found')
        vm_row = db.row_to_dict(c.execute('SELECT * FROM vms WHERE id=?', (rp['vm_id'],)).fetchone())
    conn_id = options.get('connection_id') or vm_row['connection_id']
    cr = connection(conn_id)
    if cr['type'] not in ('vcenter', 'esxi'):
        raise RuntimeError(f'restore to a {cr["type"]} connection is not supported yet')
    # The source connection is never contacted: a restore reads only the repository, so a
    # vCenter that no longer exists is no obstacle to restoring its VMs elsewhere.
    with open_api(cr) as api:
        proxy = pick_proxy(cr['id'])
        where = cr['name'] if conn_id == vm_row['connection_id'] else f'{cr["name"]} (a different connection than the backup came from)'
        task.log(f'restore point {rp["id"]} of {vm_row["name"]} -> {where} via {"proxy " + proxy["name"] if proxy else "HTTPS upload"}')
        try:
            res = vmware.restore_vm(task, api, rp, options, proxy)
        except Exception as e:
            vct = getattr(task, 'vc_task', None)
            if vct is not None:
                vct.failed(f'{type(e).__name__}: {e}')
            raise
    tasks.create('inventory', subject=cr['name'], created_by='restore', connection_id=cr['id'])
    return res


def register_tasks():
    from . import export as export_mod
    tasks.register('export', export_mod.export_restore_point)
    tasks.register('inventory', refresh_inventory)
    tasks.register('backup_job', backup_job)
    tasks.register('backup_vm', backup_vm)
    tasks.register('restore', restore)
    tasks.register('gc', gc)


def scheduler_run_job(job_id, created_by):
    run_job(job_id, created_by=created_by)
    maybe_gc()
