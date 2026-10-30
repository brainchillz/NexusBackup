"""Notifications: the webhook fires for switched-on events, carries a signature, dedups,
and the test endpoint reports failures."""
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

os.environ.setdefault('HVB_DATA_DIR', tempfile.mkdtemp(prefix='hvb-test-data-'))  # conftest sets these first
os.environ.setdefault('HVB_REPO', tempfile.mkdtemp(prefix='hvb-test-repo-'))

from hvbackup import db  # noqa: E402
from hvbackup.core import notify, settings, tasks  # noqa: E402


class Hook(BaseHTTPRequestHandler):
    got = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        Hook.got.append((dict(self.headers), body))
        self.send_response(Hook.status)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture(scope='module')
def hook():
    srv = HTTPServer(('127.0.0.1', 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{srv.server_port}/hook'
    srv.shutdown()


def _wait(n, secs=5):
    t0 = time.time()
    while len(Hook.got) < n and time.time() - t0 < secs:
        time.sleep(0.05)
    return len(Hook.got) >= n


def test_send_respects_switches_signs_and_dedups(hook):
    db.init()
    notify._last.clear()
    Hook.got.clear()
    assert notify.send('backup_failed', 'x') is False  # webhook off
    settings.update({'webhook_url': hook, 'webhook_secret': 's3cret', 'notify_backup_failed': True})
    assert notify.send('verify_failed', 'Restore point 1 of web01 is damaged') is False  # event switched off
    assert notify.send('backup_failed', 'Backup of web01 failed', 'boom', key='web01', vm='web01') is True
    assert notify.send('backup_failed', 'Backup of web01 failed', 'boom again', key='web01') is False  # within the dedup window
    assert _wait(1)
    headers, body = Hook.got[0]
    doc = json.loads(body)
    assert doc['event'] == 'backup_failed' and doc['vm'] == 'web01' and doc['text'] == 'Backup of web01 failed: boom' and doc['content'] == doc['text']
    assert headers['X-Hvbackup-Signature'] == 'sha256=' + hmac.new(b's3cret', body, hashlib.sha256).hexdigest()
    assert headers['Content-Type'] == 'application/json'
    with pytest.raises(ValueError):
        notify.send('nope', 'x')
    # the masked secret never leaves the API
    row = next(s for s in settings.describe() if s['key'] == 'webhook_secret')
    assert row['value'] == '••••••••'


def test_failed_task_notifies(hook):
    notify._last.clear()
    Hook.got.clear()
    settings.update({'notify_task_failed': True})
    tasks.register('boom', lambda t: (_ for _ in ()).throw(RuntimeError('kaput')))
    tid = tasks.create('boom', subject='unit test')
    assert _wait(1)
    doc = json.loads(Hook.got[0][1])
    assert doc['event'] == 'task_failed' and doc['task_id'] == tid and 'kaput' in doc['detail'] and 'unit test' in doc['title']


def test_test_endpoint_reports_answer(hook):
    Hook.got.clear()
    assert notify.test(hook, 'k')['status'] == 200
    Hook.status = 500
    try:
        with pytest.raises(ValueError):
            notify.test(hook)
    finally:
        Hook.status = 200
    with pytest.raises(ValueError):
        notify.test('http://127.0.0.1:9/nothing')
    settings.update({'webhook_url': None, 'webhook_secret': None, 'notify_backup_failed': None, 'notify_task_failed': None})
