"""Candle Momentum end to end: engine -> Autopilot -> Risk Engine -> pipeline -> MT5 adapter -> (fake) bridge, plus the Watchdog and the HTTP API.

    python3 -m unittest tests.test_candle_momentum_e2e -v

SAFETY: every order goes to tests/fake_bridge.py (an in-memory stand-in for the MT5 Expert Advisor) through the real adapter. No MT5, no network, no real order is
possible. These tests prove the wiring and the rules in SIMULATION; they do NOT prove behaviour against a live broker.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.fake_bridge import FakeBridge
from trademate import candle_momentum as cm_pkg
from trademate.autopilot import Autopilot
from trademate.broker import create_adapter
from trademate.candle_momentum.diagnostics import EventLog
from trademate.candle_momentum.engine import CandleMomentumEngine
from trademate.candle_momentum.feed import AdapterFeed
from trademate.candle_momentum.store import ConfigStore, StateStore
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.decision_hub import DecisionHub
from trademate.pipeline import ApprovalStore
from trademate.risk import RiskRules, RiskRulesStore
from trademate.selection_store import SelectionStore
from trademate.setups_ledger import SetupLedger
from trademate.strategies.base import EXEC_TIMEFRAMES
from trademate.strategies.selection import StrategySelectionStore
from trademate.strategy.watchdog import WatchdogManager

PIP = 0.0001


class E2E(unittest.TestCase):
    LOT = 0.01

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.bridge = FakeBridge(tmp).start()
        self.addCleanup(self.bridge.stop)
        self.settings = Settings(app=AppSettings(data_dir=tmp), mt5=Mt5Settings(data_dir=tmp), trading=TradingSettings(command_timeout_seconds=1.0, confirm_timeout_seconds=0.6))
        self.adapter = create_adapter(self.settings)
        now = int(time.time())
        self.t5 = now // 300 * 300
        self.bridge.scripted_candles[("EURUSD", "M5")] = [[self.t5 - 600, 1.0990, 1.1002, 1.0988, 1.1000], [self.t5 - 300, 1.1000, 1.1004, 1.0996, 1.1000],
                                                          [self.t5, 1.1000, 1.1000, 1.1000, 1.1000]]
        self.hub = DecisionHub()
        self.ledger = SetupLedger(tmp / "ledger.json")
        self.engine = CandleMomentumEngine(AdapterFeed(self.adapter), self.settings, ConfigStore(None, EXEC_TIMEFRAMES), StateStore(None), EventLog(None), hub=self.hub, threaded=False)
        cm_pkg.set_engine(self.engine)
        self.addCleanup(cm_pkg.set_engine, None)
        strategy_store = StrategySelectionStore(tmp / "strategy_selection.json", "candle_momentum")
        self.store = ApprovalStore(tmp / "pipeline_log.jsonl")
        for p in (mock.patch.object(app_module, "adapter", self.adapter), mock.patch.object(app_module, "settings", self.settings),
                  mock.patch.object(app_module, "pipeline_store", self.store), mock.patch.object(app_module, "risk_store", RiskRulesStore(tmp / "risk.json", RiskRules(1.0, 3.0, 3.0, 3))),
                  mock.patch.object(app_module, "strategy_store", strategy_store), mock.patch.object(app_module, "selection_store", SelectionStore(tmp / "sel.json", "M5")),
                  mock.patch.object(app_module, "setup_ledger", self.ledger)):
            p.start()
            self.addCleanup(p.stop)
        self.lot = self.LOT
        self.autopilot = Autopilot(tmp / "autopilot.json", selection=lambda: ("EURUSD", "candle_momentum", "M5"), hub_get=self.hub.get, lot_for=lambda s: self.lot,
                                   propose=app_module._autopilot_propose, execute=app_module._autopilot_execute, positions=lambda: {p.ticket for p in self.adapter.get_positions()},
                                   history=lambda: [], observer=lambda ev: self.engine.report_execution(ev) if ev.get("strategy_id") == "candle_momentum" else None)
        self.engine.on_signal = lambda *a: self.autopilot.wake()
        self.engine.release_hook = lambda cid: (self.ledger.release(cid), self.autopilot.forget_attempt(cid))
        self.price(1.10000, 1.10012)
        ok, msg = self.engine.start("EURUSD", "M5")
        self.assertTrue(ok, msg)
        self.engine.step()
        ok, msg = self.autopilot.start("runs", 5, spawn=False)
        self.assertTrue(ok, msg)

    def price(self, bid, ask):
        self.bridge.bid, self.bridge.ask = bid, ask
        self.bridge.write_snapshot()

    def lc(self):
        return self.engine._slots["EURUSD|M5"].machine.lc

    def opens(self):
        return [c for c in self.bridge.commands_seen if c["cmd"] == "open"]

    def signal(self, bid=1.10100):
        self.price(bid, bid + 0.00012)
        self.engine.step()

    # ------------------------------------------------------------------------------------------------------------------
    def test_26_successful_simulated_execution_end_to_end(self):
        self.assertEqual(self.lc()["state"], "MONITORING")
        self.signal(1.10100)
        self.assertEqual(self.lc()["state"], "RISK_PENDING")
        self.assertEqual(self.hub.get("EURUSD", "candle_momentum", "M5")["decision"], "BUY")
        self.assertTrue(self.autopilot._wake.is_set())                # the engine woke the Autopilot itself; nobody polled
        self.autopilot.tick()
        self.assertEqual(len(self.opens()), 1)
        pos = list(self.bridge.positions.values())[0]
        fill = pos["price_open"]
        lc = self.lc()
        self.assertEqual(lc["state"], "POSITION_OPEN")
        self.assertEqual(lc["ticket"], list(self.bridge.positions)[0])
        self.assertAlmostEqual(lc["entry_price"], fill, places=6)
        self.assertAlmostEqual(lc["target_price"], fill + 10 * PIP, places=6)      # from the ACTUAL fill
        self.assertAlmostEqual(pos["tp"], fill + 10 * PIP, places=5)               # and that is the take-profit the broker holds
        self.assertAlmostEqual(pos["sl"], 1.0999, places=5)                         # the existing protective stop: 1 pip beyond the candle open
        self.assertEqual(pos["volume"], 0.01)
        self.assertEqual(lc["risk_decision"]["status"], "approved")
        self.assertNotEqual(self.hub.get("EURUSD", "candle_momentum", "M5")["decision"], "BUY")   # the executed signal is no longer an entry signal
        for _ in range(5):                                            # more ticks, more Autopilot cycles: still ONE order
            self.signal(1.10110)
            self.autopilot.tick()
        self.assertEqual(len(self.opens()), 1)

    def test_target_reached_then_closed_through_the_real_adapter(self):
        self.signal(1.10100)
        self.autopilot.tick()
        tp = self.lc()["target_price"]
        self.price(tp, tp + 0.00012)
        self.engine.step()
        self.assertEqual(self.lc()["state"], "TARGET_REACHED")
        ticket = self.lc()["ticket"]
        self.adapter.close_position(ticket)                           # the broker's take-profit closing it (simulated)
        self.engine.step(time.time() + 2)
        self.assertEqual(self.lc()["state"], "POSITION_CLOSED")
        self.assertIsNotNone(self.lc()["closed"])

    def test_risk_engine_rejection_sends_nothing(self):
        self.lot = 1.0                                                # inside the per-order volume cap, but ~12 pips x $10 x 1 lot of risk is above the 1% limit
        self.signal(1.10100)
        self.autopilot.tick()
        self.assertEqual(self.opens(), [])
        self.assertEqual(self.lc()["state"], "REJECTED")
        self.assertEqual(self.lc()["risk_decision"]["status"], "rejected")
        self.assertIn("Risk", self.lc()["reason"] + self.lc()["risk_decision"]["message"])

    def test_invalid_lot_size_sends_nothing(self):
        self.lot = 0.005                                              # below the broker minimum of 0.01
        self.signal(1.10100)
        self.autopilot.tick()
        self.assertEqual(self.opens(), [])
        self.assertEqual(self.lc()["state"], "REJECTED")

    def test_price_runs_away_between_signal_and_order_is_missed_not_chased(self):
        self.signal(1.10100)
        self.price(1.10100 + 6 * PIP, 1.10112 + 6 * PIP)              # +6 pips before the executor looks (no engine step in between)
        self.autopilot.tick()
        self.assertEqual(self.opens(), [])
        self.assertEqual(self.lc()["state"], "MISSED")

    def test_explicit_slippage_tolerance_replaces_the_generic_guard(self):
        self.engine.cfg_store.save(__import__("trademate.candle_momentum.models", fromlist=["CMConfig"]).CMConfig("EURUSD", "M5", max_slippage_units=0.5))
        self.engine.reload_config("EURUSD", "M5")
        self.signal(1.10100)
        self.price(1.10100 + 1 * PIP, 1.10112 + 1 * PIP)              # 1 pip worse > the 0.5-pip tolerance
        self.autopilot.tick()
        self.assertEqual(self.opens(), [])
        self.assertEqual(self.lc()["state"], "MISSED")
        self.assertIn("slippage", self.lc()["risk_decision"]["message"].lower())

    def test_stale_snapshot_blocks_everything(self):
        self.bridge.stop()                                            # the bridge stops writing; the snapshot ages
        time.sleep(0.05)
        self.engine.step(time.time() + 60)
        self.assertNotEqual(self.hub.get("EURUSD", "candle_momentum", "M5")["state"], "fresh")
        self.assertEqual(self.opens(), [])

    def test_manual_analyse_uses_the_same_rules_and_does_not_disturb_the_engine(self):
        self.signal(1.10100)
        before = self.lc()["candle_id"], self.lc()["state"]
        res = app_module._strategy_for_pipeline("EURUSD")
        self.assertEqual(res["result"]["decision"], "BUY")
        self.assertTrue(res["result"]["candle_momentum"]["monitoring"])
        self.assertEqual((self.lc()["candle_id"], self.lc()["state"]), before)


class ThreadedFullStackTest(unittest.TestCase):
    """Everything on its REAL threads: the engine worker (background candle fetches), the Watchdog, the Autopilot loop (poll interval 30 s, so only the engine's
    immediate wake-up can make the trade happen within seconds), the real Flask routes, the real adapter, the Risk Engine and the fake bridge. Proves the hand-off is
    event-driven and that nothing needs a click or a browser."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.bridge = FakeBridge(tmp).start()
        self.addCleanup(self.bridge.stop)
        from trademate.config import CandleMomentumSettings
        self.settings = Settings(app=AppSettings(data_dir=tmp), mt5=Mt5Settings(data_dir=tmp), trading=TradingSettings(command_timeout_seconds=1.0, confirm_timeout_seconds=0.6),
                                 candle_momentum=CandleMomentumSettings(poll_seconds=0.05))
        self.adapter = create_adapter(self.settings)
        t5 = int(time.time()) // 300 * 300
        self.bridge.scripted_candles[("EURUSD", "M5")] = [[t5 - 600, 1.0990, 1.1002, 1.0988, 1.1000], [t5 - 300, 1.1000, 1.1004, 1.0996, 1.1000], [t5, 1.1000, 1.1000, 1.1000, 1.1000]]
        self.hub = DecisionHub()
        self.ledger = SetupLedger(tmp / "ledger.json")
        self.engine = CandleMomentumEngine(AdapterFeed(self.adapter), self.settings, ConfigStore(None, EXEC_TIMEFRAMES), StateStore(tmp / "cm_state.json"), EventLog(None),
                                           hub=self.hub, async_fetch=True)
        cm_pkg.set_engine(self.engine)
        self.addCleanup(cm_pkg.set_engine, None)
        strategy_store = StrategySelectionStore(tmp / "strategy_selection.json", "candle_momentum")
        self.wd = WatchdogManager(lambda *a, **k: (_ for _ in ()).throw(AssertionError("polling evaluator must not run")), hub=self.hub, engine=self.engine)
        self.autopilot = Autopilot(tmp / "autopilot.json", selection=lambda: ("EURUSD", "candle_momentum", "M5"), hub_get=self.hub.get, lot_for=lambda s: 0.01,
                                   propose=app_module._autopilot_propose, execute=app_module._autopilot_execute, positions=lambda: {p.ticket for p in self.adapter.get_positions()},
                                   history=lambda: [], poll_seconds=30.0, observer=lambda ev: self.engine.report_execution(ev) if ev.get("strategy_id") == "candle_momentum" else None)
        self.engine.on_signal = lambda *a: self.autopilot.wake()
        self.addCleanup(self.autopilot.shutdown)
        self.addCleanup(self.wd.stop)
        self.addCleanup(self.engine.stop_all)
        for p in (mock.patch.object(app_module, "adapter", self.adapter), mock.patch.object(app_module, "settings", self.settings),
                  mock.patch.object(app_module, "pipeline_store", ApprovalStore(tmp / "pl.jsonl")),
                  mock.patch.object(app_module, "risk_store", RiskRulesStore(tmp / "risk.json", RiskRules(1.0, 3.0, 3.0, 3))),
                  mock.patch.object(app_module, "strategy_store", strategy_store), mock.patch.object(app_module, "selection_store", SelectionStore(tmp / "sel.json", "M5")),
                  mock.patch.object(app_module, "setup_ledger", self.ledger), mock.patch.object(app_module, "candle_engine", self.engine),
                  mock.patch.object(app_module, "watchdog", self.wd), mock.patch.object(app_module, "decision_hub", self.hub), mock.patch.object(app_module, "autopilot", self.autopilot)):
            p.start()
            self.addCleanup(p.stop)
        self.client = app.test_client()

    def price(self, bid):
        self.bridge.bid, self.bridge.ask = bid, round(bid + 0.00012, 5)
        self.bridge.write_snapshot()

    def until(self, cond, seconds=6.0):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def lifecycle(self):
        return (self.engine.snapshot("EURUSD", "M5").get("machine") or {}).get("current") or {}

    def test_signal_becomes_a_position_within_seconds_with_no_click_and_no_polling(self):
        self.price(1.10000)
        r = self.client.post("/api/strategylab/watchdog/start", json={"symbol": "EURUSD"})
        self.assertEqual(r.status_code, 202, r.get_json())
        self.assertEqual(r.get_json()["watchdog"]["mode"], "tick_engine")
        ok, msg = self.autopilot.start("runs", 1, spawn=True)
        self.assertTrue(ok, msg)
        self.assertTrue(self.until(lambda: self.lifecycle().get("state") == "MONITORING"), self.lifecycle())
        self.assertEqual(list(self.bridge.positions), [])
        t0 = time.time()
        self.price(1.10100)                                           # the trigger is crossed; NOBODY clicks Analyse and Autopilot only polls every 30 s
        self.assertTrue(self.until(lambda: len(self.bridge.positions) == 1), f"no position; lifecycle={self.lifecycle().get('state')} reason={self.lifecycle().get('reason')}")
        elapsed = time.time() - t0
        print(f"\n    [info] trigger crossed -> broker position in {elapsed:.2f}s (simulated bridge; includes the fake bridge's own 0.25 s snapshot cadence)")
        self.assertLess(elapsed, 5.0)
        self.assertTrue(self.until(lambda: self.lifecycle().get("state") in ("POSITION_OPEN", "TARGET_REACHED")))
        self.assertEqual(len([c for c in self.bridge.commands_seen if c["cmd"] == "open"]), 1)
        time.sleep(0.6)                                               # more ticks, more loops: still ONE order
        self.price(1.10110)
        time.sleep(0.6)
        self.assertEqual(len([c for c in self.bridge.commands_seen if c["cmd"] == "open"]), 1)
        st = self.client.get("/api/candle-momentum/state?symbol=EURUSD&timeframe=M5").get_json()
        self.assertTrue(st["watchdog"]["active"])
        self.assertEqual(st["machine"]["current"]["state"], "POSITION_OPEN")
        self.assertEqual(st["machine"]["current"]["ticket"], list(self.bridge.positions)[0])
        dec = self.client.get("/api/decision/current?symbol=EURUSD&strategy=candle_momentum&timeframe=M5").get_json()
        self.assertTrue(dec["monitoring"])
        self.assertNotEqual(dec.get("decision"), "BUY")
        self.assertEqual(self.wd.status()["interval_seconds"], 0.05)


class WatchdogIntegrationTests(unittest.TestCase):
    def setUp(self):
        from tests.fake_mt5_bridge import FakeClock, FakeFeed
        self.clock = time.time
        self.feed = FakeFeed(self.clock)
        self.hub = DecisionHub()
        self.engine = CandleMomentumEngine(self.feed, Settings(), ConfigStore(None, EXEC_TIMEFRAMES), StateStore(None), EventLog(None), hub=self.hub, threaded=False)
        calls = []
        self.calls = calls
        self.wd = WatchdogManager(lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("the polling evaluator must never run for Candle Momentum")),
                                  hub=self.hub, engine=self.engine)
        self.addCleanup(self.wd.stop)

    def test_watchdog_monitors_through_the_engine_not_the_polling_loop(self):
        ok, msg = self.wd.start("EURUSD", "candle_momentum", "M5")
        self.assertTrue(ok, msg)
        self.assertTrue(self.wd.start("EURUSD", "candle_momentum", "M5")[0])      # duplicate start: still one engine slot
        self.assertEqual(len(self.engine._slots), 1)
        self.feed._candle("EURUSD", "M5")
        self.engine.step()
        self.feed.tick(1.10105)
        deadline = time.time() + 3
        while time.time() < deadline and not any(e.get("decision") == "BUY" for e in self.wd.status().get("events", [])):
            self.engine.step()
            time.sleep(0.05)
        st = self.wd.status()
        self.assertEqual(st["mode"], "tick_engine")
        self.assertEqual(st["interval_seconds"], 0.25)                # the REAL monitoring cadence, not a 60 s poll
        self.assertEqual(self.calls, [])
        buys = [e for e in st["events"] if e.get("decision") == "BUY"]
        self.assertEqual(len(buys), 1)                                # one notification for the one signal
        rec = self.hub.get("EURUSD", "candle_momentum", "M5")
        self.assertEqual(rec["decision"], "BUY")                      # the notification and the decision panel can never disagree: one record
        self.assertEqual(rec["result"]["candle_momentum"]["lifecycle"]["candle_id"], self.engine._slots["EURUSD|M5"].machine.lc["candle_id"])

    def test_stop_releases_the_engine(self):
        self.wd.start("EURUSD", "candle_momentum", "M5")
        self.assertIn("EURUSD|M5", self.engine._slots)
        self.wd.stop()
        self.assertNotIn("EURUSD|M5", self.engine._slots)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.client = app.test_client()
        cfg_store = ConfigStore(tmp / "cfg.json", EXEC_TIMEFRAMES)
        engine = CandleMomentumEngine(mock.MagicMock(), Settings(), cfg_store, StateStore(None), EventLog(None), hub=None, threaded=False)
        for p in (mock.patch.object(app_module, "cm_config_store", cfg_store), mock.patch.object(app_module, "candle_engine", engine)):
            p.start()
            self.addCleanup(p.stop)

    def test_config_roundtrip_and_validation(self):
        r = self.client.get("/api/candle-momentum/config?symbol=EURUSD&timeframe=M15").get_json()
        self.assertEqual((r["config"]["trigger_units"], r["customised"]), (10.0, False))
        r = self.client.post("/api/candle-momentum/config", json={"symbol": "EURUSD", "timeframe": "M15", "trigger_units": 5, "target_units": 12, "sl_buffer_units": 2})
        self.assertTrue(r.get_json()["ok"], r.get_json())
        r = self.client.get("/api/candle-momentum/config?symbol=EURUSD&timeframe=M15").get_json()
        self.assertEqual((r["config"]["trigger_units"], r["config"]["target_units"], r["customised"]), (5.0, 12.0, True))
        self.assertEqual(self.client.get("/api/candle-momentum/config?symbol=EURUSD&timeframe=M5").get_json()["config"]["trigger_units"], 10.0)    # independent per timeframe
        for bad in ({"trigger_units": 0}, {"trigger_units": "abc"}, {"sl_buffer_units": -1}, {"max_entry_attempts": 0}, {"max_entry_attempts": 99}):
            resp = self.client.post("/api/candle-momentum/config", json={"symbol": "EURUSD", "timeframe": "M5", **bad})
            self.assertEqual(resp.status_code, 400, bad)
            self.assertFalse(resp.get_json()["ok"])

    def test_invalid_symbol_and_timeframe_are_refused(self):
        self.assertEqual(self.client.get("/api/candle-momentum/state?symbol=" + "X" * 40 + "&timeframe=M5").status_code, 400)
        self.assertEqual(self.client.get("/api/candle-momentum/state?symbol=EURUSD&timeframe=M7").status_code, 400)
        self.assertEqual(self.client.post("/api/candle-momentum/config", json={"symbol": "", "timeframe": "M5"}).status_code, 400)

    def test_browser_refresh_state_is_read_only_and_repeatable(self):
        a = self.client.get("/api/candle-momentum/state?symbol=EURUSD&timeframe=M5").get_json()
        b = self.client.get("/api/candle-momentum/state?symbol=EURUSD&timeframe=M5").get_json()
        self.assertTrue(a["ok"])
        self.assertEqual(a["monitoring"], b["monitoring"])
        self.assertEqual(a["engine"]["cadence_seconds"], 0.25)
        self.assertIn("watchdog", a)
        self.assertIn("autopilot", a)

    def test_events_endpoint(self):
        r = self.client.get("/api/candle-momentum/events?limit=5").get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(self.client.get("/api/candle-momentum/events?after_id=x").status_code, 400)


if __name__ == "__main__":
    unittest.main()
