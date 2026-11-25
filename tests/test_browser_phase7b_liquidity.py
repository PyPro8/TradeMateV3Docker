"""Phase 7B: liquidity overlay, settings controls and the Analysis page's Liquidity panel, in real Chromium with deterministic candles.

    python3 -m unittest tests.test_browser_phase7b_liquidity -v        (SKIPPED, not passed, when Playwright/Chromium is missing)
"""

import json
import unittest

from tests.browser_support import BrowserBase, QuoteStub
from tests.test_browser_phase7a import AUDIT_JS
from tests.test_structure_regressions import mk

# (high, low, close) for the chart's default swing width 3. Equal highs 10 (candles 3 and 10), equal lows 6 (6 and 13).
# Candle 17 pokes through 10 with its wick (high 10.4) and closes back inside (9.5); candle 18 closes the picture.
ROWS = [(8, 6.5, 7), (8.5, 6.8, 7.5), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8.5, 6.8, 7.5), (8, 6, 7), (8.5, 6.8, 7.5), (9, 7, 8), (9.5, 7.5, 8.5),
        (10, 8, 9), (9, 7, 8), (8.5, 6.8, 7.5), (8, 6, 7), (8.5, 6.8, 7.5), (9, 7, 8), (9.5, 7.5, 8.5), (10.4, 8, 9.5), (9, 7.5, 8.5)]


class LiquidityStub(QuoteStub):
    def __init__(self):
        super().__init__()
        self.series_by["EURUSD"]["H1"] = mk(ROWS)
        self.series_by["EURUSD"]["M15"] = mk(ROWS)
        self.quotes["EURUSD"] = (9.0, 9.4)
        self.no_spec = False

    def get_symbol_spec(self, symbol):
        if self.no_spec:
            raise __import__("app").BrokerError("no spec")
        return super().get_symbol_spec(symbol)


def evil(text="x"):
    return f'{text}<img src=x onerror="window.__xss=1">'


class LiquidityBrowserTest(BrowserBase):
    broker_factory = LiquidityStub

    def setUp(self):
        super().setUp()
        self.broker.no_spec = False
        self.broker.connected = True
        # every test starts from the default (liquidity hidden, wick mode, 5 ticks); the settings store is shared by the class
        self.set_settings(liquidity_visible=False, liquidity_sweep_mode="wick", liquidity_tolerance_ticks=5, liquidity_max_pools=8)

    def set_settings(self, **kw):
        """Save settings on the server, then reload so the page starts from them (it reads them once at load)."""
        self.page.goto(self.base + "/")
        self.page.evaluate("""async (b) => { await fetch('/api/chart/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)}); }""", kw)
        self.page.reload()
        self.until("window.TradeMateApp && document.getElementById('cs-liquidity') && !document.getElementById('cs-liquidity').disabled")

    def drawn(self):
        return self.page.evaluate("() => window.TradeMateApp.liquidityDrawn()")

    def open_analysis(self):
        self.page.goto(self.base + "/#analysis")
        self.page.evaluate("() => { var t = document.getElementById('an-tech'); if (t) t.open = true; }")      # Task 2: detail lives in the collapsed 'Technical details'
        self.until("document.getElementById('an-status-code').textContent !== 'ANALYSING'")
        self.until("document.querySelectorAll('#an-liquidity .an-sec').length > 0 || /not analysed/i.test(document.getElementById('an-liquidity').innerText)")

    # ================= Analysis page =================
    def test_the_analysis_page_lists_pools_and_the_sweep_with_the_real_numbers(self):
        self.open_analysis()
        text = self.page.evaluate("() => document.getElementById('an-liquidity').innerText")
        for needed in ("does not reveal hidden orders", "Active buy-side liquidity (BSL) (0)", "Active sell-side liquidity (SSL) (1)",
                       "Confirmed sweeps (1)", "Equal highs / equal lows pools (1)", "Equal highs/lows tolerance", "5 ticks",
                       "price to trade through the level (wick)"):
            self.assertIn(needed, text)
        self.page.click("#an-liquidity details:has-text('sweep') summary >> nth=0")
        body = self.page.evaluate("() => document.getElementById('an-liquidity').innerText")
        for needed in ("Pool swept", "Crossing candle", "Detection mode: wick", "closed back inside", "not a reversal signal", "No structural break"):
            self.assertIn(needed, body)
        self.assertFalse(self.page.evaluate("() => /\\bbuy now\\b|\\bsell now\\b/i.test(document.getElementById('view-analysis').innerText)"))

    def test_interpretation_keeps_facts_rules_and_watch_apart(self):
        self.open_analysis()
        t = self.page.evaluate("() => document.getElementById('an-interp').innerText").lower()
        for needed in ("observed facts", "rule-based interpretation", "conditions to watch", "liquidity pool(s) were inferred",
                       "does not mean a reversal", "if a candle trades through the unswept sell-side level"):
            self.assertIn(needed, t)

    def test_liquidity_failure_is_reported_honestly_and_structure_still_works(self):
        self.broker.no_spec = True
        self.open_analysis()
        self.assertIn("not analysed", self.page.evaluate("() => document.getElementById('an-liquidity').innerText").lower())
        self.assertIn("tick size", self.page.evaluate("() => document.getElementById('an-liquidity').innerText"))
        self.assertIn(self.page.evaluate("() => document.getElementById('an-status-code').textContent"), ("READY", "WATCH"))

    def test_server_text_in_the_liquidity_panel_is_never_run_as_html(self):
        payload = self.page.evaluate("""async () => (await fetch('/api/analysis?symbol=EURUSD&timeframe=H1')).json()""")
        payload["liquidity"]["note"] = evil("note")
        payload["liquidity"]["ssl"][0]["explanation"]["lines"] = [evil("line")]
        payload["liquidity"]["ssl"][0]["kind_label"] = evil("label")
        payload["liquidity"]["sweeps"][0]["explanation"]["lines"] = [evil("sweep")]
        self.page.route("**/api/analysis**", lambda route, request: route.fulfill(status=200, content_type="application/json", body=json.dumps(payload)))
        self.page.goto(self.base + "/#analysis")
        self.page.evaluate("() => { var t = document.getElementById('an-tech'); if (t) t.open = true; }")      # Task 2: detail lives in the collapsed 'Technical details'
        self.until("document.getElementById('an-liquidity').innerText.indexOf('note<img') >= 0")
        self.assertIsNone(self.page.evaluate("() => window.__xss"))
        self.assertEqual(self.page.evaluate("() => document.querySelectorAll('#an-liquidity img, #an-liquidity script').length"), 0)

    def test_page_contrast_in_both_themes_and_no_horizontal_overflow_on_a_phone(self):
        self.open_analysis()
        for theme in ("dark", "light"):
            self.page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)", theme)
            bad = [x for x in self.page.evaluate(AUDIT_JS) if not x["disabled"] and "btn-primary" not in x["cls"]]
            self.assertEqual(bad, [], theme)
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.page.wait_for_timeout(200)
        self.assertLessEqual(self.page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth"), 1)

    # ================= chart navigation and overlay =================
    def test_show_on_chart_opens_the_chart_with_the_overlay_only_when_asked(self):
        self.open_analysis()
        self.page.click("#an-liquidity .an-event summary >> nth=0")
        self.page.click("#an-liquidity button:has-text('Show on chart') >> nth=0")
        self.until("location.hash === '#chart'")
        self.assertFalse(self.page.evaluate("() => window.TradeMateApp.liquidityVisible()"))      # the first button respects the setting
        self.page.goto(self.base + "/#analysis")
        self.page.evaluate("() => { var t = document.getElementById('an-tech'); if (t) t.open = true; }")      # Task 2: detail lives in the collapsed 'Technical details'
        self.until("document.querySelectorAll('#an-liquidity button').length > 0")
        self.page.click("#an-liquidity .an-event summary >> nth=0")                              # expand a card, as a user would
        self.page.click("#an-liquidity button:has-text('with liquidity overlay') >> nth=0")
        self.until("location.hash === '#chart' && window.TradeMateApp.liquidityVisible()")
        self.until("window.TradeMateApp.liquidityDrawn() && window.TradeMateApp.liquidityDrawn().pools > 0", timeout=20)
        self.assertEqual(self.page.evaluate("() => window.TradeMateApp.liquidityDrawn().sweeps"), 1)

    def test_default_chart_stays_clean_and_the_toolbar_toggles_and_persists(self):
        self.open_chart()
        self.assertFalse(self.page.evaluate("() => window.TradeMateApp.liquidityVisible()"))
        self.assertIsNone(self.drawn())                                                          # not even requested from the server
        self.assertFalse([r for r in self.requests if "/api/candles" in r[1] and "liquidity=1" in r[1]])
        btn = "#view-chart [data-role=liquidity-btn]"
        self.page.click(btn)
        self.until("window.TradeMateApp.liquidityDrawn() && window.TradeMateApp.liquidityDrawn().pools > 0", timeout=20)
        self.assertEqual(self.page.get_attribute(btn, "aria-pressed"), "true")
        self.page.wait_for_timeout(1200)                                                         # let the debounced save reach the server
        self.page.reload()
        self.until("window.TradeMateApp && window.TradeMateApp.liquidityVisible()")
        self.page.click(btn)
        self.until("!window.TradeMateApp.liquidityVisible()")
        self.assertEqual(self.page.get_attribute(btn, "aria-pressed"), "false")

    def test_pool_lines_have_their_own_colours_and_never_replace_the_quote_lines(self):
        self.set_settings(liquidity_visible=True)
        self.open_chart()
        self.until("window.TradeMateApp.liquidityDrawn() && window.TradeMateApp.liquidityDrawn().pools > 0", timeout=20)
        self.until("window.TradeMateApp.chartQuotes().some(f => f && f.state === 'live')")
        f = self.page.evaluate("() => window.TradeMateApp.chartQuotes().filter(Boolean).pop()")
        self.assertEqual(f["bid"]["color"].lower(), "#ff5c5c")                                    # bid red / ask green are untouched
        self.assertEqual(f["ask"]["color"].lower(), "#2fd38a")
        self.assertEqual(self.page.evaluate("() => document.querySelectorAll('#view-chart [data-role=sl]').length > 0"), True)
        self.assertEqual(self.page.evaluate("() => document.querySelector('#view-chart [data-role=sl]').value"), "")   # no trade level was created
        found = self.page.evaluate("""() => { const cv = document.querySelector('#view-chart canvas'), ctx = cv.getContext('2d'); const seen = {amber: 0, cyan: 0};
            for (let y = 0; y < cv.height; y++) for (let x = 20; x < cv.width - 60; x += 3) { const d = ctx.getImageData(x, y, 1, 1).data;
              if (d[0] > 225 && d[1] > 140 && d[1] < 190 && d[2] < 70) seen.amber++; if (d[0] < 70 && d[1] > 190 && d[2] > 215) seen.cyan++; } return seen; }""")
        self.assertGreater(found["cyan"], 5)                                                      # the sell-side pool is really drawn (cyan)

    def test_settings_controls_apply_validate_and_persist(self):
        self.page.goto(self.base + "/#settings")
        self.until("document.getElementById('cs-liquidity') && !document.getElementById('cs-liquidity').disabled")
        self.page.check("#cs-liquidity")
        self.page.select_option("#cs-liq-mode", "close")
        self.page.wait_for_timeout(1500)
        saved = self.page.evaluate("async () => (await (await fetch('/api/chart/settings')).json()).settings")
        self.assertTrue(saved["liquidity_visible"])
        self.assertEqual(saved["liquidity_sweep_mode"], "close")
        self.page.fill("#cs-liq-ticks", "999")                                                    # out of range: refused, nothing saved
        self.page.dispatch_event("#cs-liq-ticks", "change")
        self.page.wait_for_timeout(1200)
        self.assertEqual(self.page.evaluate("async () => (await (await fetch('/api/chart/settings')).json()).settings.liquidity_tolerance_ticks"), 5)
        self.assertIn("from 0 to 500", self.page.evaluate("() => document.getElementById('cs-status').innerText"))   # the user is told why
        self.page.goto(self.base + "/#chart")
        self.until("window.TradeMateApp.liquidityDrawn() && window.TradeMateApp.liquidityDrawn().pools > 0", timeout=20)
        self.assertEqual(self.page.evaluate("() => window.TradeMateApp.liquidityDrawn().sweeps"), 0)    # close mode: the wick alone is not a sweep

    def test_switching_symbol_or_timeframe_replaces_the_overlay_without_duplicates(self):
        self.set_settings(liquidity_visible=True)
        self.open_chart()
        self.until("window.TradeMateApp.liquidityDrawn() && window.TradeMateApp.liquidityDrawn().pools > 0", timeout=20)
        first = self.drawn()
        self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'M15', {})")
        self.until("window.TradeMateApp.liquidityDrawn() && window.TradeMateApp.liquidityDrawn().key.indexOf('M15') >= 0", timeout=20)
        second = self.drawn()
        self.assertEqual((second["pools"], second["sweeps"]), (first["pools"], first["sweeps"]))   # same candles: same pools, none doubled
        self.page.evaluate("() => window.TradeMateApp.openChart('XAUUSD', 'H1', {})")
        self.until("window.TradeMateApp.liquidityDrawn() && window.TradeMateApp.liquidityDrawn().key.indexOf('XAUUSD') >= 0", timeout=20)
        self.assertNotIn("EURUSD", self.drawn()["key"])

    def test_missing_tick_size_draws_nothing_and_does_not_break_the_chart(self):
        self.set_settings(liquidity_visible=True)
        self.broker.no_spec = True
        self.open_chart()
        self.page.wait_for_timeout(1500)
        self.assertIsNone(self.drawn())
        self.assertGreater(self.page.evaluate("() => document.querySelector('#view-chart canvas').width"), 100)


if __name__ == "__main__":
    unittest.main()
