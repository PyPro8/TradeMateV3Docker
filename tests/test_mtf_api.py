"""Multi-timeframe Strategy Engine: service, /api/strategy/mtf and the [strategy] settings.

A read-only stub feed (no order method exists on it: touching one fails the test), a fixed clock, no MT5, no orders.

    python3 -m unittest tests.test_mtf_api -v
"""

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.mtf_fixtures import NOW, ReadOnlyFeed, buy_rows
from trademate.broker.base import ConnectionState, ConnectionStatus
from trademate.config import ConfigError, StrategySettings, load_config
from trademate.strategy import service


class Clock:
    """Stands in for the `time` module inside app.py only."""

    @staticmethod
    def time():
        return NOW


class ApiBase(unittest.TestCase):
    def use(self, feed, lookback=24):
        patches = [mock.patch.object(app_module, "adapter", feed), mock.patch.object(app_module, "time", Clock),
                   mock.patch.object(app_module, "settings", dataclasses.replace(app_module.settings, strategy=StrategySettings(trigger_lookback=lookback)))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return app.test_client()

    def result(self, client, symbol="EURUSD"):
        resp = client.get(f"/api/strategy/mtf?symbol={symbol}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertTrue(body["ok"])
        return body["result"]


class StrategyApi(ApiBase):
    def test_returns_the_full_structured_result_and_a_buy_for_the_reference_scenario(self):
        feed = ReadOnlyFeed(buy_rows())
        r = self.result(self.use(feed))
        self.assertEqual((r["decision"], r["direction"], r["setup_status"]), ("BUY", "bullish", "confirmed"))
        for tf in ("M5", "M15", "H1", "H4"):
            self.assertEqual(r["data"][tf]["status"], "ok")
        self.assertTrue(r["clock"]["known"])
        self.assertEqual(r["rule_id"], "MTF-ZONE-REACTION-v1")

    def test_h4_is_fetched_as_native_data_from_the_broker(self):
        feed = ReadOnlyFeed(buy_rows())
        r = self.result(self.use(feed))
        self.assertEqual(set(feed.requested), {"M1", "M5", "M15", "H1", "H4"}, feed.requested)
        self.assertIsNone(r["data"]["H4"]["build"])

    def test_h4_is_unavailable_when_native_h4_cannot_be_fetched(self):
        r = self.result(self.use(ReadOnlyFeed(buy_rows(), fail={"H1"})))
        self.assertEqual(r["data"]["H1"]["status"], "unavailable")
        self.assertEqual(r["data"]["H4"]["status"], "ok")
        r = self.result(self.use(ReadOnlyFeed(buy_rows(), fail={"H4"})))
        self.assertEqual(r["data"]["H4"]["status"], "unavailable")
        self.assertIn("H4", r["data"]["H4"]["reason"])
        self.assertEqual(r["decision"], "WATCH")
        self.assertIsNone(r["entry"])

    def test_a_missing_entry_timeframe_reports_candidate_without_qualifying_it(self):
        for tf in ("M5", "M15"):
            with self.subTest(missing=tf):
                r = self.result(self.use(ReadOnlyFeed(buy_rows(), fail={tf})))
                expected = "WATCH" if tf == "M5" else "WAIT"
                self.assertEqual((r["decision"], r["data"][tf]["status"]), (expected, "unavailable"))
                self.assertIsNone(r["entry"])
                self.assertIsNone(r.get("selected_setup_family"))

    def test_disconnected_broker_waits_with_every_timeframe_unavailable(self):
        feed = ReadOnlyFeed(buy_rows(), status=ConnectionStatus(ConnectionState.DISCONNECTED, "MT5 is not running."))
        r = self.result(self.use(feed))
        self.assertEqual(r["decision"], "WAIT")
        self.assertEqual({d["status"] for d in r["data"].values()}, {"unavailable"})
        self.assertEqual(feed.requested, [])                                      # nothing was fetched
        self.assertFalse(r["clock"]["known"])

    def test_unknown_broker_clock_never_confirms_a_setup(self):
        feed = ReadOnlyFeed(buy_rows(), fail={"M1"})                              # no fresh 1-minute candle: the offset cannot be measured
        r = self.result(self.use(feed))
        self.assertFalse(r["clock"]["known"])
        self.assertNotIn(r["decision"], ("BUY", "SELL"))
        self.assertTrue(any(i["code"] == "clock_unknown" for d in r["data"].values() for i in d["issues"]))

    def test_stale_market_data_waits(self):
        feed = ReadOnlyFeed(buy_rows(), now=NOW)
        with mock.patch.object(Clock, "time", staticmethod(lambda: NOW + 8 * 3600)):
            feed.now = NOW + 8 * 3600
            r = self.result(self.use(feed))
        self.assertEqual(r["decision"], "WAIT")
        self.assertEqual(r["data"]["M5"]["status"], "stale")

    def test_the_route_cannot_trade_or_size(self):
        """ReadOnlyFeed raises AssertionError on any attribute other than status, symbols, spec and candles."""
        with mock.patch("trademate.risk.engine.evaluate_trade") as risk:
            self.result(self.use(ReadOnlyFeed(buy_rows())))
        risk.assert_not_called()

    def test_symbol_is_required_and_internal_errors_do_not_leak(self):
        client = self.use(ReadOnlyFeed(buy_rows()))
        self.assertEqual(client.get("/api/strategy/mtf").status_code, 400)
        self.assertEqual(client.get("/api/strategy/mtf?symbol=" + "X" * 40).status_code, 400)
        with mock.patch.object(app_module, "evaluate_strategy", side_effect=RuntimeError("secret internals")):
            resp = client.get("/api/strategy/mtf?symbol=EURUSD")
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("secret", resp.get_data(as_text=True))

    def test_settings_lookback_changes_the_decision(self):
        r = self.result(self.use(ReadOnlyFeed(buy_rows()), lookback=12))
        self.assertEqual(r["decision"], "WATCH")

    def test_the_analysis_page_still_reports_its_modules_unchanged(self):
        """Regression: the single-timeframe Analysis page keeps saying multi-timeframe / setups are not part of it."""
        from trademate.analysis import MODULES
        self.assertEqual({m["id"]: m["status"] for m in MODULES}["multi_timeframe"], "not_available")


class StrategySettingsTest(unittest.TestCase):
    def load(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.toml"
            path.write_text(body, encoding="utf-8")
            return load_config(path)

    def test_defaults_when_the_section_is_absent(self):
        self.assertEqual(self.load("").strategy, StrategySettings())

    def test_values_are_read(self):
        s = self.load("[strategy]\nmin_rr = 2\ntrigger_lookback = 20\nsl_buffer_ticks = 3\n").strategy
        self.assertEqual((s.min_rr, s.trigger_lookback, s.sl_buffer_ticks), (2.0, 20, 3))

    def test_bad_values_are_rejected_with_a_clear_message(self):
        for body in ("[strategy]\nmin_rr = 0\n", "[strategy]\nmin_rr = \"high\"\n", "[strategy]\ntrigger_lookback = 0\n",
                     "[strategy]\ntrigger_lookback = 2.5\n", "[strategy]\nsl_buffer_ticks = -1\n"):
            with self.subTest(body=body), self.assertRaises(ConfigError) as ctx:
                self.load(body)
            self.assertIn("[strategy]", str(ctx.exception))

    def test_shipped_settings_file_parses(self):
        self.assertEqual(load_config().strategy.min_rr, 1.5)

    def test_tick_buffer_is_converted_with_the_symbols_tick_size(self):
        cfg = service.strategy_config(dataclasses.replace(load_config(), strategy=StrategySettings(sl_buffer_ticks=5)), tick_size=0.01)
        self.assertAlmostEqual(cfg.sl_buffer, 0.05)
        self.assertEqual(service.strategy_config(load_config(), tick_size=0.01).sl_buffer, 0.0)


if __name__ == "__main__":
    unittest.main()
