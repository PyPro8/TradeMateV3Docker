/* TradeMate router: the ONE owner of page navigation.

   Routes are the `.view` sections of the page (id "view-<name>"), addressed by the URL hash (#chart, #history ...). This module:
     - normalises the hash (case, trailing slash, "?query", "/sub/path") and canonicalises the address bar with history.replaceState,
       an unknown route becomes #dashboard (canonicalised too, never left dangling);
     - shows exactly one view, marks every nav link for that view with aria-current="page", sets the document title, resets scroll and moves focus;
     - tells every subscriber ONCE per route change ({name, previous}), in registration order (the script order of the page);
     - tells "activation" subscribers on EVERY nav activation, including a tap on the link of the page you are already on
       (no hashchange fires for that, which is how the mobile drawer used to stay open over the chart);
     - back / forward / refresh all arrive here through the single hashchange listener below. There is no other hashchange listener in the application.

   API: TradeMateRouter.on(fn)  .onActivate(fn)  .current()  .go(name)  .routes()  .listenerCount() (test hook) */
(function () {
  'use strict';
  var DEFAULT = 'dashboard';
  var ALIASES = { 'command-centre': 'strategylab', commandcentre: 'strategylab', 'account-centre': 'accountcenter', 'account-center': 'accountcenter', 'trade-history': 'history' };
  var subs = [], activations = [];
  var cur = null;

  function routes() {
    return Array.prototype.map.call(document.querySelectorAll('.view[id^="view-"]'), function (v) { return v.id.slice(5); });
  }
  function resolve(hash) {
    var raw = String(hash || '').replace(/^#\/?/, '').split(/[?\/&]/)[0].trim().toLowerCase();
    if (ALIASES[raw]) raw = ALIASES[raw];
    return routes().indexOf(raw) >= 0 ? raw : null;
  }
  function canonical(name) { return '#' + name; }

  function paint(name) {
    document.querySelectorAll('.view').forEach(function (v) { v.hidden = v.id !== 'view-' + name; });
    document.querySelectorAll('[data-view]').forEach(function (link) {
      if (link.getAttribute('data-view') === name) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    var view = document.getElementById('view-' + name);
    document.title = (view.getAttribute('data-title') || name) + ' · TradeMate';
    document.documentElement.setAttribute('data-route', name);
  }

  function sync(fireFocus) {
    var wanted = resolve(location.hash) || DEFAULT;
    if (location.hash !== canonical(wanted)) {
      try { history.replaceState(history.state, '', location.pathname + location.search + canonical(wanted)); } catch (e) { /* file:// and friends: the view still switches */ }
    }
    var previous = cur;
    if (wanted === cur) return false;
    cur = wanted;
    paint(wanted);
    var main = document.getElementById('main');
    if (main) { main.scrollTop = 0; if (fireFocus) main.focus({ preventScroll: true }); }
    subs.slice().forEach(function (fn) {
      try { fn({ name: wanted, previous: previous }); } catch (e) { if (window.console) console.error('router subscriber failed', e); }
    });
    return true;
  }

  window.addEventListener('hashchange', function () { sync(true); });

  // Every activation of a navigation link, including the link of the page already shown (no hashchange then).
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href^="#"]') : null;
    if (!a) return;
    var name = resolve(a.getAttribute('href'));
    if (!name) return;
    var same = name === cur;
    activations.slice().forEach(function (fn) { try { fn({ name: name, same: same }); } catch (err) { if (window.console) console.error(err); } });
    if (same) {                                              // nothing will change the hash: still reset the scroll, like a navigation does
      var main = document.getElementById('main');
      if (main) main.scrollTop = 0;
      e.preventDefault();
    }
  });

  window.TradeMateRouter = {
    on: function (fn) { if (typeof fn === 'function' && subs.indexOf(fn) < 0) subs.push(fn); },
    onActivate: function (fn) { if (typeof fn === 'function' && activations.indexOf(fn) < 0) activations.push(fn); },
    current: function () { return cur || resolve(location.hash) || DEFAULT; },
    go: function (name) {
      var n = resolve(name) || DEFAULT;
      if (location.hash !== canonical(n)) location.hash = canonical(n);
      sync(true);                                          // applied now, not on the later async hashchange (which then finds nothing left to do)
    },
    routes: routes,
    listenerCount: function () { return { route: subs.length, activate: activations.length }; }
  };

  sync(false);                                               // the initial route: shown now, canonicalised, subscribers register afterwards and read current()
})();
