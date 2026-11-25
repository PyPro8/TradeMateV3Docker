"""Strategy Lab foundation: the capability registry, /api/strategylab/status and the page markup. No MT5, no orders.

    python3 -m unittest tests.test_strategylab -v
"""

import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module
from app import app
from tests.mtf_fixtures import ReadOnlyFeed
from trademate import strategylab
from trademate.broker.base import ConnectionState, ConnectionStatus

ROOT = Path(__file__).resolve().parent.parent


class Registry(unittest.TestCase):
    def test_every_state_is_one_of_the_documented_states(self):
        for connected in (True, False):
            for c in strategylab.capabilities(connected):
                self.assertIn(c["state"], strategylab.STATES, c)
                self.assertTrue(c["detail"])

    def test_only_working_broker_backed_capabilities_are_available(self):
        caps = {c["id"]: c for c in strategylab.capabilities(True)}
        self.assertEqual(
            {k for k, c in caps.items() if c["state"] == strategylab.AVAILABLE},
            {"broker", "selector", "analyse", "mtf", "decision", "setup", "watchdog", "notifications", "sound", "strategy", "execution", "autopilot"},
        )
        # Controlled execution exists (REVIEW TRADE -> CONFIRM & EXECUTE) and so does Autopilot (trademate/autopilot.py, Trading controls panel).
        self.assertEqual(caps["autopilot"]["state"], strategylab.AVAILABLE)
        self.assertEqual({c["id"]: c["state"] for c in strategylab.capabilities(False)}["execution"], strategylab.DISCONNECTED)

    def test_broker_state_follows_the_real_connection(self):
        self.assertEqual({c["id"]: c["state"] for c in strategylab.capabilities(False)}["broker"], strategylab.DISCONNECTED)
        self.assertEqual({c["id"]: c["state"] for c in strategylab.capabilities(True)}["broker"], strategylab.AVAILABLE)

    def test_the_roadmap_parts_are_named_for_each_planned_capability(self):
        caps = {c["id"]: c["part"] for c in strategylab.capabilities(True)}
        self.assertEqual(
            (caps["selector"], caps["analyse"], caps["mtf"], caps["setup"], caps["decision"], caps["watchdog"], caps["sound"], caps["execution"]),
            (5, 5, 6, 8, 9, 10, 11, None),
        )

    def test_the_registry_cannot_trade_size_or_analyse(self):
        tree = ast.parse((ROOT / "trademate" / "strategylab.py").read_text(encoding="utf-8"))
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual([i for i in imports if getattr(i, "module", "") not in ("__future__",)], [])


class Route(unittest.TestCase):
    def get(self, feed):
        with mock.patch.object(app_module, "adapter", feed):
            return app.test_client().get("/api/strategylab/status").get_json()

    def test_connected_broker(self):
        body = self.get(ReadOnlyFeed({}))
        self.assertTrue(body["ok"] and body["broker"]["connected"])
        self.assertEqual(body["part"], strategylab.IMPLEMENTED_PART)
        self.assertEqual({c["id"]: c["state"] for c in body["capabilities"]}["broker"], "Available")
        self.assertEqual(body["states"], list(strategylab.STATES))

    def test_disconnected_broker_is_reported_as_disconnected(self):
        body = self.get(ReadOnlyFeed({}, status=ConnectionStatus(ConnectionState.DISCONNECTED, "MT5 is not running.")))
        self.assertFalse(body["broker"]["connected"])
        self.assertEqual({c["id"]: c["state"] for c in body["capabilities"]}["broker"], "Disconnected")
        self.assertEqual(body["broker"]["message"], "MT5 is not running.")

    def test_the_route_only_reads_the_connection_state(self):
        """ReadOnlyFeed raises on any attribute other than the read methods, so this fails if the route touched anything else."""
        self.assertTrue(self.get(ReadOnlyFeed({}))["ok"])

    def test_watchdog_start_validates_the_instrument_and_is_read_only(self):
        feed = SimpleNamespace(
            get_connection_status=lambda: ConnectionStatus(ConnectionState.CONNECTED, "ok", 0.1),
            get_symbol_info=lambda symbol: SimpleNamespace(name="EURUSD"),
        )
        monitor = mock.Mock()
        monitor.start.return_value = (True, "Starting read-only monitoring for EURUSD.")
        monitor.status.return_value = {"active": True, "symbol": "EURUSD", "state": "Monitoring", "events": []}
        with mock.patch.object(app_module, "adapter", feed), mock.patch.object(app_module, "watchdog", monitor):
            response = app.test_client().post("/api/strategylab/watchdog/start", json={"symbol": "EURUSD"})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])
        monitor.start.assert_called_once()                              # bound to the selected strategy and its execution timeframe
        self.assertEqual(monitor.start.call_args.args[:2], ("EURUSD", "choch"))

    def test_watchdog_refuses_start_when_broker_is_disconnected(self):
        feed = SimpleNamespace(
            get_connection_status=lambda: ConnectionStatus(ConnectionState.DISCONNECTED, "MT5 is not running."),
        )
        monitor = mock.Mock()
        with mock.patch.object(app_module, "adapter", feed), mock.patch.object(app_module, "watchdog", monitor):
            response = app.test_client().post("/api/strategylab/watchdog/start", json={"symbol": "EURUSD"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["ok"])
        monitor.start.assert_not_called()

    def test_watchdog_stop_is_idempotent(self):
        monitor = mock.Mock()
        monitor.stop.return_value = (True, "Watchdog is stopped.")
        monitor.status.return_value = {"active": False, "state": "Stopped", "events": []}
        with mock.patch.object(app_module, "watchdog", monitor):
            response = app.test_client().post("/api/strategylab/watchdog/stop", json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])


class Markup(unittest.TestCase):
    html = None

    @classmethod
    def setUpClass(cls):
        cls.html = app.test_client().get("/").get_data(as_text=True)

    def test_nav_entry_sits_right_after_strategy(self):
        names = re.findall(r'class="nav-link" href="#(\w+)" data-view="\1"', self.html)
        self.assertEqual(names[names.index("strategy") + 1], "strategylab")
        self.assertEqual(names[names.index("analysis") + 1], "strategy")            # the existing ordering is untouched

    def test_the_page_has_every_planned_area(self):
        for needle in ('id="view-strategylab"', "Command Centre", 'id="lab-market"', 'id="lab-analyse"', 'id="lab-watchdog"', "Strategy decision",
                       "Multi-timeframe context", "Setup details", "Trade review", "Recent activity", "Monitoring", "Notifications and sound", "Execution", "Autopilot",
                       "Welcome to Command Centre"):
            self.assertIn(needle, self.html, needle)
        self.assertIn('id="view-accountcenter"', self.html)
        self.assertIn('id="account-centre-login"', self.html)
        self.assertIn('id="lab-market-dropdown"', self.html)
        self.assertIn('id="lab-market-saved"', self.html)
        self.assertIn("no account-login or account-switch command", self.html)
        self.assertIn('id="lab-greeting"', self.html)
        self.assertIn('id="display-name-input"', self.html)
        self.assertNotIn('class="lab-status-tile"', self.html)
        self.assertNotIn('class="lab-steps"', self.html)
        app_js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Connected · identity verified", app_js)
        css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
        self.assertEqual(css.count(".lab-greeting {"), 1)
        self.assertIn(".lab-toolbar { position: relative; z-index: 5; }", css)
        self.assertIn(".lab-market-saved { position: absolute; z-index: 30;", css)

    def test_every_control_ships_disabled_so_nothing_looks_operational(self):
        section = self.html[self.html.index('id="view-strategylab"'):self.html.index("<!-- ===== Trades")]
        for ident in ("lab-market", "lab-analyse", "lab-watchdog"):          # the old inert lab-autopilot placeholder was replaced by the Trading controls
            tag = re.search(r'<(?:button|select|input)[^>]*id="%s"[^>]*>' % ident, section).group(0)
            self.assertIn(" disabled", tag, ident)
        for ident in ("cc-strategy", "cc-review-btn"):                      # the strategy dropdown and REVIEW TRADE also ship disabled
            tag = re.search(r'<(?:button|select|input)[^>]*id="%s"[^>]*>' % ident, section).group(0)
            self.assertIn(" disabled", tag, ident)
        self.assertRegex(section, r'id="cc-review"[^>]*hidden')             # nothing to review until a strategy qualifies a setup
        self.assertRegex(section, r'id="cc-execute"[^>]*hidden')            # no execute control until the Risk Engine has approved a review
        self.assertNotRegex(section, r"EXECUTE (BUY|SELL)")
        self.assertNotIn("<form", section)

    def test_the_page_says_how_orders_are_and_are_not_sent(self):
        section = self.html[self.html.index('id="view-strategylab"'):self.html.index("<!-- ===== Trades")]
        self.assertIn("only executes setups", section)                                # Autopilot (Trading controls) acts only on risk-approved strategy setups
        self.assertIn("sends nothing", section)                                       # REVIEW TRADE only sizes with the Risk Engine
        self.assertIn("REVIEW TRADE", section)
        self.assertIn("CONFIRM &amp; EXECUTE", section)                               # an explicit second step is required
        self.assertIn("cancels a pending review", section)                            # strategy switching cancels pending proposals

    def test_script_never_builds_html(self):
        src = (ROOT / "static" / "js" / "strategylab.js").read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(bad, src)

    def test_the_existing_strategy_page_is_still_there_with_the_risk_engine_and_mtf_panel(self):
        for needle in ('id="view-strategy"', 'id="mtf-panel"', "Proposed Trade", "Risk Engine"):
            self.assertIn(needle, self.html)


if __name__ == "__main__":
    unittest.main()
