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
function stateBadge(s) {
  const m = { done: 'ok', running: 'info', queued: 'info', failed: 'bad', cancelled: 'warn' };
  return badge(s, m[s] || '');
}
function powerBadge(p) {
  if (p === 'poweredOn') return badge('on', 'ok');
  if (p === 'poweredOff') return badge('off', '');
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
  bg.innerHTML = `<div class="modal" role="dialog"><header><span>${esc(o.title)}</span><button class="btn sm" data-x>✕</button></header>
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

/* Restore dialog shared by the VM and restore-point pages. */
async function restoreDialog(rp, vmName, connectionId) {
  const conns = await api('GET', '/api/v1/connections');
  let detail = {};
  try { detail = await api('GET', `/api/v1/restore-points/${rp.id}`); } catch (e) { /* older point */ }
  const spec = detail.spec || {};
  const place = spec.placement || {};
  const ds = (spec.disks && spec.disks[0] && spec.disks[0].datastore) || (place.datastores && place.datastores[0]) || '';
  modal({
    title: 'Restore ' + vmName,
    body: `<p class="muted">Restore point ${esc(rp.id)} from ${esc(fmtTime(rp.created))} (${esc(rp.kind)}, ${esc(fmtBytes(rp.allocated_bytes))} allocated).</p>
      <label class="f"><span>New VM name</span><input name="name" value="${esc(vmName + '-restored')}"></label>
      <div class="row">
        <label class="f"><span>Target connection</span><select name="connection_id">${conns.map(c => `<option value="${c.id}" ${c.id === connectionId ? 'selected' : ''}>${esc(c.name)} (${esc(c.type)})</option>`).join('')}</select></label>
        <label class="f"><span>Host</span><input name="host" value="${esc(place.host || '')}" placeholder="ESXi host name as vCenter shows it"></label>
        <label class="f"><span>Datastore</span><input name="datastore" value="${esc(ds)}"></label>
      </div>
      <label class="c"><input type="checkbox" name="new_identity" checked> New identity (fresh MAC addresses and BIOS UUID). Untick only when the original VM is gone.</label>
      <label class="c"><input type="checkbox" name="power_on"> Power on after restore</label>
      <p class="muted">A restored copy keeps the guest's host name, host keys and DHCP client identity. Do not run it next to the original on the same network.</p>`,
    buttons: [{ label: 'Cancel' }, {
      label: 'Restore', cls: 'primary', onClick: async m => {
        const body = { name: val(m, 'name'), connection_id: Number(val(m, 'connection_id')), host: val(m, 'host') || null,
          datastore: val(m, 'datastore') || null, new_identity: val(m, 'new_identity'), power_on: val(m, 'power_on') };
        const res = await api('POST', `/api/v1/restore-points/${rp.id}/restore`, body);
        startedTask(res, 'Restore started');
      }
    }]
  });
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
