# hvbackup plug-in for Nexus Dashboard

Declarative tier (one `plugin.yaml`, no code). Shows the backup server's service
state, the one-line status, recent runs, the server journal, and embeds the full
hvbackup console in an iframe.

## Install

1. Copy this directory to `<dashboard app dir>/plugins/hvbackup` (root-owned, 0644 files)
   on the node that runs hvbackup, and restart the dashboard service.
2. Enable **VM Backup** on the dashboard's Modules page.
3. Give the dashboard's service user a read-only hvbackup token:

   ```
   hvbackup tokens add dashboard --username <a viewer-role user>
   sudo -u dashboard mkdir -p ~dashboard/.config/hvbackup
   sudo -u dashboard sh -c 'printf "HVB_URL=https://127.0.0.1:8460\nHVB_TOKEN=hvb_...\nHVB_INSECURE=1\n" > ~/.config/hvbackup/client.conf && chmod 600 ~/.config/hvbackup/client.conf'
   ```

4. Edit the iframe `src` in `plugin.yaml` to the server's public URL so browsers can reach it.

No sudoers lines are needed: the commands run as the dashboard user with a viewer
token, which can only read.

## Notes

- "Recent runs" expects `hvbackup --json runs list` to emit JSON; the dashboard's
  `json_lines` parser takes one object per line, so this widget shows rows only on
  dashboards that accept a JSON array as well. If yours does not, drop the widget;
  the Status table and the iframe carry the same information.
- The Python-tier plug-in lives next door in `../vm-backup/`: native pages,
  front-page card, summary block, alerts for failed or overdue backups and
  offline proxies, history metrics, Back up now / Run now / Export OVA. Prefer
  it where a Python plug-in is acceptable; install one or the other, not both.
