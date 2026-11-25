/* TradeMate Rapid Fire panel. Real controls only, bound to /api/rapid-fire/*. Every figure comes from the server; nothing is computed here
   beyond display formatting. Editing a field is never overwritten by a refresh while the form has focus. */
(function () {
  'use strict';
  var panel = document.getElementById('rf-panel'); if (!panel) return;
  var DASH = '---', REFRESH_MS = 2000;
  function $(id) { return document.getElementById(id); }
  function set(id, t) { var e = $(id); if (e && e.textContent !== t) e.textContent = t; }
  function num(v, d) { return typeof v === 'number' && isFinite(v) ? v.toFixed(d == null ? 2 : d) : DASH; }
  function signed(v, c) { return typeof v === 'number' && isFinite(v) ? (v > 0 ? '+' : '') + v.toFixed(2) + (c ? ' ' + c : '') : DASH; }
  function post(url, body) {
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })
      .then(function (r) { return r.json(); }).catch(function () { return { ok: false, message: 'TradeMate could not be reached.' }; });
  }
  function getJSON(url) { return fetch(url, { cache: 'no-store' }).then(function (r) { return r.json(); }).catch(function () { return null; }); }
  function visible() { var v = $('view-strategy'); return !!v && !v.hidden && !document.hidden; }
  var form = $('rf-form'), filled = false, last = null;

  function fillForm(c) {
    var map = { 'rf-symbol': 'symbol', 'rf-trigger': 'trigger_points', 'rf-target': 'target_points', 'rf-max': 'max_entries_per_candle', 'rf-lot': 'lot', 'rf-emergency': 'emergency_points', 'rf-spread': 'max_spread_points', 'rf-profit-fire': 'profit_per_fire', 'rf-comm-lot': 'commission_per_lot' };
    Object.keys(map).forEach(function (id) { var v = c[map[id]]; $(id).value = v == null ? '' : v; });
    $('rf-at-enabled').checked = !!c.anti_twin_enabled;
    filled = true;
  }

  /* the take-profit plan: exactly what the server calculated from the broker's data (or why it could not) */
  function planText(p) {
    var e = $('rf-plan'); if (!e) return;
    if (!p) { e.textContent = 'Take-profit plan: not available.'; return; }
    if (!p.ok) { e.textContent = 'Take-profit plan: ' + (p.problem || 'cannot be calculated.'); e.classList.add('is-warn'); return; }
    e.classList.remove('is-warn');
    if (p.mode !== 'money') { e.textContent = 'Take-profit plan: fixed distance of ' + p.required_points + ' points (no profit per fire set).'; return; }
    var t = 'Take-profit plan: ' + num(p.profit_per_fire, 2) + ' ' + (p.currency || '') + ' net at ' + p.lot + ' lot = ' + num(p.required_points, 1) + ' points from the fill (' + p.required_price_distance + ' in price, ' + p.required_ticks + ' tick(s)).';
    if (p.spread_points != null) t += ' Spread now ' + num(p.spread_points, 1) + ' points, already inside that distance.';
    if (p.buy_tp_price != null) t += ' A buy now would target ' + p.buy_tp_price + ', a sell ' + p.sell_tp_price + '.';
    e.textContent = t + ' ' + (p.commission_note || '') + ' ' + (p.target_label || '') + ((p.warnings || []).length ? ' WARNING: ' + p.warnings.join(' ') : '');
  }

  function render(s) {
    last = s;
    if (!s || !s.ok) { set('rf-state', 'Unavailable'); $('rf-state').setAttribute('data-tone', 'warn'); set('rf-reason', 'Rapid Fire status could not be read from the server.'); return; }
    var tone = s.state === 'RUNNING' ? 'ok' : s.state === 'BLOCKED' ? 'bad' : s.state === 'NOT_CONFIGURED' ? 'warn' : 'idle';
    set('rf-state', s.state.replace('_', ' ')); $('rf-state').setAttribute('data-tone', tone);
    set('rf-reason', s.state_reason || '');
    var w = $('rf-warnings'); w.hidden = !(s.warnings && s.warnings.length); w.textContent = (s.warnings || []).join(' ');
    $('rf-start').disabled = !!s.running; $('rf-stop').disabled = !s.running;
    $('rf-symbol').disabled = !!s.running;
    if (!filled || !form.contains(document.activeElement) && !form.getAttribute('data-dirty')) fillForm(s.config);
    var i = s.instrument || {}, k = s.candle || {}, p = s.pnl || {}, at = s.anti_twin || {};
    set('rf-instrument', (i.broker_symbol || s.config.symbol) + ' · M1');
    set('rf-quote', i.bid != null ? 'bid ' + i.bid + ' / ask ' + i.ask + (i.spread_points != null ? ' · spread ' + i.spread_points + ' pts' : '') : (i.problem || 'no quote'));
    set('rf-shots', k.shots_used != null ? k.shots_used + ' of ' + k.max + ' used' : DASH);
    set('rf-ref', k.reference != null ? 'reference ' + k.reference + ' (' + k.reference_kind + '), ' + k.shots_remaining + ' left' : 'waiting for a candle');
    set('rf-net', p.realised_net == null ? 'n/a' : signed(p.realised_net, p.currency));
    var n = $('rf-net'); n.classList.remove('pl-pos', 'pl-neg', 'pl-zero'); n.classList.add(p.realised_net == null || Math.abs(p.realised_net) < .005 ? 'pl-zero' : (p.realised_net > 0 ? 'pl-pos' : 'pl-neg'));
    set('rf-net-sub', p.closed_count ? p.closed_count + ' closed · ' + p.wins + ' won · ' + p.losses + ' lost' + (p.floating != null ? ' · floating ' + signed(p.floating, p.currency) : '') : 'no closed Rapid Fire trades yet');
    planText(s.target_plan);
    set('rf-at', at.status || DASH); set('rf-at-sub', at.message || '');
    var m = $('rf-metrics'); m.replaceChildren();
    [['Point value per lot', i.money_per_point_per_lot == null ? 'unknown (broker data missing)' : num(i.money_per_point_per_lot, 4) + ' ' + (i.currency || '')],
     ['Volume min / step / max', i.volume_min != null ? i.volume_min + ' / ' + i.volume_step + ' / ' + i.volume_max : DASH],
     ['Estimated gross per shot', i.estimated_profit_per_shot == null ? 'set a lot to estimate' : num(i.estimated_profit_per_shot, 2) + ' ' + (i.currency || '')],
     ['Estimated loss at emergency exit', i.estimated_loss_at_emergency == null ? 'set lot and emergency exit' : num(i.estimated_loss_at_emergency, 2) + ' ' + (i.currency || '')]]
      .forEach(function (r) { var d = document.createElement('div'), a = document.createElement('dt'), b = document.createElement('dd'); a.textContent = r[0]; b.textContent = r[1]; d.appendChild(a); d.appendChild(b); m.appendChild(d); });
    var body = $('rf-pos'); body.replaceChildren();
    if (!s.positions.length) { var tr = document.createElement('tr'); tr.className = 'empty-row'; var td = document.createElement('td'); td.colSpan = 8; td.textContent = 'None'; tr.appendChild(td); body.appendChild(tr); }
    s.positions.forEach(function (t) {
      var tr = document.createElement('tr');
      [t.role === 'anti_twin' ? 'ANTI-TWIN' : 'ORIGINAL', String(t.side || '').toUpperCase(), num(t.volume, 2), t.entry_price == null ? DASH : String(t.entry_price), t.target_price == null ? DASH : String(t.target_price), t.emergency_price == null ? DASH : String(t.emergency_price), String(t.status).replace(/_/g, ' ') + (t.twin_state ? ' · twin ' + String(t.twin_state).replace(/_/g, ' ') : '')]
        .forEach(function (v, j) { var c = document.createElement('td'); if (j >= 2 && j <= 5) c.className = 'num'; c.textContent = v; tr.appendChild(c); });
      var act = document.createElement('td');
      if (t.status === 'ENTRY_UNCERTAIN') {
        [['It filled', 'filled'], ['It did not fill', 'not_filled']].forEach(function (o) {
          var b = document.createElement('button'); b.type = 'button'; b.className = 'btn btn-sm'; b.textContent = o[0];
          b.addEventListener('click', function () {
            var ticket = null;
            if (o[1] === 'filled') { var v = window.prompt('MT5 position ticket number:'); if (v === null) return; ticket = parseInt(v, 10); if (!(ticket > 0)) { set('rf-msg', 'A ticket number is required.'); return; } }
            post('/api/rapid-fire/trades/' + encodeURIComponent(t.id) + '/resolve', { outcome: o[1], ticket: ticket }).then(function (j) { set('rf-msg', j.message || ''); refresh(true); });
          });
          act.appendChild(b);
        });
      }
      tr.appendChild(act); body.appendChild(tr);
    });
    var le = s.last_execution; set('rf-last', le ? (le.message || JSON.stringify(le)) : 'No Rapid Fire order has been sent yet.');
    var ev = $('rf-events'); ev.replaceChildren();
    (s.events || []).slice().reverse().forEach(function (e) { var li = document.createElement('li'); li.textContent = (e.event || e.kind || 'event') + (e.message ? ': ' + e.message : ''); ev.appendChild(li); });
  }

  var busy = false;
  function refresh(force) {
    if ((busy || !visible()) && !force) return;
    busy = true;
    getJSON('/api/rapid-fire/status').then(render).then(function () { busy = false; }, function () { busy = false; });
  }

  form.addEventListener('input', function () { form.setAttribute('data-dirty', '1'); set('rf-save-msg', ''); });
  function save() {
    function val(id) { var v = $(id).value.trim(); return v === '' ? null : Number(v); }
    var body = { trigger_points: val('rf-trigger'), target_points: val('rf-target'), max_entries_per_candle: val('rf-max'), lot: val('rf-lot'), emergency_points: val('rf-emergency'),
                 max_spread_points: val('rf-spread'), anti_twin_enabled: $('rf-at-enabled').checked,
                 profit_per_fire: val('rf-profit-fire'), commission_per_lot: val('rf-comm-lot') || 0 };
    if (!$('rf-symbol').disabled) body.symbol = $('rf-symbol').value.trim();
    post('/api/rapid-fire/config', body).then(function (j) {
      set('rf-save-msg', j.ok ? 'Saved. ' + (j.note || '') : ((j.problems && j.problems.join(' ')) || j.message || 'Not saved.'));
      $('rf-save-msg').className = 'note' + (j.ok ? '' : ' is-error');
      if (j.ok) { form.removeAttribute('data-dirty'); fillForm(j.config); }
      refresh(true);
    });
  }
  $('rf-save').addEventListener('click', save);
  form.addEventListener('keydown', function (e) { if (e.key === 'Enter' && e.target && e.target.tagName === 'INPUT' && e.target.type !== 'checkbox') { e.preventDefault(); save(); } });
  $('rf-start').addEventListener('click', function () { post('/api/rapid-fire/start').then(function (j) { set('rf-msg', j.message || ''); refresh(true); }); });
  $('rf-stop').addEventListener('click', function () { post('/api/rapid-fire/stop').then(function (j) { set('rf-msg', j.message || ''); refresh(true); }); });
  $('rf-suggest').addEventListener('click', function () {
    var p = $('rf-profit').value.trim();
    getJSON('/api/rapid-fire/lot-suggestion?profit=' + encodeURIComponent(p)).then(function (j) {
      if (!j) { set('rf-suggest-out', 'TradeMate could not be reached.'); return; }
      set('rf-suggest-out', j.ok ? 'Suggested lot ' + j.lot + ': estimated ' + num(j.estimated_profit, 2) + ' ' + j.currency + ' gross at ' + j.target_points + ' points. ' + (j.note || '') : (j.reason || j.message || 'No suggestion.'));
      if (j.ok) { $('rf-lot').value = j.lot; form.setAttribute('data-dirty', '1'); }
    });
  });

  window.TradeMateRouter.on(function (r) { if (r.name === 'strategy') refresh(true); });
  setInterval(refresh, REFRESH_MS);
  window.TradeMateRapidFire = { refresh: function () { refresh(true); } };
  refresh(false);
})();
