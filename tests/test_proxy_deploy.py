"""First-boot data and image bookkeeping for server-driven proxy deployment."""
import os

from hvbackup.core import proxy_deploy


def test_cloud_config_carries_agent_conf_unit_and_bootstrap():
    cc = proxy_deploy.cloud_config('proxy-x', 'https://backup.example:8460', 'hvp_secret', True, 'ssh-ed25519 AAAA key')
    assert cc.startswith('#cloud-config\n')
    assert 'hostname: proxy-x\n' in cc
    assert 'HVB_URL=https://backup.example:8460' in cc
    assert 'HVB_PROXY_TOKEN=hvp_secret' in cc
    assert 'HVB_INSECURE=1' in cc
    assert '/etc/systemd/system/hvbackup-agent.service' in cc
    assert '/opt/hvbackup/venv/bin/python /opt/hvbackup/proxy_agent.py' in cc
    assert '/api/v1/agent/files/proxy_agent.py' in cc
    assert '- [bash, /opt/hvbackup/bootstrap.sh]' in cc
    assert '      - ssh-ed25519 AAAA key\n' in cc
    # every write_files content line is indented under its block (valid YAML literal)
    for line in cc.splitlines():
        if line.startswith(('HVB_', '[Unit]', '#!/bin/bash')):
            raise AssertionError(f'unindented content line: {line}')


def test_cloud_config_without_key_has_no_users_block_and_verifies_tls():
    cc = proxy_deploy.cloud_config('p', 'https://b:8460', 't', False)
    assert 'users:' not in cc
    assert 'HVB_INSECURE=0' in cc


def test_image_status_reports_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy_deploy.config, 'PROXY_IMAGE', '')
    monkeypatch.setattr(proxy_deploy.config, 'IMAGE_DIR', str(tmp_path))
    monkeypatch.setattr(proxy_deploy.config, 'PROXY_IMAGE_URL', 'https://x/y/img.ova')
    st = proxy_deploy.image_status()
    assert st['cached'] is False and st['path'] == os.path.join(str(tmp_path), 'img.ova') and st['url'] == 'https://x/y/img.ova'
    open(st['path'], 'wb').write(b'abc')
    st = proxy_deploy.image_status()
    assert st['cached'] is True and st['size'] == 3
    monkeypatch.setattr(proxy_deploy.config, 'PROXY_IMAGE', '/nonexistent.ova')
    assert proxy_deploy.image_status()['local'] is True and proxy_deploy.image_status()['cached'] is False
