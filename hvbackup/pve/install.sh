#!/bin/bash
# hvbackup node plug-in installer for Proxmox VE 9. Run as root on EVERY node of the cluster:
#   curl -ksS https://<backup-server>:8460/api/v1/pve/install.sh | bash
# Installs the storage plug-in and backup provider (Perl), the Python helper in a venv, the
# nbdfuse tool the file-handle mechanism needs, and restarts the PVE daemons so the new
# storage type is known. Idempotent; rerun to upgrade. No credentials are involved: the
# storage token is set when the server registers the storage on the cluster.
set -euo pipefail
HVB_URL="${HVB_URL:-__HVB_URL__}"
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
command -v pveversion >/dev/null || { echo "this is not a Proxmox VE node"; exit 1; }
pveversion | grep -q 'pve-manager/9' || echo "warning: PVE 9 is required for the backup-provider API"

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q libnbd-bin python3-venv curl >/dev/null

echo "==> helper in /opt/hvbackup/venv"
mkdir -p /opt/hvbackup
[ -x /opt/hvbackup/venv/bin/python ] || python3 -m venv /opt/hvbackup/venv
/opt/hvbackup/venv/bin/pip install -q --upgrade pip >/dev/null
/opt/hvbackup/venv/bin/pip install -q blake3 requests zstandard urllib3
curl -ksS "$HVB_URL/api/v1/pve/files/pve_helper.py" -o /opt/hvbackup/pve_helper.py
chmod 755 /opt/hvbackup/pve_helper.py
chmod -R a+rX /opt/hvbackup

echo "==> Perl plug-ins"
mkdir -p /usr/share/perl5/PVE/Storage/Custom
curl -ksS "$HVB_URL/api/v1/pve/files/HvbackupPlugin.pm" -o /usr/share/perl5/PVE/Storage/Custom/HvbackupPlugin.pm
curl -ksS "$HVB_URL/api/v1/pve/files/Hvbackup.pm" -o /usr/share/perl5/PVE/BackupProvider/Plugin/Hvbackup.pm
perl -I/usr/share/perl5 -e 'use PVE::Storage::Custom::HvbackupPlugin; print "plug-in compiles\n"'

echo "==> restarting PVE daemons"
systemctl restart pvedaemon pveproxy pvestatd pvescheduler
echo "==> done on $(hostname). Register the storage from the backup server: Connections -> Proxmox nodes -> Register storage"
