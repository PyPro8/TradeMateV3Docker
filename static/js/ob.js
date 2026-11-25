/* Chart items for the order block overlay. Pure functions, no DOM (tested by tests/ob_js_test.js).
   Input: the `order_blocks` block of /api/candles (the Python engine's own result) and the candle rows [time, o, h, l, c].
   Output: zones in CANDLE INDICES (so the chart places them with the same x-mapping as candles) with the engine's boundaries
   passed through UNCHANGED. This file never detects, recalculates or adjusts a block. A block whose source candle is not among the
   loaded candles, or whose numbers are not usable, is dropped, and an unavailable / invalid analysis gives null (nothing is drawn).

   Horizontal extent, from the engine's own times only:
     live block (candidate / confirmed)  source candle -> the newest candle (still valid as far as the engine can tell)
     invalidated / expired (only if asked for)  source candle -> the candle at invalidated_time / expired_time
   By default only live blocks are returned: the chart would otherwise fill with finished history (it is listed on the Analysis page).
   An order block is a candle zone picked by price structure, not evidence of institutional activity or a prediction. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TradeMateOrderBlocks = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var LIVE = ['candidate', 'confirmed'];
  var KNOWN = ['candidate', 'confirmed', 'invalidated', 'expired'];

  function num(v) { return typeof v === 'number' && isFinite(v); }

  function label(direction, status, provisional) {
    var base = direction === 'bullish' ? 'Bull OB' : 'Bear OB';
    if (status === 'candidate') base += ' (candidate)';
    else if (status === 'invalidated') base += ' (invalidated)';
    else if (status === 'expired') base += ' (expired)';
    return base + (provisional === true && (status === 'candidate' || status === 'confirmed') ? ' (forming)' : '');
  }

  function overlayItems(ob, candles, opts) {
    if (!ob || ob.available !== true || !Array.isArray(ob.blocks) || !Array.isArray(candles) || !candles.length) return null;
    var statuses = opts && Array.isArray(opts.statuses) ? opts.statuses : LIVE;
    var idx = {}, i, last = candles.length - 1, blocks = [];
    for (i = 0; i < candles.length; i++) {
      if (candles[i] && num(candles[i][0])) idx[candles[i][0]] = i;
    }
    ob.blocks.forEach(function (b) {
      if (!b || (b.direction !== 'bullish' && b.direction !== 'bearish')) return;
      if (KNOWN.indexOf(b.status) < 0 || statuses.indexOf(b.status) < 0) return;
      if (!num(b.upper) || !num(b.lower) || !(b.upper > b.lower)) return;
      var from = idx[b.source_time];
      if (from === undefined) return;                                          // not among the loaded candles: never placed at a guess
      var to = last;
      if (b.status === 'invalidated' || b.status === 'expired') {
        to = idx[b.status === 'invalidated' ? b.invalidated_time : b.expired_time];
        if (to === undefined) return;
      }
      if (to < from) return;
      blocks.push({ id: String(b.id), direction: b.direction, status: b.status, upper: b.upper, lower: b.lower, from: from, to: to,
                    confirmed: b.status === 'confirmed', live: LIVE.indexOf(b.status) >= 0, provisional: b.provisional === true,
                    label: label(b.direction, b.status, b.provisional) });
    });
    return { blocks: blocks };
  }

  return { overlayItems: overlayItems, label: label };
});
