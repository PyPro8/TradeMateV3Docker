// Run with: node tests/fvg_js_test.js (also run by tests/test_fvg_js.py)
// Checks static/js/fvg.js: the engine's FVG result -> chart items. The boundaries must pass through UNCHANGED (JS never recalculates a gap).
const assert = require('assert');
const F = require('../static/js/fvg.js');

const T0 = 1760000000, STEP = 300;
const candles = [0, 1, 2, 3, 4, 5, 6, 7].map((i) => [T0 + i * STEP, 10, 11, 9, 10]);
const t = (i) => T0 + i * STEP + 0.0;          // the engine reports times as floats (1760000300.0): JSON numbers, equal to the integer candle times

const fvg = {
  available: true,
  gaps: [
    { id: 'FVG-BULL-' + t(0), direction: 'bullish', upper: 11.25, lower: 10.5, size: 0.75, first_time: t(0), middle_time: t(1), third_time: t(2), formed_time: t(2), confirmed: true, confirmed_time: t(2) },
    { id: 'FVG-BEAR-' + t(2), direction: 'bearish', upper: 12, lower: 9.125, size: 2.875, first_time: t(2), middle_time: t(3), third_time: t(4), formed_time: t(4), confirmed: true, confirmed_time: t(4) },
    { id: 'FVG-BULL-' + t(5), direction: 'bullish', upper: 10.8, lower: 10.2, size: 0.6, first_time: t(5), middle_time: t(6), third_time: t(7), formed_time: t(7), confirmed: false, confirmed_time: null },
  ],
};

// ---- boundaries pass through untouched; times become candle indices ----
const out = F.overlayItems(fvg, candles);
assert.strictEqual(out.gaps.length, 3);
const by = Object.fromEntries(out.gaps.map((g) => [g.id, g]));
const bull = by['FVG-BULL-' + t(0)], bear = by['FVG-BEAR-' + t(2)], forming = by['FVG-BULL-' + t(5)];
assert.deepStrictEqual([bull.direction, bull.upper, bull.lower, bull.from, bull.to, bull.confirmed, bull.label], ['bullish', 11.25, 10.5, 0, 2, true, 'Bull FVG']);
assert.deepStrictEqual([bear.direction, bear.upper, bear.lower, bear.from, bear.to, bear.confirmed, bear.label], ['bearish', 12, 9.125, 2, 4, true, 'Bear FVG']);
assert.deepStrictEqual([forming.from, forming.to, forming.confirmed], [5, 7, false]);               // unconfirmed stays unconfirmed
for (const g of out.gaps) assert.ok(g.upper > g.lower);
// exact values, not recomputed from the candles (the candles here have high 11 / low 9: a JS re-derivation would give other numbers)
assert.strictEqual(bull.lower, 10.5);
assert.strictEqual(bull.upper, 11.25);

// ---- nothing is invented: unavailable, invalid or empty input draws nothing ----
assert.strictEqual(F.overlayItems(null, candles), null);
assert.strictEqual(F.overlayItems(undefined, candles), null);
assert.strictEqual(F.overlayItems({ available: false, reason: 'x', gaps: fvg.gaps }, candles), null);   // unavailable wins even if gaps are present
assert.strictEqual(F.overlayItems({ ...fvg, available: undefined }, candles), null);
assert.strictEqual(F.overlayItems({ ...fvg, available: 'true' }, candles), null);
assert.strictEqual(F.overlayItems({ available: true }, candles), null);                                   // no gaps array
assert.strictEqual(F.overlayItems({ available: true, gaps: 'x' }, candles), null);
assert.strictEqual(F.overlayItems(fvg, []), null);
assert.strictEqual(F.overlayItems(fvg, null), null);
assert.strictEqual(F.overlayItems('garbage', candles), null);
assert.deepStrictEqual(F.overlayItems({ available: true, gaps: [] }, candles), { gaps: [] });            // a valid analysis with no gaps: nothing to draw, not an error

// ---- bad individual gaps are dropped, the good ones stay ----
const base = fvg.gaps[0];
const bad = [
  null, 5, 'x', {},
  { ...base, id: 'A', direction: 'sideways' },
  { ...base, id: 'B', direction: undefined },
  { ...base, id: 'C', upper: NaN }, { ...base, id: 'D', lower: Infinity }, { ...base, id: 'E', upper: '11' }, { ...base, id: 'F', lower: null },
  { ...base, id: 'G', upper: 10.5, lower: 10.5 },                      // not a gap (zero height)
  { ...base, id: 'H', upper: 10, lower: 10.5 },                        // inverted
  { ...base, id: 'I', first_time: 424242 },                            // not among the loaded candles: never placed at a guess
  { ...base, id: 'J', third_time: 424242 },
  { ...base, id: 'K', first_time: t(3), third_time: t(2) },            // third before first
  { ...base, id: 'L', first_time: t(2), third_time: t(2) },
];
const mixed = F.overlayItems({ available: true, gaps: bad.concat([fvg.gaps[1]]) }, candles);
assert.deepStrictEqual(mixed.gaps.map((g) => g.id), ['FVG-BEAR-' + t(2)]);

// ---- confirmed is only true when the engine says exactly true ----
for (const v of [undefined, null, 'true', 1, 0, false]) {
  const o = F.overlayItems({ available: true, gaps: [{ ...base, confirmed: v }] }, candles);
  assert.strictEqual(o.gaps[0].confirmed, false, String(v));
}

// ---- a different candle set gives different positions (symbol / timeframe change): nothing is kept between calls ----
const shifted = candles.map((c) => [c[0] + 10 * STEP, c[1], c[2], c[3], c[4]]);
assert.deepStrictEqual(F.overlayItems(fvg, shifted).gaps, []);                                          // the old gaps' times are not in the new series
const moved = F.overlayItems({ available: true, gaps: fvg.gaps.map((g) => ({ ...g, first_time: g.first_time + 10 * STEP, middle_time: g.middle_time + 10 * STEP, third_time: g.third_time + 10 * STEP })) }, shifted);
assert.deepStrictEqual(moved.gaps.map((g) => [g.from, g.to]), out.gaps.map((g) => [g.from, g.to]));
// repeated calls give identical items and never mutate the input
const before = JSON.stringify(fvg), cbefore = JSON.stringify(candles);
assert.deepStrictEqual(F.overlayItems(fvg, candles), F.overlayItems(fvg, candles));
assert.strictEqual(F.overlayItems(fvg, candles).gaps.length, 3);                                        // 3 again, not 6
assert.strictEqual(JSON.stringify(fvg), before); assert.strictEqual(JSON.stringify(candles), cbefore);

// ---- candle rows with garbage in them do not break the lookup ----
const dirty = candles.concat([null, [], ['x'], [NaN, 1, 1, 1, 1]]);
assert.strictEqual(F.overlayItems(fvg, dirty).gaps.length, 3);

// ---- labels carry the direction in words (never colour alone) ----
assert.strictEqual(F.label('bullish'), 'Bull FVG');
assert.strictEqual(F.label('bearish'), 'Bear FVG');
console.log('fvg.js: all checks passed');
