// Run with: node tests/clock_test.js   (also run by tests/test_clock_js.py)
// Checks static/js/clocks.js: the two clocks (Eastern Time with EST/EDT), the candle countdown maths and the DOM mounter.
// Pure functions + a fake DOM: no browser, no network, no broker. Every instant below is a fixed UTC time, so the results do not
// depend on the computer's own timezone or on today's date.
const assert = require('assert');
const C = require('../static/js/clocks.js');

let checks = 0;
const eq = (actual, expected, msg) => { checks++; assert.strictEqual(actual, expected, `${msg || ''} -> got ${JSON.stringify(actual)}, expected ${JSON.stringify(expected)}`); };
const utc = (s) => Date.parse(s);
const ET = C.EASTERN;
const clock = (iso, zone) => { const c = C.formatClock(utc(iso), zone); return `${c.time} ${c.zone}`; };

// ---------------------------------------------------------------- Eastern Time: standard and daylight time
eq(ET, 'America/New_York', 'Eastern Time uses the IANA zone, not a hand-written offset');
eq(clock('2026-01-15T12:00:00Z', ET), '07:00:00 EST', 'January is EST (UTC-5)');
eq(clock('2026-07-15T12:00:00Z', ET), '08:00:00 EDT', 'July is EDT (UTC-4)');
eq(clock('2026-10-19T12:00:00Z', ET), '08:00:00 EDT', '19 Oct 2026 is still EDT');

// DST starts Sunday 8 March 2026 at 02:00 EST (= 07:00 UTC): 01:59:59 EST -> 03:00:00 EDT, no 02:xx exists
eq(clock('2026-03-08T06:59:59Z', ET), '01:59:59 EST', 'one second before spring-forward');
eq(clock('2026-03-08T07:00:00Z', ET), '03:00:00 EDT', 'spring-forward: the clock jumps to 03:00 EDT');
// DST ends Sunday 1 November 2026 at 02:00 EDT (= 06:00 UTC): 01:59:59 EDT -> 01:00:00 EST (that hour happens twice)
eq(clock('2026-11-01T05:30:00Z', ET), '01:30:00 EDT', 'first 01:30 of fall-back day (EDT)');
eq(clock('2026-11-01T05:59:59Z', ET), '01:59:59 EDT', 'one second before fall-back');
eq(clock('2026-11-01T06:00:00Z', ET), '01:00:00 EST', 'fall-back: the clock returns to 01:00 EST');
eq(clock('2026-11-01T06:30:00Z', ET), '01:30:00 EST', 'second 01:30 of fall-back day (EST)');
// DST rules for a later year too (no table of dates in the code)
eq(clock('2027-03-14T06:59:59Z', ET), '01:59:59 EST', '2027 spring-forward is 14 March');
eq(clock('2027-03-14T07:00:00Z', ET), '03:00:00 EDT', '2027 spring-forward');

// midnight and date rollover in Eastern Time
eq(clock('2026-07-15T04:00:00Z', ET), '00:00:00 EDT', 'midnight EDT is 04:00 UTC');
eq(C.formatClock(utc('2026-07-15T03:59:59Z'), ET).date, 'Tue 14 Jul', 'the date is still 14 July one second before ET midnight');
eq(C.formatClock(utc('2026-07-15T04:00:00Z'), ET).date, 'Wed 15 Jul', 'the date rolls over at ET midnight');
eq(clock('2026-01-01T05:00:00Z', ET), '00:00:00 EST', 'midnight EST is 05:00 UTC');

// ---------------------------------------------------------------- the local / configured clock
eq(clock('2026-07-15T12:00:00Z', 'Asia/Manila'), '20:00:00 GMT+8', 'configured zone without DST');
eq(clock('2026-01-15T12:00:00Z', 'Australia/Sydney'), '23:00:00 GMT+11', 'southern-hemisphere DST (January)');
eq(clock('2026-07-15T12:00:00Z', 'Australia/Sydney'), '22:00:00 GMT+10', 'southern-hemisphere standard time (July)');
eq(clock('2026-07-15T12:00:00Z', 'UTC'), '12:00:00 UTC', 'UTC');
eq(C.formatClock(utc('2026-07-15T12:00:00Z'), 'Asia/Manila').zoneName, 'Asia/Manila', 'the zone name is reported for the label');
eq(C.validZone('America/New_York'), true, 'known zone');
eq(C.validZone('Not/AZone'), false, 'unknown zone');
eq(C.validZone(''), false, 'empty is not a zone');
// an unknown configured zone must not break the page: it falls back to the browser zone and says so
const bad = C.formatClock(utc('2026-07-15T12:00:00Z'), 'Not/AZone');
eq(bad.fallback, true, 'fallback flagged');
assert.ok(/^\d\d:\d\d:\d\d$/.test(bad.time), 'a time is still produced'); checks++;
// empty zone = the browser's own zone: same instant as an explicit conversion to that zone
const browserZone = new Intl.DateTimeFormat().resolvedOptions().timeZone;
eq(C.formatClock(utc('2026-07-15T12:00:00Z'), '').time, C.formatClock(utc('2026-07-15T12:00:00Z'), browserZone).time, 'empty zone = browser zone');

// ---------------------------------------------------------------- countdown maths
// Broker clock = UTC+3. Candle times are the broker's wall clock stored as an epoch (that is how MT5 reports them).
const OFF = 3 * 3600;
const nowAt = (serverSeconds) => (serverSeconds - OFF) * 1000;        // a real Date.now() that corresponds to a given broker-clock second
const BASE = Date.UTC(2026, 9, 19, 0, 0, 0) / 1000;                   // 19 Oct 2026 00:00:00 on the BROKER clock (as an epoch)
const cd = (tf, open, serverNow, extra) => C.countdown(Object.assign({ tf, candleTime: open, offsetSeconds: OFF, nowMs: nowAt(serverNow) }, extra || {}));

// every timeframe: exact boundaries
const lengths = { M1: 60, M5: 300, M15: 900, M30: 1800, H1: 3600, H4: 14400, D1: 86400 };
for (const [tf, secs] of Object.entries(lengths)) {
  eq(C.TF_SECONDS[tf], secs, `${tf} length`);
  const open = BASE + 8 * 86400 - (8 * 86400) % secs;                  // an aligned candle open
  let r = cd(tf, open, open);
  eq(r.state, 'live', `${tf} at the open`);  eq(r.remaining, secs, `${tf} full length remaining at the open`);
  r = cd(tf, open, open + secs - 1);
  eq(r.state, 'live', `${tf} one second before the close`);  eq(r.remaining, 1, `${tf} 1 s left`);
  r = cd(tf, open, open + secs);
  eq(r.state, 'pending', `${tf} exactly at the close`);  eq(r.text, '00:00', `${tf} shows 00:00 at the close`);
  r = cd(tf, open, open + secs + C.GRACE_SECONDS);
  eq(r.state, 'pending', `${tf} still waiting for the next candle after the grace time`);
  r = cd(tf, open, open + secs + C.GRACE_SECONDS + 1);
  eq(r.state, 'closed', `${tf} no new candle shortly after the grace time -> market closed`);
  eq(r.text, '\u2014', `${tf} closed shows a dash, never a number`);
}

// formatting
eq(cd('M5', BASE, BASE + 134).text, '02:46', 'M5, 2:14 into the candle -> 02:46 left');
eq(cd('H1', BASE, BASE + 3600 - 59).text, '00:59', 'H1 last minute');
eq(cd('H4', BASE, BASE + 3600).text, '3:00:00', 'H4 shows hours');
eq(cd('D1', BASE, BASE + 1).text, '23:59:59', 'D1 shows hours');
eq(cd('H1', BASE, BASE + 1800.4).text, '30:00', 'fractional seconds round the remaining time UP (1799.6 s -> 30:00)');
eq(cd('M1', BASE, BASE + 59.6).text, '00:01', '0.4 s left still reads 00:01, never 00:00 while the candle is open');
eq(cd('M1', BASE, BASE + 59.6).state, 'live', '...and is live');

// the broker offset matters: the same wall-clock moment with another offset gives the same answer only if the candle is on that clock
for (const off of [2 * 3600, 3 * 3600, -5 * 3600, 19800 /* UTC+5:30 */, 0]) {
  const now = Date.UTC(2026, 9, 19, 12, 0, 0);                            // a fixed real instant
  const serverNow = now / 1000 + off;
  const open = serverNow - serverNow % 900;                              // the M15 candle that is open on THAT broker's clock
  const r = C.countdown({ tf: 'M15', candleTime: open, offsetSeconds: off, nowMs: now });
  eq(r.state, 'live', `offset ${off}: live`);
  eq(r.remaining, open + 900 - serverNow, `offset ${off}: remaining uses the broker clock`);
}
// a wrong offset is detected rather than shown: the candle appears to open in the future
eq(C.countdown({ tf: 'M5', candleTime: BASE + 7200, offsetSeconds: OFF, nowMs: nowAt(BASE) }).state, 'unavailable', 'candle far in the broker future -> unavailable');
eq(C.countdown({ tf: 'M5', candleTime: BASE + 3, offsetSeconds: OFF, nowMs: nowAt(BASE) }).state, 'live', 'a few seconds of clock difference is tolerated');

// midnight on the broker clock: the D1 candle closes at broker midnight and the next one opens
const dayOpen = BASE;                                                     // 00:00 broker time
eq(cd('D1', dayOpen, dayOpen + 86399).text, '00:01', 'D1 one second before broker midnight');
eq(cd('D1', dayOpen, dayOpen + 86400).state, 'pending', 'D1 at broker midnight, next candle not here yet');
eq(cd('D1', dayOpen + 86400, dayOpen + 86400).text, '24:00:00', 'D1 new candle: full day left');
eq(cd('D1', dayOpen + 86400, dayOpen + 86400).state, 'live', 'D1 new candle is live');

// ---------------------------------------------------------------- never a misleading number
const ok = { tf: 'M5', candleTime: BASE, offsetSeconds: OFF, nowMs: nowAt(BASE + 100) };
eq(C.countdown(ok).state, 'live', 'baseline input is live');
const without = (over) => C.countdown(Object.assign({}, ok, over));
eq(without({ candleTime: null }).state, 'unavailable', 'no candle timestamp');
eq(without({ candleTime: undefined }).state, 'unavailable', 'undefined candle timestamp');
eq(without({ candleTime: NaN }).state, 'unavailable', 'NaN candle timestamp');
eq(without({ candleTime: '1700000000' }).state, 'unavailable', 'a string is not a timestamp');
eq(without({ tf: 'W1' }).state, 'unavailable', 'unknown timeframe');
eq(without({ tf: undefined }).state, 'unavailable', 'no timeframe');
eq(without({ offsetSeconds: null }).state, 'unavailable', 'broker time unknown');
eq(without({ offsetSeconds: NaN }).state, 'unavailable', 'broker time NaN');
eq(without({ fresh: { connected: false } }).state, 'stale', 'MT5 disconnected');
eq(without({ fresh: { connected: true, candlesAgeMs: 60000 } }).state, 'stale', 'candles not refreshed for a minute');
eq(without({ fresh: { connected: true, candlesAgeMs: 4000, clockAgeMs: 30 * 60 * 1000 } }).state, 'stale', 'broker clock reading too old');
eq(without({ fresh: { connected: true, candlesAgeMs: 4000, clockAgeMs: 30000 } }).state, 'live', 'fresh data is live');
for (const state of ['unavailable', 'stale']) {
  const r = without(state === 'stale' ? { fresh: { connected: false } } : { offsetSeconds: null });
  eq(r.remaining, null, `${state} carries no number`);
  eq(r.text, '\u2014', `${state} shows a dash`);
  assert.ok(r.reason.length > 0, 'a reason is always given'); checks++;
}
eq(C.countdown().state, 'unavailable', 'no input at all');
eq(C.countdown({ tf: 'm5', candleTime: BASE, offsetSeconds: OFF, nowMs: nowAt(BASE + 100) }).state, 'live', 'timeframe is case-insensitive');

// a symbol/timeframe change: the new timeframe changes the answer for the same candle time at once (no cached value)
eq(cd('M5', BASE, BASE + 100).text, '03:20', 'M5 200 s left');
eq(cd('M15', BASE, BASE + 100).text, '13:20', 'M15 800 s left for the same open time');
eq(cd('H1', BASE, BASE + 100).text, '58:20', 'H1 3500 s left');

// ---------------------------------------------------------------- the DOM mounter (fake document)
function fakeEl() {
  const el = { textContent: '', title: '', attrs: {}, children: {}, setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
               writes: 0 };
  Object.defineProperty(el, 'text', { get() { return this.textContent; }, set(v) { this.textContent = v; this.writes++; } });
  return el;
}
function fakeBox() {
  const parts = { 'data-clock-time': fakeEl(), 'data-clock-zone': fakeEl(), 'data-clock-date': fakeEl() };
  const box = fakeEl();
  box.querySelector = (sel) => { const m = /\[(data-clock-[a-z]+)\]/.exec(sel); return m ? (parts[m[1]] || null) : null; };
  box.parts = parts;
  return box;
}
const boxes = { local: fakeBox(), eastern: fakeBox() };
const doc = { querySelector: (sel) => { const m = /\[data-clock="([a-z]+)"\]/.exec(sel); return m ? boxes[m[1]] || null : null; } };
let fakeNow = utc('2026-07-15T12:00:00.250Z');
const timers = [];
let ticks = 0;
const handle = C.mountClocks({ document: doc, now: () => fakeNow, localZone: 'Asia/Manila', setTimeoutFn: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
                               onTick: () => { ticks++; } });
eq(boxes.local.parts['data-clock-time'].textContent, '20:00:00', 'local clock painted immediately in the configured zone');
eq(boxes.local.parts['data-clock-zone'].textContent, 'GMT+8', 'local zone label');
eq(boxes.eastern.parts['data-clock-time'].textContent, '08:00:00', 'eastern clock painted immediately');
eq(boxes.eastern.parts['data-clock-zone'].textContent, 'EDT', 'eastern zone label in summer');
assert.ok(/Eastern Time/.test(boxes.eastern.title) && /America\/New_York/.test(boxes.eastern.title), 'eastern clock is labelled'); checks++;
assert.ok(/Local time \(Asia\/Manila\)/.test(boxes.local.title), 'local clock is labelled with its zone'); checks++;
eq(ticks, 1, 'onTick called once at mount');
eq(timers.length, 1, 'exactly one timer is pending (no duplicate update loops)');
assert.ok(timers[0].ms > 0 && timers[0].ms <= 1005, `next update is scheduled within a second (${timers[0].ms} ms)`); checks++;
eq(timers[0].ms, 1000 - 250 + 5, 'it wakes just after the next whole second');
// one second later: fires, repaints, schedules exactly one more
fakeNow += 1000;
timers.shift().fn();
eq(boxes.local.parts['data-clock-time'].textContent, '20:00:01', 'local clock advanced one second');
eq(boxes.eastern.parts['data-clock-time'].textContent, '08:00:01', 'eastern clock advanced one second');
eq(ticks, 2, 'onTick called again');
eq(timers.length, 1, 'still exactly one pending timer');
// crossing into winter: same code, no manual switch
fakeNow = utc('2026-11-01T05:59:59.500Z');
handle.tick();
eq(boxes.eastern.parts['data-clock-time'].textContent, '01:59:59', 'eastern before fall-back');
eq(boxes.eastern.parts['data-clock-zone'].textContent, 'EDT', 'EDT before fall-back');
fakeNow = utc('2026-11-01T06:00:00.500Z');
handle.tick();
eq(boxes.eastern.parts['data-clock-time'].textContent, '01:00:00', 'eastern after fall-back');
eq(boxes.eastern.parts['data-clock-zone'].textContent, 'EST', 'label switches to EST by itself');
// unchanged text is not rewritten (no needless DOM work each second)
const w = boxes.eastern.parts['data-clock-zone'].writes;
handle.tick();
eq(boxes.eastern.parts['data-clock-zone'].writes, w, 'an unchanged label is not rewritten');
// a failing onTick must never stop the clocks
const boom = C.mountClocks({ document: doc, now: () => fakeNow, localZone: '', setTimeoutFn: () => 0, onTick: () => { throw new Error('boom'); } });
eq(typeof boom.tick, 'function', 'mounting survives a throwing onTick');
// a page with no clock elements does not throw
C.mountClocks({ document: { querySelector: () => null }, now: () => fakeNow, setTimeoutFn: () => 0 });
checks++;
handle.stop();
const before = timers.length;
fakeNow += 1000;
handle.tick();
eq(timers.length, before, 'tick() after stop() schedules nothing');

console.log(`clocks.js: all ${checks} checks passed`);
