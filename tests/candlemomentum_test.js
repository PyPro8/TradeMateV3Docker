// Node checks for static/js/candlemomentum.js (the Candle Momentum panel's pure logic). Run: node tests/candlemomentum_test.js
const assert = require('assert');
const cm = require('../static/js/candlemomentum.js');
let n = 0;
function check(name, fn) { fn(); n++; }

const base = { ok: true, symbol: 'EURUSD', timeframe: 'M5', server_time: 1000, config: { trigger_units: 10, target_units: 10 },
  conversion: { status: 'VERIFIED', digits: 5, unit: 0.0001, trigger_price_distance: 0.001 }, quote: { bid: 1.1004, ask: 1.10052 },
  engine: { running: true, cadence_seconds: 0.25 }, watchdog: { active: true }, autopilot: { armed: false }, feed: { price_updates_per_second: 1 },
  machine: { current: { state: 'BULLISH', open: 1.1, price: 1.1004, direction: 'bullish', trigger_price: 1.101, updated_at: 999, reason: 'Price is above the open.',
                         risk_decision: { status: 'none' }, entry_price: null, target_price: null, plan_target_price: 1.102, transitions: [] } } };
const get = (d, label) => cm.rows(d).filter(r => r.label === label)[0];

check('every one of the 16 lifecycle states has a label, a tone and a plain-English meaning', () => {
  const names = ['NEW_CANDLE','MONITORING','BULLISH','BEARISH','TRIGGER_REACHED','ENTRY_VALIDATION','RISK_PENDING','EXECUTING','POSITION_OPEN','TARGET_REACHED','POSITION_CLOSED','INVALIDATED','MISSED','REJECTED','EXECUTION_FAILED','EXPIRED'];
  assert.deepStrictEqual(Object.keys(cm.STATES).sort(), names.slice().sort());
  names.forEach(s => { const i = cm.stateInfo(s); assert.ok(i[0] && i[1] && i[2].length > 20, s); });
});
check('distance to trigger is in strategy units, computed from the instrument unit (not a forex constant)', () => {
  assert.strictEqual(Math.round(cm.distanceToTrigger(base.machine.current, 0.0001) * 1000) / 1000, 6);
  assert.strictEqual(cm.distanceToTrigger({ direction: 'bullish', price: 1000, trigger_price: 1010 }, 1.0), 10);            // an index with 1 price unit per pip
});
check('a reached trigger reads 0, a bearish one measures downward', () => {
  assert.strictEqual(cm.distanceToTrigger({ direction: 'bullish', price: 1.1011, trigger_price: 1.101 }, 0.0001), 0);
  assert.strictEqual(cm.distanceToTrigger({ direction: 'bearish', price: 1.0995, trigger_price: 1.099 }, 0.0001) > 4.9, true);
});
check('unknown inputs are dashes, never invented numbers', () => {
  assert.strictEqual(cm.distanceToTrigger(null, 0.0001), null);
  assert.strictEqual(cm.distanceToTrigger({ direction: 'bullish', price: 1, trigger_price: 2 }, null), null);
  assert.strictEqual(cm.fmtPrice(null, 5), '\u2014');
  assert.deepStrictEqual(cm.rows(null), []);
  assert.strictEqual(get({ ...base, machine: { current: null } }, 'Trigger price').value, '\u2014');
});
check('prices use the instrument digits', () => {
  assert.strictEqual(get(base, 'Candle open').value, '1.10000');
  assert.strictEqual(get(base, 'Live price (bid / ask)').value, '1.10040 / 1.10052');
  assert.strictEqual(get(base, 'Trigger price').value, '1.10100');
});
check('the grid carries every field the Command Centre must show', () => {
  ['Strategy','Instrument / timeframe','Candle open','Live price (bid / ask)','Candle direction','Trigger distance','Trigger price','Distance remaining to trigger','Target distance',
   'Expected target price','Actual entry price','Lifecycle state','Watchdog','Autopilot','Risk Engine decision','Last update','Engine','Instrument conversion'].forEach(l => assert.ok(get(base, l), l));
});
check('before a fill the target is a plan; after a fill it is measured from the actual fill', () => {
  assert.ok(get(base, 'Expected target price').value.includes('plan'));
  const filled = JSON.parse(JSON.stringify(base)); Object.assign(filled.machine.current, { state: 'POSITION_OPEN', entry_price: 1.10112, target_price: 1.10212 });
  assert.ok(get(filled, 'Expected target price').value.includes('actual fill'));
  assert.strictEqual(get(filled, 'Actual entry price').value, '1.10112');
  assert.strictEqual(get(base, 'Actual entry price').value, 'Not executed');
});
check('watchdog / autopilot / unverified conversion are reported honestly', () => {
  assert.ok(get({ ...base, watchdog: { active: false } }, 'Watchdog').value.startsWith('Not running'));
  assert.strictEqual(get({ ...base, autopilot: { armed: true } }, 'Autopilot').value, 'On');
  const bad = get({ ...base, conversion: { status: 'UNVERIFIED', reasons: ['tick value missing'] } }, 'Instrument conversion');
  assert.ok(bad.value.includes('tick value missing') && bad.tone === 'bad');
});
check('the engine cadence is shown separately from the page refresh', () => {
  assert.ok(get(base, 'Engine').value.includes('every 0.25 s'));
  assert.ok(get(base, 'Engine').value.includes('broker price updates 1/s'));   // distinct from the engine's own cadence
  assert.ok(get({ ...base, engine: { running: false } }, 'Engine').value === 'Stopped');
});
check('the exact reason for a missed / rejected entry is displayed', () => {
  const d = JSON.parse(JSON.stringify(base)); Object.assign(d.machine.current, { state: 'MISSED', reason: 'MISSED: price is 7.2 units beyond the trigger.' });
  assert.strictEqual(cm.explanation(d), 'MISSED: price is 7.2 units beyond the trigger.');
  assert.strictEqual(cm.explanation({ machine: { current: null } }), 'Waiting for the broker\u2019s forming candle.');
});
check('event timestamps show a real local date/time and elapsed age instead of NOW', () => {
  const now = 1700000300;
  const then = 1700000000;
  const label = cm.eventTime(now, then);
  assert.ok(label.includes('300 seconds ago') === false);
  assert.ok(label.includes('5 minutes ago'));
  assert.ok(label.includes(new Date(then * 1000).toLocaleString()));
  assert.ok(!/^NOW$/i.test(label));
});
check('the settings form is sent as typed; empty slippage means "not set"; nothing else is defaulted', () => {
  const b = cm.formBody('EURUSD', 'M5', { trigger_units: ' 5 ', target_units: '12', max_slippage_units: '', sl_buffer_units: '' }, true);
  assert.deepStrictEqual(b, { symbol: 'EURUSD', timeframe: 'M5', enabled: true, trigger_units: 5, target_units: 12, max_slippage_units: null });
  assert.ok(Number.isNaN(cm.formBody('X', 'M5', { trigger_units: 'abc' }, true).trigger_units));                    // sent as NaN -> the server refuses it
});
check('a feed problem is surfaced', () => {
  const r = cm.rows({ ...base, feed: { problem: 'MT5 is not connected' } }).filter(x => x.label === 'Feed problem')[0];
  assert.ok(r && r.tone === 'bad');
});
console.log('candlemomentum_test.js: ' + n + ' groups, all checks passed');
