"""Web UI tests (Flask test client). Run with:
    python -m unittest discover -s tests -v
"""

import os
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import app as app_module
from app import app, settings
from trademate.broker import create_adapter
from trademate.config import AppSettings, Mt5Settings


class WebShellTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def get_index(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_index_loads_with_placeholders(self):
        html = self.get_index()
        for text in ("TRADEMATE", "MT5: CHECKING", "Waiting for market data...", "No active trades",
                     "NOT ACTIVE", "Current Market", "Open Trades", "START BACKTEST", "RAPID FIRE"):
            self.assertIn(text, html)
        # Vision: the dead "CREATE STRATEGY" button (it only said "not implemented yet") is gone; strategies are chosen / configured, not created
        self.assertNotIn("CREATE STRATEGY", html)

    def test_all_navigation_sections_exist(self):
        html = self.get_index()
        for name in ("dashboard", "chart", "strategy", "strategylab", "accountcenter", "trades", "journal", "backtest", "settings"):
            self.assertIn(f'id="view-{name}"', html)
            self.assertIn(f'data-view="{name}"', html)

    def test_trade_buttons_start_disabled_until_broker_connects(self):
        html = self.get_index()
        for role in ("panel-buy", "panel-sell"):
            self.assertRegex(html, rf'<button[^>]*data-role="{role}"[^>]*\bdisabled\b')

    def test_the_oversized_quick_buy_and_sell_buttons_are_gone_but_order_entry_remains(self):
        """Task 2 (Objective C): the big chart-bottom BUY / SELL buttons were removed. The Trade Panel buttons and the lot input stay."""
        html = self.get_index()
        for role in ("quick-buy", "quick-sell", "quick-buy-price", "quick-sell-price"):
            self.assertNotIn(f'data-role="{role}"', html)
        for role in ("panel-buy", "panel-sell", "lot", "pos-add"):
            self.assertIn(f'data-role="{role}"', html)
        with open(os.path.join(app.static_folder, "js", "app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertNotIn("quick-buy", js)
        self.assertNotIn("quick-sell", js)
        self.assertIn("placeOrder('buy', root)", js)
        self.assertIn("placeOrder('sell', root)", js)

    def test_chart_workspace_controls_exist(self):
        html = self.get_index()
        for hook in ('data-role="max-btn"', 'data-role="theme-btn"', 'data-role="resize-y"', 'data-role="resize-x"',
                     'data-zoom="reset"', 'data-ctype="line"', 'data-role="hud-ohlc"', 'data-role="lot"'):
            self.assertIn(hook, html)
        with open(os.path.join(app.static_folder, "js", "app.js"), encoding="utf-8") as f:
            js = f.read()
        for needle in ("'Escape'", "sym.bid", "sym.ask", "CHART_COLORS"):
            self.assertIn(needle, js)

    def test_market_card_has_up_down_arrows_and_watchlist(self):
        html = self.get_index()
        for hook in ('id="mkt-up"', 'id="mkt-down"', 'id="sym-search"', 'id="sym-results"', 'id="watch-chips"'):
            self.assertIn(hook, html)

    def test_no_forms_and_no_scripts_beyond_app_js(self):
        html = self.get_index()
        self.assertNotIn("<form", html)
        self.assertEqual(re.findall(r"<script[^>]*src=\"([^\"]+)\"", html), ["/static/js/router.js?v=vision-1", "/static/js/pl.js", "/static/js/outcome.js", "/static/js/slcalc.js", "/static/js/quotes.js", "/static/js/liquidity.js", "/static/js/fvg.js", "/static/js/ob.js", "/static/js/chart_style.js?v=adaptive-candles-1", "/static/js/clocks.js", "/static/js/app.js?v=chart-extras-1", "/static/js/analysis.js", "/static/js/mtf.js", "/static/js/labgreeting.js", "/static/js/strategylab.js?v=cc-redesign-1", "/static/js/commandcentre.js?v=five-strategies-2", "/static/js/candlemomentum.js?v=cm-engine-3", "/static/js/bullbear.js?v=bb-1", "/static/js/safety.js?v=safety-1", "/static/js/dashboard.js?v=vision-1", "/static/js/targetmonitor.js?v=vision-2", "/static/js/strategies.js?v=vision-1", "/static/js/rapidfire.js?v=vision-1", "/static/js/journal.js?v=journal-1"])
        self.assertNotRegex(html, r"<script(?![^>]*src=)")

    def test_javascript_only_talks_to_known_endpoints(self):
        with open(os.path.join(app.static_folder, "js", "app.js"), encoding="utf-8") as f:
            js = f.read()
        for forbidden in ("XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "eval("):
            self.assertNotIn(forbidden, js)
        urls = set(re.findall(r"'(/api/[a-z/]+)", js))
        self.assertTrue(urls >= {"/api/broker", "/api/candles", "/api/chart/settings", "/api/trading", "/api/order"})
        self.assertIn("'/api/position/'", js)
        self.assertNotRegex(js, r"https?://")

    def test_pl_colours_follow_sign_not_direction(self):
        with open(os.path.join(app.static_folder, "js", "app.js"), encoding="utf-8") as f:
            js = f.read()
        with open(os.path.join(app.static_folder, "css", "style.css"), encoding="utf-8") as f:
            css = f.read()
        self.assertRegex(js, r"function plClass\(v\)")
        self.assertIn("'pl-pos'", js)
        self.assertRegex(css, r"\.pl-pos \{ color: var\(--pl-pos\)")
        self.assertRegex(css, r"\.pl-neg \{ color: var\(--pl-neg\)")
        self.assertRegex(css, r"--pl-pos: #[0-9A-Fa-f]{6}")   # blue
        self.assertRegex(css, r"--pl-neg: #[0-9A-Fa-f]{6}")   # red
        self.assertNotRegex(css, r"\.dir-(buy|sell)[^}]*color")  # direction never sets the P/L colour

    def test_only_index_and_static_routes_exist(self):
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        self.assertEqual(rules, {"/",
                                 "/Full Test.html",
                                 "/api/analysis",
                                 "/api/autopilot/emergency-stop",
                                 "/api/autopilot/pause",
                                 "/api/autopilot/resume-pause",
                                 "/api/autopilot/start",
                                 "/api/autopilot/status",
                                 "/api/autopilot/stop",
                                 "/api/broker",
                                 "/api/trade-journal",
                                 "/api/trade-journal/<eid>",
                                 "/api/trade-journal/import-rapid-fire",
                                 "/api/calc/trade",
                                 "/api/bull-bear/config",
                                 "/api/bull-bear/events",
                                 "/api/bull-bear/state",
                                 "/api/candle-momentum/config",
                                 "/api/candle-momentum/events",
                                 "/api/candle-momentum/state",
                                 "/api/candles",
                                 "/api/chart/drawings",
                                 "/api/chart/indicators",
                                 "/api/chart/settings",
                                 "/api/decision/current",
                                 "/api/execution-settings",
                                 "/api/executions",
                                 "/api/executions/<exec_id>/reconcile",
                                 "/api/executions/<exec_id>/resolve",
                                 "/api/fulltest/cancel",
                                 "/api/fulltest/catalog",
                                 "/api/fulltest/run",
                                 "/api/fulltest/status",
                                 "/api/hard-reset",
                                 "/api/hard-reset/restore",
                                 "/api/history",
                                 "/api/local-history/clear",
                                 "/api/lot",
                                 "/api/market/time",
                                 "/api/notifications",
                                 "/api/order",
                                 "/api/order/review",
                                 "/api/pending",
                                 "/api/pending/<int:ticket>/cancel",
                                 "/api/pending/<int:ticket>/modify",
                                 "/api/pending/review",
                                 "/api/pip",
                                 "/api/pipeline/approvals/<approval_id>",
                                 "/api/pipeline/execute",
                                 "/api/pipeline/propose",
                                 "/api/position/<int:ticket>/close",
                                 "/api/position/<int:ticket>/modify",
                                 "/api/rapid-fire/chart-levels",
                                 "/api/rapid-fire/config",
                                 "/api/rapid-fire/lot-suggestion",
                                 "/api/rapid-fire/start",
                                 "/api/rapid-fire/status",
                                 "/api/rapid-fire/stop",
                                 "/api/rapid-fire/trades",
                                 "/api/rapid-fire/trades/<tid>/resolve",
                                 "/api/risk/consecutive",
                                 "/api/risk/consecutive/reset",
                                 "/api/risk/daily-history",
                                 "/api/risk/evaluate",
                                 "/api/risk/rules",
                                 "/api/risk/size",
                                 "/api/risk/sl",
                                 "/api/risk/state",
                                 "/api/safety/status",
                                 "/api/safety/unlock",
                                 "/api/selection",
                                 "/api/sessions",
                                 "/api/strategy/analyse",
                                 "/api/strategy/catalogue",
                                 "/api/strategy/mtf",
                                 "/api/strategy/mtf/trade-review",
                                 "/api/strategy/select",
                                 "/api/strategylab/status",
                                 "/api/strategylab/watchdog/start",
                                 "/api/strategylab/watchdog/stop",
                                 "/api/symbols/add",
                                 "/api/symbols/search",
                                 "/api/symbols/spec",
                                 "/api/trade-settings",
                                 "/api/trading",
                                 "/api/watch",
                                 "/full-test",
                                 "/static/<path:filename>"})

    def test_static_files_served(self):
        for path in ("/static/css/style.css", "/static/js/app.js", "/static/favicon.svg"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            response.close()

    def test_broker_endpoint_survives_missing_mt5(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = create_adapter(replace(settings, mt5=Mt5Settings(data_dir=Path(tmp)), app=AppSettings(data_dir=Path(tmp))))
            with mock.patch.object(app_module, "adapter", empty):
                response = self.client.get("/api/broker")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"]["state"], "DISCONNECTED")
        self.assertIsNone(data["account"])
        self.assertEqual(data["positions"], [])
        self.assertEqual(data["symbols"], [])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_broker_endpoint_only_accepts_get(self):
        self.assertEqual(self.client.post("/api/broker").status_code, 405)

    def test_unknown_route_is_404(self):
        self.assertEqual(self.client.get("/api/status").status_code, 404)

    def test_security_headers(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
