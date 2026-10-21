"""Unit tests for the pure-Python pieces: chunk arithmetic, chunk store, auth, uuids."""
import os
import tempfile

import pytest

os.environ.setdefault('HVB_DATA_DIR', tempfile.mkdtemp(prefix='hvb-test-data-'))  # conftest sets these first
os.environ.setdefault('HVB_REPO', tempfile.mkdtemp(prefix='hvb-test-repo-'))

from hvbackup.core import auth, uuids  # noqa: E402
from hvbackup.providers import base  # noqa: E402
from hvbackup.store.chunks import ZERO_HASH, Repository, hash_bytes  # noqa: E402

CS = 4 * 1024 * 1024


def test_extents_to_chunk_ranges_merges_and_rounds():
    cap = 10 * CS
    assert base.extents_to_chunk_ranges([(0, 1)], CS, cap) == [[0, 1]]
    assert base.extents_to_chunk_ranges([(CS - 1, 2)], CS, cap) == [[0, 2]]
    assert base.extents_to_chunk_ranges([(0, CS), (CS, CS)], CS, cap) == [[0, 2]]
    assert base.extents_to_chunk_ranges([(5 * CS, CS), (0, CS)], CS, cap) == [[0, 1], [5, 1]]
    # an extent past the end is clipped to the last chunk
    assert base.extents_to_chunk_ranges([(9 * CS + 10, 5 * CS)], CS, cap) == [[9, 1]]
    assert base.ranges_chunk_count([[0, 2], [5, 1]]) == 3


def test_records_roundtrip():
    pairs = [(0, b'\x01' * 32), (7, ZERO_HASH), (2 ** 40, b'\xff' * 32)]
    assert base.unpack_records(base.pack_records(pairs)) == pairs
    assert base.unpack_records(b'') == []


def test_chunk_store_roundtrip_and_zero_elision():
    rep = Repository(tempfile.mkdtemp(prefix='hvb-repo-')).open()
    data = os.urandom(1024) + bytes(CS - 1024)
    h, stored, new = rep.put_raw(data)
    assert new and stored > 0 and h == hash_bytes(data)
    assert rep.has(h) and rep.get(h) == data
    h2, stored2, new2 = rep.put_raw(data)
    assert h2 == h and stored2 == 0 and not new2
    hz, sz, nz = rep.put_raw(bytes(CS))
    assert hz == ZERO_HASH and sz == 0 and rep.has(ZERO_HASH) and rep.get(ZERO_HASH) == bytes(CS)
    with pytest.raises(ValueError):
        rep.put_compressed(rep.get_compressed(h), b'\x00' * 32)
    assert rep.missing([h, b'\x01' * 32]) == [b'\x01' * 32]


def test_manifest_index_and_gc():
    rep = Repository(tempfile.mkdtemp(prefix='hvb-repo-')).open()
    keep = rep.put_raw(os.urandom(CS))[0]
    drop = rep.put_raw(os.urandom(CS))[0]
    rel = rep.write_index('vm-uuid', 1, '2000', [keep, ZERO_HASH])
    assert rep.read_index(rel) == [keep, ZERO_HASH]
    rep.write_manifest('vm-uuid', 1, {'disks': [{'key': '2000', 'index': rel}]})
    res = rep.gc(grace_seconds=0)
    assert res['removed'] == 1 and rep.has(keep) and not rep.has(drop)


def test_password_and_roles():
    h = auth.hash_password('s3cret')
    assert auth.check_password('s3cret', h) and not auth.check_password('nope', h)
    assert auth.role_at_least('admin', 'viewer') and not auth.role_at_least('viewer', 'restore')


def test_vmware_uuid_byte_swap():
    assert uuids.same_vm('420c9518-bdb1-7aa2-a9b6-c6ceff60f964', '18950c42-b1bd-a27a-a9b6-c6ceff60f964')
    assert uuids.same_vm('420c9518-bdb1-7aa2-a9b6-c6ceff60f964', '420C9518-BDB1-7AA2-A9B6-C6CEFF60F964')
    assert not uuids.same_vm('420c9518-bdb1-7aa2-a9b6-c6ceff60f964', '420c9518-bdb1-7aa2-a9b6-c6ceff60f965')
    assert not uuids.same_vm('garbage', '420c9518-bdb1-7aa2-a9b6-c6ceff60f964')


def test_users_and_tokens_roundtrip():
    from hvbackup import db
    db.init()
    uid = auth.create_user('tester', 'pw', 'operator')
    assert auth.authenticate_password('tester', 'pw')['id'] == uid
    t = auth.create_token(uid, 'cli')
    u = auth.authenticate_token(t)
    assert u['username'] == 'tester' and u['via'] == 'token'
    assert auth.authenticate_token('hvb_bogus') is None
    pid, pt = auth.create_proxy('p1')
    assert auth.authenticate_proxy(pt)['id'] == pid
    s = auth.make_session('tester')
    assert auth.read_session(s)['username'] == 'tester'
    assert auth.read_session(s + 'x') is None
