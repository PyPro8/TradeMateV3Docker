// Run with: node tests/ob_js_test.js (also run by tests/test_ob_js.py)
// Checks static/js/ob.js: the engine's order block result -> chart items. Boundaries must pass through UNCHANGED (JS never recalculates a block)
// and the horizontal extent may only come from times the engine itself reports.
const assert = require('assert');
const OB = require('../static/js/ob.js');

const T0 = 1760000000, STEP = 300;
const candles = Array.from({ length: 12 }, (_, i) => [T0 + i * STEP, 10, 11, 9, 10]);
const t = (i) => T0 + i * STEP + 0.0;          // the engine reports times as floats; JSON numbers equal the integer candle times

const base = { upper: 10.6, lower: 9.8, source_time: t(4), provisional: false, invalidated_time: null, expired_time: null };
const ob = {
  available: true,
  blocks: [
    { ...base, id: 'OB-BULL-A', direction: 'bullish', status: 'confirmed' },
    { ...base, id: 'OB-BEAR-B', direction: 'bearish', status: 'candidate', upper: 11.2, lower: 10.9, source_time: t(6), provisional: true },
    { ...base, id: 'OB-BULL-C', direction: 'bullish', status: 'invalidated', source_time: t(2), invalidated_time: t(8) },
    { ...base, id: 'OB-BEAR-D', direction: 'bearish', status: 'expired', source_time: t(1), expired_time: t(5) },
  ],
};

// ---- default: only LIVE blocks, boundaries untouched, extent = source candle -> newest candle ----
const out = OB.overlayItems(ob, candles);
assert.deepStrictEqual(out.blocks.map((b) => b.id), ['OB-BULL-A', 'OB-BEAR-B']);
const [a, b] = out.blocks;
assert.deepStrictEqual([a.direction, a.status, a.upper, a.lower, a.from, a.to, a.confirmed, a.live, a.provisional, a.label], ['bullish', 'confirmed', 10.6, 9.8, 4, 11, true, true, false, 'Bull OB']);
assert.deepStrictEqual([b.direction, b.status, b.upper, b.lower, b.from, b.to, b.confirmed, b.provisional, b.label], ['bearish', 'candidate', 11.2, 10.9, 6, 11, false, true, 'Bear OB (candidate) (forming)']);
for (const x of out.blocks) assert.ok(x.upper > x.lower);
assert.strictEqual(a.upper, 10.6); assert.strictEqual(a.lower, 9.8);                  // exact engine values, not recomputed from candle highs / lows (10 / 9 / 11 here)

// ---- finished blocks only when asked for, and then end at the engine's own invalidated / expired time ----
const all = OB.overlayItems(ob, candles, { statuses: ['candidate', 'confirmed', 'invalidated', 'expired'] });
const by = Object.fromEntries(all.blocks.map((x) => [x.id, x]));
assert.deepStrictEqual([by['OB-BULL-C'].from, by['OB-BULL-C'].to, by['OB-BULL-C'].live, by['OB-BULL-C'].label], [2, 8, false, 'Bull OB (invalidated)']);
assert.deepStrictEqual([by['OB-BEAR-D'].from, by['OB-BEAR-D'].to, by['OB-BEAR-D'].label], [1, 5, 'Bear OB (expired)']);
// a finished block whose end time is not among the candles is dropped, never given a guessed end
const noEnd = OB.overlayItems({ available: true, blocks: [{ ...ob.blocks[2], invalidated_time: 424242 }] }, candles, { statuses: ['invalidated'] });
assert.deepStrictEqual(noEnd.blocks, []);

// ---- nothing is invented: unavailable, invalid or empty input draws nothing ----
for (const bad of [null, undefined, 'x', 5, [], {}, { available: false, reason: 'r', blocks: ob.blocks }, { available: 'true', blocks: ob.blocks }, { available: true }, { available: true, blocks: 'x' }]) {
  assert.strictEqual(OB.overlayItems(bad, candles), null, JSON.stringify(bad));
}
assert.strictEqual(OB.overlayItems(ob, []), null);
assert.strictEqual(OB.overlayItems(ob, null), null);
assert.deepStrictEqual(OB.overlayItems({ available: true, blocks: [] }, candles), { blocks: [] });   // a valid analysis with no blocks: nothing to draw, not an error

// ---- bad individual blocks are dropped, good ones stay ----
const good = ob.blocks[0];
const junk = [null, 5, 'x', {}, { ...good, id: 'a', direction: 'sideways' }, { ...good, id: 'b', status: 'weird' }, { ...good, id: 'c', status: 'candidate', upper: NaN },
  { ...good, id: 'd', lower: Infinity }, { ...good, id: 'e', upper: '10.6' }, { ...good, id: 'f', lower: null }, { ...good, id: 'g', upper: 9.8, lower: 9.8 },
  { ...good, id: 'h', upper: 9, lower: 9.8 }, { ...good, id: 'i', source_time: 424242 }, { ...good, id: 'j', source_time: undefined }];
assert.deepStrictEqual(OB.overlayItems({ available: true, blocks: junk.concat([{ ...good, id: 'keep' }]) }, candles).blocks.map((x) => x.id), ['keep']);

// ---- a block that ends before it starts is dropped ----
assert.deepStrictEqual(OB.overlayItems({ available: true, blocks: [{ ...ob.blocks[2], source_time: t(9), invalidated_time: t(3) }] }, candles, { statuses: ['invalidated'] }).blocks, []);

// ---- provisional only when the engine says exactly true; never for finished blocks' label ----
for (const v of [undefined, null, 'true', 1, 0, false]) assert.strictEqual(OB.overlayItems({ available: true, blocks: [{ ...good, provisional: v }] }, candles).blocks[0].provisional, false, String(v));
assert.strictEqual(OB.label('bullish', 'invalidated', true), 'Bull OB (invalidated)');

// ---- a different candle set gives different positions; nothing is kept between calls; input is never mutated ----
const shifted = candles.map((c) => [c[0] + 20 * STEP, c[1], c[2], c[3], c[4]]);
assert.deepStrictEqual(OB.overlayItems(ob, shifted).blocks, []);
const before = JSON.stringify(ob), cbefore = JSON.stringify(candles);
assert.deepStrictEqual(OB.overlayItems(ob, candles), OB.overlayItems(ob, candles));
assert.strictEqual(OB.overlayItems(ob, candles).blocks.length, 2);                      // 2 again, not 4
assert.strictEqual(JSON.stringify(ob), before); assert.strictEqual(JSON.stringify(candles), cbefore);
assert.strictEqual(OB.overlayItems(ob, candles.concat([null, [], ['x'], [NaN, 1, 1, 1, 1]])).blocks.length, 2);
console.log('ob.js: all checks passed');
