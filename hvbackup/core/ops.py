"""Operations behind the API: inventory refresh, backup jobs, restores, retention, GC.
Each is registered as a task kind and runs in a task thread."""
import json
import logging
import threading
import time

from .. import db
from ..providers import proxmox, vmware
from ..store.chunks import repo
from . import scheduler, secrets, tasks, uuids

log = logging.getLogger('hvbackup.ops')
PROXY_FRESH = 120  # seconds since last hello for a proxy to count as online
_slots = {}
_slots_guard = threading.Lock()
_running = {}


def concurrency():
    """Backups/restores/syncs running at once per connection (Settings)."""
    from . import settings
    return max(int(settings.get('concurrency')), 1)



class connection_slot:
    """At most `concurrency()` long operations (backup, restore, replication) run against one
    connection at a time, so a scheduler firing many jobs on many vCenters does not start a
    snapshot storm; the rest wait here, cancellable. The limit is read at every acquire, so a
    change in Settings applies to the next operation."""

    def __init__(self, task, conn_id, what='work'):
        self.task, self.conn_id, self.what = task, conn_id, what
        with _slots_guard:
            self.cv = _slots.setdefault(conn_id, threading.Condition())
            _running.setdefault(conn_id, 0)

    def __enter__(self):
        waited = False
        with self.cv:
            while _running[self.conn_id] >= concurrency():
                if not waited:
                    self.task.log(f'waiting for a free slot on this connection ({concurrency()} {self.what} operations already running there)')
                    waited = True
                self.cv.wait(timeout=5)
                self.task.check_cancel()
            _running[self.conn_id] += 1
        return self

    def __exit__(self, *a):
        with self.cv:
            _running[self.conn_id] -= 1
            self.cv.notify_all()


def connection(conn_id):
    with db.conn() as c:
        r = db.row_to_dict(c.execute('SELECT * FROM connections WHERE id=?', (conn_id,)).fetchone())
    if not r:
        raise RuntimeError(f'connection {conn_id} not found')
    return r


def open_api(conn_row):
    if conn_row['type'] in ('vcenter', 'esxi'):
        return vmware.VMware(conn_row['host'], conn_row['username'], secrets.decrypt(conn_row['secret_enc']), bool(conn_row['verify_ssl']))
    if conn_row['type'] == 'proxmox':
        return proxmox.Proxmox(conn_row['host'], conn_row['username'], secrets.decrypt(conn_row['secret_enc']), bool(conn_row['verify_ssl']))
    raise RuntimeError(f'provider {conn_row["type"]} not implemented yet')


def pve_setup(conn_row):
    """What an admin needs to connect a Proxmox cluster: the one-line node installer and the
    storage registration state."""
    from . import settings
    base = settings.public_url()
    with open_api(conn_row) as api:
        about = api.about()
    return {'install': f'curl -ksS {base}/api/v1/pve/install.sh | bash' if base else None,
            'public_url': base or None, 'nodes': about.get('nodes', []), 'storage': about.get('pve_storage'),
            'storages': about.get('pve_storages', [])}


def pve_register_storage(conn_row, storage_id='hvbackup'):
    """Register (or re-key) the hvbackup storage on the cluster. The storage entry carries a
    proxy token tied to this connection; every node's plug-in uses it."""
    from . import auth, settings, tls
    import hashlib
    import urllib.parse
    if not settings.public_url():
        raise RuntimeError('set the public URL of this server first (Settings > Server, or HVB_URL)')
    u = urllib.parse.urlparse(settings.public_url())
    name = f'pve-{conn_row["name"]}'
    with db.conn() as c:
        old = c.execute('SELECT id FROM proxies WHERE name=?', (name,)).fetchone()
        if old:
            c.execute('DELETE FROM proxies WHERE id=?', (old[0],))
    pid, token = auth.create_proxy(name, conn_row['id'])
    fingerprint = None
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
        crt, _ = tls.ensure_cert()
        der = x509.load_pem_x509_certificate(open(crt, 'rb').read()).public_bytes(Encoding.DER)
        fingerprint = ':'.join(f'{b:02X}' for b in hashlib.sha256(der).digest())
    except Exception as e:  # noqa: BLE001
        log.warning('cannot compute the server certificate fingerprint: %s', e)
    with open_api(conn_row) as api:
        state = api.storage_register(storage_id, u.hostname, u.port or 8460, token, fingerprint)
        about = api.about()
    with db.conn() as c:
        c.execute('UPDATE connections SET info_json=? WHERE id=?', (json.dumps(about), conn_row['id']))
    return {'storage': storage_id, 'state': state, 'proxy_id': pid, 'fingerprint': fingerprint}


def test_connection(conn_row):
    with open_api(conn_row) as api:
        return api.about()


def vsphere_plugin(conn_row, action):
    from ..providers import vsphere_plugin as vsp
    from . import settings
    if action in ('register', 'update') and not settings.public_url():
        raise RuntimeError('set the public URL of this server first (Settings > Server, or HVB_URL) before registering the plug-in')
    with open_api(conn_row) as api:
        if not api.about()['is_vcenter']:
            raise RuntimeError('the vSphere Client plug-in registers with vCenter, not a standalone ESXi host')
        if action in ('register', 'update'):
            return vsp.register(api, settings.public_url())
        if action == 'unregister':
            return vsp.unregister(api)
        return vsp.status(api)


def placement(conn_row):
    with open_api(conn_row) as api:
        return api.placement()


def online_proxies(conn_id=None):
    """Online proxies; for a connection, its own proxies first, then proxies tied to another
    connection whose VM this connection also sees (a host managed by a vCenter and added as a
    standalone connection too, or two vCenters sharing a cluster's storage). Linked rows carry
    via_link=True and link_moref: the proxy VM's moref on *this* connection."""
    with db.conn() as c:
        rows = [db.row_to_dict(r) for r in c.execute('SELECT * FROM proxies WHERE enabled=1 AND last_seen>=?', (time.time() - PROXY_FRESH,))]
        links = {}
        if conn_id is not None:
            links = {r['proxy_id']: r['vm_moref'] for r in c.execute('SELECT proxy_id, vm_moref FROM proxy_links WHERE connection_id=?', (conn_id,))}
    if conn_id is None:
        return rows
    own = [r for r in rows if r['connection_id'] == conn_id]
    linked = [{**r, 'via_link': True, 'link_moref': links[r['id']]} for r in rows if r['connection_id'] != conn_id and r['id'] in links]
    return own + linked


def pick_proxy(conn_id):
    ps = online_proxies(conn_id)
    return ps[0] if ps else None


def _uuid_key(u):
    if not u:
        return None
    return uuids.normalise(u) or str(u).strip().lower()


def adopt_moved_vms(c, task, connection_id, vms):
    """A VM that left another connection (cross-vCenter vMotion, a cluster migration, an
    unregister and re-register that gave it a new moref) keeps its row, history, restore points
    and CBT state: the gone row with the same UUID is re-pointed at this connection and moref.
    Two live rows with the same UUID (a host seen both directly and through its vCenter) are
    left alone; a row here with its own restore points is never overwritten."""
    moved = []
    for v in vms:
        key = _uuid_key(v.get('uuid'))
        if not key:
            continue
        cur = c.execute('SELECT id FROM vms WHERE connection_id=? AND moref=?', (connection_id, v['moref'])).fetchone()
        cur_id = cur['id'] if cur else None
        if cur_id is not None:
            own = c.execute('SELECT COUNT(*) FROM restore_points WHERE vm_id=? AND deleted=0', (cur_id,)).fetchone()[0]
            if own:
                continue
        cands = []
        for r in c.execute('SELECT v.id, v.connection_id, v.moref, v.name, v.uuid, '
                           ' (SELECT COUNT(*) FROM restore_points rp WHERE rp.vm_id=v.id AND rp.deleted=0) AS rps,'
                           ' (SELECT MAX(created) FROM restore_points rp WHERE rp.vm_id=v.id AND rp.deleted=0) AS newest'
                           ' FROM vms v WHERE v.gone=1 AND v.uuid IS NOT NULL AND v.id!=?', (cur_id or -1,)):
            if _uuid_key(r['uuid']) == key and r['rps']:
                cands.append(dict(r))
        if not cands:
            continue
        old = max(cands, key=lambda r: (r['newest'] or 0, r['rps']))
        old_conn = c.execute('SELECT name FROM connections WHERE id=?', (old['connection_id'],)).fetchone()
        if cur_id is not None:
            # the fresh row has no history: jobs that already name it follow the surviving row
            for j in c.execute('SELECT id, vm_ids_json FROM jobs WHERE connection_id=?', (connection_id,)).fetchall():
                ids = json.loads(j['vm_ids_json'] or '[]')
                if cur_id in ids:
                    c.execute('UPDATE jobs SET vm_ids_json=? WHERE id=?', (json.dumps([old['id'] if i == cur_id else i for i in ids]), j['id']))
            c.execute('DELETE FROM vms WHERE id=?', (cur_id,))
        c.execute('UPDATE vms SET connection_id=?, moref=?, gone=0 WHERE id=?', (connection_id, v['moref'], old['id']))
        where = f'{old_conn["name"] if old_conn else "connection " + str(old["connection_id"])} ({old["moref"]})'
        task.log(f'{v["name"]}: the same VM (UUID {v["uuid"]}) left {where}; its {old["rps"]} restore point(s) and CBT state now belong to it here')
        c.execute('INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)',
                  (time.time(), 'inventory', 'vm_moved', f'{v["name"]} {old["uuid"]}: {where} -> connection {connection_id} ({v["moref"]})'))
        moved.append({'vm_id': old['id'], 'name': v['name'], 'from_connection_id': old['connection_id'], 'from_moref': old['moref']})
    return moved


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
        moved = adopt_moved_vms(c, task, connection_id, vms)
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
        # (a proxy given a connection explicitly keeps it: a host that is also managed by a vCenter shows the same VM twice)
        for p in c.execute('SELECT id,name,connection_id,vm_moref,bios_uuid FROM proxies WHERE bios_uuid IS NOT NULL').fetchall():
            hit = next((v for v in vms if uuids.same_vm(v['uuid'], p['bios_uuid'])), None)
            if hit is None:
                c.execute('DELETE FROM proxy_links WHERE proxy_id=? AND connection_id=?', (p['id'], connection_id))
                continue
            if p['connection_id'] is None or (p['connection_id'] == connection_id and not p['vm_moref']):
                c.execute('UPDATE proxies SET connection_id=?, vm_moref=? WHERE id=?', (connection_id, hit['moref'], p['id']))
                task.log(f'proxy {p["name"]} is VM {hit["name"]} on this connection')
            elif p['connection_id'] != connection_id:
                # tied elsewhere, but its VM is visible here too: usable from here as well
                fresh = c.execute('SELECT 1 FROM proxy_links WHERE proxy_id=? AND connection_id=?', (p['id'], connection_id)).fetchone() is None
                c.execute('INSERT OR REPLACE INTO proxy_links(proxy_id,connection_id,vm_moref,seen) VALUES(?,?,?,?)', (p['id'], connection_id, hit['moref'], now))
                if fresh:
                    task.log(f'proxy {p["name"]} (tied to another connection) is also VM {hit["name"]} here; backups and restores on this connection can use it')
    task.log(f'{len(vms)} VMs' + (f', {len(moved)} moved here from another connection' if moved else ''))
    return {'vms': len(vms), 'moved': moved}


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
            if vm_row['connection_id'] != j['connection_id']:
                other = connection(vm_row['connection_id'])
                task.log(f'{vm_row["name"]}: moved to {other["name"]}; add it to a job on that connection (its restore points went with it), skipped')
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
    if vm_row.get('is_template'):
        raise RuntimeError(f'{vm_row["name"]} is a template: vSphere cannot snapshot templates. Convert it to a VM or clone it, then back that up')
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
        if vm_row.get('is_template'):
            raise RuntimeError(f'{vm_row["name"]} is a template: vSphere cannot snapshot templates. Convert it to a VM or clone it, then back that up')
        proxy = pick_proxy(cr['id'])
        if proxy is None and vm_row['power_state'] == 'poweredOn':
            # maybe a proxy exists but is tied to no connection yet
            for p in online_proxies():
                if p['connection_id'] is None and p.get('bios_uuid') and api.vm_by_uuid(p['bios_uuid']) is not None:
                    with db.conn() as c:
                        c.execute('UPDATE proxies SET connection_id=? WHERE id=?', (cr['id'], p['id']))
                    proxy = p
                    break
        with connection_slot(task, cr['id'], 'backup'):
            if cr['type'] == 'proxmox':
                rp_id, stats = proxmox.backup_vm(task, api, cr, vm_row, run_id, quiesce=bool(job.get('quiesce', 1)))
            else:
                rp_id, stats = vmware.backup_vm(task, api, vm_row, run_id, proxy, quiesce=bool(job.get('quiesce', 1)))
        with db.conn() as c:
            c.execute('UPDATE runs SET finished=?, status=?, kind=?, transport=?, bytes_read=?, bytes_stored=?, restore_point_id=? WHERE id=?',
                      (time.time(), 'done', stats['kind'], stats['transport'], stats['bytes_read'], stats.get('bytes_stored', 0), rp_id, run_id))
        if job.get('retention'):
            apply_retention(task, vm_row['id'], job['retention'])
        on_new_restore_point(task, vm_row['id'], rp_id)
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
        if cr['type'] != 'proxmox':
            try:
                vmware.vsp.post_event(api, api.vm_by_moref(vm_row['moref']), vmware.vsp.EVENT_FAILED, f'hvbackup backup of {vm_row["name"]} failed: {type(e).__name__}: {e}')
            except Exception:  # noqa: BLE001
                pass
        with db.conn() as c:
            c.execute('UPDATE runs SET finished=?, status=?, error=? WHERE id=?', (time.time(), 'failed', f'{type(e).__name__}: {e}'[:1000], run_id))
        from . import notify
        notify.send('backup_failed', f'Backup of {vm_row["name"]} failed', f'{type(e).__name__}: {e}'[:500], key=vm_row['name'],
                    vm=vm_row['name'], connection=cr['name'], run_id=run_id, task_id=task.id)
        return {'vm': vm_row['name'], 'status': 'failed', 'error': str(e)}


def on_new_restore_point(task, vm_id, rp_id):
    """A fresh restore point of a VM: replication jobs that follow backups of it start a sync."""
    try:
        from . import replication
        started = replication.on_new_restore_point(vm_id, rp_id)
        for name, tid in started:
            task.log(f'replication {name}: sync started (task {tid})')
    except Exception as e:  # noqa: BLE001
        log.exception('replication trigger failed')
        task.log(f'WARNING: could not start replication after this backup: {e}')


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


def protection_of(c, vm_id):
    """What protects a VM: the backup jobs and replications naming it (with how many other VMs
    each has), its replicas on other connections, and its restore points."""
    jobs, replications = [], []
    for j in c.execute('SELECT id,name,vm_ids_json,schedule,enabled FROM jobs ORDER BY name').fetchall():
        ids = json.loads(j['vm_ids_json'] or '[]')
        if vm_id in ids:
            jobs.append({'id': j['id'], 'name': j['name'], 'schedule': j['schedule'], 'enabled': bool(j['enabled']), 'others': len(ids) - 1})
    for r in c.execute('SELECT r.id,r.name,r.vm_ids_json,r.enabled,t.name AS target_connection_name FROM replications r '
                       'JOIN connections t ON t.id=r.target_connection_id ORDER BY r.name').fetchall():
        ids = json.loads(r['vm_ids_json'] or '[]')
        if vm_id in ids:
            replications.append({'id': r['id'], 'name': r['name'], 'enabled': bool(r['enabled']), 'target_connection_name': r['target_connection_name'], 'others': len(ids) - 1})
    replicas = [db.row_to_dict(r) for r in c.execute('SELECT r.id,r.name,r.moref,r.state,r.replication_id,r.target_connection_id,t.name AS target_connection_name '
                                                      'FROM replicas r JOIN connections t ON t.id=r.target_connection_id WHERE r.vm_id=? ORDER BY r.name', (vm_id,)).fetchall()]
    n, allocated = c.execute('SELECT COUNT(*), COALESCE(SUM(allocated_bytes),0) FROM restore_points WHERE vm_id=? AND deleted=0', (vm_id,)).fetchone()
    return {'jobs': jobs, 'replications': replications, 'replicas': replicas, 'restore_points': n, 'restore_points_bytes': allocated,
            'protected': bool(jobs or replications)}


def unprotect_vm(vm_id, delete_restore_points=False, delete_replica_vms=False, user=None):
    """Take a VM out of every backup job and replication. A job or replication left with no VM
    is deleted. Its replicas are forgotten (the standby copy is no longer maintained); with
    delete_replica_vms the copies are destroyed on their targets too, otherwise they stay as
    ordinary VMs there. Restore points are kept unless delete_restore_points; kept points stay
    restorable and the VM shows as "not in a job" until they are deleted."""
    from . import replication
    with db.conn() as c:
        vm = db.row_to_dict(c.execute('SELECT id,name FROM vms WHERE id=?', (vm_id,)).fetchone())
        if not vm:
            raise RuntimeError('VM not found')
        info = protection_of(c, vm_id)
        out = {'vm_id': vm_id, 'name': vm['name'], 'jobs_left': [], 'jobs_deleted': [], 'replications_left': [], 'replications_deleted': [],
               'replicas_forgotten': 0, 'replica_vms_destroyed': 0, 'restore_points_deleted': 0}
        for table, items, left, gone in (('jobs', info['jobs'], out['jobs_left'], out['jobs_deleted']),
                                         ('replications', info['replications'], out['replications_left'], out['replications_deleted'])):
            for it in items:
                ids = [i for i in json.loads(c.execute(f'SELECT vm_ids_json FROM {table} WHERE id=?', (it['id'],)).fetchone()[0] or '[]') if i != vm_id]
                if ids:
                    c.execute(f'UPDATE {table} SET vm_ids_json=? WHERE id=?', (json.dumps(ids), it['id']))
                    left.append(it['name'])
                else:
                    c.execute(f'DELETE FROM {table} WHERE id=?', (it['id'],))
                    gone.append(it['name'])
        rp_ids = [r['id'] for r in c.execute('SELECT id FROM restore_points WHERE vm_id=? AND deleted=0 ORDER BY id', (vm_id,)).fetchall()] if delete_restore_points else []
    scheduler.refresh_next_runs()
    for r in info['replicas']:
        res = replication.forget(r['id'], delete_vm=delete_replica_vms, user=user)
        out['replicas_forgotten'] += 1
        out['replica_vms_destroyed'] += int(bool(res.get('vm_destroyed')))
    for rp_id in rp_ids:
        delete_restore_point(rp_id)
        out['restore_points_deleted'] += 1
    db.audit(user or 'api', 'vm_unprotect', f'{vm["name"]} ({vm_id}): ' + ', '.join(
        f'{k} {v}' for k, v in out.items() if k not in ('vm_id', 'name') and v))
    return out


def gc(task, grace_seconds=3600):
    task.log('garbage collection: marking live chunks')
    res = repo().gc(grace_seconds=grace_seconds, log=task.log)
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_gc',?)", (str(time.time()),))
    return res


def maybe_gc():
    """Called by the scheduler loop: run GC once a day at ~03:00 in the configured zone, and
    the other daily chores (catalog copy, update check) alongside."""
    from . import catalog_backup, updates, verify
    try:
        catalog_backup.maybe_copy()
        updates.check()
        verify.maybe_schedule()
    except Exception:  # noqa: BLE001
        log.exception('daily chores failed')
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
    if cr['type'] not in ('vcenter', 'esxi', 'proxmox'):
        raise RuntimeError(f'restore to a {cr["type"]} connection is not supported yet')
    # a restore onto the other platform is a conversion first: the converted restore point
    # (kept, so the next restore of the same point is immediate) then restores normally
    from ..store.chunks import repo
    from . import convert
    src_platform = repo().read_manifest(rp['manifest']).get('platform') or 'vmware'
    target_platform = 'proxmox' if cr['type'] == 'proxmox' else 'vmware'
    if src_platform != target_platform:
        task.log(f'restore point {rp["id"]} is a {src_platform} image and {cr["name"]} is {target_platform}: converting first')
        new_id = convert.ensure_converted(task, rp, target_platform, options)
        with db.conn() as c:
            rp = db.row_to_dict(c.execute('SELECT * FROM restore_points WHERE id=?', (new_id,)).fetchone())
    # The source connection is never contacted: a restore reads only the repository, so a
    # vCenter that no longer exists is no obstacle to restoring its VMs elsewhere.
    with open_api(cr) as api, connection_slot(task, cr['id'], 'restore'):
        where = cr['name'] if conn_id == vm_row['connection_id'] else f'{cr["name"]} (a different connection than the backup came from)'
        if cr['type'] == 'proxmox':
            task.log(f'restore point {rp["id"]} of {vm_row["name"]} -> {where} through the Proxmox restore API')
            res = proxmox.restore_vm(task, api, cr, rp, options)
            tasks.create('inventory', subject=cr['name'], created_by='restore', connection_id=cr['id'])
            return res
        proxy = pick_proxy(cr['id'])
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
    from . import proxy_deploy
    tasks.register('deploy_proxy', proxy_deploy.deploy_proxy)
    from . import convert
    tasks.register('convert', convert.convert_task)
    from . import replication
    tasks.register('replicate', replication.replicate_task)
    from . import verify
    tasks.register('verify', verify.verify_task)
    from . import windows_tools
    tasks.register('windows_tools', lambda task, name: {'path': windows_tools.download(name, task)})


def scheduler_run_job(job_id, created_by):
    run_job(job_id, created_by=created_by)
    maybe_gc()


def scheduler_run_replication(replication_id, created_by):
    from . import replication
    replication.run(replication_id, created_by=created_by)
