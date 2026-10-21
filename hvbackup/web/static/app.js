/* hvbackup UI helpers: API access, formatting, tables, modals, polling. No framework. */
'use strict';
const ROLES = ['viewer', 'restore', 'operator', 'admin'];
const USER = window.USER || { username: '', role: 'viewer' };
function can(role) { return ROLES.indexOf(USER.role) >= ROLES.indexOf(role); }

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(method, path, body) {
  const opts = { method, credentials: 'same-origin', headers: {} };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  if (r.status === 401) {
    if (!location.pathname.startsWith('/login')) location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    throw new Error('not signed in');
  }
  let data = null;
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('json')) data = await r.json(); else data = await r.text();
  if (!r.ok) {
    const msg = data && data.detail ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : (r.status + ' ' + r.statusText);
    throw new Error(msg);
  }
  return data;
}

function fmtBytes(n) {
  if (n === null || n === undefined || isNaN(n)) return '';
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let i = 0; n = Number(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(n < 10 ? 2 : 1)) + ' ' + u[i];
}
function fmtTime(t) {
  if (!t) return '';
  const d = new Date(t * 1000);
  const p = x => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function ago(t) {
  if (!t) return 'never';
  let s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return (s / 3600).toFixed(1) + 'h ago';
  return (s / 86400).toFixed(1) + 'd ago';
}
function until(t) {
  if (!t) return '';
  let s = t - Date.now() / 1000;
  if (s < 0) return 'due';
  if (s < 3600) return 'in ' + Math.round(s / 60) + 'm';
  if (s < 86400) return 'in ' + (s / 3600).toFixed(1) + 'h';
  return 'in ' + (s / 86400).toFixed(1) + 'd';
}
function dur(a, b) {
  if (!a) return '';
  const s = Math.max(0, (b || Date.now() / 1000) - a);
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's';
  return Math.floor(s / 3600) + 'h ' + Math.round((s % 3600) / 60) + 'm';
}
function titleTime(t) { return `<span title="${esc(fmtTime(t))}">${esc(ago(t))}</span>`; }

function badge(text, cls) { return `<span class="badge ${cls || ''}">${esc(text)}</span>`; }

/* ─── Appearance: dark/light + theme drop-ins (Nexus Dashboard contract) ─── */
function applyTheme(t) {
  const h = document.documentElement;
  document.getElementById('theme-css')?.remove();
  if (!t) {
    h.removeAttribute('data-theme'); h.removeAttribute('data-theme-variant');
    localStorage.removeItem('hvbTheme.v1');
    if (typeof window.NexusThemes === 'object') for (const k in window.NexusThemes) try { window.NexusThemes[k].unmount?.(); } catch (e) { /* theme bug */ }
    return;
  }
  const variant = t.variant || t.default_variant || (t.variants[0] && t.variants[0].id) || '';
  h.setAttribute('data-theme', t.id);
  if (variant) h.setAttribute('data-theme-variant', variant); else h.removeAttribute('data-theme-variant');
  if (t.modes.indexOf('light') < 0) { h.classList.remove('theme-light'); }
  if (t.modes.indexOf('dark') < 0) { h.classList.add('theme-light'); }
  const l = document.createElement('link'); l.rel = 'stylesheet'; l.id = 'theme-css';
  l.href = `/themes/${t.id}/theme.css?v=${encodeURIComponent(t.v || '0')}`;
  document.head.appendChild(l);
  localStorage.setItem('hvbTheme.v1', JSON.stringify({ id: t.id, variant, modes: t.modes, js: !!t.js, v: t.v }));
  if (t.js && !document.getElementById('theme-js')) {
    const s = document.createElement('script'); s.id = 'theme-js'; s.src = `/themes/${t.id}/theme.js?v=${encodeURIComponent(t.v || '0')}`;
    s.onload = () => mountTheme(t.id, variant);
    document.head.appendChild(s);
  } else mountTheme(t.id, variant);
}
function mountTheme(id, variant) {
  const th = window.NexusThemes && window.NexusThemes[id];
  if (!th || !th.mount) return;
  try { th.mount({ isAuthed: () => !!(USER && USER.username), currentPage: () => location.pathname, variant: () => variant }); } catch (e) { console.warn('theme', e); }
  document.dispatchEvent(new CustomEvent('nexus:theme', { detail: { id, variant } }));
}
function initAppearance() {
  const h = document.documentElement;
  const toggle = document.getElementById('mode-toggle');
  const pick = document.getElementById('theme-pick');
  const vpick = document.getElementById('variant-pick');
  if (!toggle) return;
  const themes = window.THEMES || [];
  const saved = JSON.parse(localStorage.getItem('hvbTheme.v1') || 'null');
  const label = () => { toggle.textContent = h.classList.contains('theme-light') ? 'Dark mode' : 'Light mode'; };
  label();
  toggle.onclick = () => {
    h.classList.toggle('theme-light');
    localStorage.setItem('theme', h.classList.contains('theme-light') ? 'light' : 'dark');
    label();
    document.dispatchEvent(new CustomEvent('nexus:mode', { detail: { light: h.classList.contains('theme-light') } }));
  };
  if (!themes.length) return;
  pick.hidden = false;
  pick.innerHTML = '<option value="">Nexus (default)</option>' + themes.map(t => `<option value="${esc(t.id)}" ${saved && saved.id === t.id ? 'selected' : ''}>${esc(t.name)}</option>`).join('');
  const drawVariants = () => {
    const t = themes.find(x => x.id === pick.value);
    vpick.hidden = !(t && t.variants && t.variants.length);
    if (!vpick.hidden) {
      const cur = (saved && saved.id === t.id && saved.variant) || t.default_variant || t.variants[0].id;
      vpick.innerHTML = t.variants.map(v => `<option value="${esc(v.id)}" ${v.id === cur ? 'selected' : ''}>${esc(v.label || v.id)}</option>`).join('');
      vpick.title = t.variant_label || 'Variant';
    }
  };
  drawVariants();
  pick.onchange = () => { const t = themes.find(x => x.id === pick.value); drawVariants(); applyTheme(t ? { ...t, variant: vpick.hidden ? '' : vpick.value } : null); };
  vpick.onchange = () => { const t = themes.find(x => x.id === pick.value); if (t) applyTheme({ ...t, variant: vpick.value }); };
  if (saved && saved.js) { const t = themes.find(x => x.id === saved.id); if (t) mountTheme(t.id, saved.variant); }
}
function stateBadge(s) {
  const m = { done: 'ok', running: 'info', queued: 'info', failed: 'bad', cancelled: 'warn' };
  return badge(s, m[s] || '');
}
function powerBadge(p) {
  if (p === 'poweredOn') return badge('on', 'ok');
  if (p === 'poweredOff') return badge('off', 'gray');
  return badge(p || '?', 'warn');
}
function progressBar(frac, state) {
  const pct = Math.round((frac || 0) * 100);
  return `<div class="progress ${state === 'done' ? 'done' : state === 'failed' ? 'failed' : ''}" title="${pct}%"><div style="width:${pct}%"></div></div>`;
}

/* renderTable(el, columns, rows): columns = [{h:'Header', f: row => html, cls:'num'}] */
function renderTable(el, columns, rows, emptyText) {
  if (!rows || !rows.length) { el.innerHTML = `<div class="empty">${esc(emptyText || 'Nothing here yet.')}</div>`; return; }
  let h = '<div class="tablewrap"><table class="t"><thead><tr>';
  for (const c of columns) h += `<th class="${c.cls || ''}">${esc(c.h)}</th>`;
  h += '</tr></thead><tbody>';
  for (const r of rows) {
    h += `<tr${r.id !== undefined ? ` data-id="${esc(r.id)}"` : ''}>`;
    for (const c of columns) h += `<td class="${c.cls || ''}">${c.f(r)}</td>`;
    h += '</tr>';
  }
  el.innerHTML = h + '</tbody></table></div>';
}

/* modal({title, body, buttons:[{label, cls, onClick(modal) -> may return promise; return false to keep open}]}) */
function modal(o) {
  const bg = document.createElement('div');
  bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal" role="dialog"><header><span>${esc(o.title)}</span><button class="btn sm" data-x aria-label="Close">&times;</button></header>
    <div class="body">${o.body}<div class="err"></div></div><footer></footer></div>`;
  const footer = bg.querySelector('footer');
  const close = () => bg.remove();
  bg.querySelector('[data-x]').onclick = close;
  bg.addEventListener('click', e => { if (e.target === bg && !o.sticky) close(); });
  const buttons = o.buttons || [{ label: 'Close' }];
  for (const b of buttons) {
    const btn = document.createElement('button');
    btn.className = 'btn ' + (b.cls || '');
    btn.textContent = b.label;
    btn.onclick = async () => {
      if (!b.onClick) return close();
      btn.disabled = true;
      try {
        const keep = await b.onClick(bg);
        if (keep !== false) close();
      } catch (e) {
        bg.querySelector('.err').textContent = e.message || String(e);
      } finally { btn.disabled = false; }
    };
    footer.appendChild(btn);
  }
  document.body.appendChild(bg);
  const first = bg.querySelector('input,select,textarea');
  if (first) first.focus();
  return bg;
}
function confirmModal(title, text, onOk, cls) {
  return modal({ title, body: `<p>${esc(text)}</p>`, buttons: [{ label: 'Cancel' }, { label: title, cls: cls || 'primary', onClick: onOk }] });
}
function val(m, name) { const e = m.querySelector(`[name="${name}"]`); if (!e) return undefined; return e.type === 'checkbox' ? e.checked : e.value; }

function toast(msg, ms) {
  const t = document.createElement('div');
  t.className = 'toast'; t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms || 3500);
}

/* pollWhile(fn, ms): fn returns true to keep polling. Pauses when the tab is hidden. */
function pollWhile(fn, ms) {
  let timer = null, stopped = false;
  const tick = async () => {
    if (stopped) return;
    let again = true;
    try { again = await fn(); } catch (e) { console.warn(e); }
    if (again !== false && !stopped) timer = setTimeout(tick, document.hidden ? ms * 5 : ms);
  };
  tick();
  return () => { stopped = true; clearTimeout(timer); };
}

/* After starting a task, show a toast with a link and nudge the tasks poller. */
function startedTask(res, what) {
  const id = res && res.task_id;
  toast((what || 'Started') + (id ? ` (task ${id})` : ''));
  document.dispatchEvent(new CustomEvent('task-started', { detail: id }));
  return id;
}

/* Restore dialog shared by the VM and restore-point pages. The target may be any connection,
   not only the one the backup came from (DR): host, datastore, folder and networks are read
   from the chosen target and defaulted to the source's names where they exist there. */
async function restoreDialog(rp, vmName, connectionId) {
  const conns = await api('GET', '/api/v1/connections');
  let detail = {};
  try { detail = await api('GET', `/api/v1/restore-points/${rp.id}`); } catch (e) { /* older point */ }
  const spec = detail.spec || {};
  const place = spec.placement || {};
  const srcConn = conns.find(c => c.id === connectionId) || {};
  const srcPlatform = detail.platform || (srcConn.type === 'proxmox' ? 'proxmox' : 'vmware');
  const platformOf = c => (c && c.type === 'proxmox') ? 'proxmox' : 'vmware';
  const srcDs = (spec.disks && spec.disks[0] && spec.disks[0].datastore) || (place.datastores && place.datastores[0]) || '';
  const srcNets = [...new Set((spec.nics || []).map(n => n.network || ''))];
  const need = rp.allocated_bytes || 0;
  const m = modal({
    title: 'Restore ' + vmName,
    body: `<p class="muted">Restore point ${esc(rp.id)} from ${esc(fmtTime(rp.created))} (${esc(rp.kind)}, ${esc(fmtBytes(rp.allocated_bytes))} allocated)${place.host ? `, taken on ${esc(place.host)} / ${esc(srcDs)}` : ''}.</p>
      <label class="f"><span>New VM name</span><input name="name" value="${esc(vmName + '-restored')}"></label>
      <div class="row">
        <label class="f"><span>Target connection</span><select name="connection_id">${conns.map(c => `<option value="${c.id}" ${c.id === connectionId ? 'selected' : ''}>${esc(c.name)} (${esc(c.type)})</option>`).join('')}</select></label>
        <label class="f"><span>Host</span><select name="host"></select></label>
        <label class="f"><span>Datastore</span><select name="datastore"></select></label>
      </div>
      <div class="row"><label class="f"><span>VM folder</span><select name="folder"><option value="">(root VM folder)</option></select></label><div id="netmap" class="row" style="flex:2"></div></div>
      <p class="muted" id="drnote" style="display:none"></p>
      <div id="convnote" style="display:none"><p class="muted"><b>Conversion.</b> This restore point is a <span id="convfrom"></span> image and the target is <span id="convto"></span>. The server first makes a <span id="convto2"></span> restore point of it (Linux guests: drivers in the initramfs, interface names pinned, a few MiB of new blocks), keeps it for later restores, then restores that. The copy boots with the target's own disk and network devices.</p>
      <label class="c"><input type="checkbox" name="install_tools"> Install the target's guest tools (QEMU guest agent or open-vm-tools) on the copy's first boot; needs package repositories reachable from the copy</label></div>
      <label class="c"><input type="checkbox" name="new_identity" checked> New identity (fresh MAC addresses and BIOS UUID). Untick only when the original VM is gone.</label>
      <label class="c"><input type="checkbox" name="power_on"> Power on after restore</label>
      <p class="muted">A restored copy keeps the guest's host name, host keys and DHCP client identity. Do not run it next to the original on the same network.</p>`,
    buttons: [{ label: 'Cancel' }, {
      label: 'Restore', cls: 'primary', onClick: async m => {
        if (!m.querySelector('[name="host"]').value) throw new Error('pick a host on the target');
        const networks = {};
        srcNets.forEach((n, i) => { const e = m.querySelector(`[name="net${i}"]`); if (e && e.value) networks[n] = e.value; });
        const body = { name: val(m, 'name'), connection_id: Number(val(m, 'connection_id')), host: val(m, 'host') || null,
          datastore: val(m, 'datastore') || null, folder: val(m, 'folder') || null, networks,
          new_identity: val(m, 'new_identity'), power_on: val(m, 'power_on'), install_tools: val(m, 'install_tools') };
        const res = await api('POST', `/api/v1/restore-points/${rp.id}/restore`, body);
        startedTask(res, 'Restore started');
      }
    }]
  });
  const sel = n => m.querySelector(`[name="${n}"]`);
  const errBox = m.querySelector('.err');
  let placement = null;
  function fillHost() {
    const h = placement && placement.hosts.find(x => x.name === sel('host').value);
    const info = (placement && placement.datastores) || {};
    const dss = (h ? h.datastores : []).map(n => ({ name: n, ...(info[n] || {}) })).sort((a, b) => (b.free || 0) - (a.free || 0));
    sel('datastore').innerHTML = dss.map(d => `<option value="${esc(d.name)}" ${d.accessible === false || (d.free != null && d.free < need) ? 'disabled' : ''}>${esc(d.name)}${d.free != null ? ` — ${esc(fmtBytes(d.free))} free` : ''}${d.accessible === false ? ' (inaccessible)' : d.free != null && d.free < need ? ' (too small)' : ''}</option>`).join('');
    const srcOk = dss.find(d => d.name === srcDs && d.accessible !== false && !(d.free != null && d.free < need));
    if (srcOk) sel('datastore').value = srcDs;
    const nets = h ? h.networks : [];
    m.querySelector('#netmap').innerHTML = srcNets.map((n, i) => {
      const found = nets.includes(n);
      const auto = !found && nets.length === 1;
      return `<label class="f"><span>Network for ${esc(n || '(unknown)')}${found || auto ? '' : ' <b>— not on this host, choose</b>'}</span><select name="net${i}">${(found || auto ? '' : '<option value="">(choose)</option>') + nets.map(x => `<option value="${esc(x)}" ${x === n || auto ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select></label>`;
    }).join('');
  }
  async function loadPlacement() {
    const cid = Number(sel('connection_id').value);
    sel('host').innerHTML = '<option value="">loading…</option>'; sel('datastore').innerHTML = ''; m.querySelector('#netmap').innerHTML = '';
    errBox.textContent = '';
    try { placement = await api('GET', `/api/v1/connections/${cid}/placement`); }
    catch (e) { placement = null; sel('host').innerHTML = ''; errBox.textContent = 'Cannot read the target: ' + e.message; return; }
    const hosts = placement.hosts || [];
    sel('host').innerHTML = hosts.map(h => {
      const bad = (h.connection_state && h.connection_state !== 'connected') || h.maintenance;
      return `<option value="${esc(h.name)}" ${bad ? 'disabled' : ''}>${esc(h.name)}${h.cluster ? ` (${esc(h.cluster)})` : ''}${h.maintenance ? ' — maintenance' : ''}${h.connection_state && h.connection_state !== 'connected' ? ` — ${esc(h.connection_state)}` : ''}</option>`;
    }).join('');
    const usable = hosts.filter(h => !((h.connection_state && h.connection_state !== 'connected') || h.maintenance));
    const pick = (cid === connectionId && usable.find(h => h.name === place.host)) || usable.find(h => h.datastores.includes(srcDs)) || usable[0];
    if (pick) sel('host').value = pick.name;
    sel('folder').innerHTML = '<option value="">(root VM folder)</option>' + (placement.folders || []).map(f => `<option value="${esc(f.moref)}" ${cid === connectionId && f.moref === place.folder ? 'selected' : ''}>${esc(f.name)}</option>`).join('');
    const note = m.querySelector('#drnote');
    note.style.display = cid === connectionId ? 'none' : '';
    note.textContent = 'This is not the connection the backup came from. Only the repository is read; the source is not contacted. Check the host, datastore and networks the copy should use.';
    const tconn = conns.find(c => c.id === cid);
    const tplat = platformOf(tconn);
    const conv = m.querySelector('#convnote');
    conv.style.display = tplat === srcPlatform ? 'none' : '';
    m.querySelector('#convfrom').textContent = srcPlatform === 'proxmox' ? 'Proxmox' : 'VMware';
    m.querySelector('#convto').textContent = tplat === 'proxmox' ? 'Proxmox' : 'VMware';
    m.querySelector('#convto2').textContent = tplat === 'proxmox' ? 'Proxmox' : 'VMware';
    m.querySelector('[name="host"]').closest('label').querySelector('span').textContent = tplat === 'proxmox' ? 'Node' : 'Host';
    m.querySelector('[name="datastore"]').closest('label').querySelector('span').textContent = tplat === 'proxmox' ? 'Storage' : 'Datastore';
    m.querySelector('[name="folder"]').closest('label').querySelector('span').textContent = tplat === 'proxmox' ? 'Pool' : 'VM folder';
    fillHost();
  }
  sel('connection_id').onchange = loadPlacement;
  sel('host').onchange = fillHost;
  await loadPlacement();
}

/* Job editor shared by the Jobs page and the "Protect" flow on the VMs page. */
async function jobDialog(job, preset) {
  const conns = await api('GET', '/api/v1/connections');
  const vms = await api('GET', '/api/v1/vms');
  const j = job || { name: '', connection_id: (preset && preset.connection_id) || (conns[0] && conns[0].id), vm_ids: (preset && preset.vm_ids) || [],
    schedule: '0 1 * * *', retention: { keep_last: 14, keep_days: 0 }, enabled: true, quiesce: true };
  const presets = [['0 1 * * *', 'Daily at 01:00'], ['0 */6 * * *', 'Every 6 hours'], ['0 1 * * 0', 'Weekly, Sunday 01:00'], ['', 'Manual only']];
  const body = `
    <label class="f"><span>Name</span><input name="name" value="${esc(j.name)}" placeholder="e.g. nightly-core"></label>
    <div class="row">
      <label class="f"><span>Connection</span><select name="connection_id" ${job ? 'disabled' : ''}>${conns.map(c => `<option value="${c.id}" ${c.id === j.connection_id ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</select></label>
      <label class="f"><span>Schedule (cron, server local time)</span><input name="schedule" value="${esc(j.schedule || '')}" list="cronp"><datalist id="cronp">${presets.map(p => `<option value="${p[0]}">${p[1]}</option>`).join('')}</datalist></label>
    </div>
    <div class="row">
      <label class="f"><span>Keep last N restore points (0 = unlimited)</span><input name="keep_last" type="number" min="0" value="${esc(j.retention.keep_last || 0)}"></label>
      <label class="f"><span>Keep for days (0 = unlimited)</span><input name="keep_days" type="number" min="0" value="${esc(j.retention.keep_days || 0)}"></label>
    </div>
    <label class="c"><input type="checkbox" name="quiesce" ${j.quiesce ? 'checked' : ''}> Quiesce the guest file system when VMware Tools is running</label>
    <label class="c"><input type="checkbox" name="enabled" ${j.enabled ? 'checked' : ''}> Enabled</label>
    <label class="f"><span>Virtual machines</span><input name="filter" placeholder="filter…" autocomplete="off"></label>
    <div class="vmlist" id="vmlist"></div>`;
  const m = modal({
    title: job ? 'Edit job' : 'New backup job', body, sticky: true,
    buttons: [{ label: 'Cancel' }, {
      label: job ? 'Save' : 'Create', cls: 'primary', onClick: async mm => {
        const ids = [...mm.querySelectorAll('#vmlist input:checked')].map(e => Number(e.value));
        if (!ids.length) throw new Error('select at least one VM');
        const data = { name: val(mm, 'name'), vm_ids: ids, schedule: val(mm, 'schedule') || null,
          retention: { keep_last: Number(val(mm, 'keep_last')), keep_days: Number(val(mm, 'keep_days')) },
          enabled: val(mm, 'enabled'), quiesce: val(mm, 'quiesce') };
        if (job) await api('PATCH', `/api/v1/jobs/${job.id}`, data);
        else await api('POST', '/api/v1/jobs', { ...data, connection_id: Number(val(mm, 'connection_id')) });
        toast(job ? 'Job saved' : 'Job created');
        document.dispatchEvent(new CustomEvent('jobs-changed'));
      }
    }]
  });
  const list = m.querySelector('#vmlist');
  const sel = new Set(j.vm_ids);
  const draw = () => {
    const cid = Number(val(m, 'connection_id'));
    const f = (val(m, 'filter') || '').toLowerCase();
    const rows = vms.filter(v => v.connection_id === cid && (!f || v.name.toLowerCase().includes(f)));
    list.innerHTML = rows.length ? rows.map(v => `<label class="c"><input type="checkbox" value="${v.id}" ${sel.has(v.id) ? 'checked' : ''}> ${esc(v.name)} <span class="muted">${esc(fmtBytes(v.size_bytes))} · ${esc(v.power_state === 'poweredOn' ? 'on' : 'off')}</span></label>`).join('')
      : '<div class="muted">No VMs on this connection. Refresh its inventory first.</div>';
  };
  list.addEventListener('change', e => { if (e.target.checked) sel.add(Number(e.target.value)); else sel.delete(Number(e.target.value)); });
  m.querySelector('[name=filter]').addEventListener('input', draw);
  m.querySelector('[name=connection_id]').addEventListener('change', () => { sel.clear(); draw(); });
  draw();
}
