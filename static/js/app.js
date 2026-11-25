/* TradeMate UI. Vanilla JS.
   Reads broker data from /api/broker and candles from /api/candles.
   Writes (order / modify / close) go to the TradeMate server, which sends them to MT5. The UI never
   invents a position or a price: it only shows what the broker reports, and it says "Executed" only
   after the server reports broker confirmation. */
(function () {
  'use strict';

  var DEFAULT_VIEW = 'dashboard';
  var main = document.getElementById('main');
  var sidebar = document.getElementById('sidebar');
  var scrim = document.getElementById('scrim');
  var toggleBtn = document.getElementById('nav-toggle');
  var toast = document.getElementById('toast');
  var toastTimer = null;

  /* ---------- switching pages: owned by router.js (TradeMateRouter). app.js only subscribes. ---------- */
  var Router = window.TradeMateRouter;
  function currentView() { return Router.current(); }
  Router.onActivate(function () { closeDrawer(false); });      // every nav tap closes the drawer, including a tap on the page you are already on
  Router.on(function () { closeDrawer(false); });

  /* ---------- mobile menu ---------- */
  function isDrawerLayout() { return window.matchMedia('(max-width: 899px)').matches; }

  function syncDrawerAria() {
    if (isDrawerLayout() && !sidebar.classList.contains('is-open')) sidebar.setAttribute('aria-hidden', 'true');
    else sidebar.removeAttribute('aria-hidden');
  }

  function openDrawer() {
    sidebar.classList.add('is-open');
    scrim.hidden = false;
    toggleBtn.setAttribute('aria-expanded', 'true');
    syncDrawerAria();
    var link = sidebar.querySelector('.nav-link[aria-current="page"]') || sidebar.querySelector('.nav-link');
    if (link) link.focus();
  }

  function closeDrawer(returnFocus) {
    var wasOpen = sidebar.classList.contains('is-open');
    sidebar.classList.remove('is-open');
    scrim.hidden = true;
    toggleBtn.setAttribute('aria-expanded', 'false');
    syncDrawerAria();
    if (wasOpen && returnFocus) toggleBtn.focus();
  }

  toggleBtn.addEventListener('click', function () {
    if (sidebar.classList.contains('is-open')) closeDrawer(true); else openDrawer();
  });
  document.getElementById('nav-close').addEventListener('click', function () { closeDrawer(true); });
  scrim.addEventListener('click', function () { closeDrawer(true); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeDrawer(true); });
  window.addEventListener('resize', function () { if (!isDrawerLayout()) closeDrawer(false); syncDrawerAria(); });

  /* ---------- basic button interactions ---------- */
  function showToast(message) {
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.hidden = true; }, 3000);
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;

    // Buttons that only explain they are not implemented yet.
    var noticeBtn = e.target.closest('[data-notice]');
    if (noticeBtn) { showToast(noticeBtn.getAttribute('data-notice')); return; }
  });

  /* ---------- helpers ---------- */
  var DASH = '---';
  var POLL_MS = 2000;
  var CANDLE_POLL_MS = 5000;
  var ZONES_MAX_AGE_MS = CANDLE_POLL_MS * 4;       // FVG / order block zones come from the last candle refresh: older than this (refresh failing, broker down) they are STALE and not drawn
  var QUOTE_MAX_AGE_MS = POLL_MS * 3 + 1000;      // a quote older than this (no successful poll) is STALE, never drawn as live
  var QUOTE_MAX_STATUS_AGE_S = 10;                // the bridge snapshot itself older than this is STALE
  var QUOTES = window.TradeMateQuotes;
  var polling = false;
  var UNREACHABLE = {
    status: { state: 'ERROR', message: 'Cannot reach the TradeMate server.', age_seconds: null, warnings: [] },
    account: null, positions: [], symbols: []
  };

  function $(id) { return document.getElementById(id); }
  function setText(id, text) { var el = $(id); if (el) el.textContent = text; }
  function setClass(id, cls) { var el = $(id); if (el) el.className = cls; }
  function roleAll(role) { return document.querySelectorAll('[data-role="' + role + '"]'); }
  function inRole(root, role) { return root.querySelector('[data-role="' + role + '"]'); }

  var moneyFmt = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  function money(v, cur) { return v == null ? DASH : moneyFmt.format(v) + (cur ? ' ' + cur : ''); }
  function price(v, digits) { return v == null ? DASH : v.toFixed(digits == null ? 5 : digits); }
  function digitsOf(sym) { return sym && typeof sym.digits === 'number' ? sym.digits : 5; }

  /* Profit is BLUE, loss is RED, flat is neutral. Driven only by the sign of the live P/L. */
  function plClass(v) {
    if (v == null || !isFinite(v)) return 'pl-zero';
    if (v >= 0.005) return 'pl-pos';
    if (v <= -0.005) return 'pl-neg';
    return 'pl-zero';
  }
  function plText(v, cur) {
    if (v == null || !isFinite(v)) return DASH;
    var sign = v >= 0.005 ? '+' : (v <= -0.005 ? '-' : '');
    return sign + moneyFmt.format(Math.abs(v)) + (cur ? ' ' + cur : '');
  }
  function setPL(el, v, cur) {
    el.textContent = plText(v, cur);
    el.classList.remove('pl-pos', 'pl-neg', 'pl-zero');
    el.classList.add(plClass(v));
  }

  function stateClass(state) {
    if (state === 'CONNECTED') return 'is-ok';
    if (state === 'CONNECTING') return 'is-warn';
    return 'is-bad';
  }
  function titleCase(text) { return text.charAt(0) + text.slice(1).toLowerCase(); }

  function cell(text, cls) {
    var td = document.createElement('td');
    td.textContent = text;
    if (cls) td.className = cls;
    return td;
  }
  function emptyRow(body, cols, text) {
    var tr = document.createElement('tr');
    tr.className = 'empty-row';
    var td = cell(text);
    td.colSpan = cols;
    tr.appendChild(td);
    body.replaceChildren(tr);
    body._rows = {};
  }
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  /* ---------- shared state (a mirror of the broker, never the authority) ---------- */
  var state = {
    connected: false, account: null, positions: [], symbols: [],
    watch: [], selected: null, tf: 'H1', trading: { enabled: true, max_volume: 1 },
    candles: [], candleKey: '', candleMsg: 'Waiting for market data...', bars: 120,
    extra: {},  // static info of symbols found by search that are not in the snapshot yet (prices are never kept)
    unresolved: [],  // orders sent whose real outcome is still unknown (from TradeMate's local journal)
    spec: {}    // name -> { data, at, busy }: contract/tick data from MT5, needed to turn price moves into money
  };
  var terminals = [];

  function loadLocal(key, fallback) {
    try { var v = JSON.parse(localStorage.getItem(key)); return v == null ? fallback : v; } catch (e) { return fallback; }
  }
  function saveLocal(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* ignore */ } }
  state.watch = loadLocal('trademate.watchlist', null);
  state.selected = loadLocal('trademate.selected', null);
  state.tf = loadLocal('trademate.tf', 'H1');

  function symbolByName(name) {
    for (var i = 0; i < state.symbols.length; i++) if (state.symbols[i].name === name) return state.symbols[i];
    return state.extra[name] || null;
  }
  function symbolByNameSnap(name) {
    for (var i = 0; i < state.symbols.length; i++) if (state.symbols[i].name === name) return true;
    return false;
  }
  function allSymbols() {
    var seen = {}, out = state.symbols.slice();
    out.forEach(function (x) { seen[x.name] = true; });
    Object.keys(state.extra).forEach(function (n) { if (!seen[n]) out.push(state.extra[n]); });
    return out;
  }
  function watchList() {
    return (state.watch || []).filter(function (n) { return !!symbolByName(n) || n === state.selected; });
  }

  /* Contract/tick data for the TP/SL money values. Always asked of MT5 (the lean snapshot has no tick value),
     refreshed every minute because tick values move with exchange rates. */
  function specFor(name) { var e = state.spec[name]; return e && e.data ? e.data : null; }
  function ensureSpec(name) {
    if (!name || !state.connected) return;
    var e = state.spec[name] || (state.spec[name] = { data: null, at: 0, busy: false, error: '' });
    if (e.busy || Date.now() - e.at < (e.data ? 60000 : 15000)) return;
    e.busy = true;
    fetch('/api/symbols/spec?symbol=' + encodeURIComponent(name), { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (res) {
        if (res.ok && res.spec) {
          var first = !e.data;
          e.data = res.spec; e.error = '';
          if (res.account_currency) e.currency = res.account_currency;
          if (first) terminals.forEach(function (t) { t.draw(); });
        } else { e.error = res.message || 'No contract data.'; }
      })
      .catch(function (err) { e.error = String(err && err.message || err); })
      .then(function () { e.busy = false; e.at = Date.now(); });
  }

  var slControllers = [];     // stop-loss panels: recalculated when the symbol or lot size changes
  function setSelected(name) {
    if (!name || name === state.selected) return;
    state.selected = name;
    slControllers.forEach(function (c) { c.refresh(); });
    ensureSpec(name);
    saveLocal('trademate.selected', name);
    state.candles = []; state.candleKey = ''; state.candleMsg = 'Loading ' + name + '...';
    renderSelection();
    loadCandles(true);
  }
  function cycle(step) {
    var list = watchList();
    if (!list.length) return;
    var i = list.indexOf(state.selected);
    i = i < 0 ? 0 : (i + step + list.length) % list.length;
    setSelected(list[i]);
  }

  /* ---------- execution status (every write shows Sending -> Executed / Failed) ---------- */
  var execLog = [];
  var EXEC_ICON = { sending: '', ok: '\u2713', fail: '\u2715', unknown: '!' };
  function execMessage(e) {
    var place = /^PLACE ORDER/.test(e.label);
    if (e.kind === 'sending') return place ? 'Sending order...' : 'Sending...';
    var t = String(e.text).replace(/\s*[\u2713\u2717\u26A0]\s*/g, ' ').replace(/\s+/g, ' ').trim();
    if (e.kind === 'ok') return t.replace(/^Executed/, place ? 'Order executed' : 'Executed');
    if (e.kind === 'fail') return t.replace(/^Failed\s*/, place ? 'Order failed: ' : 'Failed: ');
    return t;
  }
  function renderExec() {
    roleAll('exec').forEach(function (box) {
      var compact = box.hasAttribute('data-compact');
      var shown = execLog.slice(compact ? -1 : -4).reverse();
      if (compact && shown.length && shown[0].kind === 'ok' && Date.now() - shown[0].at > 12000) shown = [];
      var nodes = shown.map(function (e, idx) {
        var div = document.createElement('div');
        div.className = 'exec-line exec-' + e.kind + (idx ? ' exec-old' : '');
        var ico = document.createElement('span'); ico.className = 'exec-ico'; ico.textContent = EXEC_ICON[e.kind] || '';
        var title = document.createElement('span'); title.className = 'exec-title'; title.textContent = e.label;
        var msg = document.createElement('span'); msg.className = 'exec-msg'; msg.textContent = execMessage(e);
        div.appendChild(ico); div.appendChild(title); div.appendChild(msg);
        return div;
      });
      if (!nodes.length && !compact) {
        var idle = document.createElement('div');
        idle.className = 'exec-line exec-idle';
        idle.textContent = 'No orders sent yet.';
        nodes = [idle];
      }
      box.replaceChildren.apply(box, nodes);
    });
  }
  function execStart(label) {
    var entry = { kind: 'sending', label: label, text: 'Sending...', at: Date.now() };
    execLog.push(entry);
    renderExec();
    return {
      done: function (kind, text) {
        entry.kind = kind; entry.text = text; entry.at = Date.now(); renderExec();
        if (kind === 'ok') setTimeout(renderExec, 12500);
      }
    };
  }

  function postJSON(url, body) {
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, 45000);
    return fetch(url, {
      method: 'POST', cache: 'no-store', signal: ctl.signal,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
    }).then(function (r) {
      clearTimeout(timer);
      // The server answers writes with a JSON body even on an error status (state: failed / unknown). Use it.
      return r.json().catch(function () { return null; }).then(function (data) {
        if (data && typeof data === 'object' && (typeof data.state === 'string' || typeof data.ok === 'boolean')) return data;
        throw new Error('HTTP ' + r.status);
      });
    }).catch(function (err) {
        clearTimeout(timer);
        // No usable answer at all. The order may or may not have reached MT5: say exactly that and ask the
        // server's journal (it records every attempt before sending), never claim "failed".
        return { state: 'unknown', message: 'No usable answer from the TradeMate server (' + err.message +
          '). The outcome is UNKNOWN: TradeMate is checking its record. Do not repeat the order until it says what happened.' };
    });
  }

  function runWrite(label, url, body) {
    var st = execStart(label);
    return postJSON(url, body).then(function (res) {
      var view = TradeMateOutcome.describe(res);   // branches on res.outcome (structured), never on message text
      st.done(view.kind, view.title + (view.text ? '\n' + view.text : ''));
      if (res.risk_report && res.risk_report.breaches && res.risk_report.breaches.length) {
        showToast('Risk Engine: ' + res.risk_report.breaches.join(' '));
      }
      poll(true);  // always re-read the broker, whatever the outcome
      hist.stale = true; act.at = 0;   // a close/modify changes the history: reload it next time it is looked at
      return res;
    });
  }

  /* Phase 6: every new order is REVIEWED by the Risk Engine first. The user sees the numbers and must confirm.
     The server judges the order again on submit; this dialog is for the user, never the authority. */
  function money(v, cur) { return v == null || !isFinite(v) ? DASH : v.toFixed(2) + ' ' + (cur || ''); }
  function reviewRow(k, v) {
    var row = document.createElement('div'); row.className = 'rv-row';
    var a = document.createElement('span'); a.textContent = k;
    var b = document.createElement('strong'); b.textContent = v;
    row.appendChild(a); row.appendChild(b); return row;
  }
  function showOrderReview(rv, body, onSubmit) {
    var old = document.getElementById('order-review'); if (old) old.remove();
    var wrap = document.createElement('div'); wrap.id = 'order-review'; wrap.className = 'rv-overlay';
    var box = document.createElement('div'); box.className = 'rv-box'; wrap.appendChild(box);
    var h = document.createElement('h3');
    h.textContent = rv.approved ? 'Review order: approved by Risk Engine' : 'Review order: not approved';
    h.className = rv.approved ? 'rv-ok' : 'rv-bad'; box.appendChild(h);
    var o = rv.order, e = rv.estimates, cur = rv.currency;
    if (o) {
      var pend = o.order_type ? PEND_LABEL[o.order_type] : null;
      box.appendChild(reviewRow('Order', (pend ? pend.toUpperCase() : o.side.toUpperCase()) + ' ' + (rv.approved_volume || o.volume) + ' ' + o.symbol));
      if (pend) {
        box.appendChild(reviewRow('Entry price', String(o.price) + ' (the order waits for this price)'));
        box.appendChild(reviewRow('Expires', fmtExpiry(o.expiration)));
      } else if (o.entry != null) box.appendChild(reviewRow('Reference price', String(o.entry) + ' (market fill may differ)'));
      box.appendChild(reviewRow('Stop-loss', o.sl == null ? DASH : String(o.sl)));
      box.appendChild(reviewRow('Take-profit', o.tp == null ? DASH : String(o.tp)));
    }
    if (e) {
      box.appendChild(reviewRow('Est. risk at SL', money(e.risk_amount, cur)));
      box.appendChild(reviewRow('Est. reward at TP', money(e.reward_amount, cur)));
      box.appendChild(reviewRow('Reward : risk', e.reward_to_risk == null ? DASH : e.reward_to_risk.toFixed(2)));
    }
    var d = rv.decision;
    if (d && d.trade_risk_percent != null) box.appendChild(reviewRow('Risk of this trade', d.trade_risk_percent.toFixed(2) + '% of equity'));
    var msg = document.createElement('p'); msg.className = 'rv-msg'; msg.textContent = rv.message; box.appendChild(msg);
    (rv.warnings || []).forEach(function (w) { var p = document.createElement('p'); p.className = 'rv-warn'; p.textContent = '\u26A0 ' + w; box.appendChild(p); });
    var note = document.createElement('p'); note.className = 'rv-note';
    note.textContent = 'Estimates only, not guaranteed outcomes. Swap, commission and slippage are not included.'; box.appendChild(note);
    var bar = document.createElement('div'); bar.className = 'rv-bar'; box.appendChild(bar);
    function btn(text, cls, fn) { var b = document.createElement('button'); b.type = 'button'; b.className = 'btn ' + cls; b.textContent = text; b.addEventListener('click', fn); bar.appendChild(b); }
    btn('Cancel', '', function () { wrap.remove(); });
    if (!rv.approved && rv.resize) {
      btn('Use ' + rv.resize.volume + ' lots instead', 'btn-buy', function () {
        wrap.remove(); onSubmit(Object.assign({}, body, { volume: rv.resize.volume }), true);
      });
    }
    var bSide = body.side || (String(body.order_type || '').indexOf('buy') === 0 ? 'buy' : 'sell');
    if (rv.approved) btn(body.order_type ? 'Place ' + PEND_LABEL[body.order_type] : 'Confirm ' + bSide.toUpperCase(), bSide === 'buy' ? 'btn-buy' : 'btn-sell', function () { wrap.remove(); onSubmit(body, false); });
    document.body.appendChild(wrap);
  }

  function placeOrder(side, root) {
    var sym = state.selected;
    if (!sym) { showToast('Pick a symbol first.'); return; }
    var lot = parseFloat(inRole(root, 'lot').value);
    var slRaw = inRole(root, 'sl').value.trim(), tpRaw = inRole(root, 'tp').value.trim();
    var body = { symbol: sym, side: side, volume: lot };
    var slGate = root._slCtl ? root._slCtl.gate(side) : { ok: true };       // risk mode: only a fresh, valid, same-side stop may be sent
    if (!slGate.ok) { var stg = execStart('PLACE ORDER'); stg.done('fail', 'Not sent \u2717 ' + slGate.why); return; }
    if (slRaw !== '') body.sl = parseFloat(slRaw);
    if (tpRaw !== '') body.tp = parseFloat(tpRaw);
    if (!isFinite(lot) || lot <= 0) { var st = execStart('PLACE ORDER'); st.done('fail', 'Failed \u2717 Enter a lot size above 0.'); return; }
    if (body.sl == null || !isFinite(body.sl)) { var st2 = execStart('PLACE ORDER'); st2.done('fail', 'Failed \u2717 A stop-loss is required: the Risk Engine cannot approve a trade without one.'); return; }
    if (body.tp != null && !isFinite(body.tp)) { var st3 = execStart('PLACE ORDER'); st3.done('fail', 'Failed \u2717 Take-profit is not a valid number.'); return; }
    setTradeButtons(true);
    var rs = execStart('RISK CHECK ' + side.toUpperCase() + ' ' + lot + ' ' + sym);
    postJSON('/api/order/review', body).then(function (rv) {
      setTradeButtons(false);
      if (rv.state === 'unknown' || rv.ok === false && !rv.stage) { rs.done('fail', 'Risk check failed \u2717 ' + (rv.message || 'No answer from the server.') + ' Nothing was sent.'); return; }
      rs.done(rv.approved ? 'ok' : 'fail', rv.approved ? 'Risk check passed \u2713 Waiting for your confirmation.' : 'Not approved \u2717 ' + rv.message);
      showOrderReview(rv, body, function (finalBody) {
        setTradeButtons(true);
        runWrite('PLACE ORDER ' + finalBody.side.toUpperCase() + ' ' + finalBody.volume + ' ' + sym, '/api/order', finalBody).then(function (res) {
          setTradeButtons(false);
          if (res.state === 'executed') terminals.forEach(function (t) { t.clearDraft(); });
        });
      });
    });
  }

  var sendingTrade = false;
  function setTradeButtons(sending) {
    sendingTrade = sending;
    updateTradeButtons();
  }
  function updateTradeButtons() {
    var ok = state.connected && state.trading.enabled && !!state.selected && !sendingTrade;
    ['panel-buy', 'panel-sell'].forEach(function (r) {
      roleAll(r).forEach(function (b) { b.disabled = !ok; });
    });
  }

  /* ---------- connection / account rendering ---------- */
  function renderStatus(st, acct) {
    var s = st.state;
    var pill = $('mt5-pill');
    pill.className = 'pill ' + (s === 'CONNECTED' ? 'pill-ok' : s === 'CONNECTING' ? 'pill-warn' : 'pill-bad');
    setText('mt5-pill-text', 'MT5: ' + s);
    setText('mt5-state', s);
    setClass('mt5-state', stateClass(s));
    setText('settings-mt5-state', titleCase(s));
    setClass('settings-mt5-state', stateClass(s));
    setText('acct-pill', acct ? 'Account: ' + acct.login + ' (' + String(acct.environment).toUpperCase() + ')' : 'Account: ---');

    setText('br-state', s);
    setClass('br-state', stateClass(s));
    setText('br-broker', acct && acct.broker ? acct.broker : DASH);
    setText('br-server', acct && acct.server ? acct.server : DASH);
    setText('br-env', acct ? String(acct.environment).toUpperCase() : DASH);
    setText('br-login', acct ? String(acct.login) : DASH);
    setText('br-currency', acct && acct.currency ? acct.currency : DASH);
    setText('br-margin', acct ? money(acct.margin, acct.currency) : DASH);
    setText('br-age', typeof st.age_seconds === 'number' ? st.age_seconds.toFixed(1) + 's ago' : DASH);

    var msg = st.message || '';
    if (st.warnings && st.warnings.length) msg += ' ' + st.warnings.join(' ');
    setText('br-message', msg);
    setClass('br-message', 'note' + (s === 'ERROR' || s === 'DISCONNECTED' ? ' is-bad' : (s === 'CONNECTING' || (st.warnings && st.warnings.length) ? ' is-warn' : '')));

    var ex = $('exec-state');
    if (ex) {
      var can = s === 'CONNECTED' && state.trading.enabled;
      ex.textContent = can ? 'DEMO READY' : (state.trading.enabled ? 'WAITING FOR MT5' : 'DISABLED');
      ex.className = can ? 'is-ok' : 'is-warn';
    }
  }

  function renderAccountCards(acct) {
    setText('acct-balance', acct ? money(acct.balance, acct.currency) : DASH);
    setText('acct-equity', acct ? money(acct.equity, acct.currency) : DASH);
    setText('acct-free-margin', acct ? money(acct.free_margin, acct.currency) : DASH);
    setText('acct-margin', acct ? money(acct.margin, acct.currency) : DASH);
    var el = $('acct-profit');
    if (el) { if (acct) setPL(el, acct.profit, acct.currency); else { el.textContent = DASH; el.className = ''; } }
  }

  /* ---------- Current Market: watchlist + up/down quote switching ---------- */
  function ensureWatchlist() {
    if (!state.symbols.length) return;
    var valid = watchList();
    if (state.watch == null) {                      // first run: start with what MT5 Market Watch offers
      state.watch = state.symbols.slice(0, 8).map(function (s) { return s.name; });
      saveLocal('trademate.watchlist', state.watch);
      valid = watchList();
    }
    if (!state.selected || (!symbolByName(state.selected) && (state.watch || []).indexOf(state.selected) < 0)) {
      var first = valid[0] || state.symbols[0].name;
      state.selected = first;
      saveLocal('trademate.selected', first);
      state.candles = []; state.candleKey = '';
    }
  }

  function renderWatchControls() {
    var chips = $('watch-chips');
    if (!chips) return;
    var list = watchList();
    var nodes = list.map(function (name) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'chip'; b.setAttribute('data-sym', name);
      b.setAttribute('aria-pressed', name === state.selected ? 'true' : 'false');
      b.appendChild(document.createTextNode(name));
      var x = document.createElement('span');
      x.className = 'chip-x'; x.setAttribute('data-remove', name); x.setAttribute('aria-label', 'Remove ' + name);
      x.title = 'Remove from watchlist'; x.textContent = '\u00d7';
      b.appendChild(x);
      return b;
    });
    chips.replaceChildren.apply(chips, nodes);

    var hasMany = list.length > 1;
    $('mkt-up').disabled = !hasMany; $('mkt-down').disabled = !hasMany;
  }

  function renderQuote() {
    var sym = symbolByName(state.selected);
    if (!sym) {
      setText('mkt-symbol', DASH); setText('mkt-price', DASH); setText('mkt-ask', DASH); setText('mkt-spread', DASH);
      return;
    }
    var d = digitsOf(sym);
    setText('mkt-symbol', sym.name);
    setText('mkt-price', price(sym.bid, d));
    setText('mkt-ask', price(sym.ask, d));
    setText('mkt-spread', sym.spread_points == null ? DASH : sym.spread_points + ' pts');
  }

  function renderMarket(symbols) {
    var body = $('market-body');
    if (!symbols.length) { emptyRow(body, 4, 'No market data'); return; }
    var rows = symbols.map(function (sym) {
      var d = digitsOf(sym), tr = document.createElement('tr');
      tr.appendChild(cell(sym.name));
      tr.appendChild(cell(price(sym.bid, d), 'num'));
      tr.appendChild(cell(price(sym.ask, d), 'num'));
      tr.appendChild(cell(sym.spread_points == null ? DASH : String(sym.spread_points), 'num'));
      return tr;
    });
    body.replaceChildren.apply(body, rows);
  }

  function renderSelection() {
    renderQuote();
    renderWatchControls();
    terminals.forEach(function (t) { t.syncSymbol(); t.draw(); });
    updateTradeButtons();
  }

  $('mkt-up').addEventListener('click', function () { cycle(-1); });
  $('mkt-down').addEventListener('click', function () { cycle(1); });
  /* ----- broker-wide symbol search: any instrument the broker offers ----- */
  var searchBox = $('sym-search'), results = $('sym-results'), searchTimer = null, searchSeq = 0;
  var TRADE_MODE = { 0: 'trading disabled', 1: 'long only', 2: 'short only', 3: 'close only' };

  function syncWatch() {
    postJSON('/api/watch', { symbols: state.watch || [] });   // lets the bridge keep streaming prices
  }
  function resultsMessage(text) {
    var d = document.createElement('div'); d.className = 'sym-msg'; d.textContent = text;
    results.replaceChildren(d); results.hidden = false;
  }
  function runSearch() {
    var q = searchBox.value.trim(), seq = ++searchSeq;
    if (q.length < 2) { results.hidden = true; return; }
    resultsMessage('Searching the broker...');
    fetch('/api/symbols/search?limit=40&q=' + encodeURIComponent(q), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (seq !== searchSeq) return;
        if (!res.ok) { resultsMessage(res.message || 'Search failed.'); return; }
        if (!res.matches.length) { resultsMessage('No broker symbol matches "' + q + '".'); return; }
        var nodes = res.matches.map(function (m) {
          var b = document.createElement('button');
          b.type = 'button'; b.className = 'sym-item'; b.setAttribute('data-add', m.name);
          var n = document.createElement('b'); n.textContent = m.name;
          var small = document.createElement('small');
          var flag = TRADE_MODE[m.trade_mode];
          small.textContent = [m.description, m.path].filter(Boolean).join(' \u00b7 ');
          b.appendChild(n); b.appendChild(small);
          if (flag) { var f = document.createElement('small'); f.className = 'sym-flag'; f.textContent = '\u26A0 ' + flag; b.appendChild(f); }
          return b;
        });
        if (res.truncated) {
          var more = document.createElement('div'); more.className = 'sym-msg';
          more.textContent = res.total_matches + ' matches. Type more to narrow down.';
          nodes.push(more);
        }
        results.replaceChildren.apply(results, nodes); results.hidden = false;
      })
      .catch(function () { if (seq === searchSeq) resultsMessage('Cannot reach the TradeMate server.'); });
  }
  searchBox.addEventListener('input', function () { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 300); });
  searchBox.addEventListener('keydown', function (e) { if (e.key === 'Escape') { results.hidden = true; } e.stopPropagation(); });
  document.addEventListener('click', function (e) {
    if (!e.target.closest('.sym-search')) results.hidden = true;
  });
  results.addEventListener('click', function (e) {
    var item = e.target.closest('[data-add]');
    if (!item) return;
    var name = item.getAttribute('data-add');
    resultsMessage('Adding ' + name + '...');
    postJSON('/api/symbols/add', { symbol: name }).then(function (res) {
      if (!res.ok) { resultsMessage(res.message || 'Could not add ' + name + '.'); return; }
      var info = res.symbol; info.bid = null; info.ask = null;     // never show a stale price
      state.extra[info.name] = info;
      if ((state.watch || []).indexOf(info.name) < 0) state.watch = (state.watch || []).concat([info.name]);
      saveLocal('trademate.watchlist', state.watch);
      results.hidden = true; searchBox.value = '';
      setSelected(info.name);
      renderWatchControls();
      poll(true);
    });
  });
  $('watch-chips').addEventListener('click', function (e) {
    var x = e.target.closest('[data-remove]');
    if (x) {
      var name = x.getAttribute('data-remove');
      state.watch = (state.watch || []).filter(function (n) { return n !== name; });
      saveLocal('trademate.watchlist', state.watch);
      syncWatch();
      if (state.selected === name && watchList().length) setSelected(watchList()[0]);
      renderWatchControls();
      return;
    }
    var chip = e.target.closest('[data-sym]');
    if (chip) setSelected(chip.getAttribute('data-sym'));
  });
  document.addEventListener('keydown', function (e) {
    if (e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
    if (e.key === 'ArrowUp') { cycle(-1); e.preventDefault(); }
    else if (e.key === 'ArrowDown') { cycle(1); e.preventDefault(); }
  });

  /* ---------- Open trades (shared by Dashboard and Trades page) ---------- */
  var closeArm = {};  // ticket -> timer, first click arms, second click closes

  function lvlInput(label) {
    var input = document.createElement('input');
    input.type = 'number'; input.step = 'any'; input.className = 'lvl-input';
    input.setAttribute('aria-label', label); input.placeholder = '---';
    input.addEventListener('input', function () { input._dirty = true; input.classList.add('is-dirty'); });
    return input;
  }

  function makeTradeRow(p) {
    var tr = document.createElement('tr');
    tr.className = 'trade-row'; tr.setAttribute('data-ticket', p.ticket);
    tr._c = {
      symbol: cell(''), dir: cell(''), vol: cell('', 'num'), entry: cell('', 'num'),
      sl: cell('', 'num'), tp: cell('', 'num'), pl: cell('', 'num'), manage: cell('', 'num manage')
    };
    tr._slIn = lvlInput('Stop loss for ' + p.symbol); tr._tpIn = lvlInput('Take profit for ' + p.symbol);
    tr._c.sl.appendChild(tr._slIn); tr._c.tp.appendChild(tr._tpIn);
    var wrap = document.createElement('span'); wrap.className = 'manage-cell';
    tr._modBtn = document.createElement('button');
    tr._modBtn.type = 'button'; tr._modBtn.className = 'btn btn-sm'; tr._modBtn.textContent = 'MODIFY';
    tr._closeBtn = document.createElement('button');
    tr._closeBtn.type = 'button'; tr._closeBtn.className = 'btn btn-sm btn-danger'; tr._closeBtn.textContent = 'CLOSE';
    wrap.appendChild(tr._modBtn); wrap.appendChild(tr._closeBtn);
    tr._c.manage.appendChild(wrap);
    ['symbol', 'dir', 'vol', 'entry', 'sl', 'tp', 'pl', 'manage'].forEach(function (k) { tr.appendChild(tr._c[k]); });

    tr._modBtn.addEventListener('click', function () { modifyFromRow(tr); });
    [tr._slIn, tr._tpIn].forEach(function (inp) {
      inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') modifyFromRow(tr); });
    });
    tr._closeBtn.addEventListener('click', function () { closeFromRow(tr); });
    return tr;
  }

  function setRowBusy(tr, busy) {
    tr.classList.toggle('is-busy', busy);
    tr._modBtn.disabled = busy; tr._closeBtn.disabled = busy;
  }

  function modifyFromRow(tr) {
    var ticket = parseInt(tr.getAttribute('data-ticket'), 10), body = {}, what = [];
    if (tr._slIn._dirty) { body.sl = tr._slIn.value.trim() === '' ? null : parseFloat(tr._slIn.value); what.push('SL'); }
    if (tr._tpIn._dirty) { body.tp = tr._tpIn.value.trim() === '' ? null : parseFloat(tr._tpIn.value); what.push('TP'); }
    if (!what.length) { showToast('Change the SL or TP value first.'); return; }
    setRowBusy(tr, true);
    runWrite('MODIFY ' + what.join('+') + ' #' + ticket, '/api/position/' + ticket + '/modify', body).then(function (res) {
      setRowBusy(tr, false);
      // Whatever happened, show the broker's real values again (success: new values; failure: old ones).
      tr._slIn._dirty = false; tr._tpIn._dirty = false;
      tr._slIn.classList.remove('is-dirty'); tr._tpIn.classList.remove('is-dirty');
    });
  }

  function closeFromRow(tr) {
    var ticket = parseInt(tr.getAttribute('data-ticket'), 10);
    if (!closeArm[ticket]) {
      tr._closeBtn.textContent = 'CONFIRM?'; tr._closeBtn.classList.add('is-armed');
      closeArm[ticket] = setTimeout(function () {
        delete closeArm[ticket]; tr._closeBtn.textContent = 'CLOSE'; tr._closeBtn.classList.remove('is-armed');
      }, 3000);
      return;
    }
    clearTimeout(closeArm[ticket]); delete closeArm[ticket];
    tr._closeBtn.textContent = 'CLOSE'; tr._closeBtn.classList.remove('is-armed');
    setRowBusy(tr, true);
    runWrite('CLOSE #' + ticket + ' ' + tr.getAttribute('data-symbol'), '/api/position/' + ticket + '/close', {}).then(function () {
      setRowBusy(tr, false);  // the row disappears on its own once the broker no longer lists the position
    });
  }

  function updateTradeRow(tr, p, cur) {
    var sym = symbolByName(p.symbol), d = digitsOf(sym);
    tr.setAttribute('data-symbol', p.symbol);
    tr._c.symbol.textContent = p.symbol;
    tr._c.dir.textContent = String(p.direction).toUpperCase();
    tr._c.dir.className = 'dir-' + p.direction;
    tr._c.vol.textContent = p.volume == null ? DASH : p.volume.toFixed(2);
    tr._c.entry.textContent = price(p.price_open, d);
    [[tr._slIn, p.stop_loss], [tr._tpIn, p.take_profit]].forEach(function (pair) {
      var inp = pair[0];
      if (inp._dirty || document.activeElement === inp) return;   // never overwrite what the user is typing
      inp.value = pair[1] == null ? '' : pair[1].toFixed(d);
    });
    setPL(tr._c.pl, p.profit, cur);
  }

  function renderPositions(positions, connected, cur) {
    roleAll('trades-body').forEach(function (body) {
      if (!connected) { emptyRow(body, 8, 'No data. MT5 is not connected.'); return; }
      if (!positions.length) { emptyRow(body, 8, 'No open positions'); return; }
      if (!body._rows) body._rows = {};
      if (body.querySelector('.empty-row')) { body.replaceChildren(); body._rows = {}; }
      var seen = {};
      positions.forEach(function (p, idx) {
        seen[p.ticket] = true;
        var tr = body._rows[p.ticket];
        if (!tr) { tr = makeTradeRow(p); body._rows[p.ticket] = tr; }
        updateTradeRow(tr, p, cur);
        if (body.children[idx] !== tr) body.insertBefore(tr, body.children[idx] || null);
      });
      Object.keys(body._rows).forEach(function (t) {
        if (!seen[t]) { body._rows[t].remove(); delete body._rows[t]; }
      });
    });
    var total = positions.reduce(function (sum, p) { return sum + (p.profit || 0); }, 0);
    roleAll('open-pl').forEach(function (el) {
      if (!connected || !positions.length) { el.textContent = DASH; el.classList.remove('pl-pos', 'pl-neg', 'pl-zero'); }
      else { setPL(el, total, cur); el.textContent = 'Total ' + el.textContent; }
    });
  }

  /* ---------- Pending orders (Phase 7B) ---------- */
  var PEND_LABEL = { buy_limit: 'Buy Limit', sell_limit: 'Sell Limit', buy_stop: 'Buy Stop', sell_stop: 'Sell Stop' };
  var pendArm = {};
  state.pendingSupported = false; state.orders = [];

  function fmtExpiry(epoch) {
    if (epoch == null) return 'Until cancelled';
    var d = new Date(epoch * 1000);
    return isNaN(d.getTime()) ? DASH : d.toLocaleString();
  }
  function pendInput() {
    var i = document.createElement('input'); i.type = 'number'; i.step = 'any'; i.className = 'input input-sm manage-in';
    i.addEventListener('input', function () { i._dirty = true; i.classList.add('is-dirty'); });
    return i;
  }
  function makePendingRow(o) {
    var tr = document.createElement('tr'); tr.setAttribute('data-order', String(o.ticket));
    tr._c = {};
    ['symbol', 'type', 'vol', 'price', 'sl', 'tp', 'exp', 'ticket', 'manage'].forEach(function (k) {
      var td = document.createElement('td'); if (['vol', 'price', 'sl', 'tp', 'ticket', 'manage'].indexOf(k) >= 0) td.className = 'num'; tr._c[k] = td; tr.appendChild(td);
    });
    tr._priceIn = pendInput(); tr._slIn = pendInput(); tr._tpIn = pendInput();
    tr._c.price.appendChild(tr._priceIn); tr._c.sl.appendChild(tr._slIn); tr._c.tp.appendChild(tr._tpIn);
    var wrap = document.createElement('span'); wrap.className = 'manage-cell';
    tr._applyBtn = document.createElement('button'); tr._applyBtn.type = 'button'; tr._applyBtn.className = 'btn btn-sm'; tr._applyBtn.textContent = 'APPLY';
    tr._cancelBtn = document.createElement('button'); tr._cancelBtn.type = 'button'; tr._cancelBtn.className = 'btn btn-sm btn-danger'; tr._cancelBtn.textContent = 'CANCEL ORDER';
    wrap.appendChild(tr._applyBtn); wrap.appendChild(tr._cancelBtn); tr._c.manage.appendChild(wrap);
    tr._applyBtn.addEventListener('click', function () { modifyPendingFromRow(tr); });
    tr._cancelBtn.addEventListener('click', function () { cancelPendingFromRow(tr); });
    return tr;
  }
  function setPendBusy(tr, busy) { tr.classList.toggle('is-busy', busy); tr._applyBtn.disabled = busy; tr._cancelBtn.disabled = busy; }
  function modifyPendingFromRow(tr) {
    var ticket = parseInt(tr.getAttribute('data-order'), 10), body = {}, what = [];
    [['price', tr._priceIn, 'ENTRY'], ['sl', tr._slIn, 'SL'], ['tp', tr._tpIn, 'TP']].forEach(function (f) {
      if (!f[1]._dirty) return;
      var raw = f[1].value.trim();
      if (f[0] === 'price' && raw === '') return;               // the entry price cannot be empty
      body[f[0]] = raw === '' ? null : parseFloat(raw); what.push(f[2]);
    });
    if (!what.length) { showToast('Change the entry, SL or TP value first.'); return; }
    setPendBusy(tr, true);
    runWrite('MODIFY ' + what.join('+') + ' PENDING #' + ticket, '/api/pending/' + ticket + '/modify', body).then(function () {
      setPendBusy(tr, false);
      [tr._priceIn, tr._slIn, tr._tpIn].forEach(function (i) { i._dirty = false; i.classList.remove('is-dirty'); });   // show the broker's real values again
    });
  }
  function cancelPendingFromRow(tr) {
    var ticket = parseInt(tr.getAttribute('data-order'), 10);
    if (!pendArm[ticket]) {                                       // two clicks: the first only arms
      tr._cancelBtn.textContent = 'CONFIRM?'; tr._cancelBtn.classList.add('is-armed');
      pendArm[ticket] = setTimeout(function () { delete pendArm[ticket]; tr._cancelBtn.textContent = 'CANCEL ORDER'; tr._cancelBtn.classList.remove('is-armed'); }, 3000);
      return;
    }
    clearTimeout(pendArm[ticket]); delete pendArm[ticket];
    tr._cancelBtn.textContent = 'CANCEL ORDER'; tr._cancelBtn.classList.remove('is-armed');
    setPendBusy(tr, true);
    runWrite('CANCEL PENDING #' + ticket + ' ' + tr.getAttribute('data-symbol'), '/api/pending/' + ticket + '/cancel', {}).then(function () { setPendBusy(tr, false); });
  }
  function updatePendingRow(tr, o) {
    var d = digitsOf(symbolByName(o.symbol));
    tr.setAttribute('data-symbol', o.symbol);
    tr._c.symbol.textContent = o.symbol;
    tr._c.type.textContent = PEND_LABEL[o.order_type] || o.order_type;
    tr._c.type.className = 'dir-' + o.direction;
    tr._c.vol.textContent = o.volume == null ? DASH : o.volume.toFixed(2);
    [[tr._priceIn, o.price], [tr._slIn, o.stop_loss], [tr._tpIn, o.take_profit]].forEach(function (pair) {
      var inp = pair[0];
      if (inp._dirty || document.activeElement === inp) return;   // never overwrite what the user is typing
      inp.value = pair[1] == null ? '' : pair[1].toFixed(d);
    });
    tr._c.exp.textContent = fmtExpiry(o.expiration);
    tr._c.ticket.textContent = '#' + o.ticket;
  }
  function renderPending(orders, connected, supported) {
    roleAll('pending-body').forEach(function (body) {
      var note = body.closest('section') ? body.closest('section').querySelector('[data-role="pending-note"]') : null;
      if (note) note.textContent = !connected ? '' : (supported ? '' : 'Pending orders are unavailable: the running TradeMateBridge EA does not report pending-order support. Recompile and re-attach the EA (version 0.5.0 or newer) in MT5.');
      if (!connected) { emptyRow(body, 9, 'No data. MT5 is not connected.'); return; }
      if (!orders.length) { emptyRow(body, 9, supported ? 'No pending orders' : 'Pending orders are not available with this EA'); return; }
      if (!body._rows) body._rows = {};
      if (body.querySelector('.empty-row')) { body.replaceChildren(); body._rows = {}; }
      var seen = {};
      orders.forEach(function (o, idx) {
        seen[o.ticket] = true;
        var tr = body._rows[o.ticket];
        if (!tr) { tr = makePendingRow(o); body._rows[o.ticket] = tr; }
        updatePendingRow(tr, o);
        if (body.children[idx] !== tr) body.insertBefore(tr, body.children[idx] || null);
      });
      Object.keys(body._rows).forEach(function (t) { if (!seen[t]) { body._rows[t].remove(); delete body._rows[t]; } });
    });
  }

  /* order-type selector in the trade panel: Market keeps the BUY / SELL buttons; the four pending types use their own button */
  function pendingTypeOf(root) { var v = inRole(root, 'order-type'); return v && v.value !== 'market' ? v.value : null; }
  function syncPendingControls() {
    document.querySelectorAll('[data-role="pend-box"]').forEach(function (box) {
      var sel = box.querySelector('[data-role="order-type"]'), note = box.querySelector('[data-role="pend-note"]'), place = box.querySelector('[data-role="pend-place"]');
      Array.prototype.forEach.call(sel.options, function (o) { if (o.value !== 'market') o.disabled = !state.pendingSupported; });
      if (!state.pendingSupported && sel.value !== 'market') { sel.value = 'market'; box.querySelector('[data-role="pend-fields"]').hidden = true; }
      if (note) note.textContent = state.connected && !state.pendingSupported ? 'Pending order types need the updated TradeMateBridge EA (0.5.0 or newer); the running EA does not report support.' : '';
      if (place) place.disabled = !state.pendingSupported;
    });
  }
  function placePending(root) {
    var type = pendingTypeOf(root), sym = state.selected;
    if (!type) { showToast('Choose a pending order type first.'); return; }
    if (!sym) { showToast('Pick a symbol first.'); return; }
    var lot = parseFloat(inRole(root, 'lot').value), priceRaw = inRole(root, 'pend-price').value.trim();
    var slRaw = inRole(root, 'sl').value.trim(), tpRaw = inRole(root, 'tp').value.trim(), expRaw = inRole(root, 'pend-exp').value;
    var label = PEND_LABEL[type].toUpperCase();
    function fail(msg) { var st0 = execStart('PLACE ' + label); st0.done('fail', 'Not sent \u2717 ' + msg); }
    if (!isFinite(lot) || lot <= 0) { fail('Enter a lot size above 0.'); return; }
    if (priceRaw === '' || !isFinite(parseFloat(priceRaw))) { fail('Enter the entry price the order should wait for.'); return; }
    if (slRaw === '' || !isFinite(parseFloat(slRaw))) { fail('A stop-loss is required: the Risk Engine cannot approve a trade without one.'); return; }
    if (tpRaw !== '' && !isFinite(parseFloat(tpRaw))) { fail('Take-profit is not a valid number.'); return; }
    var body = { order_type: type, symbol: sym, volume: lot, price: parseFloat(priceRaw), sl: parseFloat(slRaw), tp: tpRaw === '' ? null : parseFloat(tpRaw) };
    if (expRaw) { var ms = new Date(expRaw).getTime(); if (!isFinite(ms)) { fail('The expiry date is not valid.'); return; } body.expiration = Math.floor(ms / 1000); }
    var gate = root._slCtl ? root._slCtl.gate(type.indexOf('buy') === 0 ? 'buy' : 'sell') : { ok: true };
    if (!gate.ok) { fail(gate.why); return; }
    setTradeButtons(true);
    var rs = execStart('RISK CHECK ' + label + ' ' + lot + ' ' + sym);
    postJSON('/api/pending/review', body).then(function (rv) {
      setTradeButtons(false);
      if (rv.ok === false && !rv.stage) { rs.done('fail', 'Risk check failed \u2717 ' + (rv.message || 'No answer.') + ' Nothing was sent.'); return; }
      rs.done(rv.approved ? 'ok' : 'fail', rv.approved ? 'Checks passed \u2713 Waiting for your confirmation.' : 'Not approved \u2717 ' + rv.message);
      showOrderReview(rv, body, function (finalBody) {
        setTradeButtons(true);
        runWrite('PLACE ' + label + ' ' + finalBody.volume + ' ' + sym, '/api/pending', finalBody).then(function (res) {
          setTradeButtons(false);
          if (res.state === 'executed') { inRole(root, 'pend-price').value = ''; terminals.forEach(function (t) { t.clearDraft(); }); }
        });
      });
    });
  }

  /* ---------- Chart ---------- */
  var CHART_COLORS = {
    dark: {
      grid: 'rgba(255,255,255,.055)', frame: 'rgba(255,255,255,.14)', axis: '#B7C2DA', text: '#F2F5FC',
      up: '#4DA3FF', down: '#FF5C6C', line: '#8DB8F5', areaTop: 'rgba(77,163,255,.28)',
      bid: '#FF5C5C', ask: '#2FD38A', bidFg: '#2A0508', askFg: '#04241A', spread: 'rgba(160,174,200,.10)', entry: '#AFC0E3', entryTag: '#41537A',
      pillBg: 'rgba(8,14,30,.82)', handleRing: '#070C18', outline: 'rgba(222,230,248,.55)',
      cross: 'rgba(222,230,248,.5)', crossBand: 'rgba(255,255,255,.04)', crossTag: '#DDE5F7', crossTagFg: '#0A1226'
    },
    light: {
      grid: 'rgba(15,27,51,.07)', frame: 'rgba(15,27,51,.2)', axis: '#44547A', text: '#0F1B33',
      up: '#0B63D6', down: '#D6303F', line: '#0B63D6', areaTop: 'rgba(11,99,214,.22)',
      bid: '#C4262E', ask: '#117A45', bidFg: '#FFFFFF', askFg: '#FFFFFF', spread: 'rgba(15,27,51,.07)', entry: '#5D6E94', entryTag: '#5D6E94',
      pillBg: 'rgba(255,255,255,.88)', handleRing: '#FFFFFF', outline: 'rgba(15,27,51,.55)',
      cross: 'rgba(15,27,51,.45)', crossBand: 'rgba(15,27,51,.05)', crossTag: '#0F1B33', crossTagFg: '#F3F6FC'
    }
  };
  /* One theme for the whole application. The server copy (data/chart_settings.json) is the saved preference; the browser
     copy below only lets the page paint in the right theme before the server answers (no flash). */
  state.theme = loadLocal('trademate.chartTheme', 'dark') === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', state.theme);
  /* The chart has its OWN theme setting. "auto" (the default) follows the application theme above, so nothing changes for anyone who
     never touches it; "dark" / "light" pins the CHART only. Changing it never changes state.theme (the application theme). */
  var CHART_THEME_MODES = ['auto', 'dark', 'light'];
  state.chartThemeSetting = (function () { var v = loadLocal('trademate.chartThemeMode', 'auto'); return CHART_THEME_MODES.indexOf(v) >= 0 ? v : 'auto'; })();
  function chartTheme() { return state.chartThemeSetting === 'dark' || state.chartThemeSetting === 'light' ? state.chartThemeSetting : state.theme; }
  state.ctype = loadLocal('trademate.chartType', 'candles') === 'line' ? 'line' : 'candles';
  /* Candle colours + market-structure visibility. The saved copy lives on the server (data/chart_settings.json);
     this is only the live mirror. Defaults here equal the server's defaults and are replaced as soon as the saved
     settings arrive; a routine chart refresh never writes them back. */
  var STYLE = window.TradeMateChartStyle;
  state.candleStyle = { preset: 'red_black', bull: '#E53935', bear: '#000000' };   // = the server default
  state.presets = [];
  state.structureVisible = false;
  state.liquidityVisible = false; state.liquidity = null;   // { key, data, items } for the candles currently loaded
  state.liquiditySettings = { tolerance_ticks: 5, sweep_mode: 'wick', max_pools: 8 };
  /* Fair value gaps: the engine's result is ALWAYS requested and kept for the candles on screen; fvgVisible only decides whether the
     zones are drawn, so hiding them never discards or stops the analysis. */
  state.fvgVisible = false; state.fvg = null;                 // { key, data, items }
  /* Order blocks: same rule. The engine's result is ALWAYS requested and kept for the candles on screen; obVisible only decides
     whether the zones are drawn, so hiding them never discards or stops the analysis. */
  state.obVisible = false; state.ob = null;                   // { key, data, items }
  state.structure = null;           // { key, data, items } for the candles currently loaded
  state.chartSettingsLoaded = false;
  /* Liquidity overlay colours. Distinct from candles, the bid (red) / ask (green) quote lines, structure lines and trade lines. */
  var LIQ_COLORS = { dark: { BSL: '#F5A524', SSL: '#2DD4E8' }, light: { BSL: '#8A5200', SSL: '#0B6E82' } };
  /* Fair value gap zones: blue = bullish, purple = bearish. Distinct from the candles (user colours), bid/ask lines, structure labels,
     amber / cyan liquidity lines and trade levels; the direction is also written in the label, never colour alone. */
  var FVG_COLORS = { dark: { bullish: '#4C8DFF', bearish: '#C16BFF' }, light: { bullish: '#1F5FD1', bearish: '#8A2BC9' } };
  /* Order block zones: teal-green = bullish (demand), orange = bearish (supply). Large boxes that run to the newest candle, so they cannot be
     mistaken for the three-candle blue / purple fair value gap boxes; the direction and status are also written in the label. */
  /* Pending orders (the broker's real orders waiting to fill): magenta, drawn as a long dash-dot line with its own label card. Distinct
     from open-position entry lines (grey-blue dashes with a P/L card), stop-loss (red) and take-profit (blue) lines, and from every zone. */
  var PEND_COLORS = { dark: '#FF4FD8', light: '#B3138F' };
  var PEND_FG = { dark: '#2A0620', light: '#FFFFFF' };
  var OB_COLORS = { dark: { bullish: '#35D6A2', bearish: '#FF8A4C' }, light: { bullish: '#0A8A63', bearish: '#B85410' } };
  var STRUCT_COLORS = {
    dark: { hh: '#7FD1AE', ll: '#F59BA6', bos: '#8DB8F5', choch: '#F2C46D', eq: '#B7C2DA' },
    light: { hh: '#0E7A54', ll: '#B3263A', bos: '#0B63D6', choch: '#A86800', eq: '#44547A' }
  };
  var layoutPrefs = loadLocal('trademate.layout', {});
  function saveLayout() { saveLocal('trademate.layout', layoutPrefs); }

  /* ---- chart extras shared by every chart: moving average (display only, never a strategy input) and the user's MANUAL lines (never the automatic strategy trendlines) ---- */
  var TF_SEC = { M1: 60, M2: 120, M5: 300, M15: 900, M30: 1800, H1: 3600, H4: 14400, D1: 86400 };
  var MA = { cfg: { enabled: false, type: 'EMA', period: 20, timeframe: 'chart' }, loaded: false, msg: '', cache: {}, busy: false };
  var MAN = { symbol: null, lines: [], selected: null, tool: false, loading: false };
  function redrawAll() { terminals.forEach(function (t) { t.draw(); t.syncExtras && t.syncExtras(); }); }
  function maLoad() {
    if (MA.loaded) return; MA.loaded = true;
    fetch('/api/chart/indicators', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) { if (j && j.ma) { MA.cfg = j.ma; redrawAll(); } }).catch(function () { MA.loaded = false; });
  }
  function maSave(patch) {
    var next = Object.assign({}, MA.cfg, patch), mine = (MA.seq = (MA.seq || 0) + 1);
    MA.cfg = next; MA.msg = '';
    fetch('/api/chart/indicators', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ma: patch }) })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (mine !== MA.seq) return;                                   /* a newer change is already on screen: an older answer must never overwrite it */
        if (j.ok) MA.cfg = j.ma; else { MA.cfg = j.ma || MA.cfg; MA.msg = j.message || 'That setting was refused.'; }
        redrawAll();
      })
      .catch(function () { if (mine === MA.seq) { MA.msg = 'Could not save the moving-average setting.'; redrawAll(); } });
    redrawAll();
  }
  function maCompute(candles, type, period) {
    var n = candles.length, out = new Array(n).fill(null), i, j;
    if (n < period) return out;
    if (type === 'SMA') {
      var sum = 0;
      for (i = 0; i < n; i++) { sum += candles[i][4]; if (i >= period) sum -= candles[i - period][4]; if (i >= period - 1) out[i] = sum / period; }
    } else if (type === 'WMA') {
      var den = period * (period + 1) / 2;
      for (i = period - 1; i < n; i++) { var acc = 0; for (j = 0; j < period; j++) acc += candles[i - period + 1 + j][4] * (j + 1); out[i] = acc / den; }
    } else {                                                           /* EMA seeded with the SMA of the first `period` closes */
      var seed = 0; for (i = 0; i < period; i++) seed += candles[i][4];
      var prev = seed / period, k = 2 / (period + 1); out[period - 1] = prev;
      for (i = period; i < n; i++) { prev = candles[i][4] * k + prev * (1 - k); out[i] = prev; }
    }
    return out;
  }
  function maSource(sym, chartTf, chartCandles) {
    var tf = MA.cfg.timeframe === 'chart' ? chartTf : MA.cfg.timeframe;
    if (tf === chartTf) return { tf: tf, candles: chartCandles };
    var key = sym + '|' + tf, c = MA.cache[key];
    if ((!c || Date.now() - c.at > 20000) && !MA.busy && sym) {
      MA.busy = true;
      fetch(['/api/candles', '?timeframe=', tf, '&count=300&symbol=', encodeURIComponent(sym)].join(''), { cache: 'no-store' })     /* the moving-average source: a plain candle read, separate from the chart loader */
        .then(function (r) { return r.json(); })
        .then(function (res) { MA.cache[key] = { at: Date.now(), candles: (res.ok && res.candles) ? res.candles : [] }; })
        .catch(function () { MA.cache[key] = { at: Date.now(), candles: [] }; })
        .then(function () { MA.busy = false; redrawAll(); });
    }
    return { tf: tf, candles: c ? c.candles : [] };
  }
  function manLoad(sym) {
    if (!sym || MAN.symbol === sym || MAN.loading) return;
    MAN.loading = true;
    fetch('/api/chart/drawings?symbol=' + encodeURIComponent(sym), { cache: 'no-store' }).then(function (r) { return r.json(); })
      .then(function (j) { MAN.symbol = sym; MAN.lines = (j && j.lines) || []; MAN.selected = null; })
      .catch(function () { MAN.symbol = sym; MAN.lines = []; })
      .then(function () { MAN.loading = false; redrawAll(); });
  }
  function manSave() {
    if (!MAN.symbol) return;
    fetch('/api/chart/drawings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ symbol: MAN.symbol, lines: MAN.lines }) }).catch(function () { /* the line stays on screen; the next change saves again */ });
  }
  function manDelete() {
    if (!MAN.selected) return false;
    MAN.lines = MAN.lines.filter(function (l) { return l.id !== MAN.selected; }); MAN.selected = null; manSave(); redrawAll(); return true;
  }
  document.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || (e.target && e.target.isContentEditable)) return;
    if ((e.key === 'Delete' || e.key === 'Backspace') && MAN.selected) { if (manDelete()) e.preventDefault(); }
    else if (e.key === 'Escape' && MAN.tool) { MAN.tool = false; redrawAll(); }
  });

  function Terminal(root) {
    var self = this;
    var canvas = inRole(root, 'canvas'), stage = inRole(root, 'stage');
    var ctx = canvas.getContext('2d');
    var W = 0, H = 0, dpr = 1;
    var RIGHT_PAD = 6;                       // empty bars kept to the right of the newest candle
    var pad = { l: 8, r: 80, t: 58, b: 34 };
    var pending = null;      // proposed SL/TP change on an existing position, not yet sent
    var drag = null;         // line drag { kind, ..., frozen: {lo, hi} }
    var pan = null;          // chart pan { x0, back0, moved }
    var hover = null;        // crosshair {x, y}
    var lastRange = null;
    var sending = false;
    var axisDrag = null;     // price-axis drag { y0, range0 }: vertical scale
    var autoSpan = 0;        // height of the auto-fitted price range (limits how far the scale can be stretched)
    var autoBtn = null;      // on-canvas "A" button rectangle, when the price scale is manual
    // px per candle, bars scrolled back from the newest, and the price range: null = auto-fit, else {lo, hi} chosen by the user
    var view = { spacing: null, back: -RIGHT_PAD, key: '', yRange: null };
    var lotInput = inRole(root, 'lot'), slInput = inRole(root, 'sl'), tpInput = inRole(root, 'tp');
    var modBar = inRole(root, 'mod-bar');
    var hudCache = {};
    var selectedLineId = null;     // selected trade line whose contextual info is currently shown

    function resize() {
      var r = stage.getBoundingClientRect();
      var nd = window.devicePixelRatio || 1, nw = Math.max(0, Math.floor(r.width)), nh = Math.max(0, Math.floor(r.height));
      if (nw !== W || nh !== H || nd !== dpr) {
        dpr = nd; W = nw; H = nh;
        canvas.width = Math.floor(W * dpr); canvas.height = Math.floor(H * dpr);
      }
      self.draw();
    }
    if (window.ResizeObserver) new ResizeObserver(resize).observe(stage);
    window.addEventListener('resize', resize);

    function visible() { return root.getClientRects().length > 0 && W > 0; }
    this.isVisible = visible;
    this.applyTheme = function () {                      // HUD colours depend on the chart theme
      var ct = chartTheme(), btn = inRole(root, 'theme-btn');
      root.setAttribute('data-chart-theme', ct);
      if (btn) {
        var next = ct === 'light' ? 'dark' : 'light';
        btn.setAttribute('aria-label', 'Chart theme: ' + ct + '. Switch only the chart to ' + next + '; the rest of the application is unchanged.');
        btn.setAttribute('title', 'Chart theme: ' + ct + (state.chartThemeSetting === 'auto' ? ' (follows the application theme)' : ' (set for the chart only)') + '. Click for ' + next + '.');
      }
      hudCache = {}; self.draw();
    };

    function defaultSpacing() { return W < 520 ? 9 : (W < 900 ? 10.5 : 12.5); }
    function spacing() { return view.spacing || defaultSpacing(); }
    function plotWidth() { return Math.max(40, W - pad.l - pad.r); }
    function clampBack() {
      // Only keeps the chart from being lost entirely: the newest candle may travel almost the whole width to the
      // left, and the oldest almost the whole width to the right.
      var n = state.candles.length, vis = plotWidth() / spacing();
      view.back = Math.max(-vis * 0.9, Math.min(n - 3, view.back));
    }
    function plotHeight() { return Math.max(40, H - pad.t - pad.b); }
    /* Vertical position is free (no clamp). Only the SIZE of the visible price range is kept sane. */
    function setRange(lo, hi) {
      if (!isFinite(lo) || !isFinite(hi) || hi <= lo) return;
      var sym = symbolByName(state.selected), minSpan = ((sym && sym.point) || 1e-5) * 5, span = hi - lo, mid = (hi + lo) / 2;
      var maxSpan = (autoSpan || span) * 400;
      if (span < minSpan) span = minSpan; else if (span > maxSpan) span = maxSpan;
      view.yRange = { lo: mid - span / 2, hi: mid + span / 2 };
    }
    function scaleRange(range0, factor) {        // factor < 1 stretches the candles taller, > 1 compresses them
      var mid = (range0.hi + range0.lo) / 2, half = (range0.hi - range0.lo) / 2 * factor;
      setRange(mid - half, mid + half);
    }
    this.scalePrice = function (factor) { if (lastRange) { scaleRange(lastRange, factor); self.draw(); } };
    this.fitPrice = function () { view.yRange = null; self.draw(); };
    this.resetView = function () { view.spacing = null; view.back = -RIGHT_PAD; view.yRange = null; self.draw(); };
    this.zoom = function (factor) {
      view.spacing = Math.max(3, Math.min(40, spacing() * factor)); clampBack(); self.draw();
    };

    function symPositions() {
      return state.positions.filter(function (p) { return p.symbol === state.selected; });
    }
    /* The broker's real pending orders for the selected symbol, exactly as /api/state reports them (state.orders is replaced on every poll, so
       a cancelled / filled / modified order is gone or moved on the next poll). Nothing here is created, sent or invented. */
    function ordersFresh() {
      if (!state.connected || !state.quoteAt || Date.now() - state.quoteAt > QUOTE_MAX_AGE_MS) return false;
      return !(typeof state.statusAge === 'number' && state.statusAge > QUOTE_MAX_STATUS_AGE_S);
    }
    this.ordersFresh = ordersFresh;
    /* The FVG / order block zones are only as current as the candle refresh that produced them. When that stops (the broker is down, the
       request keeps failing) the old zones must not stay on the chart looking current. */
    function zonesFresh() { return !!(state.connected && state.candlesAt && Date.now() - state.candlesAt <= ZONES_MAX_AGE_MS); }
    this.zonesFresh = zonesFresh;
    var lastZoneNotices = [];
    this.zoneNotices = function () { return lastZoneNotices.slice(); };
    function zoneNoticeFor(block, name) {      // why a switched-on overlay shows nothing: unavailable engine result, or stale data
      if (!block || block.key !== state.candleKey) return null;
      if (!block.data || block.data.available !== true) return name + ' unavailable: ' + ((block.data && block.data.reason) || 'the engine returned no result');
      return null;
    }
    function symOrders(ignoreFreshness) {
      if (!ignoreFreshness && !ordersFresh()) return [];
      return (state.orders || []).filter(function (o) {
        return o && o.symbol === state.selected && typeof o.price === 'number' && isFinite(o.price) && o.price > 0 && PEND_LABEL[o.order_type];
      });
    }
    function posByTicket(t) {
      for (var i = 0; i < state.positions.length; i++) if (state.positions[i].ticket === t) return state.positions[i];
      return null;
    }
    function draftValue(input) {
      var v = parseFloat(input.value);
      return isFinite(v) && v > 0 ? v : null;
    }

    function niceStep(raw) {
      var mag = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / mag;
      return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
    }

    function indexRange(n) {
      var s = spacing(), vis = plotWidth() / s, R = n - 1 - view.back;
      return { s: s, R: R, first: Math.max(0, Math.floor(R - vis) - 1), last: Math.min(n - 1, Math.ceil(R) + 1) };
    }
    function xOf(i, ir) { return pad.l + plotWidth() - (ir.R - i) * ir.s - ir.s / 2; }
    function indexAt(x, ir) { return Math.round(ir.R - (pad.l + plotWidth() - ir.s / 2 - x) / ir.s); }

    function computeRange(ir, sym) {
      if (drag && drag.frozen) return drag.frozen;
      if (frozenRange) return frozenRange;
      if (view.yRange) return view.yRange;
      var lo = Infinity, hi = -Infinity, c = state.candles;
      for (var i = ir.first; i <= ir.last; i++) { lo = Math.min(lo, c[i][3]); hi = Math.max(hi, c[i][2]); }
      if (!isFinite(lo) || !isFinite(hi)) return null;
      var span = (hi - lo) || (hi * 0.001) || 1, near = span * 1.5;
      function include(v, always) {
        if (v == null || !isFinite(v) || v <= 0) return;
        if (always || (v >= lo - near && v <= hi + near)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      }
      symPositions().forEach(function (p) { include(p.price_open); include(p.stop_loss); include(p.take_profit); });
      symOrders().forEach(function (o) { include(o.price); });
      if (pending) include(pending.price, true);
      include(draftValue(slInput), true); include(draftValue(tpInput), true);
      var lqr = liveQuote(); if (lqr.state === 'live') { include(lqr.bid, true); include(lqr.ask, true); }
      span = hi - lo || 1;
      autoSpan = span * 1.14;
      return { lo: lo - span * 0.07, hi: hi + span * 0.07 };
    }

    var lastQuoteFrame = null;
    var lastPendNotice = null;        // text shown instead of the pending lines when the broker feed is not fresh
    var lastPendFrame = null;         // what the pending-order lines last DREW (screen coordinates); null when none were drawn
    var lastObFrame = null;           // what the order block zones last DREW (screen coordinates); null when nothing was drawn
    var lastFvgFrame = null;          // what the zones last DREW (screen coordinates), for tests and diagnostics; null when nothing was drawn
    function liveQuote() {
      return QUOTES.quoteState({ symbols: state.symbols, name: state.selected, connected: state.connected, receivedAt: state.quoteAt,
        now: Date.now(), maxAgeMs: QUOTE_MAX_AGE_MS, statusAgeSeconds: state.statusAge, maxStatusAgeSeconds: QUOTE_MAX_STATUS_AGE_S });
    }
    this.quoteFrame = function () { return lastQuoteFrame; };
    this.fvgFrame = function () { return lastFvgFrame; };
    this.obFrame = function () { return lastObFrame; };
    this.pendingFrame = function () { return lastPendFrame; };
    var lastRfFrame = [];             // what the Rapid Fire exit lines last DREW (price, kind, ticket), for tests and diagnostics
    this.rfFrame = function () { return lastRfFrame; };
    this.pendingNotice = function () { return lastPendNotice; };
    this.quoteState = liveQuote;

    function yOf(v) { return pad.t + (lastRange.hi - v) / (lastRange.hi - lastRange.lo) * (H - pad.t - pad.b); }
    function vOf(y) { return lastRange.hi - (y - pad.t) / (H - pad.t - pad.b) * (lastRange.hi - lastRange.lo); }

    function rfLevels() {
      var ps = symPositions();
      return (state.rfLevels || []).filter(function (lv) { return lv.symbol === state.selected && ps.some(function (p) { return p.ticket === lv.ticket; }); });
    }
    function lines() {
      /* Everything draggable/visible besides candles, in hit-test priority order. */
      var out = [];
      if (pending) {
        var pp = posByTicket(pending.ticket);
        if (pp) out.push({ id: 'pending', kind: 'pending', field: pending.field, price: pending.price, ticket: pp.ticket, drag: true });
      }
      symPositions().forEach(function (p) {
        var moving = pending && pending.ticket === p.ticket ? pending.field : null;
        out.push({ id: 'entry-' + p.ticket, kind: 'entry', price: p.price_open, pos: p, drag: false });
        if (p.stop_loss != null) out.push({ id: 'sl-' + p.ticket, kind: 'sl', price: p.stop_loss, pos: p, drag: true, faint: moving === 'sl' });
        if (p.take_profit != null) out.push({ id: 'tp-' + p.ticket, kind: 'tp', price: p.take_profit, pos: p, drag: true, faint: moving === 'tp' });
      });
      rfLevels().forEach(function (lv) {       // Rapid Fire's own (software-managed) exits, only for positions that are on the MT5 account right now
        if (lv.tp && lv.tp.price != null) out.push({ id: 'rftp-' + lv.ticket, kind: 'rf', rf: 'tp', field: 'RF TP ' + String(lv.side || '').toUpperCase(), price: lv.tp.price, lv: lv, info: lv.tp, drag: false });
        if (lv.sl && lv.sl.price != null) out.push({ id: 'rfsl-' + lv.ticket, kind: 'rf', rf: 'sl', field: 'RF EMERGENCY ' + String(lv.side || '').toUpperCase(), price: lv.sl.price, lv: lv, info: lv.sl, drag: false });
      });
      var ds = draftValue(slInput), dt = draftValue(tpInput);
      if (ds != null) out.push({ id: 'draft-sl', kind: 'draft', field: 'sl', price: ds, drag: true });
      if (dt != null) out.push({ id: 'draft-tp', kind: 'draft', field: 'tp', price: dt, drag: true });
      return out;
    }

    var DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    function two(n) { return ('0' + n).slice(-2); }
    function axisTime(t) {
      var d = new Date(t * 1000);
      if (state.tf === 'D1') return { text: d.getUTCDate() + ' ' + MON[d.getUTCMonth()], major: d.getUTCDate() <= 7 };
      var dayStart = d.getUTCHours() === 0 && d.getUTCMinutes() === 0;
      return dayStart ? { text: d.getUTCDate() + ' ' + MON[d.getUTCMonth()], major: true }
                      : { text: two(d.getUTCHours()) + ':' + two(d.getUTCMinutes()), major: false };
    }
    function fullTime(t) {
      var d = new Date(t * 1000);
      return DOW[d.getUTCDay()] + ' ' + d.getUTCDate() + ' ' + MON[d.getUTCMonth()] + ' ' + two(d.getUTCHours()) + ':' + two(d.getUTCMinutes());
    }

    function setHud(key, build) {
      var node = inRole(root, key[0]);
      if (hudCache[key[0]] === key[1]) return;
      hudCache[key[0]] = key[1];
      build(node);
    }


    /* Market-structure overlay (analysis only: these are NOT orders and never use the trade colours / handles).
       Items come from the server's engine for exactly the candles on screen and are placed by candle time and
       price; anything whose time is not among the loaded candles is skipped, never drawn at a guessed spot. */
    /* Liquidity pools (dotted, amber = buy-side, cyan = sell-side) and sweeps (diamonds). Analysis only: never draggable, never
       an order level, and replaced as a whole on every refresh (state.liquidity is rebuilt, not appended to). */
    function drawLiquidity(ir, plotL, plotR, plotW) {
      var lq = state.liquidity;
      if (!state.liquidityVisible || !lq || lq.key !== state.candleKey || !lq.items) return;     // hidden, unavailable, or another symbol/timeframe
      var LC = LIQ_COLORS[chartTheme()] || LIQ_COLORS.dark, small = W < 520;
      var focusId = state.analysisFocus && state.analysisFocus.symbol === state.selected && state.analysisFocus.tf === state.tf ? state.analysisFocus.id : null;
      ctx.save();
      ctx.beginPath(); ctx.rect(plotL, 0, plotW, H); ctx.clip();
      ctx.font = '600 ' + (small ? 9.5 : 10.5) + 'px ' + cssVar('--mono'); ctx.textBaseline = 'bottom'; ctx.textAlign = 'left';
      lq.items.pools.forEach(function (p) {
        if (p.to < ir.first || p.from > ir.last) return;
        var y = Math.round(yOf(p.level)) + 0.5, x1 = Math.max(plotL, xOf(p.from, ir)), x2 = Math.min(plotR, xOf(p.to, ir));
        if (y < pad.t - 2 || y > H - pad.b + 2 || x2 <= x1) return;
        var color = LC[p.side], swept = p.state === 'SWEPT', focus = focusId === p.id;
        ctx.globalAlpha = swept ? 0.4 : (p.provisional ? 0.6 : 0.95);
        ctx.strokeStyle = color; ctx.lineWidth = focus ? 2.5 : (p.kind === 'equal' ? 1.6 : 1); ctx.setLineDash(swept ? [1, 4] : [2, 3]);
        ctx.beginPath(); ctx.moveTo(x1, y); ctx.lineTo(x2, y); ctx.stroke(); ctx.setLineDash([]);
        if (x2 - x1 >= 34) { ctx.fillStyle = color; ctx.globalAlpha = swept ? 0.55 : 1; ctx.fillText(p.label + (p.provisional ? '?' : ''), x1 + 3, y - 2); }
        if (focus) { ctx.globalAlpha = 1; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x1, y, 6, 0, Math.PI * 2); ctx.stroke(); }
      });
      lq.items.sweeps.forEach(function (s) {
        if (s.index < ir.first || s.index > ir.last) return;
        var x = xOf(s.index, ir), y = Math.round(yOf(s.level)) + 0.5, r = 5;
        if (y < pad.t - 2 || y > H - pad.b + 2) return;
        var color = LC[s.side], focus = focusId === s.id || focusId === s.poolId;
        ctx.globalAlpha = 1; ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = focus ? 2.5 : 1.5;
        ctx.beginPath(); ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath();
        if (s.confirmed) ctx.fill(); else ctx.stroke();            // hollow = still forming, may vanish
        if (focus) { ctx.beginPath(); ctx.arc(x, y, 10, 0, Math.PI * 2); ctx.stroke(); }
      });
      ctx.restore();
    }

    function drawStructure(ir, plotL, plotR, plotW, C) {
      var st = state.structure;
      if (!state.structureVisible || !st || st.key !== state.candleKey || !st.items) return;   // hidden, or data of another symbol/timeframe
      var SC = STRUCT_COLORS[chartTheme()] || STRUCT_COLORS.dark, small = W < 520;
      ctx.save();
      ctx.font = '600 ' + (small ? 10 : 11) + 'px ' + cssVar('--mono'); ctx.textBaseline = 'middle'; ctx.textAlign = 'center';
      st.items.events.forEach(function (e) {
        if (e.to < ir.first || e.from > ir.last || !isFinite(e.level)) return;
        var y = Math.round(yOf(e.level)) + 0.5, x1 = xOf(e.from, ir), x2 = xOf(e.to, ir);
        if (y < pad.t - 2 || y > H - pad.b + 2) return;
        var color = e.type === 'CHOCH' ? SC.choch : SC.bos;
        var focus = state.analysisFocus && state.analysisFocus.id === e.id && state.analysisFocus.symbol === state.selected && state.analysisFocus.tf === state.tf;
        ctx.globalAlpha = e.confirmed ? 0.9 : 0.5;
        ctx.strokeStyle = color; ctx.lineWidth = focus ? 2.5 : 1; ctx.setLineDash(e.confirmed ? [5, 3] : [2, 3]);
        ctx.beginPath(); ctx.moveTo(x1, y); ctx.lineTo(x2, y); ctx.stroke(); ctx.setLineDash([]);
        if (focus) {                                   // chosen on the Analysis page: a ring on the breaking candle, not a trade line
          ctx.globalAlpha = 1; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x2, y, 7, 0, Math.PI * 2); ctx.stroke();
        }
        if (x2 - x1 >= 30) {
          var text = (e.type === 'CHOCH' ? 'CHoCH' : 'BOS') + (e.confirmed ? '' : (x2 - x1 >= 110 ? ' (forming)' : '?')),
              mx = (x1 + x2) / 2, ty = y + (e.direction === 'bullish' ? -8 : 8);
          ctx.lineWidth = 3; ctx.strokeStyle = C.pillBg; ctx.strokeText(text, mx, ty);
          ctx.fillStyle = color; ctx.fillText(text, mx, ty);
        }
      });
      st.items.swings.forEach(function (sw) {
        if (sw.index < ir.first || sw.index > ir.last) return;
        var up = sw.label === 'HH' || sw.label === 'HL', down = sw.label === 'LH' || sw.label === 'LL';
        var y = yOf(sw.price) + (sw.kind === 'high' ? -9 : 9), x = xOf(sw.index, ir);
        if (y < pad.t - 4 || y > H - pad.b + 4) return;
        ctx.globalAlpha = sw.confirmed ? 1 : 0.55;
        ctx.lineWidth = 3; ctx.strokeStyle = C.pillBg; ctx.strokeText(sw.label, x, y);
        ctx.fillStyle = up ? SC.hh : down ? SC.ll : SC.eq; ctx.fillText(sw.label, x, y);
      });
      ctx.restore();
    }
    /* Order block zones: a translucent box from the source candle to the newest candle (live blocks only), between the engine's own upper
       and lower boundary (nothing is recalculated here). Solid + stronger fill + edge bar = confirmed; dashed and fainter = a candidate
       or a block whose status rests on the still-forming candle. Drawn BEHIND the candles. Not an order level, never draggable, replaced
       as a whole on every refresh. A block is a candle zone chosen by price structure: no volume or order-flow data stands behind it. */
    function drawOB(ir, plotL, plotR, plotW) {
      lastObFrame = null;
      var ob = state.ob;
      if (!state.obVisible || !ob || ob.key !== state.candleKey || !ob.items || !lastRange || !zonesFresh()) return;   // hidden, unavailable, or another symbol/timeframe
      var OC = OB_COLORS[chartTheme()] || OB_COLORS.dark, small = W < 520, half = Math.max(3, ir.s * 0.35), rects = [];
      ctx.save();
      ctx.beginPath(); ctx.rect(plotL, pad.t, plotW, H - pad.t - pad.b); ctx.clip();
      ctx.font = '600 ' + (small ? 9.5 : 10.5) + 'px ' + cssVar('--mono'); ctx.textBaseline = 'top'; ctx.textAlign = 'left';
      ob.items.blocks.forEach(function (b) {
        if (b.to < ir.first || b.from > ir.last) return;
        var x1 = xOf(b.from, ir) - half, x2 = xOf(b.to, ir) + half, yTop = yOf(b.upper), yBot = yOf(b.lower);
        if (!isFinite(yTop) || !isFinite(yBot) || yBot < pad.t || yTop > H - pad.b) return;
        var h = Math.max(1, yBot - yTop), color = OC[b.direction], settled = b.confirmed && !b.provisional, left = Math.max(x1, plotL);
        ctx.globalAlpha = settled ? 0.16 : 0.07; ctx.fillStyle = color; ctx.fillRect(x1, yTop, x2 - x1, h);
        ctx.globalAlpha = settled ? 0.9 : 0.75; ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash(settled ? [] : [5, 3]);
        ctx.strokeRect(x1 + 0.5, yTop + 0.5, Math.max(0, x2 - x1 - 1), Math.max(0, h - 1)); ctx.setLineDash([]);
        if (settled) { ctx.globalAlpha = 0.95; ctx.fillStyle = color; ctx.fillRect(x1, yTop, 3, h); }       // the edge bar marks a settled, confirmed block
        if (x2 - left >= 60 && h >= 13) { ctx.globalAlpha = 1; ctx.fillStyle = color; ctx.fillText(b.label, left + 6, yTop + 2); }
        rects.push({ id: b.id, direction: b.direction, status: b.status, confirmed: b.confirmed, provisional: b.provisional, upper: b.upper, lower: b.lower,
                     from: b.from, to: b.to, x1: x1, x2: x2, yTop: yTop, yBottom: yTop + h });
      });
      ctx.restore();
      lastObFrame = { key: ob.key, rects: rects, range: { hi: lastRange.hi, lo: lastRange.lo },
                      plot: { l: plotL, r: plotR, t: pad.t, b: H - pad.b }, ir: { first: ir.first, last: ir.last, R: ir.R, s: ir.s },
                      theme: chartTheme(), colors: OC };
    }
    /* Fair value gap zones: a translucent box from candle 1 to candle 3 of each gap, between the engine's own upper and lower boundary
       (nothing is recalculated here). Solid = confirmed; dashed and fainter = the newest candle is still forming, so the gap may
       change or vanish. Drawn BEHIND the candles. Not an order level, never draggable, replaced as a whole on every refresh. The zone
       covers only the three candles that form the gap: fills / mitigation are not tracked, so nothing is implied about later price. */
    function drawFVG(ir, plotL, plotR, plotW) {
      lastFvgFrame = null;
      var fv = state.fvg;
      if (!state.fvgVisible || !fv || fv.key !== state.candleKey || !fv.items || !lastRange || !zonesFresh()) return;   // hidden, unavailable, or another symbol/timeframe
      var FC = FVG_COLORS[chartTheme()] || FVG_COLORS.dark, small = W < 520, half = Math.max(3, ir.s * 0.35), rects = [];
      ctx.save();
      ctx.beginPath(); ctx.rect(plotL, pad.t, plotW, H - pad.t - pad.b); ctx.clip();
      ctx.font = '600 ' + (small ? 9.5 : 10.5) + 'px ' + cssVar('--mono'); ctx.textBaseline = 'top'; ctx.textAlign = 'left';
      fv.items.gaps.forEach(function (g) {
        if (g.to < ir.first || g.from > ir.last) return;
        var x1 = xOf(g.from, ir) - half, x2 = xOf(g.to, ir) + half, yTop = yOf(g.upper), yBot = yOf(g.lower);
        if (!isFinite(yTop) || !isFinite(yBot) || yBot < pad.t || yTop > H - pad.b) return;
        var h = Math.max(1, yBot - yTop), color = FC[g.direction];
        ctx.globalAlpha = g.confirmed ? 0.22 : 0.1; ctx.fillStyle = color; ctx.fillRect(x1, yTop, x2 - x1, h);
        ctx.globalAlpha = g.confirmed ? 0.9 : 0.75; ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash(g.confirmed ? [] : [3, 3]);
        ctx.strokeRect(x1 + 0.5, yTop + 0.5, Math.max(0, x2 - x1 - 1), Math.max(0, h - 1)); ctx.setLineDash([]);
        if (x2 - x1 >= 56 && h >= 13) {
          ctx.globalAlpha = 1; ctx.fillStyle = color;
          ctx.fillText(g.label + (g.confirmed ? '' : ' (forming)'), x1 + 3, yTop + 2);
        }
        rects.push({ id: g.id, direction: g.direction, confirmed: g.confirmed, upper: g.upper, lower: g.lower, from: g.from, to: g.to,
                     x1: x1, x2: x2, yTop: yTop, yBottom: yTop + h });
      });
      ctx.restore();
      lastFvgFrame = { key: fv.key, rects: rects, range: { hi: lastRange.hi, lo: lastRange.lo },
                       plot: { l: plotL, r: plotR, t: pad.t, b: H - pad.b }, ir: { first: ir.first, last: ir.last, R: ir.R, s: ir.s },
                       theme: chartTheme(), colors: FC };
    }
    /* ---- moving average line + manual lines (drawn after the candles; they never change the price range or any strategy) ---- */
    var curIr = null, manFrame = [], manDrag = null;
    function timeAtX(x, ir) {
      var c = state.candles, sec = TF_SEC[state.tf] || 60, f = ir.R - (pad.l + plotWidth() - ir.s / 2 - x) / ir.s;
      var i = Math.max(0, Math.min(c.length - 1, Math.floor(f)));
      return c[i][0] + (f - i) * sec;
    }
    function xAtTime(t, ir) {
      var c = state.candles, sec = TF_SEC[state.tf] || 60, lo = 0, hi = c.length - 1, i = 0;
      while (lo <= hi) { var mid = (lo + hi) >> 1; if (c[mid][0] <= t) { i = mid; lo = mid + 1; } else hi = mid - 1; }
      return xOf(i + (t - c[i][0]) / sec, ir);
    }
    function drawMA(ir, plotL, plotR, C) {
      if (!MA.cfg.enabled || !state.candles.length) return;
      var src = maSource(state.selected, state.tf, state.candles), srcSec = TF_SEC[src.tf] || 60;
      if (src.candles.length < MA.cfg.period) { lastMaNote = MA.cfg.type + ' ' + MA.cfg.period + ' (' + src.tf + '): not enough candles yet'; return; }
      var series = maCompute(src.candles, MA.cfg.type, MA.cfg.period), same = src.candles === state.candles, c = state.candles, j = 0, started = false, lastV = null, lastX = 0;
      ctx.save(); ctx.beginPath(); ctx.rect(plotL, pad.t, plotR - plotL, H - pad.t - pad.b); ctx.clip();
      ctx.strokeStyle = (C && C.ma) || '#f2b24a'; ctx.lineWidth = 1.7; ctx.lineJoin = 'round'; ctx.beginPath();
      for (var i = ir.first; i <= ir.last; i++) {
        var v;
        if (same) v = series[i];
        else {                                                         /* the newest CLOSED candle of the MA timeframe at this chart candle's time: no look-ahead */
          while (j + 1 < src.candles.length && src.candles[j + 1][0] + srcSec <= c[i][0]) j++;
          v = (src.candles[j][0] + srcSec <= c[i][0]) ? series[j] : null;
        }
        if (v == null) { started = false; continue; }
        var x = xOf(i, ir), y = yOf(v);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        lastV = v; lastX = x;
      }
      ctx.stroke();
      if (lastV != null) {
        ctx.font = '600 11px ' + cssVar('--mono'); ctx.fillStyle = (C && C.ma) || '#f2b24a'; ctx.textAlign = 'right';
        ctx.fillText(MA.cfg.type + ' ' + MA.cfg.period + ' ' + src.tf, Math.min(plotR - 6, lastX), Math.max(pad.t + 12, yOf(lastV) - 8));
      }
      ctx.restore();
    }
    var lastMaNote = '';
    function drawManual(ir, plotL, plotR) {
      manFrame = [];
      if (!state.candles.length || !lastRange) return;
      ctx.save(); ctx.beginPath(); ctx.rect(plotL, pad.t, plotR - plotL, H - pad.t - pad.b); ctx.clip();
      MAN.lines.forEach(function (l) {
        var x1 = xAtTime(l.t1, ir), y1 = yOf(l.p1), x2 = xAtTime(l.t2, ir), y2 = yOf(l.p2), sel = MAN.selected === l.id;
        manFrame.push({ id: l.id, x1: x1, y1: y1, x2: x2, y2: y2 });
        ctx.strokeStyle = sel ? '#ffffff' : '#5ec8ff'; ctx.lineWidth = sel ? 2.4 : 1.8; ctx.setLineDash(sel ? [] : [7, 4]);
        ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); ctx.setLineDash([]);
        [[x1, y1], [x2, y2]].forEach(function (p) {
          ctx.beginPath(); ctx.arc(p[0], p[1], sel ? 6 : 3.5, 0, Math.PI * 2); ctx.fillStyle = sel ? '#5ec8ff' : '#0b1220'; ctx.fill(); ctx.strokeStyle = '#5ec8ff'; ctx.lineWidth = 1.5; ctx.stroke();
        });
      });
      ctx.restore();
    }
    function segDist(px, py, a) {
      var dx = a.x2 - a.x1, dy = a.y2 - a.y1, len2 = dx * dx + dy * dy, t = len2 ? Math.max(0, Math.min(1, ((px - a.x1) * dx + (py - a.y1) * dy) / len2)) : 0;
      return Math.hypot(px - (a.x1 + t * dx), py - (a.y1 + t * dy));
    }
    function manualHit(pos, touch) {
      var er = touch ? 16 : 10, br = touch ? 12 : 7, best = null;
      for (var k = manFrame.length - 1; k >= 0; k--) {
        var a = manFrame[k];
        if (Math.hypot(pos.x - a.x1, pos.y - a.y1) <= er) return { id: a.id, part: 'p1' };
        if (Math.hypot(pos.x - a.x2, pos.y - a.y2) <= er) return { id: a.id, part: 'p2' };
        if (!best && segDist(pos.x, pos.y, a) <= br) best = { id: a.id, part: 'body' };
      }
      return best;
    }
    function inPlot(pos) { return pos.x >= pad.l && pos.x <= W - pad.r && pos.y >= pad.t && pos.y <= H - pad.b; }
    function manualPoint(pos) {
      var x = Math.max(pad.l, Math.min(W - pad.r, pos.x)), y = Math.max(pad.t, Math.min(H - pad.b, pos.y));
      return { t: timeAtX(x, curIr), p: vOf(y) };
    }
    function manualDown(e, pos) {
      if (!curIr || !lastRange || !state.candles.length || !MAN.symbol) return false;
      var touch = e.pointerType === 'touch';
      if (MAN.tool && inPlot(pos)) {
        var pt = manualPoint(pos), id = 'm' + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36);
        MAN.lines.push({ id: id, t1: pt.t, p1: pt.p, t2: pt.t, p2: pt.p }); MAN.selected = id;
        manDrag = { mode: 'create', id: id, x0: pos.x, y0: pos.y }; frozenRange = { lo: lastRange.lo, hi: lastRange.hi };
        return true;
      }
      var h = manualHit(pos, touch);
      if (h) {
        MAN.selected = h.id;
        var ln = MAN.lines.filter(function (l) { return l.id === h.id; })[0], pt0 = manualPoint(pos);
        manDrag = { mode: h.part, id: h.id, t0: pt0.t, p0: pt0.p, orig: Object.assign({}, ln), x0: pos.x, y0: pos.y };
        frozenRange = { lo: lastRange.lo, hi: lastRange.hi };
        return true;
      }
      if (MAN.selected) { MAN.selected = null; redrawAll(); }
      return false;
    }
    function manualMove(e, pos) {
      if (!manDrag) return false;
      var ln = MAN.lines.filter(function (l) { return l.id === manDrag.id; })[0];
      if (!ln) { manDrag = null; return false; }
      var pt = manualPoint(pos);
      if (manDrag.mode === 'create' || manDrag.mode === 'p2') { ln.t2 = pt.t; ln.p2 = pt.p; }
      else if (manDrag.mode === 'p1') { ln.t1 = pt.t; ln.p1 = pt.p; }
      else { var dt = pt.t - manDrag.t0, dp = pt.p - manDrag.p0; ln.t1 = manDrag.orig.t1 + dt; ln.t2 = manDrag.orig.t2 + dt; ln.p1 = manDrag.orig.p1 + dp; ln.p2 = manDrag.orig.p2 + dp; }
      self.draw(); return true;
    }
    function manualUp(e) {
      if (!manDrag) return false;
      var d = manDrag; manDrag = null; frozenRange = null;
      if (d.mode === 'create') {
        var a = manFrame.filter(function (f) { return f.id === d.id; })[0];
        if (!a || Math.hypot(a.x2 - a.x1, a.y2 - a.y1) < 8) { MAN.lines = MAN.lines.filter(function (l) { return l.id !== d.id; }); MAN.selected = null; }
        MAN.tool = false;                                                  /* one line per activation; select the tool again for another */
      }
      manSave(); try { canvas.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
      delete pointers[e.pointerId]; redrawAll(); return true;
    }
    var frozenRange = null;
    this.syncExtras = function () {
      var lb = inRole(root, 'line-btn'); if (lb) lb.setAttribute('aria-pressed', MAN.tool ? 'true' : 'false');
      var db = inRole(root, 'line-del'); if (db) db.disabled = !MAN.selected;
      var mb = inRole(root, 'ma-btn'); if (mb) mb.setAttribute('aria-pressed', MA.cfg.enabled ? 'true' : 'false');
      var pop = inRole(root, 'ma-pop');
      if (pop) {
        var en = inRole(root, 'ma-enabled'), ty = inRole(root, 'ma-type'), pe = inRole(root, 'ma-period'), tf = inRole(root, 'ma-tf'), mg = inRole(root, 'ma-msg');
        if (document.activeElement !== pe) pe.value = MA.cfg.period;
        en.checked = MA.cfg.enabled; ty.value = MA.cfg.type; tf.value = MA.cfg.timeframe; mg.textContent = MA.msg || (lastMaNote && MA.cfg.enabled ? lastMaNote : '');
      }
      canvas.classList.toggle('is-drawing', MAN.tool);
    };

    this.draw = function () {
      lastPendFrame = null; lastPendNotice = null; lastZoneNotices = [];
      lastObFrame = null;
      lastFvgFrame = null;               // before the early return: a hidden chart must not keep reporting zones it is no longer drawing
      if (!W || !H) return;
      var C = CHART_COLORS[chartTheme()] || CHART_COLORS.dark;
      var small = W < 520;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      var sym = symbolByName(state.selected), msg = inRole(root, 'chart-msg');
      var d = digitsOf(sym), n = state.candles.length;
      var name = state.selected || '---';
      inRole(root, 'chart-title').textContent = name;
      inRole(root, 'chart-title-bar').textContent = name;
      inRole(root, 'hud-tf').textContent = state.tf;
      var sBtn = inRole(root, 'structure-btn'); if (sBtn) sBtn.setAttribute('aria-pressed', state.structureVisible ? 'true' : 'false');
      var lBtn = inRole(root, 'liquidity-btn'); if (lBtn) lBtn.setAttribute('aria-pressed', state.liquidityVisible ? 'true' : 'false');
      var fBtn = inRole(root, 'fvg-btn'); if (fBtn) fBtn.setAttribute('aria-pressed', state.fvgVisible ? 'true' : 'false');
      var oBtn = inRole(root, 'ob-btn'); if (oBtn) oBtn.setAttribute('aria-pressed', state.obVisible ? 'true' : 'false');
      var aBtn = inRole(root, 'overlays-btn'); if (aBtn) aBtn.setAttribute('aria-pressed', anyOverlayVisible() ? 'true' : 'false');
      root.querySelectorAll('[data-ctype]').forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-ctype') === state.ctype ? 'true' : 'false'); });

      pad.t = small ? 50 : 60; pad.b = small ? 30 : 34;
      var fAxis = (small ? 12 : 13) + 'px ' + cssVar('--mono'), fTag = '600 ' + (small ? 12 : 13) + 'px ' + cssVar('--mono');
      ctx.font = fAxis;
      pad.r = Math.max(62, Math.ceil(ctx.measureText('0'.repeat(Math.max(4, price(sym && sym.bid != null ? sym.bid : 1, d).length + 1))).width) + 22);

      if (view.key !== state.candleKey) { view.key = state.candleKey; view.back = -RIGHT_PAD; view.yRange = null; }
      if (!n) {
        lastRange = null;
        msg.hidden = false;
        inRole(root, 'chart-msg-text').textContent = state.connected ? state.candleMsg : 'Waiting for market data...';
        hudCache = {};
        inRole(root, 'hud-price').textContent = liveQuote().state === 'live' ? price(liveQuote().bid, d) : '---';
        inRole(root, 'hud-chg').textContent = '';
        inRole(root, 'hud-ohlc').textContent = '';
        return;
      }
      clampBack();
      var ir = indexRange(n); curIr = ir; maLoad(); manLoad(state.selected);
      lastRange = computeRange(ir, sym);
      if (!lastRange) { msg.hidden = false; return; }
      msg.hidden = true;

      var plotL = pad.l, plotR = W - pad.r, plotT = pad.t, plotB = H - pad.b, plotW = plotR - plotL;
      var lastC = state.candles[n - 1];
      var up = lastC[4] >= lastC[1];
      var tags = [];     // axis tags, collision-resolved below

      // ---- horizontal grid + price labels (nice round steps, never closer than ~64px) ----
      var maxLabels = Math.max(3, Math.floor((plotB - plotT) / 64));
      var step = niceStep((lastRange.hi - lastRange.lo) / maxLabels);
      var gridVals = [];
      for (var v = Math.ceil(lastRange.lo / step) * step, guard = 0; v <= lastRange.hi && guard < 60; v += step, guard++) gridVals.push(v);
      ctx.lineWidth = 1; ctx.strokeStyle = C.grid;
      gridVals.forEach(function (gv) {
        var y = Math.round(yOf(gv)) + 0.5;
        ctx.beginPath(); ctx.moveTo(plotL, y); ctx.lineTo(plotR, y); ctx.stroke();
      });

      // ---- vertical grid + time labels (spacing derived from pixels, so they never collide) ----
      var minGap = small ? 78 : 104, every = Math.max(1, Math.ceil(minGap / ir.s));
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      var lastLabelRight = -1e9;
      for (var i0 = Math.ceil(ir.first / every) * every; i0 <= ir.last; i0 += every) {
        var tx = xOf(i0, ir);
        if (tx < plotL + 24 || tx > plotR - 24) continue;
        var tt = axisTime(state.candles[i0][0]);
        ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(Math.round(tx) + 0.5, plotT); ctx.lineTo(Math.round(tx) + 0.5, plotB); ctx.stroke();
        ctx.font = (tt.major ? '700 ' : '') + (small ? 12 : 13) + 'px ' + cssVar('--mono');
        var tw = ctx.measureText(tt.text).width;
        if (tx - tw / 2 > lastLabelRight + 10) {
          ctx.fillStyle = tt.major ? C.text : C.axis; ctx.fillText(tt.text, tx, plotB + (H - plotB) / 2);
          lastLabelRight = tx + tw / 2;
        }
      }
      // axis frame
      ctx.strokeStyle = C.frame; ctx.beginPath();
      ctx.moveTo(plotR + 0.5, plotT); ctx.lineTo(plotR + 0.5, plotB + 0.5); ctx.lineTo(plotL, plotB + 0.5); ctx.stroke();

      // ---- series ----
      ctx.save();
      ctx.beginPath(); ctx.rect(plotL, 0, plotW, H); ctx.clip();
      drawOB(ir, plotL, plotR, plotW);                               // behind the candles, and behind the fair value gaps
      drawFVG(ir, plotL, plotR, plotW);                              // behind the candles
      if (state.ctype === 'line') {
        ctx.beginPath();
        for (var li = ir.first; li <= ir.last; li++) {
          var lx = xOf(li, ir), ly = yOf(state.candles[li][4]);
          if (li === ir.first) ctx.moveTo(lx, ly); else ctx.lineTo(lx, ly);
        }
        var grad = ctx.createLinearGradient(0, plotT, 0, plotB);
        grad.addColorStop(0, C.areaTop); grad.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.strokeStyle = C.line; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
        ctx.lineTo(xOf(ir.last, ir), plotB); ctx.lineTo(xOf(ir.first, ir), plotB); ctx.closePath();
        ctx.fillStyle = grad; ctx.fill();
      } else {
        var bw = Math.max(3, Math.floor(ir.s * 0.7)); if (bw % 2 === 0) bw++;
        var wickW = ir.s >= 8 ? 1.5 : 1;
        var outBull = STYLE.isLowContrast(state.candleStyle.bull, chartTheme()), outBear = STYLE.isLowContrast(state.candleStyle.bear, chartTheme());
        for (var ci = ir.first; ci <= ir.last; ci++) {
          var c = state.candles[ci], kind = STYLE.candleKind(c[1], c[4]), x = Math.round(xOf(ci, ir));
          var col = STYLE.candleColor(kind, state.candleStyle, C.axis);   // doji (close == open) = neutral, never bull/bear
          var needsOutline = (kind === 'bull' && outBull) || (kind === 'bear' && outBear);
          if (needsOutline) {   // a wick in a colour that hides on this background (black on the dark chart) gets a thin light halo; its own colour is untouched
            ctx.strokeStyle = C.outline; ctx.lineWidth = wickW + 2;
            ctx.beginPath(); ctx.moveTo(x, yOf(c[2]) - 1); ctx.lineTo(x, yOf(c[3]) + 1); ctx.stroke();
          }
          ctx.fillStyle = ctx.strokeStyle = col;
          ctx.lineWidth = wickW;
          ctx.beginPath(); ctx.moveTo(x, yOf(c[2])); ctx.lineTo(x, yOf(c[3])); ctx.stroke();
          var yo = yOf(c[1]), yc = yOf(c[4]), top = Math.min(yo, yc), h = Math.max(1.5, Math.abs(yc - yo));
          ctx.fillRect(x - (bw - 1) / 2, top, bw, h);
          if (needsOutline) {   // outline only; the fill colour is untouched
            ctx.save(); ctx.lineWidth = 1; ctx.strokeStyle = C.outline;
            ctx.strokeRect(x - (bw - 1) / 2 + 0.5, top + 0.5, bw - 1, Math.max(0.5, h - 1));
            ctx.restore();
          }
        }
      }
      drawStructure(ir, plotL, plotR, plotW, C);
      drawLiquidity(ir, plotL, plotR, plotW);
      ctx.restore();
      drawMA(ir, plotL, plotR, C);
      drawManual(ir, plotL, plotR);

      // ---- bid (RED) / ask (GREEN) quote lines: real MT5 quotes, drawn only while LIVE. Not trade levels: never draggable ----
      function quoteLine(val, color) {
        var y = Math.round(yOf(val)) + 0.5;
        ctx.save(); ctx.beginPath(); ctx.rect(plotL, 0, plotW, H); ctx.clip();
        ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash([]);
        ctx.beginPath(); ctx.moveTo(plotL, y); ctx.lineTo(plotR, y); ctx.stroke();
        ctx.restore();
      }
      var lq = liveQuote(), hasQuote = lq.state === 'live';
      lastQuoteFrame = { state: lq.state, reason: lq.reason, symbol: state.selected, tf: state.tf, theme: chartTheme() };
      if (hasQuote) {
        var yA = yOf(lq.ask), yB = yOf(lq.bid);
        if (yB - yA >= 1) { ctx.fillStyle = C.spread; ctx.fillRect(plotL, yA, plotW, yB - yA); }
        quoteLine(lq.ask, C.ask);
        quoteLine(lq.bid, C.bid);
        tags.push({ y: yA, text: price(lq.ask, d), bg: C.ask, fg: C.askFg, w: 700 });
        tags.push({ y: yB, text: price(lq.bid, d), bg: C.bid, fg: C.bidFg, w: 700, big: true });
        ctx.save(); ctx.font = '700 ' + (small ? 10 : 11) + 'px ' + cssVar('--mono'); ctx.textAlign = 'right'; ctx.textBaseline = 'bottom';
        ctx.fillStyle = C.ask; ctx.fillText('ASK', plotR - 6, Math.round(yA) - 2);
        ctx.fillStyle = C.bid; ctx.textBaseline = 'top'; ctx.fillText('BID', plotR - 6, Math.round(yB) + 2);
        ctx.restore();
        lastQuoteFrame.bid = { price: lq.bid, y: yB, color: C.bid }; lastQuoteFrame.ask = { price: lq.ask, y: yA, color: C.ask };
        lastQuoteFrame.spreadPoints = lq.spreadPoints; lastQuoteFrame.spreadPrice = lq.spreadPrice;
      } else if (lastC) {
        tags.push({ y: yOf(lastC[4]), text: price(lastC[4], d), bg: STYLE.candleColor(STYLE.candleKind(lastC[1], lastC[4]), state.candleStyle, C.axis), fg: '#fff', w: 700, big: true });   // last CANDLE close, not a quote
      }
      ctx.save(); ctx.font = '600 ' + (small ? 10.5 : 11.5) + 'px ' + cssVar('--mono'); ctx.textAlign = 'right'; ctx.textBaseline = 'top';
      ctx.fillStyle = hasQuote ? C.axis : (chartTheme() === 'light' ? '#8A4B00' : '#F2B76B');
      ctx.fillText(QUOTES.badge(lq, d), plotR - 8, pad.t + 6);
      ctx.restore();

      // ---- position / draft / pending lines ----
      var cur = state.account ? state.account.currency : '';
      var specEntry = state.spec[state.selected];
      var curCode = cur || (specEntry && specEntry.currency) || '';
      var hx = plotR - 34;
      function hline(y, color, dash, width) {
        ctx.strokeStyle = color; ctx.lineWidth = width || 1; ctx.setLineDash(dash || []);
        ctx.beginPath(); ctx.moveTo(plotL, y); ctx.lineTo(plotR, y); ctx.stroke(); ctx.setLineDash([]);
      }
      function handle(x, y, color) {
        ctx.fillStyle = color; ctx.strokeStyle = C.handleRing; ctx.lineWidth = 2.5;
        ctx.beginPath(); ctx.arc(x, y, 7.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      }
      function pill(x, y, text, color, fg) {      // single-line label, used for levels that are off-screen
        if (y < plotT + 16) y += 34;
        ctx.font = '700 ' + (small ? 12 : 13) + 'px ' + cssVar('--mono');
        var w = Math.min(plotW - 16, ctx.measureText(text).width + 16);
        ctx.fillStyle = C.pillBg; ctx.strokeStyle = color; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.roundRect(x, y - 11, w, 22, 11); ctx.fill(); ctx.stroke();
        ctx.fillStyle = fg || color; ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText(text, x + 8, y + 0.5);
      }
      var slColor = C.down, tpColor = C.up;
      var fs = small ? 12 : 13, rowH = small ? 16 : 18;
      var cards = [];      // glass info cards beside each level; laid out together below so they never overlap
      var pendingY = null;

      /* Pips + money for one SL/TP level, from the REAL trade parameters (side, entry, lots) and MT5's tick data.
         It is recomputed on every redraw, so it follows the line live while it is being dragged. */
      function levelInfo(ln) {
        var sym2 = symbolByName(state.selected), spec = specFor(state.selected), side, entry, vol;
        var merged = Object.assign({}, sym2 ? { point: sym2.point, digits: sym2.digits } : {}, spec || {});
        if (ln.kind === 'sl' || ln.kind === 'tp') { side = ln.pos.direction; entry = ln.pos.price_open; vol = ln.pos.volume; }
        else if (ln.kind === 'pending') {
          var pp = posByTicket(pending.ticket);
          if (!pp) return null;
          side = pp.direction; entry = pp.price_open; vol = pp.volume;
        } else if (ln.kind === 'draft' && sym2) {
          vol = parseFloat(lotInput.value);
          side = TradeMatePL.inferSide(ln.field, ln.price, sym2.bid, sym2.ask);
          entry = TradeMatePL.entryFor(side, sym2.bid, sym2.ask);
        } else return null;
        if (!side) return null;
        return { side: side, pips: TradeMatePL.pips(merged, side, entry, ln.price),
                 money: TradeMatePL.money(merged, side, entry, ln.price, vol) };
      }
      function signColor(info) {
        var v = info && info.money != null ? info.money : (info ? info.pips : null);
        return v == null ? C.axis : v > 0 ? C.up : v < 0 ? C.down : C.axis;
      }
      function levelRows(ln, title, color, note) {
        var info = levelInfo(ln), sc = signColor(info);
        return [
          [{ t: title, c: color, w: 700 }, { t: '  ' + price(ln.price, d), c: C.text, w: 700 }].concat(note ? [{ t: '  ' + note, c: C.axis, w: 600 }] : []),
          [{ t: TradeMatePL.fmtPips(info ? info.pips : null), c: sc, w: 600 }, { t: '   ' + TradeMatePL.fmtMoney(info ? info.money : null, curCode), c: sc, w: 700 }]
        ];
      }

      lastRfFrame = [];
      lines().forEach(function (ln) {
        if (ln.price == null) return;
        if (ln.price < lastRange.lo || ln.price > lastRange.hi) {      // off-screen level: edge marker, not a draggable line
          var above = ln.price > lastRange.hi, ey = above ? plotT + 14 : plotB - 14;
          var inf = (selectedLineId === ln.id && ln.kind !== 'entry') ? levelInfo(ln) : null;
          var offPL = ln.kind === 'entry' ? TradeMatePL.positionPL(ln.pos, Object.assign({}, symbolByName(ln.pos.symbol) ? { point: symbolByName(ln.pos.symbol).point, digits: symbolByName(ln.pos.symbol).digits } : {}, specFor(ln.pos.symbol) || {})).value : null;
          var lbl = (ln.kind === 'entry' ? TradeMatePL.entryLabel(ln.pos.direction, plText(offPL, cur)) : (ln.field || ln.kind).toUpperCase()) + (above ? ' \u25B2 ' : ' \u25BC ') + price(ln.price, d) +
            (inf && inf.money != null ? '  ' + TradeMatePL.fmtMoney(inf.money, curCode) : '');
          pill(plotL + 8, ey, lbl, ln.kind === 'rf' ? (ln.rf === 'sl' ? slColor : tpColor) : ln.kind === 'sl' ? slColor : ln.kind === 'tp' ? tpColor : C.entry);
          return;
        }
        var y = Math.round(yOf(ln.price)) + 0.5;
        if (ln.kind === 'entry') {
          var p = ln.pos;
          hline(y, C.entry, [10, 5], 1.25);
          tags.push({ y: y, text: price(p.price_open, d), bg: C.entryTag, fg: '#fff', w: 600 });
          // EVERY entry line carries its own live "BUY +x.xx" / "SELL -x.xx" label (not only the selected one).
          var pSym = symbolByName(p.symbol), pSpec = Object.assign({}, pSym ? { point: pSym.point, digits: pSym.digits } : {}, specFor(p.symbol) || {});
          var pl = TradeMatePL.positionPL(p, pSpec).value;
          var cls = plClass(pl), plc = cls === 'pl-pos' ? C.up : cls === 'pl-neg' ? C.down : C.axis;
          cards.push({ y: y, border: plc, alpha: 1, rows: [[{ t: (p.direction === 'buy' ? '\u25B2 ' : '\u25BC ') + TradeMatePL.entryLabel(p.direction, plText(pl, cur)), c: plc, w: 700 }]] });
        } else if (ln.kind === 'sl' || ln.kind === 'tp') {
          var col = ln.kind === 'sl' ? slColor : tpColor, fa = ln.faint ? 0.35 : 1;
          ctx.globalAlpha = fa;
          hline(y, col, [], 1.75);
          tags.push({ y: y, text: price(ln.price, d), bg: col, fg: '#fff', w: 700 });
          handle(hx, y, col);
          ctx.globalAlpha = 1;
          if (selectedLineId === ln.id) {
            cards.push({ y: y, border: col, alpha: fa, rows: levelRows(ln, ln.kind.toUpperCase(), col, '') });
          }
        } else if (ln.kind === 'rf') {                  // Rapid Fire exit: dotted, read-only, labelled with its price, distance and money
          var rc = ln.rf === 'sl' ? slColor : tpColor, ri = ln.info || {}, rsign = ln.rf === 'sl' ? -1 : 1;
          hline(y, rc, [2, 4], 2.25);
          tags.push({ y: y, text: price(ln.price, d), bg: rc, fg: '#fff', w: 700 });
          cards.push({ y: y, border: rc, alpha: 1, rows: [[{ t: ln.field, c: rc, w: 700 }, { t: '  ' + price(ln.price, d), c: C.text, w: 700 }, { t: '  software', c: C.axis, w: 600 }],
            [{ t: (ri.points != null ? rsign * ri.points + ' pts' : ''), c: rc, w: 600 }, { t: ri.money != null ? '   ' + TradeMatePL.fmtMoney(rsign * ri.money, curCode) : '', c: rc, w: 700 }]] });
          lastRfFrame.push({ ticket: ln.lv.ticket, kind: ln.rf, price: ln.price, y: y, points: ri.points, money: ri.money });
        } else if (ln.kind === 'pending') {
          var pc = ln.field === 'sl' ? slColor : tpColor;
          hline(y, pc, [8, 5], 2.25);
          tags.push({ y: y, text: price(ln.price, d), bg: pc, fg: '#fff', w: 700 });
          handle(hx, y, '#FFFFFF');
          cards.push({ y: y, border: pc, alpha: 1, rows: levelRows(ln, 'NEW ' + ln.field.toUpperCase(), pc, 'not sent') });
          pendingY = y;
        } else if (ln.kind === 'draft') {
          var dc = ln.field === 'sl' ? slColor : tpColor, dinfo = levelInfo(ln);
          hline(y, dc, [3, 4], 1.75);
          tags.push({ y: y, text: price(ln.price, d), bg: dc, fg: '#fff', w: 700 });
          handle(hx, y, dc);
          cards.push({ y: y, border: dc, alpha: 1, rows: levelRows(ln, ln.field.toUpperCase(), dc, dinfo ? 'if ' + dinfo.side.toUpperCase() : 'draft') });
        }
      });

      // ---- pending orders: read-only lines at the broker's real entry price (never draggable, never sent anywhere) ----
      lastZoneNotices = [];
      (function drawZoneNotices() {                // a clear message instead of silence when a switched-on overlay cannot be drawn
        if (!state.fvgVisible && !state.obVisible) return;
        var msgs = [];
        if (!zonesFresh()) msgs.push('Zones hidden: chart data is not current');
        else {
          if (state.fvgVisible) { var a = zoneNoticeFor(state.fvg, 'Fair value gaps'); if (a) msgs.push(a); }
          if (state.obVisible) { var b = zoneNoticeFor(state.ob, 'Order blocks'); if (b) msgs.push(b); }
        }
        var PCn = chartTheme() === 'light' ? '#8A5A00' : '#F2B24A';   // amber: a notice, not an order colour
        msgs.forEach(function (m, i) { lastZoneNotices.push(m); pill(plotL + 8, plotT + 74 + i * 26, m.toUpperCase(), PCn); });
      })();
      (function drawPendingOrders() {
        var orders = symOrders(), PC = PEND_COLORS[chartTheme()] || PEND_COLORS.dark, PF = PEND_FG[chartTheme()] || PEND_FG.dark, drawn = [];
        lastPendNotice = null;
        if (!orders.length && symOrders(true).length) {               // we hold orders but the broker feed is not fresh: say so, draw none
          lastPendNotice = 'Pending orders hidden: no fresh broker data';
          pill(plotL + 8, plotT + 14, lastPendNotice.toUpperCase(), PC);
        }
        orders.forEach(function (o) {
          var buy = o.direction === 'buy', name = (PEND_LABEL[o.order_type] || o.order_type).toUpperCase();
          var head = (buy ? '\u25B2 ' : '\u25BC ') + name + (o.volume == null ? '' : ' ' + o.volume.toFixed(2)) + ' @ ' + price(o.price, d);
          if (o.price < lastRange.lo || o.price > lastRange.hi) {          // off-screen entry: an edge marker, like the other levels
            var above = o.price > lastRange.hi;
            pill(plotL + 8, above ? plotT + 14 : plotB - 14, 'PENDING ' + head + (above ? ' \u25B2' : ' \u25BC'), PC);
            drawn.push({ ticket: o.ticket, order_type: o.order_type, direction: o.direction, price: o.price, y: null, offscreen: true, label: head });
            return;
          }
          var y = Math.round(yOf(o.price)) + 0.5;
          hline(y, PC, [14, 4, 2, 4], 1.5);
          tags.push({ y: y, text: price(o.price, d), bg: PC, fg: PF, w: 700 });
          var more = 'pending \u00b7 ' + (o.stop_loss != null ? 'SL ' + price(o.stop_loss, d) : 'no SL') + ' \u00b7 ' + (o.take_profit != null ? 'TP ' + price(o.take_profit, d) : 'no TP') +
            ' \u00b7 #' + o.ticket;
          cards.push({ y: y, border: PC, alpha: 1, rows: [[{ t: head, c: PC, w: 700 }], [{ t: more, c: C.axis, w: 600 }]] });
          drawn.push({ ticket: o.ticket, order_type: o.order_type, direction: o.direction, price: o.price, y: y, offscreen: false, label: head });
        });
        lastPendFrame = drawn.length ? { symbol: state.selected, orders: drawn, range: { hi: lastRange.hi, lo: lastRange.lo },
                                         plot: { l: plotL, r: plotR, t: plotT, b: plotB }, theme: chartTheme(), color: PC } : null;
      })();

      // Lay the cards out: above their line by default, below if there is no room, never on top of each other.
      (function drawCards() {
        if (!cards.length) return;
        var padX = 9, padY = 5, gap = 7, maxW = plotW - 16, placed = [];
        cards.forEach(function (cd) {
          var w = 0;
          cd.rows.forEach(function (row) {
            var rw = 0;
            row.forEach(function (pt) { ctx.font = pt.w + ' ' + fs + 'px ' + cssVar('--mono'); pt.width = ctx.measureText(pt.t).width; rw += pt.width; });
            w = Math.max(w, rw);
          });
          cd.w = Math.min(maxW, Math.ceil(w) + padX * 2); cd.h = cd.rows.length * rowH + padY * 2; cd.x = plotL + 10;
        });
        function blocker(top, h) {
          for (var k = 0; k < placed.length; k++) if (top < placed[k].top + placed[k].h + 4 && placed[k].top < top + h + 4) return placed[k];
          return null;
        }
        cards.sort(function (a, b) { return a.y - b.y; });
        cards.forEach(function (cd) {
          var top = cd.y - gap - cd.h, b, tries = 0;
          if (top < plotT + 2 || blocker(top, cd.h)) top = cd.y + gap;
          while ((b = blocker(top, cd.h)) && tries++ < 8) top = b.top + b.h + 4;
          cd.top = Math.max(plotT + 2, Math.min(plotB - cd.h - 2, top));
          placed.push(cd);
        });
        ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
        cards.forEach(function (cd) {
          ctx.save(); ctx.globalAlpha = cd.alpha;
          ctx.beginPath(); ctx.roundRect(cd.x, cd.top, cd.w, cd.h, 10); ctx.fillStyle = C.pillBg; ctx.fill();
          ctx.strokeStyle = cd.border; ctx.lineWidth = 1; ctx.stroke();
          ctx.beginPath(); ctx.rect(cd.x, cd.top, cd.w, cd.h); ctx.clip();
          cd.rows.forEach(function (row, ri) {
            var x = cd.x + padX, ty = cd.top + padY + rowH * ri + rowH / 2 + 0.5;
            row.forEach(function (pt) { ctx.font = pt.w + ' ' + fs + 'px ' + cssVar('--mono'); ctx.fillStyle = pt.c; ctx.fillText(pt.t, x, ty); x += pt.width; });
          });
          ctx.restore();
        });
      })();
      // keep the "apply at broker" chip away from the line being moved
      var wantPos = pendingY != null && pendingY > plotB - 120 ? 'top' : 'bottom';
      if (modBar.getAttribute('data-pos') !== wantPos) modBar.setAttribute('data-pos', wantPos);

      // ---- crosshair (exact price, exact time, the candle under it) ----
      var hi = -1;
      if (hover && !drag && hover.x >= plotL && hover.x <= plotR && hover.y >= plotT && hover.y <= plotB) {
        hi = Math.max(0, Math.min(n - 1, indexAt(hover.x, ir)));
        var cx = Math.round(xOf(hi, ir)) + 0.5, cy = Math.round(hover.y) + 0.5;
        ctx.save();
        ctx.fillStyle = C.crossBand; ctx.fillRect(cx - ir.s / 2, plotT, ir.s, plotB - plotT);
        ctx.strokeStyle = C.cross; ctx.lineWidth = 1; ctx.setLineDash([5, 4]);
        ctx.beginPath(); ctx.moveTo(cx, plotT); ctx.lineTo(cx, plotB); ctx.moveTo(plotL, cy); ctx.lineTo(plotR, cy); ctx.stroke();
        ctx.restore(); ctx.setLineDash([]);
      }

      // ---- axis tags: resolve overlaps, then draw; grid labels skip anything near a tag ----
      var th = small ? 22 : 24;
      tags.sort(function (a, b) { return a.y - b.y; });
      for (var k = 1; k < tags.length; k++) if (tags[k].y - tags[k - 1].y < th) tags[k].y = tags[k - 1].y + th;
      var overflow = tags.length ? tags[tags.length - 1].y - (plotB - th / 2) : 0;
      if (overflow > 0) for (var k2 = tags.length - 1; k2 >= 0; k2--) { tags[k2].y -= overflow; if (k2 && tags[k2].y - tags[k2 - 1].y > th) overflow = 0; }
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.font = fAxis;
      gridVals.forEach(function (gv) {
        var gy = yOf(gv);
        if (gy < plotT + 4 || gy > plotB - 4) return;
        for (var q = 0; q < tags.length; q++) if (Math.abs(tags[q].y - gy) < th * 0.62) return;
        ctx.fillStyle = C.axis; ctx.fillText(price(gv, d), plotR + 12, gy);
      });
      function axisTag(y, text, bg, fg, weight, wide) {
        ctx.font = weight + ' ' + (small ? 12 : 13) + 'px ' + cssVar('--mono');
        var w = wide ? Math.min(ctx.measureText(text).width + 16, pad.r + 70) : pad.r - 4;
        var x = plotR + 2;
        ctx.fillStyle = bg; ctx.beginPath(); ctx.roundRect(x, y - th / 2 + 1, w, th - 2, 8); ctx.fill();
        ctx.fillStyle = fg; ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText(text, x + 8, y + 0.5);
      }
      tags.forEach(function (t) { axisTag(t.y, t.text, t.bg, t.fg, t.w, t.wide); });

      if (hi >= 0) {   // crosshair tags go on top of everything
        var hy = Math.max(plotT, Math.min(plotB, hover.y));
        axisTag(hy, price(vOf(hy), d), C.crossTag, C.crossTagFg, 700, false);
        var label = fullTime(state.candles[hi][0]);
        ctx.font = '600 ' + (small ? 12 : 13) + 'px ' + cssVar('--mono');
        var lw = ctx.measureText(label).width + 18, lx2 = Math.max(plotL, Math.min(plotR - lw, xOf(hi, ir) - lw / 2));
        ctx.fillStyle = C.crossTag; ctx.beginPath(); ctx.roundRect(lx2, plotB + 3, lw, H - plotB - 6, 8); ctx.fill();
        ctx.fillStyle = C.crossTagFg; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(label, lx2 + lw / 2, plotB + (H - plotB) / 2);
      }

      // ---- "AUTO" button: shown only while the price scale is manual; click/tap to fit the candles again ----
      autoBtn = null;
      if (view.yRange) {
        var abw = Math.min(pad.r - 8, 56), abh = Math.max(18, H - plotB - 8);
        autoBtn = { x: W - abw - 4, y: plotB + (H - plotB - abh) / 2, w: abw, h: abh };
        ctx.fillStyle = C.crossTag; ctx.beginPath(); ctx.roundRect(autoBtn.x, autoBtn.y, abw, abh, 8); ctx.fill();
        ctx.fillStyle = C.crossTagFg; ctx.font = '700 ' + (small ? 11 : 12) + 'px ' + cssVar('--mono');
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText('AUTO', autoBtn.x + abw / 2, autoBtn.y + abh / 2 + 0.5);
      }

      // ---- HUD: symbol, price, change, OHLC of the hovered (or newest) candle, spread ----
      var hc = state.candles[hi >= 0 ? hi : n - 1];
      var bidNow = hasQuote ? lq.bid : hc[4];     // no live quote: the last candle close, flagged below
      inRole(root, 'hud-price').textContent = price(bidNow, d);
      inRole(root, 'hud-price').title = hasQuote ? 'Live bid' : 'Last candle close. There is no live quote: ' + lq.reason;
      var chg = hc[4] - hc[1], pct = hc[1] ? chg / hc[1] * 100 : 0, chgEl = inRole(root, 'hud-chg');
      chgEl.textContent = (chg >= 0 ? '+' : '\u2212') + Math.abs(chg).toFixed(d) + '  (' + (pct >= 0 ? '+' : '\u2212') + Math.abs(pct).toFixed(2) + '%)';
      chgEl.className = 'hud-chg ' + (chg >= 0 ? 'pl-pos' : 'pl-neg');
      var ohlcKey = [hc[1], hc[2], hc[3], hc[4], hasQuote ? lq.ask : '', hasQuote ? lq.spreadPoints : '', d, small].join('|');
      setHud(['hud-ohlc', ohlcKey], function (node) {
        var parts = [['O', hc[1]], ['H', hc[2]], ['L', hc[3]], ['C', hc[4]]], nodes = [];
        if (small) parts.shift();
        parts.forEach(function (pr) {
          var span = document.createElement('span'), lab = document.createElement('i');
          lab.textContent = pr[0]; span.appendChild(lab); span.appendChild(document.createTextNode(price(pr[1], d)));
          nodes.push(span);
        });
        if (hasQuote && !small) {
          var sp = document.createElement('span'), l2 = document.createElement('i');
          l2.textContent = 'ASK'; l2.style.color = C.ask; sp.appendChild(l2); sp.appendChild(document.createTextNode(price(lq.ask, d)));
          nodes.push(sp);
          var sp2 = document.createElement('span'), l3 = document.createElement('i');
          l3.textContent = 'SPR'; sp2.appendChild(l3);
          sp2.appendChild(document.createTextNode(lq.spreadPoints == null ? '---' : String(lq.spreadPoints)));
          nodes.push(sp2);
        }
        node.replaceChildren.apply(node, nodes);
      });
    };

    this.syncSymbol = function () {
      var sel = inRole(root, 'symbol-select');
      var all = allSymbols().filter(function (x) { return x.name === state.selected || watchList().indexOf(x.name) >= 0 || !!symbolByNameSnap(x.name); });
      var key = all.map(function (s) { return s.name; }).join('|');
      if (sel._key !== key) {
        var opts = all.map(function (s) { return new Option(s.name, s.name); });
        sel.replaceChildren.apply(sel, opts.length ? opts : [new Option('---', '')]);
        sel._key = key;
      }
      if (state.selected && sel.value !== state.selected) sel.value = state.selected;
      var sym = symbolByName(state.selected);
      var d = digitsOf(sym);
      if (sym && sym.volume_min != null) { lotInput.min = sym.volume_min; if (sym.volume_step) lotInput.step = sym.volume_step; }
      // tf buttons
      root.querySelectorAll('[data-tf]').forEach(function (b) {
        b.setAttribute('aria-pressed', b.getAttribute('data-tf') === state.tf ? 'true' : 'false');
      });
      // + SL / + TP are offered only when the selected symbol has an open position missing that level
      var missing = symPositions().filter(function (p) { return p.stop_loss == null || p.take_profit == null; })[0];
      inRole(root, 'pos-add').hidden = !missing;
      if (missing) {
        inRole(root, 'add-sl').hidden = missing.stop_loss != null;
        inRole(root, 'add-tp').hidden = missing.take_profit != null;
      }
      // drop a pending change whose position vanished (closed elsewhere, e.g. MT5 mobile)
      if (pending && (!posByTicket(pending.ticket) || pending.symbol !== state.selected)) self.cancelPending(true);
    };

    this.clearDraft = function () { slInput.value = ''; tpInput.value = ''; self.draw(); };

    /* ----- proposing / applying a SL/TP change ----- */
    function showPending() {
      var p = posByTicket(pending.ticket), sym = symbolByName(p.symbol), d = digitsOf(sym);
      var from = pending.field === 'sl' ? p.stop_loss : p.take_profit;
      inRole(root, 'mod-text').textContent = pending.field.toUpperCase() + ' \u00b7 ' + (from == null ? 'none' : price(from, d)) +
        ' \u2192 ' + price(pending.price, d);
      modBar.title = 'Move ' + pending.field.toUpperCase() + ' of ' + p.symbol + ' ' + p.direction.toUpperCase() + ' #' + p.ticket;
      modBar.hidden = false;
      inRole(root, 'mod-apply').disabled = sending;
    }
    this.cancelPending = function (silent) {
      pending = null; modBar.hidden = true; sending = false;
      if (!silent) self.draw();
    };
    this.propose = function (ticket, field, priceValue) {
      var p = posByTicket(ticket), sym = symbolByName(p.symbol);
      var d = digitsOf(sym);
      pending = { ticket: ticket, field: field, price: Number(priceValue.toFixed(d)), symbol: p.symbol };
      showPending(); self.draw();
    };

    inRole(root, 'mod-cancel').addEventListener('click', function () { self.cancelPending(); });
    inRole(root, 'mod-apply').addEventListener('click', function () {
      if (!pending || sending) return;
      var req = pending, body = {};
      body[req.field] = req.price;
      sending = true; inRole(root, 'mod-apply').disabled = true;
      runWrite('MODIFY ' + req.field.toUpperCase() + ' #' + req.ticket + ' \u2192 ' + req.price, '/api/position/' + req.ticket + '/modify', body)
        .then(function () {
          // Success or failure, drop the proposal: the chart now shows exactly what the broker holds.
          self.cancelPending();
        });
    });

    function defaultDistance(p) {
      var sym = symbolByName(p.symbol);
      var point = sym && sym.point ? sym.point : 0.00001;
      var pts = Math.max(100, ((sym && sym.stops_level) || 0) * 2 + ((sym && sym.spread_points) || 0) * 2);
      return pts * point;
    }
    function addLevel(field) {
      var p = symPositions().filter(function (x) { return (field === 'sl' ? x.stop_loss : x.take_profit) == null; })[0];
      if (!p) return;
      var dist = defaultDistance(p), ref = p.price_current != null ? p.price_current : p.price_open;
      var above = (field === 'tp') === (p.direction === 'buy');
      self.propose(p.ticket, field, above ? ref + dist : ref - dist);
    }
    inRole(root, 'add-sl').addEventListener('click', function () { addLevel('sl'); });
    inRole(root, 'add-tp').addEventListener('click', function () { addLevel('tp'); });

    /* ----- pointer interaction -----
       drag an SL/TP line (or its handle)  -> change that level
       drag the price axis (right edge)    -> stretch / compress the candles (vertical scale)
       drag anywhere else                  -> pan the chart, sideways and up/down (nothing is clamped vertically)
       pinch with two fingers              -> sideways = time zoom, up/down = vertical scale
       AUTO button / double-click the axis -> fit the candles to the screen again */
    function pointerPos(e) { var r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
    function hitTest(y, touch) {
      if (!lastRange) return null;
      var best = null, bestDist = touch ? 24 : 12;        // fingers need a bigger target than a mouse
      lines().forEach(function (ln) {
        if (!ln.drag || ln.price == null || ln.price < lastRange.lo || ln.price > lastRange.hi) return;
        var dist = Math.abs(yOf(ln.price) - y);
        if (dist <= bestDist) { best = ln; bestDist = dist; }
      });
      return best;
    }
    function inAxis(pos) { return !!lastRange && pos.x > W - pad.r; }
    function onAutoBtn(pos) { return !!autoBtn && pos.x >= autoBtn.x - 6 && pos.x <= autoBtn.x + autoBtn.w + 6 && pos.y >= autoBtn.y - 6 && pos.y <= autoBtn.y + autoBtn.h + 6; }
    function copyRange() { return { lo: lastRange.lo, hi: lastRange.hi }; }

    var pointers = {}, pinch = null, lastTap = null;       // active touches, for two-finger pinch
    function pinchPts() { var k = Object.keys(pointers); return [pointers[k[0]], pointers[k[1]]]; }
    function pinchDist() { var q = pinchPts(); return Math.hypot(q[0].x - q[1].x, q[0].y - q[1].y) || 1; }
    function pinchDy() { var q = pinchPts(); return Math.max(1, Math.abs(q[0].y - q[1].y)); }

    canvas.addEventListener('pointerdown', function (e) {
      if (e.button !== undefined && e.button > 0) return;
      var pos = pointerPos(e), touch = e.pointerType === 'touch';
      pointers[e.pointerId] = pos;
      if (Object.keys(pointers).length === 2 && state.candles.length && lastRange) {
        var q = pinchPts();
        pinch = { d0: pinchDist(), s0: spacing(), dy0: pinchDy(), range0: copyRange(),
                  vertical: Math.abs(q[0].y - q[1].y) > Math.abs(q[0].x - q[1].x) * 1.7 };
        pan = null; drag = null; axisDrag = null; hover = null;
        canvas.setPointerCapture(e.pointerId); e.preventDefault(); return;
      }
      if (!state.candles.length || !lastRange) return;
      if (manualDown(e, pos)) { canvas.setPointerCapture(e.pointerId); self.draw(); redrawAll(); e.preventDefault(); return; }
      if (onAutoBtn(pos)) { self.fitPrice(); e.preventDefault(); return; }
      if (inAxis(pos)) {                                      // the price axis scales the candles vertically
        axisDrag = { y0: e.clientY, range0: copyRange(), moved: false, touch: touch };
        canvas.style.cursor = 'ns-resize'; hover = null;
      } else {
        var ln = hitTest(pos.y, touch);
        if (ln) {
          // A line click selects it; the line itself stays visible. For draggable
          // SL/TP lines we preserve the existing drag-to-modify behavior.
          selectedLineId = ln.id;
          if (!sending) {
            if (ln.kind === 'pending') drag = { kind: 'pending' };
            else if (ln.kind === 'draft') drag = { kind: 'draft', field: ln.field };
            else drag = { kind: 'new', ticket: ln.pos.ticket, field: ln.kind };
            drag.frozen = { lo: lastRange.lo, hi: lastRange.hi };
          }
          self.draw();
        } else {                                              // otherwise: pan the chart and clear contextual info
          selectedLineId = null;
          pan = { x0: e.clientX, y0: e.clientY, back0: view.back, range0: copyRange(), moved: false, vertical: false, touch: touch };
          canvas.style.cursor = 'grabbing';
        }
      }
      canvas.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    canvas.addEventListener('pointermove', function (e) {
      var pos = pointerPos(e);
      if (pointers[e.pointerId]) pointers[e.pointerId] = pos;
      if (pinch && Object.keys(pointers).length >= 2) {
        if (pinch.vertical) scaleRange(pinch.range0, pinch.dy0 / pinchDy());          // fingers apart = taller candles
        else { view.spacing = Math.max(3, Math.min(40, pinch.s0 * pinchDist() / pinch.d0)); clampBack(); }
        self.draw(); return;
      }
      if (manualMove(e, pos)) return;
      if (axisDrag) {
        var dyA = e.clientY - axisDrag.y0;
        if (Math.abs(dyA) > 2) axisDrag.moved = true;
        scaleRange(axisDrag.range0, Math.exp(dyA * 0.005));                              // drag up = taller, down = shorter
        self.draw(); return;
      }
      if (pan) {
        var dx = e.clientX - pan.x0, dy = e.clientY - pan.y0;
        if (Math.abs(dx) > 3 || Math.abs(dy) > 3) pan.moved = true;
        view.back = pan.back0 + dx / spacing(); clampBack();
        // Up/down movement only takes over once the drag is clearly not just a sideways pan, so an ordinary
        // horizontal scroll never leaves the price auto-fit by accident.
        if (!pan.vertical && Math.abs(dy) > 8 && Math.abs(dy) >= Math.abs(dx) * 0.5) pan.vertical = true;
        if (pan.vertical) {
          var shift = dy / plotHeight() * (pan.range0.hi - pan.range0.lo);
          view.yRange = { lo: pan.range0.lo + shift, hi: pan.range0.hi + shift };
        }
        if (!pan.touch) hover = pos;
        self.draw();
        return;
      }
      if (!drag) {
        if (e.pointerType !== 'touch') {
          hover = pos;
          canvas.style.cursor = onAutoBtn(pos) ? 'pointer' : (inAxis(pos) || hitTest(pos.y, false)) ? 'ns-resize' : 'crosshair';
          self.draw();
        }
        return;
      }
      var sym = symbolByName(state.selected), d = digitsOf(sym);
      var v = Number(vOf(Math.max(pad.t, Math.min(H - pad.b, pos.y))).toFixed(d));
      if (drag.kind === 'draft') {
        (drag.field === 'sl' ? slInput : tpInput).value = v.toFixed(d);
        if (drag.field === 'sl') slInput.dispatchEvent(new Event('input'));     // lets the stop-loss panel follow a chart drag
      } else {
        if (drag.kind === 'new') { self.propose(drag.ticket, drag.field, v); drag.kind = 'pending'; }
        else { pending.price = v; showPending(); }
      }
      self.draw();
    });

    function endDrag(e) {
      if (manualUp(e)) return;
      delete pointers[e.pointerId];
      if (pinch) { if (Object.keys(pointers).length < 2) pinch = null; try { canvas.releasePointerCapture(e.pointerId); } catch (err) { /* released */ } return; }
      if (!drag && !pan && !axisDrag) return;
      var tap = (pan && !pan.moved && pan.touch) || (axisDrag && !axisDrag.moved && axisDrag.touch);
      var wasAxis = !!axisDrag, pos = pointerPos(e);
      if (tap) {                                              // touch: double-tap resets, single tap toggles the crosshair
        var now = Date.now();
        if (lastTap && now - lastTap.t < 320 && Math.hypot(pos.x - lastTap.x, pos.y - lastTap.y) < 30) {
          if (wasAxis) self.fitPrice(); else self.resetView();
          hover = null; lastTap = null;
        } else {
          lastTap = { t: now, x: pos.x, y: pos.y };
          if (!wasAxis) hover = hover ? null : pos;
        }
      }
      drag = null; pan = null; axisDrag = null;
      canvas.style.cursor = e.pointerType === 'touch' ? '' : 'crosshair';
      try { canvas.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
      self.draw();  // un-freeze the price scale
    }
    canvas.addEventListener('pointerup', endDrag);
    canvas.addEventListener('pointercancel', endDrag);
    canvas.addEventListener('pointerleave', function (e) { if (e.pointerType !== 'touch' && !drag && !pan && !axisDrag) { hover = null; self.draw(); } });
    canvas.addEventListener('dblclick', function (e) { if (inAxis(pointerPos(e))) self.fitPrice(); else self.resetView(); });
    canvas.addEventListener('wheel', function (e) {
      var workspace = root.classList.contains('terminal-tall') || root.classList.contains('is-max');
      if (!(e.ctrlKey || e.metaKey || e.altKey || workspace)) return;      // on the dashboard plain scrolling still scrolls the page
      if (e.altKey || inAxis(pointerPos(e))) self.scalePrice(e.deltaY < 0 ? 1 / 1.1 : 1.1);   // wheel up = taller candles
      else if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) { view.back -= e.deltaX / spacing(); clampBack(); self.draw(); }
      else self.zoom(e.deltaY < 0 ? 1.12 : 1 / 1.12);
      e.preventDefault();
    }, { passive: false });

    slInput.addEventListener('input', function () { self.draw(); });
    tpInput.addEventListener('input', function () { self.draw(); });

    /* ----- controls ----- */
    inRole(root, 'symbol-select').addEventListener('change', function (e) {
      var name = e.target.value;
      if (name && state.watch && state.watch.indexOf(name) < 0) {
        state.watch.push(name); saveLocal('trademate.watchlist', state.watch); syncWatch();
      }
      selectedLineId = null;
      setSelected(name);
    });
    root.querySelectorAll('[data-tf]').forEach(function (b) {
      b.addEventListener('click', function () {
        state.tf = b.getAttribute('data-tf'); saveLocal('trademate.tf', state.tf);
        state.candles = []; state.candleKey = ''; state.candleMsg = 'Loading ' + state.tf + ' candles...';
        terminals.forEach(function (t) { t.syncSymbol(); t.draw(); });
        loadCandles(true);
      });
    });
    root.querySelectorAll('[data-zoom]').forEach(function (b) {
      b.addEventListener('click', function () {
        var kind = b.getAttribute('data-zoom');
        if (kind === 'reset') self.resetView(); else self.zoom(kind === 'in' ? 1.25 : 0.8);
      });
    });
    root.querySelectorAll('[data-ctype]').forEach(function (b) {
      b.addEventListener('click', function () {
        state.ctype = b.getAttribute('data-ctype'); saveLocal('trademate.chartType', state.ctype);
        terminals.forEach(function (t) { t.draw(); });
      });
    });
    inRole(root, 'theme-btn').addEventListener('click', function () { toggleChartTheme(); });   // the chart's own theme: the top-bar switch and the application theme are untouched
    var structBtn = inRole(root, 'structure-btn');
    if (structBtn) structBtn.addEventListener('click', function () { setStructureVisible(!state.structureVisible); });
    var liqBtn = inRole(root, 'liquidity-btn'); if (liqBtn) liqBtn.addEventListener('click', function () { setLiquidityVisible(!state.liquidityVisible); });
    var fvgBtn = inRole(root, 'fvg-btn'); if (fvgBtn) fvgBtn.addEventListener('click', function () { setFvgVisible(!state.fvgVisible); });
    var obBtn = inRole(root, 'ob-btn'); if (obBtn) obBtn.addEventListener('click', function () { setObVisible(!state.obVisible); });
    /* one click: hide every analysis overlay if any is shown, otherwise show them all (the three single buttons stay independent) */
    var allBtn = inRole(root, 'overlays-btn');
    if (allBtn) allBtn.addEventListener('click', function () { setAllOverlays(!anyOverlayVisible()); });

    /* trade panel show/hide */
    var panelBtn = inRole(root, 'panel-btn');
    function applyPanel() {
      var hidden = layoutPrefs.panelHidden === true;
      root.classList.toggle('panel-hidden', hidden);
      panelBtn.setAttribute('aria-pressed', hidden ? 'false' : 'true');
    }
    panelBtn.addEventListener('click', function () {
      layoutPrefs.panelHidden = !(layoutPrefs.panelHidden === true); saveLayout();
      terminals.forEach(function (t) { t.applyPanel(); });
    });
    this.applyPanel = applyPanel;

    /* maximize: the chart takes the whole window (Esc or the button returns) */
    var maxBtn = inRole(root, 'max-btn');
    this.isMax = function () { return root.classList.contains('is-max'); };
    this.setMax = function (on) {
      if (on) terminals.forEach(function (t) { if (t !== self && t.isMax()) t.setMax(false); });
      root.classList.toggle('is-max', on);
      document.body.classList.toggle('chart-max', on);
      maxBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
      maxBtn.setAttribute('aria-label', on ? 'Exit maximized chart' : 'Maximize chart');
      maxBtn.title = on ? 'Exit maximized chart (Esc)' : 'Maximize chart (Esc to exit)';
      if (on) root.scrollIntoView({ block: 'nearest' });
      setTimeout(resize, 0);
    };
    maxBtn.addEventListener('click', function () { self.setMax(!self.isMax()); });

    /* line tool, delete, moving-average popover */
    var lineBtn = inRole(root, 'line-btn'), delBtn = inRole(root, 'line-del'), maBtn = inRole(root, 'ma-btn'), maPop = inRole(root, 'ma-pop');
    if (lineBtn) lineBtn.addEventListener('click', function () { MAN.tool = !MAN.tool; redrawAll(); });
    if (delBtn) delBtn.addEventListener('click', function () { manDelete(); });
    if (maBtn && maPop) {
      maBtn.addEventListener('click', function () { var open = maPop.hidden; maPop.hidden = !open; maBtn.setAttribute('aria-expanded', open ? 'true' : 'false'); self.syncExtras(); });
      inRole(root, 'ma-enabled').addEventListener('change', function (e) { maSave({ enabled: e.target.checked }); });
      inRole(root, 'ma-type').addEventListener('change', function (e) { maSave({ type: e.target.value }); });
      inRole(root, 'ma-tf').addEventListener('change', function (e) { maSave({ timeframe: e.target.value }); });
      inRole(root, 'ma-period').addEventListener('change', function (e) { var v = parseInt(e.target.value, 10); if (isFinite(v)) maSave({ period: v }); else self.syncExtras(); });
    }

    /* drag-to-resize: height (bottom handle) and width (handle between chart and trade panel) */
    var chartPanel = root.querySelector('.chart-panel'), tall = root.classList.contains('terminal-tall');
    var hKey = tall ? 'h_page' : 'h_dash';
    function applyLayout() {
      if (layoutPrefs[hKey]) root.style.setProperty('--chart-h', layoutPrefs[hKey] + 'px'); else root.style.removeProperty('--chart-h');
      if (layoutPrefs.panelW) root.style.setProperty('--panel-w', layoutPrefs.panelW + 'px'); else root.style.removeProperty('--panel-w');
    }
    this.applyLayout = applyLayout;
    function startResize(handle, e, onMove, onEnd) {
      if (e.button !== undefined && e.button > 0) return;
      handle.setPointerCapture(e.pointerId); handle.classList.add('is-dragging'); document.body.classList.add('is-resizing');
      function move(ev) { onMove(ev); }
      function up(ev) {
        handle.classList.remove('is-dragging'); document.body.classList.remove('is-resizing');
        handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); handle.removeEventListener('pointercancel', up);
        try { handle.releasePointerCapture(ev.pointerId); } catch (err) { /* released */ }
        onEnd(); saveLayout();
      }
      handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up); handle.addEventListener('pointercancel', up);
      e.preventDefault();
    }
    var ry = inRole(root, 'resize-y'), rx = inRole(root, 'resize-x');
    ry.addEventListener('pointerdown', function (e) {
      var y0 = e.clientY, h0 = chartPanel.getBoundingClientRect().height;
      startResize(ry, e, function (ev) {
        var h = Math.round(Math.max(340, Math.min(Math.max(520, window.innerHeight * 1.6), h0 + ev.clientY - y0)));
        layoutPrefs[hKey] = h; root.style.setProperty('--chart-h', h + 'px');
      }, function () {});
    });
    ry.addEventListener('dblclick', function () { delete layoutPrefs[hKey]; saveLayout(); applyLayout(); });
    rx.addEventListener('pointerdown', function (e) {
      var x0 = e.clientX, w0 = root.querySelector('.trade-panel').getBoundingClientRect().width;
      startResize(rx, e, function (ev) {
        var w = Math.round(Math.max(260, Math.min(Math.min(560, root.clientWidth * 0.5), w0 - (ev.clientX - x0))));
        layoutPrefs.panelW = w; terminals.forEach(function (t) { t.applyLayout(); });
      }, function () {});
    });
    rx.addEventListener('dblclick', function () { delete layoutPrefs.panelW; saveLayout(); terminals.forEach(function (t) { t.applyLayout(); }); });
    applyLayout(); applyPanel(); self.applyTheme();

    if (window.TradeMateSL) {                                  // two-way stop-loss panel (preview only)
      root._slCtl = TradeMateSL.attach(root, { getSymbol: function () { return state.selected; },
        getEntry: function () { var t = pendingTypeOf(root); return t ? inRole(root, 'pend-price').value : null; },
        getDirection: function () { var t = pendingTypeOf(root); return t ? (t.indexOf('buy') === 0 ? 'buy' : 'sell') : null; },
        getLot: function () { return inRole(root, 'lot').value; }, post: postJSON });
      if (root._slCtl) slControllers.push(root._slCtl);
    }
    var typeSel = inRole(root, 'order-type');
    if (typeSel) {
      var onType = function () {
        var pending = pendingTypeOf(root);
        inRole(root, 'pend-fields').hidden = !pending;
        if (root._slCtl) root._slCtl.refresh();                  // the stop-loss calculator follows the pending entry and side
      };
      typeSel.addEventListener('change', onType);
      inRole(root, 'pend-price').addEventListener('input', function () { if (root._slCtl) root._slCtl.refresh(); });
      inRole(root, 'pend-place').addEventListener('click', function () { placePending(root); });
    }
    inRole(root, 'panel-buy').addEventListener('click', function () { placeOrder('buy', root); });
    inRole(root, 'panel-sell').addEventListener('click', function () { placeOrder('sell', root); });

    resize();
  }

  if (!CanvasRenderingContext2D.prototype.roundRect) {      // older browsers
    CanvasRenderingContext2D.prototype.roundRect = function (x, y, w, h, r) {
      r = Math.min(r || 0, w / 2, h / 2);
      this.moveTo(x + r, y); this.arcTo(x + w, y, x + w, y + h, r); this.arcTo(x + w, y + h, x, y + h, r);
      this.arcTo(x, y + h, x, y, r); this.arcTo(x, y, x + w, y, r); this.closePath();
    };
  }
  document.querySelectorAll('[data-terminal]').forEach(function (root) { terminals.push(new Terminal(root)); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') terminals.forEach(function (t) { if (t.isMax()) t.setMax(false); });
  });
  Router.on(function () { terminals.forEach(function (t) { if (t.isMax()) t.setMax(false); }); });
  // keep lot size in step across the two chart panels
  document.addEventListener('input', function (e) {
    if (e.target && e.target.getAttribute && e.target.getAttribute('data-role') === 'lot') {
      roleAll('lot').forEach(function (el) { if (el !== e.target) el.value = e.target.value; });
      slControllers.forEach(function (c) { c.refresh(); });
    }
  });

  /* ---------- small public API for the Analysis page (reads selection, opens a chart; changes nothing else) ---------- */
  window.TradeMateApp = {
    selection: function () { return { symbol: state.selected, tf: state.tf }; },
    symbols: function () { return watchList().concat(state.symbols.map(function (x) { return x.name; })).filter(function (n, i, a) { return a.indexOf(n) === i; }); },
    structureVisible: function () { return !!state.structureVisible; },
    liquidityVisible: function () { return !!state.liquidityVisible; },
    fvgVisible: function () { return !!state.fvgVisible; },
    fvgDrawn: function () { return state.fvg && state.fvg.items ? { gaps: state.fvg.items.gaps.length, key: state.fvg.key } : null; },
    fvgFrame: function () { return terminals.map(function (t) { return t.fvgFrame(); }); },
    pendingFrame: function () { return terminals.map(function (t) { return t.pendingFrame(); }); },
    pendingNotice: function () { return terminals.map(function (t) { return t.pendingNotice(); }); },
    zoneNotices: function () { return terminals.map(function (t) { return t.zoneNotices(); }); },
    candleStyle: function () { return { preset: state.candleStyle.preset, bull: state.candleStyle.bull, bear: state.candleStyle.bear }; },
    obVisible: function () { return !!state.obVisible; },
    obDrawn: function () { return state.ob && state.ob.items ? { blocks: state.ob.items.blocks.length, key: state.ob.key } : null; },
    obFrame: function () { return terminals.map(function (t) { return t.obFrame(); }); },
    overlays: function () { return { structure: !!state.structureVisible, liquidity: !!state.liquidityVisible, fvg: !!state.fvgVisible, order_blocks: !!state.obVisible }; },
    /* the analysis results currently held for the candles on screen, whether or not each overlay is shown (hiding never discards them) */
    analysisData: function () {
      return { candleKey: state.candleKey,
               structure: state.structure && state.structure.items ? { key: state.structure.key, events: state.structure.items.events.length, swings: state.structure.items.swings.length } : null,
               liquidity: state.liquidity && state.liquidity.items ? { key: state.liquidity.key, pools: state.liquidity.items.pools.length, sweeps: state.liquidity.items.sweeps.length } : null,
               fvg: state.fvg && state.fvg.data ? { key: state.fvg.key, available: state.fvg.data.available === true, gaps: (state.fvg.data.gaps || []).length } : null,
               order_blocks: state.ob && state.ob.data ? { key: state.ob.key, available: state.ob.data.available === true, blocks: (state.ob.data.blocks || []).length } : null };
    },
    liquidityDrawn: function () { return state.liquidity && state.liquidity.items ? { pools: state.liquidity.items.pools.length, sweeps: state.liquidity.items.sweeps.length, key: state.liquidity.key } : null; },
    /* what each chart last DREW for the quote lines (for tests and diagnostics): state, bid/ask price + y + colour, spread */
    rfFrame: function () { return terminals.map(function (t) { return t.rfFrame(); }); },
    chartQuotes: function () { return terminals.map(function (t) { return t.quoteFrame(); }); },
    /* Open the chart for symbol/tf. `eventId` highlights one structure event (a distinct ring, never a trade line).
       The overlay is only switched on when the user asked for it (`showStructure`): the saved visibility setting is respected. */
    openChart: function (symbol, tf, opts) {
      opts = opts || {};
      if (tf && tf !== state.tf) {
        state.tf = tf; saveLocal('trademate.tf', tf);
        state.candles = []; state.candleKey = ''; state.candleMsg = 'Loading ' + tf + ' candles...';
        terminals.forEach(function (t) { t.syncSymbol(); t.draw(); });
      }
      state.analysisFocus = opts.eventId && symbol ? { id: opts.eventId, symbol: symbol, tf: tf || state.tf } : null;
      if (symbol) setSelected(symbol);
      if (opts.showStructure) setStructureVisible(true);
      if (opts.showLiquidity) setLiquidityVisible(true);
      if (opts.showFvg) setFvgVisible(true);
      if (opts.showOrderBlocks) setObVisible(true);
      loadCandles(true);
      Router.go('chart');
    }
  };

  /* ---------- candles from MT5 ---------- */
  var candleBusy = false;
  function anyChartVisible() { return terminals.some(function (t) { return t.isVisible(); }); }
  function fvgItems(fvg, candles) {
    try { return window.TradeMateFvg ? window.TradeMateFvg.overlayItems(fvg, candles) : null; } catch (e) { return null; }
  }
  function obItems(ob, candles) {
    try { return window.TradeMateOrderBlocks ? window.TradeMateOrderBlocks.overlayItems(ob, candles) : null; } catch (e) { return null; }
  }
  function loadCandles(force) {
    if (candleBusy || !state.selected || !state.connected || document.hidden) return;
    if (!force && !anyChartVisible()) return;
    var key = state.selected + '|' + state.tf;
    candleBusy = true;
    fetch('/api/candles?symbol=' + encodeURIComponent(state.selected) + '&timeframe=' + state.tf + '&count=300&fvg=1&order_blocks=1&structure=1' + (state.liquidityVisible ? '&liquidity=1' : ''), { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (res) {
        if (key !== state.selected + '|' + state.tf) return;     // user switched while loading
        if (res.ok && res.candles.length) {
          state.candles = res.candles; state.candleKey = key; state.candlesAt = Date.now();
          // replaced as a whole on every refresh (never appended), so overlays cannot pile up or go stale
          state.structure = res.structure ? { key: key, data: res.structure, items: STYLE.overlayItems(res.structure, res.candles) } : null;
          state.liquidity = res.liquidity ? { key: key, data: res.liquidity, items: window.TradeMateLiquidity.overlayItems(res.liquidity, res.candles) } : null;
          state.fvg = res.fvg ? { key: key, data: res.fvg, items: fvgItems(res.fvg, res.candles) } : null;     // a bad block draws nothing, never breaks the chart
          state.ob = res.order_blocks ? { key: key, data: res.order_blocks, items: obItems(res.order_blocks, res.candles) } : null;
        } else { state.candleMsg = res.message || 'No candles available.'; }
        terminals.forEach(function (t) { t.draw(); });
      })
      .catch(function () { state.candleMsg = 'Cannot load candles from the TradeMate server.'; terminals.forEach(function (t) { t.draw(); }); })
      .then(function () { candleBusy = false; });
  }
  setInterval(function () { loadCandles(false); }, CANDLE_POLL_MS);

  /* The newest candle follows the live bid between candle refreshes. */
  function tickLastCandle() {
    var sym = symbolByName(state.selected);
    if (!sym || sym.bid == null || !state.candles.length || state.candleKey !== state.selected + '|' + state.tf) return;
    var c = state.candles[state.candles.length - 1];
    c[4] = sym.bid; c[2] = Math.max(c[2], sym.bid); c[3] = Math.min(c[3], sym.bid);
  }

  /* ---------- Chart appearance: candle colours + market-structure visibility (saved on the server) ---------- */
  var csPending = {}, csTimer = null, csBusy = false, csSaveFailed = false;
  function csStatus(text, bad) {
    var el = $('cs-status'); if (!el) return;
    el.textContent = text || ''; el.classList.toggle('is-bad', !!bad);
  }
  function setBg(el, color) { if (el) el.style.setProperty('background-color', color); }   // CSSOM: allowed by the page's CSP

  function renderChartSettings() {
    var cs = state.candleStyle, sel = $('cs-preset');
    if (!sel) return;
    var savedLegacyPreset = cs.preset !== 'custom' && !state.presets.some(function (p) { return p.id === cs.preset; });
    var presetSignature = state.presets.map(function (p) { return p.id; }).join('|') + '|saved:' + (savedLegacyPreset ? cs.preset : '');
    if (!sel.options.length || sel.getAttribute('data-n') !== presetSignature) {
      sel.textContent = '';
      var options = state.presets.slice();
      if (savedLegacyPreset) {
        options.push({ id: cs.preset, name: 'Saved colors' });
      }
      options.concat([{ id: 'custom', name: 'Custom' }]).forEach(function (p) {
        var o = document.createElement('option'); o.value = p.id; o.textContent = p.name; sel.appendChild(o);
      });
      sel.setAttribute('data-n', presetSignature);
      var box = $('cs-swatches'); box.textContent = '';
      state.presets.forEach(function (p) {
        var b = document.createElement('button'); b.type = 'button'; b.className = 'cs-swatch'; b.setAttribute('data-preset', p.id);
        var chips = document.createElement('span'); chips.className = 'cs-chips';
        [0, 1].forEach(function () { var chip = document.createElement('i'); chips.appendChild(chip); });
        var name = document.createElement('span'); name.className = 'cs-swatch-name'; name.textContent = p.name;
        b.appendChild(chips); b.appendChild(name);
        b.addEventListener('click', function () { choosePreset(p.id); });
        box.appendChild(b);
      });
    }
    sel.value = cs.preset;
    var current = state.presets.filter(function (p) { return p.id === cs.preset; })[0];
    setText('cs-preset-name', current ? current.name : (cs.preset === 'custom' ? 'Custom' : 'Saved colors'));
    state.presets.forEach(function (p) {
      var colors = p.theme_colors && p.theme_colors[chartTheme()] || p;
      var button = document.querySelector('.cs-swatch[data-preset="' + p.id + '"]');
      if (!button) return;
      button.setAttribute('aria-label', p.name + ': bullish ' + colors.bull_color + ', bearish ' + colors.bear_color);
      var chips = button.querySelectorAll('.cs-chips i');
      setBg(chips[0], colors.bull_color); setBg(chips[1], colors.bear_color);
    });
    document.querySelectorAll('.cs-swatch').forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-preset') === cs.preset ? 'true' : 'false'); });
    [['bull', cs.bull], ['bear', cs.bear]].forEach(function (pair) {
      var picker = $('cs-' + pair[0] + '-picker'), hex = $('cs-' + pair[0] + '-hex');
      if (picker.value.toUpperCase() !== pair[1]) picker.value = pair[1].toLowerCase();
      if (document.activeElement !== hex || STYLE.normalizeHex(hex.value) !== pair[1]) hex.value = pair[1];
      hex.removeAttribute('aria-invalid');
      setBg($('cs-' + pair[0] + '-chip'), pair[1]);
    });
    var warn = STYLE.contrastWarning(cs.bull, cs.bear, chartTheme()), w = $('cs-warning');
    w.textContent = warn; w.hidden = !warn;
    var box2 = $('cs-structure'); if (box2) box2.checked = state.structureVisible;
    var lv = $('cs-liquidity'); if (lv) lv.checked = state.liquidityVisible;
    var fvc = $('cs-fvg'); if (fvc) fvc.checked = state.fvgVisible;
    var obc = $('cs-ob'); if (obc) obc.checked = state.obVisible;
    var cth = $('cs-chart-theme'); if (cth) cth.value = state.chartThemeSetting;
    var lt = $('cs-liq-ticks'); if (lt && document.activeElement !== lt) lt.value = state.liquiditySettings.tolerance_ticks;
    var lm = $('cs-liq-mode'); if (lm) lm.value = state.liquiditySettings.sweep_mode;
    var lx = $('cs-liq-max'); if (lx && document.activeElement !== lx) lx.value = state.liquiditySettings.max_pools;
    var prev = $('cs-preview');
    if (prev) { prev.setAttribute('data-theme', chartTheme()); setBg($('cs-prev-bull'), cs.bull); setBg($('cs-prev-bear'), cs.bear); }
  }
  function redrawCharts() { terminals.forEach(function (t) { t.draw(); }); }

  function syncPresetColorsToChartTheme(record) {
    var preset = state.presets.filter(function (p) { return p.id === state.candleStyle.preset; })[0];
    var colors = preset && preset.theme_colors && preset.theme_colors[chartTheme()];
    if (!colors || (colors.bull_color === state.candleStyle.bull && colors.bear_color === state.candleStyle.bear)) return;
    state.candleStyle = { preset: preset.id, bull: colors.bull_color, bear: colors.bear_color };
    if (record) {
      csPending.bull_color = colors.bull_color; csPending.bear_color = colors.bear_color;
      scheduleChartSave(); csStatus('Saving...');
    }
    redrawCharts();
  }

  /* ---- application theme (light / dark): themed presets follow the chart's effective theme ---- */
  function setTheme(theme, persist) {
    theme = theme === 'light' ? 'light' : 'dark';
    state.theme = theme;
    document.documentElement.setAttribute('data-theme', theme);
    saveLocal('trademate.chartTheme', theme);
    var tb = $('theme-toggle');
    if (tb) {
      tb.setAttribute('aria-checked', theme === 'light' ? 'true' : 'false');
      tb.setAttribute('title', theme === 'light' ? 'Light theme (click for dark)' : 'Dark theme (click for light)');
    }
    var sel = $('theme-select'); if (sel) sel.value = theme;
    syncPresetColorsToChartTheme(!!persist);
    terminals.forEach(function (t) { t.applyTheme(); });
    renderChartSettings();
    if (persist) { csPending.theme = theme; scheduleChartSave(); csStatus('Saving...'); }
  }
  function toggleTheme() { setTheme(state.theme === 'light' ? 'dark' : 'light', true); }
  /* ---- chart theme: only the chart (candles, grid, HUD, chart panel and toolbar); never state.theme ---- */
  function setChartTheme(mode, persist) {
    if (CHART_THEME_MODES.indexOf(mode) < 0) mode = 'auto';
    state.chartThemeSetting = mode;
    saveLocal('trademate.chartThemeMode', mode);
    syncPresetColorsToChartTheme(!!persist);
    terminals.forEach(function (t) { t.applyTheme(); });
    renderChartSettings();
    if (persist) { saveLocal('trademate.chartThemeUnsaved', mode); csPending.chart_theme = mode; scheduleChartSave(); csStatus('Saving...'); }   // the marker is cleared once the server has it
  }
  function toggleChartTheme() { setChartTheme(chartTheme() === 'light' ? 'dark' : 'light', true); }   // an explicit choice: the chart stops following the application

  function applyChartSettings(saved, presets) {
    if (presets) state.presets = presets;
    state.candleStyle = { preset: saved.preset, bull: saved.bull_color, bear: saved.bear_color };
    state.structureVisible = saved.structure_visible === true;
    state.liquidityVisible = saved.liquidity_visible === true;
    state.fvgVisible = saved.fvg_visible === true;
    state.obVisible = saved.order_blocks_visible === true;
    var savedChartTheme = CHART_THEME_MODES.indexOf(saved.chart_theme) >= 0 ? saved.chart_theme : 'auto';
    var unsaved = loadLocal('trademate.chartThemeUnsaved', null);        // a choice made here that the server has not confirmed (page reloaded before the save landed)
    if (CHART_THEME_MODES.indexOf(unsaved) >= 0 && unsaved !== savedChartTheme && csPending.chart_theme === undefined) { csPending.chart_theme = unsaved; scheduleChartSave(); }
    if (CHART_THEME_MODES.indexOf(unsaved) >= 0 && unsaved !== savedChartTheme) savedChartTheme = unsaved;
    else if (unsaved !== null) saveLocal('trademate.chartThemeUnsaved', null);   // the server already agrees: nothing left to re-send
    if (savedChartTheme !== state.chartThemeSetting) { state.chartThemeSetting = savedChartTheme; saveLocal('trademate.chartThemeMode', savedChartTheme); terminals.forEach(function (t) { t.applyTheme(); }); }
    state.liquiditySettings = { tolerance_ticks: saved.liquidity_tolerance_ticks, sweep_mode: saved.liquidity_sweep_mode, max_pools: saved.liquidity_max_pools };
    state.chartSettingsLoaded = true;
    if (saved.theme && saved.theme !== state.theme) setTheme(saved.theme, false);       // the saved preference wins
    renderChartSettings(); redrawCharts();
  }

  function flushChartSettings(onExit) {
    csTimer = null;
    if (!Object.keys(csPending).length || (csBusy && onExit !== true)) return;
    var body = csPending; csPending = {}; csBusy = true;
    fetch('/api/chart/settings', { method: 'POST', cache: 'no-store', keepalive: onExit === true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.message || 'The chart settings were not saved.');
        csSaveFailed = false;
        if (res.settings && res.settings.chart_theme === loadLocal('trademate.chartThemeUnsaved', null) && csPending.chart_theme === undefined) saveLocal('trademate.chartThemeUnsaved', null);
        if (!Object.keys(csPending).length) { applyChartSettings(res.settings, res.presets); csStatus('Saved.'); }   // newer edits win over this echo
      })
      .catch(function (err) {
        csSaveFailed = true; csStatus('Not saved: ' + err.message, true);
        if (!Object.keys(csPending).length) loadChartSettings();            // show what is really saved
      })
      .then(function () { csBusy = false; if (Object.keys(csPending).length) scheduleChartSave(); });
  }
  function scheduleChartSave() { if (csTimer) clearTimeout(csTimer); csTimer = setTimeout(flushChartSettings, 300); }
  /* A change made less than 300 ms before the page is reloaded or closed must not be lost (the chart would silently revert to the old
     theme / colours): send what is still waiting, with keepalive so the browser finishes the request while the page unloads. */
  function flushChartSettingsOnExit() { flushChartSettings(true); }   // the one POST site, with keepalive so the browser finishes it while the page unloads
  window.addEventListener('pagehide', flushChartSettingsOnExit);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flushChartSettingsOnExit(); });

  /* One edit at a time keeps the request valid: a preset choice sends only the preset, a colour edit only colours. */
  function setColors(bull, bear) {
    state.candleStyle = { preset: STYLE.matchPreset(bull, bear, state.presets), bull: bull, bear: bear };
    delete csPending.preset; csPending.bull_color = bull; csPending.bear_color = bear;
    renderChartSettings(); redrawCharts(); scheduleChartSave(); csStatus('Saving...');
  }
  function choosePreset(id) {
    if (!state.chartSettingsLoaded) return;
    if (id === 'custom') {
      state.candleStyle.preset = 'custom';
      delete csPending.bull_color; delete csPending.bear_color; csPending.preset = 'custom';
    } else {
      var p = state.presets.filter(function (x) { return x.id === id; })[0];
      if (!p) return;
      var colors = p.theme_colors && p.theme_colors[chartTheme()] || p;
      state.candleStyle = { preset: id, bull: colors.bull_color, bear: colors.bear_color };
      delete csPending.bull_color; delete csPending.bear_color; csPending.preset = id;
    }
    renderChartSettings(); redrawCharts(); scheduleChartSave(); csStatus('Saving...');
  }
  function setStructureVisible(on) {
    if (!state.chartSettingsLoaded) return;
    state.structureVisible = !!on;
    csPending.structure_visible = state.structureVisible;
    renderChartSettings(); redrawCharts(); scheduleChartSave(); csStatus('Saving...');
  }
  function setLiquidityVisible(on) {
    if (!state.chartSettingsLoaded) return;
    state.liquidityVisible = !!on;
    csPending.liquidity_visible = state.liquidityVisible;
    if (state.liquidityVisible) loadCandles(true);                // the liquidity data is only fetched while it is shown
    renderChartSettings(); redrawCharts(); scheduleChartSave(); csStatus('Saving...');
  }
  function setFvgVisible(on) {
    if (!state.chartSettingsLoaded) return;
    state.fvgVisible = !!on;                                      // display only: the FVG data is fetched and kept either way
    csPending.fvg_visible = state.fvgVisible;
    renderChartSettings(); redrawCharts(); scheduleChartSave(); csStatus('Saving...');
  }
  function setObVisible(on) {
    if (!state.chartSettingsLoaded) return;
    state.obVisible = !!on;                                       // display only: the order block data is fetched and kept either way
    csPending.order_blocks_visible = state.obVisible;
    renderChartSettings(); redrawCharts(); scheduleChartSave(); csStatus('Saving...');
  }
  function anyOverlayVisible() { return !!(state.structureVisible || state.liquidityVisible || state.fvgVisible || state.obVisible); }
  function setAllOverlays(on) {                                   // Show all / Hide all: the four analysis overlays, one saved update
    if (!state.chartSettingsLoaded) return;
    on = !!on;
    state.structureVisible = state.liquidityVisible = state.fvgVisible = state.obVisible = on;
    csPending.structure_visible = csPending.liquidity_visible = csPending.fvg_visible = csPending.order_blocks_visible = on;
    if (on) loadCandles(true);                                    // the liquidity data is only fetched while it is shown
    renderChartSettings(); redrawCharts(); scheduleChartSave(); csStatus('Saving...');
  }
  function setLiquidityOption(key, value) {                       // key: liquidity_tolerance_ticks | liquidity_sweep_mode | liquidity_max_pools
    if (!state.chartSettingsLoaded) return;
    csPending[key] = value; scheduleChartSave(); csStatus('Saving...');
    state.liquidity = null;                                       // old results used the old setting: never show them
    setTimeout(function () { loadCandles(true); }, 700);          // after the debounced save has reached the server
  }
  function loadChartSettings() {
    return fetch('/api/chart/settings', { cache: 'no-store' }).then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok || Object.keys(csPending).length) return;
        // Nothing saved on the server yet but this browser remembers a theme (older versions): keep the user's choice.
        var legacy = null; try { legacy = JSON.parse(localStorage.getItem('trademate.chartTheme')); } catch (e) { legacy = null; }
        var migrate = res.stored === false && (legacy === 'light' || legacy === 'dark') && legacy !== res.settings.theme;
        applyChartSettings(res.settings, res.presets); csStatus('');
        if (migrate) setTheme(legacy, true);
      })
      .catch(function () { /* the periodic retry below tries again */ });
  }
  (function wireChartSettings() {
    if (!$('cs-preset')) return;
    $('cs-preset').addEventListener('change', function (e) { choosePreset(e.target.value); });
    ['bull', 'bear'].forEach(function (side) {
      var picker = $('cs-' + side + '-picker'), hex = $('cs-' + side + '-hex');
      function commit(value) {
        var v = STYLE.normalizeHex(value);
        if (!v || !state.chartSettingsLoaded) { hex.setAttribute('aria-invalid', 'true'); return; }
        var cs = state.candleStyle;
        setColors(side === 'bull' ? v : cs.bull, side === 'bear' ? v : cs.bear);
      }
      picker.addEventListener('input', function () { commit(picker.value); });
      hex.addEventListener('input', function () { commit(hex.value); });
      hex.addEventListener('blur', function () { hex.value = state.candleStyle[side]; hex.removeAttribute('aria-invalid'); });
    });
    $('cs-structure').addEventListener('change', function (e) { setStructureVisible(e.target.checked); });
    $('cs-liquidity').addEventListener('change', function (e) { setLiquidityVisible(e.target.checked); });
    $('cs-fvg').addEventListener('change', function (e) { setFvgVisible(e.target.checked); });
    $('cs-ob').addEventListener('change', function (e) { setObVisible(e.target.checked); });
    $('cs-overlays-show').addEventListener('click', function () { setAllOverlays(true); });
    $('cs-overlays-hide').addEventListener('click', function () { setAllOverlays(false); });
    $('cs-liq-ticks').addEventListener('change', function (e) { var v = parseInt(e.target.value, 10); if (isFinite(v) && v >= 0 && v <= 500) setLiquidityOption('liquidity_tolerance_ticks', v); else { csStatus('Tolerance must be a whole number of ticks from 0 to 500.', true); renderChartSettings(); } });
    $('cs-liq-mode').addEventListener('change', function (e) { setLiquidityOption('liquidity_sweep_mode', e.target.value); });
    $('cs-liq-max').addEventListener('change', function (e) { var v = parseInt(e.target.value, 10); if (isFinite(v) && v >= 1 && v <= 50) setLiquidityOption('liquidity_max_pools', v); else { csStatus('Pools per side must be a whole number from 1 to 50.', true); renderChartSettings(); } });
    if ($('theme-toggle')) $('theme-toggle').addEventListener('click', toggleTheme);
    if ($('theme-select')) $('theme-select').addEventListener('change', function (e) { setTheme(e.target.value, true); });
    if ($('cs-chart-theme')) $('cs-chart-theme').addEventListener('change', function (e) { setChartTheme(e.target.value, true); });
    ['cs-bull-picker', 'cs-bear-picker', 'cs-bull-hex', 'cs-bear-hex', 'cs-preset', 'cs-structure', 'cs-liquidity', 'cs-fvg', 'cs-ob', 'cs-chart-theme', 'cs-overlays-show', 'cs-overlays-hide', 'cs-liq-ticks', 'cs-liq-mode', 'cs-liq-max'].forEach(function (id) { $(id).disabled = true; });
  })();
  setTheme(state.theme, false);        // sync the switch / select with the theme the page started in
  var csRetry = setInterval(function () {
    if (state.chartSettingsLoaded) { clearInterval(csRetry); return; }
    loadChartSettings();
  }, 5000);
  var csEnable = setInterval(function () {
    if (!state.chartSettingsLoaded) return;
    clearInterval(csEnable);
    ['cs-bull-picker', 'cs-bear-picker', 'cs-bull-hex', 'cs-bear-hex', 'cs-preset', 'cs-structure', 'cs-liquidity', 'cs-fvg', 'cs-ob', 'cs-chart-theme', 'cs-overlays-show', 'cs-overlays-hide', 'cs-liq-ticks', 'cs-liq-mode', 'cs-liq-max'].forEach(function (id) { if ($(id)) $(id).disabled = false; });
  }, 200);

  /* ---------- Trade history: closed trades read from MT5's own deal history (nothing is invented) ---------- */
  var hist = { all: false, hiddenCount: 0, days: loadLocal('trademate.historyDays', 30), trades: null, currency: '', msg: '', busy: false, at: 0, stale: true };
  if ([7, 30, 90, 365].indexOf(hist.days) < 0) hist.days = 30;
  var REASONS = { sl: 'Stop loss', tp: 'Take profit', so: 'Stop-out', client: 'Manual', mobile: 'Mobile', web: 'Web', expert: 'Expert', other: 'Closed' };
  function histVisible() { return currentView() === 'history'; }
  function pad2(n) { return ('0' + n).slice(-2); }
  /* Times are MT5 server time, the same clock as the chart's time axis. */
  function histDate(t) { if (t == null) return DASH; var d = new Date(t * 1000); return d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate()); }
  function histTime(t) { if (t == null) return DASH; var d = new Date(t * 1000); return pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()) + ':' + pad2(d.getUTCSeconds()); }
  function histPrice(v, symName) {
    if (v == null) return DASH;
    var sym = symbolByName(symName);
    if (sym && typeof sym.digits === 'number') return v.toFixed(sym.digits);
    var dec = (String(v).split('.')[1] || '').length;
    return v.toFixed(Math.max(2, Math.min(5, dec)));
  }
  function histResult(net) {
    if (net == null) return { text: DASH, cls: 'res-flat' };
    if (net >= 0.005) return { text: 'WIN', cls: 'res-win' };
    if (net <= -0.005) return { text: 'LOSS', cls: 'res-loss' };
    return { text: 'BREAKEVEN', cls: 'res-flat' };
  }
  function histRow(t, cur) {
    var tr = document.createElement('tr');
    tr.className = 'hist-row';
    function add(label, text, cls) { var c = cell(text, cls); c.setAttribute('data-label', label); tr.appendChild(c); return c; }
    var when = add('Closed', '', 'h-when');
    var d1 = document.createElement('span'), d2 = document.createElement('small');
    d1.textContent = histDate(t.time_close); d2.textContent = histTime(t.time_close);
    when.appendChild(d1); when.appendChild(d2);
    when.title = 'Opened ' + histDate(t.time_open) + ' ' + histTime(t.time_open) + ' (MT5 server time)';
    var symCell = add('Symbol', t.symbol || DASH, 'h-sym');
    if (t.rapid_fire) {                                        // optional tag from the Rapid Fire ledger: strategy, timeframe, original / twin link
      var rf = t.rapid_fire, b1 = document.createElement('small'), b2;
      b1.className = 'badge-rf'; b1.textContent = (rf.strategy || 'RAPID FIRE') + ' · ' + (rf.timeframe || 'M1');
      symCell.appendChild(document.createElement('br')); symCell.appendChild(b1);
      if (rf.role === 'anti_twin' || rf.twin_ticket) {
        b2 = document.createElement('small'); b2.className = 'badge-twin';
        b2.textContent = rf.role === 'anti_twin' ? 'ANTI-TWIN of #' + (rf.twin_of == null ? '?' : rf.twin_of) : 'ORIGINAL, twin #' + rf.twin_ticket;
        symCell.appendChild(document.createTextNode(' ')); symCell.appendChild(b2);
      }
      if (rf.target_points != null) symCell.title = 'Rapid Fire target ' + rf.target_points + ' points (software-managed, no broker TP/SL). Closed by: ' + (rf.closed_by || 'unknown');
    }
    var type = add('Type', (t.direction || '').toUpperCase() || DASH, 'h-type dir-' + (t.direction === 'sell' ? 'sell' : 'buy'));
    type.classList.add(t.direction === 'sell' ? 'is-sell' : 'is-buy');
    add('Volume', t.volume == null ? DASH : String(t.volume), 'num');
    add('Entry', histPrice(t.price_open, t.symbol), 'num');
    add('Exit', histPrice(t.price_close, t.symbol), 'num');
    add('SL', t.stop_loss == null ? DASH : histPrice(t.stop_loss, t.symbol), 'num');
    add('TP', t.take_profit == null ? DASH : histPrice(t.take_profit, t.symbol), 'num');
    var pl = add('P/L', '', 'num h-pl');
    setPL(pl, t.net, '');
    if ((t.swap || t.commission) && t.net != null) pl.title = 'Profit ' + moneyFmt.format(t.profit) + ', swap ' + moneyFmt.format(t.swap || 0) + ', commission ' + moneyFmt.format(t.commission || 0);
    var res = histResult(t.net), rc = add('Result', '', 'h-res');
    var badge = document.createElement('span'), why = document.createElement('small');
    badge.className = 'res ' + res.cls; badge.textContent = res.text;
    why.textContent = REASONS[t.reason] || REASONS.other;
    rc.appendChild(badge); rc.appendChild(why);
    return tr;
  }
  function renderHistory() {
    var body = $('history-body'), note = $('history-note');
    if (!body) return;
    document.querySelectorAll('[data-hdays]').forEach(function (b) { b.setAttribute('aria-pressed', Number(b.getAttribute('data-hdays')) === hist.days ? 'true' : 'false'); });
    var cur = hist.currency || (state.account ? state.account.currency : '');
    var trades = hist.trades || [], net = 0, wins = 0, losses = 0, best = null;
    trades.forEach(function (t) {
      if (t.net == null) return;
      net += t.net;
      if (t.net >= 0.005) wins++; else if (t.net <= -0.005) losses++;
      if (best == null || t.net > best) best = t.net;
    });
    var hid = $('history-hidden');
    if (hid) { hid.hidden = !(hist.hiddenCount > 0 || hist.all); $('history-hidden-text').textContent = hist.all ? 'Showing ALL MT5 history, including trades cleared from this view. ' : hist.hiddenCount + ' older MT5 trade(s) are hidden here because you deleted TradeMate\'s history. They are still in MT5. '; $('history-showall').textContent = hist.all ? 'Hide cleared trades' : 'Show all MT5 history'; }
    setText('hist-count', hist.trades ? String(trades.length) : DASH);
    var netEl = $('hist-net'); if (netEl) { if (hist.trades) setPL(netEl, net, cur); else { netEl.textContent = DASH; netEl.className = ''; } }
    setText('hist-rate', wins + losses ? Math.round(wins / (wins + losses) * 100) + '%  (' + wins + 'W / ' + losses + 'L)' : DASH);
    var bestEl = $('hist-best'); if (bestEl) { if (best != null) setPL(bestEl, best, cur); else { bestEl.textContent = DASH; bestEl.className = ''; } }

    if (hist.trades && trades.length) {
      body.replaceChildren.apply(body, trades.map(function (t) { return histRow(t, cur); }));
      note.textContent = 'Times are MT5 server time. P/L is net of swap and commission, in ' + (cur || 'account currency') + '.';
      note.className = 'note panel-note';
    } else {
      var msg = hist.busy && !hist.trades ? 'Loading history from MT5...' :
        hist.msg ? hist.msg :
        hist.trades ? 'No closed trades in the last ' + (hist.days === 365 ? 'year' : hist.days + ' days') + '.' :
        'Waiting for MT5...';
      emptyRow(body, 10, msg);
      note.textContent = hist.msg && !hist.trades ? '' : 'Closed trades appear here as soon as MT5 reports them.';
      note.className = 'note panel-note' + (hist.msg ? ' is-bad' : '');
    }
  }
  function loadHistory(force) {
    if (hist.busy) return;
    if (!state.connected) { hist.msg = ''; renderHistory(); return; }
    if (!force && !hist.stale && Date.now() - hist.at < 8000) return;      /* newly closed trades appear within seconds without pressing Refresh */
    hist.busy = true; renderHistory();
    var days = hist.days;
    fetch('/api/history?days=' + days + '&limit=300' + (hist.all ? '&all=1' : ''), { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (res) {
        if (days !== hist.days) { hist.stale = true; return; }          // the period changed while loading
        if (res.ok) { hist.trades = res.trades || []; hist.currency = res.currency || ''; hist.msg = ''; hist.hiddenCount = res.hidden_count || 0; }
        else { hist.trades = null; hist.msg = res.message || 'MT5 could not supply the history.'; }
        hist.stale = false;
      })
      .catch(function () { hist.trades = null; hist.msg = 'Cannot load the history from the TradeMate server.'; hist.stale = false; })
      .then(function () { hist.busy = false; hist.at = Date.now(); renderHistory(); });
  }
  document.querySelectorAll('[data-hdays]').forEach(function (b) {
    b.addEventListener('click', function () {
      hist.days = Number(b.getAttribute('data-hdays')); saveLocal('trademate.historyDays', hist.days);
      hist.stale = true; hist.trades = null; hist.msg = ''; renderHistory(); loadHistory(true);
    });
  });
  window.addEventListener('trademate:trade-closed', function () { hist.stale = true; if (histVisible()) { loadHistory(true); loadActivity(true); } });   /* a closed-trade notification refreshes the table at once */
  var histRefresh = $('history-refresh');
  if (histRefresh) histRefresh.addEventListener('click', function () { loadHistory(true); });
  Router.on(function () { if (histVisible()) { renderHistory(); loadHistory(false); loadActivity(true); } });


  /* ---------- TradeMate activity: the local journal of every order TradeMate sent ---------- */
  var act = { rows: null, at: 0, busy: false, msg: '' };
  var STATE_LABEL = { pending: ['SENDING', 'st-warn'], not_sent: ['NOT SENT', 'st-flat'], rejected: ['REJECTED', 'st-bad'],
    confirmed: ['CONFIRMED', 'st-good'], unconfirmed: ['NOT CONFIRMED', 'st-warn'], unknown: ['UNKNOWN', 'st-warn'],
    reconciled_executed: ['EXECUTED \u2713', 'st-good'], reconciled_not_executed: ['NOT EXECUTED', 'st-flat'] };
  var ACTION_LABEL = { place: 'Open', modify: 'Modify SL/TP', close: 'Close' };
  function localStamp(sec) { var d = new Date(sec * 1000); return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds()); }
  function renderActivity() {
    var body = $('activity-body');
    if (!body) return;
    if (!act.rows || !act.rows.length) {
      emptyRow(body, 8, act.msg || (act.rows ? 'No orders recorded by TradeMate yet.' : 'Loading...'));
      return;
    }
    body.replaceChildren.apply(body, act.rows.map(function (r) {
      var tr = document.createElement('tr'); tr.className = 'hist-row';
      function add(label, text, cls) { var c = cell(text, cls); c.setAttribute('data-label', label); tr.appendChild(c); return c; }
      add('Time', localStamp(r.created_at), 'a-when');
      add('Action', ACTION_LABEL[r.action] || r.action, 'h-type');
      add('Symbol', r.symbol || DASH, 'h-sym');
      add('Type', (r.side || '').toUpperCase() || DASH, 'h-type dir-' + (r.side === 'sell' ? 'sell' : 'buy'));
      add('Volume', r.volume == null ? DASH : String(r.volume), 'num');
      add('Ticket', r.ticket == null ? DASH : '#' + r.ticket, 'num');
      var st = STATE_LABEL[r.state] || [r.state, 'st-flat'], sc = add('State', '', 'a-state'), badge = document.createElement('span');
      badge.className = 'st ' + st[1]; badge.textContent = st[0]; sc.appendChild(badge);
      add('Detail', r.message || '', 'a-detail');
      return tr;
    }));
  }
  function loadActivity(force) {
    if (act.busy || (!force && Date.now() - act.at < 5000)) return;
    act.busy = true;
    fetch('/api/executions', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (res) { if (res.ok) { act.rows = res.executions || []; act.msg = ''; } else { act.rows = null; act.msg = res.message || 'No local record available.'; } })
      .catch(function () { act.rows = null; act.msg = 'Cannot load TradeMate\'s local record.'; })
      .then(function () { act.busy = false; act.at = Date.now(); renderActivity(); });
  }

  /* ---------- unknown-outcome bar ---------- */
  function postSimple(url, body) {
    return fetch(url, { method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })
      .then(function (r) { return r.json(); });
  }
  function renderUnresolved() {
    var bar = $('unresolved-bar');
    if (!bar) return;
    var list = state.unresolved || [];
    bar.hidden = !list.length;
    if (!list.length) return;
    var r = list[0], what = r.action === 'place' ? ((r.side || '').toUpperCase() + ' ' + r.volume + ' ' + r.symbol) :
      (ACTION_LABEL[r.action] || r.action) + ' #' + r.ticket;
    $('ub-title').textContent = 'Order outcome unknown: ' + what + (list.length > 1 ? '  (+' + (list.length - 1) + ' more)' : '');
    $('ub-msg').textContent = ' ' + (r.message || '') + ' Do not send it again until this is resolved.';
    bar.setAttribute('data-id', r.id);
  }
  function ubAct(fn) { var id = $('unresolved-bar').getAttribute('data-id'); if (id) fn(id); }
  var ubCheck = $('ub-check');
  if (ubCheck) {
    ubCheck.addEventListener('click', function () {
      ubAct(function (id) {
        ubCheck.disabled = true;
        postSimple('/api/executions/' + encodeURIComponent(id) + '/reconcile', {})
          .then(function (res) { showToast(res.ok ? (res.execution.message || 'Checked.') : (res.message || 'Could not check right now.')); })
          .catch(function () { showToast('Could not reach the TradeMate server.'); })
          .then(function () { ubCheck.disabled = false; poll(true); act.at = 0; });
      });
    });
    function ubResolve(outcome, warning) {
      ubAct(function (id) {
        if (!window.confirm(warning)) return;
        postSimple('/api/executions/' + encodeURIComponent(id) + '/resolve', { outcome: outcome })
          .then(function (res) { showToast(res.ok ? 'Recorded.' : (res.message || 'Could not record that.')); })
          .catch(function () { showToast('Could not reach the TradeMate server.'); })
          .then(function () { poll(true); act.at = 0; });
      });
    }
    $('ub-notexec').addEventListener('click', function () { ubResolve('not_executed', 'Only confirm this after checking MT5\'s Trade and History tabs. If the order DID execute, sending it again would double your position.\n\nMark it as NOT executed?'); });
    $('ub-exec').addEventListener('click', function () { ubResolve('executed', 'Mark this order as executed? Only do so after seeing it in MT5.'); });
  }

  /* ---------- Delete Trading History (TradeMate's LOCAL record only) ---------- */
  var clearDialog = $('clear-dialog'), clearBtn = $('clear-history-btn');
  if (clearBtn && clearDialog) {
    clearBtn.addEventListener('click', function () {
      var n = (state.unresolved || []).length, warn = $('clear-warn');
      warn.hidden = !n;
      if (n) warn.textContent = n + ' order(s) with an UNKNOWN outcome will also be forgotten. Check MT5 for them first.';
      if (clearDialog.showModal) clearDialog.showModal(); else clearDialog.setAttribute('open', '');
    });
    $('clear-cancel').addEventListener('click', function () { clearDialog.close(); });
    $('clear-confirm').addEventListener('click', function () {
      var confirmBtn = $('clear-confirm'), newest = 0;
      (hist.trades || []).forEach(function (t) { if (t.time_close && t.time_close > newest) newest = t.time_close; });
      confirmBtn.disabled = true;
      var body = { confirm: 'DELETE' };
      if (newest) body.hide_mt5_view_before = newest;          // what is on screen now; no MT5 request is made
      postSimple('/api/local-history/clear', body)
        .then(function (res) {
          if (!res.ok) { showToast(res.message || 'Could not delete the history.'); return; }
          clearDialog.close();
          showToast('TradeMate history deleted (' + res.deleted + ' record' + (res.deleted === 1 ? '' : 's') + '). MT5 was not touched.');
          hist.all = false; hist.stale = true; hist.trades = null; act.rows = null; act.at = 0;
          renderHistory(); renderActivity(); loadHistory(true); loadActivity(true); poll(true);
        })
        .catch(function () { showToast('Could not reach the TradeMate server. Nothing was deleted.'); })
        .then(function () { confirmBtn.disabled = false; });
    });
  }
  var showAll = $('history-showall');
  if (showAll) showAll.addEventListener('click', function () { hist.all = !hist.all; hist.stale = true; hist.trades = null; renderHistory(); loadHistory(true); });

  /* ---------- Risk Engine (Phase 5.2): shows the rules + state, asks the server for ONE decision. Sends no order. ---------- */
  var risk = { names: '', at: 0, busy: false };
  function postRisk(url, body) {
    return fetch(url, { method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); });
  }
  function riskSyncSymbols() {
    var sel = $('risk-symbol');
    if (!sel) return;
    var names = state.symbols.map(function (s) { return s.name; });
    var key = names.join('|');
    if (key === risk.names) return;
    risk.names = key;
    var keep = sel.value || state.selected || '';
    sel.textContent = '';
    names.forEach(function (n) { var o = document.createElement('option'); o.value = n; o.textContent = n; sel.appendChild(o); });
    if (names.indexOf(keep) >= 0) sel.value = keep;
  }
  function pctText(v) { return v == null ? '' : ' (' + v.toFixed(2) + '%)'; }
  function riskShowState(res) {
    var set = function (k, text, cls) {
      var el = document.querySelector('[data-rs="' + k + '"]');
      if (el) { el.textContent = text; el.className = cls || ''; }
    };
    var note = $('risk-state-note');
    if (res.rules && document.activeElement && document.activeElement.className.indexOf('input') < 0) {
      $('rule-trade').value = res.rules.max_risk_per_trade_percent; $('rule-daily').value = res.rules.max_daily_loss_percent;
      $('rule-active').value = res.rules.max_total_active_risk_percent; $('rule-positions').value = res.rules.max_positions;
    }
    if (!res.ok) {
      ['equity', 'balance', 'todays_loss', 'daily_remaining', 'open_positions', 'active_risk', 'floating_pl', 'daily_status'].forEach(function (k) { set(k, DASH); });
      note.textContent = res.message || 'MT5 data is not available.';
      return;
    }
    var s = res.state, cur = s.currency;
    set('equity', money(s.equity, cur)); set('balance', money(s.balance, cur));
    set('todays_loss', s.todays_loss == null ? 'unknown' : money(s.todays_loss, cur) + pctText(s.todays_loss_percent), s.todays_loss ? 'is-warn' : '');
    set('daily_remaining', s.daily_remaining == null ? 'unknown' : money(s.daily_remaining, cur) + pctText(s.daily_remaining_percent));
    set('open_positions', s.open_positions + ' / ' + s.rules.max_positions);
    set('active_risk', money(s.active_risk, cur) + pctText(s.active_risk_percent) + (s.active_risk_complete ? '' : ' + unknown'),
        s.active_risk_complete ? '' : 'is-warn');
    set('floating_pl', s.floating_pl == null ? DASH : plText(s.floating_pl, cur), plClass(s.floating_pl));
    var list = $('risk-positions'); list.textContent = '';
    s.positions.forEach(function (p) {
      var row = document.createElement('div'), dt = document.createElement('dt'), dd = document.createElement('dd');
      dt.textContent = '#' + p.ticket + ' ' + (p.direction || '').toUpperCase() + ' ' + p.symbol + ' ' + (p.volume == null ? DASH : p.volume);
      dd.textContent = 'entry ' + (p.price_open == null ? DASH : p.price_open) + ' \u00B7 SL ' + (p.stop_loss == null ? 'none' : p.stop_loss) +
        ' \u00B7 TP ' + (p.take_profit == null ? 'none' : p.take_profit) + ' \u00B7 P&L ' + plText(p.floating_pl, cur) +
        ' \u00B7 risk at SL ' + (p.risk_amount == null ? 'unknown (' + p.note + ')' : money(p.risk_amount, cur));
      row.appendChild(dt); row.appendChild(dd); list.appendChild(row);
    });
    set('daily_status', s.todays_loss == null ? 'UNKNOWN' : (s.daily_locked ? 'TRADING LOCKED' : 'OK'), s.daily_locked ? 'is-bad' : (s.todays_loss == null ? 'is-warn' : 'is-ok'));
    var msgs = [];
    if (s.unprotected.length) msgs.push('Active risk cannot be fully calculated: ' + s.unprotected.join('; ') + '.');
    if (s.history_error) msgs.push('Daily loss unknown: ' + s.history_error);
    msgs.push('Day = ' + (s.day_start_source || 'unknown') + '.');
    note.textContent = msgs.join(' ');
  }
  function riskRefresh(force) {
    if (!$('risk-state') || risk.busy || !state.connected && !force) return;
    if ($('view-strategy').hidden && !force) return;
    if (!force && Date.now() - risk.at < 3000) return;
    risk.busy = true;
    fetch('/api/risk/state', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(riskShowState)
      .catch(function () { /* keep what is shown */ }).then(function () { risk.busy = false; risk.at = Date.now(); });
  }
  function riskShowDecision(d, cur) {
    var out = $('risk-out');
    var set = function (k, text, cls) { var el = out.querySelector('[data-risk="' + k + '"]'); el.textContent = text; el.className = cls || ''; };
    var ok = d.decision === 'ACCEPTED';
    var lots = function (v) { return v == null ? DASH : (+v.toFixed(8)) + ' lots'; };
    set('requested', d.requested_risk_amount == null ? DASH : money(d.requested_risk_amount, cur) + pctText(d.requested_risk_percent));
    set('ideal', lots(d.ideal_volume));
    set('broker', lots(d.broker_valid_volume));
    set('risk_amount', d.trade_risk_amount == null ? DASH : money(d.trade_risk_amount, cur) + pctText(d.trade_risk_percent));
    set('normalized_volume', d.approved_volume == null ? 'none (not accepted)' : lots(d.approved_volume), ok ? 'is-ok' : '');
    set('effective', d.effective_max_risk_amount == null ? DASH : money(d.effective_max_risk_amount, cur) + pctText(d.effective_max_risk_percent) + ' \u2013 ' + d.limiting_rule);
    set('decision', d.decision + (d.trading_locked ? ' \u2013 TRADING LOCKED' : ''), ok ? 'is-ok' : 'is-bad');
    set('rejection_reason', (d.rejection_reasons && d.rejection_reasons.length) ? d.rejection_reasons.join('  |  ') : DASH, ok ? '' : 'is-bad');
    var banner = $('risk-banner');
    banner.textContent = ok ? 'ACCEPTED: passed every risk check. NO order has been sent. Sending orders is Phase 6 (Execution), not built yet.'
                            : 'REJECTED: no order is sent.';
    banner.className = 'note ' + (ok ? 'is-ok' : 'is-bad');
    var box = $('risk-checks'); box.textContent = '';
    (d.checks || []).forEach(function (c) {
      var row = document.createElement('div'), dt = document.createElement('dt'), dd = document.createElement('dd');
      dt.textContent = c.status + '  ' + c.name; dd.textContent = c.detail; dd.className = c.passed ? 'is-ok' : 'is-bad';
      row.appendChild(dt); row.appendChild(dd); box.appendChild(row);
    });
  }
  function riskEvaluate() {
    postRisk('/api/risk/evaluate', { symbol: $('risk-symbol').value, direction: $('risk-direction').value, entry: $('risk-entry').value,
                                     stop_loss: $('risk-sl').value, take_profit: $('risk-tp').value, risk_percent: $('risk-pct').value })
      .then(function (res) {
        if (!res.ok) { showToast(res.message || 'Risk evaluation failed.'); return; }
        riskShowDecision(res.decision, res.account_currency);
        riskRefresh(true);
      }, function () { showToast('Could not reach TradeMate.'); });
  }
  function riskSaveRules() {
    postRisk('/api/risk/rules', { max_risk_per_trade_percent: $('rule-trade').value, max_daily_loss_percent: $('rule-daily').value,
                                  max_total_active_risk_percent: $('rule-active').value, max_positions: $('rule-positions').value })
      .then(function (res) { showToast(res.ok ? 'Risk limits saved.' : (res.message || 'Could not save the limits.')); riskRefresh(true); },
            function () { showToast('Could not reach TradeMate.'); });
  }
  if ($('risk-calc-btn')) {
    $('risk-calc-btn').addEventListener('click', riskEvaluate);
    $('rules-save').addEventListener('click', riskSaveRules);
    $('risk-live').addEventListener('click', function () {
      var name = $('risk-symbol').value, buy = $('risk-direction').value === 'buy';
      var sym = state.symbols.filter(function (s) { return s.name === name; })[0];
      var px = sym ? (buy ? sym.ask : sym.bid) : null;
      if (px == null) { showToast('No live price for that symbol yet.'); return; }
      $('risk-entry').value = px;
    });
    Router.on(function () { riskRefresh(true); });
  }

  function renderAccountCentre(status, account) {
    var connected = !!(status && status.state === 'CONNECTED' && account);
    var badge = $('account-centre-status');
    if (badge) {
      badge.textContent = connected ? 'Connected · identity verified' : (status ? status.state : 'Unavailable');
      badge.className = connected ? 'badge badge-ok' : 'badge';
      badge.setAttribute('data-state', connected ? 'connected' : 'unavailable');
    }
    var values = {
      broker: connected ? account.broker : DASH,
      server: connected ? account.server : DASH,
      environment: connected && account.environment ? String(account.environment).toUpperCase() : DASH,
      login: connected ? String(account.login) : DASH,
      currency: connected ? (account.currency || DASH) : DASH,
      leverage: connected && account.leverage != null ? '1:' + account.leverage : DASH,
      balance: connected ? money(account.balance, account.currency) : DASH,
      equity: connected ? money(account.equity, account.currency) : DASH,
      'free-margin': connected ? money(account.free_margin, account.currency) : DASH,
      profit: connected ? plText(account.profit, account.currency) : DASH,
      age: connected && typeof status.age_seconds === 'number' && isFinite(status.age_seconds)
        ? status.age_seconds.toFixed(1) + 's ago' : DASH
    };
    Object.keys(values).forEach(function (key) {
      var el = $('account-centre-' + key);
      if (el) el.textContent = values[key];
    });
    var message = $('account-centre-message');
    if (message) {
      message.textContent = connected
        ? 'Identity is taken from the currently connected MT5 snapshot. Figures update with the broker connection.'
        : (status && status.message) || 'No fresh, verified account information is available.';
    }
  }

  /* ---------- Rapid Fire exit levels for the chart ---------- */
  var rfLevelsAt = 0, rfLevelsGoodAt = 0;
  var RF_LEVELS_KEEP_MS = 15000;     // one failed / stale poll must not wipe the lines of a position that is still open; they are kept (and only for tickets still open) this long
  function loadRfLevels() {
    if (!state.positions.length) { state.rfLevels = []; return; }              // nothing open: nothing to draw
    var now = Date.now(); if (now - rfLevelsAt < 1200) return; rfLevelsAt = now;
    function keepOrClear() { if (Date.now() - rfLevelsGoodAt > RF_LEVELS_KEEP_MS) state.rfLevels = []; }
    fetch('/api/rapid-fire/chart-levels', { cache: 'no-store' }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.ok && !j.stale) { state.rfLevels = j.levels || []; rfLevelsGoodAt = Date.now(); } else keepOrClear();
      }).catch(keepOrClear);
  }

  /* ---------- polling the broker ---------- */
  function renderBroker(data) {
    var connected = data.status.state === 'CONNECTED';
    state.connected = connected;
    if (connected) { state.quoteAt = Date.now(); state.statusAge = data.status.age_seconds; }   // time of the last SUCCESSFUL poll
    state.pendingSupported = data.pending_supported === true;
    state.orders = connected ? (data.orders || []) : [];
    state.account = data.account;
    state.positions = data.positions || [];
    loadRfLevels();
    state.pendingSupported = data.pending_supported === true;
    state.orders = connected ? (data.orders || []) : [];
    state.symbols = data.symbols || [];
    state.unresolved = data.executions_unresolved || [];
    renderUnresolved();
    var cur = data.account ? data.account.currency : '';

    Object.keys(state.extra).forEach(function (n) { if (symbolByNameSnap(n)) delete state.extra[n]; });
    ensureWatchlist();
    if (connected && !state.watchSynced && state.watch) { state.watchSynced = true; syncWatch(); }
    renderStatus(data.status, data.account);
    renderAccountCentre(data.status, data.account);
    renderAccountCards(data.account);
    renderMarket(state.symbols);
    renderPositions(state.positions, connected, cur);
    renderPending(state.orders, connected, state.pendingSupported);
    syncPendingControls();
    tickLastCandle();
    renderQuote();
    renderWatchControls();
    ensureSpec(state.selected);
    riskSyncSymbols();
    riskRefresh(false);
    terminals.forEach(function (t) { t.syncSymbol(); t.draw(); });
    updateTradeButtons();
    if (connected && !state.candles.length) loadCandles(false);
    if (histVisible()) { loadHistory(false); loadActivity(false); }
  }

  function poll(now) {
    if ((polling && !now) || document.hidden) return;
    polling = true;
    fetch('/api/broker', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(renderBroker, function () { renderBroker(UNREACHABLE); })
      .then(function () { polling = false; }, function (err) { polling = false; console.error('TradeMate: could not render broker data', err); });
  }

  setInterval(poll, POLL_MS);
  var lastQuoteStates = null;
  setInterval(function () {       // a quote goes STALE with time even if no new poll arrives: redraw when the state changes
    var key = terminals.map(function (t) { return t.quoteState().state + (t.ordersFresh() ? '+' : '-') + (t.zonesFresh() ? 'z' : '_'); }).join('|');
    if (lastQuoteStates !== null && key !== lastQuoteStates) terminals.forEach(function (t) { t.draw(); });
    lastQuoteStates = key;
  }, 1000);
  document.addEventListener('visibilitychange', function () { poll(); });
  Router.on(function () { setTimeout(function () { terminals.forEach(function (t) { t.draw(); }); loadCandles(false); }, 0); });

  /* ---------- Clocks and candle countdown (static/js/clocks.js) ----------
   * Two clocks in the top bar (local + Eastern Time) and "closes in mm:ss" in each chart toolbar. Both only write text into
   * existing elements once a second; the chart is never redrawn or reloaded for them, and candle data is never touched.
   * Countdown = candle open (broker time) + timeframe - now (broker time). The broker's UTC offset is measured by the server
   * from live candles (/api/market/time). When it is unknown, the data is stale or the market is closed, the countdown says so
   * instead of showing a number. */
  var CLOCKS = window.TradeMateClocks;
  var brokerClock = { offset: null, at: 0, detail: '' };
  function loadBrokerClock() {
    if (document.hidden) return;
    fetch('/api/market/time', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (res) {
      brokerClock.offset = (res && typeof res.server_offset_seconds === 'number') ? res.server_offset_seconds : null;
      brokerClock.at = Date.now();               // an answer of "unknown" is still a fresh answer
      brokerClock.detail = (res && res.detail) || '';
    }).catch(function () { /* keep the last reading; it goes stale on its own after a few minutes */ });
  }
  function renderCountdown(nowMs) {
    var els = roleAll('candle-countdown');
    if (!els.length || !CLOCKS) return;
    nowMs = nowMs || Date.now();
    var key = state.selected + '|' + state.tf;
    var ready = state.candleKey === key && state.candles.length > 0;
    var r = ready
      ? CLOCKS.countdown({ tf: state.tf, candleTime: state.candles[state.candles.length - 1][0], offsetSeconds: brokerClock.offset, nowMs: nowMs,
                           fresh: { connected: state.connected, candlesAgeMs: state.candlesAt ? nowMs - state.candlesAt : null,
                                    clockAgeMs: brokerClock.at ? nowMs - brokerClock.at : null } })
      : { state: 'unavailable', text: '\u2014', reason: state.selected ? 'Loading candles' : 'No symbol selected' };
    var tip = r.reason ? 'Candle countdown unavailable or paused: ' + r.reason + (r.state === 'unavailable' && brokerClock.detail && r.reason === 'Broker time unknown' ? ' (' + brokerClock.detail + ')' : '')
                       : 'Time left before the current ' + state.tf + ' candle closes (broker time)';
    els.forEach(function (el) {
      if (el.getAttribute('data-state') !== r.state) el.setAttribute('data-state', r.state);
      var txt = el.querySelector('[data-role="candle-countdown-text"]');
      if (txt && txt.textContent !== r.text) txt.textContent = r.text;
      if (el.title !== tip) el.title = tip;
    });
  }
  if (CLOCKS) {
    var localBox = $('clock-local');
    CLOCKS.mountClocks({ document: document, localZone: localBox ? (localBox.getAttribute('data-clock-zone-config') || '') : '', onTick: renderCountdown });
    loadBrokerClock();
    setInterval(loadBrokerClock, 30000);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) { loadBrokerClock(); renderCountdown(); } });
  }

  /* ---------- start ---------- */
  fetch('/api/trading', { cache: 'no-store' }).then(function (r) { return r.json(); })
    .then(function (t) { state.trading = t; updateTradeButtons(); }, function () { /* keep defaults */ });
  loadChartSettings();
  renderExec();
  syncDrawerAria();
  renderHistory();
  renderActivity();
  poll();
})();
