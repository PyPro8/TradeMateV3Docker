// Run with: node tests/pl_test.js   (also run by tests/test_pl_js.py)
// Checks the pips/money maths against hand-computed values for different instrument types.
const assert = require('assert');
const PL = require('../static/js/pl.js');

const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) <= eps, `${a} !~ ${b}`);

const EURUSD = { digits: 5, point: 1e-5, tick_size: 1e-5, tick_value: 1, tick_value_profit: 1, tick_value_loss: 1 };
// USD account, USDJPY at 150.00: one tick (0.001 JPY) on 1 lot = 100 JPY = 0.6667 USD
const USDJPY = { digits: 3, point: 0.001, tick_size: 0.001, tick_value: 0.6667, tick_value_profit: 0.6667, tick_value_loss: 0.6667 };
// XAUUSD, 100 oz contract: one tick (0.01) on 1 lot = 1 USD
const XAUUSD = { digits: 2, point: 0.01, tick_size: 0.01, tick_value: 1, tick_value_profit: 1, tick_value_loss: 1 };
// Cross pair: a gain and a loss convert at different rates
const CROSS = { digits: 5, point: 1e-5, tick_size: 1e-5, tick_value: 0.9, tick_value_profit: 0.95, tick_value_loss: 0.85 };

// pip size
near(PL.pipSize(EURUSD), 1e-4); near(PL.pipSize(USDJPY), 0.01); near(PL.pipSize(XAUUSD), 0.01);
near(PL.pipSize({ digits: 4, point: 1e-4 }), 1e-4);
assert.strictEqual(PL.pipSize({}), null);

// BUY EURUSD 1 lot: TP +25 pips = +$250, SL -20 pips = -$200
near(PL.pips(EURUSD, 'buy', 1.1, 1.1025), 25);          near(PL.money(EURUSD, 'buy', 1.1, 1.1025, 1), 250);
near(PL.pips(EURUSD, 'buy', 1.1, 1.098), -20);          near(PL.money(EURUSD, 'buy', 1.1, 1.098, 1), -200);
// SELL EURUSD 0.5 lot: TP below entry is profit, SL above is loss
near(PL.pips(EURUSD, 'sell', 1.1, 1.0975), 25);         near(PL.money(EURUSD, 'sell', 1.1, 1.0975, 0.5), 125);
near(PL.pips(EURUSD, 'sell', 1.1, 1.102), -20);         near(PL.money(EURUSD, 'sell', 1.1, 1.102, 0.5), -100);
// a TP dragged to the wrong side shows a NEGATIVE value instead of hiding the mistake
assert.ok(PL.money(EURUSD, 'buy', 1.1, 1.095, 1) < 0);
// JPY pair, 0.1 lot, 50 pips -> 50 * 6.667 * 0.1 = 33.335
near(PL.pips(USDJPY, 'buy', 150, 150.5), 50);           near(PL.money(USDJPY, 'buy', 150, 150.5, 0.1), 33.335, 1e-3);
// Gold, 0.1 lot, +10.00 -> 1000 ticks * $1 * 0.1 = $100
near(PL.money(XAUUSD, 'buy', 2000, 2010, 0.1), 100);    near(PL.money(XAUUSD, 'sell', 2000, 2010, 0.1), -100);
// profit and loss use their own tick value
near(PL.money(CROSS, 'buy', 1.1, 1.101, 1), 100 * 0.95); near(PL.money(CROSS, 'buy', 1.1, 1.099, 1), -100 * 0.85);
// older bridge: only tick_value
near(PL.money({ tick_size: 1e-5, tick_value: 1 }, 'buy', 1.1, 1.1025, 1), 250);
// missing / invalid data -> null, never a guess
assert.strictEqual(PL.money(null, 'buy', 1.1, 1.2, 1), null);
assert.strictEqual(PL.money({ point: 1e-5, digits: 5 }, 'buy', 1.1, 1.2, 1), null);
assert.strictEqual(PL.money(EURUSD, 'buy', 1.1, 1.2, 0), null);
assert.strictEqual(PL.money(EURUSD, 'buy', 1.1, 1.2, NaN), null);
assert.strictEqual(PL.money(EURUSD, 'hold', 1.1, 1.2, 1), null);
assert.strictEqual(PL.money(EURUSD, 'buy', null, 1.2, 1), null);
assert.strictEqual(PL.pips(EURUSD, 'buy', 1.1, undefined), null);

// side inference for a level set before the order exists
assert.strictEqual(PL.inferSide('tp', 1.105, 1.1, 1.1001), 'buy');
assert.strictEqual(PL.inferSide('tp', 1.095, 1.1, 1.1001), 'sell');
assert.strictEqual(PL.inferSide('sl', 1.095, 1.1, 1.1001), 'buy');
assert.strictEqual(PL.inferSide('sl', 1.105, 1.1, 1.1001), 'sell');
assert.strictEqual(PL.inferSide('sl', 1.105, null, 1.1001), null);
assert.strictEqual(PL.entryFor('buy', 1.1, 1.1001), 1.1001);
assert.strictEqual(PL.entryFor('sell', 1.1, 1.1001), 1.1);

// formatting
assert.strictEqual(PL.fmtPips(25), '+25.0 pips');
assert.strictEqual(PL.fmtPips(-20.04), '\u221220.0 pips');
assert.strictEqual(PL.fmtPips(0), '0.0 pips');
assert.strictEqual(PL.fmtPips(null), '\u2014');
assert.strictEqual(PL.fmtMoney(null, 'USD'), '\u2014');
assert.ok(/^\+\$?125\.00$|^\+\$125\.00$/.test(PL.fmtMoney(125, 'USD')), PL.fmtMoney(125, 'USD'));
assert.ok(PL.fmtMoney(-100, 'USD').startsWith('\u2212'), PL.fmtMoney(-100, 'USD'));
assert.strictEqual(PL.fmtMoney(5, 'US CENTS'), '+5.00 US CENTS');   // invalid code falls back cleanly
assert.strictEqual(PL.fmtMoney(5, ''), '+5.00');
assert.strictEqual(PL.fmtMoney(0.001, 'USD').replace(/[^0-9.]/g, ''), '0.00');

// ---- Phase 5: P/L shown on the ENTRY line (positionPL / entryLabel) ----
const plSign = (v) => (v > 0 ? '+' : v < 0 ? '-' : '') + Math.abs(v).toFixed(2);
const pos = (direction, open, current, volume, extra = {}) => ({ direction, price_open: open, price_current: current, volume, profit: null, ...extra });
// BUY, price above entry -> positive; below -> negative (EURUSD 1 lot, 25 pips = 250)
let r = PL.positionPL(pos('buy', 1.1, 1.1025, 1), EURUSD);  assert.strictEqual(r.source, 'computed'); near(r.value, 250);
r = PL.positionPL(pos('buy', 1.1, 1.098, 1), EURUSD);        assert.ok(r.value < 0); near(r.value, -200);
// SELL, price below entry -> positive; above -> negative (NOT the BUY formula)
r = PL.positionPL(pos('sell', 1.1, 1.0975, 0.5), EURUSD);    assert.ok(r.value > 0); near(r.value, 125);
r = PL.positionPL(pos('sell', 1.1, 1.102, 0.5), EURUSD);     assert.ok(r.value < 0); near(r.value, -100);
// gold: BUY +10.00 on 0.52 lots = +520 ; SELL +10.00 against = -520
near(PL.positionPL(pos('buy', 2650, 2660, 0.52), XAUUSD).value, 520);
near(PL.positionPL(pos('sell', 2650, 2660, 0.52), XAUUSD).value, -520);
// cross pair: a loss converts with tick_value_loss, a gain with tick_value_profit
near(PL.positionPL(pos('buy', 1.1, 1.1001, 1), CROSS).value, 10 * 0.95);
near(PL.positionPL(pos('buy', 1.1, 1.0999, 1), CROSS).value, -10 * 0.85);
// MT5's own floating profit always wins over the derived value
r = PL.positionPL(pos('sell', 1.1, 1.102, 0.5, { profit: -7.2 }), EURUSD); assert.strictEqual(r.source, 'mt5'); near(r.value, -7.2);
r = PL.positionPL(pos('buy', 1.1, 1.1, 1, { profit: 0 }), EURUSD);          assert.strictEqual(r.source, 'mt5'); near(r.value, 0);
// no data -> null, never a made-up number
assert.strictEqual(PL.positionPL(pos('buy', 1.1, null, 1), EURUSD).value, null);
assert.strictEqual(PL.positionPL(pos('buy', 1.1, 1.2, 1), null).value, null);
assert.strictEqual(PL.positionPL(null, EURUSD).value, null);
// label: direction + explicit sign
assert.strictEqual(PL.entryLabel('buy', plSign(25.4) + ' USD'), 'BUY +25.40 USD');
assert.strictEqual(PL.entryLabel('buy', plSign(-12.7) + ' USD'), 'BUY -12.70 USD');
assert.strictEqual(PL.entryLabel('sell', plSign(22.1) + ' USD'), 'SELL +22.10 USD');
assert.strictEqual(PL.entryLabel('sell', plSign(-9.45) + ' USD'), 'SELL -9.45 USD');
// two positions on one symbol: each label uses ONLY its own position's data
const pa = pos('buy', 1.1, 1.1020, 1, { profit: 20 }), pb = pos('sell', 1.1, 1.1020, 1, { profit: -20 });
assert.strictEqual(PL.entryLabel(pa.direction, plSign(PL.positionPL(pa, EURUSD).value) + ' USD'), 'BUY +20.00 USD');
assert.strictEqual(PL.entryLabel(pb.direction, plSign(PL.positionPL(pb, EURUSD).value) + ' USD'), 'SELL -20.00 USD');
// zero P/L: no sign, consistent for both sides
assert.strictEqual(PL.entryLabel('buy', plSign(0) + ' USD'), 'BUY 0.00 USD');
assert.strictEqual(PL.entryLabel('sell', plSign(0) + ' USD'), 'SELL 0.00 USD');
console.log('entry-line P/L checks passed');

console.log('pl.js: all checks passed');
