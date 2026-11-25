/* TradeMate Full Test dashboard. Vanilla JS.
   SAFETY: every piece of text from the server (test names, failure traces, logs) is placed with textContent, never as HTML,
   so output containing script tags or onerror attributes is shown as plain text and cannot run. No HTML string is ever built from server text.
   The page can only ask the server to run test GROUP IDs it listed itself; it cannot send a command. */
(function () {
  'use strict';

  var HEADERS = { 'Content-Type': 'application/json', 'X-TradeMate-Test': '1' };
  var isFile = location.protocol === 'file:';
  var catalog = null, groupViews = {}, polling = null, lastState = null;

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  /* same theme as the app (stored by the app in this browser); harmless default otherwise */
  try {
    var t = JSON.parse(localStorage.getItem('trademate.chartTheme'));
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
  } catch (e) { /* ignore */ }

  function badge(status) {
    var b = el('span', 'ft-badge', status);
    b.setAttribute('data-status', status);
    return b;
  }
  function fmtSeconds(s) { return s == null ? '-' : (s < 60 ? s.toFixed(1) + ' s' : Math.floor(s / 60) + ' min ' + Math.round(s % 60) + ' s'); }
  function fmtWhen(iso) {
    if (!iso) return 'never';
    var d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }

  /* ---------- group list ---------- */
  function buildGroups() {
    var ul = $('ft-groups');
    clear(ul);
    groupViews = {};
    catalog.groups.forEach(function (g) {
      var li = el('li', 'ft-group');
      li.setAttribute('data-group', g.id);
      var head = el('div', 'ft-group-head');
      var cb = el('input'); cb.type = 'checkbox'; cb.id = 'ft-sel-' + g.id; cb.setAttribute('data-group', g.id);
      var lab = el('label', 'ft-group-title', g.title); lab.setAttribute('for', cb.id);
      var kind = el('span', 'ft-tag', g.kind === 'browser' ? 'BROWSER' : g.kind === 'integration' ? 'INTEGRATION (simulated broker)' : 'AUTOMATED');
      head.appendChild(cb); head.appendChild(lab); head.appendChild(kind);
      if (g.simulated_broker && g.kind !== 'integration') head.appendChild(el('span', 'ft-tag ft-tag-sim', 'SIMULATED BROKER'));
      var st = badge('NOT RUN'); head.appendChild(st);
      var counts = el('span', 'ft-group-counts', '');
      head.appendChild(counts);
      li.appendChild(head);
      if (g.info) li.appendChild(el('p', 'ft-group-info', g.info));
      g.warnings.forEach(function (w) { li.appendChild(el('p', 'ft-group-warn', '! ' + w)); });
      var details = el('details', 'ft-details'); details.hidden = true;
      var sum = el('summary', null, 'Details'); details.appendChild(sum);
      var body = el('div', 'ft-details-body'); details.appendChild(body);
      li.appendChild(details);
      ul.appendChild(li);
      groupViews[g.id] = { li: li, status: st, counts: counts, details: details, body: body, cb: cb };
    });
    var opt = $('ft-optin'); clear(opt);
    (catalog.opt_in || []).forEach(function (o) {
      var li = el('li', 'ft-group');
      var head = el('div', 'ft-group-head');
      head.appendChild(el('span', 'ft-group-title', o.title));
      head.appendChild(el('span', 'ft-tag', 'OPT-IN DEMO EXECUTION'));
      head.appendChild(badge('NOT RUN'));
      li.appendChild(head);
      li.appendChild(el('p', 'ft-group-info', o.why));
      var code = el('pre', 'ft-code', o.command); code.setAttribute('tabindex', '0');
      li.appendChild(code);
      opt.appendChild(li);
    });
    if (catalog.disclaimer) $('ft-disclaimer').textContent = catalog.disclaimer;
  }

  function renderGroup(g) {
    var v = groupViews[g.id]; if (!v) return;
    v.status.textContent = g.status; v.status.setAttribute('data-status', g.status);
    v.counts.textContent = g.status === 'NOT RUN' ? '' :
      g.passed + ' passed · ' + (g.failed + g.error) + ' failed · ' + g.skipped + ' skipped' + (g.seconds != null ? ' · ' + fmtSeconds(g.seconds) : '');
    clear(v.body);
    var any = false;
    if (g.note) { v.body.appendChild(el('p', 'ft-note', g.note)); any = true; }
    (g.tests || []).filter(function (t) { return t.status !== 'passed'; }).forEach(function (t) {
      var box = el('div', 'ft-test ft-test-' + t.status);
      box.appendChild(el('strong', null, t.status.toUpperCase() + '  ' + t.id));
      if (t.detail) { var pre = el('pre', 'ft-code', t.detail); pre.setAttribute('tabindex', '0'); box.appendChild(pre); }
      v.body.appendChild(box); any = true;
    });
    (g.checks || []).forEach(function (c) {
      var box = el('div', 'ft-test ft-test-' + (c.status === 'PASS' ? 'passed' : 'failed'));
      box.appendChild(el('strong', null, c.status + '  node ' + c.name));
      if (c.detail) { var pre = el('pre', 'ft-code', c.detail); pre.setAttribute('tabindex', '0'); box.appendChild(pre); }
      v.body.appendChild(box); any = true;
    });
    if (g.log) {
      var lg = el('details', 'ft-log'); lg.appendChild(el('summary', null, 'Raw output'));
      var pre2 = el('pre', 'ft-code', g.log); pre2.setAttribute('tabindex', '0'); lg.appendChild(pre2);
      v.body.appendChild(lg); any = true;
    }
    v.details.hidden = !any;
    if (g.status === 'FAIL') v.details.open = true;
  }

  /* ---------- overall ---------- */
  function renderRun(run, running) {
    var overall = run ? (running ? 'RUNNING' : (run.overall || 'NEVER RUN')) : 'NEVER RUN';
    $('ft-overall').textContent = overall; $('ft-overall').setAttribute('data-status', overall);
    var passed = 0, failed = 0, skipped = 0, notrun = 0, seconds = 0, any = false;
    if (run) {
      (run.groups || []).forEach(function (g) {
        renderGroup(g);
        passed += g.passed; failed += g.failed + g.error; skipped += g.skipped; if (g.seconds != null) { seconds += g.seconds; any = true; }
        if (g.status === 'NOT RUN') notrun += 1;
      });
      var selected = (run.groups || []).length, total = catalog ? catalog.groups.length : selected;
      notrun += Math.max(0, total - selected);
    } else if (catalog) { notrun = catalog.groups.length; }
    $('ft-c-passed').textContent = passed; $('ft-c-failed').textContent = failed; $('ft-c-skipped').textContent = skipped;
    $('ft-c-notrun').textContent = notrun; $('ft-c-seconds').textContent = any ? fmtSeconds(seconds) : '-';
    $('ft-c-when').textContent = run ? fmtWhen(run.finished || run.started) : 'never';
    $('ft-overall-note').textContent = running ? 'Running' + (run && run.current ? ': ' + run.current : '') + '...' :
      (run && run.overall_note) || (run ? '' : 'No run yet. Press Run all tests.');
    $('ft-progress').textContent = running ? 'A run is in progress. Results appear group by group.' : '';
    $('ft-cancel').disabled = !running;
    ['ft-run-all', 'ft-run-sel'].forEach(function (id) { $(id).disabled = running || isFile; });
  }

  function refused(message) {
    var b = $('ft-refused'); b.hidden = !message; b.textContent = message || '';
    ['ft-run-all', 'ft-run-sel', 'ft-cancel'].forEach(function (id) { $(id).disabled = true; });
  }

  /* ---------- talking to the server ---------- */
  function getJSON(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      return r.json().catch(function () { return { ok: false, message: 'HTTP ' + r.status }; }).then(function (j) { j._status = r.status; return j; });
    });
  }
  function postJSON(url, body) {
    return fetch(url, { method: 'POST', headers: HEADERS, body: JSON.stringify(body || {}) }).then(function (r) {
      return r.json().catch(function () { return { ok: false, message: 'HTTP ' + r.status }; }).then(function (j) { j._status = r.status; return j; });
    });
  }

  function poll() {
    return getJSON('/api/fulltest/status').then(function (res) {
      if (!res.ok) { refused(res.message || 'The server refused the request.'); stopPolling(); return; }
      var running = !!(res.state && res.state.running);
      var run = running ? res.state : (res.last || (res.state && res.state.run_id ? res.state : null));
      lastState = run; renderRun(run, running);
      if (running && !polling) polling = setInterval(poll, 1000);
      if (!running) stopPolling();
    }).catch(function () { $('ft-overall-note').textContent = 'Cannot reach the TradeMate server.'; });
  }
  function stopPolling() { if (polling) { clearInterval(polling); polling = null; } }

  function start(groups) {
    $('ft-refused').hidden = true;
    postJSON('/api/fulltest/run', { groups: groups }).then(function (res) {
      if (!res.ok) { $('ft-overall-note').textContent = res.message || 'Could not start the run.'; if (res._status === 403) refused(res.message); return; }
      poll();
    });
  }
  function selectedIds() {
    return Object.keys(groupViews).filter(function (id) { return groupViews[id].cb.checked; });
  }

  function init() {
    if (isFile) {
      $('ft-file-warning').hidden = false;
      var port = $('ft-port'), link = $('ft-open-link');
      var upd = function () { var p = parseInt(port.value, 10); link.href = 'http://127.0.0.1:' + (p >= 1 && p <= 65535 ? p : 5000) + '/full-test'; };
      port.addEventListener('input', upd); upd();
      ['ft-run-all', 'ft-run-sel', 'ft-cancel', 'ft-refresh'].forEach(function (id) { $(id).disabled = true; });
      $('ft-overall-note').textContent = 'Not connected: this page was opened as a file.';
      return;
    }
    $('ft-run-all').addEventListener('click', function () { start('all'); });
    $('ft-run-sel').addEventListener('click', function () {
      var ids = selectedIds();
      if (!ids.length) { $('ft-overall-note').textContent = 'Tick at least one group first.'; return; }
      start(ids);
    });
    $('ft-cancel').addEventListener('click', function () { postJSON('/api/fulltest/cancel', {}).then(poll); });
    $('ft-refresh').addEventListener('click', function () { poll(); });
    $('ft-select-all').addEventListener('change', function (e) {
      Object.keys(groupViews).forEach(function (id) { groupViews[id].cb.checked = e.target.checked; });
    });
    getJSON('/api/fulltest/catalog').then(function (res) {
      if (!res.ok) { refused(res.message || 'The Full Test runner is not available.'); return; }
      catalog = res; buildGroups(); poll();
    }).catch(function () { $('ft-overall-note').textContent = 'Cannot reach the TradeMate server.'; });
  }
  init();
})();
