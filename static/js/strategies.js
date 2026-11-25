/* TradeMate Strategy hub: six independent strategies as cards, built only from real data.
   The five catalogue strategies: /api/selection (catalogue, selected strategy, instrument, timeframe), /api/trade-settings (their saved / default
   lot, target and stop distances), /api/decision/current (the selected strategy's latest decision). RAPID FIRE: /api/rapid-fire/status.
   "Select" uses the same call the Command Centre uses (POST /api/selection). Nothing is shown that the backend did not report. */
(function () {
  'use strict';
  var REFRESH_MS = 6000, DASH = '---';
  var grid = document.getElementById('strat-grid'); if (!grid) return;
  var chip = document.getElementById('hub-chip'), msg = document.getElementById('hub-msg');
  var busy = false;

  function visible() { var v = document.getElementById('view-strategy'); return !!v && !v.hidden && !document.hidden; }
  function getJSON(url, opt) { return fetch(url, opt || { cache: 'no-store' }).then(function (r) { return r.json(); }).catch(function () { return null; }); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function row(dl, k, v) { var d = el('div'); d.appendChild(el('dt', null, k)); d.appendChild(el('dd', null, v == null || v === '' ? DASH : String(v))); dl.appendChild(d); }
  var open = {};                                              // which "what is this?" notes are expanded (kept across refreshes)

  function card(o) {
    var c = el('article', 'strat-card'); c.setAttribute('data-strategy', o.id); c.setAttribute('data-state', o.state);
    var head = el('div', 'strat-head');
    var title = el('h3', 'strat-name', o.name);
    var info = el('button', 'tc-info', 'i'); info.type = 'button'; info.setAttribute('aria-label', 'About ' + o.name);
    info.setAttribute('aria-expanded', open[o.id] ? 'true' : 'false');
    head.appendChild(title); head.appendChild(info);
    var st = el('span', 'tc-chip', o.label); st.setAttribute('data-tone', o.tone); head.appendChild(st);
    c.appendChild(head);
    c.appendChild(el('p', 'strat-summary', o.summary));
    var about = el('p', 'note strat-about', o.about); about.hidden = !open[o.id]; c.appendChild(about);
    info.addEventListener('click', function () { open[o.id] = !open[o.id]; about.hidden = !open[o.id]; info.setAttribute('aria-expanded', open[o.id] ? 'true' : 'false'); });
    var dl = el('dl', 'kv kv-rows strat-kv');
    o.rows.forEach(function (r) { row(dl, r[0], r[1]); });
    c.appendChild(dl);
    var foot = el('div', 'strat-foot');
    (o.actions || []).forEach(function (a) { foot.appendChild(a); });
    c.appendChild(foot);
    return c;
  }

  function link(text, href, view, primary) {
    var a = el('a', 'btn btn-sm' + (primary ? ' btn-primary' : ''), text); a.href = href; if (view) a.setAttribute('data-view', view); return a;
  }
  function button(text, fn, primary) {
    var b = el('button', 'btn btn-sm' + (primary ? ' btn-primary' : ''), text); b.type = 'button'; b.addEventListener('click', fn); return b;
  }

  function select(id) {
    msg.textContent = 'Selecting...';
    getJSON('/api/selection', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ strategy: id }) }).then(function (j) {
      msg.textContent = j && j.ok ? 'Selected. The Command Centre now uses this strategy.' : ((j && j.message) || 'The selection could not be saved.');
      refresh(true);
    });
  }

  function refresh(force) {
    if (busy || (!force && !visible())) return;
    busy = true;
    getJSON('/api/selection').then(function (sel) {
      var jobs = [getJSON('/api/rapid-fire/status')];
      if (sel && sel.ok && sel.selected_id) {
        jobs.push(getJSON('/api/decision/current?symbol=' + encodeURIComponent(sel.symbol || '') + '&strategy=' + encodeURIComponent(sel.strategy_id) + '&timeframe=' + encodeURIComponent(sel.timeframe || '')));
        jobs.push(getJSON('/api/trade-settings?symbol=' + encodeURIComponent(sel.symbol || '') + '&strategy=' + encodeURIComponent(sel.strategy_id)));
      }
      return Promise.all(jobs).then(function (r) { render(sel, r[0], r[1], r[2]); });
    }).then(function () { busy = false; }, function () { busy = false; });
  }

  function render(sel, rf, dec, ts) {
    var cards = [];
    if (!(sel && sel.ok)) {
      grid.replaceChildren(el('p', 'note is-bad', 'The strategy catalogue could not be read from the TradeMate server.'));
      chip.textContent = 'Unavailable'; chip.setAttribute('data-tone', 'bad'); return;
    }
    (sel.strategies || []).forEach(function (s) {
      var isSel = s.id === sel.selected_id;
      var rec = isSel && dec && dec.ok ? dec.record : null;
      var cfg = isSel && ts && ts.ok ? ts.values : null;
      var rows = [
        ['Instrument', isSel ? sel.symbol : 'set when selected'],
        ['Timeframe', isSel ? sel.timeframe : 'set when selected'],
        ['Configuration', cfg ? 'lot ' + cfg.lot + ' · target ' + cfg.target_distance + ' · stop ' + cfg.stop_distance + (s.id === 'candle_momentum' ? ' (own engine settings in the Command Centre)' : '') : 'saved per instrument in Settings'],
        ['Recent activity', !isSel ? 'only the selected strategy is monitored' : (rec ? ((rec.decision || rec.state || 'no decision') + (typeof rec.age_seconds === 'number' ? ' · ' + Math.round(rec.age_seconds) + 's ago' : '')) : (dec && dec.monitoring ? 'monitoring, no decision yet' : 'no decision recorded yet'))],
        ['Performance', 'not tracked per strategy']
      ];
      var acts = isSel ? [link('Open Command Centre', '#strategylab', 'strategylab', true)] : [button('Select', function () { select(s.id); }, true), link('Command Centre', '#strategylab', 'strategylab')];
      cards.push(card({ id: s.id, name: s.name, summary: s.summary, about: s.tooltip || s.description, state: isSel ? 'active' : 'inactive',
                        label: isSel ? 'Selected' : 'Not selected', tone: isSel ? 'ok' : 'idle', rows: rows, actions: acts }));
    });
    // RAPID FIRE: its own engine
    if (rf && rf.ok) {
      var c = rf.config || {}, p = rf.pnl || {}, k = rf.candle || {}, at = rf.anti_twin || {};
      var tone = rf.state === 'RUNNING' ? 'ok' : rf.state === 'BLOCKED' ? 'bad' : rf.state === 'NOT_CONFIGURED' ? 'warn' : 'idle';
      var perf = typeof p.closed_count === 'number' && p.closed_count > 0 ? p.closed_count + ' closed · net ' + (typeof p.realised_net === 'number' ? p.realised_net.toFixed(2) : DASH) + (typeof p.wins === 'number' ? ' · win ' + (p.wins / p.closed_count * 100).toFixed(0) + '%' : '') : 'no closed Rapid Fire trades yet';
      cards.push(card({ id: 'rapid_fire', name: 'RAPID FIRE', summary: 'Beauty in simplicity: quick entries, quick exits, repeated small captures.',
        about: 'An independent M1 engine. It enters on a small move of price from the candle open, takes profit a fixed number of points from the fill, allows at most the configured entries per candle, and has no conventional stop-loss: exits are software-managed. Every order still passes the central Risk Engine.',
        state: rf.running ? 'active' : 'inactive', label: String(rf.state || 'STOPPED').replace('_', ' '), tone: tone,
        rows: [['Instrument', c.symbol], ['Timeframe', c.timeframe],
               ['Configuration', c.trigger_points != null ? 'trigger ' + c.trigger_points + ' pts · target ' + c.target_points + ' pts · ' + c.max_entries_per_candle + ' per candle · lot ' + (c.lot == null ? 'not set' : c.lot) : null],
               ['Entries this candle', k.shots_used != null ? k.shots_used + ' used, ' + k.shots_remaining + ' left' : null],
               ['Recent activity', (rf.last_execution && rf.last_execution.message) || rf.state_reason || 'none yet'],
               ['Anti-Twin', at.status], ['Performance', perf]],
        actions: [button('Configure below', function () { var p2 = document.getElementById('rf-panel'); if (p2) p2.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, true)] }));
    } else {
      cards.push(card({ id: 'rapid_fire', name: 'RAPID FIRE', summary: 'Beauty in simplicity: quick entries, quick exits, repeated small captures.',
        about: 'An independent M1 engine with its own panel.', state: 'unavailable', label: 'Unavailable', tone: 'warn',
        rows: [['Status', 'Rapid Fire status could not be read from the server']], actions: [] }));
    }
    grid.replaceChildren.apply(grid, cards);
    var running = (sel.selected_id ? 1 : 0) + (rf && rf.running ? 1 : 0);
    chip.textContent = (sel.selected ? sel.selected.name : 'No strategy') + (rf && rf.running ? ' + Rapid Fire' : '');
    chip.setAttribute('data-tone', running ? 'ok' : 'idle');
  }

  window.TradeMateRouter.on(function (r) { if (r.name === 'strategy') refresh(true); });
  setInterval(refresh, REFRESH_MS);
  document.addEventListener('visibilitychange', function () { refresh(false); });
  window.TradeMateStrategies = { refresh: function () { refresh(true); } };
  refresh(false);
})();
