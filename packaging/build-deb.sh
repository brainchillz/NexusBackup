#!/bin/bash
# Build hvbackup_<version>_amd64.deb into dist/. The package bundles a virtualenv at
# /opt/hvbackup/venv, so build it on the same release you will install on (same python3).
#   packaging/build-deb.sh                 build on this host
#   packaging/build-deb.sh --docker ubuntu:26.04   build inside a throwaway container for that release
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
if [ "${1:-}" = "--docker" ]; then
    IMG="${2:-ubuntu:26.04}"
    docker run --rm -v "$SRC:/src:ro" -v "$SRC/dist:/dist" "$IMG" bash -c \
        'apt-get update -q >/dev/null && apt-get install -y -q python3 python3-venv python3-pip >/dev/null && cp -r /src /build && cd /build && packaging/build-deb.sh && cp dist/*.deb /dist/'
    exit 0
fi
VER=$(python3 -c "import re;print(re.search(r'version = \"([^\"]+)\"', open('$SRC/pyproject.toml').read()).group(1))")
REL=$(. /etc/os-release 2>/dev/null && echo "${ID}${VERSION_ID}" || echo linux)
W=$(mktemp -d)
P="$W/hvbackup_${VER}_amd64"
mkdir -p "$P/DEBIAN" "$P/opt/hvbackup/share" "$P/etc/systemd/system" "$P/usr/share/doc/hvbackup"
python3 -m venv "$P/opt/hvbackup/venv"
"$P/opt/hvbackup/venv/bin/pip" install -q --upgrade pip >/dev/null
"$P/opt/hvbackup/venv/bin/pip" install -q "$SRC"
# make the venv relocatable to /opt/hvbackup/venv
find "$P/opt/hvbackup/venv/bin" -type f -exec sed -i "1s#^\#!$P/opt/hvbackup/venv#\#!/opt/hvbackup/venv#" {} \;
sed -i "s#$P/opt/hvbackup/venv#/opt/hvbackup/venv#g" "$P/opt/hvbackup/venv/pyvenv.cfg"
cp "$SRC/packaging/hvbackup.service" "$SRC/packaging/hvbackup-agent.service" "$P/etc/systemd/system/"
sed -n '/^# hvbackup server configuration/,/^CONF$/p' "$SRC/packaging/install.sh" | sed '$d' > "$P/opt/hvbackup/share/hvbackup.conf.example"
sed -n '/^# hvbackup proxy agent/,/^CONF$/p' "$SRC/packaging/install.sh" | sed '$d' > "$P/opt/hvbackup/share/agent.conf.example"
cp "$SRC/README.md" "$P/usr/share/doc/hvbackup/"
cp "$SRC/packaging/deb/postinst" "$SRC/packaging/deb/prerm" "$P/DEBIAN/"; chmod 755 "$P/DEBIAN/postinst" "$P/DEBIAN/prerm"
cat > "$P/DEBIAN/control" <<CTL
Package: hvbackup
Version: ${VER}-1~${REL}
Section: admin
Priority: optional
Architecture: amd64
Depends: python3 (>= 3.12), systemd
Maintainer: hvbackup
Description: Hypervisor backup server and proxy agent
 Backs up running VMs from vCenter, ESXi and Proxmox into one deduplicated
 store on Linux, restores them, and converts between platforms.
CTL
mkdir -p "$SRC/dist"
dpkg-deb --build --root-owner-group "$P" "$SRC/dist/hvbackup_${VER}-1~${REL}_amd64.deb" >/dev/null
rm -rf "$W"
ls -la "$SRC/dist/"*.deb
