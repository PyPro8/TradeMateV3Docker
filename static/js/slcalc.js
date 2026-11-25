/* TradeMate two-way Stop Loss panel (Phase 7A.1).

   Mode "Stop Loss Price": you type an SL price, the server shows the money lost if it is hit.
   Mode "Risk Amount":     you type the money you accept to lose, the server shows the SL price that fits.
   Every number comes from POST /api/risk/sl, which uses the Risk Engine's own tick maths in the ACCOUNT currency. This file
   does no money maths. It is a PREVIEW: it never sends an order, and /api/order re-checks everything at submission.

   Rules enforced here:
   - a result is shown only while it matches the current inputs; otherwise it is hidden and "Recalculating" is shown
   - responses that arrive late (older request id) are dropped
   - requests are debounced; there is never more than one in flight per panel
   - switching modes never overwrites what you typed, and never starts a circular update
   - text from the server is placed with textContent only
   Pure helpers (no DOM) are exported for tests/slcalc_test.js. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TradeMateSL = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var MODES = { price: 'price', risk: 'risk' };
  var DEBOUNCE_MS = 350, REFRESH_MS = 8000;

  function num(text) {
    if (text === null || text === undefined) return null;
    var t = String(text).trim();
    if (t === '' || !/^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(t)) return null;
    var v = Number(t);
    return isFinite(v) ? v : null;
  }

  /* what a request depends on. Entry is NOT part of the key: it follows the market and is returned by the server. */
  function inputsKey(s) {
    return JSON.stringify([s.mode, s.symbol, s.direction, s.volume, s.mode === 'risk' ? s.riskAmount : s.stopLoss, s.entry == null ? null : s.entry]);
  }

  function withEntry(body, s) { if (s.pendingEntry && s.entry > 0) body.entry = s.entry; return body; }   // a pending order is measured from ITS price

  /* null when a request cannot be made yet (and why), else the request body */
  function buildRequest(s, requestId) {
    if (!s.symbol) return { error: 'Pick a symbol.' };
    if (s.direction !== 'buy' && s.direction !== 'sell') return { error: 'Pick BUY or SELL.' };
    if (!(s.volume > 0)) return { error: 'Enter a lot size above 0.' };
    if (s.pendingEntry && !(s.entry > 0)) return { error: 'Enter the pending order\'s entry price first.' };
    if (s.mode === 'risk') {
      if (!(s.riskAmount > 0)) return { error: 'Enter the amount you accept to lose.' };
      return { body: withEntry({ mode: 'risk', symbol: s.symbol, direction: s.direction, volume: s.volume, risk_amount: s.riskAmount, request_id: requestId }, s) };
    }
    if (!(s.stopLoss > 0)) return { error: 'Enter a stop-loss price.' };
    return { body: withEntry({ mode: 'price', symbol: s.symbol, direction: s.direction, volume: s.volume, stop_loss: s.stopLoss, request_id: requestId }, s) };
  }

  /* Switching modes: decide what the fields show. Never overwrites typed input; never auto-recalculates (no loops).
     `last` is the latest VALID result for the current inputs, or null. */
  function switchMode(prev, next, fields, last) {
    var out = { riskText: fields.riskText, slText: fields.slText, recalc: false };
    if (prev === next) return out;
    if (prev === 'price' && next === 'risk' && last && last.valid && typeof last.risk_amount === 'number' && fields.riskText.trim() === '') {
      out.riskText = last.risk_amount.toFixed(2);          // start from what the typed stop-loss currently risks
    }
    // risk -> price: the calculated SL is already in the SL field and stays editable
    return out;
  }

  /* may the BUY/SELL button submit? In risk mode the SL must be a freshly calculated, valid one for the same side. */
  function submitGate(s, shown, side) {
    if (s.mode !== 'risk') return { ok: true };
    if (s.direction !== side) return { ok: false, why: 'The stop-loss was calculated for ' + s.direction.toUpperCase() + '. Switch the direction or press the matching button.' };
    if (!shown || shown.pending) return { ok: false, why: 'The stop-loss is still being calculated. Try again in a moment.' };
    if (shown.key !== inputsKey(s)) return { ok: false, why: 'The inputs changed. Wait for the new stop-loss.' };
    if (!shown.result || !shown.result.valid) return { ok: false, why: (shown.result && shown.result.message) || 'No valid stop-loss for this risk.' };
    if (!(Math.abs(s.stopLoss - shown.result.stop_loss) <= 1e-9)) return { ok: false, why: 'The stop-loss no longer matches your risk amount. Wait for the recalculation.' };
    return { ok: true };
  }

  function fmt(v, d) { return typeof v === 'number' && isFinite(v) ? v.toFixed(d) : '-'; }

  /* lines to display for one result. kind: ok | warn | bad. All plain text. */
  function describe(resp, currency, digits) {
    var r = resp && resp.result, lim = resp && resp.limit;
    if (!r) return { kind: 'bad', lines: [(resp && resp.message) || 'No answer.'] };
    var cur = currency || r.currency || '';
    if (!r.valid) {
      var bad = [r.message || 'No valid calculation.'];
      if (typeof r.nearest_valid_stop === 'number') bad.push('Nearest valid stop-loss: ' + r.nearest_valid_stop);
      return { kind: 'bad', lines: bad };
    }
    var lines = [];
    if (r.mode === 'risk') {
      lines.push('Stop-loss price: ' + fmt(r.stop_loss, digits));
      lines.push('Requested risk: ' + fmt(r.requested_risk, 2) + ' ' + cur);
      lines.push('Final risk at that stop: ' + fmt(r.risk_amount, 2) + ' ' + cur);
      if (r.difference > 1e-9) lines.push('Difference: ' + fmt(r.difference, 2) + ' ' + cur + ' less (prices move in whole steps).');
    } else {
      lines.push('Loss if the stop-loss is hit: ' + fmt(r.risk_amount, 2) + ' ' + cur);
    }
    lines.push('Entry used (' + (resp.entry_source === 'live' ? 'live market price' : 'as entered') + '): ' + fmt(r.entry, digits));
    lines.push('Distance: ' + r.stop_ticks + ' ticks · ' + r.volume + ' lots');
    var kind = 'ok';
    if (lim && lim.checked) {
      lines.push(lim.message);
      if (!lim.ok) kind = 'bad';
    } else if (lim) { lines.push(lim.message); kind = 'warn'; }
    return { kind: kind, lines: lines, notes: (r.notes || []).slice(), quantity: r.quantity_note };
  }

  /* ------------------------------ DOM controller ------------------------------ */
  function attach(root, deps) {
    var q = function (role) { return root.querySelector('[data-role="' + role + '"]'); };
    var box = q('sl-box');
    if (!box) return null;
    var els = {
      modeBtns: box.querySelectorAll('[data-sl-mode]'), dirBtns: box.querySelectorAll('[data-sl-dir]'),
      risk: q('sl-risk'), riskWrap: q('sl-risk-wrap'), sl: q('sl'), out: q('sl-out'), title: q('sl-title'), cur: q('sl-cur'),
      slLabel: q('sl-label')
    };
    var st = { mode: MODES.price, direction: 'buy', shown: null, seq: 0, inflight: false, queued: false, timer: null, typedAt: 0 };

    function state() {
      var forced = deps.getDirection ? deps.getDirection() : null, rawEntry = deps.getEntry ? deps.getEntry() : null;
      return { mode: st.mode, symbol: deps.getSymbol(), direction: forced || st.direction, volume: num(deps.getLot()),
               riskAmount: num(els.risk.value), stopLoss: num(els.sl.value),
               pendingEntry: rawEntry !== null && rawEntry !== undefined, entry: rawEntry === null || rawEntry === undefined ? null : num(rawEntry) };
    }
    function setText(node, text) { node.textContent = text; }
    function clearNode(n) { while (n.firstChild) n.removeChild(n.firstChild); }

    function paintMode() {
      var risk = st.mode === MODES.risk;
      els.modeBtns.forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-sl-mode') === st.mode)); });
      var forcedDir = deps.getDirection ? deps.getDirection() : null;
      els.dirBtns.forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-sl-dir') === (forcedDir || st.direction))); b.disabled = !!forcedDir; });
      els.riskWrap.hidden = !risk;
      els.sl.readOnly = risk;                       // risk mode: the SL is CALCULATED, not typed
      els.sl.setAttribute('aria-readonly', String(risk));
      els.sl.classList.toggle('sl-calculated', risk);
      setText(els.slLabel, risk ? 'Stop Loss price (calculated from your risk)' : 'Stop Loss (required)');
    }

    function render(entry) {
      clearNode(els.out);
      var s = state();
      if (!entry) { setText(els.out, ''); els.out.removeAttribute('data-kind'); return; }
      els.out.setAttribute('data-kind', entry.kind);
      entry.lines.forEach(function (l) { var p = document.createElement('div'); p.className = 'sl-line'; p.textContent = l; els.out.appendChild(p); });
      (entry.notes || []).forEach(function (l) { var p = document.createElement('div'); p.className = 'sl-note'; p.textContent = l; els.out.appendChild(p); });
      if (entry.quantity) { var n = document.createElement('div'); n.className = 'sl-note'; n.textContent = entry.quantity; els.out.appendChild(n); }
    }

    function showMessage(text, kind) { render({ kind: kind || 'warn', lines: [text] }); }

    function apply(resp, key, digits) {
      var d = describe(resp, resp && resp.result && resp.result.currency, digits);
      st.shown = { key: key, result: resp.result, pending: false, entry: d };
      if (st.mode === MODES.risk && resp.result && resp.result.valid) els.sl.value = String(resp.result.stop_loss);   // calculated SL
      else if (st.mode === MODES.risk) els.sl.value = '';
      render(d);
      deps.onChange && deps.onChange();
    }

    function request() {
      clearTimeout(st.timer);
      var s = state();
      var built = buildRequest(s, 0);
      if (built.error) { st.shown = null; if (st.mode === MODES.risk) els.sl.value = ''; showMessage(built.error, 'warn'); deps.onChange && deps.onChange(); return; }
      if (st.inflight) { st.queued = true; return; }
      var key = inputsKey(s), id = ++st.seq;
      built.body.request_id = id;
      st.inflight = true;
      st.shown = { key: key, pending: true };
      showMessage('Recalculating...', 'warn');
      deps.post('/api/risk/sl', built.body).then(function (resp) {
        st.inflight = false;
        var cur = state();
        if (!resp || resp.request_id !== id || inputsKey(cur) !== key) {     // stale answer: inputs changed while it was in flight
          if (st.queued || inputsKey(cur) !== key) { st.queued = false; request(); }
          return;
        }
        apply(resp, key, resp.digits);
        if (st.queued) { st.queued = false; request(); }
      }, function () {
        st.inflight = false; st.shown = null;
        showMessage('Could not reach TradeMate. No calculation was made.', 'bad');
        deps.onChange && deps.onChange();
      });
    }
    function schedule() {
      st.typedAt = Date.now();
      st.shown = st.shown && Object.assign({}, st.shown, { pending: true });     // never leave an old number looking current
      showMessage('Recalculating...', 'warn');
      clearTimeout(st.timer);
      st.timer = setTimeout(request, DEBOUNCE_MS);
    }

    els.modeBtns.forEach(function (b) {
      b.addEventListener('click', function () {
        var next = b.getAttribute('data-sl-mode');
        if (next === st.mode) return;
        var last = st.shown && st.shown.result;
        var sw = switchMode(st.mode, next, { riskText: els.risk.value, slText: els.sl.value }, last);
        st.mode = next;
        els.risk.value = sw.riskText;
        paintMode();
        // Show the current inputs' answer once; this is a single request, not a loop, and it does not rewrite typed values.
        st.shown = null;
        request();
      });
    });
    els.dirBtns.forEach(function (b) {
      b.addEventListener('click', function () { st.direction = b.getAttribute('data-sl-dir'); paintMode(); schedule(); });
    });
    els.risk.addEventListener('input', schedule);
    els.sl.addEventListener('input', function () {
      // In risk mode the field is read-only, so an input event here means the stop was moved elsewhere (a chart drag):
      // you now own the stop, so switch to price mode instead of fighting the drag.
      if (st.mode === MODES.risk) { st.mode = MODES.price; paintMode(); }
      schedule();
    });
    paintMode();
    request();
    var refresh = setInterval(function () {
      if (document.hidden || st.inflight || Date.now() - st.typedAt < 3000) return;
      if (buildRequest(state(), 0).error) return;
      request();
    }, REFRESH_MS);

    return {
      refresh: function () { paintMode(); schedule(); },       // call when the symbol, lot size, order type or pending entry changes
      direction: function () { return st.direction; },
      mode: function () { return st.mode; },
      gate: function (side) { return submitGate(state(), st.shown, side); },
      destroy: function () { clearInterval(refresh); clearTimeout(st.timer); }
    };
  }

  return { num: num, inputsKey: inputsKey, buildRequest: buildRequest, switchMode: switchMode, submitGate: submitGate,
           describe: describe, attach: attach, MODES: MODES };
});
