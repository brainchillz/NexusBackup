# Changelog

Releases of NexusBackup (command and package `hvbackup`). Dates are the day
the release was tagged.

## Unreleased

**Unprotect.** A VM is protected when a job or a replication names it, not
when it happens to have restore points: the inventory's "Protected by"
column links to them, "not in a job, N points kept" marks a VM whose
history outlived its job, and the dashboard count follows. **Unprotect
selected** in the inventory (shown when a selected VM is protected, next to
Protect selected) and **Unprotect** on the VM page take a VM out of every
job and replication at once, delete a job or replication left empty and
forget its replicas; checkboxes also delete its restore points and destroy
the replica VMs. `POST /api/v1/vms/unprotect`, `hvbackup vms unprotect`.
Before this the only path was editing each job.

**Server unresponsive after a reboot.** The repository's chunk count comes
from walking every chunk file. Until the first walk of a freshly started
server finished, every status request (the Proxmox nodes' storage check,
the dashboard) began a walk of its own; on a cold cache that takes minutes,
the requests filled the whole thread pool and the server stopped answering
until the walks ended, with the Proxmox storage shown offline meanwhile.
The walk now only ever runs once at a time and in the background, the last
result is kept in `<repository>/stats.json` and served at once after a
restart, and `stats_pending` in `GET /api/v1/status` marks a repository not
yet counted.

**Proxmox 9 refuses a body on DELETE** (HTTP 501), which broke forgetting a
replica with its VM; the client now sends DELETE parameters in the query.

**Container-first settings.** Runtime settings moved from environment
variables into a `settings` table edited under **Settings** (admin); the
environment variable or `hvbackup.conf` key, when set, is the bootstrap
default and locks the field with a note saying where it comes from.
`GET/PATCH /api/v1/settings`. Settings: public URL, time zone (cron schedules
ran in UTC before, whatever the host's zone; the job and replication dialogs
now name the zone), session lifetime, log level (applied at once), update
check, listen address and port (restart), concurrency per connection
(applied to the next operation), catalog copies, proxy image URL, checksum
and local file.

**First administrator from the sign-in page.** While the server has no
account the sign-in page asks for one (`POST /api/v1/auth/setup`, a door that
closes after the first account). `init-admin` stays for scripts.

**Catalog backup and restore.** A daily archive of the catalog (consistent
SQLite copy), `secret.key` and the certificate into `<repository>/catalog/`
(keep 7, both configurable); **Download a backup now**; restore from an
upload or a repository copy, staged and applied at the next start with the
old catalog kept as `catalog.db.before-restore`.

**Certificate manager.** Settings → Certificate shows the server certificate
(names, issuer, expiry, SHA-256 fingerprint, whether the listener serves it
yet), regenerates the self-signed pair with chosen names, imports a real
certificate with key and chain (checked to match before anything is
written), reverts to the previous pair, downloads the PEM for `HVB_CA`, and
after the restart lists what pins it (Proxmox storages, vSphere plug-in
registrations, proxies, dashboard plug-in) with a re-push button for each.
`GET/POST /api/v1/certificate/...`. Proxies deployed after a real certificate
is installed verify it. The sidebar warns 30 days before expiry.

**Verification jobs.** A scheduled task (Sundays 04:00 by default) reads
every chunk a restore point references, decompresses it and checks the
BLAKE3 hash against the chunk's name; points not verified within seven days
are taken, oldest first, shared chunks read once per run. Each point shows
ok or damaged with the time in the restore point lists; damaged points raise
a sidebar notice and a dashboard plug-in alert. **Verify…** on a point runs
it now, optionally with a test boot: the point is restored isolated (NICs
disconnected), powered on, the guest tools or agent awaited, then deleted.
`POST /restore-points/{id}/verify`, `POST /maintenance/verify`,
`hvbackup verify`.

**Notifications.** One webhook (Settings → Notifications): a URL, an
optional secret, and a tick per event to send: failed backups, failed
verifications, failed replications, failed tasks. The JSON document carries `text`/`content` for Slack,
Mattermost, Discord and Teams incoming webhooks and structured fields for
everything else; an optional secret signs each request
(`X-Hvbackup-Signature`). Delivery retries and never blocks the task. **Send
a test** reports the answer.

**Schedule picker.** Job, replication and verification schedules are chosen
as "every few hours", "every day at", "every week on … at", "every month on
day … at" (or custom cron), with a preview of the next three runs in the
server's zone; lists show the schedule in words.

**Fixed: Proxmox storage "not online" under load.** The node's online check
hit a server endpoint that walked every chunk file; under backup load the
walk exceeded the node's 15 s timeout and vzdump refused to start. The walk
now runs once at start and in the background afterwards; the answer is
instant.

**Server log and audit log in the UI**, opened from Settings → Logs into a window-sized scrolling viewer, the server log with level and text filters;
**Restart server** button (exits once no task runs; the shipped unit is now
`Restart=always`, Compose already restarts); **update notice** in the sidebar
from a daily release check (off with the setting); **themes uploaded as a
zip** and deleted from Settings; **proxy image uploaded as an OVA** for
offline sites.

## 0.3.0 (2026-10-28)

**Scheduled replication.** Standby copies of VMs on another connection, of the
same or the other platform, synced after every backup of a member VM, on a
cron schedule, or both. The first sync restores the newest restore point
powered off as `<vm>-replica`; later syncs write only the chunks that differ
into the replica's disks (through the proxy on VMware, which needs agent
0.2.0; older agents fall back to a rebuild) and extend grown disks. Failover,
isolated test boot, end-test and forget (optionally destroying the VM), from
the Replication page, the API (`/replications`, `/replicas`), the CLI and the
dashboard plug-in. Replicas are flagged in the inventory. Cross-platform
replication converts with the identity of the first conversion pinned.

**Several vCenters.** A VM that reappears under a new moref keeps its row,
restore points and CBT state (audit `vm_moved`); jobs skip VMs that moved
away; a proxy is usable from every connection that sees its VM; the vSphere
Client plug-in's session exchange works across Enhanced Linked Mode;
`HVB_CONCURRENCY` (default 2) caps backups, restores and syncs per connection.

**Conversion between VMware and Proxmox** (Linux guests): restore points are exported over NBD from the repository, fixed up
in a qcow2 overlay with libguestfs, and only the changed blocks become new
chunks. Cross-platform restores convert by themselves; `convert` exists in
the API and CLI to convert ahead.

**Container image with conversion.** The image now carries `virt-v2v`,
libguestfs and a kernel for its appliance, so conversion works in Docker
like on a native install. `compose.kvm.yml` maps `/dev/kvm` in for speed
(`get.sh --docker` adds it when the host has the device). `GET /status`
reports whether conversion is available, Settings shows it, and the restore
and replication dialogs refuse a cross-platform target up front on a server
that lacks the tools instead of failing minutes into the task.

Also: `Agent_Install.md`, a guide for installs done by an AI assistant; the
proxy agent is at 0.2.0 (zero-chunk writes for replica updates); one test
conftest isolates the suite from any real catalog.

Upgrading: native `git pull && sudo ./packaging/install.sh`; Docker
`git pull && docker compose pull && docker compose up -d`. The catalog schema
extends itself on start. Proxies keep working on agent 0.1.0; upgrade them
(`install.sh --agent`, or redeploy) to get incremental replica syncs.

## 0.1.0 (2026-10-21)

First public release: backups of running and powered-off VMs from vCenter,
standalone ESXi and Proxmox VE 9 into one deduplicated repository; restore as
new VM onto the source or any other connection; OVF/OVA export; jobs,
schedules, retention, garbage collection; web UI, CLI, REST API with OpenAPI;
vSphere Client plug-in; Nexus Dashboard plug-ins; native, Debian package and
Docker installs; proxy VM deployment from the UI.
