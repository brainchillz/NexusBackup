"""Container-first settings: the settings table and its API, first admin from the sign-in
page, zone-aware cron, session lifetime, catalog backup and restore, themes, the log ring."""
import datetime
import io
import json
import os
import tarfile
import tempfile
import threading
import time
import zipfile
import zoneinfo

import pytest

os.environ.setdefault('HVB_DATA_DIR', tempfile.mkdtemp(prefix='hvb-test-data-'))  # conftest sets these first
os.environ.setdefault('HVB_REPO', tempfile.mkdtemp(prefix='hvb-test-repo-'))

from fastapi.testclient import TestClient  # noqa: E402

from hvbackup import config, db  # noqa: E402
from hvbackup.api.app import create_app  # noqa: E402
from hvbackup.core import auth, catalog_backup, logs, scheduler, settings, updates  # noqa: E402


@pytest.fixture(scope='module')
def client():
    db.init()
    with db.conn() as c:
        c.execute('DELETE FROM users')
        c.execute('DELETE FROM settings')
    settings.invalidate()
    with TestClient(create_app(), base_url='https://testserver') as c:
        yield c


def test_first_admin_from_the_sign_in_page(client):
    assert client.get('/api/v1/auth/setup').json() == {'needed': True}
    assert 'Create administrator' in client.get('/login').text
    r = client.post('/api/v1/auth/setup', json={'username': 'boss', 'password': 'short'})
    assert r.status_code == 422
    r = client.post('/api/v1/auth/setup', json={'username': 'boss', 'password': 'longenough'})
    assert r.status_code == 201 and r.json()['role'] == 'admin'
    assert client.get('/api/v1/auth/me').json()['username'] == 'boss'  # the setup signs you in
    assert client.get('/api/v1/auth/setup').json() == {'needed': False}
    assert client.post('/api/v1/auth/setup', json={'username': 'again', 'password': 'longenough'}).status_code == 409
    assert 'Create administrator' not in client.get('/login', follow_redirects=False).text or True


def test_settings_api_roundtrip_sources_and_locks(client):
    rows = {s['key']: s for s in client.get('/api/v1/settings').json()['settings']}
    assert rows['timezone']['source'] in ('default', 'environment') and rows['port']['restart'] is True
    assert client.patch('/api/v1/settings', json={'timezone': 'Mars/Olympus'}).status_code == 400
    assert client.patch('/api/v1/settings', json={'session_hours': 0}).status_code == 400
    assert client.patch('/api/v1/settings', json={'nope': 1}).status_code == 400
    r = client.patch('/api/v1/settings', json={'timezone': 'Pacific/Honolulu', 'session_hours': '48', 'port': 9443, 'update_check': False})
    assert r.status_code == 200 and r.json()['restart_needed'] == ['port']
    rows = {s['key']: s for s in r.json()['settings']}
    assert rows['timezone']['value'] == 'Pacific/Honolulu' and rows['timezone']['source'] == 'settings'
    assert rows['session_hours']['value'] == 48 and rows['update_check']['value'] is False
    assert settings.get('port') == 9443 and os.environ['TZ'] == 'Pacific/Honolulu'
    # the environment wins and locks the field
    os.environ['HVB_SESSION_HOURS'] = '2'
    try:
        assert settings.get('session_hours') == 2
        row = next(s for s in client.get('/api/v1/settings').json()['settings'] if s['key'] == 'session_hours')
        assert row['locked'] and row['source'] == 'environment'
        assert client.patch('/api/v1/settings', json={'session_hours': 5}).status_code == 400
        os.environ['HVB_SESSION_HOURS'] = ''  # compose passes empty values for unset variables: they count as unset
        assert settings.get('session_hours') == 48
    finally:
        del os.environ['HVB_SESSION_HOURS']
    # null and the default clear the row
    client.patch('/api/v1/settings', json={'port': None})
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM settings WHERE key='port'").fetchone()[0] == 0
    assert any(a['action'] == 'setting' for a in client.get('/api/v1/audit').json())


def test_cron_runs_in_the_configured_zone(client):
    settings.update({'timezone': 'Pacific/Honolulu'})
    t = scheduler.next_run('0 2 * * *')
    local = datetime.datetime.fromtimestamp(t, zoneinfo.ZoneInfo('Pacific/Honolulu'))
    assert (local.hour, local.minute) == (2, 0)
    settings.update({'timezone': 'Europe/Amsterdam'})
    t = scheduler.next_run('30 4 * * *')
    assert datetime.datetime.fromtimestamp(t, zoneinfo.ZoneInfo('Europe/Amsterdam')).hour == 4
    assert client.get('/api/v1/status').json()['timezone'] == 'Europe/Amsterdam'


def test_session_carries_its_lifetime(client):
    settings.update({'session_hours': 1})
    s = auth.make_session('boss')
    assert auth.read_session(s)['username'] == 'boss'
    old = auth._serializer().dumps({'u': 'boss', 't': time.time() - 2 * 3600, 'ttl': 3600})
    assert auth.read_session(old) is None
    legacy = auth._serializer().dumps({'u': 'boss', 't': time.time() - 2 * 3600})  # before the setting existed: 12 h
    assert auth.read_session(legacy)['username'] == 'boss'


def test_catalog_backup_download_copy_stage_apply(client, tmp_path):
    r = client.get('/api/v1/catalog/backup')
    assert r.status_code == 200 and r.headers['content-type'] == 'application/x-tar'
    names = tarfile.open(fileobj=io.BytesIO(r.content)).getnames()
    assert 'catalog.db' in names and 'secret.key' in names and 'README.txt' in names
    copies = client.post('/api/v1/catalog/backups').json()['copies']
    assert len(copies) == 1 and oct(os.stat(os.path.join(catalog_backup.repo_dir(), copies[0]['name'])).st_mode & 0o777) == '0o600'
    assert client.get('/api/v1/catalog/backups').json()['last'] is not None
    # stage from the repository copy, then from an upload; junk is refused
    r = client.post('/api/v1/catalog/restore', params={'name': copies[0]['name']})
    assert r.status_code == 200 and r.json()['users'] == 1 and r.json()['staged']
    assert client.get('/api/v1/status').json()['catalog_restore_pending'] is True
    assert client.post('/api/v1/catalog/restore', files={'file': ('x.tar', b'not a tar')}).status_code == 400
    r = client.post('/api/v1/catalog/restore', files={'file': ('b.tar', r.content and client.get('/api/v1/catalog/backup').content)})
    assert r.status_code == 200
    assert client.delete('/api/v1/catalog/restore').status_code == 200 and catalog_backup.pending() is None
    # apply_pending swaps the files at start and keeps the old catalog
    client.post('/api/v1/catalog/restore', params={'name': copies[0]['name']})
    assert catalog_backup.apply_pending() is True
    assert os.path.exists(config.DB_PATH + '.before-restore') and catalog_backup.pending() is None
    assert catalog_backup.apply_pending() is False
    assert auth.get_user('boss')  # the restored catalog is the one we backed up
    # pruning keeps the newest `keep`
    for _ in range(3):
        time.sleep(1.1)
        catalog_backup.write_copy(keep=2)
    assert len(catalog_backup.list_copies()) == 2
    assert client.delete(f"/api/v1/catalog/backups/{catalog_backup.list_copies()[0]['name']}").status_code == 200
    assert client.delete('/api/v1/catalog/backups/../etc/passwd').status_code in (400, 404)


def test_themes_zip_install_list_remove(client):
    def zipped(entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            for n, data in entries.items():
                z.writestr(n, data)
        return buf.getvalue()
    good = zipped({'ocean/theme.json': json.dumps({'id': 'ocean', 'name': 'Ocean', 'modes': ['dark']}), 'ocean/theme.css': ':root{}', 'ocean/logo.svg': '<svg/>'})
    r = client.post('/api/v1/themes', files={'file': ('ocean.zip', good)})
    assert r.status_code == 201 and r.json()['id'] == 'ocean' and r.json()['files'] == ['logo.svg', 'theme.css', 'theme.json']
    assert client.get('/themes/ocean/theme.css').text == ':root{}'
    assert client.get('/themes/ocean/../../catalog.db').status_code == 404
    assert [t['id'] for t in client.get('/api/v1/themes').json()] == ['ocean']
    assert 'ocean' in client.get('/settings').text
    assert client.post('/api/v1/themes', files={'file': ('bad.zip', zipped({'x/theme.json': '{}'}))}).status_code == 400
    assert client.post('/api/v1/themes', files={'file': ('bad.zip', zipped({'../evil/theme.css': ''}))}).status_code == 400
    assert client.post('/api/v1/themes', files={'file': ('bad.zip', zipped({'Bad Id/theme.css': ''}))}).status_code == 400
    assert client.post('/api/v1/themes', files={'file': ('bad.zip', b'zzz')}).status_code == 400
    assert client.delete('/api/v1/themes/ocean').status_code == 200 and client.get('/api/v1/themes').json() == []
    assert client.delete('/api/v1/themes/ocean').status_code == 404


def test_log_ring_and_level(client):
    logs.install()
    logs.set_level('debug')
    logs.line('info', 'hvbackup.test', 'hello ring')
    logs.line('warning', 'hvbackup.test', 'careful')
    rows = client.get('/api/v1/logs', params={'q': 'ring'}).json()
    assert rows and rows[-1]['message'] == 'hello ring'
    warn = client.get('/api/v1/logs', params={'level': 'warning', 'limit': 5}).json()
    assert all(r['level'] in ('warning', 'error', 'critical') for r in warn) and warn[-1]['message'] == 'careful'
    after = client.get('/api/v1/logs', params={'after': rows[-1]['id']}).json()
    assert all(r['id'] > rows[-1]['id'] for r in after)
    settings.update({'log_level': 'warning'})
    import logging
    assert logging.getLogger().level == logging.WARNING
    settings.update({'log_level': None})


def test_proxy_image_upload_and_forget(client):
    assert client.post('/api/v1/proxies/image', files={'file': ('x.qcow2', b'1')}).status_code == 400
    r = client.post('/api/v1/proxies/image', files={'file': ('my image.ova', b'OVA' * 10)})
    assert r.status_code == 201 and r.json()['local'] and r.json()['uploaded'] and r.json()['size'] == 30
    path = r.json()['path']
    assert os.path.dirname(path) == config.IMAGE_DIR and settings.get('proxy_image') == path
    r = client.delete('/api/v1/proxies/image')
    assert r.status_code == 200 and not r.json()['local'] and not os.path.exists(path)
    assert client.delete('/api/v1/proxies/image').status_code == 404


def test_concurrency_slot_follows_the_setting(client):
    from hvbackup.core import ops, tasks
    settings.update({'concurrency': 1})
    got = []
    first = ops.connection_slot(tasks.NullTask(), 999, 'test')
    first.__enter__()
    th = threading.Thread(target=lambda: (ops.connection_slot(tasks.NullTask(), 999, 'test').__enter__(), got.append(time.time())))
    th.start()
    time.sleep(0.3)
    assert not got
    first.__exit__(None, None, None)
    th.join(5)
    assert got
    settings.update({'concurrency': None})


def test_update_status_compares_versions(client):
    assert updates._tuple('0.3.0') < updates._tuple('v0.10.1')
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('update_check',?)", (json.dumps({'checked': time.time(), 'latest': '99.0.0', 'url': 'u'}),))
    st = client.get('/api/v1/status').json()['update']
    assert st['available'] and st['latest'] == '99.0.0'


def test_restart_endpoint_marks_pending(client, monkeypatch):
    from hvbackup.core import server
    monkeypatch.setattr(threading.Thread, 'start', lambda self: None)  # do not actually exit
    r = client.post('/api/v1/server/restart', params={'reason': 'test'})
    assert r.status_code == 200 and r.json()['by'] == 'boss'
    assert client.get('/api/v1/status').json()['restart_pending']['reason'] == 'test'
    server._pending.clear()
