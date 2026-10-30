"""Runtime settings: a `settings` table in the catalog that the UI edits. Environment
variables and the config file are bootstrap defaults: a key they set wins over the
table and shows locked in the UI, with a note saying where it comes from. Values
are JSON in the table so a bool or an int survives the round trip.

get('public_url') is the one call sites use; it reads the environment first, then the
table, then the default. Changes that take effect at once (log level, time zone) are
applied by apply(); keys flagged restart=True need the server restarted."""
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import zoneinfo

from .. import config, db

log = logging.getLogger('hvbackup.settings')
_BOOT_TZ = os.environ.get('TZ')  # apply() exports TZ itself; only the value at start counts as "set by the environment"
_cache = {}
_cache_t = 0.0
_lock = threading.Lock()
CACHE_TTL = 5.0


def _system_timezone():
    tz = os.environ.get('TZ')
    if tz:
        return tz
    try:
        target = os.readlink('/etc/localtime')
        if 'zoneinfo/' in target:
            return target.split('zoneinfo/', 1)[1]
    except OSError:
        pass
    try:
        return open('/etc/timezone').read().strip() or 'UTC'
    except OSError:
        return 'UTC'


DEFAULT_PROXY_IMAGE_URL = 'https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.ova'

# key -> env var, default, kind, label, help, restart, section. Order is the UI order.
SPEC = {
    'public_url': dict(env='HVB_URL', default='', kind='url', section='server', label='Public URL',
                       help='What browsers, proxy VMs, Proxmox nodes and vCenter reach this server at, e.g. https://backup.example.net:8460. '
                            'Proxies, the Proxmox storage registration and the vSphere plug-in pin it: after a change, redeploy the proxies, '
                            're-register the storage (Connections) and update the plug-in registration.'),
    'timezone': dict(env='TZ', default=_system_timezone(), kind='timezone', section='server', label='Time zone',
                     help='Cron schedules of jobs and replications and the nightly maintenance hour run in this zone.'),
    'session_hours': dict(env='HVB_SESSION_HOURS', default=12, kind='int', section='server', label='Session lifetime (hours)',
                          help='How long a web sign-in lasts. Existing sessions keep the lifetime they were issued with.', min=1, max=24 * 30),
    'log_level': dict(env='HVB_LOG_LEVEL', default='info', kind='choice', choices=['debug', 'info', 'warning'], section='server',
                      label='Log level', help='Applied at once to the server log (stdout, journal or container log, and the Logs card).'),
    'update_check': dict(env='HVB_UPDATE_CHECK', default=True, kind='bool', section='server', label='Check for updates',
                         help='Once a day, ask GitHub for the newest release and show a notice in the sidebar when this server is behind. The request carries nothing but the version check.'),
    'bind': dict(env='HVB_BIND', default='0.0.0.0', kind='str', section='server', label='Listen address', restart=True,
                 help='Address the HTTPS listener binds to. Takes effect after a restart.'),
    'port': dict(env='HVB_PORT', default=8460, kind='int', section='server', label='Listen port', restart=True, min=1, max=65535,
                 help='Port of the HTTPS listener. In Docker the published port in compose.yml must match. Takes effect after a restart.'),
    'concurrency': dict(env='HVB_CONCURRENCY', default=2, kind='int', section='server', label='Concurrent operations per connection', min=1, max=32,
                        help='Backups, restores and replication syncs running at once against one vCenter, host or cluster; the rest wait in their task.'),
    'verify_enabled': dict(env='HVB_VERIFY', default=True, kind='bool', section='verify', label='Scheduled verification',
                           help='Read every chunk of every restore point on the schedule below and mark the point ok or damaged.'),
    'verify_schedule': dict(env='HVB_VERIFY_SCHEDULE', default='0 4 * * 0', kind='cron', section='verify', label='Verification schedule (cron)',
                            help='In the server\'s time zone. Default: Sundays at 04:00.'),
    'verify_max_age_days': dict(env='HVB_VERIFY_MAX_AGE_DAYS', default=7, kind='int', section='verify', label='Skip points verified within (days)', min=1, max=365,
                                help='A scheduled run reads only points not verified within this many days, so the work spreads over the weeks on a large repository.'),
    'webhook_enabled': dict(env='HVB_WEBHOOK', default=False, kind='bool', section='notify', label='Send notifications to a webhook',
                            help='One HTTPS POST with a JSON document per event. Slack, Mattermost, Discord and Teams incoming webhooks show it as a message; anything else gets the fields.'),
    'webhook_url': dict(env='HVB_WEBHOOK_URL', default='', kind='url', section='notify', label='Webhook URL'),
    'webhook_secret': dict(env='HVB_WEBHOOK_SECRET', default='', kind='secret', section='notify', label='Signing secret (optional)',
                           help='When set, every request carries X-Hvbackup-Signature: sha256=HMAC-SHA256(secret, body).'),
    'notify_backup_failed': dict(env='HVB_NOTIFY_BACKUP_FAILED', default=True, kind='bool', section='notify', label='Failed backups'),
    'notify_verify_failed': dict(env='HVB_NOTIFY_VERIFY_FAILED', default=True, kind='bool', section='notify', label='Failed verifications (damaged restore points)'),
    'notify_replication_failed': dict(env='HVB_NOTIFY_REPLICATION_FAILED', default=True, kind='bool', section='notify', label='Failed replication syncs'),
    'notify_task_failed': dict(env='HVB_NOTIFY_TASK_FAILED', default=True, kind='bool', section='notify', label='Failed tasks (restores, exports, conversions, proxy deployments, anything else)'),
    'catalog_backup': dict(env='HVB_CATALOG_BACKUP', default=True, kind='bool', section='catalog', label='Daily catalog copy into the repository',
                           help='Writes a copy of the catalog, the secret key and the certificate into <repository>/catalog/ once a day, so the repository alone can rebuild a dead server.'),
    'catalog_backup_keep': dict(env='HVB_CATALOG_BACKUP_KEEP', default=7, kind='int', section='catalog', label='Catalog copies to keep', min=1, max=365),
    'proxy_image_url': dict(env='HVB_PROXY_IMAGE_URL', default=DEFAULT_PROXY_IMAGE_URL, kind='url', section='proxy', label='Proxy image URL',
                            help='Cloud image (OVA) the server deploys proxy VMs from. The checksum is read from SHA256SUMS next to it when present.'),
    'proxy_image_sha256': dict(env='HVB_PROXY_IMAGE_SHA256', default='', kind='str', section='proxy', label='Proxy image SHA-256',
                               help='Expected checksum of the download, for mirrors without a SHA256SUMS file. Blank: use the publisher\'s list or skip verification.'),
    'proxy_image': dict(env='HVB_PROXY_IMAGE', default='', kind='path', section='proxy', label='Local proxy image',
                        help='Path of an OVA on the server to use instead of downloading (offline sites). Upload one below to set it.'),
}


def _coerce(key, raw):
    spec = SPEC[key]
    kind = spec['kind']
    if kind == 'int':
        try:
            v = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f'{spec["label"]}: not a whole number')
        if 'min' in spec and v < spec['min'] or 'max' in spec and v > spec['max']:
            raise ValueError(f'{spec["label"]}: must be between {spec.get("min", "")} and {spec.get("max", "")}')
        return v
    if kind == 'bool':
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')
    if kind == 'choice':
        v = str(raw).strip().lower()
        if v not in spec['choices']:
            raise ValueError(f'{spec["label"]}: one of {", ".join(spec["choices"])}')
        return v
    if kind == 'cron':
        from . import scheduler
        v = str(raw).strip()
        if v and not scheduler.validate(v):
            raise ValueError(f'{spec["label"]}: not a cron expression (minute hour day month weekday)')
        return v
    if kind == 'timezone':
        v = str(raw).strip()
        try:
            zoneinfo.ZoneInfo(v)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            raise ValueError(f'{spec["label"]}: unknown zone {v!r} (use an IANA name such as Europe/Amsterdam or UTC)')
        return v
    if kind == 'url':
        v = str(raw).strip().rstrip('/')
        if v:
            u = urllib.parse.urlparse(v)
            if u.scheme not in ('http', 'https') or not u.netloc:
                raise ValueError(f'{spec["label"]}: must be an http(s) URL')
        return v
    if kind == 'secret':
        return str(raw).strip()
    if kind == 'path':
        v = str(raw).strip()
        if v and not os.path.isabs(v):
            raise ValueError(f'{spec["label"]}: must be an absolute path')
        return v
    return str(raw).strip()


def _bootstrap(key):
    """(value, source) from the environment or the config file, or (None, None). An empty
    value counts as unset: compose.yml passes HVB_URL=${HVB_URL:-} even when nobody set it."""
    env = SPEC[key]['env']
    v = _BOOT_TZ if env == 'TZ' else os.environ.get(env)
    if v is not None and v.strip() != '':
        return v, 'environment'
    v = config._file.get(env)
    if v is not None and v.strip() != '':
        return v, 'config file'
    return None, None


def _table():
    global _cache, _cache_t
    now = time.time()
    if now - _cache_t < CACHE_TTL:
        return _cache
    with _lock:
        with db.conn() as c:
            rows = {r['key']: r['value'] for r in c.execute('SELECT key, value FROM settings')}
        out = {}
        for k, v in rows.items():
            if k in SPEC:
                try:
                    out[k] = json.loads(v)
                except ValueError:
                    pass
        _cache, _cache_t = out, now
    return _cache


def invalidate():
    global _cache_t
    _cache_t = 0.0


def resolve(key):
    """(value, source): source is environment, config file, settings or default."""
    raw, src = _bootstrap(key)
    if src:
        try:
            return _coerce(key, raw), src
        except ValueError as e:
            log.warning('%s: ignoring %s=%r (%s)', key, SPEC[key]['env'], raw, e)
    t = _table()
    if key in t:
        try:
            return _coerce(key, t[key]), 'settings'
        except ValueError:
            pass
    return SPEC[key]['default'], 'default'


def get(key):
    return resolve(key)[0]


def describe():
    """Every setting for the API: value, where it comes from, whether the UI may change it."""
    out = []
    for key, spec in SPEC.items():
        value, source = resolve(key)
        if spec['kind'] == 'secret':
            value = '••••••••' if value else ''
        out.append({'key': key, 'value': value, 'default': spec['default'], 'source': source, 'locked': source in ('environment', 'config file'),
                    'env': spec['env'], 'kind': spec['kind'], 'choices': spec.get('choices'), 'label': spec['label'], 'help': spec.get('help', ''),
                    'section': spec['section'], 'restart': bool(spec.get('restart'))})
    return out


def update(changes, user=None):
    """Validate and store changes {key: value}; a value of None or the default clears the
    row. Returns the keys that need a restart to take effect."""
    coerced = {}
    for key, raw in changes.items():
        if key not in SPEC:
            raise ValueError(f'unknown setting {key}')
        if _bootstrap(key)[1]:
            raise ValueError(f'{SPEC[key]["label"]} is set by the {_bootstrap(key)[1]} ({SPEC[key]["env"]}); change it there')
        coerced[key] = None if raw is None else _coerce(key, raw)
    restart = []
    with db.conn() as c:
        for key, v in coerced.items():
            before = resolve(key)[0]
            if v is None or v == SPEC[key]['default']:
                c.execute('DELETE FROM settings WHERE key=?', (key,))
            else:
                c.execute('INSERT OR REPLACE INTO settings(key, value, updated) VALUES(?,?,?)', (key, json.dumps(v), time.time()))
            if before != (SPEC[key]['default'] if v is None else v):
                if SPEC[key]['kind'] == 'secret':
                    db.audit(user, 'setting', f'{key}: changed')
                else:
                    db.audit(user, 'setting', f'{key}: {before!r} -> {(SPEC[key]["default"] if v is None else v)!r}')
                if SPEC[key].get('restart'):
                    restart.append(key)
    invalidate()
    apply()
    return restart


def apply():
    """Push settings that take effect at once into the process: the log level and the zone."""
    from . import logs
    logs.set_level(get('log_level'))
    tz = get('timezone')
    if os.environ.get('TZ') != tz:
        os.environ['TZ'] = tz
        time.tzset()


def zone():
    return zoneinfo.ZoneInfo(get('timezone'))


def public_url(fallback=''):
    return (get('public_url') or fallback or '').rstrip('/')


VALID_KEY = re.compile(r'^[a-z][a-z0-9_]{1,63}$')
