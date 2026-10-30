"""Certificate manager: info, regenerate, import with key and chain checks, revert, pins,
and the API behind Settings > Certificate."""
import datetime
import os
import tempfile

import pytest

os.environ.setdefault('HVB_DATA_DIR', tempfile.mkdtemp(prefix='hvb-test-data-'))  # conftest sets these first
os.environ.setdefault('HVB_REPO', tempfile.mkdtemp(prefix='hvb-test-repo-'))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hvbackup import config, db  # noqa: E402
from hvbackup.api.app import create_app  # noqa: E402
from hvbackup.core import auth, tls  # noqa: E402


def _pem(obj):
    if hasattr(obj, 'public_bytes'):
        return obj.public_bytes(serialization.Encoding.PEM).decode()
    return obj.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def _chain(cn='backup.example.net', days=400):
    """A CA, an intermediate and a leaf for cn with a SAN."""
    now = datetime.datetime.now(datetime.timezone.utc)

    def cert(subject, issuer, key, signer, ca, san=None):
        b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
             .not_valid_after(now + datetime.timedelta(days=days)).add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if san:
            b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
        return b.sign(signer, hashes.SHA256())
    root_k, int_k, leaf_k = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root_n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Test Root')])
    int_n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Test Intermediate')])
    leaf_n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    root = cert(root_n, root_n, root_k, root_k, True)
    inter = cert(int_n, root_n, int_k, root_k, True)
    leaf = cert(leaf_n, int_n, leaf_k, int_k, False, san=cn)
    return root, inter, leaf, leaf_k


@pytest.fixture(scope='module')
def client():
    db.init()
    with db.conn() as c:
        c.execute('DELETE FROM users')
    auth.create_user('certadmin', 'longenough', 'admin')
    with TestClient(create_app(), base_url='https://testserver') as c:
        c.post('/api/v1/auth/login', json={'username': 'certadmin', 'password': 'longenough'})
        yield c


def test_generated_certificate_info_and_regenerate(client):
    tls.ensure_cert()
    i = tls.info()
    assert i['self_signed'] and i['origin'] == 'generated' and 'localhost' in i['names'] and len(i['fingerprint']) == 95
    assert not i['restart_needed'] and not i['previous_available']
    tls.mark_running()
    old_fp = i['fingerprint']
    r = client.post('/api/v1/certificate/regenerate', json={'names': ['backup.example.net', '10.0.0.5'], 'days': 30})
    assert r.status_code == 200
    i = r.json()
    assert i['names'] == ['backup.example.net', '10.0.0.5'] and i['common_name'] == 'backup.example.net' and 29 <= i['days_left'] <= 30
    assert i['restart_needed'] and i['running_fingerprint'] == old_fp and i['previous_available']
    assert oct(os.stat(tls.paths()[1]).st_mode & 0o777) == '0o600'
    assert client.post('/api/v1/certificate/regenerate', json={'names': ['bad name'], 'days': 30}).status_code == 400
    assert client.post('/api/v1/certificate/regenerate', json={'names': ['x'], 'days': 0}).status_code == 400
    st = client.get('/api/v1/status').json()['certificate']
    assert st['restart_needed'] and st['self_signed']


def test_import_checks_key_chain_and_expiry(client):
    root, inter, leaf, leaf_k = _chain()
    other_k = ec.generate_private_key(ec.SECP256R1())
    r = client.post('/api/v1/certificate/import', json={'cert': _pem(leaf), 'key': _pem(other_k)})
    assert r.status_code == 400 and 'does not match' in r.json()['detail']
    r = client.post('/api/v1/certificate/import', json={'cert': _pem(leaf), 'key': _pem(leaf_k), 'chain': _pem(root) + _pem(inter)})
    assert r.status_code == 400 and 'chain order' in r.json()['detail']
    assert client.post('/api/v1/certificate/import', json={'cert': 'junk', 'key': _pem(leaf_k)}).status_code == 400
    assert client.post('/api/v1/certificate/import', json={'cert': _pem(leaf), 'key': 'junk'}).status_code == 400
    _, _, expired, expired_k = _chain(days=-1)
    r = client.post('/api/v1/certificate/import', json={'cert': _pem(expired), 'key': _pem(expired_k)})
    assert r.status_code == 400 and 'expired' in r.json()['detail']
    r = client.post('/api/v1/certificate/import', json={'cert': _pem(leaf) + _pem(inter), 'key': _pem(leaf_k), 'chain': _pem(root)})
    assert r.status_code == 200
    i = r.json()
    assert not i['self_signed'] and i['origin'] == 'imported' and i['chain'] == ['CN=Test Intermediate', 'CN=Test Root'] and i['names'] == ['backup.example.net']
    assert i['restart_needed'] and i['previous_available']
    pem = client.get('/api/v1/certificate/download').content
    assert pem.count(b'BEGIN CERTIFICATE') == 3
    # the proxy deploy flow verifies once the certificate is not self-signed
    assert tls.is_self_signed() is False
    # audit rows name the actions
    actions = {a['action'] for a in client.get('/api/v1/audit').json()}
    assert {'certificate_regenerated', 'certificate_imported'} <= actions


def test_revert_swaps_pairs_both_ways(client):
    before = tls.info()
    r = client.post('/api/v1/certificate/revert')
    assert r.status_code == 200 and r.json()['self_signed'] and r.json()['origin'] == 'generated' and r.json()['fingerprint'] != before['fingerprint']
    r = client.post('/api/v1/certificate/revert')
    assert r.status_code == 200 and r.json()['fingerprint'] == before['fingerprint'] and r.json()['origin'] == 'imported'


def test_pins_list_connections_and_proxies(client):
    import json
    import time
    from hvbackup.core import secrets
    with db.conn() as c:
        c.execute("INSERT INTO connections(type,name,host,username,secret_enc,created,info_json) VALUES('proxmox','cert-pve','h','u',?,?,?)",
                  (secrets.encrypt('x'), time.time(), json.dumps({'pve_storage': {'storage': 'hvbackup'}})))
        c.execute("INSERT INTO connections(type,name,host,username,secret_enc,created) VALUES('vcenter','cert-vc','h','u',?,?)", (secrets.encrypt('x'), time.time()))
    auth.create_proxy('cert-proxy')
    pins = client.get('/api/v1/certificate').json()['pins']
    kinds = [p['kind'] for p in pins]
    assert 'pve_storage' in kinds and 'vsphere_plugin' in kinds and kinds[-2:] == ['proxies', 'dashboard']
    assert pins[0]['action'] == 'pve-storage' and pins[0]['storage'] == 'hvbackup' and pins[2]['count'] >= 1


def test_managed_outside_is_read_only(client, monkeypatch):
    crt, key = tls.paths()
    monkeypatch.setattr(config, 'TLS_CERT', crt)
    monkeypatch.setattr(config, 'TLS_KEY', key)
    i = client.get('/api/v1/certificate').json()
    assert i['managed_outside'] and i['origin'] == 'environment' and not i['previous_available']
    assert client.post('/api/v1/certificate/regenerate', json={}).status_code == 400
    assert client.post('/api/v1/certificate/revert').status_code == 400
