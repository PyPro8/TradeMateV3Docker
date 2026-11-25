"""MT5 adapter tests.

These use TEST FIXTURE snapshot files written to temporary folders. They check
that the adapter parses and judges snapshots correctly. They do NOT prove that
a real MT5 terminal is connected; that is checked with `python -m trademate.broker`.
"""

import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from trademate.broker import ConnectionState, create_adapter
from trademate.broker.mt5_adapter import find_common_files_dir
from trademate.config import AppSettings, Mt5Settings, Settings, load_config
from trademate.errors import BrokerUnavailableError, ConfigError

ROOT = Path(__file__).resolve().parent.parent


def fixture_snapshot(**overrides):
    data = {
        "schema": 2,
        "written_at_gmt": 1,
        "bridge": {"name": "TradeMateBridge", "version": "0.1.0", "read_only": False, "trading_enabled": True, "uptime_seconds": 500},
        "terminal": {"connected": True, "name": "MetaTrader 5", "company": "Test Broker", "build": 1},
        "account": {
            "login": 11111111, "server": "Headway-Demo", "company": "Test Broker Ltd", "currency": "USD",
            "leverage": 100, "trade_mode": "demo", "balance": 1000.5, "equity": 1001.25, "margin": 10.0,
            "free_margin": 991.25, "margin_level": 10012.5, "profit": 0.75,
        },
        "positions": [{"ticket": 7, "symbol": "EURUSD", "type": "buy", "volume": 0.01, "price_open": 1.1,
                       "price_current": 1.2, "sl": None, "tp": 1.3, "profit": 0.75, "swap": 0.0}],
        "symbols": [{"name": "EURUSD", "description": "Euro vs US Dollar", "digits": 5, "point": 1e-05,
                     "spread": 12, "contract_size": 100000.0, "volume_min": 0.01, "volume_max": 100.0,
                     "volume_step": 0.01, "stops_level": 10, "freeze_level": 0, "trade_mode": 4, "currency_base": "EUR", "currency_profit": "USD",
                     "bid": 1.1, "ask": 1.10012}],
    }
    data.update(overrides)
    return data


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.file = self.dir / "trademate_snapshot.json"

    def adapter(self, **kwargs):
        return create_adapter(Settings(mt5=Mt5Settings(data_dir=self.dir, **kwargs)))

    def write(self, data, age=0.0):
        self.file.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        if age:
            old = time.time() - age
            os.utime(self.file, (old, old))

    def state(self, **kwargs):
        return self.adapter(**kwargs).get_connection_status().state

    # ---- connection states ----
    def test_missing_configured_folder_is_error(self):
        adapter = create_adapter(Settings(mt5=Mt5Settings(data_dir=self.dir / "nope")))
        self.assertEqual(adapter.get_connection_status().state, ConnectionState.ERROR)

    def test_no_snapshot_file_is_disconnected(self):
        self.assertEqual(self.state(), ConnectionState.DISCONNECTED)

    def test_fresh_snapshot_is_connected(self):
        self.write(fixture_snapshot())
        status = self.adapter().get_connection_status()
        self.assertEqual(status.state, ConnectionState.CONNECTED)
        self.assertLess(status.age_seconds, 5)

    def test_stale_snapshot_is_disconnected_and_data_refused(self):
        self.write(fixture_snapshot(), age=60)
        adapter = self.adapter()
        self.assertEqual(adapter.get_connection_status().state, ConnectionState.DISCONNECTED)
        with self.assertRaises(BrokerUnavailableError):
            adapter.get_account()

    def test_corrupt_snapshot_is_error_not_crash(self):
        self.write("{ this is not json")
        adapter = self.adapter()
        self.assertEqual(adapter.get_connection_status().state, ConnectionState.ERROR)
        with self.assertRaises(BrokerUnavailableError):
            adapter.get_positions()

    def test_wrong_schema_is_error(self):
        self.write(fixture_snapshot(schema=99))
        self.assertEqual(self.state(), ConnectionState.ERROR)

    def test_non_demo_account_is_refused(self):
        snap = fixture_snapshot()
        snap["account"]["trade_mode"] = "real"
        self.write(snap)
        adapter = self.adapter()
        self.assertEqual(adapter.get_connection_status().state, ConnectionState.ERROR)
        with self.assertRaises(BrokerUnavailableError):
            adapter.get_account()

    def test_unknown_environment_is_refused(self):
        snap = fixture_snapshot()
        snap["account"]["trade_mode"] = "unknown"
        self.write(snap)
        self.assertEqual(self.state(), ConnectionState.ERROR)

    def test_terminal_offline_just_started_is_connecting(self):
        snap = fixture_snapshot()
        snap["terminal"]["connected"] = False
        snap["bridge"]["uptime_seconds"] = 5
        self.write(snap)
        self.assertEqual(self.state(), ConnectionState.CONNECTING)

    def test_terminal_offline_for_long_is_disconnected(self):
        snap = fixture_snapshot()
        snap["terminal"]["connected"] = False
        self.write(snap)
        self.assertEqual(self.state(), ConnectionState.DISCONNECTED)

    def test_server_mismatch_warns_but_connects(self):
        self.write(fixture_snapshot())
        status = self.adapter(expected_server="other.server").get_connection_status()
        self.assertEqual(status.state, ConnectionState.CONNECTED)
        self.assertEqual(len(status.warnings), 1)

    # ---- data ----
    def test_account_positions_symbols_prices(self):
        self.write(fixture_snapshot())
        adapter = self.adapter()
        account = adapter.get_account()
        self.assertEqual((account.login, account.currency, account.environment), (11111111, "USD", "demo"))
        self.assertEqual((account.balance, account.equity, account.margin, account.free_margin),
                         (1000.5, 1001.25, 10.0, 991.25))
        positions = adapter.get_positions()
        self.assertEqual((positions[0].symbol, positions[0].direction, positions[0].stop_loss, positions[0].take_profit),
                         ("EURUSD", "buy", None, 1.3))
        self.assertEqual(adapter.get_symbols()[0].spread_points, 12)
        price = adapter.get_symbol_price("eurusd")
        self.assertEqual((price.symbol, price.bid, price.ask), ("EURUSD", 1.1, 1.10012))

    def test_unknown_symbol_raises(self):
        self.write(fixture_snapshot())
        with self.assertRaises(BrokerUnavailableError):
            self.adapter().get_symbol_price("NOPE")

    def test_garbage_values_do_not_crash(self):
        snap = fixture_snapshot()
        snap["account"]["balance"] = "abc"
        snap["account"]["equity"] = float("nan")
        snap["positions"] = ["junk", {"symbol": "X"}, {"ticket": 1, "symbol": "EURUSD", "type": "sell"}]
        snap["symbols"] = [None, {"bid": 1}, {"name": "GBPUSD", "bid": "x"}]
        self.write(snap)
        adapter = self.adapter()
        account = adapter.get_account()
        self.assertIsNone(account.balance)
        self.assertIsNone(account.equity)
        self.assertEqual(len(adapter.get_positions()), 1)
        self.assertEqual([s.name for s in adapter.get_symbols()], ["GBPUSD"])

    # ---- folder detection ----
    def test_autodetects_wine_common_files(self):
        target = self.dir / ".wine/drive_c/users/someone/AppData/Roaming/MetaQuotes/Terminal/Common/Files"
        target.mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if k != "WINEPREFIX"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("pathlib.Path.home", return_value=self.dir):
            found, _ = find_common_files_dir(None)
        self.assertEqual(found, target)

    def test_autodetect_reports_places_searched(self):
        env = {k: v for k, v in os.environ.items() if k != "WINEPREFIX"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("pathlib.Path.home", return_value=self.dir):
            found, searched = find_common_files_dir(None)
        self.assertIsNone(found)
        self.assertTrue(searched)


class ExecutionSafetyTest(unittest.TestCase):
    """Static checks that the write path stays manual and DEMO-only."""

    def ea(self):
        source = (ROOT / "mt5" / "TradeMateBridge.mq5").read_text(encoding="utf-8")
        return re.sub(r"//.*", "", source)

    def test_ea_never_trades_on_its_own(self):
        code = self.ea()
        self.assertNotIn("OnTick", code)         # no per-tick logic
        self.assertNotIn("OnTradeTransaction", code)
        # trade calls exist only inside the command handlers, never in init / timer / snapshot code
        for name in ("OnInit", "OnTimer", "WriteSnapshot", "BuildSnapshot", "ProcessCommands"):
            body = re.search(r"\b" + name + r"\([^)]*\)\s*\n\s*\{(.*?)\n\s{2}\}", code, re.S)
            self.assertIsNotNone(body, name)
            self.assertNotRegex(body.group(1), r"g_trade\.(Buy|Sell|Position)", name)

    def test_ea_refuses_non_demo_and_expired_commands(self):
        code = self.ea()
        self.assertIn("ACCOUNT_TRADE_MODE_DEMO", code)
        self.assertIn("REFUSED: this is not a DEMO account", code)
        self.assertIn("Command expired", code)
        self.assertIn("AllowTrading", code)

    def test_adapter_guard_blocks_live_mode_and_disabled(self):
        from trademate.config import TradingSettings
        from trademate.errors import OrderRejectedError
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "trademate_snapshot.json").write_text(json.dumps(fixture_snapshot()))
        live = create_adapter(Settings(mode=__import__("trademate.config", fromlist=["Mode"]).Mode.LIVE,
                                       mt5=Mt5Settings(data_dir=Path(tmp.name)), app=AppSettings(data_dir=Path(tmp.name))))
        with self.assertRaises(OrderRejectedError):
            live.place_order("EURUSD", "buy", 0.01)
        off = create_adapter(Settings(app=AppSettings(data_dir=Path(tmp.name)), mt5=Mt5Settings(data_dir=Path(tmp.name)), trading=TradingSettings(enabled=False)))
        with self.assertRaises(OrderRejectedError):
            off.place_order("EURUSD", "buy", 0.01)

    def test_no_credentials_in_config_or_code(self):
        text = (ROOT / "config" / "settings.toml").read_text(encoding="utf-8").lower()
        self.assertNotIn("password", re.sub(r"#.*", "", text))


class ConfigTest(unittest.TestCase):
    def test_bad_stale_value_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "s.toml"
            cfg.write_text("[mt5]\nstale_after_seconds = 0\n")
            with self.assertRaises(ConfigError):
                load_config(cfg)

    def test_defaults_load(self):
        mt5 = load_config().mt5
        self.assertEqual(mt5.snapshot_file, "trademate_snapshot.json")
        self.assertIsNone(mt5.data_dir)


if __name__ == "__main__":
    unittest.main()
