/* Chart items for the liquidity overlay. Pure functions, no DOM (tested by tests/liquidity_js_test.js).
   Input: the `liquidity` block of /api/candles and the candle rows [time, o, h, l, c]. Output: positions in CANDLE INDICES, so the
   chart can place them with the same x-mapping it uses for candles. Nothing is invented: a pool or sweep whose time is not among the
   loaded candles is dropped, and an unavailable analysis gives null (the chart draws nothing). */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TradeMateLiquidity = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function label(pool) {
    if (pool.kind === 'equal') return pool.side === 'BSL' ? 'EQH' : 'EQL';
    return pool.side;                       // BSL / SSL
  }

  function overlayItems(liq, candles) {
    if (!liq || liq.available !== true || !candles || !candles.length) return null;
    var idx = {}, i;
    for (i = 0; i < candles.length; i++) idx[candles[i][0]] = i;
    var last = candles.length - 1, pools = [], sweeps = [];
    (liq.pools || []).forEach(function (p) {
      var from = idx[p.created_time];
      if (from === undefined || !isFinite(p.level)) return;
      var to = p.state === 'SWEPT' ? idx[p.swept_time] : last;
      if (to === undefined) return;
      pools.push({ id: p.id, side: p.side, kind: p.kind, level: p.level, from: from, to: to, state: p.state, provisional: !!p.provisional,
                   label: label(p), members: p.member_count });
    });
    (liq.sweeps || []).forEach(function (s) {
      var at = idx[s.candle_time];
      if (at === undefined || !isFinite(s.level)) return;
      sweeps.push({ id: s.id, poolId: s.pool_id, side: s.side, level: s.level, index: at, confirmed: !!s.confirmed, closeBeyond: !!s.close_beyond });
    });
    return { pools: pools, sweeps: sweeps };
  }

  return { overlayItems: overlayItems, label: label };
});
