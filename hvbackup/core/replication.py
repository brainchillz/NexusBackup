"""Scheduled replication: a standby copy of a VM on another connection, kept current.

A backup is passive; a replica is a ready-to-boot VM on the target hypervisor (another
vCenter, a standalone host, a Proxmox cluster) that follows the source VM's restore
points. The source is never contacted: every sync reads only the repository.

  first sync      a restore of the newest restore point onto the target (converted first
                  when the platforms differ), powered off, named <vm><suffix>
  later syncs     VMware target: only the chunks that differ between the restore point the
                  replica carries and the newest one are written into the replica's
                  existing disks through a proxy (zeros included, so freed blocks clear);
                  without a proxy on the target the disks are re-uploaded whole over HTTPS.
                  Proxmox target: the guest is restored again over the same vmid (Proxmox
                  restores read only allocated chunks, so this is fast), MACs re-applied.
  trigger         after every backup of a member VM, and/or on a cron schedule
  failover        power the replica on (optionally with its NICs disconnected for a test);
                  syncing stops for it until it is forgotten or the test is ended
  failback        back the failed-over VM up on the target (it is an ordinary VM there) and
                  restore it to the original site like any restore

Conversions for replication pin the identity (UUID and MAC addresses) of the first sync, so
the guest's pinned interface names and the replica's NICs never drift apart.
"""
import json
import logging
import time

from .. import db
from ..store.chunks import ZERO_HASH, repo
from . import tasks

log = logging.getLogger('hvbackup.replication')
MIN_AGENT_FOR_INCREMENTAL = (0, 2, 0)  # the agent must write zero chunks into an existing disk
DEFAULT_OPTIONS = {'suffix': '-replica', 'host': None, 'datastore': None, 'folder': None, 'networks': {}, 'new_identity': True,
                   'install_tools': False}


# ── pure helpers ──────────────────────────────────────────────────────
def changed_records(old_hashes, new_hashes):
    """(index, hash) of every chunk whose content differs between two indexes of one disk.
    Chunks beyond the old index (a grown disk) count as changed unless zero; a chunk that
    became zero is included with ZERO_HASH so the replica's block is cleared."""
    out = []
    n = max(len(old_hashes), len(new_hashes))
    for i in range(n):
        a = old_hashes[i] if i < len(old_hashes) else ZERO_HASH
        b = new_hashes[i] if i < len(new_hashes) else ZERO_HASH
        if a != b:
            out.append((i, b))
    return out


def agent_version_ok(version):
    try:
        parts = tuple(int(x) for x in str(version or '0').split('.')[:3])
    except ValueError:
        return False
    return parts >= MIN_AGENT_FOR_INCREMENTAL


def replica_name(vm_name, options):
    return f'{vm_name}{options.get("suffix") if options.get("suffix") is not None else DEFAULT_OPTIONS["suffix"]}'


def options_of(repl):
    o = dict(DEFAULT_OPTIONS)
    o.update(repl.get('options') or {})
    o['networks'] = dict(o.get('networks') or {})
    return o


def plan(replica, newest_rp_id):
    """What the next sync of a replica does: 'full' (no replica yet), 'rebuild' (its disks were
    written by a test boot or it is broken), 'incremental' (carry it from its point to the
    newest), 'skip:<reason>' (nothing to do)."""
    if replica is None:
        return 'full'
    st = replica.get('state')
    if st in ('failed_over', 'test'):
        return 'skip:failed over' if st == 'failed_over' else 'skip:test failover running'
    if st == 'dirty' or not replica.get('moref'):
        return 'rebuild'
    if replica.get('source_rp_id') == newest_rp_id and st == 'synced':
        return 'skip:up to date'
    return 'incremental'


# ── catalog ───────────────────────────────────────────────────────────
def get(rid):
    with db.conn() as c:
        return db.row_to_dict(c.execute('SELECT * FROM replications WHERE id=?', (rid,)).fetchone())


def newest_restore_point(vm_id):
    """The newest restore point of the VM that is an original backup (not a conversion)."""
    with db.conn() as c:
        return db.row_to_dict(c.execute("SELECT * FROM restore_points WHERE vm_id=? AND deleted=0 AND source_rp_id IS NULL AND manifest!='' ORDER BY id DESC LIMIT 1",
                                        (vm_id,)).fetchone())


def replica_for(vm_id, target_connection_id):
    with db.conn() as c:
        return db.row_to_dict(c.execute('SELECT * FROM replicas WHERE vm_id=? AND target_connection_id=?', (vm_id, target_connection_id)).fetchone())


def run(replication_id, created_by=None, vm_ids=None):
    r = get(replication_id)
    if not r:
        raise RuntimeError('replication not found')
    return tasks.create('replicate', subject=r['name'], created_by=created_by, replication_id=replication_id, vm_ids=vm_ids)


def _sync_pending(replication_id, vm_id):
    with db.conn() as c:
        for t in c.execute("SELECT result_json FROM tasks WHERE kind='replicate' AND state IN ('queued','running')"):
            try:
                p = json.loads(t[0] or '{}').get('params', {})
            except ValueError:
                continue
            if p.get('replication_id') == replication_id and (not p.get('vm_ids') or vm_id in p['vm_ids']):
                return True
    return False


def on_new_restore_point(vm_id, rp_id):
    """Start a sync in every enabled replication that follows backups and includes this VM."""
    started = []
    with db.conn() as c:
        rows = [db.row_to_dict(r) for r in c.execute('SELECT * FROM replications WHERE enabled=1 AND after_backup=1')]
    for r in rows:
        if vm_id in (r.get('vm_ids') or []) and not _sync_pending(r['id'], vm_id):
            started.append((r['name'], run(r['id'], created_by='backup', vm_ids=[vm_id])))
    return started


# ── the task ──────────────────────────────────────────────────────────
def replicate_task(task, replication_id, vm_ids=None):
    from . import ops
    r = get(replication_id)
    if not r:
        raise RuntimeError('replication not found')
    with db.conn() as c:
        c.execute("UPDATE replications SET last_run=?, last_status='running' WHERE id=?", (time.time(), replication_id))
    ids = vm_ids or r['vm_ids']
    target = ops.connection(r['target_connection_id'])
    opts = options_of(r)
    results, failed = [], 0
    for n, vm_id in enumerate(ids):
        task.check_cancel()
        with db.conn() as c:
            vm_row = db.row_to_dict(c.execute('SELECT * FROM vms WHERE id=?', (vm_id,)).fetchone())
        if not vm_row:
            task.log(f'vm {vm_id}: not in the catalog, skipped')
            failed += 1
            continue
        task.log(f'[{n + 1}/{len(ids)}] {vm_row["name"]} -> {target["name"]}')
        try:
            res = sync_one(task, r, opts, vm_row, target)
        except tasks.Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception('replication of %s failed', vm_row['name'])
            task.log(f'{vm_row["name"]}: FAILED: {type(e).__name__}: {e}')
            res = {'vm': vm_row['name'], 'status': 'failed', 'error': str(e)}
            failed += 1
        results.append(res)
    with db.conn() as c:
        c.execute('UPDATE replications SET last_status=? WHERE id=?', ('failed' if failed else 'done', replication_id))
    if failed:
        raise RuntimeError(f'{failed} of {len(ids)} VMs failed')
    return results


def sync_one(task, repl, opts, vm_row, target_cr):
    from . import convert, ops
    rep = repo()
    replica = replica_for(vm_row['id'], target_cr['id'])
    rp = newest_restore_point(vm_row['id'])
    if not rp:
        task.log(f'{vm_row["name"]}: no restore point yet; back it up first')
        return {'vm': vm_row['name'], 'status': 'skipped', 'reason': 'no restore point'}
    what = plan(replica, rp['id'])
    if what.startswith('skip:'):
        task.log(f'{vm_row["name"]}: {what[5:]}' + (f' (restore point {rp["id"]})' if what.endswith('date') else ''))
        return {'vm': vm_row['name'], 'status': 'skipped', 'reason': what[5:]}
    name = (replica or {}).get('name') or replica_name(vm_row['name'], opts)
    with db.conn() as c:
        if replica is None:
            c.execute('INSERT INTO replicas(replication_id,vm_id,target_connection_id,name,state,created) VALUES(?,?,?,?,?,?)',
                      (repl['id'], vm_row['id'], target_cr['id'], name, 'new', time.time()))
            replica = db.row_to_dict(c.execute('SELECT * FROM replicas WHERE id=?', (c.execute('SELECT last_insert_rowid()').fetchone()[0],)).fetchone())
        c.execute("UPDATE replicas SET state='syncing', last_task_id=?, replication_id=? WHERE id=?", (task.id, repl['id'], replica['id']))
        c.execute('INSERT INTO runs(job_id,vm_id,task_id,started,status,kind) VALUES(?,?,?,?,?,?)', (None, vm_row['id'], task.id, time.time(), 'running', 'replicate'))
        run_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    try:
        manifest = rep.read_manifest(rp['manifest'])
        src_platform = manifest.get('platform') or 'vmware'
        target_platform = 'proxmox' if target_cr['type'] == 'proxmox' else 'vmware'
        push = rp
        identity = replica.get('identity') or {}
        if src_platform != target_platform:
            conv_opts = {'new_identity': opts['new_identity'], 'install_tools': opts['install_tools'], 'name': name}
            if identity.get('uuid'):
                conv_opts['identity'] = {'uuid': identity['uuid'], 'macs': identity.get('macs') or []}
            task.log(f'{vm_row["name"]}: restore point {rp["id"]} is a {src_platform} image and {target_cr["name"]} is {target_platform}: converting first')
            new_id = convert.ensure_converted(task, rp, target_platform, conv_opts)
            with db.conn() as c:
                push = db.row_to_dict(c.execute('SELECT * FROM restore_points WHERE id=?', (new_id,)).fetchone())
        push_manifest = rep.read_manifest(push['manifest'])
        if not identity.get('uuid'):
            # the identity the replica will carry: from the (converted) image for cross-platform
            # copies; same-platform copies get theirs from the created VM below
            tv = push_manifest.get('vm') or {}
            if tv.get('converted_from'):
                identity = {'uuid': tv.get('uuid', ''), 'macs': [n.get('mac', '') for n in tv.get('nics') or []]}
        with ops.open_api(target_cr) as api, ops.connection_slot(task, target_cr['id'], 'replication'):
            if target_platform == 'proxmox':
                res = _sync_proxmox(task, api, target_cr, opts, vm_row, replica, what, push, push_manifest, name, identity)
            else:
                res = _sync_vmware(task, api, target_cr, opts, vm_row, replica, what, push, push_manifest, name, identity)
        now = time.time()
        with db.conn() as c:
            c.execute("UPDATE replicas SET state='synced', moref=?, name=?, synced_rp_id=?, source_rp_id=?, synced_at=?, disks_json=?, identity_json=?, last_error=NULL WHERE id=?",
                      (res['moref'], res['name'], push['id'], rp['id'], now, json.dumps(res.get('disks') or []), json.dumps(res.get('identity') or identity), replica['id']))
            c.execute("UPDATE runs SET finished=?, status='done', kind=?, transport=?, bytes_read=?, restore_point_id=? WHERE id=?",
                      (now, 'replicate', f'{res["mode"]}/{res["transport"]}', res.get('bytes_written', 0), rp['id'], run_id))
        task.log(f'{vm_row["name"]}: replica {res["name"]} on {target_cr["name"]} is at restore point {rp["id"]} ({res["mode"]}, {res.get("bytes_written", 0) / 2**20:.0f} MiB written)')
        tasks.create('inventory', subject=target_cr['name'], created_by='replication', connection_id=target_cr['id'])
        return {'vm': vm_row['name'], 'status': 'done', 'replica': res['name'], 'restore_point': rp['id'], 'mode': res['mode'], 'bytes_written': res.get('bytes_written', 0)}
    except BaseException as e:
        err = 'cancelled' if isinstance(e, tasks.Cancelled) else f'{type(e).__name__}: {e}'
        with db.conn() as c:
            c.execute("UPDATE replicas SET state=CASE WHEN moref IS NULL THEN 'new' ELSE 'failed' END, last_error=? WHERE id=?", (err[:1000], replica['id']))
            c.execute('UPDATE runs SET finished=?, status=?, error=? WHERE id=?', (time.time(), 'cancelled' if err == 'cancelled' else 'failed', err[:1000], run_id))
        raise


# ── VMware target ─────────────────────────────────────────────────────
def _restore_options(opts, name, annotation):
    return {'name': name, 'power_on': False, 'new_identity': opts['new_identity'], 'host': opts.get('host'), 'datastore': opts.get('datastore'),
            'folder': opts.get('folder'), 'networks': opts.get('networks') or {}, 'install_tools': opts['install_tools'], 'annotation': annotation,
            'description': annotation}


def _sync_vmware(task, api, cr, opts, vm_row, replica, what, push, push_manifest, name, identity):
    from ..providers import vmware
    from . import ops
    rep = repo()
    cs = push_manifest['chunk_size']
    annotation = f'Replica of {vm_row["name"]} maintained by hvbackup replication; do not power on except to fail over'
    proxy = ops.pick_proxy(cr['id'])
    vm = None
    if replica.get('moref'):
        try:
            vm = api.vm_by_moref(replica['moref'])
            vm.name  # noqa: B018
        except Exception:  # noqa: BLE001
            task.log(f'replica VM {replica["moref"]} is gone from {cr["name"]}; making it again')
            vm = None
    if vm is not None and vm.runtime.powerState != 'poweredOff':
        raise RuntimeError(f'replica {vm.name} is powered on; power it off (or fail over / forget the replica) before syncing')
    if vm is not None and what == 'incremental':
        res = _update_vmware(task, api, cr, vm, replica, push, push_manifest, cs, proxy, rep)
        if res is not None:
            res['identity'] = identity or {'uuid': vm.config.uuid, 'macs': vmware.nic_macs(vm)}
            return res
        what = 'rebuild'
    mode = 'full'
    if vm is not None:
        task.log(f'removing replica VM {vm.name} before the full copy')
        vmware.destroy_vm(vm)
        vm = None
        mode = 'rebuild'
    res = vmware.restore_vm(task, api, push, _restore_options(opts, name, annotation), proxy)
    new_vm = api.vm_by_moref(res['moref'])
    ident = identity or {'uuid': new_vm.config.uuid, 'macs': vmware.nic_macs(new_vm)}
    return {'moref': res['moref'], 'name': res['name'], 'disks': res['disks'], 'mode': mode,
            'transport': 'hotadd' if proxy else 'https', 'bytes_written': sum(d['allocated'] for d in push_manifest['disks']), 'identity': ident}


def _update_vmware(task, api, cr, vm, replica, push, push_manifest, cs, proxy, rep):
    """Carry an existing replica from the restore point it holds to `push`. Returns the result
    or None when the replica has to be rebuilt instead (disk set changed, no previous index)."""
    from ..providers import vmware
    from .. import db as _db
    with _db.conn() as c:
        prev = _db.row_to_dict(c.execute('SELECT * FROM restore_points WHERE id=?', (replica.get('synced_rp_id'),)).fetchone())
    if not prev or not prev.get('manifest'):
        task.log('the restore point the replica was built from is gone from the catalog; rebuilding')
        return None
    try:
        prev_manifest = rep.read_manifest(prev['manifest'])
    except OSError:
        task.log('the index the replica was built from is gone from the repository; rebuilding')
        return None
    prev_disks = {d['key']: d for d in prev_manifest['disks']}
    known = {d['key']: d for d in (replica.get('disks') or [])}
    cur = {d['file']: d for d in vmware.vm_disks(vm)}
    if [d['key'] for d in push_manifest['disks']] != [d['key'] for d in prev_manifest['disks']] or set(known) != set(prev_disks):
        task.log('the source VM\'s disk set changed since the replica was made; rebuilding')
        return None
    for d in push_manifest['disks']:
        f = known[d['key']]['file']
        if f not in cur:
            task.log(f'replica disk {f} is missing; rebuilding')
            return None
        if d['capacity'] < cur[f]['capacity']:
            task.log(f'disk {d["key"]} shrank at the source ({cur[f]["capacity"]} -> {d["capacity"]} bytes); rebuilding')
            return None
    if proxy is not None and not agent_version_ok(proxy.get('version')):
        task.log(f'proxy {proxy["name"]} runs agent {proxy.get("version")}, too old to clear freed blocks (needs 0.2.0); upgrade it. Rebuilding the replica instead')
        return None
    host = vm.runtime.host
    total = 0
    plans = []
    for d in push_manifest['disks']:
        new_hashes = rep.read_index(d['index'])
        old_hashes = rep.read_index(prev_disks[d['key']]['index'])
        recs = changed_records(old_hashes, new_hashes)
        plans.append((d, known[d['key']]['file'], new_hashes, recs))
        total += len(recs) * cs
    task.log(f'replica {vm.name}: {sum(len(r) for *_, r in plans)} chunk(s) changed since restore point {prev["id"]} ({total / 2**20:.0f} MiB)'
             + ('' if proxy else '; no proxy on this connection, so every disk is re-uploaded whole over HTTPS'))
    written = 0
    for d, f, new_hashes, recs in plans:
        task.check_cancel()
        disk = cur[f]
        if d['capacity'] > disk['capacity']:
            task.log(f'disk {d["key"]}: grown at the source, extending {f} to {d["capacity"] / 2**30:.1f} GiB')
            vmware.extend_disk(api, vm, disk, d['capacity'])
        if not recs:
            continue

        def prog(b, _base=written, _tot=max(total, 1)):
            task.progress((_base + b) / _tot, f'disk {d["key"]}: {b / 2**20:.0f} MiB written')
        if proxy is not None:
            vmware._write_via_proxy(task, api, proxy, f, d['capacity'], recs, cs, prog, write_zeros=True)
        else:
            vmware._write_via_https(task, api, api.http_host(host), f, d['capacity'], new_hashes, cs, lambda b: prog(b * len(recs) * cs / max(d['capacity'], 1)))
        written += len(recs) * cs
    return {'moref': vm._moId, 'name': vm.name, 'disks': replica.get('disks') or [], 'mode': 'incremental',
            'transport': 'hotadd' if proxy else 'https', 'bytes_written': written}


# ── Proxmox target ────────────────────────────────────────────────────
def _sync_proxmox(task, api, cr, opts, vm_row, replica, what, push, push_manifest, name, identity):
    from ..providers import proxmox
    annotation = f'Replica of {vm_row["name"]} maintained by hvbackup replication; do not start except to fail over'
    options = _restore_options(opts, name, annotation)
    loc = proxmox.locate(api, replica['moref']) if replica.get('moref') else None
    recorded = (identity or {}).get('pve_macs') or {}
    if loc:
        vmtype, vmid, node, status = loc
        if status == 'running':
            raise RuntimeError(f'replica {name} ({vmtype} {vmid}) is running; stop it (or fail over / forget the replica) before syncing')
        if not recorded:
            recorded = proxmox.guest_macs(api, node, vmtype, vmid)
        options.update({'vmid': vmid, 'force': True, 'host': opts.get('host') or node})
        task.log(f'replica {name} ({vmtype} {vmid}) is restored again over itself: Proxmox replicas are rebuilt each sync (only allocated blocks are read)')
        mode = 'rebuild'
    else:
        if replica.get('moref'):
            task.log(f'replica guest {replica["moref"]} is gone from {cr["name"]}; making it again')
        mode = 'full'
        if not options.get('host'):
            # a restore refuses to guess among several nodes; an unattended sync must not stop
            # on that, so take the online node with the most free image storage and say so
            options['host'] = _pick_pve_node(api, task, push_manifest)
    res = proxmox.restore_vm(task, api, cr, push, options)
    vmtype, vmid = res['moref'].split('/')
    if recorded:
        changed = proxmox.set_guest_macs(api, res['node'], vmtype, int(vmid), recorded)
        if changed:
            task.log('kept the replica\'s MAC addresses: ' + ', '.join(f'{k}={v}' for k, v in recorded.items()))
    else:
        recorded = proxmox.guest_macs(api, res['node'], vmtype, int(vmid))
    ident = dict(identity or {})
    ident['pve_macs'] = recorded
    return {'moref': res['moref'], 'name': res['name'], 'disks': [], 'mode': mode, 'transport': 'pve',
            'bytes_written': sum(d['allocated'] for d in push_manifest['disks']), 'identity': ident}


def _pick_pve_node(api, task, push_manifest):
    nodes = [n for n in api.nodes() if n['online']]
    if not nodes:
        raise RuntimeError('no node of the cluster is online')
    src = ((push_manifest.get('vm') or {}).get('placement') or {}).get('host')
    if src in [n['name'] for n in nodes]:
        return src
    best, best_free = None, -1
    for n in nodes:
        try:
            free = max((s.get('avail') or 0) for s in api.get(f'/nodes/{n["name"]}/storage', content='images') or [] if s.get('active', 1) and s.get('enabled', 1))
        except (RuntimeError, ValueError):
            free = 0
        if free > best_free:
            best, best_free = n['name'], free
    task.log(f'no node chosen for the replica; using {best} ({best_free / 2**30:.0f} GiB free on its largest image storage). Set a node in the replication to pin it')
    return best


# ── failover and housekeeping ─────────────────────────────────────────
def _replica(replica_id):
    with db.conn() as c:
        r = db.row_to_dict(c.execute('SELECT r.*, v.name AS vm_name FROM replicas r JOIN vms v ON v.id=r.vm_id WHERE r.id=?', (replica_id,)).fetchone())
    if not r:
        raise RuntimeError('replica not found')
    if not r.get('moref'):
        raise RuntimeError('this replica has no VM yet; run its replication first')
    return r


def failover(replica_id, test=False, user=None):
    """Power the replica on. test=True disconnects its NICs first, so it can be checked without
    touching the production network; end_test() reverts that. Syncing stops either way."""
    from ..providers import proxmox, vmware
    from . import ops
    r = _replica(replica_id)
    cr = ops.connection(r['target_connection_id'])
    with ops.open_api(cr) as api:
        if cr['type'] == 'proxmox':
            loc = proxmox.locate(api, r['moref'])
            if not loc:
                raise RuntimeError(f'replica guest {r["moref"]} is gone from {cr["name"]}')
            vmtype, vmid, node, _ = loc
            proxmox.set_links_down(api, node, vmtype, vmid, bool(test))
            proxmox.start_guest(api, node, vmtype, vmid)
        else:
            vm = api.vm_by_moref(r['moref'])
            vmware.set_nics_connected(vm, not test)
            vmware.power_on(vm)
    with db.conn() as c:
        c.execute("UPDATE replicas SET state=?, failed_over_at=? WHERE id=?", ('test' if test else 'failed_over', time.time(), replica_id))
    db.audit(user or 'api', 'replica_test' if test else 'replica_failover', f'{r["vm_name"]} -> {r["name"]} on {cr["name"]}')
    return {'ok': True, 'state': 'test' if test else 'failed_over', 'name': r['name']}


def end_test(replica_id, user=None):
    """Power the test copy off, reconnect its NICs, and mark it for a rebuild (the guest wrote
    to its disks, so an incremental could not be trusted)."""
    from ..providers import proxmox, vmware
    from . import ops
    r = _replica(replica_id)
    cr = ops.connection(r['target_connection_id'])
    with ops.open_api(cr) as api:
        if cr['type'] == 'proxmox':
            loc = proxmox.locate(api, r['moref'])
            if loc:
                vmtype, vmid, node, status = loc
                if status == 'running':
                    proxmox.stop_guest(api, node, vmtype, vmid)
                proxmox.set_links_down(api, node, vmtype, vmid, False)
        else:
            vm = api.vm_by_moref(r['moref'])
            vmware.power_off(vm)
            vmware.set_nics_connected(vm, True)
    with db.conn() as c:
        c.execute("UPDATE replicas SET state='dirty', failed_over_at=NULL WHERE id=?", (replica_id,))
    db.audit(user or 'api', 'replica_end_test', f'{r["name"]} on {cr["name"]}')
    return {'ok': True, 'state': 'dirty', 'note': 'the next sync rebuilds the replica'}


def forget(replica_id, delete_vm=False, user=None):
    """Drop the replica record; optionally destroy the VM on the target too."""
    from ..providers import proxmox, vmware
    from . import ops
    with db.conn() as c:
        r = db.row_to_dict(c.execute('SELECT * FROM replicas WHERE id=?', (replica_id,)).fetchone())
    if not r:
        raise RuntimeError('replica not found')
    removed = False
    if delete_vm and r.get('moref'):
        cr = ops.connection(r['target_connection_id'])
        with ops.open_api(cr) as api:
            if cr['type'] == 'proxmox':
                loc = proxmox.locate(api, r['moref'])
                if loc:
                    vmtype, vmid, node, _ = loc
                    proxmox.destroy_guest(api, node, vmtype, vmid)
                    removed = True
            else:
                try:
                    vm = api.vm_by_moref(r['moref'])
                    vm.name  # noqa: B018
                except Exception:  # noqa: BLE001
                    vm = None
                if vm is not None:
                    vmware.destroy_vm(vm)
                    removed = True
    with db.conn() as c:
        c.execute('DELETE FROM replicas WHERE id=?', (replica_id,))
    db.audit(user or 'api', 'replica_forget', f'{r["name"]}' + (' (VM destroyed)' if removed else ''))
    return {'ok': True, 'vm_destroyed': removed}
