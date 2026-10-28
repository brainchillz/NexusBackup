# Installing NexusBackup with an AI assistant

This file is for an AI assistant (or any automation) that has been asked to
install NexusBackup for someone. It condenses [docs/INSTALL.md](docs/INSTALL.md)
into facts to collect, decisions to make, commands to run and checks that tell
you whether each step worked. Follow it top to bottom. Where it says *ask*,
stop and ask the person; where it says *check*, do not continue until the
check passes.

Naming: the product is NexusBackup. Its command, Python package, systemd
service, configuration directory and environment variables are all called
`hvbackup`. The "proxy agent" and "node plug-in" below are components of the
product, not you.

## 1. What you are building

One Linux host runs the **server**: REST API and web UI on TCP 8460, a
scheduler, an SQLite catalog, and a deduplicated chunk repository on local
storage. It backs up VMs from vCenter, standalone ESXi hosts and Proxmox VE 9
clusters, restores them as new VMs on any of those, converts Linux VMs between
VMware and Proxmox, and keeps standby replicas on another connection.

Running VMware VMs are read through a small **proxy VM** on each vSphere
cluster or standalone host; the server deploys it from a cloud image by
itself. Proxmox nodes get a **node plug-in** from a one-liner the server
serves. Nothing is installed inside the guests.

Two supported ways to run the server, same result either way:

| | Native | Docker Compose |
|---|---|---|
| Needs | Linux with systemd, Python 3.12+, root | Docker Engine with the Compose plug-in, root |
| Gets you | `/opt/hvbackup/venv`, service `hvbackup`, config `/etc/hvbackup/hvbackup.conf`, data `/var/lib/hvbackup` | container `hvbackup`, named volume `hvbackup-data`, config through `.env` |
| Pick it when | the host is dedicated to backups (recommended) | the host already runs containers, or its Python is older than 3.12 |

## 2. Collect before you start

Ask for anything you cannot find out yourself. Do not guess the public URL or
the repository location.

1. **Shell access** to the server host as root or with `sudo`. Check: `sudo -n true` succeeds, or you are root.
2. **Operating system.** Check: `cat /etc/os-release`. Tested: Ubuntu 24.04 and 26.04, Debian 13. RHEL 9 needs `python3.12` made the default `python3`, or Docker.
3. **Python** for a native install. Check: `python3 -c 'import sys; print(sys.version_info >= (3, 12))'` prints `True`, and `python3 -m venv --help` works. If not, install `python3-venv` (apt) or use Docker.
4. **Public URL** of the server, for example `https://backup.example.net:8460`. Proxies, Proxmox nodes and browsers use it; it becomes `HVB_URL` and the name on the certificate. *Ask* if the host has no DNS name the hypervisor networks can resolve. Check: `getent hosts <name>` resolves to this host.
5. **Repository directory**, where backups land. Plan for the allocated size of the VMs to protect, compressed (40 to 60 percent), plus daily change times retention. Any local filesystem; ZFS or btrfs preferred with their compression off. *Ask* which path or dataset; never pick a directory under `/etc` or `/usr` (the service cannot write there) and never a datastore of the hypervisor being protected. Check: `df -h <dir>` shows the expected free space.
6. **Network reach.** From the server host: TCP 443 to vCenter and every ESXi host, TCP 8006 to Proxmox nodes. To the server: TCP 8460 from proxies, nodes and browsers. Check: `timeout 3 bash -c '</dev/tcp/<vcenter>/443'` returns 0.
7. **Hypervisor credentials**, for the first connection: vCenter or ESXi user and password (an administrator while evaluating, or a role with the privileges listed in [README.md](README.md#requirements)); Proxmox `root@pam` or an API token `user@realm!tokenid`. *Ask* for these; never read them out of files you were not pointed to, and never echo them into shell history or logs (pass them through environment variables or prompts).
8. **Admin password** the person wants for the web login, or agreement that the installer generates one and you report it once.
9. **Conversion** between VMware and Proxmox is optional. It needs `virt-v2v` and `libguestfs-tools` (the installer adds them on Debian and Ubuntu; the container image has them) and, for speed, `/dev/kvm` on the host. Check: `ls -l /dev/kvm`. A VM host without nested virtualization has no `/dev/kvm`; conversion then still works, several times slower.

## 3. Native install

Run as root. Replace the URL, repository and admin name with the values from
section 2. Put the admin password in `HVB_ADMIN_PASSWORD` if one was chosen;
without it the installer prints a generated password once.

```bash
git clone https://github.com/brainchillz/NexusBackup.git
cd NexusBackup
git checkout v0.3.0                       # or main for the newest code
sudo HVB_ADMIN_PASSWORD='<chosen password>' ./packaging/get.sh \
     --repo /srv/hvbackup --url https://backup.example.net:8460 --admin admin
```

`get.sh` installs `git`, `python3`, `python3-venv`, `qemu-utils` and the
conversion tool chain where apt is available, runs `packaging/install.sh`,
waits for the service to listen and creates the admin. It is idempotent:
rerunning it upgrades in place and leaves accounts and configuration alone.

Check, all three must hold:

```bash
systemctl is-active hvbackup                                        # active
curl -k -s -o /dev/null -w '%{http_code}\n' https://localhost:8460/login   # 200
sudo grep -E '^HVB_(REPO|URL)=' /etc/hvbackup/hvbackup.conf          # the values you passed
```

If the service is not active: `journalctl -u hvbackup -n 50`. The usual causes
are a repository directory the `hvbackup` user cannot write, or port 8460 in
use.

Firewall, when one is active: `ufw allow 8460/tcp` (Ubuntu) or
`firewall-cmd --permanent --add-port=8460/tcp && firewall-cmd --reload`
(RHEL).

## 4. Docker Compose install

Run as root on a host where `docker compose version` works.

```bash
git clone https://github.com/brainchillz/NexusBackup.git
cd NexusBackup
git checkout v0.3.0
sudo HVB_ADMIN_PASSWORD='<chosen password>' ./packaging/get.sh --docker \
     --repo /srv/hvbackup --url https://backup.example.net:8460 --admin admin
```

This writes `.env` (`HVB_URL`, `HVB_REPO_HOST`, and
`COMPOSE_FILE=compose.yml:compose.kvm.yml` when the host has `/dev/kvm`),
pulls or builds the image, starts the stack and creates the admin. The catalog,
secrets and certificate live in the named volume `hvbackup-data`; the
repository is the host directory in `HVB_REPO_HOST`. Every CLI command in
Docker is `docker compose exec hvbackup hvbackup ...`.

Check:

```bash
docker compose ps                                                   # hvbackup running
curl -k -s -o /dev/null -w '%{http_code}\n' https://localhost:8460/login   # 200
docker compose exec hvbackup hvbackup --version                     # the release you checked out
```

## 5. Sign in and verify the server

Open `https://<server>:8460` in a browser (self-signed certificate, expected)
or do it from the shell. The API takes a cookie from `/api/v1/auth/login`;
every later call is on `/api/v1/...` and `/api/docs` has the OpenAPI page.

```bash
curl -k -s -c /tmp/hvb.cookie -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"<password>"}' https://localhost:8460/api/v1/auth/login
curl -k -s -b /tmp/hvb.cookie https://localhost:8460/api/v1/status | python3 -m json.tool
rm /tmp/hvb.cookie
```

Check in the status: `version` is the release you installed, `repository.path`
is the directory from section 2, `repository.free_bytes` matches the volume,
and `conversion.available` is `true` if conversion was wanted (`conversion.kvm`
says whether `/dev/kvm` is usable).

The CLI is a client of the API and needs a token, on the server and in the
container alike (only `init-admin` works without one). Create it in the web
UI under **Settings → API tokens**, or with the session cookie from above,
before removing it:

```bash
curl -k -s -b /tmp/hvb.cookie -H 'Content-Type: application/json' \
     -d '{"name":"setup"}' https://localhost:8460/api/v1/tokens        # {"token": "hvb_...", "note": "shown once"}
```

Put the token into `~/.config/hvbackup/client.conf` (mode 600) of the user
running the CLI on the server (`/usr/local/bin/hvbackup` on a native install):

```
HVB_URL=https://backup.example.net:8460
HVB_TOKEN=hvb_...
HVB_INSECURE=1
```

In Docker, pass it instead: `docker compose exec -e HVB_TOKEN=hvb_... hvbackup
hvbackup status`. The commands in the following sections are written for a
native install; prefix them that way in Docker.

Check: `hvbackup status` prints the same numbers as the status call above.

## 6. First connection

vCenter or ESXi, passing the password through the environment so it stays
out of the command line:

```bash
hvbackup connections add --type vcenter --name lab-vcenter --host vcenter.example.net \
    --username administrator@vsphere.local --password "$VC_PASSWORD"
```

Proxmox: `--type proxmox --host pve-01.example.net --username 'root@pam'
--password "$PVE_PASSWORD"`, or an API token as the username with its secret
as the password.

Check: `hvbackup connections list` shows the connection, and
`hvbackup vms list` lists its VMs within a minute. `hvbackup connections test
<id>` reports the API version. Do not add `--verify-ssl` unless the hypervisor
has a certificate the server trusts.

## 7. Proxy VM (vSphere only)

Running VMware VMs need a proxy VM on the same vSphere. The server builds one
from the Ubuntu 24.04 cloud image (downloaded once, checksum verified). Pick a
host, a datastore with room for the proxy's thin disk and a network with DHCP
that reaches this server, Ubuntu's mirrors and PyPI.

```bash
hvbackup connections placement <connection>         # hosts, datastores, networks, folders to choose from
hvbackup proxies deploy proxy-01 --connection <connection> --host <esxi host> \
    --datastore <datastore> --network <port group> --wait
```

Check: `hvbackup proxies list` shows the proxy **online** with agent version
0.2.0. Deployment usually takes two minutes; the `--wait` returns when the
agent calls back. If it times out, the VM could not reach apt, PyPI or
`HVB_URL`: read `/var/log/hvbackup-bootstrap.log` on the VM's console. Sites
without Internet access set `HVB_PROXY_IMAGE_URL` to a mirror or
`HVB_PROXY_IMAGE` to a local OVA in the server configuration.

Powered-off VMs and templates need no proxy. A proxy is usable from every
connection that can see its VM, so one proxy serves a vCenter and a standalone
host it manages.

## 8. Proxmox nodes (Proxmox only)

On every node of the cluster, as root, run the one-liner the server prints:

```bash
hvbackup connections pve-setup <connection>      # prints the curl | bash line for the nodes
hvbackup connections pve-storage <connection>    # registers the "hvbackup" storage on the cluster
```

Check: in the Proxmox UI the storage `hvbackup` exists on every node, and
`pvesm status` on a node lists it as active. Backup jobs created in the Proxmox
UI against this storage must have fleecing enabled (Advanced tab); jobs
started from NexusBackup set it themselves.

## 9. First backup, job, restore test

```bash
hvbackup backup <vm name or id> --wait            # first run is a full
hvbackup jobs add --name nightly --connection <id> --vms <vm> [<vm> ...] \
    --schedule '0 1 * * *' --keep-last 14
hvbackup restore-points list --vm <vm>
hvbackup restore <restore point id> --name <vm>-restoretest --wait   # powered off by default, new identity
```

Check: the backup run ends `completed` in `hvbackup runs list`; the job
appears enabled in `hvbackup jobs list`; the restored VM exists on the
hypervisor, powered off. Delete the test VM afterwards from the hypervisor, or
leave it for the person to inspect, but say which.

A restored copy keeps the guest's host name and DHCP identity. Never power
it on next to the original on the same network without saying so.

## 10. Hand-over

Report, in this order: the URL and admin account (the password only if the
installer generated it, once, and tell the person to change it under
Settings); the repository path and free space; the install method and
version; the connection, proxy or nodes, and the first backup's result; what
you did not do (certificate, firewall, a job on real VMs) and why. Point to
[docs/INSTALL.md](docs/INSTALL.md) section 12, the production checklist, for
the next steps: a real certificate, a least-privilege vCenter role, monitoring
of free space, and a backup of `/var/lib/hvbackup` (the catalog is the index
to every chunk).

## 11. Rules

- **Never restart or upgrade the service while a backup, restore or sync task runs.** The task dies and its run is marked failed. `hvbackup tasks list` and the dashboard show running tasks.
- **Never delete the repository or `/var/lib/hvbackup`**, and never run `docker compose down -v`, unless the person asked for exactly that.
- **Never pipe `get.sh` from the network with `sudo` on a host you were not told to install on**, and read any script before running it.
- **Keep secrets out of command lines and files you create.** Use environment variables or prompts; tokens go only into `client.conf` files with mode 600.
- **Do not change** `HVB_URL` after proxies or nodes exist without redeploying the proxies and re-registering the Proxmox storage: they pin the server's certificate.
- **Version**: `hvbackup --version` on the server, and `CHANGELOG.md` for what changed between releases.
- When a check fails, read the log (`journalctl -u hvbackup -n 50` or `docker compose logs --tail 50`) and the troubleshooting tables in [docs/INSTALL.md](docs/INSTALL.md#13-troubleshooting-the-install) and [README.md](README.md#troubleshooting) before changing anything else.
