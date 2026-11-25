/* TradeMate: Bull/Bear live panel (Command Centre). A structure-gated copy of the Candle Momentum panel, plus the Anti-Twin controls.

   Renders the engine's OWN state: /api/bull-bear/state returns the same lifecycle the Watchdog notifies from and the Autopilot trades from. This file decides
   nothing about trading. It never invents a number: a missing value is shown as a dash.

   The 1-second refresh below is only how often THIS PAGE asks for the state. It is not the strategy's tick-monitoring frequency: that is the engine's own cadence,
   shown in the "Engine" row (engine.cadence_seconds). */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TradeMateBullBear = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var STATES = {
    NEW_CANDLE: ['New candle', 'idle', 'A new candle has just opened; its open price is known and nothing has been judged yet.'],
    MONITORING: ['Monitoring', 'idle', 'Price equals the candle open, so the candle has no direction yet.'],
    BULLISH: ['Bullish', 'info', 'Price is above the candle open. Waiting for it to reach the bullish trigger. A bullish candle alone is never traded.'],
    BEARISH: ['Bearish', 'info', 'Price is below the candle open. Waiting for it to reach the bearish trigger. A bearish candle alone is never traded.'],
    TRIGGER_REACHED: ['Trigger reached', 'warn', 'Price crossed the trigger. The entry is being evaluated on the current quote right now.'],
    ENTRY_VALIDATION: ['Validating entry', 'warn', 'Checking the executable quote: is it fresh, is the target still available, is a protective stop possible?'],
    RISK_PENDING: ['Risk Engine pending', 'good', 'A valid entry exists. The Risk Engine and executor decide next. A notification is not permission to trade.'],
    EXECUTING: ['Executing', 'good', 'The Risk Engine approved. The order is being submitted to MT5.'],
    POSITION_OPEN: ['Position open', 'good', 'MT5 confirmed a position for this candle. Its target is measured from the actual fill.'],
    TARGET_REACHED: ['Target reached', 'good', 'Price reached the target; the broker take-profit closes the position.'],
    POSITION_CLOSED: ['Position closed', 'idle', 'The position for this candle is closed.'],
    INVALIDATED: ['Invalidated', 'bad', 'The thesis broke before entry: price came back inside the trigger. No order was sent.'],
    MISSED: ['Missed', 'bad', 'The opportunity was gone (target no longer reasonably available, or price moved away before the order). No order was sent. Nothing is chased.'],
    REJECTED: ['Rejected', 'bad', 'A safety rule refused the entry (Risk Engine, protective stop, unverified instrument data ...). No order was sent.'],
    EXECUTION_FAILED: ['Execution failed', 'bad', 'The order could not be placed or confirmed. It is not retried unless a bounded retry is configured and the order provably never left.'],
    EXPIRED: ['Expired', 'idle', 'The candle closed, or the signal outlived its validity, without an entry.']
  };

  function num(v) { return typeof v === 'number' && isFinite(v) ? v : null; }
  function fmtPrice(v, digits) {
    v = num(v);
    if (v === null) return '\u2014';
    var d = num(digits);
    return d !== null && d >= 0 && d <= 10 ? v.toFixed(d) : String(v);
  }
  function fmtUnits(v) { v = num(v); return v === null ? '\u2014' : (Math.round(v * 100) / 100) + ' units'; }
  function stateInfo(state) { return STATES[state] || [state || 'No candle yet', 'idle', 'Waiting for the broker\u2019s forming candle.']; }

  /* distance from the live price to the trigger, in strategy units; null when it cannot be known */
  function distanceToTrigger(lc, unit) {
    if (!lc || num(lc.trigger_price) === null || num(lc.price) === null || !(num(unit) > 0)) return null;
    var reached = lc.direction === 'bullish' ? lc.price >= lc.trigger_price : lc.direction === 'bearish' ? lc.price <= lc.trigger_price : false;
    return reached ? 0 : Math.abs(lc.trigger_price - lc.price) / unit;
  }

  function eventTime(now, then) {
    now = num(now); then = num(then);
    if (now === null || then === null || then <= 0) return '\u2014';
    var date = new Date(then * 1000);
    if (!isFinite(date.getTime())) return '\u2014';
    var seconds = Math.max(0, now - then);
    var elapsed = seconds < 1 ? 'less than 1 second ago' : seconds < 60 ? Math.floor(seconds) + ' seconds ago' :
      seconds < 3600 ? Math.floor(seconds / 60) + ' minutes ago' : seconds < 86400 ? Math.floor(seconds / 3600) + ' hours ago' :
      Math.floor(seconds / 86400) + ' days ago';
    return date.toLocaleString() + ' · ' + elapsed;
  }

  function ago(now, then) { return eventTime(now, then); }

  function structureText(st) {
    if (!st) return 'Not read yet';
    return st.direction ? (st.direction === 'bullish' ? 'Bullish' : 'Bearish') + (st.previous_direction ? ' (reversed from ' + st.previous_direction + ')' : '') : 'None: no CHoCH yet, nothing is traded';
  }
  function chochText(st) {
    if (!st) return '\u2014';
    if (st.state !== 'ESTABLISHED') return st.unavailable ? 'Unavailable: ' + st.summary : 'Waiting for a valid CHoCH';
    return 'Valid ' + st.direction + ' CHoCH' + (num(st.choch_time) !== null ? ' at ' + new Date(st.choch_time * 1000).toLocaleString() : '') + (st.reversals ? ' \u00b7 ' + st.reversals + ' direction change(s)' : '');
  }
  function antiTwinText(cfg, at, locked) {
    if (locked) return 'Emergency Stop active: no reversal can open';
    if (!cfg.anti_twin_enabled) return 'Off';
    var legs = (at && at.legs) || [], active = legs.filter(function (l) { return ['WATCHING', 'ARMED', 'CLOSING', 'AWAITING_CLOSE', 'OPENING'].indexOf(l.state) >= 0; });
    return 'On \u00b7 switches direction on the first adverse move' + (active.length ? ' \u00b7 ' + active.map(function (l) { return '#' + l.ticket + ' ' + l.state.toLowerCase(); }).join(', ') : '');
  }

  /* [{label, value, tone?}] for the live grid, from the /api/bull-bear/state payload. Pure. */
  function rows(d) {
    if (!d || !d.ok) return [];
    var cfg = d.config || {}, m = d.machine || {}, lc = m.current || null, q = d.quote || {}, conv = d.conversion || {}, eng = d.engine || {}, feed = d.feed || {};
    var digits = conv.digits, unit = conv.unit, info = stateInfo(lc && lc.state);
    var dist = distanceToTrigger(lc, unit);
    var target = lc && num(lc.target_price) !== null ? [fmtPrice(lc.target_price, digits), lc.entry_price !== null && lc.entry_price !== undefined ? 'from the actual fill' : 'if filled at the validated quote'] :
      lc && lc.plan_target_price !== null && lc.plan_target_price !== undefined ? [fmtPrice(lc.plan_target_price, digits), 'plan: trigger + target'] : ['\u2014', ''];
    var wd = d.watchdog || {}, ap = d.autopilot || {}, risk = (lc && lc.risk_decision) || {};
    var out = [
      { label: 'Strategy', value: 'Bull/Bear' },
      { label: 'Instrument / timeframe', value: (d.symbol || '\u2014') + ' \u00b7 ' + (d.timeframe || '\u2014') },
      { label: 'Market structure', value: structureText(m.structure), tone: m.structure && m.structure.direction ? 'good' : 'warn' },
      { label: 'CHoCH status', value: chochText(m.structure), tone: m.structure && m.structure.state === 'ESTABLISHED' ? 'good' : 'warn' },
      { label: 'Entry gate', value: lc && lc.gate ? (lc.gate.ok ? 'Open: ' : 'Closed: ') + lc.gate.reason : 'No candle judged yet', tone: lc && lc.gate && lc.gate.ok ? 'good' : 'warn' },
      { label: 'Candle open', value: fmtPrice(lc && lc.open, digits) },
      { label: 'Live price (bid / ask)', value: fmtPrice(q.bid, digits) + ' / ' + fmtPrice(q.ask, digits) },
      { label: 'Candle direction', value: lc && lc.direction ? lc.direction : 'neutral' },
      { label: 'Trigger distance', value: fmtUnits(cfg.trigger_units) + (conv.trigger_price_distance ? ' (' + fmtPrice(conv.trigger_price_distance, digits) + ' in price)' : '') },
      { label: 'Trigger price', value: fmtPrice(lc && lc.trigger_price, digits) },
      { label: 'Distance remaining to trigger', value: dist === null ? '\u2014' : dist === 0 ? 'Reached' : fmtUnits(dist) },
      { label: 'Target distance', value: fmtUnits(cfg.target_units) },
      { label: 'Expected target price', value: target[0] + (target[1] ? ' (' + target[1] + ')' : '') },
      { label: 'Actual entry price', value: lc && lc.entry_price !== null && lc.entry_price !== undefined ? fmtPrice(lc.entry_price, digits) : 'Not executed' },
      { label: 'Lifecycle state', value: info[0], tone: info[1] },
      { label: 'Watchdog', value: wd.active ? 'Monitoring tick by tick' : 'Not running (start the Watchdog)', tone: wd.active ? 'good' : 'warn' },
      { label: 'Autopilot', value: ap.armed ? 'On' : 'Off', tone: ap.armed ? 'good' : 'idle' },
      { label: 'Risk Engine decision', value: risk.status && risk.status !== 'none' ? risk.status + (risk.message ? ': ' + risk.message : '') : 'None yet' },
      { label: 'Last update', value: ago(d.server_time, lc && lc.updated_at) },
      { label: 'Engine', value: eng.running ? 'Running, reads the quote every ' + eng.cadence_seconds + ' s' + (feed.price_updates_per_second ? '; broker price updates ' + feed.price_updates_per_second + '/s' : '') : 'Stopped', tone: eng.running ? 'good' : 'warn' },
      { label: 'Instrument conversion', value: conv.status ? conv.status + (conv.status === 'VERIFIED' ? ' (1 unit = ' + fmtPrice(unit, digits) + ')' : (conv.reasons && conv.reasons.length ? ': ' + conv.reasons.join(' ') : '')) : '\u2014',
        tone: conv.status === 'VERIFIED' ? 'good' : 'bad' }
    ];
    out.push({ label: 'Anti-Twin', value: antiTwinText(cfg, d.anti_twin, d.emergency_locked), tone: d.emergency_locked ? 'bad' : cfg.anti_twin_enabled ? 'good' : 'idle' });
    if (feed.problem) out.push({ label: 'Feed problem', value: feed.problem, tone: 'bad' });
    return out;
  }

  /* the exact reason line: why we are where we are (including every missed / rejected entry) */
  function explanation(d) {
    var lc = d && d.machine && d.machine.current;
    if (!lc) return 'Waiting for the broker\u2019s forming candle.';
    return lc.reason || stateInfo(lc.state)[2];
  }

  var FIELDS = [['trigger_units', 'trigger_units'], ['target_units', 'target_units'], ['sl_buffer_units', 'sl_buffer_units'], ['max_slippage_units', 'max_slippage_units'],
                ['signal_expiry_seconds', 'signal_expiry_seconds'], ['quote_max_age_seconds', 'quote_max_age_seconds'],
                ['max_entry_attempts', 'max_entry_attempts']];
  var NULLABLE = { max_slippage_units: 1 };

  /* form values -> the POST body. Empty optional fields are sent as null; nothing is invented or defaulted here (the server validates). */
  function formBody(symbol, timeframe, values, enabled, anti) {
    var body = { symbol: symbol, timeframe: timeframe, enabled: !!enabled };
    if (anti) { body.anti_twin_enabled = !!anti.enabled; }
    FIELDS.forEach(function (f) {
      var raw = values[f[0]];
      if (raw === undefined) return;
      raw = String(raw).trim();
      if (raw === '') { if (NULLABLE[f[0]]) body[f[0]] = null; return; }
      body[f[0]] = Number(raw);
    });
    return body;
  }

  /* ----------------------------------------------------------------------------------------- DOM (browser only) */
  var bbShared = { at: 0, key: '', promise: null };
  function fetchState(symbol, tf) {
    var key = symbol + '|' + tf, now = Date.now();
    if (bbShared.key === key && bbShared.promise && now - bbShared.at < 700) return bbShared.promise;      // the two panel copies share one request
    bbShared.key = key; bbShared.at = now;
    bbShared.promise = fetch('/api/bull-bear/state?symbol=' + encodeURIComponent(symbol) + '&timeframe=' + encodeURIComponent(tf), { cache: 'no-store' }).then(function (r) { return r.json(); });
    return bbShared.promise;
  }

  function init(rootEl) {
    var card = rootEl.querySelector('[data-tc="bb-card"]');
    if (!card) return;
    var grid = card.querySelector('[data-bb="grid"]'), chip = card.querySelector('[data-bb="chip"]'), why = card.querySelector('[data-bb="why"]'), msg = card.querySelector('[data-bb="msg"]');
    var timeline = card.querySelector('[data-bb="timeline"]'), enabled = card.querySelector('[data-bb="enabled"]'), atOn = card.querySelector('[data-bb="at-enabled"]');
    var current = '', formLoaded = '', busy = false;
    function sel(n) { var e = rootEl.querySelector('[data-tc="' + n + '"]'); return e ? e.value : ''; }
    function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
    function render(d) {
      grid.textContent = '';
      rows(d).forEach(function (r) {
        var item = el('div', 'tc-cm-row'); item.appendChild(el('dt', null, r.label));
        var dd = el('dd', null, r.value); if (r.tone) dd.setAttribute('data-tone', r.tone); item.appendChild(dd); grid.appendChild(item);
      });
      var lc = d && d.machine && d.machine.current, info = stateInfo(lc && lc.state);
      chip.textContent = info[0]; chip.setAttribute('data-tone', info[1]);
      why.textContent = explanation(d);
      timeline.textContent = '';
      ((lc && lc.transitions) || []).slice(-12).reverse().forEach(function (t) {
        var stamp = eventTime(d.server_time, t.timestamp);
        var li = el('li', null, stamp + ' · ' + t.state + ' — ' + t.explanation);
        li.title = stamp; timeline.appendChild(li);
      });
    }
    function loadForm(symbol, tf) {
      var k = symbol + '|' + tf; if (formLoaded === k) return; formLoaded = k;
      fetch('/api/bull-bear/config?symbol=' + encodeURIComponent(symbol) + '&timeframe=' + encodeURIComponent(tf), { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
        if (!j.ok) return;
        FIELDS.forEach(function (f) { var i = card.querySelector('[data-bb-field="' + f[0] + '"]'); if (i) i.value = j.config[f[0]] === null ? '' : j.config[f[0]]; });
        enabled.checked = !!j.config.enabled;
        atOn.checked = !!j.config.anti_twin_enabled;
      }).catch(function () {});
    }
    function tick() {
      var show = sel('strategy') === 'bull_bear', symbol = sel('symbol'), tf = sel('timeframe');
      card.hidden = !show;
      if (!show || !symbol || !tf || busy || document.hidden) return;
      busy = true; current = symbol + '|' + tf;
      loadForm(symbol, tf);
      fetchState(symbol, tf).then(function (d) { if (current === symbol + '|' + tf) render(d); }).catch(function () { why.textContent = 'The page could not reach TradeMate.'; }).then(function () { busy = false; });
    }
    card.querySelector('[data-bb="save"]').addEventListener('click', function () {
      var values = {}; FIELDS.forEach(function (f) { var i = card.querySelector('[data-bb-field="' + f[0] + '"]'); if (i) values[f[0]] = i.value; });
      msg.textContent = 'Saving\u2026';
      fetch('/api/bull-bear/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formBody(sel('symbol'), sel('timeframe'), values, enabled.checked, { enabled: atOn.checked })) })
        .then(function (r) { return r.json(); }).then(function (j) { msg.textContent = j.message || (j.ok ? 'Saved.' : 'Not saved.'); if (j.ok) { formLoaded = ''; bbShared.at = 0; tick(); }   /* a refused save keeps what was typed */ }).catch(function () { msg.textContent = 'The settings could not be sent.'; });
    });
    card.querySelector('[data-bb="reset"]').addEventListener('click', function () {
      fetch('/api/bull-bear/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ symbol: sel('symbol'), timeframe: sel('timeframe'), reset: true }) })
        .then(function (r) { return r.json(); }).then(function (j) { msg.textContent = j.message || ''; formLoaded = ''; bbShared.at = 0; tick(); }).catch(function () {});
    });
    tick();
    setInterval(tick, 1000);
  }

  if (typeof document !== 'undefined' && document.querySelectorAll) {
    var boot = function () { Array.prototype.forEach.call(document.querySelectorAll('[data-tc-root]'), init); };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
  }

  return { structureText: structureText, chochText: chochText, antiTwinText: antiTwinText, rows: rows, explanation: explanation, stateInfo: stateInfo, distanceToTrigger: distanceToTrigger, fmtPrice: fmtPrice, formBody: formBody, eventTime: eventTime, STATES: STATES };
});
