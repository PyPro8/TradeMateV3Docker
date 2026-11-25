/* Chart items for the fair value gap (FVG) overlay. Pure functions, no DOM (tested by tests/fvg_js_test.js).
   Input: the `fvg` block of /api/candles (the Python engine's own result) and the candle rows [time, o, h, l, c].
   Output: gap positions in CANDLE INDICES (so the chart places them with the same x-mapping as candles) with the engine's boundaries
   passed through UNCHANGED. This file never detects, recalculates or adjusts a gap: a gap whose candle times are not among the
   loaded candles, or whose numbers are not usable, is dropped, and an unavailable / invalid analysis gives null (the chart draws
   nothing). A gap is a price range between candle 1 and candle 3, not a prediction or a trade signal. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TradeMateFvg = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function label(direction) {
    return direction === 'bullish' ? 'Bull FVG' : 'Bear FVG';
  }

  function num(v) { return typeof v === 'number' && isFinite(v); }

  function overlayItems(fvg, candles) {
    if (!fvg || fvg.available !== true || !Array.isArray(fvg.gaps) || !Array.isArray(candles) || !candles.length) return null;
    var idx = {}, i, gaps = [];
    for (i = 0; i < candles.length; i++) {
      if (candles[i] && num(candles[i][0])) idx[candles[i][0]] = i;
    }
    fvg.gaps.forEach(function (g) {
      if (!g || (g.direction !== 'bullish' && g.direction !== 'bearish')) return;
      if (!num(g.upper) || !num(g.lower) || !(g.upper > g.lower)) return;
      var from = idx[g.first_time], to = idx[g.third_time];
      if (from === undefined || to === undefined || to <= from) return;        // not among the loaded candles: never placed at a guess
      gaps.push({ id: String(g.id), direction: g.direction, upper: g.upper, lower: g.lower, from: from, to: to,
                  confirmed: g.confirmed === true, label: label(g.direction) });
    });
    return { gaps: gaps };
  }

  return { overlayItems: overlayItems, label: label };
});
