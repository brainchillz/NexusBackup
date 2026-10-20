"""Routes that make hvbackup a vSphere Client remote plug-in: the generated manifest,
the plug-in pages and assets, and the session exchange that turns a vSphere Client
session token into a short-lived hvbackup bearer token."""
import json
import os
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .. import APP_VERSION, config, db
from ..core import auth, ops
from ..providers import vsphere_plugin as vsp

r = APIRouter()
STATIC = os.path.join(os.path.dirname(__file__), '..', 'web', 'vsphere')
PAGES = {'global', 'vm-summary', 'vm-monitor', 'backup-now', 'restore'}


def manifest():
    return {
        'manifestVersion': '1.0.0.0',
        'requirements': {'plugin.api.version': '1.0.0.0'},
        'configuration': {'nameKey': 'plugin.name', 'icon': {'name': 'main'}},
        'global': {'view': {'navigationId': 'hvbackup.home', 'uri': 'global.html', 'navigationVisible': True}},
        'objects': {
            'VirtualMachine': {
                'summary': {'view': {'uri': 'vm-summary.html', 'icon': {'name': 'main'}}},
                'monitor': {'views': [{'navigationId': 'hvbackup.vm.monitor', 'labelKey': 'vm.monitor.title', 'uri': 'vm-monitor.html'}]},
                'menu': {'actions': [
                    {'id': 'hvbackup.backupNow', 'labelKey': 'vm.action.backup', 'icon': {'name': 'main'}, 'acceptsMultipleTargets': True,
                     'trigger': {'type': 'modal', 'uri': 'backup-now.html', 'titleKey': 'vm.action.backup.title', 'size': {'width': 560, 'height': 300}}},
                    {'id': 'hvbackup.restore', 'labelKey': 'vm.action.restore', 'icon': {'name': 'main'},
                     'trigger': {'type': 'modal', 'uri': 'restore.html', 'titleKey': 'vm.action.restore.title', 'size': {'width': 640, 'height': 520}}},
                ]},
            },
        },
        'definitions': {
            'iconSpriteSheet': {'uri': 'sprite.png', 'definitions': {'main': {'x': 0, 'y': 0}}},
            'i18n': {'locales': ['en-US'], 'definitions': {
                'plugin.name': {'en-US': 'hvbackup'},
                'vm.monitor.title': {'en-US': 'Backups'},
                'vm.action.backup': {'en-US': 'Back up now with hvbackup'},
                'vm.action.backup.title': {'en-US': 'Back up now'},
                'vm.action.restore': {'en-US': 'Restore with hvbackup'},
                'vm.action.restore.title': {'en-US': 'Restore from hvbackup'},
            }},
        },
    }


@r.get('/vsphere-plugin/plugin.json')
def plugin_json():
    return JSONResponse(manifest(), headers={'Cache-Control': 'no-cache'})


@r.get('/vsphere-plugin/{page}.html', response_class=HTMLResponse)
def page(page: str):
    if page not in PAGES:
        raise HTTPException(404)
    with open(os.path.join(STATIC, page + '.html')) as f:
        html = f.read()
    return HTMLResponse(html.replace('__HVB_VERSION__', APP_VERSION).replace('__HVB_PUBLIC_URL__', config.PUBLIC_URL or ''))


@r.get('/vsphere-plugin/{asset}')
def asset(asset: str):
    if not re.fullmatch(r'[a-z0-9_-]+\.(js|css|png|svg)', asset):
        raise HTTPException(404)
    p = os.path.join(STATIC, asset)
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, headers={'Cache-Control': 'no-cache'})


# ── session exchange ─────────────────────────────────────────────────
class Exchange(BaseModel):
    session_token: str
    vc_guid: str | None = None
    object_ids: list[str] = []


@r.post('/api/v1/auth/vsphere')
def exchange(body: Exchange, request: Request):
    """vSphere Client session token -> hvbackup bearer token (1 hour) for the plug-in pages."""
    conn_row = _connection_for_guid(body.vc_guid)
    if conn_row is None:
        raise HTTPException(400, 'no hvbackup connection matches this vCenter; add it under Connections first')
    try:
        vc_user, _ = vsp.user_from_session_token(conn_row['host'], body.session_token, body.vc_guid or conn_row['info'].get('instance_uuid'),
                                                 verify=bool(conn_row['verify_ssl']))
    except PermissionError as e:
        raise HTTPException(401, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f'session exchange with vCenter failed: {type(e).__name__}: {e}')
    user = None
    for cand in vsp.map_username(vc_user):
        user = auth.get_user(cand)
        if user and not user['disabled']:
            break
        user = None
    if user is None:
        db.audit(vc_user, 'vsphere_login_denied', 'no matching hvbackup account')
        raise HTTPException(403, f'vSphere user {vc_user} has no hvbackup account; create one with the same username (or set HVB_VSPHERE_USER_MAP)')
    token = auth.make_plugin_token(user['username'])
    db.audit(user['username'], 'vsphere_login', vc_user)
    return {'token': token, 'username': user['username'], 'role': user['role'], 'vc_user': vc_user, 'connection_id': conn_row['id'],
            'expires_in': auth.PLUGIN_TOKEN_TTL}


def _connection_for_guid(guid):
    with db.conn() as c:
        rows = [db.row_to_dict(x) for x in c.execute("SELECT * FROM connections WHERE type IN ('vcenter','esxi')")]
    for row in rows:
        row['info'] = row.get('info') or {}
    if guid:
        for row in rows:
            if (row['info'].get('instance_uuid') or '').lower() == guid.lower():
                return row
    return rows[0] if len(rows) == 1 else None


@r.get('/api/v1/vsphere/resolve')
def resolve(object_id: str, request: Request):
    """urn:vmomi:VirtualMachine:vm-123:<vc guid> -> hvbackup vm row (authenticated like any API call)."""
    from .deps import current_user
    current_user(request)
    m = re.fullmatch(r'urn:vmomi:VirtualMachine:(vm-\d+):([0-9a-fA-F-]+)', object_id)
    if not m:
        raise HTTPException(400, 'not a VirtualMachine object id')
    moref, guid = m.group(1), m.group(2)
    conn_row = _connection_for_guid(guid)
    if conn_row is None:
        raise HTTPException(404, 'no connection for this vCenter')
    with db.conn() as c:
        vm = db.row_to_dict(c.execute('SELECT id,name,moref,power_state,cbt_enabled,tools FROM vms WHERE connection_id=? AND moref=? AND gone=0',
                                      (conn_row['id'], moref)).fetchone())
    if not vm:
        raise HTTPException(404, 'VM not in hvbackup inventory yet; refresh the connection')
    return vm
