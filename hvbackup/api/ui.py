"""Web UI: Jinja2 shells that render themselves from the REST API with fetch().
The server only injects who is signed in; everything else comes from /api/v1."""
import os
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import APP_VERSION
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

NAV = [('/', 'Dashboard'), ('/vms', 'Virtual machines'), ('/jobs', 'Jobs'), ('/runs', 'Runs'),
       ('/restore-points', 'Restore points'), ('/tasks', 'Tasks'), ('/connections', 'Connections'),
       ('/proxies', 'Proxies'), ('/settings', 'Settings')]


def _user(request: Request):
    return auth.read_session(request.cookies.get(COOKIE))


def _render(request, template, title, user, **extra):
    ctx = {'request': request, 'user': {'username': user['username'], 'role': user['role']}, 'title': title,
           'nav': NAV, 'path': request.url.path, 'version': APP_VERSION}
    ctx.update(extra)
    return templates.TemplateResponse(request, template, ctx)


def _login_redirect(request):
    nxt = urllib.parse.quote(str(request.url.path) + ('?' + str(request.url.query) if request.url.query else ''))
    return RedirectResponse(f'/login?next={nxt}', status_code=302)


@r.get('/login')
def login(request: Request):
    if _user(request):
        return RedirectResponse('/', status_code=302)
    return templates.TemplateResponse(request, 'login.html', {'request': request, 'version': APP_VERSION, 'title': 'Sign in'})


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
