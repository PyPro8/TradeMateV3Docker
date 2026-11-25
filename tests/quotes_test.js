// Run with: node tests/quotes_test.js (also run by tests/test_quotes_js.py). Live / stale / unavailable rules for the bid/ask lines.
const assert = require('assert');
const Q = require('../static/js/quotes.js');

const sym = (o) => ({ name: 'EURUSD', bid: 1.10000, ask: 1.10012, point: 0.00001, ...o });
const base = { symbols: [sym()], name: 'EURUSD', connected: true, receivedAt: 1000, now: 1500, maxAgeMs: 7000, statusAgeSeconds: 0.5, maxStatusAgeSeconds: 10 };

// ---- live: correct bid/ask mapping, spread from the ACTUAL bid and ask ----
let q = Q.quoteState(base);
assert.strictEqual(q.state, 'live'); assert.strictEqual(q.bid, 1.10000); assert.strictEqual(q.ask, 1.10012);
assert.strictEqual(q.spreadPoints, 12); assert.ok(Math.abs(q.spreadPrice - 0.00012) < 1e-12);
q = Q.quoteState({ ...base, symbols: [sym({ bid: 38000.0, ask: 38002.0, point: 0.1 })] });
assert.strictEqual(q.spreadPoints, 20);                                       // 2.0 price units / 0.1 point: points are not pips or money
q = Q.quoteState({ ...base, symbols: [sym({ point: undefined })] });
assert.strictEqual(q.state, 'live'); assert.strictEqual(q.spreadPoints, null);  // no point size: spread in points is not invented
q = Q.quoteState({ ...base, symbols: [sym({ spread_points: 99 })] });
assert.strictEqual(q.spreadPoints, 12);                                       // a stored spread field is NOT used; it is calculated

// ---- the ask is never derived from the bid ----
q = Q.quoteState({ ...base, symbols: [sym({ ask: null })] });
assert.strictEqual(q.state, 'unavailable'); assert.strictEqual(q.ask, null); assert.strictEqual(q.bid, null);

// ---- stale: no recent successful poll, or an old bridge snapshot ----
q = Q.quoteState({ ...base, now: 1000 + 7001 });
assert.strictEqual(q.state, 'stale'); assert.strictEqual(q.bid, null); assert.match(q.reason, /7 s ago/);
assert.strictEqual(Q.quoteState({ ...base, now: 1000 + 7000 }).state, 'live');          // exactly at the limit is still fresh
q = Q.quoteState({ ...base, statusAgeSeconds: 11 });
assert.strictEqual(q.state, 'stale'); assert.match(q.reason, /11 s old/);

// ---- unavailable ----
for (const [over, word] of [[{ connected: false }, 'not connected'], [{ receivedAt: undefined }, 'No quote has been received'], [{ name: '' }, 'No symbol'],
  [{ name: 'XAUUSD' }, 'not in the streamed watch list'], [{ symbols: [] }, 'watch list'], [{ symbols: null }, 'watch list'],
  [{ symbols: [sym({ bid: 0 })] }, 'no valid bid/ask'], [{ symbols: [sym({ bid: -1 })] }, 'no valid bid/ask'], [{ symbols: [sym({ bid: NaN })] }, 'no valid bid/ask'],
  [{ symbols: [sym({ ask: Infinity })] }, 'no valid bid/ask'], [{ symbols: [sym({ bid: '1.1' })] }, 'no valid bid/ask'],
  [{ symbols: [sym({ bid: 1.2, ask: 1.1 })] }, 'crossed']]) {
  q = Q.quoteState({ ...base, ...over });
  assert.strictEqual(q.state, 'unavailable', JSON.stringify(over)); assert.ok(q.reason.includes(word), q.reason + ' / ' + word);
  assert.strictEqual(q.bid, null); assert.strictEqual(q.ask, null);
}
assert.strictEqual(Q.quoteState(null).state, 'unavailable');

// ---- symbol switching: only the SELECTED symbol's quote is ever returned ----
const two = [sym(), sym({ name: 'XAUUSD', bid: 2000.1, ask: 2000.4, point: 0.01 })];
assert.strictEqual(Q.quoteState({ ...base, symbols: two, name: 'XAUUSD' }).bid, 2000.1);
assert.strictEqual(Q.quoteState({ ...base, symbols: two, name: 'EURUSD' }).bid, 1.10000);
assert.strictEqual(Q.quoteState({ ...base, symbols: [two[1]], name: 'EURUSD' }).state, 'unavailable');   // old symbol's price is not reused

// ---- badge text ----
assert.strictEqual(Q.badge(Q.quoteState(base), 5), 'Spread 12 pts \u00b7 0.00012');
assert.ok(Q.badge(Q.quoteState({ ...base, now: 99999 }), 5).startsWith('Live quotes STALE:'));
assert.ok(Q.badge(Q.quoteState({ ...base, connected: false }), 5).startsWith('Live quotes UNAVAILABLE:'));
console.log('quotes.js: all checks passed');
