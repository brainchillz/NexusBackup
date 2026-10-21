#!/bin/bash
# hvbackup bootstrap installer: installs the prerequisites, fetches the source, and runs the
# native installer (or brings the Docker Compose stack up). One command on a fresh machine:
#
#   curl -fsSL https://raw.githubusercontent.com/brainchillz/NexusBackup/main/packaging/get.sh | sudo bash -s -- [options]
#   (or, with a checkout already on disk:)   sudo ./packaging/get.sh [options]
#
# Options:
#   --repo DIR          backup repository directory (default /var/lib/hvbackup/repo native, ./repo for Docker)
#   --url URL           public URL of the server (default https://<this host's FQDN>:8460)
#   --admin NAME        create or reset this admin account (password: $HVB_ADMIN_PASSWORD, a prompt, or generated and printed).
#                       A first install creates `admin` by itself; a rerun (upgrade) touches no account unless you pass this.
#   --docker            install with Docker Compose instead of natively (needs docker with the compose plug-in)
#   --agent             proxy VM: install only the agent
#   --ref BRANCH|TAG    git ref to install (default main)
#   --source DIR        use this checkout instead of cloning
#   --git URL           clone from here (default the project's GitLab; $HVB_GIT_URL also works)
# The source lands in /opt/hvbackup/src (or stays where --source points) so that `get.sh` can be rerun to upgrade.
set -euo pipefail
genpw() { head -c 300 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20; }
GIT_URL="${HVB_GIT_URL:-https://github.com/brainchillz/NexusBackup.git}"
REF=main; SRC=; MODE=native; REPO=; URL=; ADMIN=
while [ $# -gt 0 ]; do
    case "$1" in
        --repo) REPO="$2"; shift ;;
        --url) URL="$2"; shift ;;
        --admin) ADMIN="$2"; shift ;;
        --docker) MODE=docker ;;
        --agent) MODE=agent ;;
        --ref) REF="$2"; shift ;;
        --source) SRC="$2"; shift ;;
        --git) GIT_URL="$2"; shift ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)"; exit 2 ;;
    esac
    shift
done
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }

# the script may run from a checkout: prefer that over cloning
if [ -z "$SRC" ] && [ -f "$(dirname "${BASH_SOURCE[0]:-/nonexistent}")/install.sh" ] 2>/dev/null; then
    SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

pkg() {  # install distro packages
    if command -v apt-get >/dev/null; then DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@" >/dev/null
    elif command -v dnf >/dev/null; then dnf install -y -q "$@" >/dev/null
    elif command -v zypper >/dev/null; then zypper -n install "$@" >/dev/null
    else echo "cannot install packages ($*) on this distribution; install them and rerun"; exit 1; fi
}
echo "==> prerequisites"
if command -v apt-get >/dev/null; then apt-get update -q >/dev/null; fi
command -v git >/dev/null || pkg git
command -v curl >/dev/null || pkg curl
if [ "$MODE" != docker ]; then
    command -v python3 >/dev/null || pkg python3
    python3 -m venv --help >/dev/null 2>&1 || { command -v apt-get >/dev/null && pkg python3-venv || true; }
    python3 -c 'import ensurepip' 2>/dev/null || { command -v apt-get >/dev/null && pkg python3-venv || pkg python3-pip; }
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "python 3.12+ is required (found $(python3 -V)); use --docker on this host"; exit 1; }
else
    command -v docker >/dev/null || { echo "docker is not installed; see https://docs.docker.com/engine/install/ then rerun"; exit 1; }
    docker compose version >/dev/null 2>&1 || { echo "the docker compose plug-in is missing (docker-compose-plugin)"; exit 1; }
fi

if [ -z "$SRC" ]; then
    SRC=/opt/hvbackup/src
    if [ -d "$SRC/.git" ]; then
        echo "==> updating $SRC to $REF"; git -C "$SRC" fetch -q origin; git -C "$SRC" checkout -q "$REF"; git -C "$SRC" pull -q --ff-only origin "$REF" 2>/dev/null || true
    else
        echo "==> cloning $GIT_URL ($REF) into $SRC"; mkdir -p /opt/hvbackup; git clone -q --branch "$REF" "$GIT_URL" "$SRC"
    fi
fi
echo "==> source: $SRC ($(git -C "$SRC" describe --always --dirty 2>/dev/null || echo unversioned))"

case "$MODE" in
    agent)
        exec bash "$SRC/packaging/install.sh" --agent ;;
    native)
        FRESH=1; [ -f /var/lib/hvbackup/catalog.db ] && FRESH=0
        [ -n "$ADMIN" ] || [ "$FRESH" = 0 ] || ADMIN=admin
        set -- ; [ -n "$REPO" ] && set -- "$@" --repo "$REPO"
        [ -n "$ADMIN" ] && set -- "$@" --admin "$ADMIN"
        if [ "$FRESH" = 1 ]; then set -- "$@" --url "${URL:-https://$(hostname -f 2>/dev/null || hostname):8460}"; elif [ -n "$URL" ]; then set -- "$@" --url "$URL"; fi
        exec bash "$SRC/packaging/install.sh" "$@" ;;
    docker)
        cd "$SRC"
        if [ ! -f .env ]; then
            printf 'HVB_URL=%s\nHVB_REPO_HOST=%s\n' "${URL:-https://$(hostname -f 2>/dev/null || hostname):8460}" "${REPO:-$SRC/repo}" > .env
            echo "==> wrote $SRC/.env"
        else
            [ -n "$URL" ] && sed -i "s|^HVB_URL=.*|HVB_URL=$URL|" .env
            [ -n "$REPO" ] && sed -i "s|^HVB_REPO_HOST=.*|HVB_REPO_HOST=$REPO|" .env
        fi
        . ./.env; mkdir -p "$HVB_REPO_HOST"
        FRESH=1; [ -n "$(docker compose ps -aq 2>/dev/null)" ] && FRESH=0
        [ -n "$ADMIN" ] || [ "$FRESH" = 0 ] || ADMIN=admin
        echo "==> building the image and starting the stack"
        docker compose up -d --build --quiet-pull 2>&1 | grep -v '^$' || true
        for _ in $(seq 1 30); do docker compose exec -T hvbackup hvbackup --help >/dev/null 2>&1 && break; sleep 1; done
        if [ -n "$ADMIN" ]; then
            PW="${HVB_ADMIN_PASSWORD:-}"; GEN=0
            if [ -z "$PW" ]; then
                if [ -t 0 ]; then read -r -s -p "password for $ADMIN: " PW; echo; else PW=$(genpw); GEN=1; fi
            fi
            docker compose exec -T hvbackup hvbackup init-admin "$ADMIN" --password "$PW"
            [ "$GEN" = 1 ] && echo "==> generated password for $ADMIN: $PW   (shown once; change it under Settings)"
        fi
        echo "==> running. open $HVB_URL   (logs: docker compose -f $SRC/compose.yml logs -f)" ;;
esac
