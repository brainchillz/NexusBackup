# Cross-Hypervisor VM Backup and Conversion Server — Plan

Status: draft, 2026-10-20
Working name: pending (referred to as "the server" below)

## 1. What we are building

A Linux service that backs up virtual machines from VMware (vCenter or
standalone ESXi) and Proxmox VE into one hypervisor-neutral store, restores
them to either platform, and converts a VM from one platform to the other as a
restore with a guest-fixup step in the middle.

Everything is API-first. The CLI, the web UI, the vCenter plugin and the
Proxmox plugin are all clients of the same REST API. Nothing bypasses it.

### Goals

- Back up running VMs with no downtime, application-consistent where a guest
  agent exists, crash-consistent otherwise.
- Incremental forever: only changed blocks cross the network after the first
  full. VMware Changed Block Tracking on one side, QEMU dirty bitmaps on the
  other.
- One catalog and one deduplicated store across both platforms.
- Trigger and monitor backups from inside the vSphere Client and the Proxmox
  UI, not only from our own UI.
- Restore to the original platform, or to the other one with conversion.
- Login through local accounts always, with Nexus SSO and generic OpenID
  Connect as optional providers. Authorisation stays local.
- Installable by anyone. No dependency on our lab's infrastructure.

### Non-goals (for now)

- Physical machines, Hyper-V, cloud providers.
- File-level backup agents inside guests. File-level *restore* from an image
  is in scope, agents are not.
- Replacing Proxmox Backup Server for pure-Proxmox shops. Our pitch there is
  one catalog across platforms and cross-restore.
- Bundling any Broadcom software. VDDK is not used at all (Phase 0 proved the
  open-API HotAdd transport). The VMware Tools ISO for Windows conversions is
  fetched from the user's own ESXi host.
- Depending on the Broadcom developer portal at all. The vSphere API, CBT,
  snapshots, datastore HTTPS reads and writes, and OVF import are built into
  every vCenter and ESXi host, and govmomi/pyVmomi are open source. The only
  optional download is the vSphere Client SDK's JavaScript bridge for the
  vCenter plug-in, which is free with an account and expected to ship inside
  plug-ins. A paid or evaluation vSphere licence is required regardless,
  because the free ESXi licence makes the API read-only.

### Required line items

These are fixed requirements, not nice-to-haves, and each maps to a section
below.

| # | Requirement | Where |
|---|---|---|
| 1 | Own web UI, entirely on the public API: browse VMs on every connected platform, pick, run, schedule, restore | §2 Web UI |
| 2 | vSphere Client plug-in for vCenter 8 to run and schedule backups | §4 vCenter |
| 3 | Equivalent for Proxmox, as far as its UI allows | §4 Proxmox |
| 4 | Controllable from Nexus Dashboard through its plugin mechanism | §4 Nexus Dashboard |
| 5 | Operator chooses the filesystem directory backups land in | §2 Repository |
| 6 | Installs as a native package or as a Docker Compose stack with a host directory mapped for storage | §6 Packaging |

## 2. Architecture

```
                 ┌───────────────────────────────────────────────────┐
  CLI ──────────▶│                     REST API                      │
  Web UI ───────▶│  auth · vms · jobs · runs · restore-points ·      │
  vCenter plugin▶│  restores · conversions · tasks · events          │
  Proxmox shim ─▶│                                                   │
                 ├─────────────┬────────────┬──────────┬─────────────┤
                 │  Scheduler  │  Catalog   │  Chunk   │  Conversion │
                 │  + task     │  (SQLite,  │  store   │  engine     │
                 │  runner     │  Postgres  │  (fixed  │  (libguestfs│
                 │             │  later)    │  chunks, │  virt-v2v,  │
                 │             │            │  zstd)   │  KVM test)  │
                 ├─────────────┴────────────┴──────────┴─────────────┤
                 │              Hypervisor provider interface        │
                 ├──────────────────────────┬────────────────────────┤
                 │  VMware provider (push)  │  Proxmox provider      │
                 │  vSphere API + CBT +     │  (push via PVE 9       │
                 │  hot-add in a proxy VM   │  backup-provider shim) │
                 └──────────────────────────┴────────────────────────┘
```

### Provider interface

A provider answers these calls and nothing else. The storage layer never
knows which platform a disk came from.

| Call | VMware | Proxmox |
|---|---|---|
| inventory | vSphere API | PVE API `/cluster/resources` |
| freeze(vm, quiesce) | CreateSnapshot, quiesced via Tools | PVE handles fs-freeze via guest agent inside the backup job |
| changed_extents(disk, since) | QueryChangedDiskAreas with change ID | QEMU dirty bitmap handed to the plugin |
| read(disk, extents) | snapshot base disk hot-added to the proxy VM, read as a local block device; HTTPS range reads for powered-off VMs | block device exposed to the plugin on the node |
| release(snapshot) | RemoveSnapshot, wait for consolidation | PVE handles it |
| describe(vm) | config → neutral VM spec | config → neutral VM spec |
| create(vm spec, disks) | ImportVApp / datastore upload | PVE API + qm importdisk |
| write(disk, extents) | HTTPS PUT of the flat file over a VirtualDiskManager-created thin disk; hot-add write-back later | NBD or import |

Both platforms are push: a small component next to the hypervisor (a proxy VM
on vSphere, the plugin on a Proxmox node) streams blocks to the server's
ingest endpoint. One ingest path, one chunk writer, and no hypervisor
credentials on the node side.

### Phase 0 findings on VMware transports (2026-10-20, vCenter 8.0.3, VMFS on iSCSI)

| Transport | Running VM | Incremental | Measured | Verdict |
|---|---|---|---|---|
| Datastore HTTPS range reads (host or vCenter proxy) | **no**, HTTP 500: VMFS lock on the base disk even behind a snapshot | yes | 206 works on powered-off VMs | powered-off VMs and templates only |
| ExportSnapshot NFC lease | yes | no, full streamOptimized export, no Range | 25 MiB/s | last-resort fallback |
| **Hot-add to a proxy VM** (`independent_nonpersistent`, open API) | **yes** | **yes**, CBT extents read with pread | **512 MiB/s** full, 8 s wall for an incremental | **primary transport** |
| Clone to an NFS datastore on the server | yes | no, full copy per run | not measured | last resort; the NFS export itself is the instant-recovery path later |
| VDDK | yes | yes | not tested | not needed |

Spike numbers on gitlab-runner-01 (40 GB disk, 7.5 GiB allocated, running):
full in 23 s wall including quiesced snapshot and cleanup, incrementals in
8 s transferring 3 to 133 MiB. On vmbackup-target (10 GB), a marker file
written in the running VM between the full and the incremental was present
in the backup image, which mounted and passed fsck.

Restore (`spike/restore_spike.py`): VirtualDiskManager creates a thin disk,
the raw image is HTTP PUT over its flat file through the ESXi host at
77 MiB/s, CreateVM attaches it with firmware, guest type and NIC copied from
the source, and the VM booted with Tools reporting in 44 s. The marker and
hostname were confirmed over SSH inside the restored VM. Two lessons for the
product: the PUT path is the bottleneck for restores, so write-back through
the proxy (hot-add in persistent mode) is the Phase 1 restore transport; and
a restored copy carries the original's DHCP client identity and host keys,
so "restore as new VM" must offer a fresh machine identity, while "restore
in place" must stop the original first. Code is in `spike/`.

### The vSphere proxy VM

A minimal Linux appliance VM, one per cluster or standalone host, deployed
from the server (OVF import) or by hand from any template:

- Holds **no vCenter credentials**. The server does every vSphere API call
  (snapshot, CBT query, hot-add, hot-remove, consolidation). The proxy only
  runs an agent that takes work orders from the server: "device with this
  size just appeared, read these extents, push them to this ingest URL with
  this token".
- Needs shared-datastore access to the VMs it backs up, which is the same
  rule VDDK HotAdd has. On a standalone ESXi host with local storage, the
  proxy lives on that host.
- Hot-add places a small redo log in the proxy's folder for the duration of
  the read; the server removes the disk without a file operation, so the
  source VM's files are never touched.
- Sizing from the spike: 2 vCPU, 2 GB RAM saturated 512 MiB/s with four
  reader threads.
- Multiple proxies per site for parallelism; the server picks the one with
  access to the VM's datastore.

### Neutral VM spec

Stored with every restore point. Fields: name, CPU count and topology, memory,
firmware (BIOS/UEFI, secure boot flag), TPM present flag, disks (order, size,
controller type, boot flag), NICs (MAC, model, network name), SMBIOS UUID,
guest OS hint, guest agent present flag, original platform and IDs. Mapping
tables translate controller and NIC models between platforms.

### Storage format

Modelled on Proxmox Backup Server, which has proven the shape for VM images.

- Fixed 4 MiB chunks, aligned to the disk. CBT and dirty-bitmap extents are
  rounded up to chunk boundaries.
- Chunk identity by BLAKE3 hash. Store on the filesystem under the hash
  prefix. Zero chunks are elided.
- zstd compression per chunk. Optional encryption at rest per repository.
- A restore point is a manifest: the neutral VM spec, plus for each disk an
  index mapping chunk number to hash.
- Garbage collection by mark-and-sweep over manifests. Retention is a policy
  on manifests, never on chunks.
- Verification job: re-hash a sample or all chunks on a schedule.

### Repository (requirement 5)

A repository is a directory on the server's filesystem, chosen by the
operator at install time and changeable later. Everything the server writes
lives under it:

```
<repo>/
  repo.json          # format version, repository ID, encryption settings
  chunks/ab/cd/...   # content-addressed chunk files
  manifests/<vm-id>/<timestamp>.json
  staging/           # conversion overlays and ingest spool, safe to clear
```

Set by `--repo` / `BACKUP_REPO` / `repo_path` in the config file, and
mapped from a host directory under Docker. The server refuses to start on a
repository it did not initialise unless told to adopt it, and refuses to
write to one whose format version is newer than itself. Multiple
repositories with per-job targeting come later; the catalog is scoped by
repository ID from day one so that costs nothing.

Any filesystem works. ZFS or btrfs underneath is recommended for
checksumming and snapshots of the repository itself, not required.

### Web UI (requirement 1)

The server's own interface is a client of the REST API with no private
endpoints, so anything the UI can do a script can do, and the vCenter and
Nexus Dashboard plug-ins reuse the same pages. It covers:

- Connections: add vCenter, ESXi, or Proxmox, with inventory refresh.
- Inventory: every VM on every connected platform, with platform, power
  state, size, protection status, last and next backup.
- Protect: pick VMs or whole folders/pools, attach a schedule and retention
  policy, run now.
- Restore points: browse by VM, restore in place, restore as new VM,
  restore to the other platform with conversion, file-level browse.
- Tasks and events, with live progress over a websocket or SSE.
- Settings: repositories, users and tokens, SSO providers, plug-in
  registration helpers.

### Catalog

SQLite to start, with every table scoped by hypervisor connection ID so a
move to Postgres and multiple vCenters costs nothing later. Holds
connections, VMs, jobs, runs, restore points, tasks, users, tokens, audit log.

### Tasks

Every long operation is a task with ID, state, progress, log, and parent.
The API returns a task on any mutating call. Tasks are mirrored into vCenter
Recent Tasks via the extension task type, and into Proxmox's task log via the
plugin.

## 3. Authentication and authorisation

Pluggable providers, each answering only "who is this". Roles and the user
list live in the server.

| Provider | Default | Notes |
|---|---|---|
| Local accounts + API tokens | always on | Recovery path when any SSO is down. CLI and scripts use tokens. |
| Nexus SSO assertion | on when issuer configured | Copy the relying-party pattern: verify-only Ed25519, pinned key, `/sso/callback`, enroll by one-time code. Offline verification. |
| Generic OpenID Connect | off | Keycloak, Authentik, Okta, Entra. Group claim to role mapping in config. |
| vCenter session (plugin) | on with plugin | Exchange vSphere Client session for a vCenter session, validate, map user name to a local account by stripping the federation domain. Local vsphere.local accounts rejected unless explicitly mapped. |
| Proxmox ticket (plugin) | on with plugin | Same shape: validate the PVE ticket, map `user@realm`. |

Roles: admin, operator (run jobs, restore), restore-only, viewer. SSO grants
access to accounts that already exist; it never creates them.

The engine's own connection to each hypervisor is a service account with a
custom role scoped to backup privileges, stored in the server's secrets
store. On vCenter this lives in vsphere.local because federated identities
cannot log in without a browser.

## 4. Hypervisor UI integration

### vCenter remote plug-in

- Register with ExtensionManager, manifest served over HTTPS from the server.
- Views: global dashboard, VM summary portlet (last backup, next run), VM
  tab (restore points), right-click actions "Backup now" and "Restore".
- Progress through vCenter tasks; failures as custom event types so admins
  can hang vCenter alarms on them.
- Thin by design: pages are served by the server and call the REST API.

### Proxmox backup provider plugin

- Perl module installed on each node implementing the PVE 9 backup provider
  interface. Registers the server as a storage target.
- The node hands the plugin each disk as a block device plus its dirty
  bitmap; the plugin streams changed chunks to the server's ingest endpoint
  with a per-node token.
- Backups then appear in Proxmox's own backup jobs and restores run from the
  Proxmox UI through the same plugin.
- Keep the Perl shim minimal: read, hash, send. All logic lives server-side.
- Scheduling from the Proxmox UI (requirement 3) comes for free: once the
  server is a storage target, Proxmox's own backup jobs schedule it. Our UI
  shows those runs too, since they land in the same catalog.
- PVE 8 fallback: no provider API, so the server can only trigger vzdump to
  an NFS export and ingest the result as a full. Offered as a degraded mode.

### Nexus Dashboard plug-in (requirement 4)

Nexus Dashboard 3.x loads a plug-in from a directory under `plugins/`,
either declarative (`plugin.yaml`) or Python (`plugin.py` with a Flask
blueprint). We ship both, in `plugins/nexusdash/` in this repo:

- **Declarative, for any node**: a `service_status` card for the server's
  systemd unit (or its container, via the dashboard's Docker pages), an
  `iframe` widget embedding the server's web UI, and `command_table`
  widgets that run the server's CLI with a read-only token to show
  protection status and recent runs. Works today with no code in the
  dashboard.
- **Python tier, for the node that runs the server**: a blueprint whose
  routes call the server's REST API with a token kept in
  `plugins/vm-backup/state.json`, pages rendered by `static/plugin.js`
  using the dashboard's own table and card classes, a `summary` block for
  `/api/summary`, `alerts` for failed or overdue backups, `history`
  metrics for bytes protected, and a dashboard card. Backup now and restore
  actions are admin-only, enforced by the dashboard's central RBAC and
  again by the server's token role.

The server's CLI gets `--json` output on every read command and a
`status --tsv` summary specifically so the declarative tier has something
clean to parse. Sign-on hand-off: with both apps on Nexus SSO the iframe
opens already authenticated.

## 5. Conversion

A conversion is a restore to the other provider with a fixup stage.

1. Create a qcow2 overlay backed by the immutable restore point, exported
   via NBD from the chunk store.
2. Translate the neutral VM spec to the target platform.
3. Run guest fixups in the overlay with libguestfs:
   - VMware → Proxmox: virt-v2v in-place mode. Virtio driver injection for
     Windows, VMware Tools removal, QEMU guest agent, initramfs and network
     name fixes for Linux.
   - Proxmox → VMware: our own fixup module using libguestfs and hivex.
     Attach with LSI SAS and e1000e (inbox Windows drivers), register
     pvscsi and vmxnet3 from the VMware Tools ISO fetched from the ESXi
     host, remove the QEMU guest agent, regenerate initramfs on Linux where
     needed.
4. Optional test boot on the server under KVM with no network. Pass when
   the guest agent reports in or a login prompt is detected within a timeout.
5. Stream the overlay to the target through the provider and create the VM.

Warn before converting when: TPM present (BitLocker recovery key will be
required), secure boot enabled, unsupported guest OS, snapshots present,
raw device mappings or passthrough devices.

Supported guest matrix at launch: Windows Server 2019+, Windows 10/11,
Debian/Ubuntu/RHEL-family current releases, BIOS or UEFI. Everything else is
reported as unsupported, not converted badly.

Later: scheduled replication (convert and push every new restore point) and
instant recovery (boot on Proxmox from an NBD export while storage migrates;
on vSphere, mount the server's NFS export as a datastore and clone back to
production storage).

## 6. Technology choices

- **Core service: Python** (decided after the spike, 2026-10-20). Throughput
  lives in the proxy agent and the chunk store, both of which hit 360+ MiB/s
  in Python with blake3 and zstd doing the work in C; the API layer is not
  the bottleneck. Python matches the rest of the Nexus suite, pyVmomi is
  mature, and the Nexus Dashboard Python-tier plug-in shares the language.
  FastAPI gives the OpenAPI spec for free.
- **Disk access:** hot-add in the proxy VM, read with pread; datastore
  HTTPS range reads for powered-off VMs so no proxy is needed for them;
  ExportSnapshot as a last-resort full. No VDDK.
- **Conversion:** libguestfs, virt-v2v, hivex, qemu-img, qemu-nbd, KVM.
  Shelled out or via bindings; these stay external tools.
- **Catalog:** SQLite via a migration tool, Postgres later.
- **Web UI:** server-rendered pages with a small amount of JS, same assets
  reused inside the vCenter plugin iframes.
- **Packaging (requirement 6):** two supported installs, same binary.
  - *Native package*: `.deb` and `.rpm` with a systemd unit running as a
    dedicated user, config in `/etc/vm-backup/`, state in
    `/var/lib/vm-backup/`, repository path asked at install and written to
    the config. Conversion features pull in libguestfs, virt-v2v and
    qemu-utils as recommended, not required, packages.
  - *Docker Compose*: one image, a `compose.yml` that maps a host directory
    to `/repo` and a named volume for the catalog and secrets, bridge
    networking since the server pulls from the hypervisors and only needs
    one inbound port for the UI, API, plug-ins and Proxmox ingest. Conversion
    inside the container needs `/dev/kvm` mapped and the libguestfs tools in
    the image; both are documented as optional. The Nexus Dashboard
    `service_status` card then points at the container instead of a unit.
  - One config format for both, environment variables override the file.

## 7. Test environment

Access details and credentials live in `../OTNOPS-Infra` (`hosts/testserver.md`,
`hosts/vcenter.md`, `hosts/proxmox.md`); this section only records what the
project needs to know.

**serenity.onthenile.net** (192.168.35.95) is the designated backup server
for both hypervisor clusters. Not yet in production, so it doubles as the live
test host for every phase. SSH as `otnops` with the shared key, passwordless
sudo. Observed 2026-10-20:

- Ubuntu 26.04.1, 5 cores, 14 GiB RAM, `/dev/kvm` present.
- Storage: 368 GB root, 3.6 TB NVMe at `/mnt/flash`, empty 7.27 TB ZFS pool
  `volume01`. Plan: repository on `/volume01`, staging on `/mnt/flash`.
- Runs Nexus Dashboard, so the dashboard plug-in (Phase 2b) is developed
  against a real install here.
- Nothing from our tool chain is installed yet: no Docker, qemu-img,
  qemu-nbd, nbdkit, virt-v2v, guestfish or Go. Installing Docker lets both
  install paths be exercised on the same host.

**Targets**

| Platform | Address | Version | Notes |
|---|---|---|---|
| vCenter | 192.168.35.10 | 8.0.3 build 24022515 | datacenter `Datacenter`, cluster `Deskmeat`, datastore `vmstor-lun0-2tb`; federated to Nexus SSO as "Okta" |
| Proxmox | 192.168.35.107 | PVE 9.2.11 | nodes `pve-01`, `pve-02`; PVE 9 means the backup-provider plugin API is available |

Both clusters already host throwaway template-built VMs from the deployer
tools, which are the natural spike and conversion test subjects. The spike
created `vmbackup-proxy` (192.168.34.85, the prototype proxy VM) and
`vmbackup-target` (192.168.34.53, a 10 GB test subject) with `deploy-vm`,
plus `vmbackup-restored` from the restore test; all three are disposable.

## 8. Phases

### Phase 0 — Spike (done 2026-10-20)
Proved end to end on the lab vCenter: CBT enable, quiesced snapshot, changed
areas, block reads of a running VM, incremental, consistent image. The HTTPS
range-read assumption failed for running VMs and was replaced by hot-add in
a proxy VM, see the findings table above. Restore by HTTPS upload of the raw
image over a VirtualDiskManager-created disk plus CreateVM booted and was
verified over SSH. All exit criteria met. Spike code is Python with pyVmomi; the proxy agent
is small enough that the language decision for the core can still go either
way.

### Phase 1 — Core service (built 2026-10-20, soak test running)
Delivered as `hvbackup` 0.1.0: REST API with OpenAPI (`/api/docs`), SQLite
catalog, content-addressed chunk store with GC, repository on a chosen
directory, cron scheduler, task runner, local accounts, tokens and four
roles, CLI with `--json` and `status --tsv`, web UI (13 pages), vSphere
provider with hot-add via the proxy agent and HTTPS for powered-off VMs,
restore through the proxy or HTTPS upload, retention, CBT-reset fallback to
full. Packaging: native `install.sh`, `.deb` built in a container, Docker
Compose. Installed on serenity as a systemd service with the repository on
`/volume01/hvbackup` and a nightly job on the lab test VMs.
Not yet done from the original scope: server-driven deployment of the proxy
appliance (today: `install.sh --agent` on any small Linux VM).
Exit criteria still open: two weeks of nightly runs with verified restores.

### Phase 2 — vCenter plug-in
Manifest, registration, VM action and views, task mirroring, custom events.
Exit criteria: backup and restore of a VM driven entirely from the vSphere
Client.

### Phase 2b — Nexus Dashboard plug-in (declarative tier shipped)
`plugins/nexusdash/hvbackup/plugin.yaml`: service card, status table from
`hvbackup status --tsv`, recent runs, journal, embedded console. Python tier
(summary, alerts, backup-now actions) still to do.
Exit criteria: protection status, alerts and backup-now from a dashboard
node.

### Phase 3 — Proxmox provider
Ingest endpoint, PVE 9 backup provider shim, restore to Proxmox, container
backups as file trees.
Exit criteria: incremental backups of running Proxmox VMs appearing in both
catalogs, restore from the Proxmox UI.

### Phase 4 — SSO
Nexus assertion relying party, generic OIDC, vCenter and Proxmox session
mapping, role mapping config.
Exit criteria: one person reaches the same account by all four doors.

### Phase 5 — Conversion
Overlay staging, VMware → Proxmox via virt-v2v, Proxmox → VMware fixup
module, test boot, pre-flight warnings.
Exit criteria: round trip a Windows Server and a Linux VM in both directions
with working networking and guest agent.

### Phase 6 — Hardening and extras
Encryption at rest, verification jobs, VDDK accelerator, file-level restore
by mounting images, scheduled replication, instant recovery on Proxmox,
Postgres, multi-vCenter.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Hot-add needs a proxy VM with datastore access on every cluster | Server deploys and health-checks proxies; powered-off VMs fall back to HTTPS range reads with no proxy; ExportSnapshot as last resort |
| PVE 9 backup provider API changes between point releases | Thin Perl shim, pinned PVE version, integration test per release |
| Free ESXi licence blocks snapshot API | Documented requirement; detect and report clearly |
| CBT silently reset after storage vMotion or crash | Compare change IDs, fall back to full, log loudly |
| Snapshot removal stalls busy VMs | Short backup windows, orphan snapshot check before each run, alert on consolidation needed |
| Guest fixup long tail | Narrow supported matrix, clear unsupported reporting, test boot before push |
| vCenter plug-in certificate trust and reachability | Installer walks through registration; reverse proxy guidance |
| Local SSO lock-in | Local accounts first, Nexus and OIDC both optional |

## 10. Open questions

- Product name and repository layout (single repo with `server/`, `plugins/vcenter/`, `plugins/proxmox/`, `cli/`?).
- Go or Python for the core, decided by the spike.
- Licence for the project.
- Minimum vSphere version to support. 7.0 U3 is the practical floor for CBT and remote plug-ins.
