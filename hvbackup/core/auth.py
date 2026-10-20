"""Local accounts, API tokens, sessions, roles.

Roles, most to least powerful: admin > operator > restore > viewer.
  admin     everything, including users, tokens, connections, settings
  operator  run and schedule backups, restore
  restore   restore only, plus read
  viewer    read only
SSO providers (Nexus assertion, OIDC) plug in later and only answer "who is this";
the account must already exist here."""
import hashlib
import hmac
import secrets
import time

from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import db
from . import secrets as sec

ROLES = ['viewer', 'restore', 'operator', 'admin']
SESSION_TTL = 12 * 3600
PLUGIN_TOKEN_TTL = 3600


def role_at_least(have, need):
    return ROLES.index(have) >= ROLES.index(need)


# ── passwords ────────────────────────────────────────────────────────
def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    return 'scrypt$' + salt.hex() + '$' + dk.hex()


def check_password(pw, stored):
    try:
        algo, salt, dk = stored.split('$')
        if algo != 'scrypt':
            return False
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(calc.hex(), dk)
    except (ValueError, AttributeError):
        return False


# ── users ────────────────────────────────────────────────────────────
def create_user(username, password, role='viewer'):
    if role not in ROLES:
        raise ValueError('bad role')
    with db.conn() as c:
        c.execute('INSERT INTO users(username,password_hash,role,created) VALUES(?,?,?,?)',
                  (username, hash_password(password) if password else None, role, time.time()))
        return c.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()[0]


def get_user(username):
    with db.conn() as c:
        return db.row_to_dict(c.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone())


def authenticate_password(username, password):
    u = get_user(username)
    if not u or u['disabled'] or not u['password_hash'] or not check_password(password, u['password_hash']):
        return None
    return u


def list_users():
    with db.conn() as c:
        return [db.row_to_dict(r) for r in c.execute('SELECT id,username,role,created,disabled FROM users ORDER BY username')]


def set_password(username, password):
    with db.conn() as c:
        c.execute('UPDATE users SET password_hash=? WHERE username=?', (hash_password(password), username))


def set_role(username, role):
    if role not in ROLES:
        raise ValueError('bad role')
    with db.conn() as c:
        c.execute('UPDATE users SET role=? WHERE username=?', (role, username))


def delete_user(username):
    with db.conn() as c:
        admins = c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0").fetchone()[0]
        u = c.execute('SELECT role FROM users WHERE username=?', (username,)).fetchone()
        if u and u['role'] == 'admin' and admins <= 1:
            raise ValueError('cannot delete the last administrator')
        c.execute('DELETE FROM users WHERE username=?', (username,))


# ── API tokens ───────────────────────────────────────────────────────
def _token_hash(t):
    return hashlib.sha256(t.encode()).hexdigest()


def create_token(user_id, name):
    t = 'hvb_' + secrets.token_urlsafe(32)
    with db.conn() as c:
        c.execute('INSERT INTO tokens(user_id,name,token_hash,prefix,created) VALUES(?,?,?,?,?)',
                  (user_id, name, _token_hash(t), t[:10], time.time()))
    return t


def authenticate_token(t):
    if not t or not t.startswith('hvb_'):
        return None
    with db.conn() as c:
        r = c.execute('SELECT u.*, t.id AS token_id, t.last_used AS token_last_used FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=?',
                      (_token_hash(t),)).fetchone()
        if not r or r['disabled']:
            return None
        if not r['token_last_used'] or time.time() - r['token_last_used'] > 60:
            c.execute('UPDATE tokens SET last_used=? WHERE id=?', (time.time(), r['token_id']))
        u = db.row_to_dict(r)
        u['via'] = 'token'
        return u


def list_tokens(user_id=None):
    with db.conn() as c:
        q = 'SELECT t.id,t.name,t.prefix,t.created,t.last_used,u.username FROM tokens t JOIN users u ON u.id=t.user_id'
        args = ()
        if user_id is not None:
            q += ' WHERE t.user_id=?'
            args = (user_id,)
        return [db.row_to_dict(r) for r in c.execute(q + ' ORDER BY t.created', args)]


def delete_token(token_id):
    with db.conn() as c:
        c.execute('DELETE FROM tokens WHERE id=?', (token_id,))


# ── proxy tokens ─────────────────────────────────────────────────────
def create_proxy(name, connection_id=None):
    t = 'hvp_' + secrets.token_urlsafe(32)
    with db.conn() as c:
        c.execute('INSERT INTO proxies(name,connection_id,token_hash,prefix,created) VALUES(?,?,?,?,?)',
                  (name, connection_id, _token_hash(t), t[:10], time.time()))
        pid = c.execute('SELECT id FROM proxies WHERE name=?', (name,)).fetchone()[0]
    return pid, t


def authenticate_proxy(t):
    if not t or not t.startswith('hvp_'):
        return None
    with db.conn() as c:
        r = c.execute('SELECT * FROM proxies WHERE token_hash=? AND enabled=1', (_token_hash(t),)).fetchone()
        return db.row_to_dict(r)


# ── sessions ─────────────────────────────────────────────────────────
def _serializer():
    return URLSafeTimedSerializer(sec.signing_key(), salt='hvb-session')


def make_plugin_token(username):
    """Short-lived bearer token for vSphere Client plug-in pages (no cookie: they live in another origin)."""
    return 'hvbp_' + URLSafeTimedSerializer(sec.signing_key(), salt='hvb-plugin').dumps({'u': username})


def read_plugin_token(t):
    if not t or not t.startswith('hvbp_'):
        return None
    try:
        data = URLSafeTimedSerializer(sec.signing_key(), salt='hvb-plugin').loads(t[5:], max_age=PLUGIN_TOKEN_TTL)
    except BadSignature:
        return None
    u = get_user(data.get('u'))
    if not u or u['disabled']:
        return None
    u['via'] = 'vsphere'
    return u


def make_session(username):
    return _serializer().dumps({'u': username, 't': time.time()})


def read_session(cookie):
    if not cookie:
        return None
    try:
        data = _serializer().loads(cookie, max_age=SESSION_TTL)
    except BadSignature:
        return None
    u = get_user(data.get('u'))
    if not u or u['disabled']:
        return None
    u['via'] = 'session'
    return u
