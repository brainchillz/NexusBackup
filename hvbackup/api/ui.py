"""Web UI: Jinja2 shells that render themselves from the REST API with fetch().
The server injects who is signed in and which theme drop-ins exist; everything
else comes from /api/v1.

Themes follow Nexus Dashboard's contract (its THEMES.md): a directory
<HVB_DATA_DIR>/themes/<id>/ holding theme.json, theme.css and optionally
theme.js, keyed on html[data-theme="<id>"]. A theme written for the dashboard
works here unchanged."""
import json
import os
import re
import urllib.parse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import APP_VERSION, config
from ..core import auth
from .deps import COOKIE

r = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), '..', 'web', 'templates'))

PAGES = {
    '/': ('dashboard.html', 'Dashboard'),
    '/connections': ('connections.html', 'Connections'),
    '/vms': ('vms.html', 'Virtual machines'),
    '/jobs': ('jobs.html', 'Jobs'),
    '/runs': ('runs.html', 'Runs'),
    '/restore-points': ('restore_points.html', 'Restore points'),
    '/proxies': ('proxies.html', 'Proxies'),
    '/tasks': ('tasks.html', 'Tasks'),
    '/settings': ('settings.html', 'Settings'),
}

NAV = [
    ('Overview', [('/', 'Dashboard')]),
    ('Protect', [('/vms', 'Virtual machines'), ('/jobs', 'Jobs'), ('/restore-points', 'Restore points')]),
    ('Activity', [('/runs', 'Runs'), ('/tasks', 'Tasks')]),
    ('Infrastructure', [('/connections', 'Connections'), ('/proxies', 'Proxies')]),
    ('System', [('/settings', 'Settings')]),
]

THEME_ID = re.compile(r'^[a-z][a-z0-9-]{1,31}$')
THEME_FILE = re.compile(r'^[A-Za-z0-9._-]+$')
THEMES_DIR = os.path.join(config.DATA_DIR, 'themes')


def themes():
    """Theme drop-ins under <data dir>/themes/<id>/: id, name, modes, variants, and a
    version tag from file mtimes so an edited stylesheet is refetched."""
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
                    'js': os.path.exists(os.path.join(d, meta.get('js', 'theme.js'))), 'v': v})
    return out


@r.get('/themes/{theme_id}/{filename}')
def theme_file(theme_id: str, filename: str):
    if not THEME_ID.match(theme_id) or not THEME_FILE.match(filename) or filename.startswith('.'):
        raise HTTPException(404)
    base = os.path.realpath(THEMES_DIR)
    path = os.path.realpath(os.path.join(base, theme_id, filename))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        raise HTTPException(404)
    media = {'.css': 'text/css', '.js': 'text/javascript', '.json': 'application/json', '.woff2': 'font/woff2', '.woff': 'font/woff',
             '.ttf': 'font/ttf', '.png': 'image/png', '.svg': 'image/svg+xml', '.jpg': 'image/jpeg', '.webp': 'image/webp'}
    ext = os.path.splitext(filename)[1].lower()
    if ext not in media:
        raise HTTPException(404)
    return FileResponse(path, media_type=media[ext])


def _user(request: Request):
    return auth.read_session(request.cookies.get(COOKIE))


def _render(request, template, title, user, **extra):
    ctx = {'request': request, 'user': {'username': user['username'], 'role': user['role']}, 'title': title,
           'nav': NAV, 'path': request.url.path, 'version': APP_VERSION, 'themes': themes()}
    ctx.update(extra)
    return templates.TemplateResponse(request, template, ctx)


def _login_redirect(request):
    nxt = urllib.parse.quote(str(request.url.path) + ('?' + str(request.url.query) if request.url.query else ''))
    return RedirectResponse(f'/login?next={nxt}', status_code=302)


@r.get('/login')
def login(request: Request):
    if _user(request):
        return RedirectResponse('/', status_code=302)
    return templates.TemplateResponse(request, 'login.html', {'request': request, 'version': APP_VERSION, 'title': 'Sign in', 'themes': themes()})


def _page(path):
    template, title = PAGES[path]

    def view(request: Request):
        user = _user(request)
        if not user:
            return _login_redirect(request)
        return _render(request, template, title, user)
    view.__name__ = 'page_' + (path.strip('/').replace('-', '_') or 'dashboard')
    return view


for _p in PAGES:
    r.add_api_route(_p, _page(_p), methods=['GET'])


@r.get('/tasks/{task_id}')
def task_detail(request: Request, task_id: int):
    user = _user(request)
    if not user:
        return _login_redirect(request)
    return _render(request, 'task.html', f'Task {task_id}', user, task_id=task_id)


@r.get('/vms/{vm_id}')
def vm_detail(request: Request, vm_id: int):
    user = _user(request)
    if not user:
        return _login_redirect(request)
    return _render(request, 'vm.html', 'Virtual machine', user, vm_id=vm_id)
