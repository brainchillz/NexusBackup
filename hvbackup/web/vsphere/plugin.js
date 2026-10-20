/* hvbackup vSphere Client plug-in pages: bootstrap the client SDK, exchange the vSphere
   session for an hvbackup token, and talk to the hvbackup API through vCenter's reverse
   proxy (same origin as this page). */
(function () {
  'use strict';
  var HVB = window.HVB = { token: null, user: null, base: '', ctx: [], ready: false };

  function base() {
    if (HVB.base) return HVB.base;
    try { HVB.base = (window.htmlClientSdk && htmlClientSdk.getProxiedPluginServerOrigin()) || ''; } catch (e) { HVB.base = ''; }
    if (!HVB.base) { var p = location.pathname.replace(/\/vsphere-plugin\/.*$/, ''); HVB.base = location.origin + p; }
    HVB.base = HVB.base.replace(/\/$/, '');
    return HVB.base;
  }
  HVB.esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
  HVB.fmtBytes = function (n) { n = n || 0; var u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'], i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + ' ' + u[i]; };
  HVB.fmtTime = function (t) { return t ? new Date(t * 1000).toLocaleString() : '-'; };
  HVB.ago = function (t) { if (!t) return '-'; var s = Math.max(0, Date.now() / 1000 - t); if (s < 60) return Math.round(s) + 's ago'; if (s < 3600) return Math.round(s / 60) + 'm ago'; if (s < 86400) return Math.round(s / 3600) + 'h ago'; return Math.round(s / 86400) + 'd ago'; };

  HVB.api = function (method, path, body) {
    var h = { 'Accept': 'application/json' };
    if (HVB.token) h['Authorization'] = 'Bearer ' + HVB.token;
    if (body !== undefined) h['Content-Type'] = 'application/json';
    return fetch(base() + '/api/v1' + path, { method: method, headers: h, body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store' })
      .then(function (r) { return r.text().then(function (t) { var d; try { d = t ? JSON.parse(t) : null; } catch (e) { d = t; } if (!r.ok) { var err = new Error((d && d.detail) || ('HTTP ' + r.status)); err.status = r.status; throw err; } return d; }); });
  };

  function vcGuidFrom(ids) { for (var i = 0; i < ids.length; i++) { var m = /:([0-9a-fA-F-]{36})$/.exec(ids[i]); if (m) return m[1]; } return null; }

  HVB.fail = function (msg) { var el = document.getElementById('hvb-error'); if (el) { el.textContent = msg; el.style.display = 'block'; } var m = document.getElementById('hvb-main'); if (m) m.style.display = 'none'; };

  /* Initialise: SDK, context objects, session exchange. Calls cb(ctxIds) when the API is usable. */
  HVB.init = function (cb) {
    if (!window.htmlClientSdk) {
      HVB.fail('This page only works inside the vSphere Client. Open the hvbackup console directly at ' + (document.body.dataset.publicUrl || 'its own URL') + '.');
      return;
    }
    htmlClientSdk.initialize(function () {
      var ctx = [];
      try { ctx = (htmlClientSdk.app.getContextObjects() || []).map(function (o) { return o.id || o; }); } catch (e) { ctx = []; }
      HVB.ctx = ctx;
      try { var th = htmlClientSdk.app.getTheme && htmlClientSdk.app.getTheme(); if (th && /dark/i.test(th.name || th)) document.documentElement.classList.add('dark'); } catch (e) { }
      htmlClientSdk.app.getSessionInfo(function (info) {
        if (!info || !info.sessionToken) { HVB.fail('The vSphere Client did not provide a session token.'); return; }
        HVB.api('POST', '/auth/vsphere', { session_token: info.sessionToken, vc_guid: vcGuidFrom(ctx), object_ids: ctx })
          .then(function (d) { HVB.token = d.token; HVB.user = d; HVB.ready = true; cb(ctx); })
          .catch(function (e) { HVB.fail('hvbackup sign-in failed: ' + e.message); });
      });
    });
  };

  HVB.resolve = function (objectId) { return HVB.api('GET', '/vsphere/resolve?object_id=' + encodeURIComponent(objectId)); };
  HVB.close = function (data) { try { htmlClientSdk.modal.close(data); } catch (e) { } };
  HVB.badge = function (s) { var cls = /done|success|poweredOn|true/i.test(s) ? 'ok' : /fail|error|cancel/i.test(s) ? 'bad' : /running|queued/i.test(s) ? 'run' : ''; return '<span class="badge ' + cls + '">' + HVB.esc(s) + '</span>'; };
  HVB.pollTask = function (id, onUpdate) {
    function tick() { HVB.api('GET', '/tasks/' + id).then(function (t) { onUpdate(t); if (t.state === 'queued' || t.state === 'running') setTimeout(tick, 2000); }).catch(function (e) { onUpdate({ state: 'failed', message: e.message, progress: 0 }); }); }
    tick();
  };
})();
