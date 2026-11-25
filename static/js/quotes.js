/* TradeMate live-quote state for the chart's BID / ASK lines. Pure functions, no DOM (tested by tests/quotes_test.js).

   A quote is LIVE only when ALL of these hold; otherwise the chart draws NO bid/ask lines and says why:
     - MT5 is connected and the latest broker poll succeeded recently (not older than maxAgeMs)
     - the bridge snapshot itself is fresh (not older than maxStatusAgeSeconds)
     - the selected symbol is in the LATEST poll's symbol list (a symbol that is not being streamed has no live quote;
       a one-off snapshot read earlier is never presented as live)
     - bid and ask are finite, positive and not crossed (ask >= bid)
   The bridge exports no tick timestamp, so "fresh" means "the bridge snapshot is fresh and this poll is recent". It is
   never inferred from the last candle's close, and ask is never derived from bid with an assumed spread. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TradeMateQuotes = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function ok(v) { return typeof v === 'number' && isFinite(v) && v > 0; }

  /* input: { symbols, name, connected, receivedAt, now, maxAgeMs, statusAgeSeconds, maxStatusAgeSeconds }
     -> { state: 'live'|'stale'|'unavailable', reason, bid, ask, spreadPrice, spreadPoints, ageMs } */
  function quoteState(i) {
    var out = { state: 'unavailable', reason: '', bid: null, ask: null, spreadPrice: null, spreadPoints: null, ageMs: null };
    if (!i || !i.name) { out.reason = 'No symbol selected.'; return out; }
    if (!i.connected) { out.reason = 'MT5 is not connected.'; return out; }
    var age = (typeof i.receivedAt === 'number' && typeof i.now === 'number') ? i.now - i.receivedAt : null;
    out.ageMs = age;
    if (age === null) { out.reason = 'No quote has been received yet.'; return out; }
    var maxAge = typeof i.maxAgeMs === 'number' ? i.maxAgeMs : 7000;
    if (age > maxAge) { out.state = 'stale'; out.reason = 'The last quote update was ' + Math.round(age / 1000) + ' s ago.'; return out; }
    if (typeof i.statusAgeSeconds === 'number' && typeof i.maxStatusAgeSeconds === 'number' && i.statusAgeSeconds > i.maxStatusAgeSeconds) {
      out.state = 'stale'; out.reason = 'MT5 data is ' + Math.round(i.statusAgeSeconds) + ' s old.'; return out;
    }
    var sym = null, list = i.symbols || [];
    for (var k = 0; k < list.length; k++) if (list[k].name === i.name) { sym = list[k]; break; }
    if (!sym) { out.reason = 'No live quote for ' + i.name + ': it is not in the streamed watch list.'; return out; }
    if (!ok(sym.bid) || !ok(sym.ask)) { out.reason = 'MT5 reported no valid bid/ask for ' + i.name + '.'; return out; }
    if (sym.ask < sym.bid) { out.reason = 'MT5 reported a crossed quote (ask below bid); it is not shown.'; return out; }
    out.state = 'live'; out.bid = sym.bid; out.ask = sym.ask;
    out.spreadPrice = sym.ask - sym.bid;
    out.spreadPoints = ok(sym.point) ? Math.round(out.spreadPrice / sym.point) : null;     // from the actual bid and ask, not a stored spread
    return out;
  }

  /* text for the small badge on the chart */
  function badge(q, digits) {
    if (q.state === 'live') {
      var d = typeof digits === 'number' ? digits : 5;
      return 'Spread ' + (q.spreadPoints === null ? '' : q.spreadPoints + ' pts \u00b7 ') + q.spreadPrice.toFixed(d);
    }
    return 'Live quotes ' + (q.state === 'stale' ? 'STALE' : 'UNAVAILABLE') + ': ' + q.reason;
  }

  return { quoteState: quoteState, badge: badge };
});
