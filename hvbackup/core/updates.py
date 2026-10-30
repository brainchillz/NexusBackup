"""Update notice: once a day ask GitHub for the newest release of the public repository
and remember it in meta, so the UI can say when this server is behind. Off with the
update_check setting. Nothing but the request itself leaves the server."""
import json
import logging
import re
import time

import requests

from .. import APP_VERSION, db

log = logging.getLogger('hvbackup.updates')
RELEASES = 'https://api.github.com/repos/brainchillz/NexusBackup/releases/latest'
INTERVAL = 24 * 3600


def _tuple(v):
    return tuple(int(x) for x in re.findall(r'\d+', str(v))[:3]) or (0,)


def check(force=False):
    from . import settings
    if not settings.get('update_check') and not force:
        return None
    with db.conn() as c:
        r = c.execute("SELECT value FROM meta WHERE key='update_check'").fetchone()
    state = json.loads(r['value']) if r else {}
    if not force and time.time() - state.get('checked', 0) < INTERVAL:
        return state
    try:
        resp = requests.get(RELEASES, timeout=15, headers={'Accept': 'application/vnd.github+json', 'User-Agent': f'hvbackup/{APP_VERSION}'})
        resp.raise_for_status()
        rel = resp.json()
        state = {'checked': time.time(), 'latest': str(rel.get('tag_name', '')).lstrip('v'), 'url': rel.get('html_url'), 'error': None}
    except (requests.RequestException, ValueError) as e:
        state = {**state, 'checked': time.time(), 'error': str(e)[:200]}
        log.info('update check failed: %s', e)
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('update_check',?)", (json.dumps(state),))
    return state


def status():
    """What the UI shows: latest known version and whether it is newer than this one."""
    with db.conn() as c:
        r = c.execute("SELECT value FROM meta WHERE key='update_check'").fetchone()
    state = json.loads(r['value']) if r else {}
    latest = state.get('latest')
    return {'version': APP_VERSION, 'latest': latest, 'url': state.get('url'), 'checked': state.get('checked'),
            'available': bool(latest) and _tuple(latest) > _tuple(APP_VERSION), 'error': state.get('error')}
