#!/bin/bash
# Native install of hvbackup from a source checkout (Debian/Ubuntu/RHEL-family).
#   sudo ./packaging/install.sh            server: /opt/hvbackup, /etc/hvbackup/hvbackup.conf, /var/lib/hvbackup, systemd unit
#   sudo ./packaging/install.sh --agent    proxy VM: agent only, /etc/hvbackup/agent.conf template, systemd unit
# Idempotent: rerun to upgrade in place. The .deb built by build-deb.sh does the same steps.
set -euo pipefail
ROLE=server
[ "${1:-}" = "--agent" ] && ROLE=agent
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/hvbackup
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "python 3.12+ is required"; exit 1; }
python3 -m venv --help >/dev/null 2>&1 || { echo "python3-venv is required (apt install python3-venv)"; exit 1; }

echo "==> virtualenv in $PREFIX/venv"
mkdir -p "$PREFIX"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip >/dev/null
"$PREFIX/venv/bin/pip" install -q "$SRC"
install -d -m 750 /etc/hvbackup

if [ "$ROLE" = server ]; then
    id hvbackup >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d /var/lib/hvbackup hvbackup
    chgrp hvbackup /etc/hvbackup
    install -d -m 750 -o hvbackup -g hvbackup /var/lib/hvbackup
    if [ ! -f /etc/hvbackup/hvbackup.conf ]; then
        cat > /etc/hvbackup/hvbackup.conf <<'CONF'
# hvbackup server configuration (KEY=VALUE; environment variables override)
HVB_DATA_DIR=/var/lib/hvbackup
# Where backups land. Any filesystem; ZFS or btrfs recommended. Must be writable by the hvbackup user.
HVB_REPO=/var/lib/hvbackup/repo
HVB_BIND=0.0.0.0
HVB_PORT=8460
# Public URL proxies and browsers use, e.g. https://backup.example.net:8460
HVB_URL=
# Optional real certificate; a self-signed one is generated when unset
HVB_TLS_CERT=
HVB_TLS_KEY=
HVB_LOG_LEVEL=info
# Proxy VM deployment from the UI: the cloud image to import (downloaded once into HVB_DATA_DIR/images).
# HVB_PROXY_IMAGE_URL=https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.ova
# HVB_PROXY_IMAGE=/path/to/local.ova   (offline sites: use this file instead of downloading)
CONF
        chmod 640 /etc/hvbackup/hvbackup.conf; chgrp hvbackup /etc/hvbackup/hvbackup.conf
    fi
    install -m 644 "$SRC/packaging/hvbackup.service" /etc/systemd/system/hvbackup.service
    systemctl daemon-reload
    systemctl enable hvbackup >/dev/null
    systemctl restart hvbackup
    if command -v apt-get >/dev/null && ! command -v qemu-img >/dev/null; then apt-get install -y -q qemu-utils >/dev/null 2>&1 || echo "note: install qemu-utils to enable OVA export"; fi
    echo "==> server installed. Next:"
    echo "    sudo -u hvbackup HVB_CONFIG=/etc/hvbackup/hvbackup.conf $PREFIX/venv/bin/hvbackup init-admin admin"
    echo "    open https://$(hostname -f):8460"
else
    [ -f /etc/hvbackup/agent.conf ] || cat > /etc/hvbackup/agent.conf <<'CONF'
# hvbackup proxy agent: paste the values the server shows when you create the proxy
HVB_URL=https://backup-server:8460
HVB_PROXY_TOKEN=
# Trust: HVB_CA=/path/to/ca.pem, or HVB_INSECURE=1 for a self-signed server certificate
HVB_INSECURE=1
CONF
    chmod 600 /etc/hvbackup/agent.conf
    install -m 644 "$SRC/packaging/hvbackup-agent.service" /etc/systemd/system/hvbackup-agent.service
    systemctl daemon-reload
    systemctl enable hvbackup-agent >/dev/null
    if grep -q '^HVB_PROXY_TOKEN=.\+' /etc/hvbackup/agent.conf; then systemctl restart hvbackup-agent; echo "==> agent started"; else
        echo "==> agent installed. Put the server URL and proxy token in /etc/hvbackup/agent.conf, then: systemctl start hvbackup-agent"; fi
fi
ln -sf "$PREFIX/venv/bin/hvbackup" /usr/local/bin/hvbackup
