"""Phase 4 tests: trade history and the symbol data behind the TP/SL money values.

Run against tests/fake_bridge.py (a fixture standing in for the MT5 EA). They prove TradeMate's side: that
history comes ONLY from what the bridge reports (nothing invented), that it is parsed safely, and that an old
EA gets a clear upgrade message. They do not prove what real MT5 returns: see TESTING.md for the live check.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.fake_bridge import FakeBridge
from trademate.broker import create_adapter
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.errors import BrokerUnavailableError


class HistoryTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bridge = FakeBridge(Path(self._tmp.name)).start()
        self.addCleanup(self.bridge.stop)
        trading = TradingSettings(command_timeout_seconds=2.0, confirm_timeout_seconds=1.5)
        self.adapter = create_adapter(Settings(app=AppSettings(data_dir=Path(self._tmp.name)), mt5=Mt5Settings(data_dir=Path(self._tmp.name)), trading=trading))


class HistoryAdapterTest(HistoryTestBase):
    def test_no_trades_means_an_empty_list_not_invented_rows(self):
        self.assertEqual(self.adapter.get_history(30), [])

    def test_closed_trade_appears_with_real_fields(self):
        self.assertTrue(self.adapter.place_order("EURUSD", "buy", 0.02, 1.0950, 1.1050).confirmed)
        ticket = self.adapter.get_positions()[0].ticket
        self.assertTrue(self.adapter.close_position(ticket).confirmed)
        trades = self.adapter.get_history(30)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t.ticket, t.symbol, t.direction, t.volume), (ticket, "EURUSD", "buy", 0.02))
        self.assertEqual((t.stop_loss, t.take_profit), (1.095, 1.105))
        self.assertAlmostEqual(t.price_open, 1.10012)       # a buy opens at the ask
        self.assertAlmostEqual(t.price_close, 1.10000)      # and closes at the bid
        self.assertAlmostEqual(t.profit, -0.24, places=2)   # the spread cost, 0.02 lots
        self.assertAlmostEqual(t.net, t.profit)
        self.assertEqual(t.reason, "client")
        self.assertTrue(t.time_close >= t.time_open)

    def test_zero_sl_tp_mean_none(self):
        self.adapter.place_order("EURUSD", "sell", 0.01)
        self.adapter.close_position(self.adapter.get_positions()[0].ticket)
        t = self.adapter.get_history(30)[0]
        self.assertIsNone(t.stop_loss)
        self.assertIsNone(t.take_profit)
        self.assertEqual(t.direction, "sell")

    def test_newest_first_and_limit(self):
        for _ in range(3):
            self.adapter.place_order("EURUSD", "buy", 0.01)
            self.adapter.close_position(self.adapter.get_positions()[0].ticket)
            self.bridge.history[-1]["time_close"] += len(self.bridge.history) * 10   # distinct close times
        trades = self.adapter.get_history(30)
        self.assertEqual(len(trades), 3)
        self.assertEqual([t.time_close for t in trades], sorted((t.time_close for t in trades), reverse=True))
        self.assertEqual(len(self.adapter.get_history(30, limit=2)), 2)

    def test_net_includes_swap_and_commission(self):
        self.bridge.history.append({"ticket": 7, "symbol": "EURUSD", "type": "buy", "volume": 0.1, "price_open": 1.1,
                                    "price_close": 1.101, "sl": 0, "tp": 0, "profit": 10.0, "swap": -0.5,
                                    "commission": -0.7, "time_open": 1, "time_close": 2_000_000_000, "reason": "tp"})
        t = self.adapter.get_history(365)[0]
        self.assertAlmostEqual(t.net, 8.8)
        self.assertEqual(t.reason, "tp")

    def test_garbage_rows_from_the_bridge_are_skipped_not_fatal(self):
        self.bridge.history += [{"symbol": "EURUSD"}, "nonsense",
                                {"ticket": 9, "symbol": "EURUSD", "type": "sell", "volume": "x", "profit": None,
                                 "time_close": 2_000_000_000}]
        trades = self.adapter.get_history(365)
        self.assertEqual([t.ticket for t in trades], [9])
        self.assertIsNone(trades[0].volume)
        self.assertIsNone(trades[0].net)

    def test_old_ea_gets_a_clear_upgrade_message(self):
        self.bridge.legacy = True
        with self.assertRaises(BrokerUnavailableError) as ctx:
            self.adapter.get_history(30)
        self.assertIn("older than v0.4", str(ctx.exception))

    def test_days_and_limit_are_clamped(self):
        self.adapter.get_history(10 ** 6, 10 ** 6)
        sent = [c for c in self.bridge.commands_seen if c["cmd"] == "history"][-1]
        self.assertEqual((sent["days"], sent["limit"]), ("365", "500"))


class SymbolSpecTest(HistoryTestBase):
    def test_spec_has_tick_size_and_value_even_when_the_snapshot_does_not(self):
        snap = [s for s in self.adapter.get_symbols() if s.name == "EURUSD"][0]
        self.assertIsNone(snap.tick_value)                 # the lean snapshot has no tick value...
        spec = self.adapter.get_symbol_spec("EURUSD")      # ...so the spec asks MT5 directly
        self.assertEqual((spec.tick_size, spec.tick_value, spec.tick_value_profit, spec.tick_value_loss),
                         (1e-05, 1.0, 1.0, 1.0))

    def test_unknown_symbol_and_bad_names(self):
        for bad in ("NOPE", "bad name", "a=b", ""):
            with self.assertRaises(BrokerUnavailableError):
                self.adapter.get_symbol_spec(bad)

    def test_a_bridge_without_tick_data_is_reported_not_guessed(self):
        original = self.bridge._symbol_json

        def no_ticks(name, detail=False):
            data = original(name, detail)
            for key in ("tick_size", "tick_value", "tick_value_profit", "tick_value_loss"):
                data.pop(key, None)
            return data
        self.bridge._symbol_json = no_ticks
        with self.assertRaises(BrokerUnavailableError) as ctx:
            self.adapter.get_symbol_spec("EURUSD")
        self.assertIn("tick", str(ctx.exception))


class HistoryWebTest(HistoryTestBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(app_module, "adapter", self.adapter)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.test_client()

    def test_history_endpoint(self):
        empty = self.client.get("/api/history?days=7").get_json()
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["trades"], [])
        self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 0.01, "sl": 1.0995})
        ticket = self.client.get("/api/broker").get_json()["positions"][0]["ticket"]
        self.client.post(f"/api/position/{ticket}/close", json={})
        data = self.client.get("/api/history?days=7").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["currency"], "USD")
        self.assertEqual([t["ticket"] for t in data["trades"]], [ticket])
        self.assertEqual(data["trades"][0]["symbol"], "EURUSD")

    def test_history_endpoint_bad_input_and_old_ea_do_not_crash(self):
        self.assertFalse(self.client.get("/api/history?days=abc").get_json()["ok"])
        self.bridge.legacy = True
        data = self.client.get("/api/history").get_json()
        self.assertFalse(data["ok"])
        self.assertIn("v0.4", data["message"])

    def test_history_is_read_only(self):
        self.assertEqual(self.client.post("/api/history").status_code, 405)

    def test_spec_endpoint(self):
        data = self.client.get("/api/symbols/spec?symbol=EURUSD").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["account_currency"], "USD")
        self.assertEqual(data["spec"]["tick_size"], 1e-05)
        self.assertEqual(data["spec"]["tick_value"], 1.0)
        self.assertFalse(self.client.get("/api/symbols/spec?symbol=NOPE").get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
