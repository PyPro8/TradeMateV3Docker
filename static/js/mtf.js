/* TradeMate Strategy page: multi-timeframe analysis (M5, M15, H1, H4) from /api/strategy/mtf.
   Read-only: it shows a proposal and the evidence behind it. It never places an order, sizes a position or calls the Risk Engine.
   Every value comes from the server's answer for the symbol that was requested; an answer for an older request is dropped.
   All server text is placed with textContent (no HTML is ever built from it). */
(function () {
  'use strict';
  var panel = document.getElementById('mtf-panel');
  if (!panel) return;
  var $ = function (id) { return document.getElementById(id); };
  var seq = 0, ctl = null;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function kv(parent, k, v, cls) {
    var row = el('div', 'an-kv'); row.appendChild(el('span', 'an-k', k)); row.appendChild(el('span', 'an-v' + (cls ? ' ' + cls : ''), v)); parent.appendChild(row);
  }
  function bullets(parent, items) {
    if (!items || !items.length) return;
    var ul = el('ul', 'an-list'); items.forEach(function (t) { ul.appendChild(el('li', null, t)); }); parent.appendChild(ul);
  }
  function heading(parent, text) { parent.appendChild(el('h3', 'an-h', text)); }
  function num(v) { return (typeof v === 'number' && isFinite(v)) ? String(+v.toPrecision(8)) : 'not available'; }
  function app() { return window.TradeMateApp || null; }

  function fillSymbols() {
    var a = app(), s = $('mtf-symbol'); if (!a) return;
    var names = a.symbols(), want = s.value || a.selection().symbol;
    if (want && names.indexOf(want) < 0) names.unshift(want);
    var key = names.join('|');
    if (s.getAttribute('data-key') !== key) {
      clear(s); names.forEach(function (n) { var o = el('option', null, n); o.value = n; s.appendChild(o); }); s.setAttribute('data-key', key);
    }
    if (want) s.value = want;
  }

  function render(r) {
    var out = $('mtf-out'); clear(out);
    var head = el('div', 'an-status'); head.setAttribute('data-decision', r.decision);
    head.appendChild(el('strong', null, r.decision));
    head.appendChild(el('span', null, ' ' + (r.direction ? r.direction + ' ' : '') + '(setup: ' + r.setup_status + ') on ' + r.symbol + ', execution timeframe ' + r.primary_execution_timeframe));
    out.appendChild(head);
    out.appendChild(el('p', 'an-p', r.context.label + ': ' + r.context.summary));

    heading(out, 'Timeframes');
    ['H4', 'H1', 'M15', 'M5'].forEach(function (tf) {
      var a = r.timeframes[tf], d = r.data[tf], box = el('div', 'an-card');
      var line = a.analyzed ? a.bias + ' (' + a.strength + ')' : 'not analysed';
      kv(box, tf, line);
      kv(box, 'Data', d.status + (d.lag_seconds !== null && d.lag_seconds !== undefined ? ', newest closed candle ' + Math.round(d.lag_seconds) + ' s ago' : ''), d.status === 'ok' ? '' : 'an-warn');
      if (!a.analyzed || d.status !== 'ok') box.appendChild(el('p', 'an-note', d.reason));
      if (a.analyzed) {
        box.appendChild(el('p', 'an-note', a.bias_basis));
        kv(box, 'Invalidation level', num(a.invalidation.level));
        kv(box, 'Reversal', a.reversal.stage + (a.reversal.toward ? ' toward ' + a.reversal.toward : ''));
        if (a.intrabar && a.intrabar.present) box.appendChild(el('p', 'an-note', a.intrabar.note));
      }
      out.appendChild(box);
    });

    heading(out, 'Qualification gates');
    var gl = el('ul', 'an-list');
    r.gates.forEach(function (g) { gl.appendChild(el('li', null, (g.passed ? 'PASS ' : (g.evaluated ? 'FAIL ' : 'n/a  ')) + g.id + ': ' + g.detail)); });
    out.appendChild(gl);

    if (r.entry && r.stop_loss && r.take_profit_1) {
      heading(out, 'Proposed levels (a proposal: the Risk Engine decides size and approval)');
      var box = el('div', 'an-card');
      kv(box, 'Entry reference', num(r.entry.price)); kv(box, 'Entry zone', num(r.entry.zone.lower) + ' to ' + num(r.entry.zone.upper) + ' (' + r.entry.zone.source + ')');
      kv(box, 'Structural stop loss', num(r.stop_loss.price)); kv(box, 'Take profit 1', num(r.take_profit_1.price) + ' (' + r.take_profit_1.basis + ', ' + r.take_profit_1.rr.toFixed(2) + 'R)');
      kv(box, 'Take profit 2', r.take_profit_2 ? num(r.take_profit_2.price) + ' (' + r.take_profit_2.basis + ')' : 'not available');
      kv(box, 'Setup invalidation', num(r.setup_invalidation.level) + ' (' + r.setup_invalidation.timeframe + ')');
      out.appendChild(box);
    }
    var inv = r.structural_invalidation;
    heading(out, 'Bias, invalidation and reversal');
    var ib = el('div', 'an-card');
    kv(ib, 'Bias evaluated', inv.bias ? inv.bias + ' (' + inv.timeframe + ')' : 'none');
    kv(ib, 'Structural invalidation', num(inv.level));
    kv(ib, 'Reversal stage', r.reversal.stage + (r.reversal.toward ? ' toward ' + r.reversal.toward : ''));
    ib.appendChild(el('p', 'an-note', inv.basis));
    bullets(ib, r.reversal.conditions);
    out.appendChild(ib);

    if (r.reasons.length) { heading(out, 'Why not (or why waiting)'); bullets(out, r.reasons); }
    if (r.missing_evidence.length) { heading(out, 'Missing or insufficient evidence'); bullets(out, r.missing_evidence); }
    if (r.confirmation_evidence.length) { heading(out, 'Supporting evidence'); bullets(out, r.confirmation_evidence); }
    if (r.notes.length) bullets(out, r.notes);
    out.appendChild(el('p', 'an-note', 'Broker clock: ' + (r.clock.known ? r.clock.detail : 'unknown, so no setup can be confirmed. ' + (r.clock.detail || ''))));
    out.appendChild(el('p', 'an-note', r.disclaimer));
  }

  function run() {
    fillSymbols();
    var symbol = $('mtf-symbol').value; if (!symbol) { $('mtf-note').textContent = 'Choose a symbol first.'; return; }
    var mine = ++seq; if (ctl) ctl.abort(); ctl = ('AbortController' in window) ? new AbortController() : null;
    var btn = $('mtf-run'); btn.disabled = true; $('mtf-note').textContent = 'Analysing ' + symbol + '...';
    fetch('/api/strategy/mtf?symbol=' + encodeURIComponent(symbol), ctl ? { signal: ctl.signal } : undefined)
      .then(function (resp) { return resp.json().catch(function () { return { ok: false, message: 'The server sent an unreadable answer.' }; }); })
      .then(function (body) {
        if (mine !== seq) return;                                   // an answer for an older request is dropped
        if (!body.ok) { clear($('mtf-out')); $('mtf-note').textContent = body.message || 'The analysis is not available.'; return; }
        $('mtf-note').textContent = 'Analysis for ' + body.result.symbol + '. Closed candles only; nothing was sent to the broker.';
        render(body.result);
      })
      .catch(function (err) { if (mine === seq && !(err && err.name === 'AbortError')) { clear($('mtf-out')); $('mtf-note').textContent = 'Could not reach TradeMate.'; } })
      .then(function () { if (mine === seq) btn.disabled = false; });
  }

  $('mtf-run').addEventListener('click', run);
  // The symbol list arrives from the app after this script has loaded, so keep the dropdown current while the Strategy page is shown.
  function refresh() { if (!$('view-strategy').hidden) fillSymbols(); }
  window.TradeMateRouter.on(refresh);
  $('mtf-symbol').addEventListener('focus', refresh);
  setInterval(refresh, 1500);
  fillSymbols();
  window.TradeMateMtf = { run: run };
})();
