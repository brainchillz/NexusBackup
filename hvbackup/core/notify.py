"""Notifications: one webhook (Settings > Notifications) that receives a JSON document for
the events ticked there: a failed backup, a damaged restore point, a failed replication
sync, a failed task. A URL plus a tick is all it takes; there is no separate switch. The body carries `text` and `content`, so a Slack,
Mattermost, Discord or Teams incoming webhook shows it without a template; everything
else gets the structured fields. With a secret set, X-Hvbackup-Signature carries
sha256=HMAC(secret, body). Delivery runs in a thread with retries and never blocks the
task that raised the event; failures go to the server log only."""
import hashlib
import hmac
import json
import logging
import socket
import threading
import time

import requests

from .. import APP_VERSION

log = logging.getLogger('hvbackup.notify')
EVENTS = {
    'backup_failed': 'notify_backup_failed',
    'verify_failed': 'notify_verify_failed',
    'replication_failed': 'notify_replication_failed',
    'task_failed': 'notify_task_failed',
}
RETRIES = 3
_last = {}  # (event, key) -> time, to send one notice per failure, not one per retry
DEDUP_SECONDS = 300


def _post(url, body, secret):
    data = json.dumps(body, default=str).encode()
    headers = {'Content-Type': 'application/json', 'User-Agent': f'hvbackup/{APP_VERSION}'}
    if secret:
        headers['X-Hvbackup-Signature'] = 'sha256=' + hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()
    last = None
    for attempt in range(RETRIES):
        try:
            r = requests.post(url, data=data, headers=headers, timeout=15)
            if r.status_code < 300:
                return True
            last = f'HTTP {r.status_code}: {r.text[:200]}'
            if 400 <= r.status_code < 500 and r.status_code != 429:
                break
        except requests.RequestException as e:
            last = str(e)[:200]
        time.sleep(2 * (attempt + 1))
    log.warning('webhook %s: %s (%s not delivered)', url, last, body.get('event'))
    return False


def payload(event, title, text, **fields):
    from . import settings
    return {'event': event, 'title': title, 'text': f'{title}: {text}' if text else title, 'content': f'{title}: {text}' if text else title,
            'detail': text, 'server': socket.gethostname(), 'url': settings.public_url() or None, 'version': APP_VERSION,
            'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), **fields}


def send(event, title, text='', key=None, **fields):
    """Queue a notification if the webhook is on and the event's switch is on. Returns
    whether it was queued."""
    from . import settings
    if event not in EVENTS:
        raise ValueError(f'unknown event {event}')
    if not settings.get('webhook_url') or not settings.get(EVENTS[event]):
        return False
    k = (event, key or title)
    now = time.time()
    if now - _last.get(k, 0) < DEDUP_SECONDS:
        return False
    _last[k] = now
    body = payload(event, title, text, **fields)
    threading.Thread(target=_post, args=(settings.get('webhook_url'), body, settings.get('webhook_secret')), name='notify', daemon=True).start()
    return True


def test(url=None, secret=None):
    """Deliver a test document now, synchronously; raises with the reason on failure."""
    from . import settings
    url = url or settings.get('webhook_url')
    if not url:
        raise ValueError('no webhook URL')
    body = payload('test', 'hvbackup test notification', 'the webhook in Settings > Notifications works')
    data = json.dumps(body).encode()
    headers = {'Content-Type': 'application/json', 'User-Agent': f'hvbackup/{APP_VERSION}'}
    secret = secret if secret is not None else settings.get('webhook_secret')
    if secret:
        headers['X-Hvbackup-Signature'] = 'sha256=' + hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()
    try:
        r = requests.post(url, data=data, headers=headers, timeout=15)
    except requests.RequestException as e:
        raise ValueError(f'cannot reach the webhook: {e}')
    if r.status_code >= 300:
        raise ValueError(f'the webhook answered HTTP {r.status_code}: {r.text[:200]}')
    return {'status': r.status_code}
