"""Replication and multi-connection pieces that need no hypervisor: chunk diffs, sync planning,
VM identity adoption at inventory time, proxy links, scheduler tables, plug-in vCenter lookup."""
import json
import time

import pytest

from hvbackup import db
from hvbackup.core import ops, replication, scheduler, tasks
from hvbackup.store.chunks import ZERO_HASH

A, B, C = b'\x01' * 32, b'\x02' * 32, b'\x03' * 32


def test_changed_records_diff_grow_and_zero():
    assert replication.changed_records([A, B, C], [A, B, C]) == []
    assert replication.changed_records([A, B, C], [A, C, C]) == [(1, C)]
    # a chunk that became zero is written as zeros; a grown disk's new non-zero tail is written
    assert replication.changed_records([A, B], [A, ZERO_HASH, ZERO_HASH, C]) == [(1, ZERO_HASH), (3, C)]
    assert replication.changed_records([], [ZERO_HASH, A]) == [(1, A)]


def test_plan_states():
    assert replication.plan(None, 5) == 'full'
    assert replication.plan({'state': 'synced', 'moref': 'vm-1', 'source_rp_id': 5}, 5) == 'skip:up to date'
    assert replication.plan({'state': 'synced', 'moref': 'vm-1', 'source_rp_id': 4}, 5) == 'incremental'
    assert replication.plan({'state': 'failed', 'moref': 'vm-1', 'source_rp_id': 4}, 5) == 'incremental'
    assert replication.plan({'state': 'dirty', 'moref': 'vm-1', 'source_rp_id': 5}, 5) == 'rebuild'
    assert replication.plan({'state': 'new', 'moref': None, 'source_rp_id': None}, 5) == 'rebuild'
    assert replication.plan({'state': 'failed_over', 'moref': 'vm-1', 'source_rp_id': 4}, 5) == 'skip:failed over'
    assert replication.plan({'state': 'test', 'moref': 'vm-1', 'source_rp_id': 4}, 5).startswith('skip:')


def test_agent_version_gate_and_names():
    assert replication.agent_version_ok('0.2.0') and replication.agent_version_ok('1.0')
    assert not replication.agent_version_ok('0.1.0') and not replication.agent_version_ok(None) and not replication.agent_version_ok('x')
    assert replication.replica_name('web01', {'suffix': '-dr'}) == 'web01-dr'
    assert replication.replica_name('web01', {}) == 'web01-replica'
    assert replication.options_of({'options': {'host': 'h1', 'networks': None}})['host'] == 'h1'


def test_conversion_key_includes_identity():
    from hvbackup.core import convert
    assert convert.conversion_key({}) == {'new_identity': True, 'install_tools': False}
    k = convert.conversion_key({'new_identity': True, 'identity': {'uuid': 'u', 'macs': ['aa']}})
    assert k['identity'] == {'uuid': 'u', 'macs': ['aa']}


# ── catalog-backed pieces ───────────────────────────────────────────
@pytest.fixture
def catalog():
    db.init()
    with db.conn() as c:
        for t in ('replicas', 'replications', 'proxy_links', 'restore_points', 'runs', 'jobs', 'vms', 'proxies', 'connections'):
            c.execute(f'DELETE FROM {t}')
        now = time.time()
        for cid, name, typ in ((1, 'vc-a', 'vcenter'), (2, 'vc-b', 'vcenter'), (3, 'pve', 'proxmox')):
            c.execute("INSERT INTO connections(id,type,name,host,username,secret_enc,created,info_json) VALUES(?,?,?,?,?,?,?,?)",
                      (cid, typ, name, f'{name}.example', 'u', 'x', now, json.dumps({'instance_uuid': f'guid-{cid}'})))
    return db


def _inv(moref, name, uuid):
    return {'moref': moref, 'name': name, 'uuid': uuid, 'power_state': 'poweredOff', 'guest': 'g', 'tools': '', 'cbt_enabled': True,
            'is_template': False, 'host': 'h', 'folder': '', 'disks': [], 'spec': {}}


def _upsert(c, cid, vms, task):
    moved = ops.adopt_moved_vms(c, task, cid, vms)
    for v in vms:
        c.execute('''INSERT INTO vms(connection_id,moref,name,uuid,power_state,guest,tools,cbt_enabled,is_template,host,folder,disks_json,spec_json,updated,gone)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0) ON CONFLICT(connection_id,moref) DO UPDATE SET name=excluded.name, gone=0''',
                  (cid, v['moref'], v['name'], v['uuid'], 'poweredOff', 'g', '', 1, 0, 'h', '', '[]', '{}', time.time()))
    return moved


def test_vm_moved_between_vcenters_keeps_history(catalog):
    task = tasks.NullTask()
    u = '420c9518-bdb1-7aa2-a9b6-c6ceff60f964'
    with db.conn() as c:
        _upsert(c, 1, [_inv('vm-10', 'web01', u)], task)
        vm_id = c.execute('SELECT id FROM vms WHERE moref=?', ('vm-10',)).fetchone()[0]
        c.execute("INSERT INTO restore_points(vm_id,run_id,repo_id,created,manifest,size_bytes,allocated_bytes,kind,spec_json) VALUES(?,?,?,?,?,?,?,?,?)",
                  (vm_id, None, 'r', time.time(), 'm/1.json', 10, 5, 'full', '{}'))
        c.execute("INSERT INTO cbt_state(vm_id,disk_key,change_id,restore_point_id) VALUES(?,?,?,?)", (vm_id, '2000', '52 1', 1))
        c.execute("INSERT INTO jobs(name,connection_id,vm_ids_json,retention_json,created) VALUES('j-a',1,?,'{}',?)", (json.dumps([vm_id]), time.time()))
    # the VM leaves vc-a: its row is marked gone
    with db.conn() as c:
        c.execute('UPDATE vms SET gone=1 WHERE id=?', (vm_id,))
    # vc-b's refresh sees it under a new moref: the old row follows, with its points and CBT state
    with db.conn() as c:
        moved = _upsert(c, 2, [_inv('vm-77', 'web01', u.upper())], task)
        assert moved and moved[0]['vm_id'] == vm_id and moved[0]['from_connection_id'] == 1
        row = c.execute('SELECT connection_id, moref, gone FROM vms WHERE id=?', (vm_id,)).fetchone()
        assert (row['connection_id'], row['moref'], row['gone']) == (2, 'vm-77', 0)
        assert c.execute('SELECT COUNT(*) FROM vms').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM restore_points WHERE vm_id=?', (vm_id,)).fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM cbt_state WHERE vm_id=?', (vm_id,)).fetchone()[0] == 1
        assert c.execute("SELECT action FROM audit ORDER BY id DESC LIMIT 1").fetchone()[0] == 'vm_moved'


def test_vm_seen_on_two_live_connections_is_not_merged(catalog):
    task = tasks.NullTask()
    u = '420c9518-bdb1-7aa2-a9b6-c6ceff60f964'
    with db.conn() as c:
        _upsert(c, 1, [_inv('vm-10', 'web01', u)], task)
        a = c.execute('SELECT id FROM vms WHERE moref=?', ('vm-10',)).fetchone()[0]
        c.execute("INSERT INTO restore_points(vm_id,run_id,repo_id,created,manifest,size_bytes,allocated_bytes,kind,spec_json) VALUES(?,?,?,?,?,?,?,?,?)",
                  (a, None, 'r', time.time(), 'm/1.json', 10, 5, 'full', '{}'))
        # the same VM through a direct host connection: both rows live
        assert _upsert(c, 2, [_inv('5', 'web01', u)], task) == []
        assert c.execute('SELECT COUNT(*) FROM vms').fetchone()[0] == 2


def test_fresh_row_with_own_points_is_kept(catalog):
    task = tasks.NullTask()
    u = '11111111-2222-3333-4444-555555555555'
    with db.conn() as c:
        _upsert(c, 1, [_inv('vm-1', 'x', u)], task)
        old = c.execute('SELECT id FROM vms WHERE moref=?', ('vm-1',)).fetchone()[0]
        c.execute("INSERT INTO restore_points(vm_id,run_id,repo_id,created,manifest,size_bytes,allocated_bytes,kind,spec_json) VALUES(?,?,?,?,?,?,?,?,?)", (old, None, 'r', 1, 'm', 1, 1, 'full', '{}'))
        c.execute('UPDATE vms SET gone=1 WHERE id=?', (old,))
        _upsert(c, 2, [_inv('vm-9', 'x', u)], task)
        assert c.execute('SELECT COUNT(*) FROM vms').fetchone()[0] == 1  # merged
        new = c.execute('SELECT id FROM vms').fetchone()[0]
        assert new == old
        # now the merged row is live on 2; a gone twin with points elsewhere must not steal it back
        c.execute("INSERT INTO vms(connection_id,moref,name,uuid,updated,gone) VALUES(1,'vm-2','x',?,?,1)", (u, time.time()))
        twin = c.execute("SELECT id FROM vms WHERE moref='vm-2'").fetchone()[0]
        c.execute("INSERT INTO restore_points(vm_id,run_id,repo_id,created,manifest,size_bytes,allocated_bytes,kind,spec_json) VALUES(?,?,?,?,?,?,?,?,?)", (twin, None, 'r', 2, 'm2', 1, 1, 'full', '{}'))
        assert _upsert(c, 2, [_inv('vm-9', 'x', u)], task) == []
        assert c.execute('SELECT connection_id FROM vms WHERE id=?', (old,)).fetchone()[0] == 2


def test_proxy_links_make_a_proxy_usable_from_another_connection(catalog):
    from hvbackup.core import auth
    pid, _ = auth.create_proxy('px', 1)
    with db.conn() as c:
        c.execute('UPDATE proxies SET last_seen=?, vm_moref=? WHERE id=?', (time.time(), 'vm-5', pid))
        c.execute("INSERT INTO proxy_links(proxy_id,connection_id,vm_moref,seen) VALUES(?,?,?,?)", (pid, 2, '12', time.time()))
    own = ops.online_proxies(1)
    assert len(own) == 1 and not own[0].get('via_link')
    linked = ops.online_proxies(2)
    assert len(linked) == 1 and linked[0]['via_link'] and linked[0]['link_moref'] == '12'
    assert ops.online_proxies(3) == []
    assert ops.pick_proxy(2)['id'] == pid


def test_scheduler_sees_replications(catalog):
    with db.conn() as c:
        c.execute("INSERT INTO replications(name,connection_id,target_connection_id,vm_ids_json,schedule,options_json,created,next_run) VALUES('r1',1,2,'[1]','* * * * *','{}',?,?)",
                  (time.time(), time.time() - 1))
        c.execute("INSERT INTO jobs(name,connection_id,vm_ids_json,retention_json,created,schedule) VALUES('j1',1,'[1]','{}',?,'* * * * *')", (time.time(),))
    scheduler.refresh_next_runs()
    with db.conn() as c:
        assert c.execute("SELECT next_run FROM jobs WHERE name='j1'").fetchone()[0] is not None
    started = []
    scheduler._tick(lambda jid, created_by: started.append(('job', jid)), lambda rid, created_by: started.append(('repl', rid)))
    assert ('repl', 1) in started and not any(k == 'job' for k, _ in started)  # the job's first run is in the future
    with db.conn() as c:
        assert c.execute("SELECT next_run FROM replications WHERE name='r1'").fetchone()[0] > time.time()


def test_after_backup_trigger_only_for_members(catalog, monkeypatch):
    with db.conn() as c:
        c.execute("INSERT INTO replications(id,name,connection_id,target_connection_id,vm_ids_json,after_backup,options_json,created) VALUES(7,'r',1,2,'[3,4]',1,'{}',?)", (time.time(),))
        c.execute("INSERT INTO replications(id,name,connection_id,target_connection_id,vm_ids_json,after_backup,options_json,created,schedule) VALUES(8,'sched-only',1,2,'[3]',0,'{}',?, '0 * * * *')", (time.time(),))
    calls = []
    monkeypatch.setattr(replication, 'run', lambda rid, created_by=None, vm_ids=None: calls.append((rid, vm_ids)) or 99)
    assert replication.on_new_restore_point(3, 1) == [('r', 99)]
    assert replication.on_new_restore_point(5, 1) == []
    assert calls == [(7, [3])]


def test_plugin_connection_lookup_by_guid(catalog):
    from hvbackup.api import vsphere_plugin_routes as vpr
    assert vpr._connection_for_guid('GUID-2')['name'] == 'vc-b'
    assert vpr._connection_for_guid('guid-9') is None
    assert vpr._connection_for_guid(None) is None  # two vCenters: the exchange must try each
    assert [c['name'] for c in vpr._vsphere_connections()] == ['vc-a', 'vc-b']
    with db.conn() as c:
        c.execute('DELETE FROM connections WHERE id=2')
    assert vpr._connection_for_guid(None)['name'] == 'vc-a'
