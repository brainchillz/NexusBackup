"""vm-backup — hvbackup (the hypervisor backup server) as a native Nexus Dashboard module.

Python tier of the hvbackup plug-in. Everything shown here comes from the hvbackup REST
API (https://<server>:8460/api/v1) through one bearer token; nothing is read from the
server's files and no command is run. The dashboard's own login, RBAC and audit sit in
front of every route: GET routes are open to signed-in users, every POST is admin-only
by the dashboard's central rule, and the token's hvbackup role bounds what the plug-in
can do on the server regardless (a viewer token makes the actions fail cleanly).

Configuration lives in ~/.config/hvbackup/client.conf of the dashboard's service user:
the same KEY=VALUE file the hvbackup CLI and the declarative tier read, written by the
Settings dialog (admin) or by hand. The plug-in directory itself stays root-owned, as
the loader wants, so state cannot go there. Keys: HVB_URL (default
https://127.0.0.1:8460), HVB_TOKEN, HVB_INSECURE (1 = do not verify the server's TLS
certificate, the default since hvbackup's certificate is self-signed unless
HVB_TLS_CERT was set), HVB_CA (a CA file; implies verification), and two the CLI
ignores: HVB_PUBLIC_URL (what browsers reach, for the "Open console" links; defaults
to HVB_URL) and HVB_OVERDUE_HOURS (default 36).

Contributions: two pages (VM Backup, Backup History), a front-page card, a block under
"vm-backup" in /api/summary, alerts (server unreachable, failed backups, overdue VMs,
proxies offline), history metrics and Prometheus lines. The hvbackup systemd unit is
listed in the Service Manager when this node runs the server.

Portability: standard library only; valid on Python 3.9 through 3.14.
"""
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Blueprint, jsonify, request

from nexusdash.core.auth import _is_admin
from nexusdash.core.runcmd import err

PLUGIN_ID = 'vm-backup'
CLIENT_CONF = os.path.expanduser('~/.config/hvbackup/client.conf')
DEFAULT_URL = 'https://127.0.0.1:8460'
TIMEOUT = 10
RE_ID = re.compile(r'^[0-9]{1,9}\Z')
RE_URL = re.compile(r'^https?://[A-Za-z0-9._:\[\]-]+(/[A-Za-z0-9._~/-]*)?\Z')
RE_TOKEN = re.compile(r'^[A-Za-z0-9._-]{8,256}\Z')
# How long a server answer is reused. /status walks the chunk store, so it gets the longest.
CACHE_TTL = {'/status': 30, '/vms': 10, '/jobs': 10, '/proxies': 10, '/connections': 10}

bp = Blueprint(PLUGIN_ID, __name__)


class HvbError(Exception):
    def __init__(self, message, code=502):
        super().__init__(message)
        self.code = code


# ── configuration ────────────────────────────────────────────────────
def _read_client_conf():
    out = {}
    try:
        with open(CLIENT_CONF) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _write_client_conf(values):
    """Rewrite the conf with the given keys replaced, other lines kept, atomically and 0600."""
    lines = []
    seen = set()
    try:
        with open(CLIENT_CONF) as f:
            for line in f:
                raw = line.rstrip('\n')
                k = raw.split('=', 1)[0].strip() if '=' in raw and not raw.strip().startswith('#') else None
                if k in values:
                    if values[k] is not None:
                        lines.append('%s=%s' % (k, values[k]))
                    seen.add(k)
                else:
                    lines.append(raw)
    except OSError:
        lines.append('# hvbackup client config of the dashboard user; read by the hvbackup CLI and the dashboard plug-ins')
    for k, v in values.items():
        if k not in seen and v is not None:
            lines.append('%s=%s' % (k, v))
    d = os.path.dirname(CLIENT_CONF)
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = '%s.tmp.%d' % (CLIENT_CONF, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write('\n'.join(lines) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CLIENT_CONF)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _config():
    cc = _read_client_conf()
    url = (cc.get('HVB_URL') or DEFAULT_URL).rstrip('/')
    token = cc.get('HVB_TOKEN') or ''
    verify = bool(cc.get('HVB_CA')) or cc.get('HVB_INSECURE', '1') != '1'
    try:
        overdue = float(cc.get('HVB_OVERDUE_HOURS') or 36)
    except ValueError:
        overdue = 36.0
    return {'url': url, 'token': token, 'verify_ssl': verify, 'public_url': (cc.get('HVB_PUBLIC_URL') or url).rstrip('/'),
            'overdue_hours': overdue, 'source': CLIENT_CONF if token else 'none'}


# ── hvbackup API client (urllib, bearer token, small cache) ──────────
_cache = {}
_cache_lock = threading.Lock()


def _call(method, path, body=None, timeout=TIMEOUT):
    cfg = _config()
    if not cfg['token']:
        raise HvbError('no hvbackup token configured (Settings on the VM Backup page, or ~/.config/hvbackup/client.conf '
                       'of the dashboard user)', 503)
    req = urllib.request.Request(cfg['url'] + '/api/v1' + path, method=method,
                                 headers={'Authorization': 'Bearer ' + cfg['token'], 'Accept': 'application/json'})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header('Content-Type', 'application/json')
    ctx = ssl.create_default_context() if cfg['verify_ssl'] else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, data, timeout=timeout, context=ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get('detail')
        except Exception:                                   # noqa: BLE001
            detail = None
        if e.code in (401, 403):
            raise HvbError('hvbackup rejected the token (%s)' % (detail or e.code), 502)
        raise HvbError(str(detail or 'hvbackup answered HTTP %d' % e.code), 502 if e.code >= 500 else e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, 'reason', None) or e
        raise HvbError('hvbackup unreachable at %s: %s' % (cfg['url'], reason), 503)


def _cached(path):
    """GET with a short per-path cache, so the summary poll and the history sampler do not
    hammer the server. Errors are cached too (briefly), so a dead server costs one
    connection attempt per interval instead of one per dashboard poll."""
    ttl = CACHE_TTL.get(path, 5)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(path)
        if hit and now - hit[0] < ttl:
            if isinstance(hit[1], HvbError):
                raise hit[1]
            return hit[1]
    try:
        val = _call('GET', path)
    except HvbError as e:
        with _cache_lock:
            _cache[path] = (now, e)
        raise
    with _cache_lock:
        _cache[path] = (now, val)
    return val


def _invalidate():
    with _cache_lock:
        _cache.clear()


# ── derived views ────────────────────────────────────────────────────
def _protection(vms, jobs, overdue_hours):
    """Classify every VM: failed (last run failed), overdue (in an enabled scheduled job but no
    restore point within the window), protected (has a recent restore point), stale (has
    restore points but no job), unprotected (nothing)."""
    now = time.time()
    scheduled = set()
    for j in jobs or []:
        if j.get('enabled') and j.get('schedule'):
            scheduled.update(j.get('vm_ids') or [])
    out = []
    for v in vms or []:
        last = v.get('last_restore_point') or {}
        run = v.get('last_run') or {}
        age = now - last['created'] if last.get('created') else None
        if run.get('status') == 'failed':
            state = 'failed'
        elif v['id'] in scheduled and (age is None or age > overdue_hours * 3600):
            state = 'overdue'
        elif age is not None and age <= overdue_hours * 3600:
            state = 'protected'
        elif age is not None:
            state = 'stale' if v['id'] not in scheduled else 'protected'
        else:
            state = 'unprotected'
        out.append({'id': v['id'], 'name': v['name'], 'connection': v.get('connection_name'), 'power_state': v.get('power_state'),
                    'size_bytes': v.get('size_bytes', 0), 'cbt_enabled': bool(v.get('cbt_enabled')), 'restore_points': v.get('restore_points', 0),
                    'last_backup': last.get('created'), 'last_kind': last.get('kind'), 'jobs': [j['name'] for j in v.get('jobs') or []],
                    'last_run': run, 'state': state, 'scheduled': v['id'] in scheduled})
    return out


def _overview_data():
    cfg = _config()
    st = _cached('/status')
    vms = _cached('/vms')
    jobs = _cached('/jobs')
    prot = _protection(vms, jobs, cfg['overdue_hours'])
    counts = {k: sum(1 for p in prot if p['state'] == k) for k in ('failed', 'overdue', 'protected', 'stale', 'unprotected')}
    rep = st.get('repository') or {}
    return {'available': True, 'version': st.get('version'), 'url': cfg['public_url'],
            'vms': st.get('vms', 0), 'protected_vms': st.get('protected_vms', 0), 'restore_points': st.get('restore_points', 0),
            'jobs': st.get('jobs', 0), 'running': st.get('running_tasks', 0), 'failed_24h': st.get('failed_runs_24h', 0),
            'proxies_online': st.get('proxies_online', 0), 'last_gc': st.get('last_gc'),
            'repo_used': rep.get('stored_bytes', 0), 'repo_free': rep.get('free_bytes', 0), 'repo_total': rep.get('total_bytes', 0),
            'repo_path': rep.get('path'), 'chunks': rep.get('chunks', 0),
            'protected_bytes': sum(p['size_bytes'] for p in prot if p['restore_points']),
            'states': counts, 'overdue_hours': cfg['overdue_hours']}


def _unavailable(e):
    cfg = _config()
    return {'available': False, 'configured': bool(cfg['token']), 'error': str(e), 'url': cfg['public_url']}


# ── routes: reads ────────────────────────────────────────────────────
def _guard(fn):
    """Turn HvbError into the dashboard's {success:false,error} shape with a telling code."""
    def wrapped(*a, **kw):
        try:
            return fn(*a, **kw)
        except HvbError as e:
            return err(str(e), e.code)
    wrapped.__name__ = fn.__name__
    return wrapped


@bp.route('/api/vm-backup/overview')
def overview():
    try:
        d = _overview_data()
        d['connections'] = [{k: c.get(k) for k in ('id', 'name', 'type', 'host', 'vm_count', 'proxies_online', 'last_inventory', 'last_error')}
                            for c in _cached('/connections')]
        d['proxies'] = [{k: p.get(k) for k in ('id', 'name', 'connection_name', 'online', 'address', 'version', 'last_seen', 'enabled')}
                        for p in _cached('/proxies')]
        return jsonify(d)
    except HvbError as e:
        return jsonify(_unavailable(e))


@bp.route('/api/vm-backup/vms')
@_guard
def vms():
    cfg = _config()
    return jsonify({'vms': _protection(_cached('/vms'), _cached('/jobs'), cfg['overdue_hours'])})


@bp.route('/api/vm-backup/jobs')
@_guard
def jobs():
    return jsonify({'jobs': _cached('/jobs')})


@bp.route('/api/vm-backup/runs')
@_guard
def runs():
    limit = request.args.get('limit', '50')
    if not RE_ID.match(limit):
        return err('bad limit')
    return jsonify({'runs': _call('GET', '/runs?limit=%d' % min(int(limit), 500))})


@bp.route('/api/vm-backup/restore-points')
@_guard
def restore_points():
    vm_id = request.args.get('vm_id', '')
    if vm_id and not RE_ID.match(vm_id):
        return err('bad vm_id')
    return jsonify({'restore_points': _call('GET', '/restore-points' + ('?vm_id=' + vm_id if vm_id else ''))})


@bp.route('/api/vm-backup/exports')
@_guard
def exports():
    return jsonify({'exports': _call('GET', '/exports'), 'url': _config()['public_url']})


@bp.route('/api/vm-backup/tasks')
@_guard
def tasks():
    limit = request.args.get('limit', '30')
    if not RE_ID.match(limit):
        return err('bad limit')
    rows = _call('GET', '/tasks?limit=%d' % min(int(limit), 200))
    for t in rows:
        t.pop('log', None)
    return jsonify({'tasks': rows})


@bp.route('/api/vm-backup/tasks/<tid>')
@_guard
def task(tid):
    if not RE_ID.match(tid):
        return err('bad task id')
    return jsonify({'task': _call('GET', '/tasks/' + tid)})


# ── routes: actions (POST = admin-only by the dashboard's central RBAC) ──
@bp.route('/api/vm-backup/vms/<vm_id>/backup', methods=['POST'])
@_guard
def backup_now(vm_id):
    if not RE_ID.match(vm_id):
        return err('bad vm id')
    body = request.get_json(silent=True) or {}
    res = _call('POST', '/vms/%s/backup' % vm_id, {'quiesce': bool(body.get('quiesce', True))})
    _invalidate()
    return jsonify({'success': True, 'task_id': res.get('task_id')})


@bp.route('/api/vm-backup/jobs/<jid>/run', methods=['POST'])
@_guard
def run_job(jid):
    if not RE_ID.match(jid):
        return err('bad job id')
    res = _call('POST', '/jobs/%s/run' % jid)
    _invalidate()
    return jsonify({'success': True, 'task_id': res.get('task_id')})


@bp.route('/api/vm-backup/restore-points/<rp>/export', methods=['POST'])
@_guard
def export_rp(rp):
    if not RE_ID.match(rp):
        return err('bad restore point id')
    body = request.get_json(silent=True) or {}
    fmt = body.get('format', 'ova')
    if fmt not in ('ova', 'ovf'):
        return err('format must be ova or ovf')
    res = _call('POST', '/restore-points/%s/export' % rp, {'format': fmt})
    return jsonify({'success': True, 'task_id': res.get('task_id')})


@bp.route('/api/vm-backup/tasks/<tid>/cancel', methods=['POST'])
@_guard
def cancel_task(tid):
    if not RE_ID.match(tid):
        return err('bad task id')
    _call('POST', '/tasks/%s/cancel' % tid)
    return jsonify({'success': True})


# ── routes: settings ─────────────────────────────────────────────────
@bp.route('/api/vm-backup/settings')
def settings_get():
    if not _is_admin():
        return err('admin only', 403)
    cfg = _config()
    return jsonify({'url': cfg['url'], 'public_url': cfg['public_url'], 'verify_ssl': cfg['verify_ssl'],
                    'overdue_hours': cfg['overdue_hours'], 'source': cfg['source'],
                    'token_prefix': cfg['token'][:8] + '…' if cfg['token'] else '', 'config_file': CLIENT_CONF})


@bp.route('/api/vm-backup/settings', methods=['POST'])
def settings_set():
    body = request.get_json(silent=True) or {}
    values = {}
    url = (body.get('url') or '').strip().rstrip('/')
    if url:
        if not RE_URL.match(url):
            return err('url must look like https://host:8460')
        values['HVB_URL'] = url
    if 'public_url' in body:
        pub = (body.get('public_url') or '').strip().rstrip('/')
        if pub and not RE_URL.match(pub):
            return err('public_url must look like https://host:8460')
        values['HVB_PUBLIC_URL'] = pub or None
    tok = (body.get('token') or '').strip()
    if tok:
        if not RE_TOKEN.match(tok):
            return err('that does not look like an hvbackup token')
        values['HVB_TOKEN'] = tok
    if 'verify_ssl' in body:
        values['HVB_INSECURE'] = '0' if body['verify_ssl'] else '1'
    if 'overdue_hours' in body:
        try:
            h = float(body['overdue_hours'])
        except (TypeError, ValueError):
            return err('overdue_hours must be a number')
        if not 1 <= h <= 24 * 90:
            return err('overdue_hours must be between 1 and 2160')
        values['HVB_OVERDUE_HOURS'] = ('%g' % h)
    try:
        _write_client_conf(values)
    except OSError as e:
        return err('cannot write %s: %s' % (CLIENT_CONF, e), 500)
    _invalidate()
    return jsonify({'success': True})


@bp.route('/api/vm-backup/settings/test', methods=['POST'])
def settings_test():
    """Try the configured (or the posted, unsaved) credentials: who am I on the server?"""
    body = request.get_json(silent=True) or {}
    cfg = _config()
    url = (body.get('url') or cfg['url']).rstrip('/')
    tok = body.get('token') or cfg['token']
    verify = bool(body.get('verify_ssl', cfg['verify_ssl']))
    if not RE_URL.match(url) or not tok:
        return err('url and token are needed')
    req = urllib.request.Request(url + '/api/v1/auth/me', headers={'Authorization': 'Bearer ' + tok, 'Accept': 'application/json'})
    ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            me = json.loads(r.read())
        st = urllib.request.urlopen(urllib.request.Request(url + '/api/v1/status', headers=req.headers), timeout=TIMEOUT, context=ctx)
        ver = json.loads(st.read()).get('version')
    except urllib.error.HTTPError as e:
        return err('server answered HTTP %d: check the token' % e.code, 502)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return err('cannot reach %s: %s' % (url, getattr(e, 'reason', None) or e), 503)
    return jsonify({'success': True, 'username': me.get('username'), 'role': me.get('role'), 'version': ver,
                    'note': '' if me.get('role') in ('operator', 'admin') else 'this token is read-only: Backup now and Run job will be refused by the server'})


# ── module hooks ─────────────────────────────────────────────────────
def _summary():
    try:
        d = _overview_data()
    except HvbError as e:
        return _unavailable(e)
    except Exception as e:                                  # noqa: BLE001
        return _unavailable(e)
    return {k: d[k] for k in ('available', 'version', 'url', 'vms', 'protected_vms', 'restore_points', 'jobs', 'running', 'failed_24h',
                              'proxies_online', 'repo_used', 'repo_free', 'repo_total', 'protected_bytes', 'states')}


def _alerts():
    out = []
    cfg = _config()
    if not cfg['token']:
        return out                        # not set up yet: the page says so, the alarm bar should not
    try:
        vms = _cached('/vms')
        jobs = _cached('/jobs')
        proxies = _cached('/proxies')
    except HvbError as e:
        return [{'key': 'vm-backup:down', 'message': 'VM backup server: %s' % e}]
    except Exception:                                       # noqa: BLE001
        return out
    for p in _protection(vms, jobs, cfg['overdue_hours']):
        if p['state'] == 'failed':
            msg = (p['last_run'] or {}).get('error') or 'failed'
            out.append({'key': 'vm-backup:failed:%d' % p['id'], 'message': 'Backup of %s failed: %s' % (p['name'], msg[:120])})
        elif p['state'] == 'overdue':
            when = time.strftime('%Y-%m-%d', time.localtime(p['last_backup'])) if p['last_backup'] else 'never'
            out.append({'key': 'vm-backup:overdue:%d' % p['id'], 'message': 'Backup of %s is overdue (last %s)' % (p['name'], when)})
    for px in proxies or []:
        if px.get('enabled', True) and not px.get('online'):
            out.append({'key': 'vm-backup:proxy:%d' % px['id'], 'message': 'Backup proxy %s is offline' % px['name']})
    return out


HISTORY_METRICS = {'hvb_repo_used', 'hvb_protected_vms', 'hvb_protected_bytes', 'hvb_restore_points'}


def _history():
    try:
        d = _overview_data()
    except Exception:                                       # noqa: BLE001
        return []
    return [('hvb_repo_used', '', d['repo_used']), ('hvb_protected_vms', '', d['protected_vms']),
            ('hvb_protected_bytes', '', d['protected_bytes']), ('hvb_restore_points', '', d['restore_points'])]


def _metrics():
    try:
        d = _overview_data()
    except Exception:                                       # noqa: BLE001
        return ['# HELP storagedash_hvb_up hvbackup server reachable (1) or not (0)', '# TYPE storagedash_hvb_up gauge', 'storagedash_hvb_up 0']
    lines = ['# HELP storagedash_hvb_up hvbackup server reachable (1) or not (0)', '# TYPE storagedash_hvb_up gauge', 'storagedash_hvb_up 1']
    for name, key, help_ in (('vms', 'vms', 'VMs in inventory'), ('protected_vms', 'protected_vms', 'VMs with at least one restore point'),
                             ('restore_points', 'restore_points', 'restore points'), ('failed_runs_24h', 'failed_24h', 'failed backup runs in 24h'),
                             ('running_tasks', 'running', 'running tasks'), ('proxies_online', 'proxies_online', 'proxies online'),
                             ('repo_used_bytes', 'repo_used', 'repository bytes stored'), ('repo_free_bytes', 'repo_free', 'repository bytes free'),
                             ('protected_bytes', 'protected_bytes', 'provisioned bytes of protected VMs')):
        lines += ['# HELP storagedash_hvb_%s %s' % (name, help_), '# TYPE storagedash_hvb_%s gauge' % name, 'storagedash_hvb_%s %s' % (name, d[key])]
    for state, n in d['states'].items():
        lines.append('storagedash_hvb_vm_state{state="%s"} %d' % (state, n))
    return lines


MODULE = {
    'id': PLUGIN_ID,
    'label': 'VM Backup',
    'category': 'Compute',
    'version': '0.2',
    'blueprint': bp,
    'nav': {'cat': 'compute', 'cat_order': 60, 'pages': [
        {'id': 'vm-backup', 'label': 'VM Backup', 'icon': 'box3'},
        {'id': 'vm-backup-history', 'label': 'Backup History', 'icon': 'clock'}]},
    'assets': {'js': ['plugin.js']},
    'services': {'vm-backup': {'name': 'hvbackup server', 'service': 'hvbackup', 'pkg': None,
                               'binary': '/opt/hvbackup/venv/bin/hvbackup', 'alert': False}},
    'summary': _summary,
    'alerts': _alerts,
    'history': _history,
    'history_metrics': HISTORY_METRICS,
    'metrics': _metrics,
}
