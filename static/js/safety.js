/* TradeMate: Settings panels (Command Centre settings + calculator, execution & safety, chime, Hard Reset), the trading-session panel and the notification feed.
 *
 * The SERVER is the only authority for every value shown here (no browser copy can override it). Notifications use unique ids and a server sequence number:
 * a freshly loaded page asks for a BASELINE (no replay, no sound for old items); each new item is shown once and plays the chime at most once per batch.
 */
(function () {
  'use strict';
  function $(id) { return document.getElementById(id); }
  function api(url, opts) {
    opts = opts || {};
    var init = { cache: 'no-store', method: opts.method || 'GET', headers: { 'Content-Type': 'application/json' } };
    if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
    return fetch(url, init).then(function (r) { return r.json().catch(function () { return { ok: false, message: 'Unexpected answer from the server.' }; }); })
      .catch(function () { return { ok: false, message: 'Cannot reach the TradeMate server.' }; });
  }
  function text(id, v) { var n = $(id); if (n) n.textContent = v; }
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function money(v, cur) { return (v == null || !isFinite(v)) ? '---' : Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + (cur ? ' ' + cur : ''); }
  function visible(id) { var n = $(id); return !!n && !n.closest('[hidden]'); }

  /* ------------------------------------------------------------------ chime + notifications */
  var audio = null, lastPlay = 0, soundEnabled = true, lastSeq = null, seen = {}, shown = [];
  function chime() { if (!audio) { audio = new Audio('/static/audio/chime.wav?v=1'); audio.preload = 'auto'; audio.volume = 0.7; } return audio; }
  function playChime(force) {
    var now = Date.now(), a = chime();
    if (!force && (!soundEnabled || now - lastPlay < 1500)) return Promise.resolve('skipped');           /* never two chimes on top of each other */
    if (!a.paused && a.currentTime < a.duration - 0.05 && now - lastPlay < 1500) return Promise.resolve('skipped');
    lastPlay = now;
    try { a.currentTime = 0; } catch (e) { /* not seekable yet */ }
    var p = a.play();
    return (p && p.then) ? p.then(function () { return 'played'; }).catch(function () { return 'blocked'; }) : Promise.resolve('played');
  }
  function renderNotifs() {
    var ul = $('notif-list'); if (!ul) return;
    ul.innerHTML = shown.length ? shown.slice(-30).reverse().map(function (n) {
      return '<li data-level="' + esc(n.level) + '"><strong>' + esc(n.title) + '</strong> ' + esc(n.body || '') + ' <span class="note">' + new Date(n.created_at * 1000).toLocaleString() + '</span></li>';
    }).join('') : '<li>None yet.</li>';
  }
  function toast(msg) {
    var t = $('toast'); if (!t) return;
    t.textContent = msg; t.hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(function () { t.hidden = true; }, 5000);
  }
  function takeItems(items, announce) {
    var fresh = items.filter(function (n) { if (seen[n.id]) return false; seen[n.id] = true; return true; });       /* a unique id is shown once, whatever the poll returns */
    fresh.forEach(function (n) { shown.push(n); });
    if (fresh.length) renderNotifs();
    if (announce && fresh.length) {
      toast(fresh[fresh.length - 1].title + (fresh.length > 1 ? '  (+' + (fresh.length - 1) + ' more)' : ''));
      playChime(false);                                                                                         /* one chime per batch */
      if (fresh.some(function (n) { return n.kind && n.kind.indexOf('trade_') === 0; })) window.dispatchEvent(new CustomEvent('trademate:trade-closed'));
    }
  }
  function pollNotifs() {
    var url = lastSeq === null ? '/api/notifications?baseline=1' : '/api/notifications?after=' + lastSeq;
    api(url).then(function (j) {
      if (!j || j.ok === false) return;
      soundEnabled = j.sound_enabled !== false;
      if (lastSeq === null) { takeItems(j.history || [], false); lastSeq = j.latest_seq; return; }                /* baseline: history is listed, never announced */
      lastSeq = Math.max(lastSeq, j.latest_seq || 0);
      takeItems(j.items || [], true);
    });
  }

  /* ------------------------------------------------------------------ Command Centre settings + calculator */
  var sel = { symbol: null, strategy: null }, tsDirty = {}, tsEffective = null, calcTimer = null;
  function scope() { return $('ts-scope') ? $('ts-scope').value : 'global'; }
  function scopeQuery() {
    var s = scope();
    return '?symbol=' + encodeURIComponent((s === 'instrument' || s === 'strategy_instrument') ? (sel.symbol || '') : '') + '&strategy=' + encodeURIComponent((s === 'strategy' || s === 'strategy_instrument') ? (sel.strategy || '') : '');
  }
  function loadTradeSettings() {
    if (!$('ts-scope')) return;
    api('/api/selection').then(function (j) {
      sel.symbol = j.symbol || null; sel.strategy = j.strategy_id || null;
      var s = scope(), parts = [];
      if (s === 'instrument' || s === 'strategy_instrument') parts.push(sel.symbol || 'no instrument selected');
      if (s === 'strategy' || s === 'strategy_instrument') parts.push(sel.strategy || 'no strategy selected');
      text('ts-target', s === 'global' ? 'Saving applies to every instrument and strategy that has no more specific value.' : 'Saving applies to: ' + parts.join(' · '));
      return api('/api/trade-settings' + scopeQuery());
    }).then(function (j) {
      if (!j || !j.ok) return;
      tsEffective = j; tsDirty = {};
      var v = j.values, sd = (j.strategy_defaults || {})[sel.strategy];
      ['main', 'lot', 'target-dist', 'stop-dist'].forEach(function (k) {
        var map = { main: 'main_instrument', lot: 'lot', 'target-dist': 'target_distance', 'stop-dist': 'stop_distance' }, el = $('ts-' + k);
        if (el && document.activeElement !== el) el.value = v[map[k]];
      });
      $('ts-main').disabled = scope() !== 'global';
      syncStrategyGroups(v, j.sources, sd);
      var names = { main_instrument: 'Main instrument', lot: 'Lot size', target_distance: 'Target distance', stop_distance: 'Protective stop' };
      if (sel.strategy === 'market_structure') names.risk_reward = 'Risk-to-reward';
      var where = { default: 'built-in default', global: 'saved: global', instrument: 'saved: this instrument', strategy: 'saved: this strategy', strategy_instrument: 'saved: this strategy on this instrument' };
      $('ts-sources').innerHTML = Object.keys(names).map(function (k) { return '<li>' + names[k] + ': <strong>' + esc(k === 'risk_reward' ? (v[k] ? '1:' + v[k] : 'not chosen (fixed target distance)') : v[k]) + '</strong> (' + where[j.sources[k]] + ')</li>'; }).join('');
      scheduleCalc();
    });
  }
  var RR_PRESETS = ['1', '2', '3', '4', '5'];
  function syncStrategyGroups(v, sources, sd) {
    var ms = sel.strategy === 'market_structure', ch = sel.strategy === 'choch';
    if ($('ts-rr-group')) $('ts-rr-group').hidden = !ms;
    if ($('ts-choch-group')) $('ts-choch-group').hidden = !ch;
    if (ms && $('ts-rr') && document.activeElement !== $('ts-rr') && document.activeElement !== $('ts-rr-custom')) {
      var rr = v.risk_reward, key = rr === null || rr === undefined ? 'off' : (RR_PRESETS.indexOf(String(rr)) >= 0 ? String(rr) : 'custom');
      $('ts-rr').value = key; $('ts-rr-custom').value = key === 'custom' ? rr : ''; $('ts-rr-custom-field').hidden = key !== 'custom';
    }
    if (ch) text('ts-choch-note', 'Active CHoCH target: ' + (sources.target_distance === 'default' && sd !== undefined ? sd : v.target_distance) + ' pips / points from the actual entry (' + (sources.target_distance === 'default' ? 'default from settings.toml; saving a value overrides it' : 'saved') + ').');
  }
  function num(id) { var n = $(id); if (!n) return null; var t = n.value.trim().replace(',', '.'); return t === '' || !isFinite(Number(t)) ? null : Number(t); }
  function saveTradeSettings() {
    var map = { 'ts-main': 'main_instrument', 'ts-lot': 'lot', 'ts-target-dist': 'target_distance', 'ts-stop-dist': 'stop_distance' }, values = {};
    Object.keys(tsDirty).forEach(function (id) { if (!map[id]) return; var v = $(id).value.trim(); if (v !== '') values[map[id]] = id === 'ts-main' ? v : v.replace(',', '.'); });
    var s = scope(), rrOff = false;
    if (tsDirty['ts-rr'] && sel.strategy === 'market_structure') {
      var pick = $('ts-rr').value;
      if (pick === 'off') rrOff = true;
      else { var raw = pick === 'custom' ? $('ts-rr-custom').value.trim().replace(',', '.') : pick; if (pick === 'custom' && (raw === '' || !isFinite(Number(raw)) || Number(raw) <= 0)) { text('ts-msg', 'Enter a ratio above 0 (for example 2.5).'); return; } values.risk_reward = raw; }
    }
    if (!Object.keys(values).length && !rrOff) { text('ts-msg', 'Nothing changed.'); return; }
    if (rrOff && !Object.keys(values).length) {
      api('/api/trade-settings', { method: 'POST', body: { scope: s, symbol: sel.symbol, strategy: sel.strategy, reset: true, keys: ['risk_reward'] } }).then(function (j) { text('ts-msg', j.ok ? 'Saved.' : (j.message || 'That was refused.')); loadTradeSettings(); });
      return;
    }
    api('/api/trade-settings', { method: 'POST', body: { scope: s, symbol: sel.symbol, strategy: sel.strategy, values: values } }).then(function (j) {
      if (j.ok && rrOff) { api('/api/trade-settings', { method: 'POST', body: { scope: s, symbol: sel.symbol, strategy: sel.strategy, reset: true, keys: ['risk_reward'] } }).then(function () { text('ts-msg', 'Saved.'); loadTradeSettings(); }); return; }
      text('ts-msg', j.ok ? 'Saved.' : (j.message || 'That was refused.')); loadTradeSettings();
    });
  }
  function scheduleCalc() { clearTimeout(calcTimer); calcTimer = setTimeout(runCalc, 350); }
  function runCalc() {
    if (!$('ts-lot')) return;
    api('/api/calc/trade', { method: 'POST', body: { symbol: sel.symbol || undefined, volume: num('ts-lot'), target: num('ts-target-dist'), stop: num('ts-stop-dist'), max_risk: num('ts-maxrisk') } }).then(function (j) {
      var c = j.currency || '';
      if (!j.trusted) {
        ['calc-gain', 'calc-loss', 'calc-risk', 'calc-rr', 'calc-lot'].forEach(function (id) { text(id, '---'); });
        text('calc-note', j.message || 'This calculation cannot be trusted.'); return;
      }
      text('calc-gain', money(j.gain_amount, c)); text('calc-loss', money(j.loss_amount, c));
      text('calc-risk', j.risk_percent_balance == null ? '---' : j.risk_percent_balance.toFixed(2) + '% of balance / ' + (j.risk_percent_equity == null ? '---' : j.risk_percent_equity.toFixed(2) + '% of equity'));
      text('calc-rr', j.reward_to_risk == null ? '---' : '1 : ' + j.reward_to_risk.toFixed(2));
      text('calc-lot', j.allowed_lot != null ? j.allowed_lot + ' lots' : (j.allowed_lot_message ? 'none' : '---'));
      text('calc-note', [j.volume_message, j.allowed_lot_message, j.note].filter(Boolean).join(' '));
    });
  }

  /* ------------------------------------------------------------------ execution & safety */
  function renderSafety(j) {
    if (!j || !j.ok) return;
    var s = j.settings, cl = j.consecutive_losses, em = j.emergency;
    var set = function (id, v) { var n = $(id); if (n && document.activeElement !== n) { if (n.type === 'checkbox') n.checked = !!v; else n.value = v; } };
    set('ex-multi', s.allow_multiple_per_candle); set('ex-adjust', s.auto_lot_adjust); set('ex-attempts', s.lot_adjust_max_attempts); set('ex-close', s.emergency_close_positions);
    set('cl-enabled', s.consecutive_loss_enabled); set('cl-threshold', s.consecutive_loss_threshold); set('snd-enabled', s.sound_enabled);
    soundEnabled = s.sound_enabled;
    text('ex-multi-note', s.allow_multiple_per_candle ? 'ON: up to 2 entries per candle.' : 'OFF: 1 entry per candle.');
    text('cl-status', cl.enabled ? (cl.count + ' of ' + cl.threshold + ' consecutive losses.' + (cl.locked ? ' TRADING LOCKED: ' + (cl.reason || '') : '')) : 'Consecutive-loss protection is OFF (' + cl.count + ' consecutive losses counted).');
    text('em-status', em.locked ? 'EMERGENCY LOCKED since ' + new Date(em.since * 1000).toLocaleString() + ': ' + (em.reason || '') + ' No new order can be placed until you lift the lock.' : 'No emergency lock is set.');
    $('em-unlock').hidden = !em.locked;
    var chip = $('safety-lock-chip');
    if (chip) { var locked = em.locked || cl.locked; chip.textContent = em.locked ? 'EMERGENCY LOCKED' : cl.locked ? 'TRADING LOCKED' : 'Trading allowed'; chip.setAttribute('data-state', locked ? 'bad' : 'ok'); }
  }
  function loadSafety() { api('/api/safety/status').then(renderSafety); }
  function postSetting(patch) {
    api('/api/execution-settings', { method: 'POST', body: patch }).then(function (j) { text('safety-msg', j.ok ? 'Saved.' : (j.message || 'That was refused.')); loadSafety(); });
  }

  /* ------------------------------------------------------------------ hard reset */
  function loadHardReset() {
    api('/api/hard-reset').then(function (j) {
      if (!j.ok) return;
      var li = function (rows) { return rows.length ? rows.map(function (r) { return '<li>' + esc(r.file) + ': ' + esc(r.what) + '</li>'; }).join('') : '<li>nothing saved yet</li>'; };
      $('hr-plan').innerHTML = '<strong>Will be reset</strong> (backed up first):<ul>' + li(j.will_reset) + '</ul><strong>Never touched:</strong><ul>' + li(j.kept) + '</ul>';
      var ul = $('hr-backups');
      ul.innerHTML = (j.backups || []).length ? j.backups.map(function (b) { return '<li>' + esc(b.name) + ' (' + b.files.length + ' file(s)) <button type="button" class="btn btn-sm" data-restore="' + esc(b.name) + '">Restore</button></li>'; }).join('') : '<li>None.</li>';
    });
  }

  /* ------------------------------------------------------------------ sessions */
  function hhmm(iso) { return iso.slice(11, 16); }
  function loadSessions() {
    if (!$('sess-body')) return;
    api('/api/sessions').then(function (j) {
      if (!j.ok) return;
      var chip = $('sess-current');
      chip.textContent = j.current.length ? 'Open now: ' + j.current.join(' + ') + (j.overlap ? ' (overlap)' : '') : 'Between sessions'; chip.setAttribute('data-state', j.current.length ? 'ok' : 'warn');
      $('sess-body').innerHTML = j.sessions.map(function (s) {
        var mins = Math.round(s.seconds_to_change / 60), when = s.is_open ? 'closes in ' + Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm' : 'opens in ' + Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
        return '<tr><td>' + esc(s.name) + '<div class="note">' + esc(s.local_hours) + '</div></td><td>' + (s.is_open ? 'Open' : 'Closed') + '<div class="note">' + when + '</div></td><td>' + hhmm(s.open_utc) + '</td><td>' + hhmm(s.close_utc) + '</td><td>' + esc((s.pairs || []).join(', ') || '---') + '</td></tr>';
      }).join('');
      var sl = j.selected;
      text('sess-selected', sl ? 'Selected: ' + sl.symbol + ' (' + sl.asset_class.replace('_', ' ') + '): ' + sl.availability.label + (sl.availability.open_now === false ? ' · closed now' : sl.availability.open_now ? ' · available now' : '') + '. ' + sl.availability.detail : 'No instrument selected.');
      var names = { synthetic: 'Synthetic indices', forex_major: 'Forex majors', forex_jpy: 'Forex JPY', forex_other: 'Other forex', metal: 'Metals', crypto: 'Crypto', index: 'Indices', unknown: 'Other' };
      $('sess-instruments').innerHTML = Object.keys(j.instrument_groups).map(function (k) {
        return '<div><strong>' + (names[k] || k) + '</strong> (' + j.instrument_groups[k].length + '): ' + j.instrument_groups[k].map(function (s) { return s === (sl && sl.symbol) ? '<mark>' + esc(s) + '</mark>' : esc(s); }).join(', ') + '</div>';
      }).join('') || (j.symbols_known ? '' : 'Instrument list unavailable until MT5 is connected.');
      text('sess-note', j.note);
    });
  }

  /* ------------------------------------------------------------------ wiring */
  function wire() {
    if ($('ts-scope')) {
      $('ts-scope').addEventListener('change', loadTradeSettings);
      ['ts-main', 'ts-lot', 'ts-target-dist', 'ts-stop-dist'].forEach(function (id) { $(id).addEventListener('input', function () { tsDirty[id] = true; scheduleCalc(); }); });
      $('ts-maxrisk').addEventListener('input', scheduleCalc);
      $('ts-save').addEventListener('click', saveTradeSettings);
      $('ts-rr').addEventListener('change', function () { tsDirty['ts-rr'] = true; $('ts-rr-custom-field').hidden = $('ts-rr').value !== 'custom'; });
      $('ts-rr-custom').addEventListener('input', function () { tsDirty['ts-rr'] = true; });
      Array.prototype.forEach.call(document.querySelectorAll('[data-ts-preset]'), function (b) { b.addEventListener('click', function () { var t = $('ts-target-dist'); t.value = b.getAttribute('data-ts-preset'); tsDirty['ts-target-dist'] = true; scheduleCalc(); text('ts-msg', 'Press Save to keep ' + t.value + ' as the target distance.'); }); });
      $('ts-reset').addEventListener('click', function () {
        api('/api/trade-settings', { method: 'POST', body: { scope: scope(), symbol: sel.symbol, strategy: sel.strategy, reset: true } }).then(function (j) { text('ts-msg', j.ok ? 'Saved values for this scope were forgotten.' : (j.message || 'Could not reset.')); loadTradeSettings(); });
      });
    }
    if ($('ex-multi')) {
      $('ex-multi').addEventListener('change', function (e) { postSetting({ allow_multiple_per_candle: e.target.checked }); });
      $('ex-adjust').addEventListener('change', function (e) { postSetting({ auto_lot_adjust: e.target.checked }); });
      $('ex-attempts').addEventListener('change', function (e) { postSetting({ lot_adjust_max_attempts: parseInt(e.target.value, 10) }); });
      $('ex-close').addEventListener('change', function (e) { postSetting({ emergency_close_positions: e.target.checked }); });
      $('cl-enabled').addEventListener('change', function (e) { postSetting({ consecutive_loss_enabled: e.target.checked }); });
      $('cl-threshold').addEventListener('change', function (e) { postSetting({ consecutive_loss_threshold: parseInt(e.target.value, 10) }); });
      $('cl-reset').addEventListener('click', function () {
        var c = window.prompt('This resets the consecutive-loss counter to 0 and lifts a TRADING LOCKED caused by it. Your trade records are kept.\n\nType RESET to confirm:');
        if (c === null) return;
        api('/api/risk/consecutive/reset', { method: 'POST', body: { confirm: c } }).then(function (j) { text('safety-msg', j.message || ''); loadSafety(); });
      });
      $('em-unlock').addEventListener('click', function () {
        var c = window.prompt('This lifts the emergency lock so orders can be approved again. Autopilot stays stopped until you start it.\n\nType UNLOCK to confirm:');
        if (c === null) return;
        api('/api/safety/unlock', { method: 'POST', body: { confirm: c } }).then(function (j) { text('safety-msg', j.message || ''); loadSafety(); });
      });
    }
    if ($('snd-enabled')) {
      $('snd-enabled').addEventListener('change', function (e) { soundEnabled = e.target.checked; postSetting({ sound_enabled: e.target.checked }); });
      $('snd-test').addEventListener('click', function () {
        playChime(true).then(function (r) { text('snd-msg', r === 'played' ? 'The chime played. If you heard nothing, check your device volume.' : 'The browser blocked the sound. Click or type anywhere on the page once, then press Test again.'); });
      });
    }
    if ($('hr-open')) {
      $('hr-open').addEventListener('click', function () { $('hr-confirm').hidden = false; $('hr-phrase').value = ''; $('hr-phrase').focus(); });
      $('hr-cancel').addEventListener('click', function () { $('hr-confirm').hidden = true; });
      $('hr-go').addEventListener('click', function () {
        api('/api/hard-reset', { method: 'POST', body: { confirm: $('hr-phrase').value.trim() } }).then(function (j) {
          text('hr-msg', j.message || ''); if (j.ok) { $('hr-confirm').hidden = true; loadHardReset(); loadTradeSettings(); loadSafety(); }
        });
      });
      $('hr-backups').addEventListener('click', function (e) {
        var name = e.target && e.target.getAttribute && e.target.getAttribute('data-restore');
        if (!name || !window.confirm('Restore the settings saved in ' + name + '? Your current settings are backed up first.')) return;
        api('/api/hard-reset/restore', { method: 'POST', body: { name: name } }).then(function (j) { text('hr-msg', j.message || ''); loadHardReset(); loadTradeSettings(); loadSafety(); });
      });
    }
  }

  function refreshVisible() {
    if (visible('panel-trade-settings')) { if (!document.activeElement || !/INPUT|SELECT/.test(document.activeElement.tagName)) loadTradeSettings(); }
    if (visible('panel-safety')) loadSafety();
    if (visible('panel-hardreset')) loadHardReset();
  }
  wire();
  pollNotifs(); setInterval(pollNotifs, 4000);
  loadSafety(); loadSessions(); setInterval(loadSessions, 30000);
  window.TradeMateRouter.on(function () { setTimeout(refreshVisible, 50); });
  setTimeout(refreshVisible, 300); setInterval(function () { if (visible('panel-safety')) loadSafety(); }, 6000);
  window.TradeMateSafety = { playChime: playChime, pollNotifs: pollNotifs, _state: function () { return { lastSeq: lastSeq, shown: shown.length }; } };
})();
