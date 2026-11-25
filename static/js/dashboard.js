/* TradeMate Dashboard: composes the account / performance / status tiles from REAL endpoints only.
   Sources: /api/broker (account, positions), /api/history (closed trades, 30 days), /api/risk/state (today, limits, emergency lock),
   /api/autopilot/status, /api/selection (selected strategy, instrument, timeframe), /api/decision/current (Watchdog monitoring), /api/trading (trading mode),
   /api/executions (recent order attempts), /api/rapid-fire/status (optional: the Rapid Fire engine).
   Any source that cannot be read leaves its tiles at "---" with an honest note; nothing is estimated or invented.
   The tiles filled by app.js (acct-balance, acct-equity, acct-free-margin, acct-profit, acct-margin, mt5-state, exec-state) are left to it. */
(function () {
  'use strict';
  var DASH = '---', WINDOW_DAYS = 30, REFRESH_MS = 5000;
  var fmt = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  function $(id) { return document.getElementById(id); }
  function set(id, text) { var el = $(id); if (el && el.textContent !== text) el.textContent = text; }
  function money(v, cur) { return typeof v === 'number' && isFinite(v) ? fmt.format(v) + (cur ? ' ' + cur : '') : DASH; }
  function signed(v, cur) { return typeof v === 'number' && isFinite(v) ? (v > 0 ? '+' : '') + fmt.format(v) + (cur ? ' ' + cur : '') : DASH; }
  function tone(id, v) {
    var el = $(id); if (!el) return;
    el.classList.remove('pl-pos', 'pl-neg', 'pl-zero');
    el.classList.add(typeof v !== 'number' || !isFinite(v) || Math.abs(v) < 0.005 ? 'pl-zero' : (v > 0 ? 'pl-pos' : 'pl-neg'));
  }
  function getJSON(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) { return r.json(); }).catch(function () { return null; });
  }
  function visible() { var v = $('view-dashboard'); return !!v && !v.hidden && !document.hidden; }
  function mark(id, cls) { var el = $(id); if (el) el.className = cls || ''; }

  var busy = false;
  function refresh() {
    if (busy || !visible()) return;
    busy = true;
    Promise.all([getJSON('/api/broker'), getJSON('/api/history?days=' + WINDOW_DAYS + '&limit=500'), getJSON('/api/risk/state'),
                 getJSON('/api/autopilot/status'), getJSON('/api/selection'), getJSON('/api/rapid-fire/status'),
                 getJSON('/api/trading'), getJSON('/api/executions')])
      .then(function (r) {
        var sel = r[4], q = sel && sel.ok && sel.selected && sel.symbol && sel.timeframe
          ? '/api/decision/current?symbol=' + encodeURIComponent(sel.symbol) + '&strategy=' + encodeURIComponent(sel.selected.id) + '&timeframe=' + encodeURIComponent(sel.timeframe) : null;
        return (q ? getJSON(q) : Promise.resolve(null)).then(function (dec) { render(r[0], r[1], r[2], r[3], r[4], r[5]); renderStatus(r[0], r[3], r[4], r[6], r[7], dec); });
      })
      .then(function () { busy = false; }, function () { busy = false; });
  }

  // Trading status: the instrument / timeframe come from the saved Command Centre selection, the Watchdog state from the decision hub,
  // the mode from /api/trading, the last execution from TradeMate's own order journal. Unreadable source -> '---', never a guess.
  function renderStatus(broker, ap, sel, trading, execs, dec) {
    var acct = broker && broker.account;
    set('dash-currency', acct && acct.currency ? acct.currency : DASH);
    set('dash-env', acct ? String(acct.environment || '').toUpperCase() + (acct.login ? ' · account ' + acct.login : '') : 'broker not connected');
    var modeText = DASH, modeCls = '';
    if (trading && typeof trading.mode === 'string') {
      var env = acct ? String(acct.environment || '').toUpperCase() : '';
      modeText = (env ? env + ' · ' : '') + trading.mode.toUpperCase() + (trading.enabled === false ? ' · TRADING OFF' : '');
      modeCls = trading.enabled === false ? 'is-warn' : (env === 'LIVE' ? 'is-warn' : '');
    }
    set('dash-mode', modeText); mark('dash-mode', modeCls);
    var has = sel && sel.ok && sel.selected;
    set('dash-instrument', has && sel.symbol ? sel.symbol : DASH);
    set('dash-timeframe', has && sel.timeframe ? sel.timeframe : DASH);
    var wd = DASH, wdCls = '';
    if (!has) wd = 'No strategy selected';
    else if (dec && dec.ok) { wd = dec.monitoring ? 'MONITORING' + (dec.monitor_state ? ' · ' + String(dec.monitor_state).toUpperCase() : '') : 'NOT MONITORING'; wdCls = dec.monitoring ? 'is-ok' : ''; }
    set('dash-watchdog', wd); mark('dash-watchdog', wdCls);
    var le = DASH, leCls = '';
    if (execs && execs.ok && Array.isArray(execs.executions)) {
      var r0 = execs.executions[0];
      if (!r0) le = 'No order attempts recorded';
      else {
        le = [String(r0.action || 'order').toUpperCase(), r0.symbol, r0.side ? String(r0.side).toUpperCase() : null, r0.volume != null ? r0.volume + ' lot' : null, r0.state ? '(' + String(r0.state) + ')' : null].filter(Boolean).join(' ');
        leCls = /fail|reject|unknown|uncertain/i.test(String(r0.state || '')) ? 'is-warn' : '';
      }
      if (execs.unresolved && execs.unresolved.length) { le += ' · ' + execs.unresolved.length + ' outcome(s) unresolved'; leCls = 'is-warn'; }
    }
    set('dash-lastexec', le); mark('dash-lastexec', leCls);
  }

  function renderPerf(hist, cur) {
    var bars = $('perf-bars'), dist = $('perf-dist'), recent = $('perf-recent'); if (!bars) return;
    var ok = hist && hist.ok && Array.isArray(hist.trades);
    var t = ok ? hist.trades.filter(function (x) { return typeof x.net === 'number'; }) : [];
    if (!t.length) {
      var msg = ok ? 'No closed trades yet.' : ((hist && hist.message) || 'MT5 history is unavailable.');
      bars.replaceChildren(el('p', 'note', msg)); dist.replaceChildren(); recent.replaceChildren(el('li', 'note', msg));
      ['perf-won', 'perf-lost', 'perf-even', 'perf-count'].forEach(function (id) { set(id, DASH); });
      return;
    }
    var chrono = t.slice().sort(function (a, b) { return (a.time_close || 0) - (b.time_close || 0); });
    var last = chrono.slice(-30), maxAbs = Math.max.apply(null, last.map(function (x) { return Math.abs(x.net); })) || 1;
    bars.replaceChildren.apply(bars, last.map(function (x) {
      var b = el('span', 'perf-bar ' + (x.net > 0 ? 'is-win' : x.net < 0 ? 'is-loss' : 'is-even'));
      b.style.height = Math.max(4, Math.round(Math.abs(x.net) / maxAbs * 100)) + '%';
      b.setAttribute('title', (x.symbol || '') + ' ' + signed(x.net, cur));
      return b;
    }));
    var won = t.filter(function (x) { return x.net > 0.005; }).length, lost = t.filter(function (x) { return x.net < -0.005; }).length, even = t.length - won - lost;
    dist.replaceChildren(seg('win', won, t.length), seg('loss', lost, t.length), seg('even', even, t.length));
    set('perf-won', String(won)); set('perf-lost', String(lost)); set('perf-even', String(even)); set('perf-count', String(t.length));
    recent.replaceChildren.apply(recent, chrono.slice(-5).reverse().map(function (x) {
      var li = el('li'); li.appendChild(el('span', null, (x.symbol || '') + ' ' + String(x.direction || '').toUpperCase()));
      var v = el('strong', x.net > 0 ? 'pl-pos' : x.net < 0 ? 'pl-neg' : 'pl-zero', signed(x.net, cur)); li.appendChild(v); return li;
    }));
  }
  function seg(kind, n, total) { var s = el('span', 'perf-seg is-' + kind); s.style.flexGrow = String(n); s.style.display = n ? '' : 'none'; s.title = kind + ': ' + n + ' of ' + total; return s; }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  function render(broker, hist, risk, ap, sel, rf) {
    var acct = broker && broker.account, cur = acct && acct.currency;
    // active positions
    var pos = broker && Array.isArray(broker.positions) ? broker.positions : null;
    set('dash-positions', pos ? String(pos.length) : DASH);
    set('dash-positions-sub', pos ? (pos.length ? pos.reduce(function (n, p) { return n + (p.volume || 0); }, 0).toFixed(2) + ' lots open' : 'no open positions') : 'MT5 unavailable');

    // closed trades / realised / win rate: broker deal history of the last 30 days (de-duplicated by the server)
    if (hist && hist.ok && Array.isArray(hist.trades)) {
      var t = hist.trades, withNet = t.filter(function (x) { return typeof x.net === 'number'; });
      var net = withNet.reduce(function (n, x) { return n + x.net; }, 0), wins = withNet.filter(function (x) { return x.net > 0; }).length;
      var c = hist.currency || cur;
      set('dash-closed', String(t.length));
      set('dash-closed-sub', 'last ' + WINDOW_DAYS + ' days' + (hist.hidden_count ? ' (' + hist.hidden_count + ' cleared from view)' : ''));
      set('dash-realised', withNet.length ? signed(net, c) : (t.length ? DASH : money(0, c)));
      tone('dash-realised', withNet.length ? net : 0);
      set('dash-realised-sub', 'net of ' + withNet.length + ' closed trade' + (withNet.length === 1 ? '' : 's') + ', last ' + WINDOW_DAYS + ' days');
      set('dash-winrate', withNet.length ? (wins / withNet.length * 100).toFixed(1) + '%' : 'n/a');
      set('dash-winrate-sub', withNet.length ? wins + ' won, ' + (withNet.length - wins) + ' not won' : 'no closed trades yet');
    } else {
      ['dash-closed', 'dash-realised', 'dash-winrate'].forEach(function (id) { set(id, DASH); });
      set('dash-closed-sub', (hist && hist.message) || 'MT5 history unavailable');
      set('dash-realised-sub', 'MT5 history unavailable'); set('dash-winrate-sub', 'MT5 history unavailable');
    }

    renderPerf(hist, cur);

    // today + Risk Engine
    var riskText = 'Unavailable', riskTone = 'idle';
    if (risk && risk.ok && risk.state) {
      var s = risk.state, c2 = s.currency || cur;
      var today = typeof s.todays_pl === 'number' ? s.todays_pl : null;
      set('dash-today', today == null ? DASH : signed(today, c2)); tone('dash-today', today);
      var why = s.history_error || 'daily figures unavailable', subEl = $('dash-today-sub');
      set('dash-today-sub', today == null ? 'unknown: ' + (why.length > 60 ? why.slice(0, 57).replace(/\s+\S*$/, '') + '... (details: hold or tap)' : why) : 'closed ' + signed(s.todays_closed_pl, '') + ' + floating, local day');
      if (subEl) subEl.title = today == null ? why : '';
      var emerg = risk.emergency && risk.emergency.locked;
      if (emerg) { riskText = 'EMERGENCY LOCK'; riskTone = 'bad'; }
      else if (s.daily_locked) { riskText = 'LOCKED: daily loss limit reached'; riskTone = 'bad'; }
      else if (s.todays_loss == null) { riskText = 'Daily loss unknown'; riskTone = 'warn'; }
      else { riskText = 'OK: ' + money(s.daily_remaining, c2) + ' left of ' + money(s.daily_limit_amount, c2) + ' today'; riskTone = 'ok'; }
      if (s.breaches && s.breaches.length && riskTone === 'ok') { riskText = 'ATTENTION: ' + s.breaches[0]; riskTone = 'warn'; }
    } else {
      set('dash-today', DASH); tone('dash-today', null); set('dash-today-sub', 'risk state unavailable');
    }
    set('dash-risk', riskText); mark('dash-risk', riskTone === 'ok' ? 'is-ok' : riskTone === 'bad' ? 'is-bad' : riskTone === 'warn' ? 'is-warn' : '');

    // Autopilot
    var apText = DASH, apCls = '';
    if (ap && ap.ok) {
      if (ap.emergency_stopped) { apText = 'EMERGENCY STOPPED'; apCls = 'is-bad'; }
      else if (ap.armed) { apText = (ap.paused ? 'PAUSED' : 'RUNNING') + (ap.symbol ? ' · ' + ap.symbol : ''); apCls = ap.paused ? 'is-warn' : 'is-ok'; }
      else apText = 'OFF';
    }
    set('dash-autopilot', apText); mark('dash-autopilot', apCls);

    // selected strategy (the Command Centre selection); Rapid Fire is a separate engine
    if (sel && sel.ok && sel.selected) {
      set('dash-strategy', sel.selected.name + (sel.symbol ? ' · ' + sel.symbol : '') + (sel.timeframe ? ' ' + sel.timeframe : ''));
      mark('dash-strategy', '');
    } else { set('dash-strategy', 'NOT ACTIVE'); mark('dash-strategy', ''); }
    var rfText = 'Unavailable', rfCls = '';
    if (rf && rf.ok) {
      rfText = String(rf.state || 'STOPPED').replace('_', ' ');
      if (rf.config && rf.state === 'RUNNING') rfText += ' · ' + (rf.config.symbol || '') + ' ' + (rf.config.timeframe || '');
      rfCls = rf.state === 'RUNNING' ? 'is-ok' : rf.state === 'BLOCKED' ? 'is-bad' : '';
    }
    set('dash-rf', rfText); mark('dash-rf', rfCls);

    // headline chip
    var mt5 = ($('mt5-state') || {}).textContent;
    var chip = $('dash-status-chip');
    if (chip) {
      var bad = mt5 !== 'CONNECTED' || riskTone === 'bad';
      chip.textContent = bad ? (mt5 !== 'CONNECTED' ? 'MT5 not connected' : 'Trading locked') : (riskTone === 'warn' ? 'Attention' : 'All systems normal');
      chip.setAttribute('data-tone', bad ? 'bad' : riskTone === 'warn' ? 'warn' : 'ok');
    }
    set('dash-note', 'Updated ' + new Date().toLocaleTimeString() + '. Realised P/L, closed trades and win rate cover the last ' + WINDOW_DAYS + ' days of MT5 deal history.');
  }

  window.TradeMateRouter.on(function () { refresh(); });
  setInterval(refresh, REFRESH_MS);
  document.addEventListener('visibilitychange', refresh);
  window.TradeMateDashboard = { refresh: refresh };
  refresh();
})();
