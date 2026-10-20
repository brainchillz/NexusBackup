"""Server-driven proxy VM deployment.

The admin picks a connection and a place on it; the server fetches a stock cloud image
(Ubuntu 24.04 by default, cached under HVB_DATA_DIR/images), imports it as a VM with
cloud-init first-boot data carried in the OVF properties, powers it on and waits for the
agent to report in. The first-boot data holds the proxy token and installs the agent from
this server, so the only things the proxy VM needs are DHCP and reach to apt/PyPI and to
this server. The secrets are blanked from the VM's properties once the agent is online.
"""
import base64
import hashlib
import logging
import os
import threading
import time
import urllib.parse

import requests

from .. import config, db
from ..core import auth
from ..core.tasks import Cancelled
from ..providers import vmware
from . import ops

log = logging.getLogger('hvbackup.proxy_deploy')
_image_lock = threading.Lock()
SECRET_PROPERTIES = ('user-data', 'password', 'public-keys')
IMAGE_DISK_BYTES = 10 * 2**30
AGENT_DEPS = 'blake3 requests zstandard urllib3'


# ── image ────────────────────────────────────────────────────────────
def image_status():
    if config.PROXY_IMAGE:
        return {'url': None, 'path': config.PROXY_IMAGE, 'cached': os.path.exists(config.PROXY_IMAGE),
                'size': os.path.getsize(config.PROXY_IMAGE) if os.path.exists(config.PROXY_IMAGE) else None, 'local': True}
    path = _cache_path()
    return {'url': config.PROXY_IMAGE_URL, 'path': path, 'cached': os.path.exists(path),
            'size': os.path.getsize(path) if os.path.exists(path) else None, 'local': False}


def _cache_path():
    name = os.path.basename(urllib.parse.urlparse(config.PROXY_IMAGE_URL).path) or 'proxy-image.ova'
    return os.path.join(config.IMAGE_DIR, name)


def _expected_sha256(task):
    if config.PROXY_IMAGE_SHA256:
        return config.PROXY_IMAGE_SHA256.lower()
    base = config.PROXY_IMAGE_URL.rsplit('/', 1)[0] + '/SHA256SUMS'
    name = os.path.basename(urllib.parse.urlparse(config.PROXY_IMAGE_URL).path)
    try:
        r = requests.get(base, timeout=30)
        if r.ok:
            for line in r.text.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip('*') == name:
                    return parts[0].lower()
    except requests.RequestException as e:
        task.log(f'no checksum list at {base} ({e}); the download is not verified')
    return None


def _sha256(path, task=None):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for buf in iter(lambda: f.read(8 << 20), b''):
            h.update(buf)
            if task:
                task.check_cancel()
    return h.hexdigest()


def ensure_image(task):
    """Path of the OVA to deploy, downloading and verifying it once."""
    if config.PROXY_IMAGE:
        if not os.path.exists(config.PROXY_IMAGE):
            raise RuntimeError(f'HVB_PROXY_IMAGE {config.PROXY_IMAGE} does not exist')
        task.log(f'using local image {config.PROXY_IMAGE}')
        return config.PROXY_IMAGE
    path = _cache_path()
    with _image_lock:
        want = _expected_sha256(task)
        if os.path.exists(path):
            if want is None or _sha256(path, task) == want:
                task.log(f'image {os.path.basename(path)} is cached')
                return path
            task.log('cached image does not match the published checksum; downloading again')
            os.unlink(path)
        os.makedirs(config.IMAGE_DIR, exist_ok=True)
        task.log(f'downloading {config.PROXY_IMAGE_URL}')
        part = path + '.part'
        with requests.get(config.PROXY_IMAGE_URL, stream=True, timeout=120) as r:
            r.raise_for_status()
            total = int(r.headers.get('Content-Length') or 0)
            done = 0
            h = hashlib.sha256()
            with open(part, 'wb') as f:
                for buf in r.iter_content(4 << 20):
                    f.write(buf)
                    h.update(buf)
                    done += len(buf)
                    task.progress(0.1 * done / total if total else 0.05, f'downloading image: {done / 2**20:.0f}{" of " + str(total // 2**20) if total else ""} MiB')
                    task.check_cancel()
        if want and h.hexdigest() != want:
            os.unlink(part)
            raise RuntimeError('downloaded image does not match the published SHA256; not deployed')
        os.replace(part, path)
        task.log(f'image stored at {path} ({done / 2**20:.0f} MiB{", checksum verified" if want else ""})')
        return path


# ── first-boot data ──────────────────────────────────────────────────
def cloud_config(name, server_url, token, insecure, ssh_key=None):
    conf = f'HVB_URL={server_url}\nHVB_PROXY_TOKEN={token}\nHVB_INSECURE={"1" if insecure else "0"}\n'
    unit = ('[Unit]\nDescription=hvbackup proxy agent (deployed by the backup server)\nAfter=network-online.target\nWants=network-online.target\n\n'
            '[Service]\nType=simple\nUser=root\nEnvironmentFile=-/etc/hvbackup/agent.conf\n'
            'ExecStart=/opt/hvbackup/venv/bin/python /opt/hvbackup/proxy_agent.py\nRestart=always\nRestartSec=5\n\n'
            '[Install]\nWantedBy=multi-user.target\n')
    bootstrap = f'''#!/bin/bash
# hvbackup proxy bootstrap: runs once from cloud-init. Log: /var/log/hvbackup-bootstrap.log
set -u
exec >>/var/log/hvbackup-bootstrap.log 2>&1
echo "== $(date -Is) bootstrap start"
. /etc/hvbackup/agent.conf
K=""; [ "${{HVB_INSECURE:-0}}" = 1 ] && K="-k"
for i in $(seq 1 30); do
  if [ -x /opt/hvbackup/venv/bin/python ] || python3 -m venv /opt/hvbackup/venv; then
    /opt/hvbackup/venv/bin/pip install -q --upgrade pip >/dev/null 2>&1
    /opt/hvbackup/venv/bin/pip install -q {AGENT_DEPS} && break
  fi
  echo "pip/venv attempt $i failed; retrying in 20s"; sleep 20
done
for i in $(seq 1 60); do
  curl -fsS $K "$HVB_URL/api/v1/agent/files/proxy_agent.py" -o /opt/hvbackup/proxy_agent.py && break
  echo "cannot fetch the agent from $HVB_URL (attempt $i); retrying in 10s"; sleep 10
done
systemctl daemon-reload
systemctl enable --now hvbackup-agent
echo "== $(date -Is) bootstrap done"
'''
    users = ''
    if ssh_key:
        users = ('users:\n  - name: ubuntu\n    sudo: ALL=(ALL) NOPASSWD:ALL\n    shell: /bin/bash\n    lock_passwd: true\n'
                 '    ssh_authorized_keys:\n' + ''.join(f'      - {k.strip()}\n' for k in ssh_key.splitlines() if k.strip()))

    def block(text, indent='      '):
        return ''.join(indent + line + '\n' for line in text.splitlines())

    return (f'#cloud-config\nhostname: {name}\nmanage_etc_hosts: true\n{users}'
            'package_update: true\npackages:\n  - python3-venv\n  - curl\n'
            'write_files:\n'
            f'  - path: /etc/hvbackup/agent.conf\n    permissions: "0600"\n    content: |\n{block(conf)}'
            f'  - path: /etc/systemd/system/hvbackup-agent.service\n    permissions: "0644"\n    content: |\n{block(unit)}'
            f'  - path: /opt/hvbackup/bootstrap.sh\n    permissions: "0755"\n    content: |\n{block(bootstrap)}'
            'runcmd:\n  - [bash, /opt/hvbackup/bootstrap.sh]\n')


# ── the task ─────────────────────────────────────────────────────────
def deploy_proxy(task, options):
    """options: name, connection_id, server_url, host, datastore, folder, network, cpus,
    memory_mb, ssh_key, timeout_minutes."""
    name = options['name']
    cr = ops.connection(options['connection_id'])
    if cr['type'] not in ('vcenter', 'esxi'):
        raise RuntimeError(f'proxy VMs are deployed on vSphere connections only, not {cr["type"]}')
    server_url = options['server_url'].rstrip('/')
    insecure = not (config.TLS_CERT and config.TLS_KEY)
    task.log(f'deploying proxy {name} on {cr["name"]} ({cr["host"]}); the agent will call back to {server_url}'
             f'{" without certificate verification (self-signed server certificate)" if insecure else ""}')
    ova = ensure_image(task)

    proxy_id, token = auth.create_proxy(name, cr['id'])
    with db.conn() as c:
        c.execute('UPDATE proxies SET deploy_task_id=?, vm_name=? WHERE id=?', (task.id, name, proxy_id))
    vm = None
    try:
        with ops.open_api(cr) as api:
            for r in api.collect(vmware.vim.VirtualMachine, ['name']):
                if r['name'] == name:
                    raise RuntimeError(f'a VM named {name!r} already exists on this connection')
            spec = {'placement': {}, 'disks': [{'datastore': ''}], 'nics': [{'network': options.get('network') or ''}]}
            manifest = {'disks': [{'allocated': IMAGE_DISK_BYTES}]}
            target = vmware.resolve_target(api, spec, manifest, {'host': options.get('host'), 'datastore': options.get('datastore'),
                                                                   'folder': options.get('folder'), 'networks': {}}, task.log)
            net = next(iter(target['networks'].values()), None)
            task.log(f'placing on {target["host"].name} / {target["datastore"].name}, network {net.name if net else "(none)"}')
            task.progress(0.1, 'importing image')
            user_data = cloud_config(name, server_url, token, insecure, options.get('ssh_key'))
            props = {'instance-id': f'hvbackup-proxy-{proxy_id}', 'hostname': name,
                     'user-data': base64.b64encode(user_data.encode()).decode()}
            if options.get('ssh_key'):
                props['public-keys'] = options['ssh_key'].strip()
            vm = vmware.deploy_ova(task, api, ova, name, target, props, int(options.get('cpus') or 2), int(options.get('memory_mb') or 2048),
                                   annotation=f'hvbackup proxy for connection {cr["name"]}, deployed by the backup server')
            with db.conn() as c:
                c.execute('UPDATE proxies SET vm_moref=?, bios_uuid=? WHERE id=?', (vm._moId, vm.config.uuid, proxy_id))
            task.progress(0.75, 'powering on')
            vmware.wait_task(vm.PowerOnVM_Task(), 'power on')
            task.log('powered on; waiting for cloud-init to install the agent (packages come from apt and PyPI, the agent from this server)')
            started = time.time()
            deadline = started + 60 * int(options.get('timeout_minutes') or 20)
            last_note = 0
            while True:
                task.check_cancel()
                with db.conn() as c:
                    seen = c.execute('SELECT last_seen FROM proxies WHERE id=?', (proxy_id,)).fetchone()[0]
                if seen and seen >= started:
                    break
                if time.time() > deadline:
                    raise RuntimeError(f'the agent has not reported in after {int(options.get("timeout_minutes") or 20)} min; the VM is left running: '
                                       f'open its console and read /var/log/hvbackup-bootstrap.log and /var/log/cloud-init-output.log')
                if time.time() - last_note > 30:
                    g = vmware.guest_state(vm)
                    task.log(f'waiting: tools {g["tools"]}, guest IP {g["ip"] or "not yet"}')
                    task.progress(0.75 + 0.2 * min(1, (time.time() - started) / 600), f'waiting for the agent (guest IP {g["ip"] or "not yet"})')
                    last_note = time.time()
                time.sleep(5)
            g = vmware.guest_state(vm)
            with db.conn() as c:
                g['ip'] = g['ip'] or c.execute('SELECT address FROM proxies WHERE id=?', (proxy_id,)).fetchone()[0]
            task.log(f'agent online from {g["ip"] or "?"} after {time.time() - started:.0f}s')
            try:
                vmware.wipe_vapp_properties(vm, SECRET_PROPERTIES)
                task.log('first-boot secrets removed from the VM properties')
            except Exception as e:  # noqa: BLE001
                task.log(f'WARNING: could not blank the OVF properties (they hold the proxy token): {e}')
            task.progress(1, f'proxy {name} online')
            return {'proxy_id': proxy_id, 'moref': vm._moId, 'ip': g['ip'], 'name': name}
    except BaseException as e:
        if vm is None:
            with db.conn() as c:
                c.execute('DELETE FROM proxies WHERE id=?', (proxy_id,))
            task.log('nothing was created; the proxy record is removed')
        elif isinstance(e, Cancelled):
            task.log(f'cancelled; VM {name} is left as it is, proxy record {proxy_id} kept')
        else:
            task.log(f'VM {name} is left for inspection; delete it and proxy record {proxy_id} to retry under the same name')
        raise
