// Run with: node tests/slcalc_test.js (also run by tests/test_slcalc_js.py). Pure logic of the two-way stop-loss panel.
const assert = require('assert');
const S = require('../static/js/slcalc.js');

// ---- number parsing: only plain finite numbers ----
assert.strictEqual(S.num('12.5'), 12.5);
assert.strictEqual(S.num(' 500 '), 500);
assert.strictEqual(S.num('.5'), 0.5);
for (const bad of ['', '  ', 'abc', '1,5', 'NaN', 'Infinity', '-Infinity', '1e999', '0x10', null, undefined]) assert.strictEqual(S.num(bad), null, String(bad));

// ---- requests ----
const base = { mode: 'risk', symbol: 'EURUSD', direction: 'buy', volume: 0.1, riskAmount: 50, stopLoss: null };
let r = S.buildRequest(base, 7);
assert.deepStrictEqual(r.body, { mode: 'risk', symbol: 'EURUSD', direction: 'buy', volume: 0.1, risk_amount: 50, request_id: 7 });
r = S.buildRequest({ ...base, mode: 'price', stopLoss: 1.095 }, 8);
assert.deepStrictEqual(r.body, { mode: 'price', symbol: 'EURUSD', direction: 'buy', volume: 0.1, stop_loss: 1.095, request_id: 8 });
assert.ok(!('risk_amount' in r.body));
for (const [over, word] of [[{ symbol: '' }, 'symbol'], [{ direction: 'hold' }, 'BUY or SELL'], [{ volume: 0 }, 'lot'], [{ volume: null }, 'lot'],
  [{ riskAmount: 0 }, 'amount'], [{ riskAmount: null }, 'amount'], [{ mode: 'price', stopLoss: null }, 'stop-loss'], [{ mode: 'price', stopLoss: -1 }, 'stop-loss']]) {
  const e = S.buildRequest({ ...base, ...over }, 1);
  assert.ok(e.error && !e.body, JSON.stringify(over)); assert.ok(e.error.toLowerCase().includes(word.toLowerCase()), e.error);
}

// ---- stale detection: the key changes with every input that matters, and ignores the typed value of the OTHER mode ----
const k = S.inputsKey(base);
for (const over of [{ symbol: 'XAUUSD' }, { direction: 'sell' }, { volume: 0.2 }, { riskAmount: 51 }, { mode: 'price' }]) assert.notStrictEqual(S.inputsKey({ ...base, ...over }), k, JSON.stringify(over));
assert.strictEqual(S.inputsKey({ ...base, stopLoss: 9.9 }), k);           // risk mode ignores the SL field (it is an OUTPUT there)
assert.strictEqual(S.inputsKey({ ...base, mode: 'price', stopLoss: 1.1, riskAmount: 1 }), S.inputsKey({ ...base, mode: 'price', stopLoss: 1.1, riskAmount: 999 }));

// ---- switching modes: never overwrite typed input, no auto-recalculation loop ----
const last = { valid: true, risk_amount: 49.987 };
let sw = S.switchMode('price', 'risk', { riskText: '', slText: '1.0952' }, last);
assert.strictEqual(sw.riskText, '49.99'); assert.strictEqual(sw.slText, '1.0952'); assert.strictEqual(sw.recalc, false);
sw = S.switchMode('price', 'risk', { riskText: '300', slText: '1.0952' }, last);
assert.strictEqual(sw.riskText, '300');                                    // what the user typed is never replaced
sw = S.switchMode('price', 'risk', { riskText: '', slText: '1.0952' }, { valid: false });
assert.strictEqual(sw.riskText, '');                                       // nothing valid to start from: stays empty
sw = S.switchMode('risk', 'price', { riskText: '300', slText: '1.0952' }, last);
assert.deepStrictEqual([sw.riskText, sw.slText], ['300', '1.0952']);       // the calculated SL stays, editable
assert.deepStrictEqual(S.switchMode('risk', 'risk', { riskText: 'x', slText: 'y' }, last), { riskText: 'x', slText: 'y', recalc: false });

// ---- submit gate ----
const ok = { key: S.inputsKey({ ...base, stopLoss: 1.0952 }), pending: false, result: { valid: true, stop_loss: 1.0952 } };
const cur = { ...base, stopLoss: 1.0952 };
assert.ok(S.submitGate({ ...cur, mode: 'price' }, null, 'buy').ok);        // price mode: the server judges, nothing extra here
assert.ok(S.submitGate(cur, ok, 'buy').ok);
assert.ok(!S.submitGate(cur, ok, 'sell').ok);                              // calculated for BUY, you pressed SELL
assert.match(S.submitGate(cur, ok, 'sell').why, /calculated for BUY/);
assert.ok(!S.submitGate(cur, null, 'buy').ok);
assert.ok(!S.submitGate(cur, { ...ok, pending: true }, 'buy').ok);
assert.ok(!S.submitGate({ ...cur, riskAmount: 60 }, ok, 'buy').ok);        // inputs changed after the result
assert.ok(!S.submitGate(cur, { ...ok, result: { valid: false, message: 'Too small' } }, 'buy').ok);
assert.match(S.submitGate(cur, { ...ok, result: { valid: false, message: 'Too small' } }, 'buy').why, /Too small/);
assert.ok(!S.submitGate({ ...cur, stopLoss: 1.09 }, ok, 'buy').ok);        // the SL field was changed behind the calculation's back
assert.match(S.submitGate({ ...cur, stopLoss: 1.09 }, ok, 'buy').why, /no longer matches/);

// ---- what is shown ----
let d = S.describe({ entry_source: 'live', digits: 5, limit: { checked: true, ok: true, message: 'Within your per-trade limit (0.50% of equity, limit 1% = 100.00 USD).' },
  result: { mode: 'risk', valid: true, stop_loss: 1.09512, requested_risk: 50.05, risk_amount: 50.0, difference: 0.05, entry: 1.10012, stop_ticks: 500, volume: 0.1, currency: 'USD',
            notes: ['Prices move in steps...'], quantity_note: 'Risk amount = the money lost if the stop-loss is hit.' } }, 'USD', 5);
assert.strictEqual(d.kind, 'ok');
const text = d.lines.join('\n');
for (const s of ['Stop-loss price: 1.09512', 'Requested risk: 50.05 USD', 'Final risk at that stop: 50.00 USD', 'Difference: 0.05 USD less', 'live market price', 'Within your per-trade limit']) assert.ok(text.includes(s), s + '\n' + text);
assert.ok(d.quantity.includes('not') || d.quantity.includes('money lost'));

d = S.describe({ entry_source: 'provided', digits: 5, limit: { checked: true, ok: false, message: 'Above your per-trade limit: 2.00% ...' },
  result: { mode: 'price', valid: true, risk_amount: 200, entry: 1.1, stop_ticks: 2000, volume: 0.1, currency: 'USD', notes: [] } }, 'USD', 5);
assert.strictEqual(d.kind, 'bad'); assert.ok(d.lines.join('\n').includes('Loss if the stop-loss is hit: 200.00 USD')); assert.ok(d.lines.join('\n').includes('as entered'));

d = S.describe({ limit: { checked: false, message: 'Limit not checked: ...' }, result: { mode: 'price', valid: true, risk_amount: 5, entry: 1, stop_ticks: 1, volume: 1, currency: 'NGN', notes: [] } }, 'NGN', 2);
assert.strictEqual(d.kind, 'warn'); assert.ok(d.lines.join('').includes('NGN'));                   // currency is the account's, not assumed

d = S.describe({ result: { mode: 'price', valid: false, message: 'Not a valid price.', nearest_valid_stop: 1.09513 } }, 'USD', 5);
assert.strictEqual(d.kind, 'bad'); assert.ok(d.lines.join('\n').includes('Nearest valid stop-loss: 1.09513'));
assert.strictEqual(S.describe(null, 'USD', 5).kind, 'bad');
assert.strictEqual(S.describe({ message: 'MT5 data is not available.' }, 'USD', 5).kind, 'bad');
console.log('slcalc.js: all checks passed');
