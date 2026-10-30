"""Catalog backup and restore. A backup is a tar of a consistent copy of catalog.db (the
SQLite backup API, so a running server needs no pause), secret.key (it decrypts the
hypervisor credentials in the catalog) and the server certificate pair. With the
repository it is everything a dead server needs: the daily copy goes to
<repository>/catalog/, the Settings page downloads one on demand.

A restore never swaps files under a running server: the archive is unpacked into
<data>/restore-pending/ and apply_pending() moves it into place at the next start
(the Settings page offers the restart). The replaced catalog is kept as
catalog.db.before-restore."""
import io
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import time

from .. import config, db

log = logging.getLogger('hvbackup.catalog')
PENDING = 'restore-pending'
NAME = re.compile(r'^hvbackup-catalog-\d{8}-\d{6}\.tar$')
MEMBERS = ('catalog.db', 'secret.key', 'certs/server.crt', 'certs/server.key')


def _copy_catalog(dest):
    src = sqlite3.connect(config.DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def write(fileobj):
    """Write a backup tar to a file object. Returns the member names written."""
    tmp = config.DB_PATH + '.backup-tmp'
    _copy_catalog(tmp)
    written = []
    try:
        with tarfile.open(fileobj=fileobj, mode='w') as tar:
            tar.add(tmp, arcname='catalog.db')
            written.append('catalog.db')
            for rel in MEMBERS[1:]:
                p = os.path.join(config.DATA_DIR, rel)
                if os.path.exists(p):
                    tar.add(p, arcname=rel)
                    written.append(rel)
            info = tarfile.TarInfo('README.txt')
            text = (f'hvbackup catalog backup, {time.strftime("%Y-%m-%d %H:%M:%S %Z")}, data directory {config.DATA_DIR}.\n'
                    'Restore from Settings > Catalog (upload or pick a copy from the repository), or by hand: stop the server, '
                    'unpack catalog.db, secret.key and certs/ into the data directory, start it.\n').encode()
            info.size = len(text)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(text))
    finally:
        os.unlink(tmp)
    return written


def repo_dir():
    from ..store.chunks import repo
    return os.path.join(repo().path, 'catalog')


def list_copies():
    d = repo_dir()
    out = []
    try:
        names = sorted(os.listdir(d), reverse=True)
    except OSError:
        return out
    for n in names:
        if NAME.match(n):
            st = os.stat(os.path.join(d, n))
            out.append({'name': n, 'size': st.st_size, 'created': st.st_mtime})
    return out


def write_copy(keep):
    """One copy into the repository, then prune to `keep`. Returns the file name."""
    d = repo_dir()
    os.makedirs(d, exist_ok=True)
    name = time.strftime('hvbackup-catalog-%Y%m%d-%H%M%S.tar')
    path = os.path.join(d, name)
    fd = os.open(path + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        write(f)
    os.replace(path + '.tmp', path)
    for old in list_copies()[max(keep, 1):]:
        try:
            os.unlink(os.path.join(d, old['name']))
        except OSError:
            pass
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_catalog_backup',?)", (str(time.time()),))
    log.info('catalog copy written to %s', path)
    return name


def maybe_copy():
    """Scheduler hook: one copy a day when the setting is on."""
    from . import settings
    if not settings.get('catalog_backup'):
        return
    with db.conn() as c:
        r = c.execute("SELECT value FROM meta WHERE key='last_catalog_backup'").fetchone()
    last = float(r['value']) if r else 0
    if time.time() - last >= 24 * 3600:
        try:
            write_copy(settings.get('catalog_backup_keep'))
        except Exception:  # noqa: BLE001
            log.exception('daily catalog copy failed')


def _check_catalog(path):
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('the catalog in the archive fails its integrity check')
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ('users', 'connections', 'vms', 'restore_points'):
            if t not in tables:
                raise ValueError(f'the archive\'s catalog has no {t} table; not an hvbackup catalog')
        users = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        rps = c.execute('SELECT COUNT(*) FROM restore_points WHERE deleted=0').fetchone()[0]
        vms = c.execute('SELECT COUNT(*) FROM vms WHERE gone=0').fetchone()[0]
    finally:
        c.close()
    return {'users': users, 'vms': vms, 'restore_points': rps}


def stage(fileobj):
    """Unpack a backup archive into <data>/restore-pending/ after checking it. Returns what
    the catalog holds so the UI can show it before the restart."""
    pending = os.path.join(config.DATA_DIR, PENDING)
    tmp = pending + '.tmp'
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, mode=0o700)
    try:
        with tarfile.open(fileobj=fileobj, mode='r:*') as tar:
            got = set()
            for m in tar.getmembers():
                if m.name not in MEMBERS or not m.isfile():
                    continue
                got.add(m.name)
                dest = os.path.join(tmp, m.name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with tar.extractfile(m) as src, open(dest, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(dest, 0o600)
        if 'catalog.db' not in got:
            raise ValueError('the archive holds no catalog.db')
        summary = _check_catalog(os.path.join(tmp, 'catalog.db'))
        summary['members'] = sorted(got)
        shutil.rmtree(pending, ignore_errors=True)
        os.replace(tmp, pending)
    except (tarfile.TarError, OSError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f'not a readable tar archive: {e}')
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return summary


def stage_copy(name):
    if not NAME.match(name):
        raise ValueError('bad copy name')
    with open(os.path.join(repo_dir(), name), 'rb') as f:
        return stage(f)


def pending():
    p = os.path.join(config.DATA_DIR, PENDING)
    if not os.path.exists(os.path.join(p, 'catalog.db')):
        return None
    try:
        return {'staged': os.stat(os.path.join(p, 'catalog.db')).st_mtime, 'members': sorted(
            os.path.relpath(os.path.join(r, f), p) for r, _, fs in os.walk(p) for f in fs)}
    except OSError:
        return None


def discard_pending():
    shutil.rmtree(os.path.join(config.DATA_DIR, PENDING), ignore_errors=True)


def apply_pending():
    """Called before the catalog is opened at start: move a staged restore into place."""
    p = os.path.join(config.DATA_DIR, PENDING)
    if not os.path.exists(os.path.join(p, 'catalog.db')):
        return False
    log.warning('applying the staged catalog restore from %s', p)
    for suffix in ('-wal', '-shm'):
        try:
            os.unlink(config.DB_PATH + suffix)
        except FileNotFoundError:
            pass
    if os.path.exists(config.DB_PATH):
        os.replace(config.DB_PATH, config.DB_PATH + '.before-restore')
    os.replace(os.path.join(p, 'catalog.db'), config.DB_PATH)
    for rel in MEMBERS[1:]:
        src = os.path.join(p, rel)
        if os.path.exists(src):
            dest = os.path.join(config.DATA_DIR, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest):
                os.replace(dest, dest + '.before-restore')
            os.replace(src, dest)
    shutil.rmtree(p, ignore_errors=True)
    return True
