"""Chart navigation capsule, Rapid Fire exit lines on the chart, and the Trading Journal page, in a real browser (headless Chromium).
Skipped (not passed) when Playwright is missing. The page is served by the real Flask app; the Rapid Fire levels and the position are fixed payloads.

    python3 -m unittest tests.test_browser_rapidfire_nav_journal -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.browser_support import PLAYWRIGHT, BrowserBase
from trademate.trade_journal import TradeJournal

_T = {}


def _patches(cls):
    _T["dir"] = tempfile.TemporaryDirectory()
    _T["journal"] = TradeJournal(Path(_T["dir"].name) / "j.json")
    return [mock.patch.object(app_module, "trade_journal", _T["journal"])]


class _Base(BrowserBase):
    extra_patches = (_patches,)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        _T["dir"].cleanup()


@unittest.skipUnless(PLAYWRIGHT, "SKIPPED: Playwright is not installed.")
class NavigationCapsuleTest(_Base):
    JS = """() => {
      const cap = document.querySelector('#view-chart .tb-capsule'), cs = getComputedStyle(cap), r = cap.getBoundingClientRect();
      const items = Array.from(cap.querySelectorAll('button, .chart-symbol, select, input')).filter(e => e.offsetParent !== null);
      const mids = items.map(e => { const b = e.getBoundingClientRect(); return b.top + b.height / 2; });
      const spread = Math.max(...mids) - Math.min(...mids);
      const pop = document.querySelector('#view-chart [data-role=ma-pop]');
      return {wrap: cs.flexWrap, ox: cs.overflowX, oy: cs.overflowY, radius: parseFloat(cs.borderTopLeftRadius), dir: cs.flexDirection,
              rows: spread < 8 ? 1 : 2, spread: spread, n: items.length, sw: cap.scrollWidth, cw: cap.clientWidth, capH: r.height, docW: document.documentElement.scrollWidth, winW: window.innerWidth,
              popInside: cap.contains(pop), parentOk: !!cap.closest('.chart-toolbar')};
    }"""

    def check(self, width):
        self.page.set_viewport_size({"width": width, "height": 800})
        self.open_chart()
        self.page.wait_for_timeout(400)
        m = self.page.evaluate(self.JS)
        self.assertEqual((m["wrap"], m["dir"]), ("nowrap", "row"), m)
        self.assertIn(m["ox"], ("auto", "scroll"), m)
        self.assertGreaterEqual(m["radius"], 20, m)                                # a rounded capsule
        self.assertEqual(m["rows"], 1, f"the controls split into {m['rows']} rows at {width}px: {m}")
        self.assertGreater(m["n"], 8, m)
        self.assertLessEqual(m["docW"], m["winW"], f"page-level horizontal overflow at {width}px")
        self.assertFalse(m["popInside"], "the pop-over must not be clipped by the capsule")
        return m

    def test_one_row_at_desktop_tablet_and_phone_widths(self):
        for w in (1280, 1024, 768, 414, 360, 320):
            self.check(w)

    def test_a_narrow_capsule_scrolls_inside_itself_and_every_item_is_reachable(self):
        m = self.check(360)
        self.assertGreater(m["sw"], m["cw"], "at 360px the capsule must scroll sideways")
        self.page.evaluate("() => { const c = document.querySelector('#view-chart .tb-capsule'); c.style.scrollBehavior = 'auto'; c.scrollLeft = c.scrollWidth; }")
        last = self.page.evaluate("""() => { const c = document.querySelector('#view-chart .tb-capsule'), cr = c.getBoundingClientRect();
          const it = Array.from(c.querySelectorAll('button')).filter(e => e.offsetParent !== null).pop(), r = it.getBoundingClientRect();
          return r.right <= cr.right + 2 && r.left >= cr.left - 2; }""")
        self.assertTrue(last, "the last control can be scrolled fully into view inside the capsule")
        self.assertEqual(self.page.evaluate("() => document.documentElement.scrollLeft"), 0)

    def test_navigation_still_works_inside_the_capsule(self):
        self.open_chart()
        before = self.page.evaluate("() => window.TradeMateApp.selection().tf")
        target = "H1" if before != "H1" else "M15"
        self.page.locator(f'#view-chart .tb-capsule [data-tf="{target}"], #view-chart .tb-capsule button:text-is("{target}")').first.click()
        self.until(f"window.TradeMateApp.selection().tf === '{target}'")

    def test_the_moving_average_popover_opens_without_being_clipped(self):
        self.open_chart()
        self.page.locator('#view-chart [data-role="ma-btn"]').first.click()
        self.until("!document.querySelector('#view-chart [data-role=ma-pop]').hidden")
        box = self.page.locator('#view-chart [data-role="ma-pop"]').bounding_box()
        self.assertGreater(box["height"], 40)


@unittest.skipUnless(PLAYWRIGHT, "SKIPPED: Playwright is not installed.")
class RapidFireChartLinesTest(_Base):
    LEVELS = {"ok": True, "stale": False, "levels": []}

    def serve(self, levels, with_position=True):
        payload = {"ok": True, "stale": False, "levels": levels}

        def broker(route, request=None):
            resp = route.fetch()
            data = resp.json()
            if with_position:
                sym = next(s for s in data["symbols"] if s["name"] == "EURUSD")
                data["positions"] = [{"ticket": 7001, "symbol": "EURUSD", "direction": "buy", "volume": 0.02, "price_open": sym["bid"], "price_current": sym["bid"],
                                      "stop_loss": None, "take_profit": None, "profit": 0.0, "swap": 0.0, "time": 1_800_000_000, "magic": 0, "comment": ""}]
            route.fulfill(response=resp, body=json.dumps(data), content_type="application/json")
        self.page.route("**/api/broker*", broker)
        self.page.route("**/api/rapid-fire/chart-levels*", lambda route, request=None: route.fulfill(status=200, content_type="application/json", body=json.dumps(payload)))
        self.page.goto(self.base + "/#chart")
        self.until("window.TradeMateApp && window.TradeMateApp.chartQuotes().length")
        self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
        self.until("window.TradeMateApp.selection().symbol === 'EURUSD'")

    def entry(self):
        return self.page.evaluate("() => { const s = {}); return s.bid || null; }")

    def frame(self):
        return self.page.evaluate("() => window.TradeMateApp.rfFrame()[0]")

    def test_entry_take_profit_and_stop_lines_are_drawn_with_price_and_distance(self):
        self.page.set_viewport_size({"width": 1280, "height": 900})
        # levels close to the live price so they sit inside the visible range
        self.page.goto(self.base + "/#chart")
        self.until("window.TradeMateApp && window.TradeMateApp.chartQuotes().length")
        bid = self.page.evaluate("() => fetch('/api/broker').then(r => r.json()).then(d => d.symbols.find(s => s.name === 'EURUSD').bid)")
        self.assertTrue(bid)
        self.page.unroute_all() if hasattr(self.page, "unroute_all") else None
        lv = [{"ticket": 7001, "symbol": "EURUSD", "side": "buy", "volume": 0.02, "role": "original", "entry": bid,
               "tp": {"price": round(bid + 0.30, 2), "points": 30.0, "money": 0.6}, "sl": {"price": round(bid - 0.50, 2), "points": 50.0, "money": 1.0},
               "managed_by": "software", "currency": "USD"}]
        self.serve(lv)
        self.until("(window.TradeMateApp.rfFrame()[0] || []).length === 2", timeout=20)
        f = self.frame()
        kinds = {x["kind"]: x for x in f}
        self.assertEqual(set(kinds), {"tp", "sl"})
        self.assertAlmostEqual(kinds["tp"]["price"], round(bid + 0.30, 2))
        self.assertAlmostEqual(kinds["sl"]["price"], round(bid - 0.50, 2))
        self.assertLess(kinds["tp"]["y"], kinds["sl"]["y"])                      # the take-profit is above the stop for a buy
        self.assertEqual((kinds["tp"]["points"], kinds["sl"]["points"]), (30.0, 50.0))

    def test_no_lines_when_the_position_is_not_on_the_account(self):
        self.page.set_viewport_size({"width": 1280, "height": 900})
        self.serve([{"ticket": 7001, "symbol": "EURUSD", "side": "buy", "volume": 0.02, "role": "original", "entry": 1.0,
                     "tp": {"price": 1.3, "points": 30.0, "money": 0.6}, "sl": None, "managed_by": "software", "currency": "USD"}], with_position=False)
        self.page.wait_for_timeout(2500)
        self.assertEqual(self.frame(), [])                                        # a level with no MT5 position behind it is never drawn

    def test_lines_disappear_when_the_levels_go_away(self):
        self.page.set_viewport_size({"width": 1280, "height": 900})
        self.page.goto(self.base + "/#chart")
        self.until("window.TradeMateApp && window.TradeMateApp.chartQuotes().length")
        bid = self.page.evaluate("() => fetch('/api/broker').then(r => r.json()).then(d => d.symbols.find(s => s.name === 'EURUSD').bid)")
        state = {"levels": [{"ticket": 7001, "symbol": "EURUSD", "side": "buy", "volume": 0.02, "role": "original", "entry": bid,
                             "tp": {"price": round(bid + 0.30, 2), "points": 30.0, "money": 0.6}, "sl": None, "managed_by": "software", "currency": "USD"}]}

        def broker(route, request=None):
            resp = route.fetch()
            data = resp.json()
            if state["levels"]:
                sym = next(s for s in data["symbols"] if s["name"] == "EURUSD")
                data["positions"] = [{"ticket": 7001, "symbol": "EURUSD", "direction": "buy", "volume": 0.02, "price_open": sym["bid"], "price_current": sym["bid"], "stop_loss": None,
                                      "take_profit": None, "profit": 0.0, "swap": 0.0, "time": 1_800_000_000, "magic": 0, "comment": ""}]
            route.fulfill(response=resp, body=json.dumps(data), content_type="application/json")
        self.page.route("**/api/broker*", broker)
        self.page.route("**/api/rapid-fire/chart-levels*", lambda route, request=None: route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "stale": False, "levels": state["levels"]})))
        self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
        self.until("(window.TradeMateApp.rfFrame()[0] || []).length === 1", timeout=20)
        state["levels"] = []                                                      # the position closed
        self.until("(window.TradeMateApp.rfFrame()[0] || []).length === 0", timeout=20)


@unittest.skipUnless(PLAYWRIGHT, "SKIPPED: Playwright is not installed.")
class JournalPageTest(_Base):
    def setUp(self):
        super().setUp()
        for e in _T["journal"].list():
            _T["journal"].delete(e["id"])

    def open_journal(self, width=1280):
        self.page.set_viewport_size({"width": width, "height": 900})
        self.page.goto(self.base + "/#journal")
        self.until("document.querySelector('#jr-emotion').options.length > 2")

    def test_the_journal_keeps_every_documentation_field_and_entries_are_editable(self):
        self.open_journal()
        p = self.page
        p.fill("#jr-date", "2026-11-20"); p.fill("#jr-time", "09:30"); p.fill("#jr-instrument", "XAUUSD"); p.fill("#jr-strategy", "Rapid Fire")
        p.select_option("#jr-result", "win"); p.fill("#jr-pl", "12.5"); p.select_option("#jr-emotion", "anxious"); p.fill("#jr-notes", "Hesitated before the entry.")
        p.click("#jr-save")
        self.until("document.querySelectorAll('#jr-body tr[data-id]').length === 1")
        row = p.inner_text("#jr-body tr[data-id]")
        for want in ("2026-11-20", "09:30", "XAUUSD", "Rapid Fire", "Win", "12.50", "Anxious", "Hesitated before the entry."):
            self.assertIn(want, row)
        p.click("#jr-body tr[data-id] button:text-is('Edit')")
        self.assertEqual(p.input_value("#jr-notes"), "Hesitated before the entry.")           # the notepad fields come back for editing
        self.assertEqual(p.input_value("#jr-emotion"), "anxious")
        p.select_option("#jr-emotion", "calm"); p.fill("#jr-notes", "Calm after the second look.")
        p.click("#jr-save")
        self.until("document.querySelector('#jr-body tr[data-id]').textContent.indexOf('Calm after the second look.') >= 0")
        saved = _T["journal"].list()[0]
        self.assertEqual((saved["emotion"], saved["notes"], saved["instrument"]), ("calm", "Calm after the second look.", "XAUUSD"))

    def test_a_refused_save_keeps_what_was_typed(self):
        self.open_journal()
        self.page.fill("#jr-time", "09:30")                                                     # nothing else: refused
        self.page.click("#jr-save")
        self.until("document.querySelector('#jr-msg').textContent.length > 0")
        self.assertEqual(self.page.input_value("#jr-time"), "09:30")
        self.assertEqual(_T["journal"].list(), [])

    def test_the_page_fits_a_phone(self):
        self.open_journal(width=375)
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 376)
