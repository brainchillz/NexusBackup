"""vSphere Client remote plug-in: registration with vCenter's ExtensionManager, the
session-delegation exchange (vSphere Client session token -> clone ticket -> SOAP
session -> user name), and mirroring of hvbackup runs into vCenter's Recent Tasks.

Facts this relies on (vSphere Client SDK 8/9 remote plug-in guide):
  * client[0].type must be 'vsphere-client-remote' with url = manifest URL;
    server[0] (type MANIFEST_SERVER) must carry the same url and the SHA-256
    thumbprint (and ideally the PEM certificate) of the manifest server.
  * The plug-in UI calls app.getSessionInfo() for a token and POSTs it to us; we
    POST /api/ui/vcenter/session/clone-ticket on vCenter with that token in the
    vmware-api-session-id header and {"vc_guid": ...} as body, then
    SessionManager.CloneSession(ticket) gives us a session as that user.
"""
import datetime
import hashlib
import logging
import ssl
import time

import requests
from pyVim import connect
from pyVmomi import vim, vmodl

from .. import APP_VERSION, config

log = logging.getLogger('hvbackup.vsphere_plugin')
KEY = 'net.onthenile.hvbackup'
COMPANY = 'OTNOPS'
TASK_BACKUP = KEY + '.backup'
TASK_RESTORE = KEY + '.restore'
EVENT_FAILED = KEY + '.backup.failed'
EVENT_DONE = KEY + '.backup.done'


def manifest_url(public_url):
    return public_url.rstrip('/') + '/vsphere-plugin/plugin.json'


def cert_thumbprint_and_pem():
    crt, _ = _cert_paths()
    pem = open(crt, 'rb').read()
    der = ssl.PEM_cert_to_DER_cert(pem.decode())
    h = hashlib.sha256(der).hexdigest().upper()
    return ':'.join(h[i:i + 2] for i in range(0, len(h), 2)), pem.decode()


def _cert_paths():
    from ..core import tls
    return tls.ensure_cert()


def build_extension(public_url, existing=None):
    thumb, pem = cert_thumbprint_and_pem()
    url = manifest_url(public_url)
    desc = vim.Description(label='hvbackup', summary='Hypervisor backup: back up and restore VMs from the vSphere Client')
    ext = existing or vim.Extension()
    ext.key = KEY
    ext.version = APP_VERSION
    ext.company = COMPANY
    ext.description = desc
    ext.lastHeartbeatTime = datetime.datetime.now(datetime.timezone.utc)
    ext.shownInSolutionManager = False
    ext.client = [vim.Extension.ClientInfo(version=APP_VERSION, description=desc, company=COMPANY, type='vsphere-client-remote', url=url)]
    srv = vim.Extension.ServerInfo(url=url, description=desc, company=COMPANY, type='MANIFEST_SERVER', adminEmail=['root@localhost'],
                                   serverThumbprint=thumb)
    try:
        srv.serverCertificate = pem
    except Exception:  # noqa: BLE001  older vCenter without the field
        pass
    ext.server = [srv]
    ext.taskList = [vim.Extension.TaskTypeInfo(taskID=TASK_BACKUP), vim.Extension.TaskTypeInfo(taskID=TASK_RESTORE)]
    ext.eventList = [vim.Extension.EventTypeInfo(eventID=EVENT_FAILED), vim.Extension.EventTypeInfo(eventID=EVENT_DONE)]
    ext.resourceList = [vim.Extension.ResourceInfo(locale='en', module='task', data=[
        vim.KeyValue(key=TASK_BACKUP + '.label', value='hvbackup backup'),
        vim.KeyValue(key=TASK_BACKUP + '.summary', value='Back up the virtual machine with hvbackup'),
        vim.KeyValue(key=TASK_RESTORE + '.label', value='hvbackup restore'),
        vim.KeyValue(key=TASK_RESTORE + '.summary', value='Restore a virtual machine with hvbackup'),
    ]), vim.Extension.ResourceInfo(locale='en', module='event', data=[
        vim.KeyValue(key=EVENT_FAILED + '.category', value='error'),
        vim.KeyValue(key=EVENT_FAILED + '.fullFormat', value='hvbackup backup of {vm.name} failed: {reason}'),
        vim.KeyValue(key=EVENT_FAILED + '.formatOnVm', value='hvbackup backup failed: {reason}'),
        vim.KeyValue(key=EVENT_DONE + '.category', value='info'),
        vim.KeyValue(key=EVENT_DONE + '.fullFormat', value='hvbackup backup of {vm.name} completed ({kind})'),
        vim.KeyValue(key=EVENT_DONE + '.formatOnVm', value='hvbackup backup completed ({kind})'),
    ])]
    return ext


def register(api, public_url):
    """Register or update the plug-in on the vCenter behind `api` (a connected VMware object)."""
    em = api.content.extensionManager
    existing = em.FindExtension(KEY)
    ext = build_extension(public_url, existing)
    if existing:
        em.UpdateExtension(ext)
        action = 'updated'
    else:
        em.RegisterExtension(ext)
        action = 'registered'
    log.info('vSphere plug-in %s on %s (%s)', action, api.host, manifest_url(public_url))
    return {'action': action, 'key': KEY, 'version': APP_VERSION, 'manifest': manifest_url(public_url)}


def unregister(api):
    em = api.content.extensionManager
    if em.FindExtension(KEY):
        em.UnregisterExtension(KEY)
        return {'action': 'unregistered', 'key': KEY}
    return {'action': 'not registered', 'key': KEY}


def status(api):
    ext = api.content.extensionManager.FindExtension(KEY)
    if not ext:
        return {'registered': False, 'key': KEY}
    return {'registered': True, 'key': KEY, 'version': ext.version, 'manifest': ext.client[0].url if ext.client else None,
            'thumbprint': ext.server[0].serverThumbprint if ext.server else None}


# ── session delegation ───────────────────────────────────────────────
def user_from_session_token(vc_host, session_token, vc_guid, verify=False):
    """vSphere Client session token -> clone ticket -> SOAP session -> (user name, session key)."""
    r = requests.post(f'https://{vc_host}/api/ui/vcenter/session/clone-ticket', json={'vc_guid': vc_guid} if vc_guid else {},
                      headers={'vmware-api-session-id': session_token, 'Content-Type': 'application/json', 'Accept': 'application/json'},
                      verify=verify, timeout=30)
    if r.status_code >= 400:
        raise PermissionError(f'vCenter refused the session token: HTTP {r.status_code} {r.text[:200]}')
    body = r.json() if r.content else {}
    ticket = body if isinstance(body, str) else body.get('ticket') or body.get('clone_ticket') or body.get('value') or next(iter(body.values()), None)
    if not ticket:
        raise PermissionError(f'no clone ticket in vCenter response: {r.text[:200]}')
    ctx = None if verify else ssl._create_unverified_context()
    stub = connect.SmartStubAdapter(host=vc_host, port=443, sslContext=ctx, connectionPoolTimeout=0)
    si = vim.ServiceInstance('ServiceInstance', stub)
    sm = si.content.sessionManager
    session = sm.CloneSession(cloneTicket=ticket)
    try:
        return session.userName, session.key
    finally:
        try:
            sm.Logout()
        except Exception:  # noqa: BLE001
            pass


def map_username(vc_user):
    """'DOMAIN\\user', 'user@domain' or 'user' -> candidate hvbackup usernames, most specific first.
    HVB_VSPHERE_USER_MAP="administrator@vsphere.local=admin,other@dom=bob" overrides the default
    of stripping the domain."""
    u = vc_user.strip()
    if '\\' in u:
        dom, name = u.split('\\', 1)
        forms = {u.lower(), f'{name}@{dom}'.lower()}
        bare = name
    elif '@' in u:
        forms = {u.lower()}
        bare = u.split('@', 1)[0]
    else:
        forms = {u.lower()}
        bare = u
    mapping = config.get('HVB_VSPHERE_USER_MAP', '') or ''
    for pair in mapping.split(','):
        if '=' in pair:
            src, dst = pair.split('=', 1)
            if src.strip().lower() in forms:
                return [dst.strip()]
    return [bare.lower()]


# ── vCenter task mirroring ───────────────────────────────────────────
class VcTask:
    """Best effort mirror of an hvbackup run into vCenter's Recent Tasks. Silent if the
    plug-in is not registered or the account lacks Task.Create."""

    def __init__(self, api, vm, task_type, initiated_by='hvbackup'):
        self.task = None
        try:
            info = api.content.taskManager.CreateTask(obj=vm, taskTypeId=task_type, initiatedBy=initiated_by, cancelable=False)
            self.task = info.task
            self.task.SetState(state=vim.TaskInfo.State.running, result=None, fault=None)
        except Exception as e:  # noqa: BLE001
            log.debug('vCenter task mirror unavailable: %s', e)

    def progress(self, pct):
        if self.task is not None:
            try:
                self.task.UpdateProgress(int(max(0, min(100, pct))))
            except Exception:  # noqa: BLE001
                pass

    def done(self, result=None):
        if self.task is not None:
            try:
                self.task.SetState(state=vim.TaskInfo.State.success, result=result, fault=None)
            except Exception:  # noqa: BLE001
                pass

    def failed(self, message):
        if self.task is not None:
            try:
                fault = vmodl.fault.SystemError(reason=str(message)[:500])
                self.task.SetState(state=vim.TaskInfo.State.error, result=None, fault=fault)
            except Exception:  # noqa: BLE001
                pass


def post_event(api, vm, event_id, message, kind=''):
    """Custom event on the VM so admins can hang vCenter alarms on hvbackup results."""
    try:
        ev = vim.event.ExtendedEvent()
        ev.eventTypeId = event_id
        ev.managedObject = vm
        ev.createdTime = datetime.datetime.now(datetime.timezone.utc)
        ev.userName = 'hvbackup'
        ev.fullFormattedMessage = message
        ev.data = [vim.event.ExtendedEvent.Pair(key='reason', value=message), vim.event.ExtendedEvent.Pair(key='kind', value=kind)]
        ev.vm = vim.event.VmEventArgument(vm=vm, name=vm.name)
        api.content.eventManager.PostEvent(eventToPost=ev)
    except Exception as e:  # noqa: BLE001
        log.debug('vCenter event post unavailable: %s', e)
