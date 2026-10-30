"""Windows conversion: the pure pieces (flavour mapping, synthesized configs and specs, tool
status and environment, the first-boot script). The hive edit and the boot are proven on the lab."""
import os
import tempfile

os.environ.setdefault('HVB_DATA_DIR', tempfile.mkdtemp(prefix='hvb-test-data-'))  # conftest sets these first
os.environ.setdefault('HVB_REPO', tempfile.mkdtemp(prefix='hvb-test-repo-'))

from hvbackup.core import convert, settings, windows_tools  # noqa: E402


def test_windows_flavour_maps_products_and_versions():
    f = convert.windows_flavour
    assert f({'product': 'Windows Server 2022 Standard', 'major': 10, 'minor': 0, 'variant': 'Server'}) == ('win11', 'windows2019srvNext_64Guest')
    assert f({'product': 'Windows Server 2025 Datacenter', 'major': 10, 'minor': 0}) == ('win11', 'windows2022srvNext_64Guest')
    assert f({'product': 'Windows Server 2019 Standard', 'major': 10, 'minor': 0}) == ('win10', 'windows2019srv_64Guest')
    assert f({'product': 'Windows Server 2016 Standard', 'major': 10, 'minor': 0}) == ('win10', 'windows9Server64Guest')
    assert f({'product': 'Windows 11 Pro', 'major': 10, 'minor': 0, 'variant': 'Client'}) == ('win11', 'windows11_64Guest')
    assert f({'product': 'Windows 10 Pro', 'major': 10, 'minor': 0, 'variant': 'Client'}) == ('win10', 'windows9_64Guest')
    assert f({'product': 'Windows 10 Pro', 'major': 10, 'minor': 0, 'arch': 'i386'}) == ('win10', 'windows9Guest')
    assert f({'product': 'Windows Server 2012 R2 Standard', 'major': 6, 'minor': 3}) == ('win8', 'windows8Server64Guest')
    assert f({'product': 'Windows 7 Professional', 'major': 6, 'minor': 1, 'variant': 'Client'}) == ('win7', 'windows7_64Guest')


def test_target_spec_and_pve_config_for_windows():
    spec = {'name': 'ws2022', 'platform': 'vmware', 'cpus': 2, 'memory_mb': 16384, 'firmware': 'efi', 'secure_boot': True, 'tpm': False,
            'guest_id': 'windows2019srvNext_64Guest', 'guest_name': 'Microsoft Windows Server 2022 (64-bit)',
            'disks': [{'key': '2000', 'label': 'Hard disk 1', 'capacity': 90 * 2**30, 'controller': 'lsilogicsas'}],
            'nics': [{'key': '4000', 'mac': '00:50:56:a3:fb:50', 'model': 'e1000e', 'network': 'VM30'}]}
    ins = {'type': 'windows', 'product': 'Windows Server 2022 Standard', 'major': 10, 'minor': 0, 'variant': 'Server', 'arch': 'x86_64'}
    nics = [{**spec['nics'][0], 'mac': 'bc:24:11:00:00:01'}]
    t = convert.target_spec(spec, 'proxmox', ins, nics, 'u-1', False, 115, 'ws2022-pve')
    assert t['guest_id'] == 'win11' and t['tools'] == 'agent' and t['disks'][0]['controller'] == 'virtio-scsi' and t['nics'][0]['model'] == 'virtio'
    cfg = t['pve_config']
    assert 'ostype: win11' in cfg and 'bios: ovmf' in cfg and 'machine: q35' in cfg and 'agent: 1' in cfg and 'tablet: 1' in cfg
    assert 'tpmstate0' not in cfg  # a Server with no TPM at the source gets none
    spec11 = {**spec, 'guest_name': 'Microsoft Windows 11 (64-bit)', 'tpm': True}
    ins11 = {**ins, 'product': 'Windows 11 Pro', 'variant': 'Client'}
    t11 = convert.target_spec(spec11, 'proxmox', ins11, nics, 'u-2', False, 116, 'w11')
    assert 'tpmstate0: local:1,version=v2.0' in t11['pve_config'] and 'ostype: win11' in t11['pve_config']
    # towards VMware: inbox-driver devices, the tools CD only with install_tools
    pspec = {**spec, 'platform': 'proxmox', 'vmtype': 'qemu', 'vmid': 100, 'pve_config': 'x'}
    v = convert.target_spec(pspec, 'vmware', ins, nics, 'u-3', True, 117, 'ws2022-vm')
    assert v['guest_id'] == 'windows2019srvNext_64Guest' and v['disks'][0]['controller'] == 'lsilogicsas' and v['nics'][0]['model'] == 'e1000e'
    assert v['tools'] == 'VMware Tools (first boot)' and v['cdrom'] is True and 'vmtype' not in v
    v2 = convert.target_spec(pspec, 'vmware', ins, nics, 'u-4', False, 117, 'ws2022-vm')
    assert v2['tools'] == '' and 'cdrom' not in v2


def test_fixup_commands_for_windows():
    ins = {'type': 'windows'}
    cmd = convert.fixup_command('proxmox', ['/w/d0.qcow2'], ins, ['/w/x.link'], True)
    assert cmd[0] == 'virt-v2v-in-place' and '--copy-in' not in cmd and '--firstboot-install' not in cmd and '--block-driver' in cmd
    cmd = convert.fixup_command('vmware', ['/w/d0.qcow2'], ins, [], True, firstboot='/w/tools.bat')
    assert cmd == ['virt-customize', '--format', 'qcow2', '-a', '/w/d0.qcow2', '--firstboot', '/w/tools.bat']
    assert convert.fixup_command('vmware', ['/w/d0.qcow2'], ins, [], False) == ['virt-customize', '--format', 'qcow2', '-a', '/w/d0.qcow2']
    assert 'setup64.exe /S /v"/qn REBOOT=R"' in convert.VMWARE_TOOLS_FIRSTBOOT and convert.VMWARE_TOOLS_FIRSTBOOT.startswith('@echo off')


def test_windows_tools_status_env_and_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(windows_tools, 'DIR', str(tmp_path))
    st = windows_tools.status()
    assert not st['ready'] and st['missing'] == ['virtio-win.iso', 'rhsrvany.exe'] and st['fetchable'] == ['virtio-win.iso', 'rhsrvany.exe']
    assert st['files']['rhsrvany.exe']['url'].endswith('/windows-tools-1/rhsrvany.exe') and st['files']['virtio-win.iso']['url'].endswith('virtio-win.iso')
    settings.update({'windows_tools_url': 'https://mirror.example/tools/', 'virtio_win_url': None})
    assert windows_tools.url_for('pnp_wait.exe') == 'https://mirror.example/tools/pnp_wait.exe'
    import io
    windows_tools.save_upload('rhsrvany.exe', io.BytesIO(b'MZ...'))
    windows_tools.save_upload('virtio-win.iso', io.BytesIO(b'ISO'))
    st = windows_tools.status()
    assert st['ready'] and st['files']['rhsrvany.exe']['present'] and st['files']['rhsrvany.exe']['size'] == 5
    e = windows_tools.env({'PATH': '/bin'})
    assert e['VIRTIO_WIN'] == str(tmp_path / 'virtio-win.iso') and e['VIRT_TOOLS_DATA_DIR'] == str(tmp_path) and e['PATH'] == '/bin'
    windows_tools.remove('virtio-win.iso')
    assert windows_tools.status()['missing'] == ['virtio-win.iso']
    try:
        windows_tools.save_upload('evil.exe', io.BytesIO(b'x'))
        raise AssertionError('unknown names must be refused')
    except ValueError:
        pass
    settings.update({'windows_tools_url': None})
    av = convert.availability()
    assert 'windows' in av and isinstance(av['windows']['available'], bool)
