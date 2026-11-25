/* TradeMate trading controls (Command Centre + chart side navigation).
   Every copy of the component (data-tc-root) shows the SAME backend state:
     /api/selection          instrument, strategy, execution timeframe (backend validates and saves)
     /api/decision/current   THE authoritative decision for that exact selection (the Watchdog and manual Analyse both publish into it)
     /api/lot                lot size for future entries (backend validates exactly against the instrument; the browser's number is only a request)
     /api/autopilot/*        start / stop / emergency stop / progress (stop conditions are enforced by the backend)
   Nothing here decides a trade. Server text is placed with textContent only. */
(function () {
  'use strict';
  var roots = Array.prototype.slice.call(document.querySelectorAll('[data-tc-root]'));
  if (!roots.length) return;

  var POLL_MS = 2000, AP_POLL_MS = 3000, BROKER_POLL_MS = 20000;
  var sel = { symbol: '', strategy_id: '', timeframe: '' };
  var strategies = [], timeframes = [], symbols = [];
  var decisionSeq = 0, decisionApplied = 0, lastVersion = -1, lastRecordKey = '';
  var apEventId = 0, lotSymbol = '', busy = {};

  function q(root, name) { return root.querySelector('[data-tc="' + name + '"]'); }
  function all(name) { return roots.map(function (r) { return q(r, name); }).filter(Boolean); }
  function setText(name, text) { all(name).forEach(function (n) { n.textContent = text == null ? '' : String(text); }); }
  function each(fn) { roots.forEach(fn); }
  function api(url, opts) {
    opts = opts || {}; opts.cache = 'no-store';
    if (opts.body && typeof opts.body !== 'string') { opts.body = JSON.stringify(opts.body); opts.headers = { 'Content-Type': 'application/json' }; }
    return fetch(url, opts).then(function (r) {
      return r.json().then(function (j) { j.__status = r.status; return j; }, function () { return { ok: false, message: 'The server sent an unreadable answer (HTTP ' + r.status + ').', __status: r.status }; });
    });
  }
  function key() { return sel.symbol + '|' + sel.strategy_id + '|' + sel.timeframe; }

  /* ---------------------------------------------------------------- selects */
  function fillSelect(select, items, value, labelOf) {
    var before = select.value;
    select.textContent = '';
    items.forEach(function (it) {
      var o = document.createElement('option'); o.value = it.value; o.textContent = it.label; select.appendChild(o);
    });
    select.value = value || before;
    if (select.value !== (value || before) && items.length) select.selectedIndex = 0;
  }
  function renderSelects() {
    var syms = symbols.slice();
    if (sel.symbol && syms.indexOf(sel.symbol) < 0) syms.unshift(sel.symbol);
    each(function (r) {
      fillSelect(q(r, 'symbol'), syms.length ? syms.map(function (s) { return { value: s, label: s }; }) : [{ value: '', label: 'No instruments (connect MT5)' }], sel.symbol);
      fillSelect(q(r, 'strategy'), strategies.map(function (s) { return { value: s.id, label: s.name }; }), sel.strategy_id);
      var stratSel = q(r, 'strategy');
      Array.prototype.forEach.call(stratSel.options, function (o) {       /* hover: each option and the select itself carry the strategy's explanation */
        var d = strategies.filter(function (s) { return s.id === o.value; })[0];
        o.title = d && d.tooltip ? d.tooltip : '';
      });
      var curDef = strategies.filter(function (s) { return s.id === sel.strategy_id; })[0];
      stratSel.title = curDef && curDef.tooltip ? curDef.tooltip : '';
      var infoBtn = q(r, 'info'), infoTxt = q(r, 'infotext');
      if (infoBtn) infoBtn.title = curDef && curDef.tooltip ? curDef.tooltip : 'About this strategy';   /* desktop hover; a tap on the button shows the same text (mobile) */
      if (infoTxt) infoTxt.textContent = curDef && curDef.tooltip ? curDef.tooltip : '';
      fillSelect(q(r, 'timeframe'), timeframes.map(function (t) { return { value: t, label: t }; }), sel.timeframe);
      q(r, 'timeframe').disabled = false;                   /* the user's timeframe choice is always available */
    });
    var cur = strategies.filter(function (s) { return s.id === sel.strategy_id; })[0];
    var note = '';
    if (cur && cur.id === 'candle_momentum') note = 'Watches the forming ' + sel.timeframe + ' candle tick by tick; enters when it crosses your trigger distance. Target: your target distance from the actual fill.';
    else if (cur && cur.id === 'market_structure') note = 'Confirmed ' + sel.timeframe + ' swing structure (HH/HL or LH/LL). Enters when the structure is established; fixed target from the actual entry.';
    else if (cur && cur.id === 'bull_bear') note = 'Candle Momentum entries on the forming ' + sel.timeframe + ' candle, only in the direction of the market structure a valid CHoCH established. No CHoCH, or a candle against the structure: no trade.';
    else if (cur && cur.id === 'trendline') note = 'Confirmed ' + sel.timeframe + ' swings. Enters when price respects a line through lower highs / higher lows; targets the indicated level.';
    else if (cur && cur.id === 'choch') note = 'Confirmed CHoCH on ' + sel.timeframe + ' with its higher-timeframe context. Target: 100 pips from the actual entry.';
    setText('tfnote', note);
  }
  function applySelection(j) {
    var changed = key() !== (j.symbol || '') + '|' + (j.strategy_id || '') + '|' + (j.timeframe || '');
    sel = { symbol: j.symbol || '', strategy_id: j.strategy_id || '', timeframe: j.timeframe || '' };
    if (j.timeframes) timeframes = j.timeframes;
    if (j.strategies) strategies = j.strategies;
    renderSelects();
    if (changed) { decisionSeq++; decisionApplied = decisionSeq; lastVersion = -1; renderDecision(null); loadLot(); loadPip(); pollDecision(); }
  }
  function postSelection(patch) {
    return api('/api/selection', { method: 'POST', body: patch }).then(function (j) {
      if (!j.ok) { setText('ap-msg', j.message || 'The selection was refused.'); return loadSelection(); }
      applySelection(j);
    });
  }
  function loadSelection() { return api('/api/selection').then(function (j) { if (j.ok) applySelection(j); }); }

  function loadSymbols() {
    return api('/api/broker').then(function (j) {
      var names = ((j && j.symbols) || []).map(function (s) { return s.name; }).filter(Boolean);
      if (names.join() !== symbols.join()) { symbols = names; renderSelects(); }
      if (!sel.symbol && names.length) postSelection({ symbol: names[0] });
    }).catch(function () { /* the selects keep their last list */ });
  }

  /* ---------------------------------------------------------------- decision */
  function ago(sec) { sec = Math.max(0, Math.round(sec)); return sec < 60 ? sec + 's ago' : Math.round(sec / 60) + ' min ago'; }
  function tone(d) { return d === 'BUY' ? 'buy' : d === 'SELL' ? 'sell' : d === 'WATCH' ? 'watch' : 'wait'; }
  function fmt(n) { return (typeof n === 'number' && isFinite(n)) ? String(+n.toPrecision(8)) : '—'; }

  function renderDecision(body) {
    var rec = body && body.record, res = rec && rec.result;
    var decision = rec && rec.decision, state = rec ? rec.state : null;
    var label = decision || (state === 'stale' ? 'STALE' : state === 'error' ? 'ERROR' : state === 'disconnected' ? 'OFFLINE' : state === 'data_problem' ? 'DATA' : 'NONE');
    all('badge').forEach(function (b) { b.textContent = label; b.setAttribute('data-decision', decision || label); });
    var fresh;
    if (!rec) fresh = 'No analysis yet for this selection';
    else if (state === 'fresh') fresh = 'Updated ' + ago(rec.age_seconds) + ' · ' + (rec.source === 'watchdog' ? 'Watchdog' : 'manual analysis');
    else fresh = (rec.message || 'Not current') + '';
    setText('fresh', fresh);
    var mon = '';
    if (body) mon = body.monitoring ? 'Watchdog monitoring: ' + (body.monitor_state || 'active') : 'Watchdog is not monitoring this selection';
    setText('monitor', mon);
    var reason = '';
    if (res && res.reasons && res.reasons.length) reason = res.reasons[0];
    else if (res && res.context && res.context.summary) reason = res.context.summary;
    else if (rec && rec.message && state !== 'fresh') reason = rec.message;
    setText('reason', reason);
    all('setup').forEach(function (dl) {
      dl.textContent = '';
      if (!res || (decision !== 'BUY' && decision !== 'SELL')) { dl.hidden = true; return; }
      dl.hidden = false;
      var rows = [['Entry (reference)', res.entry && fmt(res.entry.price)], ['Stop-loss', res.stop_loss && fmt(res.stop_loss.price)], ['Take-profit', res.take_profit_1 && fmt(res.take_profit_1.price)],
                  ['Reward : risk', res.risk_reward && typeof res.risk_reward.rr_tp1 === 'number' ? res.risk_reward.rr_tp1.toFixed(2) : '—']];
      if (res.target && res.target.pips) rows.push(['Target', res.target.pips + ' pips from the actual entry']);
      rows.forEach(function (rw) {
        var d = document.createElement('div'), t = document.createElement('dt'), v = document.createElement('dd');
        t.textContent = rw[0]; v.textContent = rw[1] || '—'; d.appendChild(t); d.appendChild(v); dl.appendChild(d);
      });
    });
    // The Command Centre's big decision panel renders the SAME record: no Analyse click needed.
    var lab = window.TradeMateStrategyLab;
    if (lab && lab.show) {
      var k = rec ? (rec.symbol + '|' + rec.strategy_id + '|' + rec.exec_tf + '|' + rec.version) : '';
      if (rec && state === 'fresh' && res && res.timeframes && res.data) { if (k !== lastRecordKey) { lastRecordKey = k; try { lab.show(res); } catch (e) { /* the panel keeps its previous content */ } } }
      else if (lastRecordKey !== (rec ? 'x' + state : 'none')) {
        var hadDecision = lastRecordKey !== '' && lastRecordKey !== 'none';
        lastRecordKey = rec ? 'x' + state : 'none';
        if (!rec && !hadDecision) return;                   // nothing was shown for the old selection: leave the page's own empty state alone
        lab.clear(!rec ? null : state === 'fresh' ? null : (rec.message || 'No current decision.'));
      }
    }
  }
  function pollDecision() {
    if (!sel.symbol || !sel.strategy_id || !sel.timeframe) return;
    var mySeq = ++decisionSeq, myKey = key();
    var url = '/api/decision/current?symbol=' + encodeURIComponent(sel.symbol) + '&strategy=' + encodeURIComponent(sel.strategy_id) + '&timeframe=' + encodeURIComponent(sel.timeframe);
    api(url).then(function (j) {
      if (!j.ok) return;
      if (mySeq < decisionApplied) return;                  // an older response must never overwrite a newer one
      if (myKey !== key()) return;                          // the selection changed while this was in flight
      decisionApplied = mySeq;
      renderDecision(j);
    }).catch(function () {
      if (mySeq < decisionApplied || myKey !== key()) return;
      decisionApplied = mySeq;
      renderDecision({ record: { state: 'error', message: 'Cannot reach TradeMate; the decision shown is not current.', result: null }, monitoring: false });
    });
  }

  /* ---------------------------------------------------------------- lot size */
  function renderLot(j) {
    if (!j || !j.ok) { setText('lot-msg', (j && j.message) || ''); return; }
    all('lot').forEach(function (i) { if (document.activeElement !== i) i.value = j.saved != null ? String(j.saved) : ''; });
    setText('lot-state', j.will_use ? 'Using ' + j.will_use + ' lots' : 'Not set');
    setText('lot-msg', j.message || '');
    var rules = (j.volume_min != null) ? 'This instrument: minimum ' + j.volume_min + ', maximum ' + j.volume_max + ', step ' + j.volume_step + '.' : '';
    setText('lot-rules', rules);
  }
  function loadLot() { if (!sel.symbol) return; lotSymbol = sel.symbol; api('/api/lot?symbol=' + encodeURIComponent(sel.symbol)).then(function (j) { if (lotSymbol === sel.symbol) renderLot(j); }); }
  function saveLot(root) {
    var v = q(root, 'lot').value.trim();
    var symbol = sel.symbol;
    api('/api/lot', { method: 'POST', body: { symbol: symbol, volume: v } }).then(function (j) {
      if (symbol !== sel.symbol) return;
      if (j.ok) renderLot(j);
      else { setText('lot-msg', j.message || 'That lot size was refused.'); all('lot-state').forEach(function (n) { n.textContent = 'Not changed'; }); loadLot_keepMessage(j); }
    });
  }
  function loadLot_keepMessage(j) {
    var rules = (j.volume_min != null) ? 'This instrument: minimum ' + j.volume_min + ', maximum ' + j.volume_max + ', step ' + j.volume_step + '.' : '';
    if (rules) setText('lot-rules', rules);
  }

  /* ---------------------------------------------------------------- pip size */
  var SOURCES = { browser: 'set by you', settings: 'from settings.toml', automatic: 'automatic' };
  function renderPip(v) {
    if (!v || v.ok === false) { setText('pip-msg', (v && v.message) || ''); return; }
    all('pip').forEach(function (i) { if (document.activeElement !== i) i.value = v.source === 'browser' || v.source === 'settings' ? String(v.size) : ''; });
    var unitName = v.unit_name || 'pip', verified = v.status ? v.status === 'VERIFIED' : true;
    setText('pip-state', v.defined ? '1 ' + unitName + ' = ' + v.size + ' (' + (SOURCES[v.source] || v.rule) + (v.status ? ', ' + v.status.toLowerCase() : '') + ')' : 'Not defined');
    var money = (v.money_per_unit_1lot != null) ? ' 1.00 lot = ' + (+v.money_per_unit_1lot).toFixed(2) + ' ' + (v.currency || '') + ' per ' + unitName + '.' : '';
    var why = (!verified && v.problems && v.problems.length) ? ' UNVERIFIED, automatic execution is blocked: ' + v.problems.join(' ') : '';
    setText('pip-msg', v.defined ? (v.source === 'automatic' ? 'Detected from the broker contract, nothing to enter. ' : 'Your override is in use. ') + v.message + money + why : v.message + why);
    all('pip-clear').forEach(function (b) { b.hidden = v.source !== 'browser'; });
    all('pip-preview').forEach(function (ul) {
      ul.textContent = '';
      (v.preview || []).forEach(function (row) {
        var li = document.createElement('li'); li.textContent = row.label + ': ' + row.pips + ' pips = ' + row.price_distance + ' in price'; ul.appendChild(li);
      });
    });
  }
  function loadPip() { if (!sel.symbol) return; var s = sel.symbol; api('/api/pip?symbol=' + encodeURIComponent(s)).then(function (v) { if (s === sel.symbol) renderPip(v); }); }
  function savePip(root, clear) {
    var s = sel.symbol, body = { symbol: s, size: clear ? null : q(root, 'pip').value.trim() };
    api('/api/pip', { method: 'POST', body: body }).then(function (v) {
      if (s !== sel.symbol) return;
      if (v.ok === false || v.__status >= 400) { setText('pip-msg', v.message || 'That pip size was refused.'); return; }
      renderPip(v);
    });
  }

  /* ---------------------------------------------------------------- autopilot */
  var TONES = { paused: 'warn', running: 'ok', finished: 'done', stopped: 'idle', emergency_stopped: 'bad', interrupted: 'warn', blocked: 'bad', off: 'idle' };
  var LABELS = { paused: 'Paused', running: 'Running', finished: 'Finished', stopped: 'Stopped', emergency_stopped: 'Emergency locked', interrupted: 'Paused after restart', blocked: 'Blocked', off: 'Off' };
  function renderAutopilot(st) {
    if (!st) return;
    all('ap-state').forEach(function (c) { c.textContent = LABELS[st.state] || st.state; c.setAttribute('data-tone', TONES[st.state] || 'idle'); });
    each(function (r) {
      var running = !!st.armed;
      q(r, 'ap-start').textContent = st.state === 'interrupted' ? 'Resume Autopilot' : 'Start Autopilot';
      q(r, 'ap-start').disabled = running;
      q(r, 'ap-stop').disabled = !running;
      var pauseBtn = q(r, 'ap-pause');
      if (pauseBtn) { pauseBtn.disabled = !running; pauseBtn.textContent = st.paused ? 'Resume' : 'Pause'; pauseBtn.setAttribute('aria-pressed', st.paused ? 'true' : 'false'); }
      var radios = r.querySelectorAll('[data-tc="mode"]');
      Array.prototype.forEach.call(radios, function (rd) {
        if (st.mode && (running || st.state === 'interrupted')) rd.checked = (rd.value === st.mode);
        rd.disabled = running;
      });
      ['runs', 'profit', 'hours'].forEach(function (m) {
        var input = q(r, 'val-' + m), chosen = r.querySelector('[data-tc="mode"]:checked');
        var active = !!chosen && chosen.value === m;
        input.disabled = running || !active;                                 // only the selected mode's field is editable
        input.closest('.tc-mode').classList.toggle('is-active', active);
        if (st.mode === m && (running || st.state === 'interrupted') && document.activeElement !== input) input.value = String(st.value);
      });
    });
    setText('currency', st.currency || 'account currency');
    var p = st.progress;
    all('progress').forEach(function (box) { box.hidden = !p; });
    if (p) {
      var cur = p.unit === 'currency' ? p.current + ' ' + (st.currency || '') : p.unit === 'hours' ? (+p.current).toFixed(2) + ' h' : p.current;
      var tgt = p.unit === 'currency' ? p.target + ' ' + (st.currency || '') : p.unit === 'hours' ? p.target + ' h' : p.target;
      setText('progress-text', st.mode_label + ': ' + cur + ' of ' + tgt + ' · ' + st.completed_runs + ' run(s) done · net ' + st.net_pnl + ' ' + (st.currency || '') + ' · ' + st.open_trades + ' open');
      all('bar').forEach(function (b) { b.style.width = Math.round((p.fraction || 0) * 100) + '%'; });
    }
    var msg = st.message || '';
    if (st.stopped_reason && !st.armed) msg = st.stopped_reason + (st.message && st.message !== st.stopped_reason ? ' ' + st.message : '');
    setText('ap-msg', msg);
    (st.events || []).forEach(function (e) { if (e.id <= apEventId) return; apEventId = Math.max(apEventId, e.id); if (apBaselined) notify(e); });   /* the first response only sets the baseline: old events never replay as alerts */
    apBaselined = true;
  }
  function notify(e) {
    if (e.level === 'success' || e.level === 'error' || e.level === 'warning') {
      try { if (window.Notification && Notification.permission === 'granted') new Notification('TradeMate Autopilot', { body: e.text, tag: 'ap-' + e.id }); } catch (x) { /* optional */ }
    }
  }
  var apBaselined = false;
  function pollAutopilot() { api('/api/autopilot/status?after_event_id=' + apEventId).then(renderAutopilot).catch(function () {}); }
  function startAutopilot(root) {
    var chosen = root.querySelector('[data-tc="mode"]:checked');
    if (!chosen && !busy.resume) { setText('ap-msg', 'Choose one stop condition first: number of runs, profit target or elapsed hours.'); return; }
    var mode = chosen ? chosen.value : null, value = mode ? q(root, 'val-' + mode).value.trim() : null;
    api('/api/autopilot/start', { method: 'POST', body: { mode: mode, value: value, resume: /Resume/.test(q(root, 'ap-start').textContent) } }).then(function (j) {
      renderAutopilot(j); if (!j.ok) setText('ap-msg', j.message);
    });
  }
  function bindAutopilot() {
    each(function (r) {
      q(r, 'ap-start').addEventListener('click', function () { startAutopilot(r); });
      q(r, 'ap-stop').addEventListener('click', function () { api('/api/autopilot/stop', { method: 'POST', body: {} }).then(renderAutopilot); });
      var pb = q(r, 'ap-pause');
      if (pb) pb.addEventListener('click', function () { api(pb.textContent === 'Resume' ? '/api/autopilot/resume-pause' : '/api/autopilot/pause', { method: 'POST', body: {} }).then(renderAutopilot); });
      q(r, 'ap-emergency').addEventListener('click', function () { api('/api/autopilot/emergency-stop', { method: 'POST', body: {} }).then(renderAutopilot); });
      Array.prototype.forEach.call(r.querySelectorAll('[data-tc="mode"]'), function (rd) {
        rd.addEventListener('change', function () {
          each(function (o) { Array.prototype.forEach.call(o.querySelectorAll('[data-tc="mode"]'), function (x) { x.checked = (x.value === rd.value); }); });
          each(function (o) {
            ['runs', 'profit', 'hours'].forEach(function (m) {
              var inp = q(o, 'val-' + m); inp.disabled = (m !== rd.value); inp.closest('.tc-mode').classList.toggle('is-active', m === rd.value);
            });
          });
        });
      });
      q(r, 'lot-save').addEventListener('click', function () { saveLot(r); });
      q(r, 'pip-save').addEventListener('click', function () { savePip(r, false); });
      q(r, 'pip-clear').addEventListener('click', function () { savePip(r, true); });
      q(r, 'pip').addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); savePip(r, false); } });
      q(r, 'lot').addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); saveLot(r); } });
      q(r, 'symbol').addEventListener('change', function (e) { postSelection({ symbol: e.target.value }); });
      q(r, 'strategy').addEventListener('change', function (e) { postSelection({ strategy: e.target.value }); });
      (function () {
        var b = q(r, 'info'), t = q(r, 'infotext');
        if (!b || !t) return;
        function toggle(ev) {
          if (ev) { ev.preventDefault(); ev.stopPropagation(); }
          var open = t.hidden; t.hidden = !open; b.setAttribute('aria-expanded', open ? 'true' : 'false');
        }
        b.addEventListener('click', toggle);
      })();
      q(r, 'timeframe').addEventListener('change', function (e) { postSelection({ timeframe: e.target.value }); });
    });
  }

  /* ---------------------------------------------------------------- chart side navigation */
  function bindSideNav() {
    Array.prototype.forEach.call(document.querySelectorAll('.trade-panel'), function (aside) {
      var tabs = aside.querySelectorAll('[data-sidenav-tab]'), panes = aside.querySelectorAll('[data-sidenav-pane]');
      Array.prototype.forEach.call(tabs, function (tab) {
        tab.addEventListener('click', function () {
          Array.prototype.forEach.call(tabs, function (t) { t.setAttribute('aria-selected', t === tab ? 'true' : 'false'); });
          Array.prototype.forEach.call(panes, function (p) { p.hidden = p.getAttribute('data-sidenav-pane') !== tab.getAttribute('data-sidenav-tab'); });
        });
      });
    });
  }

  bindAutopilot(); bindSideNav();
  loadSelection().then(function () { loadLot(); loadPip(); loadSymbols(); pollDecision(); });
  pollAutopilot();
  setInterval(pollDecision, POLL_MS);
  setInterval(pollAutopilot, AP_POLL_MS);
  setInterval(loadSymbols, BROKER_POLL_MS);
  window.TradeMateRouter.on(function () { loadSelection(); });          // the Strategy page can change the selection: re-read it whenever a page is opened
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { pollDecision(); pollAutopilot(); } });
})();
