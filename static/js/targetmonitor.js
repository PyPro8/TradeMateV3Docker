/* TradeMate Target Monitor: a curved, instrument-cluster style gauge for the Autopilot PROFIT TARGET.
   READ ONLY. It never starts, stops or changes anything. Sources (existing, authoritative): /api/autopilot/status
   { state, armed, paused, mode, mode_label, value (the configured target when mode === 'profit'), net_pnl (the session's net P/L), currency, stopped_reason }.
   Not used: account balance or equity (they are not session profit). Not assumed: the currency (it is the account's, as MT5 reports it).
   If Autopilot's stop condition is not "profit", there is NO profit target to measure against, and the monitor says exactly that.

   model(ap, ctx) is a pure function (tested in Node: tests/targetmonitor_test.js). ctx = { brokerConnected: bool|null, ageMs: number|null, failed: bool } */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TradeMateTargetMonitor = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  var STALE_MS = 20000;
  function num(x) { return typeof x === 'number' && isFinite(x); }
  var STATE_TEXT = { running: 'RUNNING', paused: 'PAUSED', stopped: 'STOPPED', emergency_stopped: 'EMERGENCY STOPPED', finished: 'FINISHED', blocked: 'BLOCKED',
                     interrupted: 'INTERRUPTED', idle: 'OFF', off: 'OFF' };

  function model(ap, ctx) {
    ctx = ctx || {};
    var m = { kind: 'ok', fraction: 0, percent: null, session: null, target: null, remaining: null, reached: false, currency: '', status: '', statusTone: 'idle',
              headline: '', detail: '', stale: false, brokerDown: ctx.brokerConnected === false, negative: false };
    if (!ap) {
      m.kind = ctx.failed ? 'unavailable' : 'loading';
      m.status = ctx.failed ? 'DATA UNAVAILABLE' : 'LOADING'; m.statusTone = ctx.failed ? 'warn' : 'idle';
      m.headline = ctx.failed ? 'Autopilot data could not be read from TradeMate.' : 'Reading the Autopilot session...';
      return m;
    }
    m.currency = typeof ap.currency === 'string' ? ap.currency : '';
    m.stale = !!ctx.failed || (num(ctx.ageMs) && ctx.ageMs > STALE_MS);
    var st = String(ap.state || (ap.armed ? 'running' : 'idle')).toLowerCase();
    m.status = STATE_TEXT[st] || st.toUpperCase();
    m.statusTone = st === 'running' ? 'ok' : (st === 'paused' || st === 'interrupted') ? 'warn' : (st === 'emergency_stopped' || st === 'blocked') ? 'bad' : 'idle';
    if (ap.mode !== 'profit') {
      m.kind = 'no-target';
      m.session = num(ap.net_pnl) && ap.mode ? ap.net_pnl : null;      // a session profit exists only while a session has a stop condition
      m.headline = ap.mode ? 'No profit target is configured.' : 'No Autopilot session is configured.';
      m.detail = ap.mode ? 'The selected stop condition is "' + (ap.mode_label || ap.mode) + '". Choose "Profit target" in the Autopilot controls to measure progress here.'
                         : 'Choose "Profit target" in the Autopilot controls and press Start to measure progress here.';
      return m;
    }
    m.target = num(ap.value) ? ap.value : null;
    m.session = num(ap.net_pnl) ? ap.net_pnl : null;
    if (m.target == null || m.target <= 0) {
      m.kind = 'zero-target'; m.headline = 'The profit target is zero or invalid.'; m.detail = 'Set a profit target above zero in the Autopilot controls.'; return m;
    }
    if (m.session == null) { m.kind = 'no-session'; m.headline = 'Session profit is not available.'; return m; }
    m.percent = m.session / m.target * 100;
    m.fraction = Math.max(0, Math.min(1, m.session / m.target));
    m.remaining = Math.max(0, m.target - m.session);
    m.reached = m.session >= m.target - 1e-9;
    m.negative = m.session < 0;
    m.kind = m.reached ? 'reached' : 'progress';
    if (m.reached) m.statusTone = 'ok';
    m.headline = m.reached ? 'TARGET REACHED' : m.negative ? 'BELOW START (' + Math.round(m.percent) + '%)' : Math.round(m.percent) + '% COMPLETE';
    if (m.reached && st === 'finished') m.status = 'TARGET REACHED';
    return m;
  }

  /* ---------- the instrument (browser only) ---------- */
  var CX = 160, CY = 160, R = 128, NS = 'http://www.w3.org/2000/svg';
  function pt(frac, r) { var a = Math.PI * (1 - frac); return [CX + r * Math.cos(a), CY - r * Math.sin(a)]; }
  function svgEl(name, attrs) { var e = document.createElementNS(NS, name); for (var k in attrs) e.setAttribute(k, attrs[k]); return e; }

  function build(host) {
    var svg = svgEl('svg', { viewBox: '0 0 320 174', class: 'tm-svg', role: 'img', 'aria-hidden': 'true', focusable: 'false' });
    var defs = svgEl('defs', {});
    var g = svgEl('linearGradient', { id: 'tm-grad', x1: '0', y1: '0', x2: '1', y2: '0' });
    g.appendChild(svgEl('stop', { offset: '0', class: 'tm-stop-a' })); g.appendChild(svgEl('stop', { offset: '1', class: 'tm-stop-b' }));
    defs.appendChild(g); svg.appendChild(defs);
    var d = 'M ' + (CX - R) + ' ' + CY + ' A ' + R + ' ' + R + ' 0 0 1 ' + (CX + R) + ' ' + CY;
    svg.appendChild(svgEl('path', { d: d, class: 'tm-track', pathLength: '100' }));
    var prog = svgEl('path', { d: d, class: 'tm-prog', pathLength: '100', stroke: 'url(#tm-grad)' }); prog.style.strokeDasharray = '0 100'; svg.appendChild(prog);
    for (var i = 0; i <= 20; i++) {                                    // scale: a mark every 5 %, a long mark and a label every 25 %
      var major = i % 5 === 0, f = i / 20, a = pt(f, R - 14), b = pt(f, R - (major ? 28 : 21));
      svg.appendChild(svgEl('line', { x1: a[0], y1: a[1], x2: b[0], y2: b[1], class: major ? 'tm-tick tm-tick-major' : 'tm-tick' }));
      if (major) { var l = pt(f, R - 40), t = svgEl('text', { x: l[0], y: l[1] + 3, class: 'tm-tick-label', 'text-anchor': 'middle' }); t.textContent = (f * 100) + ''; svg.appendChild(t); }
    }
    var needle = svgEl('g', { class: 'tm-needle' });
    needle.appendChild(svgEl('line', { x1: CX, y1: CY, x2: CX - (R - 58), y2: CY, class: 'tm-needle-line' }));
    needle.appendChild(svgEl('circle', { cx: CX - (R - 58), cy: CY, r: 4, class: 'tm-needle-tip' }));
    svg.appendChild(needle);
    svg.appendChild(svgEl('circle', { cx: CX, cy: CY, r: 7, class: 'tm-hub' }));
    host.appendChild(svg);
    return { prog: prog, needle: needle };
  }

  function init(root) {
    var host = root.querySelector('[data-tm="gauge"]'); if (!host) return null;
    var parts = build(host), last = null, lastOkAt = 0, failed = false, busy = false;
    function $(k) { return root.querySelector('[data-tm="' + k + '"]'); }
    var fmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 });
    function money(v, cur) { return num(v) ? (v < 0 ? '-' : '') + fmt.format(Math.abs(v)) + (cur ? ' ' + cur : '') : '---'; }
    function text(k, t) { var e = $(k); if (e && e.textContent !== t) e.textContent = t; }

    function render() {
      var brokerEl = document.getElementById('mt5-state');
      var m = model(last, { brokerConnected: brokerEl ? brokerEl.textContent === 'CONNECTED' : null, ageMs: last ? Date.now() - lastOkAt : null, failed: failed && !last ? true : false });
      if (failed && last) m.stale = true;
      var live = m.kind === 'progress' || m.kind === 'reached';
      var ang = -180 + 180 * m.fraction;                                   // the needle rests at 0 % (left) and sweeps to 100 % (right)
      parts.prog.style.strokeDasharray = (m.fraction * 100) + ' 100';
      parts.needle.style.transform = 'rotate(' + (live ? 180 * m.fraction : 0) + 'deg)';
      parts.needle.style.transformOrigin = CX + 'px ' + CY + 'px';
      void ang;
      root.setAttribute('data-kind', m.kind); root.setAttribute('data-stale', m.stale ? 'true' : 'false');
      root.setAttribute('data-negative', m.negative ? 'true' : 'false'); root.setAttribute('data-reached', m.reached ? 'true' : 'false');
      text('label', live || m.session != null ? 'CURRENT SESSION' : 'TARGET MONITOR');
      text('value', live ? money(m.session, m.currency) : (m.session != null ? money(m.session, m.currency) : '---'));
      text('target', m.target != null ? 'TARGET: ' + money(m.target, m.currency) : 'TARGET: NOT SET');
      text('headline', m.headline);
      text('remaining', live ? (m.reached ? 'TARGET MET' : money(m.remaining, m.currency) + ' REMAINING') : '');
      var chip = $('status'); if (chip) { chip.textContent = (m.status || '---') + (m.stale ? ' · STALE' : ''); chip.setAttribute('data-tone', m.stale ? 'warn' : m.statusTone); }
      text('detail', (m.detail || '') + (m.brokerDown ? ' The broker is disconnected: this is the last session figure TradeMate recorded.' : '') + (m.stale && last ? ' The data is out of date.' : '') +
                     (!m.currency && last ? ' (Account currency unknown.)' : ''));
      var meter = $('meter');
      if (meter) {
        if (live) { meter.setAttribute('aria-valuenow', String(Math.round(m.fraction * 100))); meter.setAttribute('aria-valuetext', m.headline + ', ' + money(m.remaining, m.currency) + ' remaining'); }
        else { meter.removeAttribute('aria-valuenow'); meter.setAttribute('aria-valuetext', m.headline); }
      }
    }

    function visible() { var v = document.getElementById('view-dashboard'); return !!v && !v.hidden && !document.hidden; }
    function refresh() {
      if (busy || !visible()) return; busy = true;
      fetch('/api/autopilot/status', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
        if (j && j.ok) { last = j; lastOkAt = Date.now(); failed = false; } else failed = true;
      }).catch(function () { failed = true; }).then(function () { busy = false; render(); });
    }
    render();
    if (window.TradeMateRouter) window.TradeMateRouter.on(function () { refresh(); });
    setInterval(function () { refresh(); if (last) render(); }, 4000);     // a light poll, only while the Dashboard is on screen
    document.addEventListener('visibilitychange', refresh);
    refresh();
    return { refresh: refresh, model: function () { return model(last, {}); } };
  }

  var api = { model: model, STALE_MS: STALE_MS };
  if (typeof document !== 'undefined') {
    var go = function () { var r = document.getElementById('target-monitor'); if (r) api.instance = init(r); };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go); else go();
  }
  return api;
});
