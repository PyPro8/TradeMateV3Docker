"""Phase 7A.1: REAL browser checks (headless Chromium via Playwright) of the Analysis page, the two-way stop-loss panel and the
Full Test dashboard. The page is served by the real Flask app with a stub broker that can only READ (no order method exists),
so nothing here can place an order.

Skipped (and reported SKIPPED, not passed) when Playwright or Chromium is missing:
    pip install playwright && playwright install chromium
    python3 -m unittest tests.test_browser_phase7a1 -v
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    _PLAYWRIGHT = False

import app as app_module
from tests.test_browser_phase7a import AUDIT_JS, StubBroker
from tests.test_structure_engine import CFG, UP_THEN_BREAK_LOW, series
from tests.test_testrunner import FX, grp
from trademate import analysis
from trademate.broker.base import SymbolInfo
from trademate.chart_settings import ChartSettingsStore
from trademate.testrunner import RunManager

ROOT = Path(__file__).resolve().parent.parent


class SlStub(StubBroker):
    """StubBroker plus a symbol spec so the stop-loss panel has something to calculate with. Still read-only."""

    def bid(self):
        return round(self.series["H1"][-1][4], 2)               # on the 0.01 tick grid, like a real broker's price

    def get_symbols(self):
        return [self._info(self.bid())]

    @staticmethod
    def _info(bid):
        return SymbolInfo("EURUSD", "Euro", 2, 0.01, 2, 100000.0, 0.01, 100.0, 0.01, "EUR", "USD", bid, round(bid + 0.02, 2), 10, 0, 4,
                          tick_size=0.01, tick_value=1.0)

    def get_symbol_spec(self, symbol):
        if symbol != "EURUSD":
            raise app_module.BrokerError("unknown symbol")
        return self._info(self.bid())


def status_payload(code, reason="r", **over):
    base = {"ok": True, "symbol": "EURUSD", "timeframe": "H1", "supported_timeframes": ["M1", "M5", "M15", "M30", "H1", "H4", "D1"],
            "status": {"code": code, "reason": reason, "meaning": "m", "is_trade_signal": False},
            "context": {"candles_used": 0, "latest_candle": None, "time_note": "t",
                        "data": {"freshness": "unavailable", "completeness": "unavailable", "warnings": []}},
            "structure": None, "interpretation": None, "modules": [{"id": "structure", "name": "Market structure", "status": "available"}],
            "config": {}, "caveats": ["BOS and CHoCH are structural classifications."]}
    base.update(over)
    return base


@unittest.skipUnless(_PLAYWRIGHT, "SKIPPED: Playwright is not installed (pip install playwright && playwright install chromium).")
class Phase7A1BrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        cls._tmp = tempfile.TemporaryDirectory()
        cls.broker = SlStub()
        cls.runner = RunManager(Path(cls._tmp.name), groups=[grp("ok", [FX + "ok_mod"]), grp("mixed", [FX + "mixed_mod"])], cwd=ROOT)
        cls.patches = [mock.patch.object(app_module, "adapter", cls.broker),
                       mock.patch.object(app_module, "chart_store", ChartSettingsStore(Path(cls._tmp.name) / "cs.json")),
                       mock.patch.object(app_module, "test_runner", cls.runner)]
        for p in cls.patches:
            p.start()
        cls.server = make_server("127.0.0.1", 0, app_module.app, threaded=True)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            cls.pw.stop()
            cls.server.shutdown()
            for p in cls.patches:
                p.stop()
            raise unittest.SkipTest(f"SKIPPED: Chromium could not start ({str(exc)[:80]}).")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        for p in cls.patches:
            p.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.ctx = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.ctx.new_page()
        self.errors, self.requests, self.dialogs = [], [], []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append("CSP/console: " + m.text) if "Content Security Policy" in m.text else None)
        self.page.on("dialog", lambda d: (self.dialogs.append(d.message), d.dismiss()))
        self.page.on("request", lambda r: self.requests.append((r.method, r.url, r.post_data)))
        self.addCleanup(self.ctx.close)

    def tearDown(self):
        self.assertEqual(self.errors, [], "JavaScript / CSP errors in the page")
        self.assertEqual(self.dialogs, [], "a script dialog opened: injected script ran")

    def until(self, expression, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.page.evaluate(f"() => !!({expression})"):
                return
            self.page.wait_for_timeout(100)
        self.fail(f"Timed out waiting for: {expression}")

    def text(self, selector):
        return self.page.evaluate("(s) => document.querySelector(s).innerText", selector)

    def open_analysis(self, mock_payload=None):
        if mock_payload is not None:
            self.page.route("**/api/analysis**", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(mock_payload)))
        self.page.goto(self.base + "/#analysis")
        self.until("document.getElementById('an-status-code').textContent !== 'ANALYSING'")
        self.page.evaluate("() => { document.getElementById('an-tech').open = true; }")      # Task 2: the detailed sections live in 'Technical details' (collapsed by default)

    # ================= Analysis =================
    def test_analysis_is_in_the_sidebar_between_chart_and_strategy(self):
        self.page.goto(self.base + "/")
        names = self.page.evaluate("() => Array.from(document.querySelectorAll('.nav-link[data-view]')).map(a => a.getAttribute('data-view'))")
        self.assertEqual(names[names.index("chart") + 1], "analysis")
        self.assertEqual(names[names.index("analysis") + 1], "strategy")

    def test_real_analysis_of_the_stub_data_shows_every_section_with_no_invented_content(self):
        self.open_analysis()
        code = self.text("#an-status-code")
        self.assertIn(code, {"READY", "WATCH", "INSUFFICIENT DATA"})
        page = self.text("#view-analysis")
        lower = page.lower()                                                       # headings are upper-cased by CSS
        for needed in ("Instrument and data", "Market structure", "Structural events and why they appear", "Interpretation",
                       "Analysis modules", "Read this first", "MT5 server time", "Not available yet"):
            self.assertIn(needed.lower(), lower)
        if code != "INSUFFICIENT DATA":
            for needed in ("Observed facts", "Rule-based interpretation", "Conditions to watch", "Trade setups", "NOT AVAILABLE"):
                self.assertIn(needed, page)
        self.assertEqual(self.text("#an-symbol"), "EURUSD")
        self.assertEqual(self.page.evaluate("() => document.querySelectorAll('#an-tfs button').length"), 7)
        # analysis only: no order controls on this page, and the page never talked to an order endpoint
        self.assertEqual(self.page.evaluate("() => document.querySelectorAll('#view-analysis [data-role*=buy], #view-analysis [data-role*=sell]').length"), 0)
        self.assertFalse([r for r in self.requests if "/api/order" in r[1]])

    def test_every_status_state_is_rendered_from_the_servers_answer(self):
        self.open_analysis(status_payload("READY", reason="reason for READY"))
        for code in ("READY", "WATCH", "STALE DATA", "INSUFFICIENT DATA", "INVALID DATA", "UNAVAILABLE"):
            self.page.unroute_all()
            payload = status_payload(code, reason=f"reason for {code}")
            body = json.dumps(payload)
            self.page.route("**/api/analysis**", lambda route, request, b=body: route.fulfill(status=200, content_type="application/json", body=b))
            self.page.evaluate("() => window.TradeMateAnalysis.reload()")
            self.until(f"document.getElementById('an-status-reason').textContent === 'reason for {code}'")
            self.assertEqual(self.text("#an-status-code"), code)
            self.assertEqual(self.page.get_attribute("#an-status", "data-code"), code)
            self.assertIn(f"reason for {code}", self.text("#an-status-reason"))
            self.assertNotIn("READY", self.text("#an-status-code")) if code != "READY" else None

    def test_server_text_is_never_interpreted_as_html(self):
        evil = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>'
        payload = status_payload("UNAVAILABLE", reason=evil)
        payload["status"]["meaning"] = evil
        payload["context"]["data"]["warnings"] = [evil]
        payload["caveats"] = [evil]
        self.open_analysis(payload)
        self.assertIn("<img src=x", self.text("#an-status-reason"))                 # shown as text
        self.assertIsNone(self.page.evaluate("() => window.__xss"))
        self.assertEqual(self.page.evaluate("() => document.querySelectorAll('#view-analysis img, #view-analysis script').length"), 0)

    def test_event_explanation_chart_navigation_respects_the_overlay_setting(self):
        res = analysis.build_analysis("EURUSD", "H1", series(UP_THEN_BREAK_LOW), CFG.__class__(2, 2, last_candle_closed=False), 2)
        self.open_analysis(res)
        page = self.text("#an-events")
        for needed in ("CHoCH", "DEVELOPING", "CONFIRMED", "Level broken", "Breaking candle", "still forming"):
            self.assertIn(needed, page)
        self.assertEqual(self.text("#an-status-code"), "WATCH")
        self.assertFalse(self.page.evaluate("() => window.TradeMateApp.structureVisible()"))
        self.page.click("#an-events details:first-child >> text=Show on chart >> nth=0")
        self.until("location.hash === '#chart'")
        self.assertFalse(self.page.evaluate("() => window.TradeMateApp.structureVisible()"))     # saved setting respected
        self.page.goto(self.base + "/#analysis")
        self.until("document.getElementById('an-status-code').textContent !== 'ANALYSING'")

    def test_stale_answers_are_dropped_after_a_quick_timeframe_change(self):
        def handler(route):
            tf = route.request.url.split("timeframe=")[1].split("&")[0]
            if tf == "H1":
                time.sleep(1.2)                                                   # the OLD request answers late
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(status_payload("READY", reason=f"answer for {tf}", timeframe=tf)))
        self.page.route("**/api/analysis**", handler)
        self.page.goto(self.base + "/#analysis")
        self.until("document.querySelector('#an-tfs [data-tf=\"M15\"]')")
        self.page.click("#an-tfs [data-tf=\"M15\"]")
        self.until("document.getElementById('an-status-reason').textContent.indexOf('answer for M15') >= 0")
        self.page.wait_for_timeout(1800)
        self.assertIn("answer for M15", self.text("#an-status-reason"))             # the late H1 answer never replaced it

    def test_analysis_page_text_contrast_and_mobile_layout(self):
        self.open_analysis(analysis.build_analysis("EURUSD", "H1", series(UP_THEN_BREAK_LOW), CFG.__class__(2, 2, last_candle_closed=False), 2))
        for theme in ("dark", "light"):
            self.page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)", theme)
            # gradient-filled primary buttons have no flat background colour, so the audit cannot measure them (white on deep blue)
            bad = [x for x in self.page.evaluate(AUDIT_JS) if not x["disabled"] and "btn-primary" not in x["cls"]]
            self.assertEqual(bad, [], f"low-contrast text in {theme} theme")
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.page.wait_for_timeout(200)
        self.assertLessEqual(self.page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth"), 1)

    # ================= two-way stop-loss panel =================
    def open_panel(self):
        with self.page.expect_response(lambda r: "/api/candles" in r.url and r.status == 200, timeout=15000):
            self.page.goto(self.base + "/#chart")
        self.until("document.querySelector('#view-chart [data-role=sl-box]')")
        self.page.wait_for_timeout(500)

    def vis(self, role):
        return f"#view-chart [data-role={role}]"

    def test_risk_mode_calculates_the_stop_for_buy_and_sell_in_the_account_currency(self):
        self.open_panel()
        bid = self.broker.bid()
        ask = round(bid + 0.02, 2)
        self.page.fill(self.vis("lot"), "0.10")
        self.page.click("#view-chart [data-sl-mode=risk]")
        self.page.fill(self.vis("sl-risk"), "50")
        self.until("document.querySelector('#view-chart [data-role=sl-out]').innerText.indexOf('Final risk') >= 0")
        out = self.text(self.vis("sl-out"))
        self.assertIn("Requested risk: 50.00 USD", out)
        self.assertIn("Final risk at that stop: 50.00 USD", out)
        self.assertIn("live market price", out)
        self.assertAlmostEqual(float(self.page.input_value(self.vis("sl"))), ask - 5.00, places=2)     # BUY: 500 ticks of 0.01 below the ask
        self.assertTrue(self.page.evaluate("() => document.querySelector('#view-chart [data-role=sl]').readOnly"))
        self.page.click("#view-chart [data-sl-dir=sell]")
        self.until("Math.abs(parseFloat(document.querySelector('#view-chart [data-role=sl]').value) - (" + str(bid + 5.0) + ")) < 0.006")
        self.assertIn("Final risk at that stop: 50.00 USD", self.text(self.vis("sl-out")))

    def test_price_mode_shows_the_loss_and_switching_modes_never_overwrites_typed_input(self):
        self.open_panel()
        ask = round(self.broker.bid() + 0.02, 2)
        self.page.fill(self.vis("lot"), "0.10")
        self.page.fill(self.vis("sl"), f"{ask - 2.00:.2f}")
        self.until("document.querySelector('#view-chart [data-role=sl-out]').innerText.indexOf('Loss if the stop-loss is hit') >= 0")
        self.assertIn("Loss if the stop-loss is hit: 20.00 USD", self.text(self.vis("sl-out")))      # 200 ticks x $1 x 0.10 lot
        self.page.click("#view-chart [data-sl-mode=risk]")                         # price -> risk: starts from the current risk
        self.assertEqual(self.page.input_value(self.vis("sl-risk")), "20.00")
        self.assertEqual(self.page.input_value(self.vis("sl")), f"{ask - 2.00:.2f}")      # typed SL not overwritten
        self.page.fill(self.vis("sl-risk"), "35")
        self.page.click("#view-chart [data-sl-mode=price]")                        # risk -> price: the calculated SL stays, editable
        self.assertFalse(self.page.evaluate("() => document.querySelector('#view-chart [data-role=sl]').readOnly"))
        self.assertEqual(self.page.input_value(self.vis("sl-risk")), "35")

    def test_invalid_input_is_explained_and_off_grid_price_is_not_silently_changed(self):
        self.open_panel()
        ask = round(self.broker.bid() + 0.02, 2)
        self.page.fill(self.vis("lot"), "0.10")
        self.page.fill(self.vis("sl"), f"{ask - 2.005:.3f}")                       # not on the 0.01 grid
        self.until("document.querySelector('#view-chart [data-role=sl-out]').innerText.indexOf('Nearest valid') >= 0")
        self.assertEqual(self.page.input_value(self.vis("sl")), f"{ask - 2.005:.3f}")
        self.page.fill(self.vis("sl"), f"{ask + 1.00:.2f}")                        # BUY with the stop ABOVE the entry
        self.until("document.querySelector('#view-chart [data-role=sl-out]').innerText.indexOf('BELOW') >= 0")
        self.page.click("#view-chart [data-sl-mode=risk]")
        self.page.fill(self.vis("sl-risk"), "0.001")                               # smaller than one tick
        self.until("document.querySelector('#view-chart [data-role=sl-out]').innerText.indexOf('single tick') >= 0")
        self.assertEqual(self.page.input_value(self.vis("sl")), "")                # no number is left looking valid

    def test_risk_mode_blocks_a_mismatched_side_and_sends_no_order(self):
        self.open_panel()
        self.page.fill(self.vis("lot"), "0.10")
        self.page.click("#view-chart [data-sl-mode=risk]")
        self.page.fill(self.vis("sl-risk"), "50")
        self.until("document.querySelector('#view-chart [data-role=sl-out]').innerText.indexOf('Final risk') >= 0")
        self.page.click("#view-chart [data-sl-dir=sell]")
        self.until("document.querySelector('#view-chart [data-role=sl-out]').innerText.indexOf('Final risk') >= 0")
        self.page.evaluate("() => { const b = document.querySelector('#view-chart [data-role=panel-buy]'); b.disabled = false; b.click(); }")
        self.until("document.querySelector('#view-chart [data-role=exec]').innerText.indexOf('calculated for SELL') >= 0")
        self.assertFalse([r for r in self.requests if r[0] == "POST" and "/api/order" in r[1]])

    def test_the_panel_only_ever_posts_previews(self):
        self.open_panel()
        self.page.fill(self.vis("lot"), "0.10")
        self.page.click("#view-chart [data-sl-mode=risk]")
        self.page.fill(self.vis("sl-risk"), "50")
        self.page.wait_for_timeout(900)
        posts = [r for r in self.requests if r[0] == "POST"]
        self.assertTrue(posts)
        for method, url, body in posts:
            self.assertTrue("/api/risk/sl" in url or "/api/chart/settings" in url or "/api/watch" in url or "/api/trading" in url, url)
        previews = [json.loads(b) for m, u, b in posts if "/api/risk/sl" in u]
        self.assertTrue(all(set(p) <= {"mode", "symbol", "direction", "volume", "risk_amount", "stop_loss", "request_id"} for p in previews))

    # ================= Full Test dashboard =================
    def test_full_test_page_runs_the_allowlisted_groups_and_reports_honestly(self):
        self.page.goto(self.base + "/full-test")
        self.until("document.querySelectorAll('#ft-groups > li').length === 2")
        self.assertIn("simulated brokers", self.text("#ft-disclaimer").lower())
        self.assertEqual(self.text("#ft-overall"), "NEVER RUN")
        self.page.click("#ft-run-all")
        self.until("document.getElementById('ft-overall').textContent === 'FAIL'", timeout=60)       # the 'mixed' fixture group fails on purpose
        self.until("document.querySelectorAll('#ft-groups .ft-badge[data-status=PASS]').length === 1")
        groups = {li.get_attribute("data-group"): li for li in self.page.query_selector_all("#ft-groups > li")}
        self.assertIn("1 failed", groups["mixed"].inner_text())
        self.assertIn("1 skipped", groups["mixed"].inner_text())
        # the failing test's own output is shown as text, and its <img onerror> never ran
        self.assertIn("<img src=x onerror=alert(1)>", groups["mixed"].inner_text())
        self.assertEqual(self.page.evaluate("() => document.querySelectorAll('#ft-groups img').length"), 0)
        self.assertNotEqual(self.text("#ft-c-when"), "never")
        # opt-in real tests are listed as NOT RUN with their terminal command, never as runnable buttons
        optin = self.text("#ft-optin")
        self.assertIn("NOT RUN", optin)
        self.assertIn("real_demo_execution_test", optin)
        self.assertEqual(self.page.evaluate("() => document.querySelectorAll('#ft-optin button').length"), 0)
        bad = [x for x in self.page.evaluate(AUDIT_JS) if not x["disabled"]]
        self.assertEqual(bad, [], "low-contrast text on the Full Test page")

    def test_full_test_opened_as_a_file_explains_it_cannot_run_and_stays_disabled(self):
        self.page.goto((ROOT / "Full Test.html").as_uri())
        self.page.wait_for_timeout(300)
        self.assertFalse(self.page.evaluate("() => document.getElementById('ft-file-warning').hidden"))
        self.assertIn("opened as a file", self.text("#ft-file-warning"))
        for ident in ("ft-run-all", "ft-run-sel", "ft-cancel", "ft-refresh"):
            self.assertTrue(self.page.evaluate(f"() => document.getElementById('{ident}').disabled"), ident)
        self.page.fill("#ft-port", "5055")
        self.assertEqual(self.page.get_attribute("#ft-open-link", "href"), "http://127.0.0.1:5055/full-test")
        self.assertFalse([r for r in self.requests if "/api/fulltest" in r[1]])      # nothing was even attempted
        self.errors = [e for e in self.errors if "Failed to load resource" not in e]

    def test_sidebar_shows_the_full_test_link_in_development_mode(self):
        self.page.goto(self.base + "/")
        self.assertEqual(self.page.evaluate("() => document.querySelector('.nav-dev a').getAttribute('href')"), "/full-test")


if __name__ == "__main__":
    unittest.main()
