// vm-backup — hvbackup pages for Nexus Dashboard. Globals are prefixed `vmb`;
// the page/dashcard entry points follow the dashboard's name conventions.
// Everything shown comes from /api/vm-backup/* on this dashboard, which proxies
// the hvbackup REST API with the token configured in Settings.

let _vmbOverview = null;
let _vmbTimer = null;

function vmbAgo(t) {
  if (!t) return 'never';
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return (s / 3600).toFixed(1) + 'h ago';
  return (s / 86400).toFixed(1) + 'd ago';
}
function vmbDur(a, b) {
  if (!a) return '';
  const s = Math.max(0, (b || Date.now() / 1000) - a);
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's';
  return (s / 3600).toFixed(1) + 'h';
}
function vmbBadge(text, cls) {
  return `<span class="status-badge ${cls || ''}">${escapeHtml(String(text))}</span>`;
}
function vmbStateBadge(s) {
  const map = { protected: 'green', stale: 'yellow', overdue: 'red', failed: 'red', unprotected: '',
    done: 'green', running: 'yellow', queued: 'yellow', failed_run: 'red', cancelled: '', poweredOn: 'green', poweredOff: '' };
  return vmbBadge(s === 'poweredOn' ? 'on' : s === 'poweredOff' ? 'off' : s, map[s] || '');
}
function vmbErr(e) { return escapeHtml((e && e.message) || String(e)); }

// ── notice when the server cannot be used ──
function vmbUnavailable(d) {
  const admin = currentRole === 'admin';
  const setup = !d.configured;
  return `<div class="alert ${setup ? 'alert-info' : 'alert-danger'}">
    <strong>${setup ? 'Not connected yet' : 'Backup server unreachable'}</strong>
    <div>${setup ? 'This module talks to the hvbackup server with an API token. ' + (admin ? 'Open Settings and paste a token made with <code>hvbackup tokens add dashboard --username &lt;operator&gt;</code>.' : 'Ask an administrator to configure it.') : escapeHtml(d.error || '')}</div>
  </div>`;
}

function vmbHeader(title, d, extra) {
  const admin = currentRole === 'admin';
  const console_ = d && d.url ? `<a class="btn btn-sm btn-outline" href="${escapeHtml(d.url)}" target="_blank" rel="noopener">Open console</a> ` : '';
  return `<div class="page-header"><h2>${escapeHtml(title)}</h2><div>
    ${extra || ''}${console_}
    ${admin ? `<button class="btn btn-sm btn-outline" onclick="vmbSettings()">Settings</button> ` : ''}
    <button class="btn btn-sm btn-outline" onclick="renderPage('${currentPage}')">Refresh</button></div></div>`;
}

// ── page: VM Backup (overview, protection, jobs) ──
window['page_vm-backup'] = async function () {
  let d;
  try { d = await API.get('/api/vm-backup/overview'); }
  catch (e) { $('page-content').innerHTML = `<h2>VM Backup</h2><div class="alert alert-danger">${vmbErr(e)}</div>`; return; }
  _vmbOverview = d;
  if (!d.available) { $('page-content').innerHTML = vmbHeader('VM Backup', d) + vmbUnavailable(d); return; }
  let vms = [], jobs = [];
  try { vms = (await API.get('/api/vm-backup/vms')).vms; jobs = (await API.get('/api/vm-backup/jobs')).jobs; } catch (e) { /* cards still render */ }
  const pct = d.repo_total ? (d.repo_total - d.repo_free) / d.repo_total * 100 : 0;
  const st = d.states || {};
  $('page-content').innerHTML = vmbHeader('VM Backup', d) + `
    <div class="card-grid">
      <div class="card"><div class="card-head"><span class="status-dot ${st.failed || st.overdue ? 'red' : 'green'}"></span>Protection</div>
        <div class="card-value">${d.protected_vms} <span class="card-unit">of ${d.vms} VMs</span></div>
        <div class="card-sub">${st.overdue || 0} overdue · ${st.failed || 0} failed · ${st.unprotected || 0} unprotected</div></div>
      <div class="card"><div class="card-head">Restore points</div>
        <div class="card-value">${d.restore_points}</div>
        <div class="card-sub">${fmtBytesIEC(d.protected_bytes)} of VM disks protected</div></div>
      <div class="card"><div class="card-head">Repository</div>
        <div class="card-value">${fmtBytesIEC(d.repo_used)} <span class="card-unit">stored</span></div>
        <div class="card-sub">${d.chunks.toLocaleString()} chunks · ${fmtBytesIEC(d.repo_free)} free on ${escapeHtml(d.repo_path || '')}</div>${usageBar(pct)}</div>
      <div class="card"><div class="card-head"><span class="status-dot ${d.proxies.length && !d.proxies_online ? 'red' : 'green'}"></span>Activity</div>
        <div class="card-value">${d.running} <span class="card-unit">running</span></div>
        <div class="card-sub">${d.failed_24h} failed in 24h · ${d.proxies_online}/${d.proxies.length} proxies online · ${d.jobs} jobs enabled</div>
        <div class="card-sub">hvbackup ${escapeHtml(d.version || '')} · last GC ${vmbAgo(d.last_gc)}</div></div>
    </div>
    <h3 style="margin-top:22px">Connections</h3>
    <table class="table"><thead><tr><th>Name</th><th>Type</th><th>Host</th><th>VMs</th><th>Proxies online</th><th>Inventory</th><th>Error</th></tr></thead>
      <tbody>${d.connections.map(c => `<tr><td>${escapeHtml(c.name)}</td><td>${escapeHtml(c.type)}</td><td class="mono">${escapeHtml(c.host)}</td><td>${c.vm_count}</td><td>${c.proxies_online}</td><td>${vmbAgo(c.last_inventory)}</td><td style="color:var(--red)">${escapeHtml(c.last_error || '')}</td></tr>`).join('') || '<tr><td colspan="7" class="help">No connections. Add a vCenter or ESXi host in the console.</td></tr>'}</tbody></table>
    ${d.proxies.length ? `<p class="help">Proxies: ${d.proxies.map(p => `${escapeHtml(p.name)} ${vmbBadge(p.online ? 'online' : 'offline', p.online ? 'green' : 'red')}`).join(' · ')}</p>` : ''}
    <h3 style="margin-top:22px">Protection <input id="vmb-filter" class="form-control" placeholder="filter" style="width:200px;display:inline-block;margin-left:12px" oninput="vmbFilterVms()"></h3>
    <div id="vmb-vms"></div>
    <h3 style="margin-top:22px">Jobs</h3>
    <table class="table"><thead><tr><th>Name</th><th>Connection</th><th>Schedule</th><th>VMs</th><th>Retention</th><th>Next run</th><th>Last run</th><th></th></tr></thead>
      <tbody>${jobs.map(j => `<tr>
        <td>${escapeHtml(j.name)}${j.enabled ? '' : ' ' + vmbBadge('disabled')}</td><td>${escapeHtml(j.connection_name || '')}</td>
        <td class="mono">${escapeHtml(j.schedule || 'manual')}</td><td title="${escapeHtml((j.vm_names || []).join(', '))}">${(j.vm_ids || []).length}</td>
        <td>${j.retention ? (j.retention.keep_last ? 'last ' + j.retention.keep_last : '') + (j.retention.keep_days ? ' ' + j.retention.keep_days + ' days' : '') : ''}</td>
        <td>${j.next_run ? fmtTs(j.next_run) : '-'}</td><td>${j.last_run ? vmbAgo(j.last_run) + ' ' + vmbStateBadge(j.last_status === 'failed' ? 'failed_run' : j.last_status || '') : 'never'}</td>
        <td>${currentRole === 'admin' ? `<button class="btn btn-sm" onclick="vmbRunJob(${j.id}, '${jsArg(j.name)}')">Run now</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="8" class="help">No jobs yet. Create one in the console.</td></tr>'}</tbody></table>`;
  window._vmbVms = vms;
  vmbFilterVms();
};

function vmbFilterVms() {
  const q = (($('vmb-filter') || {}).value || '').toLowerCase();
  const rows = (window._vmbVms || []).filter(v => !q || v.name.toLowerCase().includes(q) || (v.connection || '').toLowerCase().includes(q) || v.state.includes(q));
  const order = { failed: 0, overdue: 1, unprotected: 2, stale: 3, protected: 4 };
  rows.sort((a, b) => (order[a.state] - order[b.state]) || a.name.localeCompare(b.name));
  const el = $('vmb-vms');
  if (!el) return;
  el.innerHTML = `<table class="table"><thead><tr><th>VM</th><th>Connection</th><th>Power</th><th>Size</th><th>State</th><th>Last backup</th><th>Points</th><th>Jobs</th><th>Last run</th><th></th></tr></thead>
    <tbody>${rows.map(v => `<tr>
      <td>${escapeHtml(v.name)}</td><td>${escapeHtml(v.connection || '')}</td><td>${vmbStateBadge(v.power_state)}</td><td>${fmtBytesIEC(v.size_bytes)}</td>
      <td>${vmbStateBadge(v.state)}</td><td>${v.last_backup ? vmbAgo(v.last_backup) + ' <span class="help" style="display:inline">(' + escapeHtml(v.last_kind || '') + ')</span>' : 'never'}</td>
      <td>${v.restore_points}</td><td>${escapeHtml((v.jobs || []).join(', '))}</td>
      <td>${v.last_run && v.last_run.status ? vmbStateBadge(v.last_run.status === 'failed' ? 'failed_run' : v.last_run.status) + (v.last_run.error ? ` <span class="help" style="display:inline" title="${escapeHtml(v.last_run.error)}">${escapeHtml(v.last_run.error.slice(0, 60))}</span>` : '') : ''}</td>
      <td>${currentRole === 'admin' ? `<button class="btn btn-sm" onclick="vmbBackupNow(${v.id}, '${jsArg(v.name)}')">Back up now</button>` : ''}</td></tr>`).join('') || `<tr><td colspan="10" class="help">${q ? 'No VM matches.' : 'No VMs in inventory yet.'}</td></tr>`}</tbody></table>`;
}

async function vmbBackupNow(id, name) {
  if (!confirm(`Back up ${name} now? A quiesced snapshot is taken and removed when the backup is done.`)) return;
  try {
    const r = await API.post(`/api/vm-backup/vms/${id}/backup`, { quiesce: true });
    vmbWatchTask(r.task_id, `Backup of ${name}`);
  } catch (e) { alert(e.message || String(e)); }
}
async function vmbRunJob(id, name) {
  if (!confirm(`Run job ${name} now?`)) return;
  try {
    const r = await API.post(`/api/vm-backup/jobs/${id}/run`);
    vmbWatchTask(r.task_id, `Job ${name}`);
  } catch (e) { alert(e.message || String(e)); }
}
async function vmbExport(rp, name) {
  if (!confirm(`Write an OVA copy of restore point ${rp} of ${name} into the server's exports folder? The backup itself is not changed.`)) return;
  try {
    const r = await API.post(`/api/vm-backup/restore-points/${rp}/export`, { format: 'ova' });
    vmbWatchTask(r.task_id, `Export of ${name}`);
  } catch (e) { alert(e.message || String(e)); }
}

// Follow a task in a modal until it finishes (polls every 2 s).
function vmbWatchTask(id, what) {
  if (_vmbTimer) clearTimeout(_vmbTimer);
  openModal(what || `Task ${id}`, `<div id="vmb-task"><p class="help">starting…</p></div>`);
  _onModalClose = () => { if (_vmbTimer) clearTimeout(_vmbTimer); _vmbTimer = null; renderPage(currentPage); };
  const tick = async () => {
    const el = $('vmb-task');
    if (!el) return;
    try {
      const t = (await API.get(`/api/vm-backup/tasks/${id}`)).task;
      const pct = Math.round((t.progress || 0) * 100);
      el.innerHTML = `<p>${vmbStateBadge(t.state === 'failed' ? 'failed_run' : t.state)} ${escapeHtml(t.message || '')}</p>${usageBar(pct)}
        <pre class="mono" style="max-height:320px;overflow:auto;font-size:11.5px;white-space:pre-wrap">${escapeHtml(t.log || '')}</pre>
        ${t.state === 'running' && currentRole === 'admin' ? `<button class="btn btn-sm btn-danger" onclick="vmbCancelTask(${t.id})">Cancel</button>` : ''}`;
      if (t.state === 'queued' || t.state === 'running') _vmbTimer = setTimeout(tick, 2000);
    } catch (e) { el.innerHTML = `<div class="alert alert-danger">${vmbErr(e)}</div>`; }
  };
  tick();
}
async function vmbCancelTask(id) {
  try { await API.post(`/api/vm-backup/tasks/${id}/cancel`); } catch (e) { alert(e.message || String(e)); }
}

// ── page: Backup History (runs, restore points, exports, tasks) ──
window['page_vm-backup-history'] = async function () {
  let d;
  try { d = await API.get('/api/vm-backup/overview'); }
  catch (e) { $('page-content').innerHTML = `<h2>Backup History</h2><div class="alert alert-danger">${vmbErr(e)}</div>`; return; }
  if (!d.available) { $('page-content').innerHTML = vmbHeader('Backup History', d) + vmbUnavailable(d); return; }
  let runs = [], rps = [], exps = [], tasks = [];
  try {
    [runs, rps, exps, tasks] = await Promise.all([
      API.get('/api/vm-backup/runs?limit=50').then(r => r.runs), API.get('/api/vm-backup/restore-points').then(r => r.restore_points),
      API.get('/api/vm-backup/exports').then(r => r.exports), API.get('/api/vm-backup/tasks?limit=30').then(r => r.tasks)]);
  } catch (e) { $('page-content').innerHTML = vmbHeader('Backup History', d) + `<div class="alert alert-danger">${vmbErr(e)}</div>`; return; }
  const admin = currentRole === 'admin';
  $('page-content').innerHTML = vmbHeader('Backup History', d) + `
    <h3>Tasks</h3>
    <table class="table"><thead><tr><th>ID</th><th>Kind</th><th>Subject</th><th>State</th><th>Progress</th><th>Message</th><th>Created</th><th>By</th></tr></thead>
      <tbody>${tasks.map(t => `<tr style="cursor:pointer" onclick="vmbWatchTask(${t.id}, '${jsArg(t.kind + ' ' + (t.subject || ''))}')">
        <td>${t.id}</td><td>${escapeHtml(t.kind)}</td><td>${escapeHtml(t.subject || '')}</td><td>${vmbStateBadge(t.state === 'failed' ? 'failed_run' : t.state)}</td>
        <td>${Math.round((t.progress || 0) * 100)}%</td><td class="help" style="margin:0">${escapeHtml((t.message || '').slice(0, 90))}</td><td>${fmtTs(t.created)}</td><td>${escapeHtml(t.created_by || '')}</td></tr>`).join('') || '<tr><td colspan="8" class="help">No tasks yet.</td></tr>'}</tbody></table>
    <h3 style="margin-top:22px">Runs</h3>
    <table class="table"><thead><tr><th>VM</th><th>Job</th><th>Started</th><th>Duration</th><th>Status</th><th>Kind</th><th>Transport</th><th>Read</th><th>Stored</th><th>Error</th></tr></thead>
      <tbody>${runs.map(r => `<tr><td>${escapeHtml(r.vm_name)}</td><td>${escapeHtml(r.job_name || '')}</td><td>${fmtTs(r.started)}</td><td>${vmbDur(r.started, r.finished)}</td>
        <td>${vmbStateBadge(r.status === 'failed' ? 'failed_run' : r.status)}</td><td>${escapeHtml(r.kind || '')}</td><td>${escapeHtml(r.transport || '')}</td>
        <td>${fmtBytesIEC(r.bytes_read)}</td><td>${fmtBytesIEC(r.bytes_stored)}</td><td style="color:var(--red)">${escapeHtml((r.error || '').slice(0, 80))}</td></tr>`).join('') || '<tr><td colspan="10" class="help">No runs yet.</td></tr>'}</tbody></table>
    <h3 style="margin-top:22px">Restore points <span class="help" style="display:inline">Restores are done from the console or the vSphere Client; exports write a copy and never change the backup.</span></h3>
    <table class="table"><thead><tr><th>ID</th><th>VM</th><th>Created</th><th>Kind</th><th>Disk size</th><th>Allocated</th><th></th></tr></thead>
      <tbody>${rps.map(r => `<tr><td>${r.id}</td><td>${escapeHtml(r.vm_name)}</td><td>${fmtTs(r.created)} <span class="help" style="display:inline">${vmbAgo(r.created)}</span></td><td>${escapeHtml(r.kind)}</td>
        <td>${fmtBytesIEC(r.size_bytes)}</td><td>${fmtBytesIEC(r.allocated_bytes)}</td>
        <td>${admin ? `<button class="btn btn-sm" onclick="vmbExport(${r.id}, '${jsArg(r.vm_name)}')">Export OVA</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="7" class="help">No restore points yet.</td></tr>'}</tbody></table>
    <h3 style="margin-top:22px">Exports</h3>
    <table class="table"><thead><tr><th>ID</th><th>VM</th><th>Restore point</th><th>Format</th><th>Status</th><th>Size</th><th>Created</th><th>Location</th><th></th></tr></thead>
      <tbody>${exps.map(e => `<tr><td>${e.id}</td><td>${escapeHtml(e.vm_name)}</td><td>${e.restore_point_id}</td><td>${escapeHtml(e.format)}</td>
        <td>${vmbStateBadge(e.status === 'failed' ? 'failed_run' : e.status)}${e.error ? ' <span class="help" style="display:inline">' + escapeHtml(e.error) + '</span>' : ''}</td>
        <td>${fmtBytesIEC(e.size_bytes)}</td><td>${fmtTs(e.created)}</td><td class="mono">${escapeHtml(e.path)}</td>
        <td>${e.status === 'done' && e.format === 'ova' && d.url ? `<a class="btn btn-sm btn-outline" href="${escapeHtml(d.url)}/api/v1/exports/${e.id}/download" target="_blank" rel="noopener">Download</a>` : ''}</td></tr>`).join('') || '<tr><td colspan="9" class="help">No exports yet.</td></tr>'}</tbody></table>`;
};

// ── settings (admin) ──
async function vmbSettings() {
  let s;
  try { s = await API.get('/api/vm-backup/settings'); } catch (e) { alert(e.message || String(e)); return; }
  openModal('VM Backup settings', `
    <p class="help">The dashboard reads the backup server through its REST API with a token. Make one on the server with
      <code>hvbackup tokens add dashboard --username &lt;user&gt;</code>: a viewer-role user shows everything, an operator-role user also allows Back up now, Run now and Export.
      Saved to <code>${escapeHtml(s.config_file || '~/.config/hvbackup/client.conf')}</code> of the dashboard's service user, the same file the hvbackup CLI reads there.</p>
    <div class="form-group"><label>Server URL (from this node)</label><input class="form-control" id="vmb-url" value="${escapeHtml(s.url)}"></div>
    <div class="form-group"><label>Public URL (what browsers open; blank = same)</label><input class="form-control" id="vmb-pub" value="${escapeHtml(s.public_url !== s.url ? s.public_url : '')}" placeholder="https://backup.example.net:8460"></div>
    <div class="form-group"><label>API token ${s.token_prefix ? `(current: <span class="mono">${escapeHtml(s.token_prefix)}</span>; blank keeps it)` : ''}</label><input class="form-control" id="vmb-token" type="password" autocomplete="off"></div>
    <div class="form-group"><label><input type="checkbox" id="vmb-verify" ${s.verify_ssl ? 'checked' : ''}> Verify the server's TLS certificate (off for the self-signed default)</label></div>
    <div class="form-group"><label>A scheduled VM counts as overdue after (hours)</label><input class="form-control" id="vmb-overdue" type="number" min="1" max="2160" value="${escapeHtml(String(s.overdue_hours))}" style="width:120px"></div>
    <div id="vmb-test" class="help"></div>
    <div style="margin-top:12px"><button class="btn btn-sm btn-outline" onclick="vmbTestSettings()">Test</button> <button class="btn btn-sm" onclick="vmbSaveSettings()">Save</button></div>`);
}
function vmbSettingsBody() {
  const b = { url: $('vmb-url').value.trim(), public_url: $('vmb-pub').value.trim(), verify_ssl: $('vmb-verify').checked, overdue_hours: Number($('vmb-overdue').value) };
  if ($('vmb-token').value.trim()) b.token = $('vmb-token').value.trim();
  return b;
}
async function vmbTestSettings() {
  const el = $('vmb-test'); el.textContent = 'testing…';
  try {
    const r = await API.post('/api/vm-backup/settings/test', vmbSettingsBody());
    el.innerHTML = `${vmbBadge('ok', 'green')} signed in as <b>${escapeHtml(r.username)}</b> (${escapeHtml(r.role)}), hvbackup ${escapeHtml(r.version || '')}${r.note ? '<br>' + escapeHtml(r.note) : ''}`;
  } catch (e) { el.innerHTML = `${vmbBadge('failed', 'red')} ${vmbErr(e)}`; }
}
async function vmbSaveSettings() {
  try { await API.post('/api/vm-backup/settings', vmbSettingsBody()); closeModal(); renderPage(currentPage); }
  catch (e) { $('vmb-test').innerHTML = `${vmbBadge('failed', 'red')} ${vmbErr(e)}`; }
}

// ── front-page card ──
window['dashcard_vm-backup'] = function (ctx) {
  const s = ((ctx && ctx.s) || {})['vm-backup'];
  if (!s) return '';
  if (!s.available) {
    return `<div class="card card-link" onclick="showPage('vm-backup')">
      <div class="card-head"><span class="status-dot ${s.configured ? 'red' : ''}"></span>VM Backup</div>
      <div class="card-value">${s.configured ? 'down' : 'not set up'}</div>
      <div class="card-sub">${escapeHtml(s.configured ? 'backup server unreachable' : 'open the page to connect')}</div></div>`;
  }
  const st = s.states || {};
  const bad = (st.failed || 0) + (st.overdue || 0);
  return `<div class="card card-link" onclick="showPage('vm-backup')">
    <div class="card-head"><span class="status-dot ${bad ? 'red' : 'green'}"></span>VM Backup</div>
    <div class="card-value">${s.protected_vms} <span class="card-unit">of ${s.vms} VMs</span></div>
    <div class="card-sub">${s.restore_points} restore points · ${fmtBytesIEC(s.repo_used)} stored${s.running ? ' · ' + s.running + ' running' : ''}</div>
    <div class="card-sub">${bad ? `${st.failed || 0} failed · ${st.overdue || 0} overdue` : 'all scheduled backups current'}</div></div>`;
};
