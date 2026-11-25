// Run with: node tests/chart_style_test.js   (also run by tests/test_chart_style_js.py)
// Candle colour semantics, hex handling, contrast warnings and the structure-overlay lookups.
const assert = require('assert');
const S = require('../static/js/chart_style.js');

// ---- hex ----
assert.strictEqual(S.normalizeHex('#abc'), '#AABBCC');
assert.strictEqual(S.normalizeHex('#26a69a'), '#26A69A');
assert.strictEqual(S.normalizeHex(' #fff '), '#FFFFFF');
for (const bad of ['red', '#12', '#GGGGGG', '#fff;}', 'javascript:x', '', null, undefined, 5, '#1234567']) {
  assert.strictEqual(S.normalizeHex(bad), null, String(bad));
}

// ---- candle semantics: close > open bull, close < open bear, equal = doji ----
assert.strictEqual(S.candleKind(1.1, 1.2), 'bull');
assert.strictEqual(S.candleKind(1.2, 1.1), 'bear');
assert.strictEqual(S.candleKind(1.1, 1.1), 'doji');
assert.strictEqual(S.candleKind(0, 0), 'doji');
const style = { bull: '#800080', bear: '#000000' };
assert.strictEqual(S.candleColor('bull', style, '#B7C2DA'), '#800080');
assert.strictEqual(S.candleColor('bear', style, '#B7C2DA'), '#000000');
assert.strictEqual(S.candleColor('doji', style, '#B7C2DA'), '#B7C2DA');   // neutral, never a bull/bear colour
const ohlc = [1, 1.2, 0.9, 1.1];
const before = JSON.stringify(ohlc);
S.candleKind(ohlc[0], ohlc[3]);
assert.strictEqual(JSON.stringify(ohlc), before);                         // data untouched

// ---- presets ----
const presets = [
  { id: 'classic', bull_color: '#26A69A', bear_color: '#EF5350' },
  { id: 'purple_black', bull_color: '#800080', bear_color: '#000000' },
  { id: 'arctic_rose', bull_color: '#55D6E8', bear_color: '#FF6B8A',
    theme_colors: { dark: { bull_color: '#55D6E8', bear_color: '#FF6B8A' }, light: { bull_color: '#007C91', bear_color: '#C2185B' } } },
];
assert.strictEqual(S.matchPreset('#800080', '#000000', presets), 'purple_black');
assert.strictEqual(S.matchPreset('#800080', '#000', presets), 'purple_black');
assert.strictEqual(S.matchPreset('#800080', '#000001', presets), 'custom');
assert.strictEqual(S.matchPreset('#26a69a', '#ef5350', presets), 'classic');
assert.strictEqual(S.matchPreset('#26A69A', '#800080', presets), 'custom');   // mixing two presets is custom
assert.strictEqual(S.matchPreset('#007C91', '#C2185B', presets), 'arctic_rose'); // the light-theme variant still identifies its preset

// ---- contrast: white / black candles on both themes ----
assert.ok(Math.abs(S.contrastRatio('#000000', '#FFFFFF') - 21) < 1e-9);
assert.strictEqual(S.contrastRatio('nope', '#FFFFFF'), null);
assert.strictEqual(S.isLowContrast('#000000', 'dark'), true);             // black on the dark chart
assert.strictEqual(S.isLowContrast('#FFFFFF', 'dark'), false);
assert.strictEqual(S.isLowContrast('#FFFFFF', 'light'), true);            // white on the light chart
assert.strictEqual(S.isLowContrast('#000000', 'light'), false);
assert.strictEqual(S.isLowContrast('#001F3F', 'dark'), true);             // navy on dark
assert.strictEqual(S.isLowContrast('#26A69A', 'dark'), false);
for (const preset of presets.filter(p => p.theme_colors)) {
  for (const [theme, colors] of Object.entries(preset.theme_colors)) {
    assert.ok(S.contrastRatio(colors.bull_color, S.CHART_BG[theme]) >= 3);
    assert.ok(S.contrastRatio(colors.bear_color, S.CHART_BG[theme]) >= 3);
  }
}
assert.strictEqual(S.contrastWarning('#26A69A', '#EF5350', 'dark'), '');
assert.match(S.contrastWarning('#FFFFFF', '#000000', 'dark'), /bearish/);
assert.match(S.contrastWarning('#FFFFFF', '#000000', 'light'), /bullish/);
assert.match(S.contrastWarning('#FFFFFF', '#000000', 'light'), /kept/);   // the user's colours are never replaced

// ---- structure overlay lookup: placed by candle time, never invented ----
const candles = [[100, 1, 2, 0, 1], [160, 1, 3, 1, 2], [220, 2, 4, 2, 3], [280, 3, 4, 2, 3]];
const structure = {
  swings: [
    { id: 'high:160', kind: 'high', pivot_time: 160, price: 3, classification: 'HH', confirmed: true },
    { id: 'low:100', kind: 'low', pivot_time: 100, price: 0, classification: null, confirmed: true },      // unclassified: no label
    { id: 'high:999', kind: 'high', pivot_time: 999, price: 9, classification: 'LH', confirmed: true },     // time not on the chart
  ],
  events: [
    { id: 'e1', type: 'BOS', direction: 'bullish', level: 3, reference_pivot_time: 160, candle_time: 280, confirmed: true },
    { id: 'e2', type: 'CHOCH', direction: 'bearish', level: 1, reference_pivot_time: 555, candle_time: 280, confirmed: true },  // unknown reference
  ],
};
const items = S.overlayItems(structure, candles);
assert.deepStrictEqual(items.swings.map(s => [s.index, s.label]), [[1, 'HH']]);
assert.deepStrictEqual(items.events.map(e => [e.from, e.to, e.type]), [[1, 3, 'BOS']]);
assert.deepStrictEqual(S.overlayItems(null, candles), { swings: [], events: [] });
assert.deepStrictEqual(S.overlayItems(structure, []), { swings: [], events: [] });
// calling it twice gives the same thing (nothing accumulates)
assert.deepStrictEqual(S.overlayItems(structure, candles), items);
// another symbol's candles: nothing matches, nothing is drawn
assert.deepStrictEqual(S.overlayItems(structure, [[5000, 1, 2, 0, 1]]), { swings: [], events: [] });

console.log('chart_style: all checks passed');
