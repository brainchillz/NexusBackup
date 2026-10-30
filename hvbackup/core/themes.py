"""Theme drop-ins under <data dir>/themes/<id>/ (theme.json, theme.css, optional theme.js),
the same contract as Nexus Dashboard's THEMES.md, so a theme written for the dashboard
works here unchanged. Settings uploads them as a zip; the directory form still works."""
import io
import json
import os
import re
import shutil
import zipfile

from .. import config

THEME_ID = re.compile(r'^[a-z][a-z0-9-]{1,31}$')
THEME_FILE = re.compile(r'^[A-Za-z0-9._-]+$')
THEMES_DIR = os.path.join(config.DATA_DIR, 'themes')
MAX_ZIP = 20 * 1024 * 1024
MAX_FILES = 200


def list_themes():
    """id, name, modes, variants, and a version tag from file mtimes so an edited
    stylesheet is refetched."""
    out = []
    try:
        ids = sorted(os.listdir(THEMES_DIR))
    except OSError:
        return out
    for tid in ids:
        d = os.path.join(THEMES_DIR, tid)
        if not THEME_ID.match(tid) or not os.path.isdir(d) or not os.path.exists(os.path.join(d, 'theme.css')):
            continue
        meta = {}
        try:
            meta = json.load(open(os.path.join(d, 'theme.json')))
        except (OSError, ValueError):
            pass
        if meta.get('id') and meta['id'] != tid:
            continue
        try:
            v = str(int(max(os.stat(os.path.join(d, f)).st_mtime for f in os.listdir(d))))
        except (OSError, ValueError):
            v = '0'
        out.append({'id': tid, 'name': meta.get('name', tid), 'description': meta.get('description', ''),
                    'modes': meta.get('modes', ['dark', 'light']), 'variants': meta.get('variants', []),
                    'variant_label': meta.get('variant_label', 'Variant'), 'default_variant': meta.get('default_variant'),
                    'js': os.path.exists(os.path.join(d, meta.get('js', 'theme.js'))), 'v': v,
                    'files': sorted(f for f in os.listdir(d) if THEME_FILE.match(f))})
    return out


def file_path(theme_id, filename):
    """Real path of a theme file, or None when the name is bad or escapes the directory."""
    if not THEME_ID.match(theme_id) or not THEME_FILE.match(filename) or filename.startswith('.'):
        return None
    base = os.path.realpath(THEMES_DIR)
    path = os.path.realpath(os.path.join(base, theme_id, filename))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        return None
    return path


def install_zip(data):
    """Install a theme from zip bytes. The files may sit at the root of the archive or in
    one top-level directory; the id comes from theme.json or that directory. Returns the
    theme as list_themes() reports it."""
    if len(data) > MAX_ZIP:
        raise ValueError('theme archive larger than 20 MiB')
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError('not a zip archive')
    names = [n for n in z.namelist() if not n.endswith('/')]
    if len(names) > MAX_FILES:
        raise ValueError('too many files in the archive')
    css = [n for n in names if n.split('/')[-1] == 'theme.css']
    if not css:
        raise ValueError('the archive holds no theme.css')
    prefix = css[0][:-len('theme.css')]
    if prefix.count('/') > 1:
        raise ValueError('theme.css must sit at the root of the archive or in one directory')
    files = {}
    for n in names:
        if not n.startswith(prefix):
            continue
        rel = n[len(prefix):]
        if '/' in rel or not THEME_FILE.match(rel) or rel.startswith('.'):
            raise ValueError(f'bad file name in the archive: {n}')
        files[rel] = z.read(n)
    meta = {}
    if 'theme.json' in files:
        try:
            meta = json.loads(files['theme.json'].decode())
        except (ValueError, UnicodeDecodeError):
            raise ValueError('theme.json is not valid JSON')
    tid = meta.get('id') or prefix.strip('/')
    if not tid or not THEME_ID.match(tid):
        raise ValueError('the theme needs an id (theme.json "id" or the directory name): lowercase letters, digits and dashes')
    dest = os.path.join(THEMES_DIR, tid)
    tmp = dest + '.tmp'
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for rel, blob in files.items():
        with open(os.path.join(tmp, rel), 'wb') as f:
            f.write(blob)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    return next(t for t in list_themes() if t['id'] == tid)


def remove(theme_id):
    if not THEME_ID.match(theme_id):
        raise ValueError('bad theme id')
    d = os.path.join(THEMES_DIR, theme_id)
    if not os.path.isdir(d):
        raise ValueError('no such theme')
    shutil.rmtree(d)
