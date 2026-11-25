/* TradeMate clocks and candle countdown.
 *
 * Pure functions first (they are tested with Node, see tests/clock_test.js), then a small DOM mounter.
 *
 *   formatClock(ms, timeZone)           "14:03:22" + zone abbreviation + date for ANY IANA zone (DST handled by Intl)
 *   countdown(input)                    time left before the current candle closes, or WHY there is none
 *
 * Nothing here touches candle data, orders or trading decisions: it only reads values and writes text.
 *
 * Time references
 *   Clocks      the computer's clock (Date.now()) shown in two zones. Eastern Time is "America/New_York", so EST/EDT
 *               switches automatically; no hand-written offsets anywhere.
 *   Countdown   MT5 candle times are BROKER SERVER time stored as an epoch. The server's offset from UTC is measured by the
 *               TradeMate server from live candles (/api/market/time); remaining = candleOpen + timeframe - (now + offset).
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TradeMateClocks = api;
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var TF_SECONDS = { M1: 60, M5: 300, M15: 900, M30: 1800, H1: 3600, H4: 14400, D1: 86400 };
  var EASTERN = 'America/New_York';
  var GRACE_SECONDS = 15;        // after the close, wait this long for the next candle to arrive before saying "no new candle"
  var FUTURE_TOLERANCE = 5;      // a candle that opens more than this far in the broker's future means the clocks disagree
  var CLOCK_MAX_AGE_MS = 15 * 60 * 1000;   // a broker-offset reading older than this is no longer trusted
  var CANDLES_MAX_AGE_MS = 20 * 1000;      // candles not refreshed for this long are stale

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  /* True when Intl knows the zone. An unknown name must never break the page. */
  function validZone(zone) {
    if (typeof zone !== 'string' || !zone) return false;
    try { new Intl.DateTimeFormat('en-US', { timeZone: zone }); return true; } catch (e) { return false; }
  }

  var fmtCache = {};
  function formatter(zone, opts, key) {
    var k = (zone || '') + '|' + key;
    if (!fmtCache[k]) {
      var o = { hourCycle: 'h23' };
      if (zone) o.timeZone = zone;
      for (var p in opts) o[p] = opts[p];
      fmtCache[k] = new Intl.DateTimeFormat('en-US', o);
    }
    return fmtCache[k];
  }
  function partsOf(fmt, ms) {
    var out = {};
    fmt.formatToParts(new Date(ms)).forEach(function (p) { out[p.type] = p.value; });
    return out;
  }

  /* One clock reading: { time:'HH:MM:SS', zone:'EDT', date:'Mon 19 Oct', zoneName:'America/New_York', ok:true }.
   * `zone` falsy = the browser's own timezone. An unknown zone falls back to the browser's and says so (ok:false, fallback:true). */
  function formatClock(ms, zone) {
    var fallback = false;
    if (zone && !validZone(zone)) { zone = ''; fallback = true; }
    var t = partsOf(formatter(zone, { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short' }, 'time'), ms);
    var d = partsOf(formatter(zone, { weekday: 'short', day: '2-digit', month: 'short' }, 'date'), ms);
    var name = zone || (new Intl.DateTimeFormat().resolvedOptions().timeZone) || 'local';
    return { time: t.hour + ':' + t.minute + ':' + t.second, zone: t.timeZoneName || '', date: d.weekday + ' ' + d.day + ' ' + d.month,
             zoneName: name, fallback: fallback };
  }

  function fmtRemaining(sec) {
    sec = Math.max(0, Math.floor(sec));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h > 0 ? h + ':' + pad(m) + ':' + pad(s) : pad(m) + ':' + pad(s);
  }

  /* input: {
   *   tf:            'M1'..'D1'
   *   candleTime:    open time of the newest candle (broker epoch seconds, exactly as MT5 reports it) or null
   *   offsetSeconds: broker clock minus UTC, or null when unknown
   *   nowMs:         Date.now()
   *   fresh:         { connected: bool, candlesAgeMs: number|null, clockAgeMs: number|null }   (all optional)
   * }
   * returns { state, text, remaining, reason }.   state is one of:
   *   live         remaining > 0: the candle is still forming
   *   pending      the candle just closed (within GRACE_SECONDS): waiting for the next one
   *   closed       it closed a while ago and no new candle opened: market closed / no ticks
   *   stale        data is not being refreshed (disconnected / candles or broker clock too old): never shown as live
   *   unavailable  no usable timestamp / timeframe / broker clock
   * `text` is what to display; it is a number ONLY in the live and pending states. */
  function countdown(input) {
    input = input || {};
    var secs = TF_SECONDS[String(input.tf || '').toUpperCase()];
    var fresh = input.fresh || {};
    if (!secs) return res('unavailable', 'Unknown timeframe');
    if (typeof input.candleTime !== 'number' || !isFinite(input.candleTime)) return res('unavailable', 'No candle yet');
    if (fresh.connected === false) return res('stale', 'MT5 not connected');
    if (typeof input.offsetSeconds !== 'number' || !isFinite(input.offsetSeconds)) return res('unavailable', 'Broker time unknown');
    if (typeof fresh.clockAgeMs === 'number' && fresh.clockAgeMs > CLOCK_MAX_AGE_MS) return res('stale', 'Broker time not refreshed');
    if (typeof fresh.candlesAgeMs === 'number' && fresh.candlesAgeMs > CANDLES_MAX_AGE_MS) return res('stale', 'Candles not refreshing');
    var serverNow = input.nowMs / 1000 + input.offsetSeconds;
    if (serverNow < input.candleTime - FUTURE_TOLERANCE) return res('unavailable', 'Clock mismatch');
    var remaining = input.candleTime + secs - serverNow;
    if (remaining > 0) {
      var r = Math.ceil(remaining);                         // 00:01 is shown until the last second has gone
      return { state: 'live', text: fmtRemaining(r), remaining: r, reason: '' };
    }
    if (-remaining <= GRACE_SECONDS) return { state: 'pending', text: '00:00', remaining: 0, reason: 'Waiting for the next candle' };
    return res('closed', 'No new candle (market closed?)');
    function res(state, reason) { return { state: state, text: '\u2014', remaining: null, reason: reason }; }
  }

  /* ---------- DOM mounter ---------- */

  /* Fills every [data-clock] element once a second. Only textContent is written: no reload, no layout of the chart.
   * opts: { document, now(): ms, localZone: string|'', onTick(ms), setTimeoutFn } (all injectable for tests). Returns { tick, stop }. */
  function mountClocks(opts) {
    var doc = opts.document, now = opts.now || Date.now, st = opts.setTimeoutFn || setTimeout;
    var local = opts.localZone || '';
    var stopped = false, timer = null;
    function setText(el, text) { if (el && el.textContent !== text) el.textContent = text; }
    function paint() {
      var ms = now();
      [['local', local], ['eastern', EASTERN]].forEach(function (pair) {
        var box = doc.querySelector('[data-clock="' + pair[0] + '"]');
        if (!box) return;
        var c = formatClock(ms, pair[1]);
        setText(box.querySelector('[data-clock-time]'), c.time);
        setText(box.querySelector('[data-clock-zone]'), c.zone);
        setText(box.querySelector('[data-clock-date]'), c.date);
        var label = pair[0] === 'local'
          ? 'Local time (' + c.zoneName + (c.fallback ? '; the configured timezone was not recognised' : '') + ')'
          : 'Eastern Time (' + c.zoneName + ', daylight saving handled automatically)';
        if (box.title !== label) box.title = label;
        box.setAttribute('aria-label', label + ': ' + c.date + ' ' + c.time + ' ' + c.zone);
      });
    }
    function tick() {
      paint();
      if (typeof opts.onTick === 'function') { try { opts.onTick(now()); } catch (e) { /* a display bug must never stop the clocks */ } }
    }
    function schedule() {
      if (stopped) return;
      var ms = now();
      timer = st(function () { tick(); schedule(); }, 1000 - (ms % 1000) + 5);   // wake just after each whole second
    }
    tick();
    schedule();
    return { tick: tick, stop: function () { stopped = true; } };
  }

  return { TF_SECONDS: TF_SECONDS, EASTERN: EASTERN, GRACE_SECONDS: GRACE_SECONDS, validZone: validZone, formatClock: formatClock,
           fmtRemaining: fmtRemaining, countdown: countdown, mountClocks: mountClocks };
}));
