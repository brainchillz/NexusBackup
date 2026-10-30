"""Files a Windows conversion needs, kept in <data>/windows-tools/ and fetched on first
use (or uploaded in Settings > Windows conversion tools):

  virtio-win.iso   Red Hat's virtio drivers for Windows and the QEMU guest agent;
                   virt-v2v injects the storage and network drivers before first boot
                   (VMware -> Proxmox)
  rhsrvany.exe     a tiny GPL service wrapper (github.com/rwmjones/rhsrvany) that
                   libguestfs uses to run first-boot scripts in Windows guests: driver
                   registration and the guest agent towards Proxmox, the VMware Tools
                   install towards VMware; required by virt-v2v for every Windows guest
  pnp_wait.exe     from the same project, optional: waits for new devices at first boot

virt-v2v finds them through VIRTIO_WIN and VIRT_TOOLS_DATA_DIR (env()). The project's CI
builds the two executables from the upstream source and publishes them as release
assets; the default URL points there."""
import hashlib
import logging
import os

import requests

from .. import config

log = logging.getLogger('hvbackup.windows_tools')
DIR = os.path.join(config.DATA_DIR, 'windows-tools')
DEFAULT_VIRTIO_WIN_URL = 'https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-virtio/virtio-win.iso'
DEFAULT_TOOLS_URL = 'https://github.com/brainchillz/NexusBackup/releases/download/windows-tools-1'
FILES = {
    'virtio-win.iso': {'setting': 'virtio_win_url', 'required': True, 'what': 'virtio drivers and the QEMU guest agent (VMware to Proxmox)'},
    'rhsrvany.exe': {'setting': 'windows_tools_url', 'required': True, 'what': 'first-boot service wrapper (both directions)'},
    'pnp_wait.exe': {'setting': 'windows_tools_url', 'required': False, 'what': 'waits for new devices at first boot (optional)'},
}
REQUIRED = tuple(n for n, f in FILES.items() if f['required'])


def path(name):
    return os.path.join(DIR, name)


def url_for(name):
    from . import settings
    base = settings.get(FILES[name]['setting']) or ''
    if name == 'virtio-win.iso':
        return base
    return base.rstrip('/') + '/' + name if base else ''


def status():
    out = {}
    for name, f in FILES.items():
        p = path(name)
        present = os.path.exists(p) and os.path.getsize(p) > 0
        out[name] = {'present': present, 'size': os.path.getsize(p) if present else None, 'path': p, 'url': url_for(name),
                     'required': f['required'], 'what': f['what']}
    missing = [n for n in REQUIRED if not out[n]['present']]
    fetchable = [n for n in missing if out[n]['url']]
    return {'files': out, 'ready': not missing, 'missing': missing, 'fetchable': fetchable, 'directory': DIR}


def _sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for buf in iter(lambda: f.read(8 << 20), b''):
            h.update(buf)
    return h.hexdigest()


def download(name, task=None):
    url = url_for(name)
    if not url:
        raise RuntimeError(f'{name}: no download URL (Settings > Windows conversion tools), and the file is not uploaded')
    os.makedirs(DIR, exist_ok=True)
    dest = path(name)
    if task:
        task.log(f'fetching {name} from {url}')
    with requests.get(url, stream=True, timeout=120, allow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get('content-length') or 0)
        done = 0
        with open(dest + '.part', 'wb') as f:
            for buf in r.iter_content(8 << 20):
                f.write(buf)
                done += len(buf)
                if task:
                    task.check_cancel()
                    if total:
                        task.progress(min(done / total, 0.99) * 0.05, f'downloading {name} ({done / 2**20:.0f} of {total / 2**20:.0f} MiB)')
    if name == 'virtio-win.iso':
        from . import settings
        want = (settings.get('virtio_win_sha256') or '').lower()
        if want:
            got = _sha256(dest + '.part')
            if got != want:
                os.unlink(dest + '.part')
                raise RuntimeError(f'{name}: SHA-256 {got} does not match the expected {want}')
    os.replace(dest + '.part', dest)
    log.info('%s fetched to %s (%d bytes)', name, dest, os.path.getsize(dest))
    return dest


def ensure(task, names=REQUIRED):
    """Make sure the named files are present, downloading the missing ones."""
    for name in names:
        if not (os.path.exists(path(name)) and os.path.getsize(path(name)) > 0):
            download(name, task)
    for name in FILES:  # the optional ones too when they can be had, quietly
        if name not in names and not os.path.exists(path(name)) and url_for(name):
            try:
                download(name, task)
            except Exception as e:  # noqa: BLE001
                if task:
                    task.log(f'{name} not fetched ({e}); continuing without it')


def save_upload(name, fileobj):
    if name not in FILES:
        raise ValueError(f'unknown file {name}')
    os.makedirs(DIR, exist_ok=True)
    dest = path(name)
    import shutil
    with open(dest + '.part', 'wb') as f:
        shutil.copyfileobj(fileobj, f, 8 << 20)
    if os.path.getsize(dest + '.part') == 0:
        os.unlink(dest + '.part')
        raise ValueError('the upload is empty')
    os.replace(dest + '.part', dest)
    return status()


def remove(name):
    if name not in FILES:
        raise ValueError(f'unknown file {name}')
    try:
        os.unlink(path(name))
    except FileNotFoundError:
        pass
    return status()


def env(base):
    """Environment additions for virt-v2v / virt-customize on a Windows guest."""
    e = dict(base)
    e['VIRTIO_WIN'] = path('virtio-win.iso')
    e['VIRT_TOOLS_DATA_DIR'] = DIR
    return e
