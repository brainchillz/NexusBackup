"""Verification: read every chunk a restore point references and check its BLAKE3 hash
against the chunk's name, so damage on the repository disk (bad sectors, a filesystem
that lost files, a mistaken rm) shows up on a schedule instead of at restore time.

A run marks each point verified ok or damaged (restore_points.verified / verify_status /
verify_error). Chunks are shared between points, so a chunk read once in a run is not
read again for the next point that uses it. The weekly job (Settings > Verification)
takes every point not verified within verify_max_age_days, oldest verification first.

Test boot (one point at a time, optional): restore the point as <vm>-verify-<id> with its
NICs disconnected, power it on, wait for the guest tools (vSphere) or the guest agent
(Proxmox) to report, then power off and delete it. That proves the backup boots, not
only that the bytes match."""
import logging
import time

from .. import db
from ..store.chunks import ZERO_HASH, hash_bytes, repo

log = logging.getLogger('hvbackup.verify')
MAX_ERRORS_LISTED = 10
BOOT_TIMEOUT = 15 * 60


def _select(c, rp_ids=None, max_age_days=None):
    if rp_ids:
        q = f'SELECT rp.*, v.name AS vm_name, v.connection_id FROM restore_points rp JOIN vms v ON v.id=rp.vm_id WHERE rp.deleted=0 AND rp.id IN ({",".join("?" * len(rp_ids))}) ORDER BY rp.id'
        return [db.row_to_dict(r) for r in c.execute(q, list(rp_ids))]
    q = 'SELECT rp.*, v.name AS vm_name, v.connection_id FROM restore_points rp JOIN vms v ON v.id=rp.vm_id WHERE rp.deleted=0'
    args = []
    if max_age_days is not None:
        q += ' AND (rp.verified IS NULL OR rp.verified < ?)'
        args.append(time.time() - max_age_days * 86400)
    q += ' ORDER BY rp.verified IS NOT NULL, rp.verified, rp.id'
    return [db.row_to_dict(r) for r in c.execute(q, args)]


def check_point(rep, rp, seen_ok, task=None, progress=None):
    """Read and hash every chunk of one point. Returns (ok, errors, chunks_read)."""
    errors, read, bad = [], 0, 0
    try:
        manifest = rep.read_manifest(rp['manifest'])
    except (OSError, ValueError) as e:
        return False, [f'manifest unreadable: {e}'], 0
    for disk in manifest.get('disks', []):
        key = disk.get('key') or disk.get('label') or '?'
        try:
            hashes = rep.read_index(disk['index'])
        except OSError as e:
            errors.append(f'disk {key}: index unreadable: {e}')
            bad += 1
            continue
        for i, h in enumerate(hashes):
            if h == ZERO_HASH or h in seen_ok:
                continue
            try:
                data = rep.get(h)
                if hash_bytes(data) != h:
                    raise ValueError('content does not match its hash')
                seen_ok.add(h)
            except FileNotFoundError:
                bad += 1
                if len(errors) < MAX_ERRORS_LISTED:
                    errors.append(f'disk {key} chunk {i}: {h.hex()[:16]} missing')
            except Exception as e:  # noqa: BLE001  (zstd errors, short files, hash mismatch)
                bad += 1
                if len(errors) < MAX_ERRORS_LISTED:
                    errors.append(f'disk {key} chunk {i}: {h.hex()[:16]} {e}')
            read += 1
            if progress and read % 64 == 0:
                progress(read)
            if task and read % 256 == 0:
                task.check_cancel()
    if bad > len(errors):
        errors.append(f'... {bad} bad chunks in all')
    return bad == 0, errors, read


def _record(rp_id, ok, errors, note=None):
    status = 'ok' if ok else 'damaged'
    err = '; '.join(errors) if errors else None
    if note and ok:
        err = note
    with db.conn() as c:
        c.execute('UPDATE restore_points SET verified=?, verify_status=?, verify_error=? WHERE id=?', (time.time(), status, err, rp_id))
        vm_name = (c.execute('SELECT v.name FROM restore_points rp JOIN vms v ON v.id=rp.vm_id WHERE rp.id=?', (rp_id,)).fetchone() or ['?'])[0]
    if not ok:
        from . import notify
        notify.send('verify_failed', f'Restore point {rp_id} of {vm_name} is damaged', (err or '')[:500], key=str(rp_id), vm=vm_name, restore_point_id=rp_id)


def verify_task(task, rp_ids=None, max_age_days=None, test_boot=False, connection_id=None):
    rep = repo()
    with db.conn() as c:
        points = _select(c, rp_ids, max_age_days)
    if not points:
        task.log('nothing to verify' + (f' (every point was verified within {max_age_days} days)' if max_age_days else ''))
        return {'points': 0, 'ok': 0, 'damaged': 0, 'chunks': 0}
    total = 0
    uniq = set()
    for rp in points:
        try:
            for d in rep.read_manifest(rp['manifest']).get('disks', []):
                uniq.update(h for h in rep.read_index(d['index']) if h != ZERO_HASH)
        except (OSError, ValueError):
            pass
    total = len(uniq) or 1
    task.log(f'verifying {len(points)} restore point(s), {total} distinct chunks ({total * rep.chunk_size / 2**30:.1f} GiB to read)')
    seen_ok = set()
    done = [0]
    counts = {'points': len(points), 'ok': 0, 'damaged': 0, 'chunks': 0}
    t0 = time.time()
    for n, rp in enumerate(points, 1):
        task.check_cancel()
        base = done[0]
        ok, errors, read = check_point(rep, rp, seen_ok, task, lambda r: task.progress((base + r) / total, f'{rp["vm_name"]} point {rp["id"]} ({n}/{len(points)})'))
        done[0] += read
        counts['chunks'] += read
        counts['ok' if ok else 'damaged'] += 1
        _record(rp['id'], ok, errors)
        if ok:
            task.log(f'point {rp["id"]} of {rp["vm_name"]}: ok ({read} chunks read)')
        else:
            task.log(f'point {rp["id"]} of {rp["vm_name"]}: DAMAGED: ' + '; '.join(errors))
            log.error('restore point %s of %s is damaged: %s', rp['id'], rp['vm_name'], '; '.join(errors))
    secs = max(time.time() - t0, 0.001)
    task.log(f'{counts["ok"]} ok, {counts["damaged"]} damaged, {counts["chunks"]} chunks read in {secs:.0f} s ({counts["chunks"] * rep.chunk_size / 2**20 / secs:.0f} MiB/s)')
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_verify',?)", (str(time.time()),))
    if test_boot:
        if len(points) != 1:
            raise RuntimeError('a test boot verifies one restore point at a time')
        if counts['damaged']:
            raise RuntimeError('the point is damaged; no test boot')
        counts['boot'] = test_boot_point(task, points[0], connection_id)
    return counts


def test_boot_point(task, rp, connection_id=None):
    """Restore the point isolated, boot it, wait for the guest to report, remove it."""
    from . import ops
    from ..providers import proxmox, vmware
    cid = connection_id or rp['connection_id']
    cr = ops.connection(cid)
    name = f'{rp["vm_name"]}-verify-{rp["id"]}'[:60]
    task.log(f'test boot: restoring point {rp["id"]} as {name} on {cr["name"]} with the NICs disconnected')
    res = ops.restore(task, rp['id'], {'name': name, 'connection_id': cid, 'power_on': False, 'new_identity': True})
    t0 = time.time()
    outcome = None
    try:
        with ops.open_api(cr) as api:
            if cr['type'] == 'proxmox':
                vmtype, vmid = res['moref'].split('/')
                node = res['node']
                proxmox.set_links_down(api, node, vmtype, vmid, True)
                proxmox.start_guest(api, node, vmtype, vmid, task)
                task.log('powered on; waiting for the guest agent')
                while time.time() - t0 < BOOT_TIMEOUT:
                    task.check_cancel()
                    try:
                        if vmtype == 'lxc':
                            st = api.get(f'/nodes/{node}/lxc/{vmid}/status/current') or {}
                            if st.get('status') == 'running' and time.time() - t0 > 30:
                                outcome = f'container running after {time.time() - t0:.0f} s'
                                break
                        else:
                            api.post(f'/nodes/{node}/qemu/{vmid}/agent/ping')
                            outcome = f'guest agent answered after {time.time() - t0:.0f} s'
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(5)
            else:
                vm = api.vm_by_moref(res['moref'])
                vmware.set_nics_connected(vm, False)
                vmware.power_on(vm)
                task.log('powered on; waiting for VMware Tools')
                while time.time() - t0 < BOOT_TIMEOUT:
                    task.check_cancel()
                    g = vmware.guest_state(vm)
                    if g['tools'] == 'guestToolsRunning':
                        outcome = f'VMware Tools running after {time.time() - t0:.0f} s'
                        break
                    if g['power'] != 'poweredOn':
                        raise RuntimeError('the test VM powered itself off')
                    time.sleep(5)
    finally:
        try:
            task.log(f'test boot: removing {name}')
            with ops.open_api(cr) as api:
                if cr['type'] == 'proxmox':
                    vmtype, vmid = res['moref'].split('/')
                    proxmox.destroy_guest(api, res['node'], vmtype, vmid, task)
                else:
                    vmware.destroy_vm(api.vm_by_moref(res['moref']))
            from . import tasks
            tasks.create('inventory', subject=cr['name'], created_by='verify', connection_id=cr['id'])
        except Exception as e:  # noqa: BLE001
            task.log(f'WARNING: could not remove the test VM {name}: {e}')
    if not outcome:
        _record(rp['id'], False, [f'test boot: the guest did not report within {BOOT_TIMEOUT // 60} minutes'])
        raise RuntimeError(f'test boot of point {rp["id"]} failed: the guest did not report within {BOOT_TIMEOUT // 60} minutes')
    _record(rp['id'], True, [], note=f'test boot ok: {outcome}')
    task.log(f'test boot: {outcome}')
    return outcome


def maybe_schedule():
    """Scheduler hook: start the weekly run when its cron time has come. The next time is
    kept in meta with the expression it was computed from, so a changed schedule applies."""
    from . import scheduler, settings, tasks
    if not settings.get('verify_enabled'):
        return
    cron = settings.get('verify_schedule')
    if not cron:
        return
    with db.conn() as c:
        nxt = c.execute("SELECT value FROM meta WHERE key='verify_next_run'").fetchone()
        used = c.execute("SELECT value FROM meta WHERE key='verify_schedule_used'").fetchone()
        running = c.execute("SELECT COUNT(*) FROM tasks WHERE kind='verify' AND state IN ('queued','running')").fetchone()[0]
    now = time.time()
    if not nxt or not used or used['value'] != cron:
        with db.conn() as c:
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('verify_next_run',?)", (str(scheduler.next_run(cron, now)),))
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('verify_schedule_used',?)", (cron,))
        return
    if now >= float(nxt['value']):
        with db.conn() as c:
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('verify_next_run',?)", (str(scheduler.next_run(cron, now)),))
        if not running:
            tasks.create('verify', subject='scheduled', created_by='scheduler', max_age_days=settings.get('verify_max_age_days'))


def summary():
    with db.conn() as c:
        damaged = c.execute("SELECT COUNT(*) FROM restore_points WHERE deleted=0 AND verify_status='damaged'").fetchone()[0]
        never = c.execute("SELECT COUNT(*) FROM restore_points WHERE deleted=0 AND verified IS NULL").fetchone()[0]
        last = c.execute("SELECT value FROM meta WHERE key='last_verify'").fetchone()
        nxt = c.execute("SELECT value FROM meta WHERE key='verify_next_run'").fetchone()
    return {'damaged_points': damaged, 'unverified_points': never, 'last_verify': float(last['value']) if last else None,
            'next_verify': float(nxt['value']) if nxt else None}
