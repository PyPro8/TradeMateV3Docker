/* TradeMate Analysis page. Shows what the Market Structure Engine determined for one symbol and timeframe, from /api/analysis.
   Read-only: no orders, no trade signals. Every value shown comes from the server's answer for the CURRENT symbol/timeframe;
   an answer for an older selection is dropped. All server text is placed with textContent (no HTML is ever built from it). */
(function () {
  'use strict';

  var FALLBACK_TFS = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1'];
  var REFRESH_MS = 15000;
  var view = document.getElementById('view-analysis');
  if (!view) return;

  var $ = function (id) { return document.getElementById(id); };
  var sel = { symbol: null, tf: null };
  var seq = 0, ctl = null, timer = null, last = null, tfs = FALLBACK_TFS.slice();

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
  function para(parent, text, cls) { parent.appendChild(el('p', cls || 'an-p', text)); }
  function bullets(parent, items) {
    var ul = el('ul', 'an-list'); items.forEach(function (t) { ul.appendChild(el('li', null, t)); }); parent.appendChild(ul);
  }
  function app() { return window.TradeMateApp || null; }
  function visible() { return !view.hidden; }

  /* ---------- selection controls ---------- */
  function fillSymbols() {
    var a = app(), s = $('an-symbol'); if (!a) return;
    var names = a.symbols(), want = sel.symbol || a.selection().symbol;
    if (want && names.indexOf(want) < 0) names.unshift(want);
    var key = names.join('|');
    if (s.getAttribute('data-key') !== key) {
      clear(s);
      names.forEach(function (n) { var o = el('option', null, n); o.value = n; s.appendChild(o); });
      s.setAttribute('data-key', key);
    }
    if (want) { s.value = want; sel.symbol = want; }
  }
  function fillTfs() {
    var box = $('an-tfs'); clear(box);
    tfs.forEach(function (t) {
      var b = el('button', 'btn btn-sm an-tf' + (t === sel.tf ? ' btn-primary' : ''), t); b.type = 'button'; b.setAttribute('data-tf', t);   // btn-primary keeps the pressed state readable in both themes
      b.setAttribute('aria-pressed', String(t === sel.tf));
      b.addEventListener('click', function () { sel.tf = t; fillTfs(); load(); });
      box.appendChild(b);
    });
  }

  /* ---------- rendering ---------- */
  function renderStatus(a) {
    var st = a.status || { code: 'UNAVAILABLE', reason: a.message || 'No answer.', meaning: '' };
    $('an-status').setAttribute('data-code', st.code);
    $('an-status-code').textContent = st.code;
    $('an-status-reason').textContent = st.reason || '';
    $('an-status-meaning').textContent = st.meaning || '';
  }

  function renderContext(a) {
    var box = $('an-context'); clear(box);
    var c = a.context || {};
    kv(box, 'Symbol', a.symbol); kv(box, 'Analysis timeframe', a.timeframe);
    kv(box, 'Supported timeframes', (a.supported_timeframes || []).join(', ') || 'not reported');
    if (c.latest_candle && c.latest_candle.time != null) {
      kv(box, 'Latest candle', (c.latest_candle.label || String(c.latest_candle.time)) + '  (MT5 server time)');
      kv(box, 'Newest candle state', c.latest_candle.state === 'forming' ? 'Treated as still forming' : String(c.latest_candle.state));
    } else { kv(box, 'Latest candle', 'not available'); }
    kv(box, 'Candles analysed', c.candles_used != null ? c.candles_used + (c.candles_received != null && c.candles_received !== c.candles_used ? ' of ' + c.candles_received + ' received' : '') : 'none');
    var d = c.data || {};
    var fresh = { live_feed: 'Live feed from MT5', stale_feed: 'STALE: MT5 stopped updating', unavailable: 'UNAVAILABLE' }[d.freshness] || d.freshness || 'unknown';
    kv(box, 'Data feed', fresh, d.freshness === 'live_feed' ? '' : 'an-bad');
    kv(box, 'Data completeness', ({ complete: 'Complete', insufficient: 'INSUFFICIENT', invalid: 'INVALID', unavailable: 'UNAVAILABLE' })[d.completeness] || String(d.completeness || 'unknown'),
       d.completeness === 'complete' ? '' : 'an-bad');
    if (d.candle_clock_note) para(box, d.candle_clock_note, 'an-note');
    if (c.time_note) para(box, c.time_note, 'an-note');
    (d.warnings || []).forEach(function (w) { para(box, 'Warning: ' + w, 'an-warn'); });
  }

  function renderStructure(a) {
    var box = $('an-structure'); clear(box);
    var s = a.structure;
    if (!s) { para(box, 'Not analysed: ' + ((a.status && a.status.reason) || 'no data') , 'an-note'); return; }
    if (s.trend) {
      kv(box, 'Trend (confirmed candles)', s.trend.confirmed);
      if (s.trend.including_developing !== s.trend.confirmed) kv(box, 'If the forming candle closes as is', s.trend.including_developing, 'an-warn');
      para(box, s.trend.note, 'an-note');
    }
    kv(box, 'Break method', s.break_mode === 'wick' ? 'Wick (high/low)' : 'Candle close');
    var al = s.active_levels || {};
    kv(box, 'Unbroken swing high', al.high ? al.high.price_text + '  (' + (al.high.pivot && al.high.pivot.label || '') + ')' : 'none');
    kv(box, 'Unbroken swing low', al.low ? al.low.price_text + '  (' + (al.low.pivot && al.low.pivot.label || '') + ')' : 'none');
    var h = el('h3', 'an-h3', 'Latest confirmed swings'); box.appendChild(h);
    var table = el('table', 'an-table'); var thead = el('thead'); var hr = el('tr');
    ['Type', 'Price', 'Label', 'Formed', 'State'].forEach(function (t) { hr.appendChild(el('th', null, t)); }); thead.appendChild(hr); table.appendChild(thead);
    var tb = el('tbody');
    (s.swings || []).slice().reverse().forEach(function (w) {
      var tr = el('tr');
      [w.kind === 'high' ? 'Swing high' : 'Swing low', w.price_text, w.classification || 'first of its kind', (w.pivot && w.pivot.label) || '', w.confirmed ? 'confirmed' : 'forming'].forEach(function (t) { tr.appendChild(el('td', null, t)); });
      tb.appendChild(tr);
    });
    table.appendChild(tb); box.appendChild(table);
  }

  function renderEvents(a) {
    var box = $('an-events'); clear(box);
    var s = a.structure;
    if (!s) { para(box, 'No structural events: the analysis did not run (see the status above).', 'an-note'); return; }
    if (!s.recent_events || !s.recent_events.length) { para(box, 'No break of a confirmed swing has occurred in these candles.', 'an-note'); return; }
    s.recent_events.forEach(function (e, i) {
      var card = el('details', 'an-event an-event-' + e.state); if (i === 0) card.open = true;
      var sum = el('summary');
      sum.appendChild(el('strong', null, (e.type === 'CHOCH' ? 'CHoCH' : 'BOS') + ' · ' + e.direction));
      sum.appendChild(el('span', 'an-pill an-pill-' + e.state, e.state === 'confirmed' ? 'CONFIRMED' : 'DEVELOPING'));
      sum.appendChild(el('span', 'an-ev-meta', 'level ' + e.level_text + ' · ' + e.candle.label + ' · ' + e.method));
      card.appendChild(sum);
      var body = el('div', 'an-event-body');
      (e.explanation.lines || []).forEach(function (l) { para(body, l); });
      var row = el('div', 'an-event-actions');
      var b1 = el('button', 'btn btn-sm', 'Show on chart'); b1.type = 'button';
      b1.addEventListener('click', function () { if (app()) app().openChart(a.symbol, a.timeframe, { eventId: e.id }); });
      var b2 = el('button', 'btn btn-sm', 'Show on chart with structure overlay'); b2.type = 'button';
      b2.addEventListener('click', function () { if (app()) app().openChart(a.symbol, a.timeframe, { eventId: e.id, showStructure: true }); });
      row.appendChild(b1);
      if (!(app() && app().structureVisible())) row.appendChild(b2);
      body.appendChild(row);
      if (!(app() && app().structureVisible())) para(body, 'The structure overlay is switched off in your chart settings, so the chart will not draw this event unless you use the second button.', 'an-note');
      card.appendChild(body); box.appendChild(card);
    });
  }

  /* ---------- liquidity (Phase 7B) ---------- */
  function chartButtons(parent, a, id) {
    var row = el('div', 'an-event-actions');
    var b1 = el('button', 'btn btn-sm', 'Show on chart'); b1.type = 'button';
    b1.addEventListener('click', function () { if (app()) app().openChart(a.symbol, a.timeframe, { eventId: id }); });
    row.appendChild(b1);
    if (!(app() && app().liquidityVisible && app().liquidityVisible())) {
      var b2 = el('button', 'btn btn-sm', 'Show on chart with liquidity overlay'); b2.type = 'button';
      b2.addEventListener('click', function () { if (app()) app().openChart(a.symbol, a.timeframe, { eventId: id, showLiquidity: true }); });
      row.appendChild(b2);
    }
    parent.appendChild(row);
  }
  function liqCard(a, item, kind) {
    var card = el('details', 'an-event an-event-' + (kind === 'sweep' ? item.state : (item.state === 'ACTIVE' ? 'confirmed' : 'developing')));
    var sum = el('summary');
    if (kind === 'sweep') {
      sum.appendChild(el('strong', null, (item.side === 'BSL' ? 'Buy-side' : 'Sell-side') + ' sweep'));
      sum.appendChild(el('span', 'an-pill an-pill-' + item.state, item.state === 'confirmed' ? 'CONFIRMED' : 'DEVELOPING'));
      sum.appendChild(el('span', 'an-ev-meta', 'level ' + item.level_text + ' \u00b7 ' + item.candle.label + ' \u00b7 ' + item.mode));
    } else {
      sum.appendChild(el('strong', null, (item.side === 'BSL' ? 'Buy-side' : 'Sell-side') + ' \u00b7 ' + item.kind_label));
      sum.appendChild(el('span', 'an-pill an-pill-' + (item.state === 'ACTIVE' ? 'confirmed' : 'developing'), item.state + (item.provisional ? ' (PROVISIONAL)' : '')));
      sum.appendChild(el('span', 'an-ev-meta', 'level ' + item.level_text + ' \u00b7 ' + item.member_count + ' swing(s)'));
    }
    card.appendChild(sum);
    var body = el('div', 'an-event-body');
    (item.explanation.lines || []).forEach(function (l) { para(body, l); });
    chartButtons(body, a, item.id);
    card.appendChild(body);
    return card;
  }
  function liqGroup(box, a, title, hint, items, kind) {
    var sec = el('div', 'an-sec'); sec.appendChild(el('h3', 'an-h3', title + ' (' + items.length + ')'));
    if (hint) para(sec, hint, 'an-note');
    if (!items.length) para(sec, 'None.', 'an-note');
    items.forEach(function (it) { sec.appendChild(liqCard(a, it, kind)); });
    box.appendChild(sec);
  }
  function renderLiquidity(a) {
    var box = $('an-liquidity'); clear(box);
    var l = a.liquidity;
    if (!l || !l.available) { para(box, 'Liquidity not analysed: ' + ((l && l.reason) || 'no data') + '.', 'an-note'); return; }
    para(box, l.note, 'an-note');
    var c = l.config || {};
    kv(box, 'Equal highs/lows tolerance', c.tolerance_ticks + ' ticks (tick size ' + c.tick_size + ')');
    kv(box, 'A sweep needs', c.sweep_mode === 'close' ? 'a candle to close beyond the level' : 'price to trade through the level (wick)');
    var h = l.hidden_pools || {};
    if (h.BSL || h.SSL) para(box, 'More pools exist than the per-side limit shows (buy-side hidden: ' + (h.BSL || 0) + ', sell-side hidden: ' + (h.SSL || 0) + ').', 'an-note');
    liqGroup(box, a, 'Active buy-side liquidity (BSL)', 'Above swing highs and equal highs that price has not crossed.', l.bsl, 'pool');
    liqGroup(box, a, 'Active sell-side liquidity (SSL)', 'Below swing lows and equal lows that price has not crossed.', l.ssl, 'pool');
    liqGroup(box, a, 'Equal highs / equal lows pools', 'Pools made of two or more swings within the tolerance. Also listed above.', l.equal, 'pool');
    liqGroup(box, a, 'Confirmed sweeps', 'The crossing candle has closed.', l.confirmed_sweeps, 'sweep');
    if (l.provisional_sweeps.length) liqGroup(box, a, 'Provisional sweeps (forming candle)', 'These can disappear when the candle closes back inside the level.', l.provisional_sweeps, 'sweep');
  }

  /* ---------- fair value gaps and order blocks (Phase 7C): the engines' own results, empty states and error states ---------- */
  function zoneButtons(parent, a, overlayVisible, showOpts, label) {
    var row = el('div', 'an-event-actions');
    var b1 = el('button', 'btn btn-sm', 'Show on chart'); b1.type = 'button';
    b1.addEventListener('click', function () { if (app()) app().openChart(a.symbol, a.timeframe, {}); });
    row.appendChild(b1);
    if (!overlayVisible) {
      var b2 = el('button', 'btn btn-sm', 'Show on chart with ' + label + ' overlay'); b2.type = 'button';
      b2.addEventListener('click', function () { if (app()) app().openChart(a.symbol, a.timeframe, showOpts); });
      row.appendChild(b2);
    }
    parent.appendChild(row);
  }
  function gapCard(a, g) {
    var card = el('details', 'an-event an-event-' + g.state);
    var sum = el('summary');
    sum.appendChild(el('strong', null, (g.direction === 'bullish' ? 'Bullish' : 'Bearish') + ' fair value gap'));
    sum.appendChild(el('span', 'an-pill an-pill-' + g.state, g.confirmed ? 'CONFIRMED' : 'DEVELOPING'));
    sum.appendChild(el('span', 'an-ev-meta', g.lower_text + ' \u2013 ' + g.upper_text + ' \u00b7 candle 3 ' + ((g.third && g.third.label) || '')));
    card.appendChild(sum);
    var body = el('div', 'an-event-body');
    ((g.explanation && g.explanation.lines) || []).forEach(function (l) { para(body, l); });
    zoneButtons(body, a, !!(app() && app().fvgVisible && app().fvgVisible()), { showFvg: true }, 'fair value gap');
    card.appendChild(body);
    return card;
  }
  function renderFvg(a) {
    var box = $('an-fvg'); if (!box) return; clear(box);
    var f = a.fvg;
    if (!f || !f.available) { para(box, 'Fair value gaps not analysed: ' + ((f && f.reason) || 'no data') + '.', 'an-note'); return; }
    para(box, f.note, 'an-note');
    kv(box, 'Gaps found', f.total + ' in ' + f.candles_used + ' candles (' + f.bullish + ' bullish, ' + f.bearish + ' bearish)');
    if (!f.total) { para(box, 'No fair value gap exists in these candles. That is a result of the analysis, not a missing feature.', 'an-note'); return; }
    var sec = el('div', 'an-sec'); sec.appendChild(el('h3', 'an-h3', 'Newest first (' + f.gaps.length + ' shown)'));
    f.gaps.forEach(function (g) { sec.appendChild(gapCard(a, g)); });
    box.appendChild(sec);
    if (f.hidden) para(box, f.hidden + ' older gap(s) are not listed.', 'an-note');
  }
  function blockCard(a, b) {
    var card = el('details', 'an-event an-event-' + b.state);
    var sum = el('summary');
    sum.appendChild(el('strong', null, (b.direction === 'bullish' ? 'Bullish' : 'Bearish') + ' order block'));
    sum.appendChild(el('span', 'an-pill an-pill-' + b.state, b.status.toUpperCase() + (b.provisional ? ' (PROVISIONAL)' : '')));
    sum.appendChild(el('span', 'an-ev-meta', b.lower_text + ' \u2013 ' + b.upper_text + ' \u00b7 source candle ' + ((b.source && b.source.label) || '')));
    card.appendChild(sum);
    var body = el('div', 'an-event-body');
    ((b.explanation && b.explanation.lines) || []).forEach(function (l) { para(body, l); });
    if (b.confirming_event) para(body, 'Confirmed by the structure engine\'s ' + b.confirming_event.type + ' at ' + ((b.confirming_event.candle && b.confirming_event.candle.label) || '') + '.', 'an-note');
    if (b.live) zoneButtons(body, a, !!(app() && app().obVisible && app().obVisible()), { showOrderBlocks: true }, 'order block');
    card.appendChild(body);
    return card;
  }
  function blockGroup(box, a, title, hint, items, hidden) {
    var sec = el('div', 'an-sec'); sec.appendChild(el('h3', 'an-h3', title + ' (' + items.length + ')'));
    if (hint) para(sec, hint, 'an-note');
    if (!items.length) para(sec, 'None.', 'an-note');
    items.forEach(function (b) { sec.appendChild(blockCard(a, b)); });
    if (hidden) para(sec, hidden + ' more not listed.', 'an-note');
    box.appendChild(sec);
  }
  function renderOrderBlocks(a) {
    var box = $('an-ob'); if (!box) return; clear(box);
    var o = a.order_blocks;
    if (!o || !o.available) { para(box, 'Order blocks not analysed: ' + ((o && o.reason) || 'no data') + '.', 'an-note'); return; }
    para(box, o.note, 'an-note');
    var c = o.counts || {};
    kv(box, 'Found in ' + o.candles_used + ' candles', o.total + ' (candidate ' + c.candidate + ', confirmed ' + c.confirmed + ', invalidated ' + c.invalidated + ', expired ' + c.expired + ')');
    if (!o.total) { para(box, 'No order block exists in these candles: no confirmed swing has an opposite-coloured candle behind it yet. That is a result of the analysis, not a missing feature.', 'an-note'); return; }
    blockGroup(box, a, 'Live: candidates and confirmed', 'A candidate is NOT confirmed. It becomes confirmed only when the structure engine reports a break of the swing the leg started from, and that swing extreme is the leg\'s origin.', o.live, o.live_hidden);
    blockGroup(box, a, 'Finished: invalidated or expired', 'Invalidated: price closed through the zone. Expired: the candidate can no longer be confirmed. Not drawn on the chart.', o.history, o.history_hidden);
  }

  function renderInterp(a) {
    var box = $('an-interp'); clear(box);
    var i = a.interpretation;
    if (!i) { para(box, 'No interpretation: ' + ((a.status && a.status.reason) || 'not analysed') + '.', 'an-note'); return; }
    [['Observed facts', 'What the candles and swings literally are.', i.observed_facts], ['Rule-based interpretation', 'What the written rules conclude from those facts.', i.rule_based],
     ['Conditions to watch', 'What WOULD change the classification. Conditional, not predictions.', i.watch]].forEach(function (g) {
      var sec = el('div', 'an-sec'); sec.appendChild(el('h3', 'an-h3', g[0])); para(sec, g[1], 'an-note'); bullets(sec, g[2]); box.appendChild(sec);
    });
    var su = el('div', 'an-sec'); su.appendChild(el('h3', 'an-h3', 'Trade setups'));
    kv(su, 'Status', i.setups.status, 'an-bad'); para(su, i.setups.note, 'an-note'); box.appendChild(su);
  }

  function renderModules(a) {
    var box = $('an-modules'); clear(box);
    (a.modules || []).forEach(function (m) {
      if (m.status === 'available') kv(box, m.name, 'Available');
      else if (m.status === 'unavailable') kv(box, m.name, 'Unavailable now: ' + (m.reason || 'not analysed'), 'an-bad');
      else kv(box, m.name, 'Not available yet', 'an-dim');
    });
    para(box, '"Available" means the engine ran on this request (it may still have found nothing). "Unavailable now" means the engine exists but could not run, and says why. "Not available yet" means the module does not exist in TradeMate. No results are shown for a module that is not available.', 'an-note');
  }
  function renderCaveats(a) { var box = $('an-caveats'); clear(box); bullets(box, a.caveats || []); }


  /* ---------- plain-language summary (payload.summary; every sentence was built by the server from engine results) ---------- */
  var ARROW = { bullish: '\u25B2 ', bearish: '\u25BC ', mixed: '\u2194 ', unclear: '? ', insufficient: '? ' };
  function dcls(d) { return d === 'bullish' ? 'up' : d === 'bearish' ? 'down' : d; }       // CSS class suffix (kept free of candle-colour words)
  function unavailable(box, text) { var d = el('div', 'an-unavail', 'Unavailable: ' + text); box.appendChild(d); }
  function zoneCard(parent, z, kind) {
    var c = el('div', 'an-zone an-zone-' + dcls(z.direction));
    c.setAttribute('data-zone', kind); c.setAttribute('data-direction', z.direction); c.setAttribute('data-status', z.status_code);
    c.appendChild(el('div', 'an-zone-range', z.direction.charAt(0).toUpperCase() + z.direction.slice(1) + ' \u00b7 ' + z.range_text));
    c.appendChild(el('div', 'an-zone-meta', 'Status: ' + z.status + (z.fill_percent ? ' (' + z.fill_percent + '% of the gap)' : '')
      + (z.forming ? ' \u00b7 still forming' : '') + (z.provisional && !z.forming && kind === 'fvg' ? ' \u00b7 provisional' : '')));
    c.appendChild(el('div', 'an-zone-meta', z.location_text + (z.formed ? ' Formed ' + z.formed + '.' : z.source ? ' Source candle ' + z.source + '.' : '')));
    parent.appendChild(c);
  }
  function zoneSection(box, sec, kind, noun) {
    clear(box);
    if (!sec || !sec.available) { unavailable(box, (sec && sec.reason) || 'This analysis is not available for the data received.'); return; }
    var shown = sec.bullish.length + sec.bearish.length;
    ['bullish', 'bearish'].forEach(function (side) {
      var g = el('div', 'an-sec'); g.appendChild(el('h3', 'an-h3', side.charAt(0).toUpperCase() + side.slice(1) + ' ' + noun));
      if (!sec[side].length) g.appendChild(el('p', 'an-note', 'None relevant right now.')); else sec[side].forEach(function (z) { zoneCard(g, z, kind); });
      box.appendChild(g);
    });
    if (!shown) para(box, 'No ' + noun + ' are relevant on these candles.', 'an-dim');
    para(box, sec.status_note, 'an-note');
    var c = sec.counts || {}, bits = [];
    Object.keys(c).forEach(function (k) { if (c[k]) bits.push(c[k] + ' ' + k); });
    if (bits.length) para(box, 'All detected on these candles: ' + bits.join(', ') + '. The nearest are shown here; the full list is under Technical details.', 'an-note');
  }
  function renderSummary(a) {
    var S = a.summary, boxes = ['an-sum-direction', 'an-sum-liquidity', 'an-sum-fvg', 'an-sum-ob', 'an-sum-overall'].map($);
    boxes.forEach(clear);
    $('an-summary').setAttribute('data-state', S ? 'ready' : 'unavailable');
    if (!S) {                                    // no analysis was produced (stale / invalid / insufficient data / error): never fill the sections
      var why = (a.status && a.status.reason) || 'No analysis is available.';
      boxes.forEach(function (b) { unavailable(b, why); });
      return;
    }
    var d = S.direction, box = boxes[0];
    var directionText = d.confirmed_direction
      ? d.confirmed_direction.charAt(0).toUpperCase() + d.confirmed_direction.slice(1) + ' direction · ' + (d.quality || d.label) + ' evidence'
      : d.headline;
    var directionClass = d.confirmed_direction || d.label;
    var row = el('div', 'an-dir'); var badge = el('span', 'an-dir-badge an-dir-' + dcls(directionClass), (ARROW[directionClass] || ARROW[d.label] || '') + directionText);
    badge.id = 'an-dir-badge'; row.appendChild(badge); row.appendChild(el('span', 'an-p', d.reason || '')); box.appendChild(row);
    if (!d.available && d.reason) { /* reason already shown beside the badge */ }
    (d.context || []).forEach(function (t) { para(box, t); });
    if (d.caveat) para(box, d.caveat, 'an-warn');
    if (d.events && d.events.length) {
      box.appendChild(el('h3', 'an-h3', 'Recent structural events'));
      d.events.forEach(function (e) {
        var c = el('div', 'an-zone an-zone-' + dcls(e.direction)); c.setAttribute('data-event', e.type);
        c.appendChild(el('div', 'an-zone-range', e.title + ' \u00b7 ' + e.level_text + (e.confirmed ? '' : ' (forming)')));
        c.appendChild(el('div', 'an-zone-meta', (e.when || '') + ' \u2014 ' + e.meaning));
        box.appendChild(c);
      });
    }
    var L = S.liquidity; box = boxes[1];
    if (!L.available) unavailable(box, L.reason);
    else {
      para(box, L.headline, 'an-p');
      if (L.levels.length) {
        box.appendChild(el('h3', 'an-h3', 'Nearest liquidity levels'));
        L.levels.forEach(function (l) { para(box, l.side + ' liquidity at ' + l.price_text + ' (' + l.what + (l.where ? ', ' + l.where + ' the last close' : '') + ')' + (l.provisional ? ' \u2014 provisional' : '') + '.'); });
      } else para(box, 'No active liquidity levels were found on these candles.', 'an-dim');
      if (L.sweeps.length) {
        box.appendChild(el('h3', 'an-h3', 'Detected sweeps'));
        L.sweeps.forEach(function (s) {
          var c = el('div', 'an-zone'); c.setAttribute('data-sweep', s.verdict);
          c.appendChild(el('div', 'an-zone-range', s.title));
          c.appendChild(el('div', 'an-zone-meta', (s.when || '') + ' \u00b7 ' + s.candles_ago + ' candle(s) ago'));
          c.appendChild(el('div', 'an-zone-meta', s.text)); box.appendChild(c);
        });
      }
      (L.provisional || []).forEach(function (p) { para(box, p.title + ' (unconfirmed: the candle has not closed).', 'an-note'); });
    }
    zoneSection(boxes[2], S.fvg, 'fvg', 'fair value gaps');
    zoneSection(boxes[3], S.order_blocks, 'ob', 'order blocks');
    var O = S.overall; box = boxes[4];
    var orow = el('div', 'an-dir'); var ob = el('span', 'an-dir-badge an-dir-' + dcls(O.verdict), (ARROW[O.verdict] || '') + (O.verdict === 'insufficient' ? 'Not enough evidence' : O.verdict.charAt(0).toUpperCase() + O.verdict.slice(1)));
    ob.id = 'an-overall-badge'; orow.appendChild(ob); orow.appendChild(el('span', 'an-p', O.text)); box.appendChild(orow);
    [['Supporting evidence', O.supporting], ['Conflicting evidence', O.conflicting], ['What remains uncertain', O.uncertain]].forEach(function (g) {
      if (!g[1].length) return;
      box.appendChild(el('h3', 'an-h3', g[0])); bullets(box, g[1]);
    });
    para(box, O.note, 'an-note');
  }

  function render(a) {
    last = a;
    if (a.supported_timeframes && a.supported_timeframes.join() !== tfs.join()) { tfs = a.supported_timeframes.slice(); fillTfs(); }
    renderStatus(a); renderSummary(a); renderContext(a); renderStructure(a); renderEvents(a); renderLiquidity(a); renderFvg(a); renderOrderBlocks(a); renderInterp(a); renderModules(a); renderCaveats(a);
  }
  function renderError(text) {
    render({ symbol: sel.symbol, timeframe: sel.tf, status: { code: 'UNAVAILABLE', reason: text, meaning: '' }, context: { data: { freshness: 'unavailable', completeness: 'unavailable', warnings: [] } },
             structure: null, interpretation: null, modules: [], caveats: [] });
  }

  /* ---------- loading ---------- */
  function load() {
    if (!sel.symbol || !sel.tf) return;
    var my = ++seq;
    if (ctl) ctl.abort();
    ctl = typeof AbortController === 'function' ? new AbortController() : null;
    $('an-status').setAttribute('data-code', 'ANALYSING');
    $('an-status-code').textContent = 'ANALYSING';
    $('an-status-reason').textContent = 'Analysing ' + sel.symbol + ' ' + sel.tf + '...';
    $('an-status-meaning').textContent = '';
    var url = '/api/analysis?symbol=' + encodeURIComponent(sel.symbol) + '&timeframe=' + encodeURIComponent(sel.tf);
    fetch(url, { cache: 'no-store', signal: ctl ? ctl.signal : undefined })
      .then(function (r) { return r.json().catch(function () { return { ok: false, message: 'HTTP ' + r.status }; }); })
      .then(function (a) {
        if (my !== seq) return;                                        // an answer for an older selection: never shown
        if (!a || a.ok === false) { renderError((a && a.message) || 'The analysis request failed.'); return; }
        if (a.symbol !== sel.symbol || a.timeframe !== sel.tf) return;
        render(a);
      })
      .catch(function (err) { if (err && err.name === 'AbortError') return; if (my === seq) renderError('Cannot reach TradeMate. No analysis was made.'); });
  }

  var waits = 0;
  function enter() {
    var a = app();
    if (a) { var cur = a.selection(); if (!sel.symbol) sel.symbol = cur.symbol; if (!sel.tf) sel.tf = cur.tf || 'H1'; }
    if (!sel.tf) sel.tf = 'H1';
    if (!sel.symbol) {                                        // the symbol list arrives from MT5 shortly after the page loads
      $('an-status').setAttribute('data-code', 'ANALYSING');
      $('an-status-code').textContent = 'ANALYSING';
      $('an-status-reason').textContent = waits < 50 ? 'Waiting for a symbol from MT5...' : 'No symbol is available from MT5, so nothing can be analysed.';
      $('an-status-meaning').textContent = '';
      if (waits++ < 50 && visible()) setTimeout(enter, 400);
      return;
    }
    waits = 0;
    fillSymbols(); fillTfs(); load();
  }

  $('an-symbol').addEventListener('change', function (e) { sel.symbol = e.target.value; load(); });
  $('an-refresh').addEventListener('click', load);
  function tick() {
    if (!visible() || document.hidden) return;
    if (!sel.symbol) { waits = 0; enter(); return; }          // still no symbol: look again
    if ($('an-auto').checked) load();
  }
  timer = setInterval(tick, REFRESH_MS);
  function onRoute() { if (visible()) enter(); }
  window.TradeMateRouter.on(onRoute);
  document.addEventListener('visibilitychange', function () { if (!document.hidden && visible()) load(); });
  onRoute();
  window.TradeMateAnalysis = { reload: load, last: function () { return last; } };
})();
