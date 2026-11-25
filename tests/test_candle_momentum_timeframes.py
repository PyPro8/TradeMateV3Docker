"""Candle Momentum: the USER chooses M1 or M5 and the choice drives data retrieval, analysis, signal, Risk Engine, order and tracking.

    python3 -m unittest tests.test_candle_momentum_timeframes -v

SAFETY: every order goes to tests/fake_bridge.py (an in-memory stand-in for the MT5 Expert Advisor) through the real adapter. No MT5, no network, no real order.
These tests prove the wiring in SIMULATION; they do NOT prove behaviour against a live broker. They do not change or assess Candle Momentum's signal logic:
they use the same LEGACY configuration and the same engine as tests/test_candle_momentum_e2e.py and only vary the timeframe.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.fake_bridge import FakeBridge
from trademate import candle_momentum as cm_pkg
from trademate.autopilot import Autopilot
from trademate.broker import create_adapter
from trademate.candle_momentum import logic
from trademate.candle_momentum.diagnostics import EventLog
from trademate.candle_momentum.engine import CandleMomentumEngine
from trademate.candle_momentum.feed import AdapterFeed
from trademate.candle_momentum.models import CMConfig
from trademate.candle_momentum.store import ConfigStore, StateStore
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.decision_hub import DecisionHub
from trademate.pipeline import ApprovalStore
from trademate.risk import RiskRules, RiskRulesStore
from trademate.selection_store import SelectionStore
from trademate.setups_ledger import SetupLedger
from trademate.strategies.base import EXEC_TIMEFRAMES
from trademate.strategies.registry import STRATEGY_IDS
from trademate.strategies.selection import StrategySelectionStore

PIP = 0.0001
SECONDS = {"M1": 60, "M2": 120, "M5": 300}


class TfRig:
    """Real engine -> Autopilot -> pipeline -> Risk Engine -> adapter -> fake bridge, for whichever timeframe the test selects. `selected` plays the user's choice."""

    def build(self, selected: str, tfs=("M1", "M5")):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.bridge = FakeBridge(tmp).start()
        self.addCleanup(self.bridge.stop)
        self.settings = Settings(app=AppSettings(data_dir=tmp), mt5=Mt5Settings(data_dir=tmp), trading=TradingSettings(command_timeout_seconds=1.0, confirm_timeout_seconds=0.6))
        self.adapter = create_adapter(self.settings)
        now = int(time.time())
        self.t = {tf: now // SECONDS[tf] * SECONDS[tf] for tf in SECONDS}
        for tf in tfs:
            n = SECONDS[tf]
            self.bridge.scripted_candles[("EURUSD", tf)] = [[self.t[tf] - 2 * n, 1.0990, 1.1002, 1.0988, 1.1000], [self.t[tf] - n, 1.1000, 1.1004, 1.0996, 1.1000],
                                                           [self.t[tf], 1.1000, 1.1000, 1.1000, 1.1000]]
        self.hub = DecisionHub()
        self.ledger = SetupLedger(tmp / "ledger.json")
        self.feed_timeframes = []
        feed = AdapterFeed(self.adapter)
        for name in ("candle",):
            orig = getattr(feed, name)
            setattr(feed, name, (lambda o: lambda symbol, timeframe, *a, **k: (self.feed_timeframes.append(timeframe), o(symbol, timeframe, *a, **k))[1])(orig))
        self.engine = CandleMomentumEngine(feed, self.settings, ConfigStore(None, EXEC_TIMEFRAMES), StateStore(None), EventLog(None), hub=self.hub, threaded=False)
        for tf in tfs:
            self.engine.cfg_store.save(CMConfig("EURUSD", tf))
        cm_pkg.set_engine(self.engine)
        self.addCleanup(cm_pkg.set_engine, None)
        self.selection = SelectionStore(tmp / "sel.json", "M5")
        self.selection.save(symbol="EURUSD", strategy_id="candle_momentum", timeframe=selected)       # the USER's choice
        strategy_store = StrategySelectionStore(tmp / "strategy_selection.json", "candle_momentum")
        self.store = ApprovalStore(tmp / "pipeline_log.jsonl")
        for p in (mock.patch.object(app_module, "adapter", self.adapter), mock.patch.object(app_module, "settings", self.settings),
                  mock.patch.object(app_module, "pipeline_store", self.store), mock.patch.object(app_module, "risk_store", RiskRulesStore(tmp / "risk.json", RiskRules(1.0, 3.0, 3.0, 3))),
                  mock.patch.object(app_module, "strategy_store", strategy_store), mock.patch.object(app_module, "selection_store", self.selection),
                  mock.patch.object(app_module, "setup_ledger", self.ledger)):
            p.start()
            self.addCleanup(p.stop)
        self.autopilot = Autopilot(tmp / "autopilot.json", selection=lambda: ("EURUSD", "candle_momentum", self.selection.timeframe("candle_momentum")), hub_get=self.hub.get,
                                   lot_for=lambda s: 0.01, propose=app_module._autopilot_propose, execute=app_module._autopilot_execute,
                                   positions=lambda: {p.ticket for p in self.adapter.get_positions()}, history=lambda: [],
                                   observer=lambda ev: self.engine.report_execution(ev) if ev.get("strategy_id") == "candle_momentum" else None)
        self.engine.on_signal = lambda *a: self.autopilot.wake()
        self.engine.release_hook = lambda cid: (self.ledger.release(cid), self.autopilot.forget_attempt(cid))
        self.price(1.10000, 1.10012)

    def price(self, bid, ask):
        self.bridge.bid, self.bridge.ask = bid, ask
        self.bridge.write_snapshot()

    def lc(self, tf):
        return self.engine._slots[f"EURUSD|{tf}"].machine.lc

    def opens(self):
        return [c for c in self.bridge.commands_seen if c["cmd"] == "open"]

    def candle_requests(self):
        """Timeframes the STRATEGY's feed asked the broker for (the clock-offset probe in brokertime.py is not strategy data)."""
        return list(self.feed_timeframes)

    def signal(self, bid=1.10100):
        self.price(bid, bid + 0.00012)
        self.engine.step()


class SelectedTimeframeExecutesTest(TfRig, unittest.TestCase):
    """Each of M1 and M5, selected by the user, runs the whole pipeline on that timeframe and ONLY that timeframe."""

    def run_selected(self, tf, other):
        self.build(tf, tfs=(tf,))                                                              # only the selected timeframe has scripted candles: any fallback to `other` would fail loudly
        ok, msg = self.engine.start("EURUSD", self.selection.timeframe("candle_momentum"))
        self.assertTrue(ok, msg)
        self.engine.step()
        ok, msg = self.autopilot.start("runs", 5, spawn=False)
        self.assertTrue(ok, msg)
        self.assertEqual(self.lc(tf)["timeframe"], tf)
        self.assertEqual(self.lc(tf)["state"], "MONITORING")
        self.signal(1.10100)
        self.assertEqual(self.lc(tf)["state"], "RISK_PENDING")
        self.assertEqual(self.hub.get("EURUSD", "candle_momentum", tf)["decision"], "BUY")
        self.assertIsNone(self.hub.get("EURUSD", "candle_momentum", other))                    # nothing was ever published for the other timeframe
        self.autopilot.tick()
        self.assertEqual(len(self.opens()), 1)
        self.assertEqual(self.lc(tf)["state"], "POSITION_OPEN")
        self.assertEqual(self.lc(tf)["risk_decision"]["status"], "approved")                   # the Risk Engine approved it
        self.assertEqual(self.autopilot.status()["exec_tf"], tf)                               # Autopilot traded the selected timeframe
        self.assertEqual(set(self.candle_requests()), {tf})                                    # data was retrieved for the selected timeframe only
        self.assertNotIn(f"EURUSD|{other}", self.engine._slots)
        for _ in range(4):                                                                     # more ticks and Autopilot cycles: still one order
            self.signal(1.10110)
            self.autopilot.tick()
        self.assertEqual(len(self.opens()), 1)

    def test_m1_selection_analyses_signals_and_executes_on_m1(self):
        self.run_selected("M1", "M5")

    def test_m5_selection_analyses_signals_and_executes_on_m5(self):
        self.run_selected("M5", "M1")

    def test_m2_selection_analyses_signals_and_executes_on_m2(self):
        self.run_selected("M2", "M1")

    def test_signal_is_judged_on_the_selected_timeframes_candle_not_the_other(self):
        """M1 and M5 have different forming candles (different opens). A tick that is bullish on one and flat on the other must only trigger the selected one."""
        self.build("M1", tfs=("M1", "M5"))
        self.bridge.scripted_candles[("EURUSD", "M5")][-1] = [self.t["M5"], 1.1010, 1.1010, 1.1010, 1.1010]    # M5 candle opened at 1.1010: a price of 1.1010 is NOT bullish on M5
        ok, msg = self.engine.start("EURUSD", "M1")
        self.assertTrue(ok, msg)
        self.engine.step()
        ok, msg = self.autopilot.start("runs", 5, spawn=False)
        self.assertTrue(ok, msg)
        self.signal(1.10100)
        self.assertEqual(self.lc("M1")["state"], "RISK_PENDING")
        self.autopilot.tick()
        self.assertEqual(len(self.opens()), 1)
        self.assertEqual(self.lc("M1")["timeframe"], "M1")


class BaselineBehaviourCharacterisationTest(TfRig, unittest.TestCase):
    """Characterises (does NOT change) the original behaviour behind the 'premature reversal' report: Candle Momentum judges the FORMING candle against its OWN open only."""

    def test_a_bearish_forming_candle_signals_sell_even_inside_a_run_of_bullish_candles(self):
        self.build("M5", tfs=("M5",))
        n = 300
        self.bridge.scripted_candles[("EURUSD", "M5")] = [[self.t["M5"] - 3 * n, 1.0980, 1.0996, 1.0978, 1.0994], [self.t["M5"] - 2 * n, 1.0994, 1.1004, 1.0992, 1.1002],
                                                           [self.t["M5"] - n, 1.1002, 1.1012, 1.1000, 1.1010], [self.t["M5"], 1.1010, 1.1010, 1.1010, 1.1010]]          # three bullish candles, then a new one
        self.price(1.10100, 1.10112)                                                      # the new candle opens at 1.1010: price equals its open (neutral)
        ok, msg = self.engine.start("EURUSD", "M5")
        self.assertTrue(ok, msg)
        self.engine.step()
        self.price(1.10000, 1.10012)                                                      # the new candle falls 10 pips below ITS open: a bearish forming candle
        self.engine.step()
        lc = self.lc("M5")
        self.assertEqual(lc["state"], "RISK_PENDING")
        self.assertEqual(self.hub.get("EURUSD", "candle_momentum", "M5")["decision"], "SELL")        # the preceding bullish candles are not an input: documented in docs/CANDLE_MOMENTUM.md

    def test_the_logic_module_has_no_input_from_previous_candles(self):
        import inspect
        from trademate.candle_momentum import logic
        sig = inspect.signature(logic.validate_entry)
        self.assertEqual(list(sig.parameters), ["direction", "trigger", "open_price", "quote", "cfg", "spec", "now"])       # open of the forming candle, trigger, quote: no history


class SwitchingAndSeparationTest(TfRig, unittest.TestCase):
    def test_switching_m1_to_m5_without_restart_keeps_each_timeframes_state_separate(self):
        self.build("M1")
        ok, msg = self.engine.start("EURUSD", "M1")
        self.assertTrue(ok, msg)
        self.engine.step()
        ok, msg = self.autopilot.start("runs", 5, spawn=False)
        self.assertTrue(ok, msg)
        self.signal(1.10100)
        self.autopilot.tick()
        self.assertEqual(len(self.opens()), 1)
        m1_candle = self.lc("M1")["candle_id"]
        self.assertEqual(self.lc("M1")["state"], "POSITION_OPEN")
        # The user switches to M5 (no restart): the engine starts an M5 slot; the M1 slot's lifecycle is untouched.
        self.selection.save(strategy_id="candle_momentum", timeframe="M5")
        self.assertEqual(self.selection.timeframe("candle_momentum"), "M5")
        ok, msg = self.engine.start("EURUSD", "M5")
        self.assertTrue(ok, msg)
        self.price(1.10000, 1.10012)
        self.engine.step()
        self.assertEqual(self.lc("M1")["candle_id"], m1_candle)
        self.assertEqual(self.lc("M1")["state"], "POSITION_OPEN")                              # not corrupted by the switch
        self.assertEqual(self.lc("M5")["timeframe"], "M5")
        self.assertNotEqual(self.lc("M5")["candle_id"], m1_candle)
        self.assertEqual(self.lc("M5")["state"], "MONITORING")
        self.autopilot.tick()                                                                  # Autopilot keeps managing the open M1 trade and opens nothing new
        self.assertEqual(len(self.opens()), 1)
        self.assertEqual(self.autopilot.status()["exec_tf"], "M1")

    def test_autopilot_started_on_m1_does_not_silently_trade_m5_after_a_switch(self):
        self.build("M1")
        for tf in ("M1", "M5"):
            ok, msg = self.engine.start("EURUSD", tf)
            self.assertTrue(ok, msg)
        self.engine.step()
        ok, msg = self.autopilot.start("runs", 5, spawn=False)
        self.assertTrue(ok, msg)
        self.selection.save(strategy_id="candle_momentum", timeframe="M5")                      # the user changes timeframe while Autopilot is armed on M1
        self.signal(1.10100)
        self.autopilot.tick()
        self.assertEqual(len(self.opens()), 0)                                                 # it will not trade the new selection
        self.assertIn("changed", self.autopilot.status()["message"])
        self.assertEqual(self.autopilot.status()["exec_tf"], "M1")

    def test_switching_back_and_forth_always_reads_the_users_choice(self):
        self.build("M5")
        for tf in ("M1", "M5", "M1", "M5", "M5", "M1"):
            self.selection.save(strategy_id="candle_momentum", timeframe=tf)
            self.assertEqual(self.selection.timeframe("candle_momentum"), tf)

    def test_selection_is_per_strategy_and_survives_a_restart(self):
        self.build("M1")
        self.selection.save(strategy_id="choch", timeframe="M15")
        reread = SelectionStore(self.selection.path, "M5")                                    # a fresh process reading the same file
        self.assertEqual(reread.timeframe("candle_momentum"), "M1")                           # M1 is remembered; the default M5 is NOT silently applied
        self.assertEqual(reread.timeframe("choch"), "M15")

    def test_candle_ids_and_duplicate_protection_are_separated_by_timeframe(self):
        a = logic.make_candle_id("EURUSD", "M1", 1_700_000_000, 1.1)
        b = logic.make_candle_id("EURUSD", "M5", 1_700_000_000, 1.1)
        self.assertNotEqual(a, b)                                                             # same open time and price: a different timeframe is a different candle
        self.assertEqual(a, logic.make_candle_id("EURUSD", "M1", 1_700_000_000, 1.1))
        self.build("M1")
        self.assertEqual(len(self.ledger._d if hasattr(self.ledger, "_d") else []), 0)
        # Both timeframes may each take their own one entry for the same instant: the engine's config keys are independent.
        self.assertNotEqual(f"EURUSD|M1", f"EURUSD|M5")

    def test_one_entry_per_candle_per_timeframe_when_both_are_running(self):
        """Running both slots (e.g. the user switched and the earlier slot is still managing a trade) never lets one timeframe's signal place the other's order."""
        self.build("M1")
        for tf in ("M1", "M5"):
            ok, msg = self.engine.start("EURUSD", tf)
            self.assertTrue(ok, msg)
        self.engine.step()
        ok, msg = self.autopilot.start("runs", 5, spawn=False)
        self.assertTrue(ok, msg)
        self.signal(1.10100)
        self.autopilot.tick()
        self.assertEqual(self.lc("M1")["state"], "POSITION_OPEN")
        self.assertEqual(len(self.opens()), 1)                                                # Autopilot is bound to the selected M1; the M5 signal did not trade
        self.assertNotEqual(self.lc("M5")["state"], "POSITION_OPEN")


class NoSilentFallbackTest(TfRig, unittest.TestCase):
    def test_every_exec_timeframe_is_accepted_and_none_is_remapped(self):
        self.build("M1")
        for tf in EXEC_TIMEFRAMES:
            self.selection.save(strategy_id="candle_momentum", timeframe=tf)
            self.assertEqual(self.selection.timeframe("candle_momentum"), tf)
        self.assertIn("M1", EXEC_TIMEFRAMES)
        self.assertIn("M5", EXEC_TIMEFRAMES)

    def test_an_unsupported_timeframe_is_refused_not_converted_to_m5(self):
        self.build("M1")
        ok, msg = self.engine.start("EURUSD", "M7")
        self.assertFalse(ok)
        self.assertNotIn("EURUSD|M5", self.engine._slots)
        with self.assertRaises(ValueError):                                                    # refused loudly; the previous valid choice stays (it used to revert to M5)
            self.selection.save(strategy_id="candle_momentum", timeframe="M7")
        self.assertEqual(self.selection.timeframe("candle_momentum"), "M1")

    def test_watchdog_never_substitutes_m5_when_no_timeframe_is_given(self):
        from trademate.strategy.watchdog import WatchdogManager
        self.build("M1")
        wd = WatchdogManager(lambda *a, **k: {}, engine=self.engine)
        ok, why = wd.start("EURUSD", "candle_momentum", None)
        self.assertFalse(ok)
        self.assertIn("timeframe", why)
        self.assertNotIn("EURUSD|M5", self.engine._slots)
        ok, why = wd.start("EURUSD", "candle_momentum", "M1")
        self.assertTrue(ok, why)
        self.assertIn("EURUSD|M1", self.engine._slots)
        self.assertNotIn("EURUSD|M5", self.engine._slots)
        ok, why = wd.retarget("EURUSD", "candle_momentum", None)                              # retargeting without a timeframe is refused too
        self.assertFalse(ok)
        self.assertNotIn("EURUSD|M5", self.engine._slots)
        ok, why = wd.retarget("EURUSD", "candle_momentum", "M5")
        self.assertTrue(ok, why)
        self.assertIn("EURUSD|M5", self.engine._slots)
        wd.stop()


class SelectionApiTest(unittest.TestCase):
    def test_api_selection_round_trips_m1_and_m5_for_candle_momentum(self):
        with tempfile.TemporaryDirectory() as d:
            store = SelectionStore(Path(d) / "sel.json", "M5")
            strategy_store = StrategySelectionStore(Path(d) / "s.json", "candle_momentum")
            info = mock.Mock()
            info.name = "EURUSD"
            fake_adapter = mock.Mock()
            fake_adapter.get_symbol_info.return_value = info
            with mock.patch.object(app_module, "selection_store", store), mock.patch.object(app_module, "strategy_store", strategy_store), \
                    mock.patch.object(app_module, "adapter", fake_adapter), mock.patch.object(app_module.watchdog, "status", return_value={"active": False}):
                client = app_module.app.test_client()
                for tf in ("M1", "M5", "M1"):
                    r = client.post("/api/selection", json={"symbol": "EURUSD", "strategy": "candle_momentum", "timeframe": tf})
                    body = r.get_json()
                    self.assertTrue(body.get("ok"), body)
                    self.assertEqual(body["timeframe"], tf)
                    self.assertEqual(client.get("/api/selection").get_json()["timeframe"], tf)
                self.assertEqual(client.get("/api/selection").get_json()["timeframes"], list(EXEC_TIMEFRAMES))
                for sid in STRATEGY_IDS:               # the selector offers every execution timeframe whichever strategy is selected
                    r = client.post("/api/selection", json={"symbol": "EURUSD", "strategy": sid, "timeframe": "M1"})
                    self.assertEqual(r.get_json()["timeframes"], list(EXEC_TIMEFRAMES), sid)
                    self.assertEqual(client.get("/api/selection").get_json()["timeframes"], list(EXEC_TIMEFRAMES), sid)
                client.post("/api/selection", json={"symbol": "EURUSD", "strategy": "candle_momentum", "timeframe": "M1"})
                bad = client.post("/api/selection", json={"symbol": "EURUSD", "strategy": "candle_momentum", "timeframe": "M7"})
                self.assertEqual(bad.status_code, 400)
                self.assertEqual(client.get("/api/selection").get_json()["timeframe"], "M1")     # a rejected value leaves the user's choice alone


if __name__ == "__main__":
    unittest.main()
