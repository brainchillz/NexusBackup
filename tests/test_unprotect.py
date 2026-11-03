"""Unprotect: a VM leaves every job and replication, empty ones are deleted, replicas are
forgotten, restore points stay unless asked; the protection summary and the dashboard count
follow job membership rather than leftover restore points."""
import json
import time

import pytest

from hvbackup import db
from hvbackup.core import ops


@pytest.fixture
def catalog():
    db.init()
    with db.conn() as c:
        for t in ('replicas', 'replications', 'proxy_links', 'restore_points', 'runs', 'jobs', 'vms', 'proxies', 'connections'):
            c.execute(f'DELETE FROM {t}')
        now = time.time()
        for cid, name, typ in ((1, 'vc-a', 'vcenter'), (2, 'vc-b', 'vcenter')):
            c.execute("INSERT INTO connections(id,type,name,host,username,secret_enc,created,info_json) VALUES(?,?,?,?,?,?,?,?)",
                      (cid, typ, name, f'{name}.example', 'u', 'x', now, '{}'))
        for vid, moref, name in ((1, 'vm-1', 'web01'), (2, 'vm-2', 'db01'), (3, 'vm-3', 'app01')):
            c.execute("INSERT INTO vms(id,connection_id,moref,name,uuid,updated,gone) VALUES(?,1,?,?,?,?,0)", (vid, moref, name, f'uuid-{vid}', now))
        c.execute("INSERT INTO jobs(id,name,connection_id,vm_ids_json,retention_json,created) VALUES(1,'nightly',1,'[1,2]','{}',?)", (now,))
        c.execute("INSERT INTO jobs(id,name,connection_id,vm_ids_json,retention_json,created) VALUES(2,'web-only',1,'[1]','{}',?)", (now,))
        c.execute("INSERT INTO replications(id,name,connection_id,target_connection_id,vm_ids_json,options_json,created) VALUES(1,'dr',1,2,'[1]','{}',?)", (now,))
        c.execute("INSERT INTO replicas(id,replication_id,vm_id,target_connection_id,name,moref,state,created) VALUES(1,1,1,2,'web01-replica',NULL,'new',?)", (now,))
        for i in range(3):
            c.execute("INSERT INTO restore_points(vm_id,run_id,repo_id,created,manifest,size_bytes,allocated_bytes,kind,spec_json) VALUES(1,NULL,'r',?,?,10,4,'full','{}')", (now + i, f'm/{i}.json'))
        c.execute("INSERT INTO restore_points(vm_id,run_id,repo_id,created,manifest,size_bytes,allocated_bytes,kind,spec_json) VALUES(3,NULL,'r',?,'m/x.json',10,4,'full','{}')", (now,))
    return db


def test_protection_of_follows_membership(catalog):
    with db.conn() as c:
        p = ops.protection_of(c, 1)
        assert [j['name'] for j in p['jobs']] == ['nightly', 'web-only'] and p['jobs'][0]['others'] == 1 and p['jobs'][1]['others'] == 0
        assert [r['name'] for r in p['replications']] == ['dr'] and p['replications'][0]['target_connection_name'] == 'vc-b'
        assert [r['name'] for r in p['replicas']] == ['web01-replica']
        assert p['restore_points'] == 3 and p['restore_points_bytes'] == 12 and p['protected']
        # points without a job: history, not protection
        p3 = ops.protection_of(c, 3)
        assert p3['restore_points'] == 1 and not p3['protected'] and not p3['jobs']


def test_unprotect_keeps_points_and_prunes_empty_jobs(catalog):
    res = ops.unprotect_vm(1, user='t')
    assert res['jobs_left'] == ['nightly'] and res['jobs_deleted'] == ['web-only']
    assert res['replications_left'] == [] and res['replications_deleted'] == ['dr']
    assert res['replicas_forgotten'] == 1 and res['replica_vms_destroyed'] == 0 and res['restore_points_deleted'] == 0
    with db.conn() as c:
        assert json.loads(c.execute('SELECT vm_ids_json FROM jobs WHERE id=1').fetchone()[0]) == [2]
        assert c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM replications').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM replicas').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM restore_points WHERE vm_id=1 AND deleted=0').fetchone()[0] == 3
        p = ops.protection_of(c, 1)
        assert not p['protected'] and p['restore_points'] == 3
        assert c.execute("SELECT COUNT(*) FROM audit WHERE action='vm_unprotect'").fetchone()[0] == 1


def test_unprotect_can_delete_points(catalog):
    res = ops.unprotect_vm(1, delete_restore_points=True, user='t')
    assert res['restore_points_deleted'] == 3
    with db.conn() as c:
        assert c.execute('SELECT COUNT(*) FROM restore_points WHERE vm_id=1 AND deleted=0').fetchone()[0] == 0
    # a VM nothing protects: a no-op that still answers
    res = ops.unprotect_vm(2, user='t')
    assert res['jobs_left'] == [] and res['jobs_deleted'] == ['nightly']
    with pytest.raises(RuntimeError):
        ops.unprotect_vm(99)
