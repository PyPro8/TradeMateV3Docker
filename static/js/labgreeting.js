/* TradeMate Command Centre greeting. Pure functions first (tested with Node, see tests/labgreeting_test.js), then a small DOM mounter.
 *
 *   partOfDay(hour)            "morning" 05:00-11:59, "afternoon" 12:00-17:59, "evening" 18:00-04:59
 *   greeting(date)             "Good morning" / "Good afternoon" / "Good evening", or "Welcome" when the date is invalid
 *   welcome({now, lastVisit})  { greeting, returning, text }   returning only when lastVisit is a real earlier time
 *   readLastVisit / recordVisit  tolerant localStorage helpers (missing, blocked or corrupt storage never throws)
 *
 * The hour is the computer's LOCAL hour (the same clock as the LOCAL clock in the top bar). Nothing here touches market data, analysis,
 * orders or trading decisions: it only reads the clock and writes text. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TradeMateLabGreeting = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  var KEY = 'trademate.lab.lastVisit';
  var NAME_KEY = 'trademate.userDisplayName';

  function validDate(d) { return d instanceof Date && !isNaN(d.getTime()); }

  function sanitizeDisplayName(value) {
    if (value === null || value === undefined) return '';
    var cleaned = String(value).replace(/\s+/g, ' ').trim();
    if (!cleaned) return '';
    cleaned = cleaned.replace(/[^\w\s'’.-]/g, '');
    return cleaned.replace(/\s+/g, ' ').trim();
  }

  function readDisplayName(storage) {
    try {
      if (!storage) return '';
      var raw = storage.getItem(NAME_KEY);
      if (raw === null || raw === undefined) return '';
      var value = JSON.parse(raw);
      if (typeof value !== 'string') return '';
      return sanitizeDisplayName(value);
    } catch (e) { return ''; }
  }

  function saveDisplayName(storage, value) {
    var name = sanitizeDisplayName(value);
    try {
      if (!storage) return name || null;
      if (!name) { storage.removeItem(NAME_KEY); return null; }
      storage.setItem(NAME_KEY, JSON.stringify(name));
      return name;
    } catch (e) { return name || null; }
  }

  function partOfDay(hour) {
    if (typeof hour !== 'number' || !isFinite(hour) || hour < 0 || hour >= 24) return null;
    var h = Math.floor(hour);
    if (h >= 5 && h < 12) return 'morning';
    if (h >= 12 && h < 18) return 'afternoon';
    return 'evening';
  }

  function greeting(date) {
    var part = validDate(date) ? partOfDay(date.getHours()) : null;
    return part ? 'Good ' + part : 'Welcome';
  }

  function withName(greetText, name) {
    var cleaned = sanitizeDisplayName(name);
    return cleaned ? greetText + ', ' + cleaned : greetText;
  }

  function welcome(opts) {
    opts = opts || {};
    var now = validDate(opts.now) ? opts.now : null;
    var last = opts.lastVisit;
    var name = sanitizeDisplayName(opts.displayName);
    // "Back" only when we know a PREVIOUS visit that is really in the past (a stored time in the future is clock skew or junk).
    var returning = !!now && typeof last === 'number' && isFinite(last) && last > 0 && last < now.getTime();
    var g = greeting(now);
    var intro = withName(g, name);
    return { greeting: g, returning: returning, displayName: name, text: intro + (returning ? '. Welcome back to Command Centre.' : '. Welcome to Command Centre.') };
  }

  function readLastVisit(storage) {
    try {
      if (!storage) return null;
      var raw = storage.getItem(KEY);
      var n = raw === null || raw === undefined ? NaN : Number(JSON.parse(raw));
      return isFinite(n) && n > 0 ? n : null;
    } catch (e) { return null; }
  }

  function recordVisit(storage, ms) {
    try { if (storage && typeof ms === 'number' && isFinite(ms)) { storage.setItem(KEY, JSON.stringify(ms)); return true; } } catch (e) { /* blocked storage */ }
    return false;
  }

  /* DOM mounter. The previous visit is read ONCE (so "welcome back" stays stable for the whole session) and this visit is recorded
   * straight away. The greeting is re-evaluated every minute and whenever the page is shown, so it follows the clock. */
  function mount(el, deps) {
    deps = deps || {};
    var storage = 'storage' in deps ? deps.storage : (typeof localStorage !== 'undefined' ? localStorage : null);
    var nowFn = deps.now || function () { return new Date(); };
    var previous = readLastVisit(storage);
    recordVisit(storage, nowFn().getTime());
    function render() {
      var currentName = readDisplayName(storage);
      var w = welcome({ now: nowFn(), lastVisit: previous, displayName: currentName });
      el.textContent = w.text;
      el.setAttribute('data-part', w.greeting);
      el.setAttribute('data-returning', String(w.returning));
      el.setAttribute('data-display-name', currentName || '');
      return w;
    }
    render();
    return { render: render, previous: function () { return previous; } };
  }

  return {
    KEY: KEY,
    NAME_KEY: NAME_KEY,
    partOfDay: partOfDay,
    greeting: greeting,
    welcome: welcome,
    readLastVisit: readLastVisit,
    recordVisit: recordVisit,
    readDisplayName: readDisplayName,
    saveDisplayName: saveDisplayName,
    sanitizeDisplayName: sanitizeDisplayName,
    mount: mount
  };
}));
