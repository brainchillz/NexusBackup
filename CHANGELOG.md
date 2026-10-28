# Changelog

Releases of NexusBackup (command and package `hvbackup`). Dates are the day
the release was tagged.

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
