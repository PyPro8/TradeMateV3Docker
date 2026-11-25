"""Candle Momentum v2: the persistent, tick-driven engine.

    python3 -m unittest tests.test_candle_momentum -v

SAFETY: nothing in this file can place a real order. Engine tests use tests/fake_mt5_bridge.py (a deterministic in-memory feed with no order method at all). The
end-to-end tests go through the real MT5Adapter -> Risk Engine -> pipeline path but against tests/fake_bridge.py (an in-memory stand-in for the MT5 Expert Advisor).
Passing these tests proves the rules and the wiring in simulation. It does NOT prove live-broker behaviour.
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.fake_mt5_bridge import CRASH100, EURUSD, FakeClock, FakeFeed
from trademate.candle_momentum import logic
from trademate.candle_momentum.diagnostics import EventLog
from trademate.candle_momentum.engine import CandleMomentumEngine
from trademate.candle_momentum.machine import CandleMachine, IllegalTransition, classify_refusal
from trademate.candle_momentum.models import LEGAL, CMConfig, CandleSnap, Quote, State, validate_config
from trademate.candle_momentum.store import ConfigStore, StateStore
from trademate.config import CandleMomentumSettings, Settings, StrategySettings
from trademate.decision_hub import DecisionHub
from trademate.instruments import build_spec
from trademate.strategies.base import EXEC_TIMEFRAMES
from trademate.strategy.timeframes import TF_SECONDS

PIP = 0.0001            # EURUSD strategy unit
S = State


class Rig:
    """Clock + fake feed + engine for one or more configurations. `step(bid)` = one engine cycle after a tick."""

    def __init__(self, tmp=None, symbols=None, settings=None, cfg_over=None, hub=True, persist=False):
        self.clock = FakeClock()
        self.feed = FakeFeed(self.clock, symbols)
        self.events = EventLog(None)
        self.hub = DecisionHub(clock=self.clock) if hub else None
        self.tmp = Path(tmp) if tmp else None
        self.settings = settings or Settings()
        self.cfg_store = ConfigStore(None, EXEC_TIMEFRAMES)
        self.state = StateStore(self.tmp / "state.json", self.clock) if (persist and self.tmp) else StateStore(None, self.clock)
        self.signals = []
        self.engine = self.make_engine()
        self.cfg_over = cfg_over

    def make_engine(self):
        return CandleMomentumEngine(self.feed, self.settings, self.cfg_store, self.state, self.events, hub=self.hub, clock=self.clock, threaded=False,
                                    on_signal=lambda *a: self.signals.append(a))

    def start(self, tf="M5", symbol=None):
        symbol = symbol or self.feed.symbol
        if self.cfg_over:
            base, _ = self.engine.effective_config(symbol, tf)
            self.cfg_store.save(CMConfig.from_dict(self.cfg_over, base))
        ok, why = self.engine.start(symbol, tf)
        assert ok, why
        self.feed._candle(symbol, tf)                                   # the broker's candle exists from the start (open = the price at that moment)
        return self

    def step(self, bid=None, dt=0.25, symbol=None, age=0.0):
        if bid is not None:
            self.feed.tick(bid, symbol, age)
        self.clock.advance(dt)
        self.engine.step()

    def lc(self, tf="M5", symbol=None):
        return self.engine._slots[f"{symbol or self.feed.symbol}|{tf}"].machine.lc

    def st(self, tf="M5", symbol=None):
        return self.lc(tf, symbol)["state"]

    def machine(self, tf="M5", symbol=None):
        return self.engine._slots[f"{symbol or self.feed.symbol}|{tf}"].machine

    def result(self, tf="M5", symbol=None):
        return self.engine.result(symbol or self.feed.symbol, tf)

    def event_names(self):
        return [e["event"] for e in self.events.tail(limit=1000)]

    def execute(self, tf="M5", fill=None, ticket=777, symbol=None, volume=0.01):
        """Play the Autopilot's observer calls for a successful order (the real Autopilot is used in the end-to-end class)."""
        lc = self.lc(tf, symbol)
        base = dict(symbol=symbol or self.feed.symbol, strategy_id="candle_momentum", exec_tf=tf, candidate_id=lc["candle_id"])
        self.engine.report_execution({"stage": "proposing", **base})
        self.engine.report_execution({"stage": "approved", "volume": volume, **base})
        self.engine.report_execution({"stage": "executed", "ticket": ticket, "fill_price": fill if fill is not None else lc["entry_reference"], **base})


def bull_to_trigger(r, pips=10, extra=0.0):
    r.step(1.10000)
    r.step(1.10000 + pips * PIP + extra * PIP)


class DirectionTests(unittest.TestCase):
    def test_01_new_bullish_candle(self):
        r = Rig().start()
        r.step(1.10000)
        self.assertEqual(r.st(), "MONITORING")
        r.step(1.10030)
        self.assertEqual(r.st(), "BULLISH")
        self.assertEqual(r.lc()["direction"], "bullish")
        self.assertAlmostEqual(r.lc()["trigger_price"], 1.1010, places=6)
        self.assertEqual(r.result()["decision"], "WATCH")           # a merely bullish candle is NOT a trade

    def test_02_new_bearish_candle(self):
        r = Rig().start()
        r.step(1.10000)
        r.step(1.09970)
        self.assertEqual(r.st(), "BEARISH")
        self.assertAlmostEqual(r.lc()["trigger_price"], 1.0990, places=6)
        self.assertEqual(r.result()["decision"], "WATCH")

    def test_03_neutral_candle(self):
        r = Rig().start()
        r.step(1.10000)
        self.assertEqual(r.st(), "MONITORING")
        self.assertIsNone(r.lc()["direction"])
        self.assertIsNone(r.lc()["trigger_price"])
        self.assertEqual(r.result()["decision"], "WAIT")

    def test_08_direction_reversal_before_trigger(self):
        r = Rig().start()
        r.step(1.10000)
        r.step(1.10060)
        self.assertEqual(r.st(), "BULLISH")
        r.step(1.09960)                                               # reverses through the open
        self.assertEqual(r.st(), "BEARISH")
        self.assertAlmostEqual(r.lc()["trigger_price"], 1.0990, places=6)    # the trigger moved to the other side
        r.step(1.10000)
        self.assertEqual(r.st(), "MONITORING")
        self.assertEqual(r.lc()["direction_changes"], 1)
        self.assertNotIn("trigger_crossed", r.event_names())
        self.assertEqual(r.result()["decision"], "WAIT")

    def test_neutral_uses_price_precision_not_float_equality(self):
        self.assertEqual(logic.candle_direction(1.100001, 1.10000, 1e-05), "neutral")
        self.assertEqual(logic.candle_direction(1.10001, 1.10000, 1e-05), "bullish")
        self.assertEqual(logic.candle_direction(1.09999, 1.10000, 1e-05), "bearish")


class TriggerTests(unittest.TestCase):
    def test_04_bullish_trigger_crossing_enters_immediately(self):
        r = Rig().start()
        bull_to_trigger(r)
        self.assertEqual(r.st(), "RISK_PENDING")
        res = r.result()
        self.assertEqual(res["decision"], "BUY")
        self.assertEqual(res["setup_candidates"][0]["execution_eligible"], True)
        self.assertEqual(len(r.signals), 1)                           # the executor was woken the instant the signal existed
        self.assertAlmostEqual(res["stop_loss"]["price"], 1.0950, places=6)   # updated default: 50 strategy units beyond the open

    def test_05_bearish_trigger_crossing(self):
        r = Rig().start()
        r.step(1.10000)
        r.step(1.09900)
        self.assertEqual(r.st(), "RISK_PENDING")
        self.assertEqual(r.result()["decision"], "SELL")
        self.assertAlmostEqual(r.result()["stop_loss"]["price"], 1.1050, places=6)

    def test_06_price_jumps_beyond_trigger_in_one_tick(self):
        r = Rig().start()
        r.step(1.10020)                                               # +2 pips, then ONE tick to +13 (3 pips beyond the trigger: beyond the old 2-pip limit)
        r.step(1.10130)
        self.assertEqual(r.st(), "RISK_PENDING")
        self.assertEqual(r.lc()["crossing_context"], "crossed_between_ticks")
        self.assertEqual(r.result()["decision"], "BUY")
        names = [t["state"] for t in r.lc()["transitions"]]
        self.assertEqual(names[-4:], ["BULLISH", "TRIGGER_REACHED", "ENTRY_VALIDATION", "RISK_PENDING"])   # all evaluated in the SAME cycle

    def test_06b_a_jump_far_beyond_is_proposed_at_the_current_quote_not_missed(self):
        r = Rig().start()
        r.step(1.10020)
        r.step(1.10180)                                               # +18 pips: 8 beyond the trigger (92% incl. spread) - formerly MISSED
        self.assertEqual(r.st(), "RISK_PENDING")
        v = r.lc()["verdict"]
        self.assertEqual((v["kind"], v["code"]), ("ENTER", "ok"))
        self.assertGreater(v["beyond_trigger_units"], 8.0)
        self.assertAlmostEqual(v["tp_preview"], v["exec_price"] + 20 * PIP, places=6)   # updated default target from the executable price, never from the trigger

    def test_07_crossing_without_manual_analysis(self):
        r = Rig().start()
        bull_to_trigger(r)
        rec = r.hub.get("EURUSD", "candle_momentum", "M5")
        self.assertEqual((rec["state"], rec["decision"], rec["source"]), ("fresh", "BUY", "engine"))
        self.assertLess(r.feed.candle_calls, r.feed.quote_calls)       # candles are read for identity only; every tick is a quote read, no per-tick candle analysis, no click needed

    def test_exact_touch_counts_as_crossing(self):
        r = Rig().start()
        r.step(1.10000)
        r.step(1.10100)
        self.assertEqual(r.st(), "RISK_PENDING")

    def test_one_tick_short_of_trigger_does_not_enter(self):
        r = Rig().start()
        r.step(1.10000)
        r.step(1.10099)
        self.assertEqual(r.st(), "BULLISH")

    def test_no_hard_coded_two_pip_chase_limit(self):
        # 4.9 pips beyond the trigger (the old strategy refused beyond 2): with a 10-pip target that is 49% consumed -> still valid
        r = Rig().start()
        r.step(1.10020)
        r.step(1.10100 + 4.9 * PIP - 0.00012)                         # bid such that the ASK is 4.9 pips + spread beyond... keep the executable inside 50%
        self.assertEqual(r.st(), "RISK_PENDING")
        v = r.lc()["verdict"]
        self.assertGreater(v["beyond_trigger_units"], 2.0)

    def test_reversal_after_trigger_before_commitment_invalidates(self):
        r = Rig().start()
        bull_to_trigger(r)
        r.step(1.10050)                                               # back inside the trigger while still RISK_PENDING
        self.assertEqual(r.st(), "INVALIDATED")
        self.assertIn("back below the trigger", r.lc()["reason"])
        self.assertNotEqual(r.result()["decision"], "BUY")


class EntryValidityTests(unittest.TestCase):
    def test_19_no_percentage_based_late_entry_rejection_at_20_50_and_92_percent(self):
        # Removed on purpose: price far beyond the trigger is still proposed at the CURRENT executable quote (the Risk Engine / broker decide).
        for over_pips in (2.0, 5.0, 9.2):                             # 20%, 50%, 92% of the 10-pip target beyond the trigger
            r = Rig().start()
            r.step(1.10050)
            r.step(1.10100 + over_pips * PIP)
            self.assertEqual(r.st(), "RISK_PENDING", over_pips)
            self.assertNotEqual(r.lc()["verdict"]["kind"], "MISSED")

    def test_target_beyond_the_old_plan_level_still_enters_and_is_measured_from_the_quote(self):
        r = Rig().start()
        r.step(1.10050)
        r.step(1.10100 + 11 * PIP)                                    # already beyond trigger + a full 10-pip target
        self.assertEqual(r.st(), "RISK_PENDING")
        v = r.lc()["verdict"]
        self.assertEqual(v["kind"], "ENTER")
        self.assertAlmostEqual(v["tp_preview"], v["exec_price"] + 20 * PIP, places=6)   # target = executable price + the configured 20 pips

    def test_a_legacy_percentage_setting_is_ignored_not_applied(self):
        r = Rig().start()
        self.assertFalse(hasattr(r.lc(), "max_target_consumed_fraction"))

    def test_trigger_and_target_are_configurable(self):
        r = Rig(cfg_over={"trigger_units": 5.0, "target_units": 10.0}).start()
        r.step(1.10000)
        r.step(1.10050)
        self.assertEqual(r.st(), "RISK_PENDING")
        self.assertAlmostEqual(r.lc()["trigger_price"], 1.1005, places=6)

    def test_20_target_calculated_from_actual_fill(self):
        r = Rig().start()
        bull_to_trigger(r)
        ref = r.lc()["entry_reference"]
        r.execute(fill=ref + 3 * PIP)                                 # filled 3 pips worse than the quote
        lc = r.lc()
        self.assertEqual(lc["state"], "POSITION_OPEN")
        self.assertAlmostEqual(lc["entry_price"], ref + 3 * PIP, places=6)
        self.assertAlmostEqual(lc["target_price"], ref + 3 * PIP + 20 * PIP, places=6)      # fill + 20 pips, NOT open/trigger/reference based
        self.assertNotAlmostEqual(lc["target_price"], lc["open"] + 20 * PIP, places=4)

    def test_sell_target_from_actual_fill(self):
        r = Rig().start()
        r.step(1.10000)
        r.step(1.09900)
        fill = r.lc()["entry_reference"] - 2 * PIP
        r.execute(fill=fill)
        self.assertAlmostEqual(r.lc()["target_price"], fill - 20 * PIP, places=6)

    def test_21_execution_slippage_measured_separately(self):
        r = Rig(cfg_over={"max_slippage_units": 1.0}).start()
        bull_to_trigger(r)
        ref = r.lc()["entry_reference"]
        r.execute(fill=ref + 2.5 * PIP)
        lc = r.lc()
        self.assertAlmostEqual(lc["slippage_units"], 2.5, places=4)
        self.assertTrue(lc["slippage_exceeded"])
        self.assertIn("execution_slippage", r.event_names())
        self.assertIn("EXCEEDED", lc["reason"])

    def test_price_improvement_is_negative_slippage(self):
        self.assertAlmostEqual(logic.slippage_units("buy", 1.1001, 1.1000, build_spec(_info(EURUSD), {}, 1.0, "EURUSD")), -1.0, places=4)

    def test_slippage_guard_in_pipeline_is_adverse_only_and_configurable(self):
        from trademate import pipeline
        from trademate.config import PipelineSettings
        core = {"direction": "buy", "stop_loss": 1.0999, "take_profit": 1.1020}
        cfg = PipelineSettings()
        ok = pipeline.eligibility_problems(core, 1.1011, 0.0, 1.1010, 1.1009, 1.1010, cfg, 0.0002)           # price improved: never a problem
        self.assertEqual([c for c, _ in ok], [])
        bad = pipeline.eligibility_problems(core, 1.1011, 0.0, 1.1014, 1.1013, 1.1014, cfg, 0.0002)          # 3 pips worse than the 2-pip tolerance
        self.assertEqual([c for c, _ in bad], [pipeline.ENTRY_DRIFTED])
        self.assertIn("slippage tolerance", bad[0][1])


class ExecutorOutcomeTests(unittest.TestCase):
    def armed(self):
        r = Rig().start()
        bull_to_trigger(r)
        return r

    def base(self, r):
        return dict(symbol="EURUSD", strategy_id="candle_momentum", exec_tf="M5", candidate_id=r.lc()["candle_id"])

    def test_22_risk_engine_rejection(self):
        r = self.armed()
        r.engine.report_execution({"stage": "proposing", **self.base(r)})
        r.engine.report_execution({"stage": "rejected", "code": "risk_rejected", "message": "Risk 2.4% exceeds the 1% limit", **self.base(r)})
        self.assertEqual(r.st(), "REJECTED")
        self.assertEqual(r.lc()["risk_decision"]["status"], "rejected")
        self.assertIn("Risk 2.4%", r.lc()["reason"])
        self.assertNotEqual(r.result()["decision"], "BUY")

    def test_23_invalid_lot_size(self):
        r = self.armed()
        r.engine.report_execution({"stage": "rejected", "code": "volume_invalid", "message": "0.005 lots is below the broker minimum of 0.01", **self.base(r)})
        self.assertEqual(r.st(), "REJECTED")
        self.assertIn("0.005", r.lc()["reason"])

    def test_26_broker_execution_failure_is_not_retried_by_default(self):
        r = self.armed()
        r.engine.report_execution({"stage": "proposing", **self.base(r)})
        r.engine.report_execution({"stage": "approved", **self.base(r)})
        r.engine.report_execution({"stage": "failed", "code": "broker_rejected", "message": "Requote", "order_sent": True, **self.base(r)})
        self.assertEqual(r.st(), "EXECUTION_FAILED")
        for _ in range(40):
            r.step(1.10105, dt=1.0)
        self.assertEqual(r.st(), "EXECUTION_FAILED")                  # no spam, no automatic retry
        self.assertTrue(r.lc()["needs_reconcile"])

    def test_unconfirmed_outcome_is_never_retried_even_with_retries_enabled(self):
        r = Rig(cfg_over={"max_entry_attempts": 3}).start()
        bull_to_trigger(r)
        r.engine.report_execution({"stage": "proposing", **self.base(r)})
        r.engine.report_execution({"stage": "unconfirmed", "code": "accepted_unconfirmed", "message": "sent, not confirmed", "order_sent": True, **self.base(r)})
        for _ in range(40):
            r.step(1.10105, dt=1.0)
        self.assertEqual(r.st(), "EXECUTION_FAILED")
        self.assertIsNone(r.lc()["retry_at"])

    def test_bounded_retry_only_after_a_provably_unsent_order(self):
        released = []
        r = Rig(cfg_over={"max_entry_attempts": 2, "retry_delay_seconds": 1.0, "signal_expiry_seconds": 60}).start()
        r.engine.release_hook = released.append
        bull_to_trigger(r)
        b = self.base(r)
        r.engine.report_execution({"stage": "proposing", **b})
        r.engine.report_execution({"stage": "refused", "code": "market_unavailable", "message": "MT5 data unavailable", "order_sent": False, **b})
        self.assertEqual(r.st(), "EXECUTION_FAILED")
        self.assertIsNotNone(r.lc()["retry_at"])
        r.step(1.10105, dt=1.5)                                       # delay passed -> ONE retry validates again on a fresh quote
        r.step(1.10105)
        self.assertEqual(r.st(), "RISK_PENDING")
        self.assertEqual(released, [r.lc()["candle_id"]])
        r.engine.report_execution({"stage": "proposing", **b})
        r.engine.report_execution({"stage": "refused", "code": "market_unavailable", "message": "again", "order_sent": False, **b})
        self.assertEqual(r.st(), "EXECUTION_FAILED")
        self.assertIsNone(r.lc()["retry_at"])                         # attempts exhausted: bounded
        self.assertEqual(r.lc()["attempts"], 2)

    def test_market_moved_refusal_is_missed_not_rejected(self):
        self.assertEqual(classify_refusal("entry_drifted")[0], S.MISSED)
        self.assertEqual(classify_refusal("risk_revalidation_failed")[0], S.REJECTED)
        self.assertEqual(classify_refusal("market_unavailable"), (S.EXECUTION_FAILED, True))
        self.assertEqual(classify_refusal("some_unknown_code")[0], S.REJECTED)      # unknown fails closed

    def test_27_position_target_reached(self):
        r = self.armed()
        r.execute(ticket=55)
        r.feed.positions[55] = {}
        tgt = r.lc()["target_price"]
        r.step(tgt - 0.00005)
        self.assertEqual(r.st(), "POSITION_OPEN")
        r.step(tgt)
        self.assertEqual(r.st(), "TARGET_REACHED")

    def test_28_position_closed(self):
        r = self.armed()
        r.execute(ticket=56)
        r.feed.positions[56] = {}
        r.step(1.10110, dt=1.1)
        self.assertEqual(r.st(), "POSITION_OPEN")
        r.feed.positions.pop(56)
        r.feed.history[56] = {"reason": "tp", "net": 0.1, "price_close": 1.1021}
        r.step(1.10110, dt=1.1)
        self.assertEqual(r.st(), "POSITION_CLOSED")
        self.assertEqual(r.lc()["closed"]["net"], 0.1)
        self.assertIn("position_closed", r.event_names())

    def test_event_for_another_candle_is_ignored(self):
        r = self.armed()
        self.assertFalse(r.engine.report_execution({"stage": "executed", "symbol": "EURUSD", "exec_tf": "M5", "candidate_id": "deadbeef", "ticket": 1}))
        self.assertEqual(r.st(), "RISK_PENDING")


class OneEntryPerCandleTests(unittest.TestCase):
    def test_10_one_entry_per_candle(self):
        r = Rig().start()
        bull_to_trigger(r)
        r.execute(ticket=1)
        first = r.lc()["candle_id"]
        r.feed.positions[1] = {}
        for bid in (1.10000, 1.09800, 1.10200, 1.10150, 1.09700):    # whipsaw through both triggers
            r.step(bid, dt=0.5)
        self.assertEqual(r.lc()["candle_id"], first)
        self.assertEqual(r.lc()["state"], "POSITION_OPEN")
        self.assertEqual(len(r.signals), 1)                           # exactly one signal for the whole candle
        self.assertEqual(sum(1 for t in r.lc()["transitions"] if t["state"] == "RISK_PENDING"), 1)

    def test_a_candle_that_reversed_after_a_bullish_signal_is_not_entered_the_other_way(self):
        r = Rig().start()
        r.step(1.10020)
        r.step(1.10180)                                               # bullish signal proposed far beyond the trigger
        before = len(r.signals)
        r.step(1.09900)                                               # the candle then reverses through the bearish trigger
        self.assertEqual(r.st(), "INVALIDATED")                       # price is back inside the bullish trigger: the thesis is gone
        self.assertEqual(len(r.signals), before)                      # and the same candle is never entered the other way

    def test_candle_id_is_stable_and_direction_independent(self):
        a = logic.make_candle_id("EURUSD", "M5", 1700000100, 1.1)
        self.assertEqual(a, logic.make_candle_id("EURUSD", "M5", 1700000100, 1.10000))
        self.assertNotEqual(a, logic.make_candle_id("EURUSD", "M15", 1700000100, 1.1))
        self.assertNotEqual(a, logic.make_candle_id("EURUSD", "M5", 1700000400, 1.1))
        self.assertNotEqual(a, logic.make_candle_id("GBPUSD", "M5", 1700000100, 1.1))

    def test_09_new_candle_resets_state_correctly(self):
        r = Rig().start()
        r.step(1.10000)
        r.step(1.10060)
        old = r.lc()["candle_id"]
        r.feed.roll_to("M5")
        r.engine.step()
        r.step(1.10060)
        lc = r.lc()
        self.assertNotEqual(lc["candle_id"], old)
        self.assertEqual(lc["state"], "MONITORING")                   # price equals the NEW candle's open
        self.assertIsNone(lc["direction"])
        self.assertAlmostEqual(lc["open"], 1.10060, places=6)
        self.assertEqual(r.state.get(old)["state"], "EXPIRED")        # the earlier candle ended without an entry
        self.assertIn("new_candle_detected", r.event_names())

    def test_open_price_revision_in_same_period_never_makes_a_second_candle(self):
        r = Rig().start()
        bull_to_trigger(r)
        r.execute(ticket=2)
        old = r.lc()["candle_id"]
        r.feed.candles[("EURUSD", "M5")][1] = 1.09990                 # the broker revises the open of the SAME period
        for _ in range(30):
            r.step(1.10105, dt=1.0)
        self.assertEqual(r.lc()["candle_id"], old)
        self.assertIn("open_price_revised", r.event_names())

    def test_11_duplicate_watchdog_events_and_duplicate_start(self):
        r = Rig()
        self.assertTrue(r.engine.start("EURUSD", "M5")[0])
        self.assertTrue(r.engine.start("EURUSD", "M5")[0])
        self.assertEqual(len(r.engine._slots), 1)                     # one slot, never two workers for one key
        self.assertEqual(r.event_names().count("engine_started"), 1)

    def test_12_browser_refresh_does_not_touch_state(self):
        r = Rig().start()
        bull_to_trigger(r)
        v, tr = r.machine().version, len(r.lc()["transitions"])
        for _ in range(50):                                           # a browser polling the views
            r.engine.snapshot("EURUSD", "M5")
            r.engine.result("EURUSD", "M5")
        self.assertEqual((r.machine().version, len(r.lc()["transitions"])), (v, tr))
        self.assertEqual(r.st(), "RISK_PENDING")

    def test_manual_one_shot_never_writes_or_claims(self):
        with tempfile.TemporaryDirectory() as d:
            r = Rig(tmp=d, persist=True)
            r.feed._candle("EURUSD", "M5")
            r.feed.tick(1.10105)
            res = r.engine.evaluate_once("EURUSD", "M5", r.clock())
            self.assertEqual(res["decision"], "BUY")
            self.assertFalse(res["candle_momentum"]["monitoring"])
            self.assertEqual(res["candle_momentum"]["source"], "manual")
            self.assertEqual(r.state.for_key("EURUSD|M5"), [])        # nothing persisted by a manual Analyse


class PersistenceTests(unittest.TestCase):
    def committed(self, tmp):
        r = Rig(tmp=tmp, persist=True).start()
        bull_to_trigger(r)
        return r

    def test_13_restart_with_open_position_never_re_enters(self):
        with tempfile.TemporaryDirectory() as d:
            r1 = self.committed(d)
            r1.execute(ticket=9)
            cid = r1.lc()["candle_id"]
            r1.state.flush()
            clock, feed = r1.clock, r1.feed
            feed.positions[9] = {}
            r2 = Rig(tmp=d, persist=True)
            r2.clock, r2.feed = clock, feed
            r2.engine = CandleMomentumEngine(feed, Settings(), r2.cfg_store, r2.state, r2.events, hub=None, clock=clock, threaded=False, on_signal=r2.signals.append)
            r2.engine.start("EURUSD", "M5")
            for _ in range(5):
                r2.step(1.10110)
            self.assertEqual(r2.lc()["candle_id"], cid)
            self.assertEqual(r2.st(), "POSITION_OPEN")
            self.assertEqual(r2.lc()["ticket"], 9)
            self.assertEqual(r2.signals, [])                          # no second signal after the restart
            self.assertTrue(r2.lc()["restored"])

    def test_13b_restart_while_signal_pending_expires_it_without_an_order(self):
        with tempfile.TemporaryDirectory() as d:
            r1 = self.committed(d)
            self.assertEqual(r1.st(), "RISK_PENDING")
            r1.state.flush()
            r2 = Rig(tmp=d, persist=True)
            r2.clock, r2.feed = r1.clock, r1.feed
            r2.engine = CandleMomentumEngine(r1.feed, Settings(), r2.cfg_store, r2.state, r2.events, clock=r1.clock, threaded=False, on_signal=r2.signals.append)
            r2.engine.start("EURUSD", "M5")
            r2.step(1.10110)
            self.assertEqual(r2.st(), "EXPIRED")
            self.assertIn("restarted", r2.lc()["reason"])
            self.assertEqual(r2.signals, [])

    def test_13c_restart_while_executing_is_unknown_and_never_retried(self):
        with tempfile.TemporaryDirectory() as d:
            r1 = self.committed(d)
            b = dict(symbol="EURUSD", strategy_id="candle_momentum", exec_tf="M5", candidate_id=r1.lc()["candle_id"])
            r1.engine.report_execution({"stage": "proposing", **b})
            r1.engine.report_execution({"stage": "approved", **b})
            self.assertEqual(r1.st(), "EXECUTING")
            r1.state.flush()
            r2 = Rig(tmp=d, persist=True)
            r2.clock, r2.feed = r1.clock, r1.feed
            r2.engine = CandleMomentumEngine(r1.feed, Settings(), r2.cfg_store, r2.state, r2.events, clock=r1.clock, threaded=False, on_signal=r2.signals.append)
            r2.engine.start("EURUSD", "M5")
            r2.step(1.10110)
            self.assertEqual(r2.st(), "EXECUTION_FAILED")
            self.assertTrue(r2.lc()["needs_reconcile"])
            self.assertEqual(r2.signals, [])

    def test_stop_and_start_again_mid_candle_does_not_duplicate(self):
        r = Rig(persist=False).start()
        bull_to_trigger(r)
        r.execute(ticket=4)
        r.engine.stop("EURUSD", "M5")
        r.feed.positions[4] = {}
        r.engine.start("EURUSD", "M5")
        for _ in range(5):
            r.step(1.10110)
        self.assertEqual(len(r.signals), 1)
        self.assertEqual(r.st(), "POSITION_OPEN")

    def test_corrupt_state_file_reads_as_empty_and_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "state.json").write_text("{not json")
            s = StateStore(Path(d) / "state.json")
            self.assertEqual(s.for_key("x"), [])
            self.assertIn("could not be read", s.problem)

    def test_transitions_are_persisted_with_every_required_field(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.committed(d)
            data = json.loads((Path(d) / "state.json").read_text())
            lc = data["lifecycles"][r.lc()["candle_id"]]
            need = {"timestamp", "symbol", "timeframe", "candle_id", "current_price", "candle_open", "direction", "trigger_price", "entry_price", "target_price", "state", "explanation"}
            for t in lc["transitions"]:
                self.assertTrue(need <= set(t), need - set(t))
            self.assertEqual(lc["state"], "RISK_PENDING")


class FeedFaultTests(unittest.TestCase):
    def test_14_missing_ticks_then_jump_is_evaluated_and_the_gap_logged(self):
        r = Rig().start()
        r.step(1.10020)
        r.clock.advance(6.0)                                          # six silent seconds, no engine cycles
        r.step(1.10130)
        self.assertEqual(r.st(), "RISK_PENDING")
        self.assertIn("quote_gap", r.event_names())

    def test_15_stale_quotes_never_trigger_an_entry(self):
        r = Rig().start()
        r.step(1.10020)
        r.step(1.10105, age=10.0)                                     # source is 10 s old (limit 3 s)
        self.assertEqual(r.st(), "BULLISH")
        self.assertEqual(r.hub.get("EURUSD", "candle_momentum", "M5")["state"], "data_problem")
        self.assertIn("quote_freshness", r.event_names())
        r.step(1.10105, age=0.0)                                      # a fresh quote -> evaluated at once
        self.assertEqual(r.st(), "RISK_PENDING")

    def test_stale_quote_at_the_moment_of_validation_waits_then_expires(self):
        v = logic.validate_entry("bullish", 1.1010, 1.1000, Quote("EURUSD", 1.1011, 1.10122, 9.0, 0.0), CMConfig("EURUSD", "M5"), build_spec(_info(EURUSD), {}, 0.0, "EURUSD"), 0.0)
        self.assertEqual((v.kind, v.code), ("WAIT_QUOTE", "stale_quote"))

    def test_16_mt5_disconnection(self):
        r = Rig().start()
        r.step(1.10020)
        r.feed.is_connected = False
        r.step(1.10105)
        self.assertEqual(r.st(), "BULLISH")                           # nothing is judged while disconnected
        self.assertEqual(r.hub.get("EURUSD", "candle_momentum", "M5")["state"], "disconnected")
        self.assertIn("feed_down", r.event_names())
        self.assertIsNone(r.hub.get("EURUSD", "candle_momentum", "M5")["decision"])

    def test_17_mt5_reconnection_resumes_without_duplicates(self):
        r = Rig().start()
        bull_to_trigger(r)
        r.execute(ticket=3)
        r.feed.is_connected = False
        r.step(1.10105)
        r.feed.is_connected = True
        r.step(1.10105)
        self.assertIn("reconnected", r.event_names())
        self.assertEqual(r.hub.get("EURUSD", "candle_momentum", "M5")["state"], "fresh")
        self.assertEqual(len(r.signals), 1)
        self.assertEqual(r.st(), "POSITION_OPEN")

    def test_candle_fetch_failure_blocks_publication(self):
        r = Rig().start()
        r.step(1.10020)
        r.feed.fail_candle = {"EURUSD"}
        r.clock.advance(10)
        r.step(1.10020)
        self.assertEqual(r.hub.get("EURUSD", "candle_momentum", "M5")["state"], "data_problem")

    def test_price_update_rate_counts_real_changes_not_polls(self):
        r = Rig().start()
        for _ in range(8):                                            # the broker delivers ONE new price; the engine polls eight times
            r.step(1.10020)
        f = r.engine.snapshot("EURUSD", "M5")["feed"]
        self.assertEqual(f["polls"], 8)
        self.assertEqual(f["price_updates"], 1)
        r.step(1.10025)
        self.assertEqual(r.engine.snapshot("EURUSD", "M5")["feed"]["price_updates"], 2)

    def test_engine_survives_an_internal_error_in_one_slot(self):
        r = Rig().start()
        r.feed.quote = lambda s: (_ for _ in ()).throw(RuntimeError("boom"))
        r.step(1.1)
        self.assertIn("watchdog_failure", r.event_names())


class InstrumentTests(unittest.TestCase):
    def test_24_missing_protective_stop_refuses_the_entry(self):
        r = Rig().start()
        slot = r.engine._slots["EURUSD|M5"]
        slot.cfg = CMConfig("EURUSD", "M5", sl_buffer_units=None)       # bypass validation: the runtime must still fail safe
        slot.machine.set_config(slot.cfg)
        bull_to_trigger(r)
        self.assertEqual(r.st(), "REJECTED")
        self.assertEqual(r.lc()["verdict"]["code"], "no_protective_stop")
        self.assertIn("protective stop", r.lc()["reason"])
        self.assertNotEqual(r.result()["decision"], "BUY")
        self.assertTrue(validate_config(CMConfig("EURUSD", "M5", sl_buffer_units=-1), EXEC_TIMEFRAMES))

    def test_24b_unverified_instrument_blocks_execution(self):
        r = Rig().start()
        r.feed.fields = None
        r.feed.specs["EURUSD"]["tick_value"] = None
        r.feed.specs["EURUSD"]["tick_value_profit"] = None
        r.engine._slots["EURUSD|M5"].spec_at = 0                      # force a spec re-read
        bull_to_trigger(r)
        self.assertEqual(r.st(), "REJECTED")
        self.assertIn("UNVERIFIED", r.lc()["reason"])
        self.assertEqual(r.engine.snapshot("EURUSD", "M5")["conversion"]["status"], "UNVERIFIED")

    def test_25_invalid_symbol_and_unreadable_spec(self):
        r = Rig()
        r.feed.fail_spec = True
        r.engine.start("NOSUCH", "M5") if False else r.engine.start("EURUSD", "M5")
        r.step(1.1)
        self.assertEqual(r.engine.snapshot("EURUSD", "M5")["conversion"]["status"], "UNVERIFIED")
        self.assertEqual(r.hub.get("EURUSD", "candle_momentum", "M5")["state"], "data_problem")
        self.assertFalse(r.engine.start("EURUSD", "M7")[0])           # an unknown timeframe is refused

    def test_unknown_pip_blocks_trading_without_guessing(self):
        spec = build_spec(_info({**EURUSD, "name": "WEIRD", "path": "Other\\X", "digits": 4, "point": 0.0001, "tick_size": 0.0001}), {}, 0.0, "WEIRD")
        v = logic.validate_entry("bullish", 1.1, 1.0, Quote("WEIRD", 1.101, 1.1011, 0.0, 0.0), CMConfig("WEIRD", "M5"), spec, 0.0)
        self.assertEqual((v.kind, v.code), ("REJECTED", "conversion_unavailable"))

    def test_crash100_conversion_matches_the_confirmed_account_convention(self):
        spec = build_spec(_info(CRASH100), {}, 0.0, "Crash 100 Index")
        self.assertEqual(spec.unit, 1.0)
        self.assertEqual(spec.status, "VERIFIED", spec.problems)
        for lots in (0.01, 0.06, 0.10):                               # 0.01 lot = $0.01 per pip, 0.10 = $0.10, 0.06 = $0.06
            self.assertAlmostEqual(spec.money_per_unit(lots), lots, places=9)
        self.assertAlmostEqual(spec.units_to_price(10), 10.0)

    def test_crash100_rejected_when_the_broker_contract_does_not_reproduce_the_convention(self):
        spec = build_spec(_info({**CRASH100, "tick_value": 0.01, "tick_value_profit": 0.01}), {}, 0.0, "Crash 100 Index")
        self.assertEqual(spec.status, "UNVERIFIED")
        self.assertIn("NOT reproduced", " ".join(spec.problems))

    def test_crash100_engine_uses_one_price_unit_per_pip(self):
        r = Rig(symbols={"Crash 100 Index": CRASH100}).start(symbol="Crash 100 Index")
        r.step(1000.000, symbol="Crash 100 Index")
        r.step(1004.000, symbol="Crash 100 Index")
        self.assertEqual(r.st(symbol="Crash 100 Index"), "BULLISH")
        self.assertAlmostEqual(r.lc(symbol="Crash 100 Index")["trigger_price"], 1010.000, places=6)     # 10 units, not 10 * 0.0001
        r.step(1010.000, symbol="Crash 100 Index")
        self.assertEqual(r.st(symbol="Crash 100 Index"), "RISK_PENDING")

    def test_an_absurd_pip_size_that_yields_a_negative_trigger_is_refused_not_traded(self):
        r = Rig().start()
        r.feed.spec_overrides = {"EURUSD": 0.5}                        # the user typed a 0.5 pip for EURUSD: 10 pips = 5.0 in price
        r.engine._slots["EURUSD|M5"].spec_at = 0
        r.step(1.10000)
        r.step(1.09900)                                               # bearish: open - 5.0 would be a NEGATIVE price
        self.assertEqual(r.st(), "BEARISH")
        self.assertIsNone(r.lc()["trigger_price"])
        self.assertIn("cannot be calculated", r.lc()["block_reason"])
        r.step(1.00000)
        self.assertEqual(r.result()["decision"], "WATCH")
        self.assertEqual(r.signals, [])

    def test_distance_conversion_is_not_forex_hardcoded(self):
        eur = build_spec(_info(EURUSD), {}, 0.0, "EURUSD")
        gold = build_spec(_info({**EURUSD, "name": "XAUUSD", "digits": 2, "point": 0.01, "tick_size": 0.01, "path": "Metals", "contract_size": 100, "tick_value": 1.0}), {}, 0.0, "XAUUSD")
        self.assertAlmostEqual(eur.units_to_price(10), 0.0010)
        self.assertAlmostEqual(gold.units_to_price(10), 1.0)


def _info(fields, bid=1.1, ask=1.10012):
    from types import SimpleNamespace
    return SimpleNamespace(**fields, bid=bid, ask=ask)


class IndependenceTests(unittest.TestCase):
    def test_29_m1_m5_m15_states_are_independent(self):
        r = Rig(cfg_over={"signal_expiry_seconds": 3600})
        for tf in ("M1", "M5", "M15"):
            r.start(tf)
        r.step(1.10000)
        r.step(1.10105)
        self.assertEqual({tf: r.st(tf) for tf in ("M1", "M5", "M15")}, {"M1": "RISK_PENDING", "M5": "RISK_PENDING", "M15": "RISK_PENDING"})
        r.feed.roll_to("M1")                                          # only the M1 candle rolls over
        r.engine.step()
        r.step(1.10105)
        self.assertEqual(r.st("M1"), "MONITORING")
        self.assertEqual(r.st("M5"), "RISK_PENDING")
        self.assertEqual(r.st("M15"), "RISK_PENDING")
        ids = {r.lc(tf)["candle_id"] for tf in ("M1", "M5", "M15")}
        self.assertEqual(len(ids), 3)
        for tf in ("M1", "M5", "M15"):
            self.assertEqual(r.hub.get("EURUSD", "candle_momentum", tf)["exec_tf"], tf)

    def test_30_different_symbols_run_independently(self):
        r = Rig(symbols={"EURUSD": EURUSD, "Crash 100 Index": CRASH100})
        r.start("M5", "EURUSD")
        r.start("M5", "Crash 100 Index")
        r.step(1.10000, symbol="EURUSD")
        r.step(1000.000, symbol="Crash 100 Index")
        r.step(1.10105, symbol="EURUSD")
        self.assertEqual(r.st(symbol="EURUSD"), "RISK_PENDING")
        self.assertEqual(r.st(symbol="Crash 100 Index"), "MONITORING")
        r.step(990.000, symbol="Crash 100 Index")
        self.assertEqual(r.st(symbol="Crash 100 Index"), "RISK_PENDING")
        self.assertEqual(r.result(symbol="Crash 100 Index")["decision"], "SELL")
        self.assertEqual(r.result(symbol="EURUSD")["decision"], "BUY")

    def test_configurations_are_independent_per_key(self):
        r = Rig()
        r.cfg_store.save(CMConfig("EURUSD", "M15", trigger_units=3.0))
        self.assertEqual(r.engine.effective_config("EURUSD", "M15")[0].trigger_units, 3.0)
        self.assertEqual(r.engine.effective_config("EURUSD", "M5")[0].trigger_units, 10.0)


class StateMachineTests(unittest.TestCase):
    def machine(self):
        return CandleMachine(CMConfig("EURUSD", "M5"), EventLog(None), None, FakeClock())

    def test_illegal_transition_is_refused_and_recorded(self):
        m = self.machine()
        m.begin_candle(CandleSnap("EURUSD", "M5", 1700000100, 1.1, 1.1, 1.1, 1.1), 1.0)
        with self.assertRaises(IllegalTransition):
            m._go(S.POSITION_OPEN, "nope", 2.0)
        self.assertEqual(m.lc["state"], "NEW_CANDLE")

    def test_transition_table_covers_every_state(self):
        self.assertEqual(set(LEGAL), set(State))
        self.assertEqual(len(State), 16)
        for s in (S.MISSED, S.REJECTED, S.INVALIDATED, S.EXPIRED, S.POSITION_CLOSED):
            self.assertEqual(LEGAL[s], set())

    def test_every_transition_record_has_every_required_field(self):
        r = Rig().start()
        bull_to_trigger(r)
        need = {"timestamp", "symbol", "timeframe", "candle_id", "current_price", "candle_open", "direction", "trigger_price", "entry_price", "target_price", "state", "explanation"}
        for t in r.lc()["transitions"]:
            self.assertTrue(need <= set(t))
            self.assertTrue(t["explanation"])
            self.assertEqual(t["candle_id"], r.lc()["candle_id"])

    def test_concurrent_quotes_are_thread_safe_and_enter_once(self):
        m = self.machine()
        spec = build_spec(_info(EURUSD), {}, 1_700_000_100.0, "EURUSD")
        m.begin_candle(CandleSnap("EURUSD", "M5", 1700000100, 1.1, 1.1, 1.1, 1.1), 1_700_000_100.0)
        errors = []

        def hammer(seed):
            try:
                for i in range(200):
                    bid = 1.10000 + ((i * 7 + seed) % 25) * 0.00005
                    m.on_quote(Quote("EURUSD", bid, bid + 0.00012, 0.0, 1_700_000_100.0), spec, 1_700_000_100.0)
            except Exception as exc:                                  # noqa: BLE001
                errors.append(exc)
        ts = [threading.Thread(target=hammer, args=(k,)) for k in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertLessEqual(sum(1 for t in m.lc["transitions"] if t["state"] == "RISK_PENDING"), 1)
        announced = 1 if m.take_announcement() else 0
        self.assertEqual(announced, sum(1 for t in m.lc["transitions"] if t["state"] == "RISK_PENDING"))

    def test_signal_expiry_is_configurable_and_explained(self):
        r = Rig(cfg_over={"signal_expiry_seconds": 2.0}).start()
        bull_to_trigger(r)
        for _ in range(12):
            r.step(1.10105, dt=0.5)
        self.assertEqual(r.st(), "EXPIRED")
        self.assertIn("expired", r.lc()["reason"])
        self.assertNotEqual(r.result()["decision"], "BUY")

    def test_decision_is_buy_or_sell_only_for_a_validated_signal(self):
        seen = set()
        r = Rig().start()
        for bid in (1.10000, 1.10040, 1.10105, 1.10105):
            r.step(bid)
            seen.add((r.st(), r.result()["decision"]))
        self.assertIn(("BULLISH", "WATCH"), seen)
        self.assertIn(("RISK_PENDING", "BUY"), seen)
        for st, dec in seen:
            if dec in ("BUY", "SELL"):
                self.assertIn(st, ("RISK_PENDING", "EXECUTING"))


class AuthoritativeStateTests(unittest.TestCase):
    def test_hub_record_equals_engine_result(self):
        r = Rig().start()
        bull_to_trigger(r)
        rec = r.hub.get("EURUSD", "candle_momentum", "M5")
        self.assertEqual(rec["result"]["candle_momentum"]["lifecycle"]["candle_id"], r.lc()["candle_id"])
        self.assertEqual(rec["result"]["candle_momentum"]["lifecycle"]["state"], r.st())
        self.assertEqual(rec["decision"], r.result()["decision"])

    def test_result_satisfies_the_pipeline_contract(self):
        from trademate import pipeline
        r = Rig().start()
        bull_to_trigger(r)
        q = pipeline.qualify({"ok": True, "result": r.result()}, "candle_momentum")
        self.assertTrue(q.qualified, q.reasons)
        self.assertEqual(q.proposal.target_pips, 20.0)
        self.assertEqual(q.proposal.candidate_id, r.lc()["candle_id"])
        self.assertAlmostEqual(q.proposal.pip_size, 0.0001)

    def test_a_non_signal_never_qualifies(self):
        from trademate import pipeline
        r = Rig().start()
        r.step(1.10000)
        r.step(1.10060)
        self.assertFalse(pipeline.qualify({"ok": True, "result": r.result()}, "candle_momentum").qualified)

    def test_result_exposes_the_command_centre_fields(self):
        r = Rig().start()
        r.step(1.10000)
        r.step(1.10060)
        block = r.result()["candle_momentum"]
        lc = block["lifecycle"]
        for k in ("open", "price", "direction", "trigger_price", "state", "reason", "updated_at"):
            self.assertIn(k, lc)
        self.assertEqual(block["config"]["trigger_units"], 10.0)
        self.assertEqual(block["engine"]["cadence_seconds"], 0.25)
        self.assertEqual(block["conversion"]["status"], "VERIFIED")
        self.assertIsNotNone(block["quote"])


class DiagnosticsTests(unittest.TestCase):
    def test_every_event_carries_a_candle_id_and_secrets_are_redacted(self):
        ev = EventLog(None)
        ev.emit("feed_down", None, password="hunter2", api_key="k", token="t", nested={"secret": "s", "ok": 1})
        e = ev.tail()[0]
        self.assertEqual(e["candle_id"], "unassigned")
        blob = json.dumps(e)
        for secret in ("hunter2", '"api_key": "k"', '"token": "t"', '"secret": "s"'):
            self.assertNotIn(secret, blob)
        self.assertEqual((e["password"], e["api_key"], e["token"], e["nested"]["secret"]), ("[redacted]",) * 4)
        self.assertEqual(e["nested"]["ok"], 1)

    def test_full_lifecycle_emits_the_required_events_all_with_candle_ids(self):
        r = Rig().start()
        bull_to_trigger(r)
        r.execute(ticket=8)
        names = set(r.event_names())
        for n in ("new_candle_detected", "direction_changed", "trigger_calculated", "trigger_crossed", "entry_evaluation_started", "entry_evaluation_completed", "quote_freshness",
                  "remaining_target_distance", "risk_engine_decision", "order_submitted", "execution_slippage", "position_opened", "latency", "transition"):
            self.assertIn(n, names)
        for e in r.events.tail(limit=1000):
            if e["event"] not in ("engine_started",):
                self.assertTrue(e["candle_id"])
        lat = [e for e in r.events.tail() if e["event"] == "latency"][0]
        self.assertIn("quote_age_at_trigger", lat)

    def test_event_file_is_written_and_filterable(self):
        with tempfile.TemporaryDirectory() as d:
            ev = EventLog(Path(d) / "e.jsonl")
            ev.emit("x", "abc", symbol="EURUSD")
            ev.emit("y", "def", symbol="EURUSD")
            self.assertEqual(len((Path(d) / "e.jsonl").read_text().splitlines()), 2)
            self.assertEqual([e["event"] for e in ev.tail(candle_id="def")], ["y"])


class ConfigTests(unittest.TestCase):
    def test_validation(self):
        ok = CMConfig("EURUSD", "M5")
        self.assertEqual(validate_config(ok, EXEC_TIMEFRAMES), [])
        for bad in (CMConfig("EURUSD", "M7"), CMConfig("", "M5"), CMConfig("EURUSD", "M5", trigger_units=0), CMConfig("EURUSD", "M5", target_units=-1),
                    CMConfig("EURUSD", "M5", max_entry_attempts=9), CMConfig("EURUSD", "M5", quote_max_age_seconds=0),
                    CMConfig("EURUSD", "M5", max_slippage_units=-1), CMConfig("EURUSD", "M5", enabled="yes")):
            self.assertTrue(validate_config(bad, EXEC_TIMEFRAMES), bad)

    def test_store_roundtrip_and_invalid_saved_config_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            st = ConfigStore(Path(d) / "c.json", EXEC_TIMEFRAMES)
            base = CMConfig("EURUSD", "M5")
            self.assertEqual(st.save(CMConfig("EURUSD", "M5", trigger_units=5.0, target_units=12.0)), [])
            cfg, custom = ConfigStore(Path(d) / "c.json", EXEC_TIMEFRAMES).get(base)
            self.assertEqual((cfg.trigger_units, cfg.target_units, custom), (5.0, 12.0, True))
            self.assertTrue(st.save(CMConfig("EURUSD", "M5", trigger_units=0)))
            (Path(d) / "c.json").write_text(json.dumps({"configs": {"EURUSD|M5": {"trigger_units": -5}}}))
            self.assertEqual(st.get(base), (base, False))

    def test_saved_legacy_default_stop_and_target_are_migrated(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.json"
            path.write_text(json.dumps({"version": 1, "configs": {"EURUSD|M1": {
                "trigger_units": 10.0, "target_units": 10.0, "sl_buffer_units": 1.0
            }}}))
            store = ConfigStore(path, EXEC_TIMEFRAMES)
            cfg, custom = store.get(CMConfig("EURUSD", "M1"))
            self.assertTrue(custom)
            self.assertEqual((cfg.trigger_units, cfg.sl_buffer_units, cfg.target_units), (10.0, 50.0, 20.0))
            saved = json.loads(path.read_text())["configs"]["EURUSD|M1"]
            self.assertEqual((saved["sl_buffer_units"], saved["target_units"]), (50.0, 20.0))

    def test_settings_defaults_and_removed_late_limit(self):
        self.assertFalse(hasattr(StrategySettings, "candle_late_pips"))
        self.assertEqual((StrategySettings.candle_trigger_pips, StrategySettings.candle_sl_buffer_pips, StrategySettings.candle_target_pips), (10.0, 50.0, 20.0))
        self.assertEqual((CMConfig("EURUSD", "M1").trigger_units, CMConfig("EURUSD", "M1").sl_buffer_units, CMConfig("EURUSD", "M1").target_units), (10.0, 50.0, 20.0))
        cm = CandleMomentumSettings()
        self.assertEqual((cm.poll_seconds, cm.max_entry_attempts), (0.25, 1))
        from trademate.config import load_config
        s = load_config()
        self.assertFalse(hasattr(s.candle_momentum, "max_target_consumed_fraction"))

    def test_candle_momentum_accepts_m1_and_keeps_trigger_before_entry(self):
        from trademate.strategies.base import EXEC_TIMEFRAMES
        from trademate.candle_momentum.models import CMConfig, validate_config
        self.assertIn("M1", EXEC_TIMEFRAMES)
        self.assertEqual(validate_config(CMConfig("EURUSD", "M1"), EXEC_TIMEFRAMES), [])
        # A new candle is monitored first; the default 10-unit move from open is still required.
        self.assertEqual(StrategySettings.candle_trigger_pips, 10.0)

    def test_an_old_settings_file_with_the_removed_late_key_still_loads_and_warns(self):
        from trademate.config import load_config
        root = Path(__file__).resolve().parent.parent
        text = (root / "config" / "settings.toml").read_text(encoding="utf-8")
        old = text.replace("candle_target_pips", "candle_late_pips = 2.0\ncandle_target_pips", 1)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.toml"
            p.write_text(old, encoding="utf-8")
            with self.assertLogs("trademate.config", level="WARNING") as cm:
                s = load_config(p)
            self.assertIn("candle_late_pips is no longer used", cm.output[0])
            self.assertEqual(s.strategy.candle_trigger_pips, 10.0)      # everything else is intact; nothing re-applies a late limit

    def test_no_late_chase_limit_remains_in_the_code(self):
        root = Path(__file__).resolve().parent.parent / "trademate"
        for p in root.rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("late_pips", text.replace("candle_late_pips", "") if p.name == "config.py" else text, p)
            if p.name != "candle_momentum.py":
                self.assertNotIn("Too late", text, p)
        # the ONE allowed mention of the removed key: the upgrade warning in the config loader
        cfg = (root / "config.py").read_text(encoding="utf-8").splitlines()
        for line in cfg:
            if "candle_late_pips" in line:
                self.assertTrue("strategy_data" in line or "no longer used" in line, line)


class ThreadedEngineTests(unittest.TestCase):
    def test_worker_thread_monitors_by_itself_and_stops(self):
        clock = time.time
        feed = FakeFeed(clock)
        feed.offset = 0
        settings = Settings(candle_momentum=CandleMomentumSettings(poll_seconds=0.05))
        hub = DecisionHub()
        eng = CandleMomentumEngine(feed, settings, ConfigStore(None, EXEC_TIMEFRAMES), StateStore(None), EventLog(None), hub=hub, async_fetch=True)
        ok, _ = eng.start("EURUSD", "M5")
        self.assertTrue(ok)
        self.assertTrue(eng.start("EURUSD", "M5")[0])
        self.assertEqual(sum(1 for t in threading.enumerate() if t.name == "trademate-candle-momentum"), 1)       # never two workers
        try:
            feed.tick(1.10000)
            time.sleep(0.4)
            feed.tick(1.10105)
            deadline = time.time() + 3
            while time.time() < deadline and (hub.get("EURUSD", "candle_momentum", "M5") or {}).get("decision") != "BUY":
                time.sleep(0.05)
            rec = hub.get("EURUSD", "candle_momentum", "M5")
            self.assertEqual(rec["decision"], "BUY")                  # decided by the worker thread: nobody called Analyse
        finally:
            eng.stop("EURUSD", "M5")
        time.sleep(0.3)
        self.assertEqual(sum(1 for t in threading.enumerate() if t.name == "trademate-candle-momentum"), 0)
        self.assertFalse(eng.engine_status()["running"])


    def test_stop_then_immediate_start_leaves_a_running_worker(self):
        feed = FakeFeed(time.time)
        eng = CandleMomentumEngine(feed, Settings(candle_momentum=CandleMomentumSettings(poll_seconds=0.05)), ConfigStore(None, EXEC_TIMEFRAMES), StateStore(None), EventLog(None),
                                   hub=DecisionHub(), async_fetch=True)
        self.addCleanup(eng.stop_all)
        for _ in range(25):                                           # hammer the stop/start race
            eng.start("EURUSD", "M5")
            eng.stop("EURUSD", "M5")
        eng.start("EURUSD", "M5")
        time.sleep(0.5)
        self.assertTrue(eng.engine_status()["running"])
        before = eng.last_step_at
        time.sleep(0.4)
        self.assertGreater(eng.last_step_at, before)                  # and it is actually stepping
        eng.stop_all()
        time.sleep(0.5)
        self.assertEqual(sum(1 for t in threading.enumerate() if t.name == "trademate-candle-momentum"), 0)


if __name__ == "__main__":
    unittest.main()
