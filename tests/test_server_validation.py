"""Phase 3: expected-server validation. Headway-Demo is the only accepted server, and everything else FAILS CLOSED.

SIMULATED (fake bridge): no real order is ever placed.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_bridge import FakeBridge
from trademate.broker import create_adapter
from trademate.broker.server_check import DEFAULT_EXPECTED_SERVER, check_server, normalise_server
from trademate.config import (AppSettings, ConfigError, Mt5Settings, Settings, TradingSettings, load_config)
from trademate.errors import OrderRejectedError

GOOD = dict(symbol="EURUSD", side="buy", volume=0.01, stop_loss=1.0950, take_profit=1.1100)


class CheckServerRule(unittest.TestCase):
    def test_headway_demo_is_accepted(self):
        self.assertTrue(check_server("Headway-Demo", "Headway-Demo")[0])

    def test_case_and_whitespace_only_differences_are_accepted(self):
        for got in ("headway-demo", "HEADWAY-DEMO", "  Headway-Demo  ", "Headway-Demo\n"):
            self.assertTrue(check_server("Headway-Demo", got)[0], got)
        self.assertEqual(normalise_server("  A   B "), "a b")

    def test_unrelated_and_near_miss_servers_are_rejected(self):
        for got in ("Headway-Real", "Headway-Demo2", "NotHeadway-Demo", "Headway-Demo-2", "Headway Demo", "Headway", "ICMarkets-Demo",
                    "mt5.demo.trade-hw.online"):
            ok, why = check_server("Headway-Demo", got)
            self.assertFalse(ok, got)
            self.assertIn("expects", why)

    def test_missing_connected_server_is_rejected(self):
        for got in (None, "", "   "):
            ok, why = check_server("Headway-Demo", got)
            self.assertFalse(ok)
            self.assertIn("cannot be verified", why)

    def test_missing_expected_server_is_rejected_not_waved_through(self):
        for want in (None, "", "  "):
            ok, why = check_server(want, "Headway-Demo")
            self.assertFalse(ok)
            self.assertIn("No expected MT5 server", why)

    def test_reason_never_contains_secrets(self):
        ok, why = check_server("Headway-Demo", "Other")
        self.assertNotRegex(why.lower(), r"password|token|secret|key")

    def test_canonical_default(self):
        self.assertEqual(DEFAULT_EXPECTED_SERVER, "Headway-Demo")
        self.assertEqual(Mt5Settings().expected_server, "Headway-Demo")


class ConfigPrecedence(unittest.TestCase):
    def load(self, toml_text, env=None):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.toml"
            p.write_text(toml_text, encoding="utf-8")
            with mock.patch.dict(os.environ, env or {}, clear=False):
                os.environ.pop("TRADEMATE_EXPECTED_SERVER", None) if not env else None
                return load_config(p).mt5.expected_server

    def test_shipped_settings_file_says_headway_demo(self):
        from trademate.config import DEFAULT_CONFIG_PATH
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRADEMATE_EXPECTED_SERVER", None)
            self.assertEqual(load_config(DEFAULT_CONFIG_PATH).mt5.expected_server, "Headway-Demo")

    def test_built_in_default_when_the_file_is_silent(self):
        self.assertEqual(self.load("[mt5]\n"), "Headway-Demo")

    def test_file_value_beats_default(self):
        self.assertEqual(self.load('[mt5]\nexpected_server = "Headway-Real"\n'), "Headway-Real")

    def test_environment_beats_file(self):
        self.assertEqual(self.load('[mt5]\nexpected_server = "Headway-Real"\n', {"TRADEMATE_EXPECTED_SERVER": "Headway-Demo"}),
                         "Headway-Demo")

    def test_value_is_trimmed(self):
        self.assertEqual(self.load('[mt5]\nexpected_server = "  Headway-Demo  "\n'), "Headway-Demo")


class OrderPathFailsClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.bridge = FakeBridge(self.dir).start()
        self.addCleanup(self.bridge.stop)

    def adapter(self, expected="Headway-Demo"):
        return create_adapter(Settings(app=AppSettings(data_dir=self.dir), mt5=Mt5Settings(data_dir=self.dir, expected_server=expected),
                                       trading=TradingSettings(command_timeout_seconds=2.0, confirm_timeout_seconds=1.5)))

    def opens(self):
        return [c for c in self.bridge.commands_seen if c.get("cmd") == "open"]

    def test_headway_demo_order_goes_through_when_everything_else_passes(self):
        r = self.adapter().place_order(**GOOD)
        self.assertTrue(r.confirmed)
        self.assertEqual(len(self.opens()), 1)

    def test_unrelated_server_refuses_the_order_before_the_bridge(self):
        with mock.patch.object(FakeBridge, "SERVER", "Headway-Real"):
            self.bridge.write_snapshot()
            with self.assertRaises(OrderRejectedError) as cm:
                self.adapter().place_order(**GOOD)
        self.assertIn("REFUSED", str(cm.exception))
        self.assertIn("Headway-Real", str(cm.exception))
        self.assertEqual(self.opens(), [])

    def test_missing_server_information_refuses_the_order(self):
        with mock.patch.object(FakeBridge, "SERVER", ""):
            self.bridge.write_snapshot()
            with self.assertRaises(OrderRejectedError) as cm:
                self.adapter().place_order(**GOOD)
        self.assertIn("cannot be verified", str(cm.exception))
        self.assertEqual(self.opens(), [])

    def test_no_expected_server_configured_refuses_the_order(self):
        with self.assertRaises(OrderRejectedError):
            self.adapter(expected="").place_order(**GOOD)
        self.assertEqual(self.opens(), [])

    def test_override_changes_the_decision_predictably(self):
        with mock.patch.object(FakeBridge, "SERVER", "Headway-Real"):
            self.bridge.write_snapshot()
            with self.assertRaises(OrderRejectedError):
                self.adapter("Headway-Demo").place_order(**GOOD)
            self.assertTrue(self.adapter("Headway-Real").place_order(**GOOD).confirmed)      # explicit override wins

    def test_status_still_connects_but_warns_when_the_server_differs(self):
        with mock.patch.object(FakeBridge, "SERVER", "Other"):
            self.bridge.write_snapshot()
            st = self.adapter().get_connection_status()
        self.assertEqual(st.state.value.lower(), "connected")
        self.assertTrue(any("refused" in w.lower() for w in st.warnings))


if __name__ == "__main__":
    unittest.main()
