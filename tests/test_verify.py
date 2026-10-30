"""Verification: chunk hashing against the repository, damage detection, the scheduled run,
the API and the status summary."""
import json
import os
import tempfile
import time

import pytest

os.environ.setdefault('HVB_DATA_DIR', tempfile.mkdtemp(prefix='hvb-test-data-'))  # conftest sets these first
os.environ.setdefault('HVB_REPO', tempfile.mkdtemp(prefix='hvb-test-repo-'))

from fastapi.testclient import TestClient  # noqa: E402

from hvbackup import db  # noqa: E402
from hvbackup.api.app import create_app  # noqa: E402
from hvbackup.core import auth, secrets, settings, tasks, verify  # noqa: E402
from hvbackup.store.chunks import repo  # noqa: E402

CS = 4 * 1024 * 1024


def _point(rep, vm_id, rp_id, chunks):
    hashes = [rep.put_raw(c)[0] for c in chunks]
    rel = rep.write_index(f'vm-{vm_id}', rp_id, '2000', hashes)
    man = rep.write_manifest(f'vm-{vm_id}', rp_id, {'platform': 'vmware', 'disks': [{'key': '2000', 'index': rel}]})
    with db.conn() as c:
        c.execute('INSERT INTO restore_points(id,vm_id,repo_id,created,manifest,size_bytes,allocated_bytes,kind,spec_json) VALUES(?,?,?,?,?,?,?,?,?)',
                  (rp_id, vm_id, rep.id, time.time(), man, CS * len(chunks), CS * len(chunks), 'full', '{}'))
    return hashes


@pytest.fixture(scope='module')
def env():
    db.init()
    rep = repo()
    with db.conn() as c:
        c.execute('DELETE FROM users')
        c.execute("INSERT INTO connections(id,type,name,host,username,secret_enc,created) VALUES(7001,'vcenter','verify-vc','h','u',?,?)", (secrets.encrypt('x'), time.time()))
        c.execute("INSERT INTO vms(id,connection_id,moref,name,updated) VALUES(7001,7001,'vm-7001','web01',?)", (time.time(),))
        c.execute('DELETE FROM restore_points')
    auth.create_user('op', 'longenough', 'operator')
    shared = os.urandom(CS)
    good = _point(rep, 7001, 1, [shared, os.urandom(CS), bytes(CS)])
    bad = _point(rep, 7001, 2, [shared, os.urandom(CS), os.urandom(CS)])
    # damage point 2: one chunk missing, one with wrong content
    os.unlink(rep.chunk_path(bad[1]))
    with open(rep.chunk_path(bad[2]), 'wb') as f:
        f.write(b'garbage')
    return {'rep': rep, 'good': good, 'bad': bad, 'shared': shared}


def test_check_point_reads_each_chunk_once_and_finds_damage(env):
    rep = env['rep']
    seen = set()
    with db.conn() as c:
        p1 = db.row_to_dict(c.execute('SELECT * FROM restore_points WHERE id=1').fetchone())
        p2 = db.row_to_dict(c.execute('SELECT * FROM restore_points WHERE id=2').fetchone())
    ok, errors, read = verify.check_point(rep, p1, seen)
    assert ok and not errors and read == 2  # the zero chunk is never read
    ok, errors, read = verify.check_point(rep, p2, seen)
    assert not ok and read == 2  # the shared chunk was seen already
    assert any('missing' in e for e in errors) and any('does not match' in e or 'zstd' in e.lower() or 'error' in e.lower() for e in errors)


def test_verify_task_records_status_and_summary(env):
    t = tasks.NullTask()
    res = verify.verify_task(t)
    assert res['points'] == 2 and res['ok'] == 1 and res['damaged'] == 1
    with db.conn() as c:
        rows = {r['id']: db.row_to_dict(r) for r in c.execute('SELECT * FROM restore_points')}
    assert rows[1]['verify_status'] == 'ok' and rows[1]['verified'] and rows[1]['verify_error'] is None
    assert rows[2]['verify_status'] == 'damaged' and 'missing' in rows[2]['verify_error']
    s = verify.summary()
    assert s['damaged_points'] == 1 and s['unverified_points'] == 0 and s['last_verify']
    # a run limited to points older than 7 days finds nothing to do
    assert verify.verify_task(t, max_age_days=7)['points'] == 0
    # test boot refuses a damaged point and more than one point
    with pytest.raises(RuntimeError):
        verify.verify_task(t, rp_ids=[2], test_boot=True)
    with pytest.raises(RuntimeError):
        verify.verify_task(t, rp_ids=[1, 2], test_boot=True)


def test_scheduled_run_follows_the_cron_setting(env, monkeypatch):
    started = []
    monkeypatch.setattr(tasks, 'create', lambda kind, **kw: started.append((kind, kw)) or 1)
    settings.update({'verify_schedule': '*/5 * * * *'})
    verify.maybe_schedule()  # first call computes the next time
    with db.conn() as c:
        nxt = float(c.execute("SELECT value FROM meta WHERE key='verify_next_run'").fetchone()['value'])
    assert nxt > time.time() and not started
    with db.conn() as c:
        c.execute("UPDATE meta SET value=? WHERE key='verify_next_run'", (str(time.time() - 1),))
    verify.maybe_schedule()
    assert started and started[0][0] == 'verify' and started[0][1]['max_age_days'] == 7
    settings.update({'verify_enabled': False})
    with db.conn() as c:
        c.execute("UPDATE meta SET value=? WHERE key='verify_next_run'", (str(time.time() - 1),))
    verify.maybe_schedule()
    assert len(started) == 1
    settings.update({'verify_enabled': None, 'verify_schedule': None})
    assert settings.get('verify_schedule') == '0 4 * * 0'
    with pytest.raises(ValueError):
        settings.update({'verify_schedule': 'every sunday'})


def test_api_and_status(env):
    with TestClient(create_app(), base_url='https://testserver') as c:
        c.post('/api/v1/auth/login', json={'username': 'op', 'password': 'longenough'})
        rows = {r['id']: r for r in c.get('/api/v1/restore-points').json()}
        assert rows[2]['verify_status'] == 'damaged' and rows[1]['verify_status'] == 'ok'
        st = c.get('/api/v1/status').json()
        assert st['damaged_points'] == 1 and st['last_verify']
        assert c.post('/api/v1/restore-points/99/verify', json={}).status_code == 404
        r = c.post('/api/v1/restore-points/1/verify', json={})
        assert r.status_code == 200
        for _ in range(50):
            t = c.get(f"/api/v1/tasks/{r.json()['task_id']}").json()
            if t['state'] in ('done', 'failed'):
                break
            time.sleep(0.1)
        assert t['state'] == 'done' and json.loads(t['result_json'] if isinstance(t.get('result_json'), str) else json.dumps(t.get('result') or {}))
        r = c.post('/api/v1/maintenance/verify', json={'max_age_days': 30})
        assert r.status_code == 200
        assert c.get('/api/v1/vms/7001').json()['restore_points'][0]['verify_status'] in ('ok', 'damaged')
