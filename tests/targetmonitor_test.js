// Run with: node tests/targetmonitor_test.js   (also run by tests/test_targetmonitor_js.py)
const assert = require('assert');
const TM = require('../static/js/targetmonitor.js');
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} !~ ${b}`);
const ap = (o) => Object.assign({ ok: true, state: 'running', armed: true, mode: 'profit', mode_label: 'Profit target', value: 10000, net_pnl: 7500, currency: 'USD' }, o);

// positive progress: 7500 of 10000 -> 75 %, 2500 remaining, currency is the account's (never assumed)
let m = TM.model(ap(), { brokerConnected: true, ageMs: 100 });
assert.strictEqual(m.kind, 'progress'); near(m.fraction, 0.75); near(m.percent, 75); near(m.remaining, 2500);
assert.strictEqual(m.headline, '75% COMPLETE'); assert.strictEqual(m.currency, 'USD'); assert.strictEqual(m.status, 'RUNNING'); assert.strictEqual(m.reached, false);
// zero progress
m = TM.model(ap({ net_pnl: 0 })); near(m.fraction, 0); near(m.remaining, 10000); assert.strictEqual(m.kind, 'progress');
// negative session profit: the arc stays empty, the figure is kept negative, the remaining is the target plus the loss
m = TM.model(ap({ net_pnl: -1500 })); near(m.fraction, 0); near(m.percent, -15); near(m.remaining, 11500); assert.strictEqual(m.negative, true); assert.strictEqual(m.kind, 'progress'); assert.strictEqual(m.headline, 'BELOW START (-15%)');
// reached and exceeded: the arc is capped at full, remaining is 0
m = TM.model(ap({ net_pnl: 10000 })); assert.strictEqual(m.reached, true); near(m.fraction, 1); near(m.remaining, 0); assert.strictEqual(m.kind, 'reached');
m = TM.model(ap({ net_pnl: 12500, state: 'finished' })); near(m.fraction, 1); assert.strictEqual(m.status, 'TARGET REACHED'); near(m.percent, 125);
// states
assert.strictEqual(TM.model(ap({ state: 'paused', paused: true })).status, 'PAUSED');
assert.strictEqual(TM.model(ap({ state: 'stopped', armed: false })).status, 'STOPPED');
assert.strictEqual(TM.model(ap({ state: 'emergency_stopped' })).statusTone, 'bad');
// zero / invalid target never divides by zero
for (const v of [0, -5, null, 'x', NaN]) { m = TM.model(ap({ value: v })); assert.strictEqual(m.kind, 'zero-target'); assert.strictEqual(m.percent, null); }
// another stop condition: no target is invented, and the reason is stated
m = TM.model(ap({ mode: 'runs', mode_label: 'Number of runs', value: 5 })); assert.strictEqual(m.kind, 'no-target'); assert.strictEqual(m.target, null); assert.ok(/Number of runs/.test(m.detail));
m = TM.model(ap({ mode: null, net_pnl: 0 })); assert.strictEqual(m.kind, 'no-target'); assert.strictEqual(m.session, null);
// missing data
m = TM.model(null, {}); assert.strictEqual(m.kind, 'loading');
m = TM.model(null, { failed: true }); assert.strictEqual(m.kind, 'unavailable');
m = TM.model(ap({ net_pnl: undefined })); assert.strictEqual(m.kind, 'no-session');
m = TM.model(ap({ currency: undefined })); assert.strictEqual(m.currency, '');
// disconnected broker and stale data are flagged, not hidden
assert.strictEqual(TM.model(ap(), { brokerConnected: false }).brokerDown, true);
assert.strictEqual(TM.model(ap(), { ageMs: TM.STALE_MS + 1 }).stale, true);
assert.strictEqual(TM.model(ap(), { ageMs: 1000 }).stale, false);
// balance / equity are not inputs: a payload that carries them changes nothing
near(TM.model(ap({ balance: 99999, equity: 99999 })).fraction, 0.75);
console.log('targetmonitor: all checks passed');
