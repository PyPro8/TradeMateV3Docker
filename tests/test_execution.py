"""Execution tests: place / modify / close through the command channel.

These run against tests/fake_bridge.py, a FIXTURE standing in for the MT5 EA. They prove TradeMate's side
of the protocol and its honesty rules (no success before confirmation, clear broker errors, expiry,
unknown outcome). They do NOT prove real MT5 / Headway behaviour: use `python -m trademate.broker.live_test`.
"""

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.fake_bridge import FakeBridge
from trademate.broker import create_adapter
from trademate.broker.base import SymbolInfo
from trademate.broker.validation import check_stops, check_volume
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.errors import ExecutionUnconfirmedError, OrderRejectedError

INFO = SymbolInfo("EURUSD", "", 5, 1e-05, 12, 100000.0, 0.01, 100.0, 0.01, "EUR", "USD", 1.1, 1.10012,
                  stops_level=10, freeze_level=0, trade_mode=4)


class ValidationTest(unittest.TestCase):
    def test_volume_rules(self):
        self.assertEqual(check_volume(INFO, 0.05, 1.0), 0.05)
        for bad in (0, -1, 0.001, 0.015, 150, float("nan"), True, "1"):
            with self.assertRaises(OrderRejectedError):
                check_volume(INFO, bad, 1.0)
        with self.assertRaises(OrderRejectedError):  # TradeMate's own safety cap
            check_volume(INFO, 2.0, 1.0)

    def test_buy_stops_side_and_distance(self):
        self.assertEqual(check_stops(INFO, "buy", 1.1, 1.0950, 1.1050), (1.095, 1.105))
        for sl, tp in ((1.1050, None), (1.09995, None), (None, 1.0950), (None, 1.10005)):
            with self.assertRaises(OrderRejectedError):
                check_stops(INFO, "buy", 1.1, sl, tp)

    def test_sell_stops_side_and_distance(self):
        self.assertEqual(check_stops(INFO, "sell", 1.1, 1.1050, 1.0950), (1.105, 1.095))
        with self.assertRaises(OrderRejectedError):
            check_stops(INFO, "sell", 1.1, 1.0950, None)
        with self.assertRaises(OrderRejectedError):
            check_stops(INFO, "sell", 1.1, None, 1.1050)

    def test_non_positive_levels_rejected(self):
        with self.assertRaises(OrderRejectedError):
            check_stops(INFO, "buy", 1.1, -1, None)


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bridge = FakeBridge(Path(self._tmp.name)).start()
        self.addCleanup(self.bridge.stop)
        self.trading = TradingSettings(command_timeout_seconds=2.0, confirm_timeout_seconds=1.5)
        self.adapter = create_adapter(Settings(app=AppSettings(data_dir=Path(self._tmp.name)), mt5=Mt5Settings(data_dir=Path(self._tmp.name)), trading=self.trading))

    def test_buy_is_confirmed_by_broker_state(self):
        result = self.adapter.place_order("EURUSD", "buy", 0.01, stop_loss=1.09, take_profit=1.11)
        self.assertTrue(result.ok and result.confirmed)
        self.assertEqual((result.position.symbol, result.position.direction, result.position.volume), ("EURUSD", "buy", 0.01))
        self.assertEqual((result.position.stop_loss, result.position.take_profit), (1.09, 1.11))
        self.assertEqual([p.ticket for p in self.adapter.get_positions()], [result.ticket])

    def test_sell_is_confirmed(self):
        result = self.adapter.place_order("EURUSD", "sell", 0.02)
        self.assertTrue(result.confirmed)
        self.assertEqual(self.adapter.get_positions()[0].direction, "sell")

    def test_modify_tp_then_sl_reflect_broker_values(self):
        ticket = self.adapter.place_order("EURUSD", "buy", 0.01).ticket
        r = self.adapter.modify_position(ticket, None, 1.1050, change_sl=False, change_tp=True)
        self.assertTrue(r.confirmed)
        self.assertEqual(r.position.take_profit, 1.105)
        r = self.adapter.modify_position(ticket, 1.095, None, change_sl=True, change_tp=False)
        self.assertTrue(r.confirmed)
        self.assertEqual((r.position.stop_loss, r.position.take_profit), (1.095, 1.105))  # TP untouched
        r = self.adapter.modify_position(ticket, None, None, change_sl=True, change_tp=False)  # remove SL
        self.assertIsNone(r.position.stop_loss)

    def test_modify_unchanged_sends_nothing(self):
        ticket = self.adapter.place_order("EURUSD", "buy", 0.01, take_profit=1.11).ticket
        seen = len(self.bridge.commands_seen)
        r = self.adapter.modify_position(ticket, None, 1.11, change_sl=False, change_tp=True)
        self.assertTrue(r.confirmed)
        self.assertEqual(len(self.bridge.commands_seen), seen)

    def test_invalid_stop_is_stopped_before_sending(self):
        ticket = self.adapter.place_order("EURUSD", "buy", 0.01).ticket
        seen = len(self.bridge.commands_seen)
        with self.assertRaises(OrderRejectedError) as ctx:
            self.adapter.modify_position(ticket, 1.2000, None, True, False)   # SL above price for a buy
        self.assertIn("Stop loss", str(ctx.exception))
        self.assertEqual(len(self.bridge.commands_seen), seen)
        self.assertIsNone(self.adapter.get_positions()[0].stop_loss)

    def test_close_disappears_only_after_broker_confirms(self):
        ticket = self.adapter.place_order("EURUSD", "buy", 0.01).ticket
        r = self.adapter.close_position(ticket)
        self.assertTrue(r.ok and r.confirmed)
        self.assertEqual(self.adapter.get_positions(), [])

    def test_broker_rejection_is_reported_with_its_reason(self):
        self.bridge.reject_with = (10019, "No money")
        with self.assertRaises(OrderRejectedError) as ctx:
            self.adapter.place_order("EURUSD", "buy", 0.01)
        self.assertIn("No money", str(ctx.exception))
        self.assertIn("10019", str(ctx.exception))
        self.assertEqual(self.adapter.get_positions(), [])   # nothing was invented

    def test_failed_modify_leaves_broker_values_visible(self):
        ticket = self.adapter.place_order("EURUSD", "buy", 0.01, take_profit=1.11).ticket
        self.bridge.reject_with = (10016, "Invalid stops")
        with self.assertRaises(OrderRejectedError):
            self.adapter.modify_position(ticket, None, 1.2, change_sl=False, change_tp=True)
        self.assertEqual(self.adapter.get_positions()[0].take_profit, 1.11)

    def test_accepted_but_not_visible_is_not_reported_as_confirmed(self):
        self.bridge.apply_but_hide = True
        r = self.adapter.place_order("EURUSD", "buy", 0.01)
        self.assertTrue(r.ok)
        self.assertFalse(r.confirmed)

    def test_no_answer_withdraws_command_and_executes_nothing(self):
        self.bridge.stop()   # MT5 / EA not running
        with self.assertRaises(Exception) as ctx:
            self.adapter.place_order("EURUSD", "buy", 0.01)
        self.assertNotIsInstance(ctx.exception, ExecutionUnconfirmedError)
        self.assertEqual(list(Path(self._tmp.name).glob("trademate_cmd_*")), [])  # command withdrawn

    def test_hung_ea_means_outcome_unknown(self):
        self.bridge.silent = True
        with self.assertRaises(ExecutionUnconfirmedError):
            self.adapter.place_order("EURUSD", "buy", 0.01)

    def test_non_demo_account_is_refused_before_any_command(self):
        self.bridge.trade_mode = "real"
        time.sleep(0.4)
        with self.assertRaises(Exception):
            self.adapter.place_order("EURUSD", "buy", 0.01)
        self.assertEqual(self.bridge.commands_seen, [])

    def test_candles(self):
        candles = self.adapter.get_candles("EURUSD", "H1", 50)
        self.assertEqual(len(candles), 50)
        self.assertLess(candles[0].time, candles[-1].time)

    def test_external_changes_are_seen(self):
        """A position opened/closed outside TradeMate (MT5 mobile) shows up via the same snapshot."""
        self.bridge.positions[777] = {"symbol": "EURUSD", "type": "sell", "volume": 0.01,
                                      "price_open": 1.1, "sl": None, "tp": None}
        time.sleep(0.5)
        self.assertEqual([p.ticket for p in self.adapter.get_positions()], [777])
        del self.bridge.positions[777]
        time.sleep(0.5)
        self.assertEqual(self.adapter.get_positions(), [])


class DynamicSymbolTest(unittest.TestCase):
    """Symbols outside the snapshot (not in the 'Market Watch'): search, info, chart, trade, manage."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bridge = FakeBridge(Path(self._tmp.name)).start()
        self.addCleanup(self.bridge.stop)
        self.adapter = create_adapter(Settings(app=AppSettings(data_dir=Path(self._tmp.name)), mt5=Mt5Settings(data_dir=Path(self._tmp.name)),
                                               trading=TradingSettings(command_timeout_seconds=2.0, confirm_timeout_seconds=1.5)))

    def test_snapshot_does_not_contain_the_new_symbol(self):
        self.assertEqual([s.name for s in self.adapter.get_symbols()], ["EURUSD"])

    def test_search_covers_whole_catalogue_by_name_description_and_path(self):
        by_name = self.adapter.search_symbols("xau")
        self.assertEqual([m.name for m in by_name.matches], ["XAUUSD"])
        self.assertEqual(by_name.total_symbols, 6)
        self.assertEqual([m.name for m in self.adapter.search_symbols("oil").matches], ["UKOIL"])
        self.assertEqual([m.name for m in self.adapter.search_symbols("indices").matches], ["US30.x"])
        self.assertEqual(self.adapter.search_symbols("zzz").matches, [])

    def test_symbol_info_on_demand_has_rules_and_prices(self):
        info = self.adapter.get_symbol_info("US30.x")
        self.assertEqual((info.digits, info.point, info.volume_min, info.volume_step), (1, 0.1, 0.1, 0.1))
        self.assertEqual((info.bid, info.ask), (38000.0, 38002.0))
        self.assertEqual(info.path, "Indices\\US")
        self.assertEqual(info.contract_size, 100000.0)
        self.assertIsNotNone(info.tick_value)

    def test_unknown_or_malicious_symbol_names(self):
        for bad in ("NOPE", "EUR USD", "A\nB", "", "x" * 60, "a=b"):
            with self.assertRaises(Exception):
                self.adapter.get_symbol_info(bad)
        with self.assertRaises(OrderRejectedError):
            self.adapter.place_order("NOPE", "buy", 0.01)

    def test_candles_for_new_symbol(self):
        self.assertEqual(len(self.adapter.get_candles("XAUUSD", "M5", 30)), 30)

    def test_full_trade_cycle_on_symbol_not_in_snapshot(self):
        r = self.adapter.place_order("XAUUSD", "buy", 0.01)          # min lot / step of this symbol
        self.assertTrue(r.confirmed)
        self.assertEqual(r.position.symbol, "XAUUSD")
        self.assertEqual(r.position.price_open, 2000.30)
        m = self.adapter.modify_position(r.ticket, None, 2010.0, change_sl=False, change_tp=True)
        self.assertTrue(m.confirmed)
        self.assertEqual(m.position.take_profit, 2010.0)
        self.assertTrue(self.adapter.close_position(r.ticket).confirmed)
        self.assertEqual(self.adapter.get_positions(), [])

    def test_symbol_lot_rules_apply_per_symbol(self):
        with self.assertRaises(OrderRejectedError):
            self.adapter.place_order("US30.x", "buy", 0.05)           # step is 0.1
        self.assertTrue(self.adapter.place_order("US30.x", "sell", 0.1).confirmed)

    def test_trading_disabled_symbol_is_refused_before_sending(self):
        with self.assertRaises(OrderRejectedError):
            self.adapter.place_order("CLOSED1", "buy", 0.01)
        self.assertNotIn("open", [c["cmd"] for c in self.bridge.commands_seen])  # only the read-only lookup ran

    def test_position_symbol_always_exported_and_watchlist_streams(self):
        self.adapter.place_order("BTCUSD", "sell", 0.01)
        time.sleep(0.5)
        self.assertIn("BTCUSD", [s.name for s in self.adapter.get_symbols()])
        self.adapter.set_watchlist(["UKOIL", "bad name", "UKOIL"])
        time.sleep(0.6)
        names = [s.name for s in self.adapter.get_symbols()]
        self.assertIn("UKOIL", names)
        self.assertEqual((Path(self._tmp.name) / "trademate_watch.txt").read_text().split(), ["UKOIL"])
        self.adapter.add_to_watchlist("XAUUSD")
        self.assertEqual((Path(self._tmp.name) / "trademate_watch.txt").read_text().split(), ["UKOIL", "XAUUSD"])

    def test_old_ea_gives_a_clear_upgrade_message(self):
        self.bridge.legacy = True
        with self.assertRaises(Exception) as ctx:
            self.adapter.search_symbols("xau")
        self.assertIn("older than v0.3", str(ctx.exception))

    def test_existing_symbol_still_trades(self):
        self.assertTrue(self.adapter.place_order("EURUSD", "buy", 0.01).confirmed)


class WebExecutionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bridge = FakeBridge(Path(self._tmp.name)).start()
        self.addCleanup(self.bridge.stop)
        trading = TradingSettings(command_timeout_seconds=2.0, confirm_timeout_seconds=1.5)
        adapter = create_adapter(Settings(app=AppSettings(data_dir=Path(self._tmp.name)), mt5=Mt5Settings(data_dir=Path(self._tmp.name)), trading=trading))
        patcher = mock.patch.object(app_module, "adapter", adapter)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.test_client()

    def test_full_cycle_over_http(self):
        r = self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 0.01, "sl": 1.0995}).get_json()
        self.assertEqual(r["state"], "executed")
        ticket = r["result"]["ticket"]
        broker = self.client.get("/api/broker").get_json()
        self.assertEqual([p["ticket"] for p in broker["positions"]], [ticket])

        r = self.client.post(f"/api/position/{ticket}/modify", json={"tp": 1.105}).get_json()
        self.assertEqual(r["state"], "executed")
        self.assertEqual(r["result"]["position"]["take_profit"], 1.105)

        r = self.client.post(f"/api/position/{ticket}/close", json={}).get_json()
        self.assertEqual(r["state"], "executed")
        self.assertEqual(self.client.get("/api/broker").get_json()["positions"], [])

    def test_failure_returns_state_failed_with_message(self):
        r = self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 0}).get_json()
        self.assertEqual(r["state"], "failed")
        self.assertIn("Volume", r["message"])
        self.bridge.reject_with = (10019, "No money")
        r = self.client.post("/api/order", json={"symbol": "EURUSD", "side": "sell", "volume": 0.01, "sl": 1.1005}).get_json()
        self.assertEqual(r["state"], "failed")
        self.assertIn("No money", r["message"])

    def test_requires_json_and_same_origin(self):
        r = self.client.post("/api/order", data="symbol=EURUSD").get_json()
        self.assertEqual(r["state"], "failed")
        r = self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 0.01, "sl": 1.0995},
                             headers={"Origin": "http://evil.example"}).get_json()
        self.assertEqual(r["state"], "failed")
        self.assertEqual(self.bridge.commands_seen, [])

    def test_symbol_endpoints(self):
        r = self.client.get("/api/symbols/search?q=gold").get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["matches"][0]["name"], "XAUUSD")
        r = self.client.post("/api/symbols/add", json={"symbol": "XAUUSD"}).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["symbol"]["digits"], 2)
        self.assertTrue(self.client.post("/api/watch", json={"symbols": ["XAUUSD"]}).get_json()["ok"])
        self.assertFalse(self.client.post("/api/symbols/add", json={"symbol": "NOPE"}).get_json()["ok"])
        self.assertFalse(self.client.post("/api/symbols/add", json={"symbol": "XAUUSD"},
                                          headers={"Origin": "http://evil.example"}).get_json()["ok"])
        o = self.client.post("/api/order", json={"symbol": "XAUUSD", "side": "buy", "volume": 0.01, "sl": 1999.5}).get_json()
        self.assertEqual(o["state"], "executed")
        pos = self.client.get("/api/broker").get_json()["positions"]
        self.assertEqual([p["symbol"] for p in pos], ["XAUUSD"])

    def test_candles_endpoint(self):
        r = self.client.get("/api/candles?symbol=EURUSD&timeframe=H1&count=20").get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["candles"]), 20)
        bad = self.client.get("/api/candles?symbol=NOPE&timeframe=H1").get_json()
        self.assertFalse(bad["ok"])


if __name__ == "__main__":
    unittest.main()
