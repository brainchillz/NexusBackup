# NexusBackup

Hypervisor backup server for Linux (command and package: `hvbackup`). It backs
up running VMs from vCenter, a standalone ESXi host or a Proxmox VE cluster
into one deduplicated store, without agents in the guests and without any
VMware SDK, restores them as new VMs, and converts Linux VMs between the two
platforms. Backups are
incremental forever: after the first full, only changed blocks cross the
network. Everything is driven by a REST API; the web UI and the CLI are clients
of it. Tested against vCenter 8.0.3 and Proxmox VE 9.2.

**How it works.** For each VM the server takes a snapshot (quiesced when VMware
Tools runs), asks vSphere's Changed Block Tracking which blocks changed since the
last run, and reads only those. Running VMs are read by a small *proxy VM* on
the same vSphere: the server hot-adds the frozen snapshot disk to it, and an
agent inside the proxy hashes 4 MiB chunks and uploads only the ones the server
does not already have. Powered-off VMs and templates are read directly over the
ESXi datastore HTTPS endpoint, no proxy needed. The server stores chunks by
content hash, so identical blocks across VMs and across restore points are kept
once. Measured through the proxy: over 360 MiB/s including hashing and
compression.

Design and test results: [PLAN.md](PLAN.md). Install guide: [docs/INSTALL.md](docs/INSTALL.md).

![Dashboard](docs/images/dashboard.png)

## Requirements

| Where | What |
|---|---|
| Backup server | Linux, Python 3.12 or newer with `python3-venv`, systemd for the native install or Docker for the container. Any filesystem for the repository (ZFS or btrfs recommended). Network reach to vCenter and every ESXi host on TCP 443. Proxies and browsers reach the server on TCP 8460. |
| Proxy VM (one per vSphere cluster or standalone host) | A small Linux VM on that vSphere with access to the datastores of the VMs it backs up. 2 vCPU, 2 GB RAM, Python 3.12+, a SCSI controller (ParaVirtual or LSI Logic). The agent runs as root (SCSI rescans and raw disk reads). It holds no vSphere credentials. |
| Conversion (optional) | `virt-v2v` and `libguestfs-tools` on the backup server (native install; the installer adds them on Debian/Ubuntu). |
| Proxmox VE | PVE 9 (backup-provider API). On every node: the plug-in from the server's installer, which adds `libnbd-bin` and a Python venv. Nodes reach the server on TCP 8460. |
| vSphere | vCenter or ESXi 7.0 U3 or newer. A **paid or evaluation licence**: the free ESXi licence makes the API read-only, so snapshots fail. VMs you back up must not already carry snapshots named `hvbackup`. |

The vCenter account you connect with needs these privileges on the relevant objects:

- Virtual machine → Snapshot management: create and remove snapshot
- Virtual machine → Change configuration: change settings, add existing disk, remove disk, advanced configuration (CBT)
- Virtual machine → Provisioning: allow disk access, allow read-only disk access, allow virtual machine download and upload
- Datastore: browse, low level file operations, allocate space
- Virtual machine → Edit inventory: create new; Interaction: power on (for restores)
- Resource: assign virtual machine to resource pool; Network: assign network (for restores)

Or use an administrator account while evaluating.

## Install

Full guide with screenshots: [docs/INSTALL.md](docs/INSTALL.md). The short
version, on a fresh Ubuntu or Debian machine:

```bash
git clone https://github.com/brainchillz/NexusBackup.git
cd NexusBackup
sudo ./packaging/get.sh --repo /srv/hvbackup --url https://backup.example.net:8460
```

`get.sh` installs the prerequisites, runs the native installer, waits for the
service and creates the `admin` account, printing its password once (set
`HVB_ADMIN_PASSWORD` to choose it). Add `--docker` for the Docker Compose
install, `--agent` on a proxy VM. Rerun it to upgrade. The three ways to
install all end up with the same server on `https://<host>:8460`:

### A. Native, from a source checkout

```bash
sudo ./packaging/install.sh --repo /srv/hvbackup --url https://backup.example.net:8460 --admin admin
```

This creates the `hvbackup` user, a virtualenv in `/opt/hvbackup/venv`,
`/etc/hvbackup/hvbackup.conf` with the given repository and URL,
`/var/lib/hvbackup`, starts the `hvbackup` systemd service and creates the
admin. Without options it writes a template configuration to edit by hand;
`--port`, `--no-start` and `--help` exist too.

### B. Debian package

Build on the same release you will install on (the package bundles a
virtualenv for that release's Python), or build inside a container:

```bash
packaging/build-deb.sh                       # on this host
packaging/build-deb.sh --docker ubuntu:26.04 # or in a throwaway container
sudo apt install ./dist/hvbackup_*.deb
```

The package installs the same files and starts the service. Then set
`HVB_REPO` and `HVB_URL` in `/etc/hvbackup/hvbackup.conf`, restart, and run
`sudo -u hvbackup HVB_CONFIG=/etc/hvbackup/hvbackup.conf /opt/hvbackup/venv/bin/hvbackup init-admin admin`.
Installed on a proxy VM where `/etc/hvbackup/agent.conf` already exists,
the same package enables `hvbackup-agent` instead of the server.

### C. Docker Compose

```bash
cp .env.example .env     # set HVB_URL and HVB_REPO_HOST
docker compose up -d     # pulls ghcr.io/brainchillz/nexusbackup; add --build to build locally
docker compose exec hvbackup hvbackup init-admin admin
```

`HVB_REPO_HOST` is the host directory backups land in, mounted at
`/var/lib/hvbackup/repo`. The catalog, secret key and certificate live in the
named volume `hvbackup-data`. In Docker, every CLI command runs as
`docker compose exec hvbackup hvbackup ...`. The container runs as root so it
can write to whatever host directory you mount; files in the repository are
owned by root. Your own certificate goes in through a Compose override, see
the guide.

### First run

1. Open `https://<server>:8460`, accept the self-signed certificate, sign in as the admin you created.
2. **Connections → Add**: type vCenter or ESXi, host, username, password. The inventory loads in a few seconds.
3. **Proxies → Deploy proxy VM**: name it, pick the connection, host, datastore and network, optionally paste an SSH public key, and deploy. The server downloads the Ubuntu 24.04 cloud image once (about 560 MiB, checksum verified, cached under the data directory), imports it as a VM, boots it with first-boot data that installs the agent from this server, and reports **online** when the agent calls back; the token is never shown and is blanked from the VM's properties once the agent is up. The VM needs DHCP, reach to Ubuntu's apt mirrors and PyPI, and reach to this server. (CLI: `hvbackup proxies deploy <name> --connection <c> [--host H --datastore DS --network NET --ssh-key-file F] --wait`.) Offline sites: set `HVB_PROXY_IMAGE_URL` to a mirror, or `HVB_PROXY_IMAGE` to an OVA already on the server. Steps 4 and 5 are then done.
4. Alternatively **Proxies → Register existing** for a VM you install yourself: name the proxy; the page shows a one-time token and the exact `/etc/hvbackup/agent.conf` to paste. (CLI: `hvbackup proxies add <name>`.) Then on that VM, as root:

   ```bash
   git clone https://github.com/brainchillz/NexusBackup.git
   cd NexusBackup
   sudo ./packaging/install.sh --agent
   sudo nano /etc/hvbackup/agent.conf        # paste HVB_URL and HVB_PROXY_TOKEN from step 3
   sudo systemctl enable --now hvbackup-agent
   ```

   The proxy shows **online** in the UI within a minute. The server matches it to its VM by BIOS UUID on the next inventory refresh; you can also pick the connection when creating it.

   Cloning an existing proxy VM is fine, but before its first boot on the network give the clone its own identity: a new `/etc/machine-id` (`rm /etc/machine-id; systemd-machine-id-setup`), fresh SSH host keys, and `dhcp-identifier: mac` in its netplan. Otherwise the clone presents the same DHCP identifier and takes the original's address. Create the clone's proxy record with its connection chosen, so a vCenter that also sees the host does not claim it.
5. **VMs → Backup now** on one VM, or create a job under **Jobs**. Watch it under **Tasks**.

For the CLI on any machine, create a token under **Settings → API tokens** and
put it in `~/.config/hvbackup/client.conf`:

```
HVB_URL=https://backup.example.net:8460
HVB_TOKEN=hvb_...
HVB_INSECURE=1
```

## Configuration

Server: `/etc/hvbackup/hvbackup.conf` (`KEY=VALUE`, environment variables
override it; `HVB_CONFIG` points elsewhere).

| Key | Default | Meaning |
|---|---|---|
| `HVB_DATA_DIR` | `/var/lib/hvbackup` | Catalog (`catalog.db`), `secret.key`, generated certificate (`certs/`). Small; back it up. |
| `HVB_REPO` | `<data>/repo` | The backup repository: `chunks/`, `manifests/`, `staging/`, `repo.json`. Large. |
| `HVB_BIND` | `0.0.0.0` | Listen address. |
| `HVB_PORT` | `8460` | Listen port (HTTPS). |
| `HVB_URL` | empty | Public base URL shown to proxies and in the UI, e.g. `https://backup.example.net:8460`. |
| `HVB_TLS_CERT` / `HVB_TLS_KEY` | empty | PEM certificate and key. Unset: a self-signed pair is generated in `<data>/certs/` on first start. |
| `HVB_LOG_LEVEL` | `info` | `debug`, `info`, `warning`. |

Agent: `/etc/hvbackup/agent.conf` on each proxy VM.

| Key | Meaning |
|---|---|
| `HVB_URL` | The server, e.g. `https://backup.example.net:8460`. |
| `HVB_PROXY_TOKEN` | The `hvp_...` token shown once when the proxy was created. |
| `HVB_CA` | Path to a CA bundle that signs the server certificate. |
| `HVB_INSECURE` | `1` to skip certificate verification (fine for the self-signed default on a trusted network). |

Client (CLI): `~/.config/hvbackup/client.conf` or environment, keys `HVB_URL`,
`HVB_TOKEN`, `HVB_CA`, `HVB_INSECURE` (defaults to `1`).

**TLS.** The server always speaks HTTPS. To use a real certificate, set
`HVB_TLS_CERT` and `HVB_TLS_KEY` and restart; then proxies can drop
`HVB_INSECURE` or set `HVB_CA`. Browsers also stop warning.

**Choosing the repository.** Any local filesystem works. ZFS or btrfs underneath
adds checksumming and snapshots of the repository itself. It must be writable
by the `hvbackup` user (uid 1000 in Docker). Never place it on the datastore
that holds the VMs you are protecting. Chunks are compressed with zstd, so
leave filesystem compression off. Size it for the allocated data of your VMs
plus the change rate over your retention period; deduplication usually brings
it well under the sum of disk sizes.

**Backing up the server itself.** Copy `HVB_DATA_DIR` (catalog, `secret.key`,
certificate) and the repository. The repository is self-describing: manifests
hold the VM specs and the chunk indexes. Losing `secret.key` only means
re-entering the hypervisor passwords; losing the catalog loses job definitions
and users but the restore points can be re-read from the repository in a
later release.

## Using it

### Web UI

`/` dashboard · `/connections` · `/vms` inventory with Backup now, Restore and
Protect selected · `/jobs` · `/runs` · `/restore-points` · `/proxies` ·
`/tasks` with live logs · `/settings` users, tokens, repository, GC, audit log.
The OpenAPI UI is at `/api/docs`.

### Roles and tokens

`viewer` < `restore` < `operator` < `admin`. Viewers read; restore can restore;
operators run and schedule backups; admins manage users, tokens, connections
and GC. API tokens inherit the role of their user and are shown once.

### CLI

`hvbackup` talks to the API with the token from `client.conf`. Add `--json`
before the subcommand for raw output.

| Command | Does | Example |
|---|---|---|
| `status [--tsv]` | One-line summary; `--tsv` is for dashboards | `hvbackup status --tsv` |
| `connections list\|add\|test\|refresh\|delete` | Manage hypervisor connections | `hvbackup connections add --type vcenter --name lab --host 192.168.35.10 --username administrator@vsphere.local` |
| `connections pve-setup\|pve-storage <c>` | Proxmox: node installer one-liner; register or re-key the `hvbackup` storage | `hvbackup connections pve-storage lab-pve` |
| `connections placement <id\|name>` | Hosts, datastores (with free space) and networks a restore can land on | `hvbackup connections placement dr-site` |
| `vms list [--connection N]` | Inventory with protection status | `hvbackup vms list` |
| `backup <vm> [--wait] [--no-quiesce]` | Back up one VM now (name or id) | `hvbackup backup web01 --wait` |
| `jobs list\|add\|run\|delete` | Scheduled jobs | `hvbackup jobs add --name nightly --vms web01 db01 --schedule "0 2 * * *" --keep-last 14` |
| `runs list [--limit N]` | Backup run history | `hvbackup runs list` |
| `restore-points list [--vm <vm>]` | Restore points | `hvbackup restore-points list --vm web01` |
| `restore <rp> --name NAME [...]` | Restore as a new VM; `--connection` (id or name, any target, the other platform included), `--host`, `--datastore`, `--folder`, `--network SRC=DST` (repeatable), `--power-on`, `--keep-identity`, `--install-tools`, `--wait` | `hvbackup restore 12 --name web01-restored --connection dr-site --network "VM Network=DR-VLAN30" --power-on --wait` |
| `convert <rp> --to proxmox\|vmware` | Make a restore point of the other platform ahead of time (Linux guests); `--install-tools`, `--keep-identity`, `--wait` | `hvbackup convert 12 --to proxmox --install-tools --wait` |
| `tasks list\|show\|wait\|cancel` | Task queue and logs | `hvbackup tasks show 7` |
| `proxies list\|add\|deploy\|image\|delete` | Proxy VMs; `deploy` builds one from the cloud image, `add` prints the token and agent.conf for one you install yourself, `image` shows the cached image | `hvbackup proxies deploy proxy-cluster1 --connection prod --network "VM Network" --wait` |
| `users list\|add\|passwd\|delete` | Local accounts; `--role` | `hvbackup users add alice --role operator` |
| `tokens list\|add\|delete` | API tokens | `hvbackup tokens add ci` |
| `gc` | Garbage-collect unreferenced chunks now | `hvbackup gc` |
| `server`, `init-admin <user>`, `agent` | Server-side: run the server, create or reset an admin, run the proxy agent | `hvbackup init-admin admin` |

### Jobs, schedules, retention

A job names a connection, a set of VMs, an optional cron schedule (five
fields, server local time), retention, and whether to quiesce. Retention is
`keep_last` (number of restore points) and `keep_days` (age); zero disables
either. The newest restore point of a VM is always kept, because it is the base
of the next incremental. Jobs without a schedule run on demand. The scheduler
checks every 30 seconds.

### Restore

Restore creates a **new** VM from a restore point; it never overwrites the
original. Options: name, target host and datastore (defaults to where the VM
lived), power on, and identity:

- **New identity** (default): new MAC addresses and a new BIOS UUID. The guest
  still carries the original hostname, DHCP client identity and SSH host keys,
  so it may claim the original's DHCP lease if both run at once.
- **Keep identity**: same MACs and UUID. Never run the copy alongside the
  original.

Restores go through the proxy when one is online (fast, hot-add write-back),
otherwise by HTTPS upload through the ESXi host (slow but works with no proxy).

#### Restoring somewhere else (disaster recovery)

A restore point can be restored onto **any** connection, not only the one the
backup came from: another vCenter, another cluster, or a standalone ESXi host
added as its own connection. The source vCenter is never contacted; a restore
reads only the repository, so a vCenter that no longer exists is no obstacle.
Pick the target in the restore dialog (web UI or vSphere Client plug-in), or
on the CLI with `--connection`.

What the restore point names is remapped onto the target:

- **Host**: the source host if it exists there; otherwise the only usable host
  (connected, not in maintenance), or the only one that sees the source
  datastore. Several candidates: you must choose (`--host`).
- **Datastore**: the source datastore if the host sees it and it is mounted
  read-write; otherwise the writable datastore with the most free space that
  fits the restore point. The dialog lists free space and greys out datastores
  that are too small.
- **Networks**: each source network by name if the host has it; otherwise the
  host's only network. Several networks and no match: you must map it
  (`--network "VM Network=DR-VLAN30"`), because a copy must not silently land
  on the wrong VLAN. The dialog shows one picker per source network.
- **Folder**: the chosen VM folder, or the datacenter's root VM folder.

Every remap is written to the task log. Explicit choices always win and are
refused with the list of candidates when they do not exist on the target.

Keep the dead vCenter's connection in the catalog: its VMs' restore points hang
off it, and deleting a connection that still owns restore points is refused.
An unreachable connection only costs a failed inventory refresh. A standalone
ESXi host added as a connection needs no proxy (restores there go by HTTPS
upload); datastore HTTP uses the address the connection was made to, since a
standalone host usually calls itself `localhost.localdomain`.

Proven in the lab on 2026-10-20: a restore point of a vCenter-managed VM
restored through a direct connection to one of the ESXi hosts, booted, got
an address.
Backups through a direct ESXi connection are proven the same day: a full of a
powered-off VM over datastore HTTPS with no proxy, an incremental with the
host answering the CBT query, then with a proxy VM on that host a quiesced
hot-add incremental of the running VM, a scheduled job on the connection,
and a hot-add restore back onto the host.

### Garbage collection

Deleting a restore point (retention or by hand) removes its manifest; the
chunks it referenced are reclaimed by GC. GC runs nightly around 03:00 and on
demand from Settings or `hvbackup gc`. Chunks younger than an hour are never
removed, so a run in progress cannot lose data.

## How a backup run works

1. The server connects to vCenter, enables CBT on the VM if it is off, and removes any `hvbackup` snapshot left by an interrupted run.
2. It creates a snapshot named `hvbackup`, quiesced if VMware Tools is running and the job allows it.
3. For each disk it asks `QueryChangedDiskAreas` for the blocks changed since the change ID recorded at the last restore point, or for all allocated blocks on the first run. If vSphere no longer knows that change ID (CBT was reset by a storage vMotion, a host crash or a consolidation failure) it logs the reset and takes a full automatically.
4. Running VM with a proxy online: the snapshot's base disk is hot-added to the proxy as an independent non-persistent disk, which never writes back to it. The agent finds the new device, reads only the changed chunks, hashes them with BLAKE3, asks the server which hashes are new, and uploads those zstd-compressed. Powered-off VM: the server reads the same ranges over the datastore HTTPS endpoint itself.
5. The server merges the new chunk hashes over the previous index, writes the manifest and index files, records the new change IDs, and marks the restore point.
6. The disk is hot-removed from the proxy and the snapshot is deleted, in `finally` blocks, so this happens on failure and on cancel too. If snapshot removal fails it is retried at the start of the next run.

The proxy only ever sees the server URL, its own token, and whatever disk the
server attached to it. It cannot talk to vCenter.

## Converting between VMware and Proxmox

A restore onto a connection of the other platform converts the restore point
first: pick a Proxmox cluster as the target of a vCenter or ESXi restore point,
or a vCenter or ESXi target for a Proxmox one. Linux guests only in this
release; Windows guests and containers are refused up front.

What happens, on the backup server:

1. Every disk of the restore point is exported read-only over NBD straight
   from the repository and a qcow2 overlay is put on it, so the tools get a
   writable disk and the backup stays untouched.
2. libguestfs inspects the guest. Towards Proxmox, `virt-v2v-in-place` makes
   it boot on KVM (virtio-scsi and virtio-net drivers in the initramfs,
   VMware-specific pieces out); towards VMware, `virt-customize` adds the
   `vmw_pvscsi` and `vmxnet3` drivers and regenerates the initramfs. In both
   directions the guest's network configuration is read: a systemd `.link`
   file pins every interface name the configuration uses (`ens192` stays
   `ens192` on Proxmox) to the NIC's MAC address, and configurations that
   match by MAC are rewritten to the new addresses. `--install-tools` adds a
   first-boot install of `qemu-guest-agent` or `open-vm-tools`.
3. Only the blocks the tools changed (typically under 100 MiB; discarded free
   space becomes zero chunks without a read) are stored as new chunks; the
   rest of the index is shared with the source point.
4. The result is a normal restore point of the same VM, kind `converted`,
   with a Proxmox config (virtio-scsi disks, virtio NICs, OVMF with an EFI
   disk when the source used UEFI) or a vSphere spec (paravirtual SCSI,
   vmxnet3, guest id from the detected distribution). It is kept, so the
   next restore of the same point onto that platform skips the conversion,
   and converted Proxmox points also appear in the Proxmox UI under the
   storage's Backups. `hvbackup convert` does the conversion without a
   restore.

Measured on the lab: a 10 GiB Ubuntu 26.04 VM from ESXi to Proxmox in 50 s of
fix-ups plus a 10 s read-back, 63 MiB new in the repository, booted on pve-01
with its interface name kept, an address by DHCP and the guest agent
installed on first boot; a 20 GiB Ubuntu 24.04 VM from Proxmox to vCenter in
12 s plus 1 s, 34 MiB new, booted on a paravirtual SCSI controller with
VMware Tools running.

Requirements: the native install with `virt-v2v` and `libguestfs-tools`
(the installer adds them on Debian/Ubuntu, makes the kernel readable for
libguestfs and puts the `hvbackup` user in the `kvm` group; without `/dev/kvm`
the fix-ups still work, several times slower). The Docker image does not
carry libguestfs yet. New identity (the default) gives the copy fresh MAC
addresses and UUID; the copy keeps the guest's host name and DHCP client
identity, so do not run it next to the original on the same network. A TPM
is not carried over and secure boot is turned off on the copy, both logged.

## Exporting a restore point as OVF/OVA

Any restore point can be written out as a portable OVA (or an OVF folder). This is a
**copy**: the restore point is not modified, stays in the catalog, stays restorable
and remains the base of the next incremental. Make as many exports as you like and
delete them freely.

```bash
hvbackup export <restore point id> --wait          # OVA under <repo>/exports/
hvbackup export <restore point id> --format ovf    # folder with .ovf, .mf and .vmdk files
hvbackup exports list
hvbackup exports download <export id> --out vm.ova
hvbackup exports delete <export id>
```

In the web UI: **Restore points → Export OVA**, then the Exports list on the same
page offers Download. In the vSphere Client the Backups tab has the same button.

What the export contains: one streamOptimized VMDK per disk (compressed, zero
blocks omitted, so it is usually much smaller than the disk), an OVF descriptor
built from the VM's recorded hardware (CPUs, memory, firmware, controllers,
disks, NICs and their networks, guest OS type) and a SHA-256 manifest. The OVA
imports into vSphere, Proxmox (`qm importovf`), VirtualBox and Workstation.
Guest drivers are whatever the guest already has; cross-platform driver fix-ups
are the conversion feature's job.

Requires `qemu-img` (package `qemu-utils`) on the backup server. Temporary space
equal to the allocated size of the largest disk is used under
`<repo>/exports/` while converting.

## vSphere Client plug-in

hvbackup registers itself with vCenter as a remote plug-in, so backups and restores
can be started from the vSphere Client without opening the hvbackup console.

```bash
# HVB_URL must be the public https URL of this server (vCenter fetches the manifest from it)
hvbackup vsphere-plugin register --connection <id>     # or update / unregister / status
```

Registration writes an extension record to vCenter's ExtensionManager with the
manifest URL `https://<server>:8460/vsphere-plugin/plugin.json` and the SHA-256
thumbprint of the server certificate, so a self-signed certificate works. vCenter
downloads the manifest within a minute and the vSphere Client shows a "plug-in
deployed" notification. Re-run `register` (it updates in place) after changing
`HVB_URL` or the certificate.

What appears in the vSphere Client:

| Where | What |
|---|---|
| Menu → hvbackup | global view: status tiles, running tasks, recent runs, protected VMs, link to the full console |
| VM → Summary | an hvbackup card: protection state, restore points, last backup, last run, Back up now |
| VM → Monitor → Backups | restore points with Restore…, run history, Back up now |
| VM right-click → Back up now with hvbackup | modal, works on a multi-selection |
| VM right-click → Restore with hvbackup | modal: restore point, name, target connection (any vCenter or host hvbackup knows), host, datastore with free space, a network picker per source network, power on, new identity |

Every hvbackup backup and restore also appears in vCenter's **Recent Tasks** on the
VM, and posts an event (`net.onthenile.hvbackup.backup.done` / `.failed`) you
can attach vCenter alarms to. Both are best effort: they need the plug-in to be
registered and the connection account to hold the Task.Create privilege.

**Who is the user?** The plug-in pages never see a password. The vSphere Client
hands them a session token, hvbackup exchanges it with vCenter for a clone ticket,
clones the session and reads the user name, then signs the user into hvbackup
with the same username (domain stripped: `jane@corp.example` and
`CORP\jane` both become `jane`). The hvbackup account must already exist with
the role you want; roles are not taken from vCenter. To map differently, set in
`/etc/hvbackup/hvbackup.conf`:

```
HVB_VSPHERE_USER_MAP=administrator@vsphere.local=admin,ops@corp.example=operator1
```

The plug-in pages talk to hvbackup through vCenter's reverse proxy, so the
browser needs no direct route to the backup server; vCenter does.

## Proxmox VE

Proxmox VE 9 clusters are backed up through Proxmox's own backup machinery: the
server registers itself on the cluster as a storage of type `hvbackup` (the PVE 9
external backup-provider API), and every backup, whether started here or from the
Proxmox UI, is a Proxmox backup job writing into this repository. Restores run the
same way round, from this UI or from the Proxmox UI (Storage → Backups → Restore).

Setup, once per cluster:

1. **Connections → Add**, type Proxmox VE, host (a node or the cluster address,
   port 8006 unless given), `user@realm` and password, or an API token as
   `user@realm!tokenid` with the secret as password. The inventory lists VMs and
   containers; templates are flagged.
2. **Connections → Proxmox nodes** shows a one-line installer to run as root on
   every node: `curl -ksS https://<server>:8460/api/v1/pve/install.sh | bash`.
   It installs the storage plug-in and backup provider (Perl, thin), a Python
   helper with its own venv under `/opt/hvbackup`, and `libnbd-bin`, then
   restarts the PVE daemons. Rerun it to upgrade. Nothing on the node holds
   hypervisor credentials; the server talks to the PVE API with the connection's
   account.
3. **Register storage** in the same dialog (CLI: `hvbackup connections
   pve-storage <c>`). The server creates the `hvbackup` storage on the cluster
   with a token tied to this connection and pins its own certificate fingerprint.
   Re-run to rotate the token.

How a VM backup runs: the server asks the guest's node to run `vzdump` against
that storage with fleecing enabled (required by the provider API; the fleecing
image goes on the guest's own storage). Proxmox freezes the guest through its
agent, exposes a snapshot access per disk with a dirty bitmap, and hands it to the
node plug-in, which reads only the dirty regions (all of the disk on the first
run), hashes 4 MiB chunks and uploads those the repository lacks. The server
merges the result with the previous restore point's index, so the catalog holds a
complete index per restore point exactly like VMware ones; the bitmap is reused
when the previous restore point still exists. Containers are streamed as a tar of
the snapshot (mechanism `directory` on the node, `tar` on restore).

Restores: from here, **Restore** on a Proxmox restore point picks a node, storage
and bridge mapping and gets a free VM ID (the name is set afterwards); from the
Proxmox UI, pick the archive under the storage's Backups. Either way the node
plug-in serves each disk from the repository over NBD through `nbdfuse` and
`qemu-img` writes it to the chosen storage; containers are fetched as a tar under
`/var/tmp/hvbackup` first (needs that much free space on the node). Deleting an
archive in the Proxmox UI deletes the restore point here; retention set on jobs
here and `prune-backups` on the storage both work.

Backup jobs created in the Proxmox UI targeting the `hvbackup` storage must have
**fleecing enabled** (Advanced). Not yet: restoring a VMware restore point onto
Proxmox or the reverse (that is conversion, Phase 5), and privileged containers
are restored unprivileged, as the provider API requires.

Proven in the lab on 2026-10-20 (PVE 9.2.11, two nodes): running VM full 20 GiB
in 17 s, incremental with bitmap 148 MiB in 1 s, container 610 MiB in 5 s, VM
and container restores from the Proxmox API in 16 s and 8 s with matching
checksums, server-side restore with rename and power-on, a vzdump started on the
node landing in this catalog, and an archive deleted from the Proxmox side.

## Nexus Dashboard plug-ins

Two plug-ins for [Nexus Dashboard](../NexusDashboard) 3.x ship in
`plugins/nexusdash/`:

- **`vm-backup`** (Python tier): native VM Backup and Backup History pages,
  a front-page card, a `vm-backup` block in `/api/summary`, alerts (server
  unreachable, failed backups, overdue VMs, offline proxies), history metrics
  and Prometheus lines; admins get Back up now, Run now and Export OVA. It
  talks to this server's REST API with one token kept in the dashboard
  user's `~/.config/hvbackup/client.conf`, configured from its Settings
  dialog. Works on any node that can reach the server.
- **`hvbackup`** (declarative, one `plugin.yaml`, no code): service card,
  status table and recent runs from the CLI, server journal, and the console
  in an iframe. For nodes where a Python plug-in is not wanted.

Install either by copying its directory to the dashboard's `plugins/` and
restarting the dashboard; each has a README with the steps. Install one, not
both.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Proxy shows offline | Agent not running, wrong URL or token, or it cannot reach port 8460 | On the proxy: `systemctl status hvbackup-agent`, `journalctl -u hvbackup-agent`. Check `/etc/hvbackup/agent.conf`. |
| Proxy deployment waits and then fails with `the agent has not reported in` | The VM booted but cloud-init could not install the agent: no DHCP lease, no route to apt/PyPI, or the server URL is not reachable from that network | Open the VM console; read `/var/log/hvbackup-bootstrap.log` and `/var/log/cloud-init-output.log`. Fix and run `bash /opt/hvbackup/bootstrap.sh`, or delete the VM and the proxy record and deploy again. |
| `… is a template: vSphere cannot snapshot templates` | Back up now or a job hit a VM template | Convert the template to a VM, or clone it and back up the clone. Templates are not included in the plug-in VM lists. |
| Proxmox: `cannot setup backup access without fleecing` | A backup job made in the Proxmox UI targets the hvbackup storage without fleecing | Edit the job, Advanced → enable fleecing. Jobs started from this server set it automatically. |
| Proxmox: `vzdump reported success but the node plug-in never completed` | The node could not reach the server or the storage token is stale | On the node: `journalctl -u pvedaemon`; re-run the installer and **Register storage** (rotates the token). |
| `conversion needs virt-v2v-in-place ... on the backup server` | A cross-platform restore on a server without libguestfs | `apt install virt-v2v libguestfs-tools`, rerun `packaging/install.sh` (kernel readability, kvm group), or convert on a native server |
| `Windows guests are not converted in this release` / `no operating system was found` | Conversion is limited to installed Linux guests | Restore to the same platform, or wait for Windows support |
| Converted copy boots without network | The guest configuration named an interface the conversion could not find, or matches a MAC it did not know | Check `/etc/systemd/network/70-hvbackup-*.link` in the copy; the task log lists the names found and the files rewritten |
| `VM is powered on and no proxy is available on this connection` | No online proxy is tied to that connection | Bring a proxy online; make sure it is a VM on that vSphere. Run **Connections → Refresh** so the server matches it by BIOS UUID, or set its connection when creating it. |
| `range read ... HTTP 500 (a running VM holds the lock; use a proxy)` | The HTTPS path was used on a running VM | Same as above: this path only works for powered-off VMs. |
| Task log says consolidation needed or pending | ESXi has not merged the snapshot delta yet | Wait, or consolidate in vCenter (VM → Snapshots → Consolidate). The next run tolerates it. |
| Agent exits at once with `the agent must run as root` | Started as an ordinary user | Use the systemd unit; it runs as root. |
| Agent says `HVB_URL and HVB_PROXY_TOKEN are required` although they are exported | `sudo` on Ubuntu 26.04 (sudo-rs) drops the environment | Put the values in `/etc/hvbackup/agent.conf`. |
| `hot-added disk ... not visible` | The proxy has no SCSI controller or the kernel did not rescan in time | Give the proxy a ParaVirtual SCSI controller; check `dmesg` on the proxy. |
| Certificate errors from the CLI or agent | Self-signed server certificate | `HVB_INSECURE=1`, or install a real certificate and set `HVB_CA`. |
| `snapshot carries no changeId; CBT not active yet` | CBT was just enabled on a running VM | Run the backup again; the second snapshot activates it. |
| `a backup of <vm> is already running` | Two runs targeted the same VM | Wait for the first or cancel it under Tasks. |
| UI returns 401 after a server restart | Sessions are signed with `secret.key`; it changed | Sign in again. Keep `HVB_DATA_DIR` persistent. |

Server logs: `journalctl -u hvbackup` (native) or `docker compose logs -f`
(Docker). Every task keeps its own log under **Tasks**.

## Upgrading and uninstalling

| Method | Upgrade | Uninstall |
|---|---|---|
| Native | `git pull && sudo ./packaging/install.sh` or `sudo ./packaging/get.sh` (restarts the service, keeps config, accounts and data) | `sudo systemctl disable --now hvbackup; sudo rm -rf /opt/hvbackup /etc/systemd/system/hvbackup.service`; remove `/etc/hvbackup`, `/var/lib/hvbackup` and the repository when you no longer need the backups |
| Debian package | Build the new `.deb`, `sudo apt install ./dist/hvbackup_*.deb` | `sudo apt remove hvbackup`; data and config stay until you delete them |
| Docker | `git pull && docker compose pull && docker compose up -d` (or `--build`) | `docker compose down`; add `-v` to drop the `hvbackup-data` volume; the repository directory on the host is untouched |
| Proxy agent | Same as native, with `--agent` (deployed proxies: redeploy, or run the agent install on them) | `sudo systemctl disable --now hvbackup-agent; sudo rm -rf /opt/hvbackup /etc/hvbackup` |

The catalog schema is created and extended automatically on start.

## Status and roadmap

Working in this release: vCenter, standalone ESXi and Proxmox VE 9 (VMs with
dirty-bitmap incrementals, containers); full and incremental
backups of running and powered-off VMs; restore as new VM, onto the source or
any other connection (DR); conversion of Linux VMs between VMware and Proxmox
in both directions; OVF/OVA export; jobs, schedules, retention, GC; web
UI, CLI, REST API with OpenAPI; vSphere Client plug-in; Nexus Dashboard
plug-ins (declarative and Python tiers); native, deb and Docker installs.

Planned: Windows guests in conversion (virtio driver injection), Nexus SSO and
OpenID Connect logins, encryption at rest, file-level restore. The design and the
Phase 0 measurements are in [PLAN.md](PLAN.md).

## Licence

MIT.
