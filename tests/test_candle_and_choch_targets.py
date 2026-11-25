"""Pips, lot size, Candle Momentum, CHoCH fixed target, decision hub, setup ledger, Autopilot rules.

    python3 -m unittest tests.test_candle_and_choch_targets -v

Everything here is pure or uses in-memory fakes: no MT5, no network, no order can be sent.
"""

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from tests.strategy_fixtures import PARAMS, SEC, forming_candle, h1_up, m5_choch_down, m5_choch_up, prepared, CFG, CLOCK
from trademate.autopilot import Autopilot, check_stop, validate_stop_config
from trademate.decision_hub import DecisionHub
from trademate.lotsize import LotStore, validate_volume
from trademate.pips import min_stop_problem, resolve_pip, snap_to_tick, target_price
from trademate.setups_ledger import SetupLedger
from trademate.strategies.base import StrategyParams, timeframes_for
from trademate.strategies.registry import evaluate_strategy_result

M5 = SEC["M5"]
PIP1 = StrategyParams(exec_tf="M5", pip_size=1.0, target_pips=10.0)      # 1 pip = 1.0 price unit: the fixtures live around 100-190


# ------------------------------------------------------------------------------------------------ pips
class PipsTest(unittest.TestCase):
    def test_forex_conventions(self):
        self.assertAlmostEqual(resolve_pip("EURUSD", 5, 0.00001, "Forex\\Majors").size, 0.0001)
        self.assertAlmostEqual(resolve_pip("USDJPY", 3, 0.001, "Forex\\Majors").size, 0.01)
        self.assertAlmostEqual(resolve_pip("EURUSD", 4, 0.0001).size, 0.0001)
        self.assertAlmostEqual(resolve_pip("XAUUSD", 2, 0.01).size, 0.1)

    def test_boom_is_not_a_forex_pip_and_is_refused_until_defined(self):
        p = resolve_pip("Boom 100 Index", 3, 0.001, "Synthetic\\Boom")
        self.assertFalse(p.ok)
        self.assertIn("pips.overrides", p.reason)
        q = resolve_pip("Boom 100 Index", 3, 0.001, "Synthetic\\Boom", {"Boom 100 Index": 1.0})
        self.assertTrue(q.ok)
        self.assertEqual(q.size, 1.0)
        self.assertNotEqual(q.size, resolve_pip("EURUSD", 5, 0.00001, "Forex").size)

    def test_bad_inputs_refused(self):
        self.assertFalse(resolve_pip("EURUSD", None, 0.0001).ok)
        self.assertFalse(resolve_pip("EURUSD", 5, 0).ok)
        self.assertFalse(resolve_pip("X", 5, 0.1, "", {"X": -1}).ok)

    def test_distance_and_snapping(self):
        pip = resolve_pip("EURUSD", 5, 0.00001, "Forex")
        self.assertAlmostEqual(target_price("buy", 1.10000, 100, pip, 0.00001), 1.11, places=6)
        self.assertAlmostEqual(target_price("sell", 1.10000, 100, pip, 0.00001), 1.09, places=6)
        self.assertEqual(snap_to_tick(1.10003, 0.0001, direction="up"), 1.1001)
        self.assertEqual(snap_to_tick(1.10009, 0.0001, direction="down"), 1.1)
        with self.assertRaises(ValueError):
            target_price("buy", 1.1, 0, pip)

    def test_min_stop_distance(self):
        self.assertIsNone(min_stop_problem("buy", 1.1, 1.09, 1.11, 0.00001, 10))
        self.assertIn("stop-loss", min_stop_problem("buy", 1.1, 1.09999, 1.11, 0.00001, 50))
        self.assertIn("take-profit", min_stop_problem("buy", 1.1, 1.09, 1.10001, 0.00001, 50))


# ------------------------------------------------------------------------------------------------ lot size
class LotTest(unittest.TestCase):
    def v(self, x, lo=0.01, hi=100, st=0.01, cap=None):
        return validate_volume(x, lo, hi, st, cap)

    def test_valid(self):
        self.assertTrue(self.v(0.07).ok)
        self.assertEqual(self.v("0.10").volume, 0.1)

    def test_below_min_above_max(self):
        self.assertEqual(self.v(0.005).code, "below_min")
        self.assertEqual(self.v(101).code, "above_max")
        self.assertEqual(self.v(5, cap=2).code, "above_cap")

    def test_step_is_exact_and_never_rounded(self):
        r = self.v(0.015)
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "off_step")
        self.assertIsNone(r.volume)
        self.assertIn("0.01 and 0.02", r.message)
        self.assertTrue(self.v(0.2, lo=0.2, hi=5, st=0.2).ok)
        self.assertFalse(self.v(0.3, lo=0.2, hi=5, st=0.2).ok)      # steps start at the minimum

    def test_invalid_input(self):
        for bad in (None, "", "abc", -1, 0, float("nan"), float("inf"), True, [], {}):
            self.assertEqual(self.v(bad).code, "invalid_input", bad)

    def test_unknown_rules_fail_closed(self):
        self.assertEqual(validate_volume(0.1, None, None, None).code, "unknown_rules")

    def test_store_roundtrip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            st = LotStore(Path(d) / "lots.json")
            self.assertIsNone(st.get("EURUSD"))
            st.set("EURUSD", 0.2)
            self.assertEqual(LotStore(Path(d) / "lots.json").get("EURUSD"), 0.2)
            (Path(d) / "lots.json").write_text("{not json")
            self.assertIsNone(st.get("EURUSD"))


# ------------------------------------------------------------------------------------------------ candle momentum
class CandleMomentumSnapshotHookTest(unittest.TestCase):
    """Candle Momentum v2 is decided by the live tick engine (tests/test_candle_momentum.py, tests/test_candle_momentum_e2e.py), not by candle snapshots.

    The eleven v1 tests that lived here asserted the REMOVED snapshot rules (including the hard-coded 2-pip "late" limit that caused the repeated late-entry
    warnings), so they were retired on purpose. Where each is covered now:
      bullish / bearish qualify, target a further 10 pips      -> test_candle_momentum: test_04, test_05, test_20_target_calculated_from_actual_fill
      not yet triggered is WATCH, flat candle has no direction -> test_01, test_02, test_03
      late entry rejected / overshoot then pullback is late    -> RETIRED (that was the bug); replaced by test_06b, test_19, test_no_hard_coded_two_pip_chase_limit
      reversal after the trigger cancels                       -> test_reversal_after_trigger_before_commitment_invalidates, test_08
      unknown pip is refused                                   -> test_unknown_pip_blocks_trading_without_guessing, test_crash100_*
      one per candle / new candle new id                       -> test_10, test_09, test_candle_id_is_stable_and_direction_independent
      missing data is not a signal                             -> test_15, test_16, test_candle_fetch_failure_blocks_publication
    What remains here is what is still true of the registry hook itself."""

    def test_the_snapshot_hook_never_produces_a_decision(self):
        rows = m5_choch_up()
        now = rows[-1][0] + M5 + 100
        series = prepared(forming_candle(rows, 100.0, 112.0, 99.0, 111.0), h1_up(), now)
        r = evaluate_strategy_result("candle_momentum", "TEST", series, PIP1, CFG, dict(CLOCK, now_server=now))
        self.assertEqual(r["decision"], "WAIT")
        self.assertIn("live engine", " ".join(r["reasons"]))
        self.assertFalse(any(c.get("execution_eligible") for c in r["setup_candidates"]))

    def test_uses_only_the_selected_timeframe(self):
        self.assertEqual(timeframes_for("M15", False), ("M15",))
        self.assertEqual(timeframes_for("M1", False), ("M1",))

    def test_the_stop_is_independent_of_the_target(self):
        from tests.fake_mt5_bridge import FakeClock, FakeFeed
        from trademate.candle_momentum.diagnostics import EventLog
        from trademate.candle_momentum.engine import CandleMomentumEngine
        from trademate.candle_momentum.models import CMConfig
        from trademate.candle_momentum.store import ConfigStore, StateStore
        from trademate.config import Settings
        from trademate.strategies.base import EXEC_TIMEFRAMES
        stops = []
        for target in (5.0, 10.0, 40.0):
            clock, store = FakeClock(), ConfigStore(None, EXEC_TIMEFRAMES)
            store.save(CMConfig("EURUSD", "M5", target_units=target))
            feed = FakeFeed(clock)
            eng = CandleMomentumEngine(feed, Settings(), store, StateStore(None), EventLog(None), clock=clock, threaded=False)
            eng.start("EURUSD", "M5")
            feed._candle("EURUSD", "M5")
            for bid in (1.10000, 1.10105):
                feed.tick(bid)
                clock.advance(0.25)
                eng.step()
            stops.append(eng.result("EURUSD", "M5")["stop_loss"]["price"])
        self.assertEqual(len(set(stops)), 1)


class ChochTargetTest(unittest.TestCase):
    def run_choch(self, rows, params):
        now = rows[-1][0] + M5 + 30
        return evaluate_strategy_result("choch", "TEST", prepared(rows, h1_up(), now), params, CFG, dict(CLOCK, now_server=now))

    def test_target_is_fixed_pips_not_a_multiple_of_the_stop(self):
        base = dict(exec_tf="M5", pip_size=0.01, max_extension_fraction=100.0)
        a = self.run_choch(m5_choch_up(), StrategyParams(target_pips=500.0, **base))
        b = self.run_choch(m5_choch_up(), StrategyParams(target_pips=900.0, **base))
        self.assertEqual(a["decision"], "BUY", a["reasons"])
        self.assertAlmostEqual(a["take_profit_1"]["price"] - a["entry"]["price"], 5.0)
        self.assertAlmostEqual(b["take_profit_1"]["price"] - b["entry"]["price"], 9.0)
        self.assertEqual(a["stop_loss"]["price"], b["stop_loss"]["price"])        # the stop did not move with the target
        self.assertNotAlmostEqual(a["risk_reward"]["rr_tp1"], 2.0)

    def test_sell_side(self):
        r = self.run_choch(m5_choch_down(), StrategyParams(exec_tf="M5", pip_size=0.01, target_pips=500.0, max_extension_fraction=100.0))
        self.assertIn(r["decision"], ("SELL", "WAIT", "WATCH"))

    def test_played_out_choch_is_not_an_entry(self):
        r = self.run_choch(m5_choch_up(), StrategyParams(exec_tf="M5", pip_size=0.01, target_pips=100.0))      # 100 pips = 1.0; the fixture already ran ~1.95
        self.assertEqual(r["decision"], "WAIT")
        self.assertTrue(any("played out" in x for x in r["reasons"]), r["reasons"])

    def test_no_pip_size_means_no_trade(self):
        r = self.run_choch(m5_choch_up(), StrategyParams(exec_tf="M5", target_pips=100.0, pip_reason="pip unknown"))
        self.assertNotIn(r["decision"], ("BUY", "SELL"))

    def test_legacy_structural_target_unchanged_without_pips(self):
        r = self.run_choch(m5_choch_up(), PARAMS)
        self.assertEqual(r["decision"], "BUY")
        self.assertIsNone(r["target"])


# ------------------------------------------------------------------------------------------------ hub
class HubTest(unittest.TestCase):
    def test_out_of_order_response_cannot_restore_an_older_decision(self):
        hub = DecisionHub()
        t1, t2 = hub.begin(), hub.begin()
        self.assertTrue(hub.publish(t2, "A", "choch", "M5", state="fresh", result={"decision": "BUY"}))
        self.assertFalse(hub.publish(t1, "A", "choch", "M5", state="fresh", result={"decision": "WAIT"}))
        self.assertEqual(hub.get("A", "choch", "M5")["decision"], "BUY")

    def test_keys_are_isolated(self):
        hub = DecisionHub()
        hub.publish(hub.begin(), "A", "choch", "M5", state="fresh", result={"decision": "BUY"})
        self.assertIsNone(hub.get("B", "choch", "M5"))
        self.assertIsNone(hub.get("A", "trend", "M5"))
        self.assertIsNone(hub.get("A", "choch", "H1"))

    def test_buy_then_wait_replaces_and_error_removes_decision(self):
        hub = DecisionHub()
        hub.publish(hub.begin(), "A", "choch", "M5", state="fresh", result={"decision": "BUY"})
        hub.publish(hub.begin(), "A", "choch", "M5", state="fresh", result={"decision": "WAIT"})
        self.assertEqual(hub.get("A", "choch", "M5")["decision"], "WAIT")
        hub.publish(hub.begin(), "A", "choch", "M5", state="error", message="boom")
        rec = hub.get("A", "choch", "M5")
        self.assertIsNone(rec["decision"])
        self.assertEqual(rec["state"], "error")

    def test_staleness_is_reported(self):
        t = [1000.0]
        hub = DecisionHub(stale_after=60, clock=lambda: t[0])
        hub.publish(hub.begin(), "A", "choch", "M5", state="fresh", result={"decision": "BUY"})
        t[0] += 61
        rec = hub.get("A", "choch", "M5")
        self.assertEqual(rec["state"], "stale")
        self.assertIsNone(rec["decision"])


class LedgerTest(unittest.TestCase):
    def test_one_claim_per_setup_even_across_restart_and_threads(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "l.json"
            led = SetupLedger(p)
            wins = []
            ts = [threading.Thread(target=lambda: wins.append(led.claim("S1"))) for _ in range(8)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(sum(wins), 1)
            self.assertTrue(SetupLedger(p).is_taken("S1"))
            self.assertFalse(SetupLedger(p).claim("S1"))
            self.assertFalse(led.claim(None))
