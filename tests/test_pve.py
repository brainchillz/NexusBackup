"""Proxmox provider: config parsing, archive names, and the node helper's NBD server."""
import importlib.util
import os
import socket
import subprocess
import tempfile
import threading

import pytest

from hvbackup.providers import proxmox

QM_CONFIG = {
    'agent': 'enabled=1', 'boot': 'order=scsi0', 'cores': 2, 'cpu': 'x86-64-v2-AES', 'ide2': 'sata_ssd:vm-9000-cloudinit,media=cdrom',
    'memory': '2048', 'name': 'ubuntu-2404-template', 'net0': 'virtio=BC:24:11:19:4E:46,bridge=vmbr0', 'ostype': 'l26',
    'scsi0': 'sata_ssd:base-9000-disk-0,discard=on,size=20G,ssd=1', 'scsihw': 'virtio-scsi-single',
    'smbios1': 'uuid=731ab89e-28d0-4c58-809e-afd7224f5fe7', 'bios': 'ovmf', 'efidisk0': 'sata_ssd:vm-9000-efi,efitype=4m,pre-enrolled-keys=1,size=4M',
}


def test_parse_size_and_kv():
    assert proxmox.parse_size('20G') == 20 * 2**30
    assert proxmox.parse_size('4M') == 4 * 2**20
    assert proxmox.parse_size('1.5T') == int(1.5 * 2**40)
    assert proxmox.parse_kv('sata_ssd:base-9000-disk-0,discard=on,size=20G') == ('sata_ssd:base-9000-disk-0', {'discard': 'on', 'size': '20G'})
    assert proxmox.parse_kv('virtio=BC:24,bridge=vmbr0') == ('', {'virtio': 'BC:24', 'bridge': 'vmbr0'})


def test_volname_roundtrip():
    assert proxmox.volname_for('qemu', 100, 12) == 'backup/vm/100/12'
    assert proxmox.volname_for('lxc', 101, 13) == 'backup/ct/101/13'
    assert proxmox.volname_parts('backup/vm/100/12') == ('qemu', 100, 12)
    assert proxmox.volname_parts('backup/ct/101/13') == ('lxc', 101, 13)
    with pytest.raises(ValueError):
        proxmox.volname_parts('backup/vm/x/1')


def test_spec_from_qemu_config():
    api = proxmox.Proxmox('192.0.2.1', 'root@pam', 'x')
    api.cluster = 'lab'
    spec = api.spec_from_qemu(9000, 'pve-01', QM_CONFIG, {'pool': '', 'tags': 'template'})
    assert spec['name'] == 'ubuntu-2404-template' and spec['cpus'] == 2 and spec['memory_mb'] == 2048
    assert spec['firmware'] == 'efi' and spec['secure_boot'] is True and spec['tpm'] is False
    assert spec['uuid'] == '731ab89e-28d0-4c58-809e-afd7224f5fe7' and spec['tools'] == 'agent'
    keys = [d['key'] for d in spec['disks']]
    assert keys == ['efidisk0', 'scsi0']  # the cloud-init cdrom is skipped
    scsi0 = next(d for d in spec['disks'] if d['key'] == 'scsi0')
    assert scsi0['capacity'] == 20 * 2**30 and scsi0['datastore'] == 'sata_ssd' and scsi0['controller'] == 'virtio-scsi-single'
    assert spec['nics'] == [{'key': 'net0', 'mac': 'BC:24:11:19:4E:46', 'model': 'virtio', 'network': 'vmbr0', 'vlan': '', 'connected': True, 'name': ''}]
    assert spec['placement']['host'] == 'pve-01' and spec['placement']['datastores'] == ['sata_ssd'] and spec['vmtype'] == 'qemu'


def test_spec_from_lxc_config():
    api = proxmox.Proxmox('192.0.2.1:8006', 'root@pam!tok', 'secret')
    api.cluster = 'lab'
    cfg = {'arch': 'amd64', 'cores': 1, 'hostname': 'ct1', 'memory': 512, 'net0': 'name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:CC,ip=dhcp,type=veth',
           'ostype': 'debian', 'rootfs': 'local-lvm:vm-101-disk-0,size=8G', 'mp0': 'local-lvm:vm-101-disk-1,mp=/data,size=2G', 'unprivileged': 1}
    spec = api.spec_from_lxc(101, 'pve-02', cfg, {})
    assert spec['name'] == 'ct1' and spec['vmtype'] == 'lxc' and spec['uuid'] == 'pve-lab-lxc-101'
    assert [(d['key'], d['capacity'], d['mountpoint']) for d in spec['disks']] == [('mp0', 2 * 2**30, '/data'), ('rootfs', 8 * 2**30, '/')]
    assert spec['nics'][0]['mac'] == 'BC:24:11:AA:BB:CC' and spec['nics'][0]['network'] == 'vmbr0' and spec['nics'][0]['model'] == 'veth'
    assert api.port == 8006 and api.hostname == '192.0.2.1'


# ── the node helper's NBD server, read back with qemu-img ───────────
def _load_helper():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hvbackup', 'pve', 'pve_helper.py')
    spec = importlib.util.spec_from_file_location('pve_helper', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not os.path.exists('/usr/bin/qemu-img'), reason='qemu-img not installed')
def test_nbd_server_serves_chunks_to_qemu_img(tmp_path):
    import blake3
    import zstandard
    helper = _load_helper()
    CH = helper.CHUNK
    size = 2 * CH + 12288  # a full chunk, a zero chunk, and a sector-aligned tail
    chunks = {}
    index = []
    content = bytearray(size)
    for i, filler in enumerate((b'A', None, b'C')):
        if filler is None:
            index.append(helper.ZERO_HASH.hex())
            continue
        n = min(CH, size - i * CH)
        data = (filler * n) + bytes(CH - n)
        h = blake3.blake3(data).hexdigest()
        chunks[h] = zstandard.ZstdCompressor().compress(data)
        index.append(h)
        content[i * CH:i * CH + n] = data[:n]

    class FakeServer:
        def call(self, method, path, **kw):
            assert method == 'GET' and path.startswith('/api/v1/agent/chunks/')
            return chunks[path.rsplit('/', 1)[1]]

    src = helper.ChunkSource(FakeServer(), index, size, prefetch=2, cache=4)
    sock = str(tmp_path / 'nbd.sock')
    ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ls.bind(sock)
    ls.listen(4)

    def serve():
        while True:
            try:
                c, _ = ls.accept()
            except OSError:
                return
            threading.Thread(target=helper._serve_one, args=(c, src, 'drive-scsi0'), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    uri = f'nbd+unix:///?socket={sock}'
    info = subprocess.run(['qemu-img', 'info', '--output=json', uri], capture_output=True, text=True, timeout=60)
    assert info.returncode == 0, info.stderr
    import json as _json
    assert _json.loads(info.stdout)['virtual-size'] == size
    out = tmp_path / 'out.raw'
    conv = subprocess.run(['qemu-img', 'convert', '-f', 'raw', '-O', 'raw', uri, str(out)], capture_output=True, text=True, timeout=120)
    assert conv.returncode == 0, conv.stderr
    assert out.read_bytes() == bytes(content)
    ls.close()
