# vm-backup: hvbackup module for Nexus Dashboard (Python tier)

Shows and drives the hypervisor backup server from the dashboard with the
dashboard's own tables, cards, alerts and history. Everything comes from the
hvbackup REST API over one API token; no files are read, no commands run,
no sudoers lines are needed.

What you get:

- **VM Backup** page: protection, restore points, repository and activity cards;
  connections and proxies; every VM with its protection state (protected,
  stale, overdue, failed, unprotected), last backup, jobs and last run; jobs
  with next and last run. Admins get *Back up now* and *Run now*.
- **Backup History** page: tasks (click one for its live log), runs, restore
  points (admins: *Export OVA*), exports with download links.
- A front-page card, a `vm-backup` block in `/api/summary`, alerts for an
  unreachable server, failed backups, overdue VMs and offline proxies, history
  metrics (`hvb_repo_used`, `hvb_protected_vms`, `hvb_protected_bytes`,
  `hvb_restore_points`) and Prometheus lines (`storagedash_hvb_*`).
- The `hvbackup` systemd unit in the Service Manager when this node runs it.

Restores stay in the hvbackup console and the vSphere Client plug-in: they need
the target picker, which the dashboard does not duplicate. *Open console* is on
every page.

The declarative sibling in `../hvbackup/` (one `plugin.yaml`) remains the
zero-code option for nodes where a Python plug-in is not wanted. Install one or
the other, not both: both put a "VM Backup" page in the sidebar.

## Install

Nexus Dashboard 3.x on any node that can reach the backup server.

1. Copy this directory to `<dashboard app dir>/plugins/vm-backup`, root-owned
   with 0644 files (the loader refuses world-writable plug-in files), and
   restart the dashboard service:

   ```
   sudo cp -r plugins/nexusdash/vm-backup /opt/nexus-dashboard/plugins/
   sudo chown -R root:root /opt/nexus-dashboard/plugins/vm-backup
   sudo systemctl restart nexus-dashboard
   ```

2. Enable **VM Backup** on the dashboard's Modules page (plug-ins start
   disabled).

3. Make a token on the backup server and give it to the module. A user with the
   *viewer* role shows everything; *operator* also allows Back up now, Run now
   and Export OVA. The dashboard enforces admin on those buttons in front of the
   server's own check.

   ```
   hvbackup users add dashboard --role operator --password '…'
   hvbackup tokens add dashboard --username dashboard
   ```

   Then on the VM Backup page: **Settings**, paste the token, set the server URL
   as seen from the dashboard node (default `https://127.0.0.1:8460`) and the
   public URL browsers should open, **Test**, **Save**. Settings are written to
   `~/.config/hvbackup/client.conf` of the dashboard's service user (mode 0600):
   the file the hvbackup CLI reads there, shared with the declarative tier, so
   one token serves all three. The plug-in directory stays root-owned. The same
   file can be written by hand:

   ```
   sudo -u dashboard sh -c 'mkdir -p ~/.config/hvbackup && printf "HVB_URL=https://127.0.0.1:8460\nHVB_TOKEN=hvb_...\nHVB_INSECURE=1\nHVB_PUBLIC_URL=https://backup.example.net:8460\nHVB_OVERDUE_HOURS=36\n" > ~/.config/hvbackup/client.conf && chmod 600 ~/.config/hvbackup/client.conf'
   ```

## Notes

- Overdue hours (Settings, `HVB_OVERDUE_HOURS`, default 36) is how old the newest restore point of
  a VM in an enabled scheduled job may be before the VM counts as overdue and
  raises an alert. Set it to a bit more than your longest schedule interval.
- Server answers are cached for 10 s (30 s for the repository status, which
  walks the chunk store), so the dashboard's summary poll and history sampler
  cost the server little.
- When the server is down, the page says so, the card goes red and one alert
  is raised. Before a token is configured there is no alert, only the page
  notice.
