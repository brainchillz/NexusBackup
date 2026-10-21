"""Conversion pieces that need no hypervisor: NBD export of a repository disk read back by
qemu-img and by our own client, overlay change mapping, spec translation."""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

from hvbackup.core import convert, nbd
from hvbackup.store import chunks

HAVE_QEMU = shutil.which('qemu-img') is not None


@pytest.fixture
def rep(tmp_path, monkeypatch):
    monkeypatch.setattr(chunks.config, 'REPO_DIR', str(tmp_path / 'repo'))
    monkeypatch.setattr(chunks.config, 'CHUNK_SIZE', 1 << 20)
    return chunks.Repository(str(tmp_path / 'repo')).open()


def test_nbd_export_roundtrip(rep, tmp_path):
    cs = rep.chunk_size
    blocks = [os.urandom(cs), bytes(cs), b'x' * cs]
    hashes = [rep.put_raw(b)[0] for b in blocks]
    src = nbd.IndexSource(rep, hashes, 3 * cs)
    sock = str(tmp_path / 'e.sock')
    with nbd.Export(src, sock, 'disk'):
        with nbd.Client(sock, 'disk') as cl:
            assert cl.size == 3 * cs
            assert cl.read(0, cs) == blocks[0]
            assert cl.read(cs + 5, 10) == bytes(10)
            assert cl.read(2 * cs + 1, 7) == b'x' * 7
            assert cl.read(cs - 3, 6) == blocks[0][-3:] + bytes(3)
        if HAVE_QEMU:
            ov = str(tmp_path / 'ov.qcow2')
            subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'raw', '-b', f'nbd+unix:///disk?socket={sock}', ov], check=True)
            info = json.loads(subprocess.check_output(['qemu-img', 'info', '--output=json', ov]))
            assert info['virtual-size'] == 3 * cs
            # write into the overlay, then the map shows exactly that chunk as changed
            subprocess.run(['qemu-io', '-f', 'qcow2', '-c', f'write -P 0x5a {2 * cs} 4096', ov], check=True, capture_output=True)
            m = json.loads(subprocess.check_output(['qemu-img', 'map', '--output=json', '-f', 'qcow2', ov]))
            assert convert.modified_chunks(m, cs, 3 * cs) == ([2], [])


def test_modified_chunks():
    m = [{'start': 0, 'length': 100, 'depth': 1}, {'start': 4096, 'length': 8192, 'depth': 0}, {'start': 5 << 20, 'length': 2 << 20, 'depth': 0}]
    assert convert.modified_chunks(m, 1 << 20, 10 << 20) == ([0, 5, 6], [])
    assert convert.modified_chunks(m, 1 << 20, 6 << 20) == ([0, 5], [])
    # discarded (zero) extents covering whole chunks need no read; a partly covered chunk does
    z = [{'start': 1 << 20, 'length': 2 << 20, 'depth': 0, 'zero': True, 'data': False},
         {'start': 3 << 20, 'length': 4096, 'depth': 0, 'zero': True, 'data': False},
         {'start': 4 << 20, 'length': 1 << 20, 'depth': 0, 'zero': True, 'data': True}]
    assert convert.modified_chunks(z, 1 << 20, 10 << 20) == ([3, 4], [1, 2])


def test_target_spec_and_pve_config():
    spec = {'platform': 'vmware', 'name': 'web 01', 'cpus': 4, 'cores_per_socket': 2, 'memory_mb': 4096, 'firmware': 'efi', 'guest_id': 'ubuntu64Guest',
            'disks': [{'key': '2000', 'label': 'Hard disk 1', 'capacity': 10 << 30}, {'key': '2001', 'label': 'Hard disk 2', 'capacity': (5 << 30) + 1}],
            'nics': [{'key': '4000', 'mac': '00:50:56:aa:bb:cc', 'model': 'vmxnet3', 'network': 'VM Network', 'connected': True}]}
    ins = {'distro': 'ubuntu', 'major': 24, 'product': 'Ubuntu 24.04 LTS'}
    nics = [{**spec['nics'][0], 'mac': 'bc:24:11:00:00:01'}]
    t = convert.target_spec(spec, 'proxmox', ins, nics, 'u-u-i-d', True, 7, 'web 01')
    assert t['vmtype'] == 'qemu' and t['vmid'] == convert.SYNTH_VMID_BASE + 7 and t['guest_id'] == 'l26'
    assert [d['key'] for d in t['disks']] == ['drive-scsi0', 'drive-scsi1'] and t['nics'][0]['model'] == 'virtio'
    cfg = t['pve_config']
    assert 'name: web-01\n' in cfg and 'cores: 4\n' in cfg and 'sockets: 1\n' in cfg and 'bios: ovmf\n' in cfg and 'machine: q35\n' in cfg
    assert cfg.startswith('#qmdump#map:scsi1:drive-scsi1::raw:\n#qmdump#map:scsi0:drive-scsi0::raw:\n')
    assert 'scsi0: local:vm-0-disk-0,size=10G\n' in cfg and 'scsi1: local:vm-0-disk-1,size=6G\n' in cfg
    assert 'net0: virtio=BC:24:11:00:00:01,bridge=vmbr0\n' in cfg and 'agent: 1\n' in cfg and 'boot: order=scsi0' in cfg
    v = convert.target_spec({**spec, 'platform': 'proxmox', 'vmtype': 'qemu', 'vmid': 100, 'pve_config': 'x'}, 'vmware', ins, nics, 'u', False, 8, 'web 01')
    assert v['guest_id'] == 'ubuntu64Guest' and 'pve_config' not in v and v['disks'][0]['controller'] == 'pvscsi' and v['disks'][0]['key'] == '2000'
    assert v['nics'][0]['mac_fixed'] and v['nics'][0]['model'] == 'vmxnet3'


def test_guest_ids_and_macs():
    assert convert.vmware_guest_id({'distro': 'debian', 'major': 13}) == 'debian12_64Guest'
    assert convert.vmware_guest_id({'distro': 'rocky', 'major': 9}) == 'rhel9_64Guest'
    assert convert.vmware_guest_id({'distro': 'alpine', 'major': 3}) == 'other5xLinux64Guest'
    for _ in range(20):
        m = convert.new_mac('vmware')
        assert m.startswith('00:50:56:') and int(m.split(':')[3], 16) <= 0x3f
        assert convert.new_mac('proxmox').startswith('bc:24:11:')


def test_nic_names_and_links(tmp_path):
    text = 'network:\n  version: 2\n  ethernets:\n    ens192:\n      dhcp4: true\n    enp3s0f1:\n      dhcp4: true\nDEVICE=eth0\ninterface-name=ens192\n'
    names = convert.nic_names_in({'/etc/netplan/a.yaml': text})
    assert names[:3] == ['ens192', 'enp3s0f1', 'eth0']
    files = {'/etc/netplan/50-cloud-init.yaml': 'ethernets:\n  eth0:\n    match:\n      macaddress: BC:24:11:AA:BB:CC\n    set-name: eth0\n',
             '/etc/sysconfig/network-scripts/ifcfg-eth0': 'HWADDR=bc:24:11:aa:bb:cc\nDEVICE=eth0\n', '/etc/network/interfaces': 'iface eth0 inet dhcp\n'}
    out = convert.rewrite_macs(files, [('bc:24:11:aa:bb:cc', '00:50:56:01:02:03'), ('', 'x')])
    assert set(out) == {'/etc/netplan/50-cloud-init.yaml', '/etc/sysconfig/network-scripts/ifcfg-eth0'}
    assert 'macaddress: 00:50:56:01:02:03' in out['/etc/netplan/50-cloud-init.yaml'] and 'HWADDR=00:50:56:01:02:03' in out['/etc/sysconfig/network-scripts/ifcfg-eth0']
    assert convert.rewrite_macs(files, [('bc:24:11:aa:bb:cc', 'BC:24:11:AA:BB:CC')]) == {}
    links = convert.link_files(str(tmp_path), [{'mac': 'bc:24:11:00:00:01'}, {'mac': 'bc:24:11:00:00:02'}, {'mac': 'bc:24:11:00:00:03'}], ['ens192', 'eth0'])
    assert len(links) == 2 and links[0].endswith('70-hvbackup-ens192.link')
    assert 'MACAddress=bc:24:11:00:00:01' in open(links[0]).read() and 'Name=ens192' in open(links[0]).read()


def test_fixup_commands():
    c = convert.fixup_command('proxmox', ['/w/d0.qcow2'], {'distro': 'ubuntu'}, ['/w/links/70-hvbackup-ens192.link'], True, [('/w/patched/etc/netplan/a.yaml', '/etc/netplan')])
    assert c[0] == 'virt-v2v-in-place' and c.count('--copy-in') == 2 and '/w/patched/etc/netplan/a.yaml:/etc/netplan' in c and '--firstboot-install' in c and c[c.index('--block-driver') + 1] == 'virtio-scsi'
    c = convert.fixup_command('vmware', ['/w/d0.qcow2'], {'distro': 'rocky', 'package_management': 'dnf'}, [], False)
    assert c[0] == 'virt-customize' and any('dracut' in x for x in c) and '--firstboot-install' not in c
    c = convert.fixup_command('vmware', ['/w/d0.qcow2'], {'distro': 'ubuntu', 'package_management': 'apt'}, [], True)
    assert any('update-initramfs' in x for x in c) and c[c.index('--firstboot-install') + 1] == 'open-vm-tools'
    assert convert.guest_is_windows({'guest_id': 'windows2019srv_64Guest'}) and convert.guest_is_windows({'guest_id': 'win11'}) and not convert.guest_is_windows({'guest_id': 'l26'})
