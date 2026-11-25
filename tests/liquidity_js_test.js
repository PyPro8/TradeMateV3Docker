// Run with: node tests/liquidity_js_test.js (also run by tests/test_liquidity_js.py)
const assert = require('assert');
const L = require('../static/js/liquidity.js');

const candles = [0, 1, 2, 3, 4, 5, 6, 7].map((i) => [1000 + i * 60, 10, 11, 9, 10]);
const liq = {
  available: true,
  pools: [
    { id: 'BSL-1060', side: 'BSL', kind: 'equal', level: 10.5, created_time: 1060, state: 'SWEPT', swept_time: 1300, provisional: false, member_count: 2 },
    { id: 'SSL-1120', side: 'SSL', kind: 'swing', level: 9.2, created_time: 1120, state: 'ACTIVE', swept_time: null, provisional: false, member_count: 1 },
    { id: 'BSL-1180', side: 'BSL', kind: 'swing', level: 10.9, created_time: 1180, state: 'ACTIVE', provisional: true, member_count: 1 },
    { id: 'SSL-1240', side: 'SSL', kind: 'equal', level: 8.8, created_time: 1240, state: 'ACTIVE', provisional: false, member_count: 3 },
  ],
  sweeps: [{ id: 'SWEEP-BSL-1060-1300', pool_id: 'BSL-1060', side: 'BSL', level: 10.5, candle_time: 1300, confirmed: false, close_beyond: true }],
};
const out = L.overlayItems(liq, candles);
assert.strictEqual(out.pools.length, 4);
const by = Object.fromEntries(out.pools.map((p) => [p.id, p]));
assert.deepStrictEqual([by['BSL-1060'].from, by['BSL-1060'].to, by['BSL-1060'].label], [1, 5, 'EQH']);      // swept: the segment ends at the sweeping candle
assert.deepStrictEqual([by['SSL-1120'].from, by['SSL-1120'].to, by['SSL-1120'].label], [2, 7, 'SSL']);      // active: runs to the newest candle
assert.strictEqual(by['BSL-1180'].label, 'BSL'); assert.strictEqual(by['BSL-1180'].provisional, true);
assert.strictEqual(by['SSL-1240'].label, 'EQL'); assert.strictEqual(by['SSL-1240'].members, 3);
assert.deepStrictEqual(out.sweeps, [{ id: 'SWEEP-BSL-1060-1300', poolId: 'BSL-1060', side: 'BSL', level: 10.5, index: 5, confirmed: false, closeBeyond: true }]);

// nothing is invented
assert.strictEqual(L.overlayItems(null, candles), null);
assert.strictEqual(L.overlayItems({ available: false, reason: 'x', pools: [{}] }, candles), null);
assert.strictEqual(L.overlayItems({ ...liq, available: undefined }, candles), null);
assert.strictEqual(L.overlayItems(liq, []), null);
const partial = L.overlayItems({ available: true, pools: [{ id: 'P', side: 'BSL', kind: 'swing', level: 1, created_time: 99999, state: 'ACTIVE', member_count: 1 },
  { id: 'Q', side: 'BSL', kind: 'swing', level: NaN, created_time: 1000, state: 'ACTIVE', member_count: 1 },
  { id: 'R', side: 'SSL', kind: 'swing', level: 5, created_time: 1000, state: 'SWEPT', swept_time: 77777, member_count: 1 }],
  sweeps: [{ id: 'S', pool_id: 'P', side: 'BSL', level: 1, candle_time: 424242, confirmed: true, close_beyond: false }] }, candles);
assert.deepStrictEqual([partial.pools.length, partial.sweeps.length], [0, 0]);                           // unknown times / non-finite levels are dropped
// repeated calls give identical items (no state kept between refreshes, so overlays cannot pile up)
assert.deepStrictEqual(L.overlayItems(liq, candles), L.overlayItems(liq, candles));
console.log('liquidity.js: all checks passed');
