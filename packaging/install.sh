#!/bin/bash
# Native install of hvbackup from a source checkout (Debian/Ubuntu/RHEL-family).
#
#   sudo ./packaging/install.sh [options]          server: /opt/hvbackup, /etc/hvbackup/hvbackup.conf, /var/lib/hvbackup, systemd unit
#   sudo ./packaging/install.sh --agent            proxy VM: agent only, /etc/hvbackup/agent.conf template, systemd unit
#
# Server options (all optional; without them the installer only writes a template config on first run):
#   --repo DIR        backup repository directory (HVB_REPO); created and chowned to hvbackup
#   --url URL         public URL proxies and browsers use (HVB_URL), e.g. https://backup.example.net:8460
#   --port N          listen port (HVB_PORT), default 8460
#   --admin NAME      create (or reset) this admin account once the service is up; the password comes
#                     from $HVB_ADMIN_PASSWORD, else a prompt, else (no terminal) it is generated and printed
#   --no-start        install everything but do not enable/restart the service
# Idempotent: rerun to upgrade in place; options given again overwrite the matching keys in the config.
# The .deb built by build-deb.sh does the same steps.
set -euo pipefail
genpw() { head -c 300 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20; }
ROLE=server; REPO=; URL=; PORT=; ADMIN=; START=1
while [ $# -gt 0 ]; do
    case "$1" in
        --agent) ROLE=agent ;;
        --repo) REPO="$2"; shift ;;
        --url) URL="$2"; shift ;;
        --port) PORT="$2"; shift ;;
        --admin) ADMIN="$2"; shift ;;
        --no-start) START=0 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)"; exit 2 ;;
    esac
    shift
done
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/hvbackup
CONF=/etc/hvbackup/hvbackup.conf
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "python 3.12+ is required"; exit 1; }
python3 -m venv --help >/dev/null 2>&1 || { echo "python3-venv is required (apt install python3-venv)"; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required (use the Docker install otherwise)"; exit 1; }

# set KEY=VALUE in the config file (replaces an existing line, commented or not)
setconf() {
    if grep -q "^#\? *$1=" "$CONF"; then sed -i "s|^#\? *$1=.*|$1=$2|" "$CONF"; else echo "$1=$2" >> "$CONF"; fi
}

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
    if [ ! -f "$CONF" ]; then
        cat > "$CONF" <<'CONF'
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
        chmod 640 "$CONF"; chgrp hvbackup "$CONF"
        echo "==> wrote $CONF"
    fi
    if [ -n "$REPO" ]; then
        install -d -m 750 -o hvbackup -g hvbackup "$REPO"
        setconf HVB_REPO "$REPO"; echo "==> repository $REPO"
    fi
    [ -n "$URL" ] && { setconf HVB_URL "$URL"; echo "==> public URL $URL"; }
    [ -n "$PORT" ] && { setconf HVB_PORT "$PORT"; echo "==> port $PORT"; }
    install -m 644 "$SRC/packaging/hvbackup.service" /etc/systemd/system/hvbackup.service
    systemctl daemon-reload
    if command -v apt-get >/dev/null && ! command -v qemu-img >/dev/null; then apt-get install -y -q qemu-utils >/dev/null 2>&1 || echo "note: install qemu-utils to enable OVA export"; fi
    # cross-platform conversion (libguestfs on this host): best effort, the server works without it
    if command -v apt-get >/dev/null && ! command -v virt-v2v-in-place >/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -q virt-v2v libguestfs-tools >/dev/null 2>&1 || echo "note: install virt-v2v and libguestfs-tools to enable VMware <-> Proxmox conversion"
    fi
    if command -v virt-v2v-in-place >/dev/null; then
        for k in /boot/vmlinuz-*; do [ -f "$k" ] && ! dpkg-statoverride --list "$k" >/dev/null 2>&1 && dpkg-statoverride --update --add root root 0644 "$k" >/dev/null 2>&1 || chmod 0644 "$k" 2>/dev/null; done
        getent group kvm >/dev/null && usermod -aG kvm hvbackup
    fi
    FRESH=1; [ -f /var/lib/hvbackup/catalog.db ] && FRESH=0
    PORTNOW=$(grep -E '^HVB_PORT=' "$CONF" | tail -1 | cut -d= -f2); URLNOW=$(grep -E '^HVB_URL=' "$CONF" | tail -1 | cut -d= -f2)
    if [ "$START" = 1 ]; then
        systemctl enable hvbackup >/dev/null
        systemctl restart hvbackup
        for _ in $(seq 1 60); do systemctl is-active --quiet hvbackup || break; (exec 3<>"/dev/tcp/127.0.0.1/${PORTNOW:-8460}") 2>/dev/null && break; sleep 1; done
        systemctl is-active --quiet hvbackup || { echo "!! the service did not start; see: journalctl -u hvbackup -n 50"; exit 1; }
    fi
    if [ -n "$ADMIN" ]; then
        PW="${HVB_ADMIN_PASSWORD:-}"; GEN=0
        if [ -z "$PW" ]; then
            if [ -t 0 ]; then read -r -s -p "password for $ADMIN: " PW; echo; else PW=$(genpw); GEN=1; fi
        fi
        runuser -u hvbackup -- env HVB_CONFIG="$CONF" "$PREFIX/venv/bin/hvbackup" init-admin "$ADMIN" --password "$PW"
        [ "$GEN" = 1 ] && echo "==> generated password for $ADMIN: $PW   (shown once; change it under Settings)"
    fi
    echo "==> server installed."
    [ -n "$ADMIN" ] || [ "$FRESH" = 0 ] || echo "    create an admin:  sudo -u hvbackup HVB_CONFIG=$CONF $PREFIX/venv/bin/hvbackup init-admin admin"
    echo "    open ${URLNOW:-https://$(hostname -f):${PORTNOW:-8460}}"
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
