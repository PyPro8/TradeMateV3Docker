// Run with: node tests/outcome_test.js   (also run by tests/test_outcome_js.py)
// Every outcome must look different, and ONLY a confirmed execution may look like success.
const assert = require('assert');
const O = require('../static/js/outcome.js');
const req = { symbol: 'XAUUSD', side: 'buy', volume: 0.01, sl: 2300.5, tp: 2310 };

let r = O.describe({ state: 'rejected_risk', outcome: 'risk_rejected', order_submitted: false, request: req,
  message: 'Trade exceeds maximum risk per trade.', risk: { requested_risk_amount: 5.12, max_risk_amount: 1, currency: 'USD', requested_risk_percent: 0.51, max_risk_percent: 0.1 } });
assert.strictEqual(r.kind, 'fail'); assert.match(r.title, /RISK REJECTED/);
for (const s of ['Symbol: XAUUSD', 'Direction: BUY', 'Requested risk: 5.12 USD', 'Maximum permitted risk: 1.00 USD', 'Order submitted to MT5: NO', 'exceeds maximum']) assert.ok(r.text.includes(s), s + ' missing in\n' + r.text);

r = O.describe({ state: 'failed', outcome: 'broker_rejected', order_submitted: true, request: req, message: 'Order rejected by MT5/broker: No money (retcode 10019)', broker: { retcode: 10019, message: 'No money' } });
assert.strictEqual(r.kind, 'fail'); assert.match(r.title, /BROKER REJECTED/);
assert.ok(r.text.includes('Retcode: 10019') && r.text.includes('Broker explanation: No money') && r.text.includes('Order submitted to MT5: YES'));
assert.ok(!/RISK/.test(r.title), 'a broker rejection must never be called a risk rejection');

r = O.describe({ state: 'failed', outcome: 'execution_refused', order_submitted: false, request: req, message: 'Trading is disabled.' });
assert.strictEqual(r.kind, 'fail'); assert.match(r.title, /EXECUTION ENGINE REFUSED/); assert.ok(r.text.includes('Order submitted to MT5: NO'));

r = O.describe({ state: 'failed', outcome: 'connection_failed', order_submitted: false, request: req, message: 'MT5 is not connected.' });
assert.strictEqual(r.kind, 'fail'); assert.match(r.title, /CONNECTION/); assert.ok(r.text.includes('Order submitted to MT5: NO'));

r = O.describe({ state: 'failed', outcome: 'invalid_request', order_submitted: false, request: {}, message: 'A stop-loss is required.' });
assert.strictEqual(r.kind, 'fail'); assert.match(r.title, /INVALID/);

r = O.describe({ state: 'unknown', outcome: 'unknown', order_submitted: null, request: req, message: 'No confirmation.' });
assert.strictEqual(r.kind, 'unknown'); assert.match(r.title, /UNKNOWN/);
assert.ok(r.text.includes('Automatic resubmission: DISABLED') && r.text.includes('Order submitted to MT5: UNKNOWN'));

r = O.describe({ state: 'unconfirmed', outcome: 'accepted_unconfirmed', order_submitted: true, request: req, message: 'Accepted.' });
assert.strictEqual(r.kind, 'unknown');

r = O.describe({ state: 'executed', outcome: 'executed', order_submitted: true, request: req, message: 'Executed: BUY 0.01 XAUUSD #123' });
assert.strictEqual(r.kind, 'ok'); assert.strictEqual(r.title, 'EXECUTED'); assert.ok(r.text.includes('BUY 0.01 XAUUSD #123'));

// Only 'executed' may be green: sweep every other outcome.
for (const o of Object.keys(O.TITLES)) if (o !== 'executed') assert.notStrictEqual(O.describe({ outcome: o, message: 'x' }).kind, 'ok', o);
// A success-looking state with a rejecting outcome must NOT be green.
assert.notStrictEqual(O.describe({ state: 'executed', outcome: 'broker_rejected', message: 'x' }).kind, 'ok');
// Legacy answers without `outcome` still map safely.
assert.strictEqual(O.describe({ state: 'failed', message: 'x' }).kind, 'fail');
assert.strictEqual(O.describe({ state: 'unknown', message: 'x' }).kind, 'unknown');
assert.strictEqual(O.describe(null).kind, 'fail');
console.log('outcome.js: all checks passed');
