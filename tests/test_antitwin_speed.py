"""Anti-Twin execution speed fix: trigger modes, fast lane, three-strategy independence and an end-to-end run on the EA-emulating bridge.

    python3 -m unittest tests.test_antitwin_speed -v

The 12 required scenarios (spec section 8) live where their mechanism lives:
 1 BUY reverses on first adverse tick      -> AntiTwinTriggerTest.test_the_first_adverse_tick_switches_without_waiting_for_a_return (+ here)
 2 SELL reverses                           -> AntiTwinTriggerTest.test_sell_moves_adversely_and_is_reversed_to_a_buy
 3 recursive BUY->SELL->BUY                -> AntiTwinTriggerTest.test_every_twin_has_its_own_entry_reference_and_the_chain_continues
 4 delayed MT5 execution                   -> EndToEndTest (EA-emulated bridge, snapshot 1 s / poll 200 ms / broker delay)
 5 rejected reversal                       -> AntiTwinTriggerTest.test_a_rejected_twin_is_retried_a_bounded_number_of_times_then_abandoned
 6 partial fill                            -> AntiTwinTriggerTest.test_twin_uses_the_actual_filled_volume_of_a_partial_fill
 7 emergency stop priority                 -> AntiTwinTriggerTest.test_emergency_stop_overrides_everything (+ between close and open)
 8 OFF                                     -> AntiTwinTriggerTest.test_disabled_anti_twin_never_acts, ...switching_it_off_between_the_close_and_the_open
 9 duplicate ticks / no double reversal    -> AntiTwinTriggerTest.test_duplicate_reversal_is_prevented (+ lane here)
10 reconnect / restart recovery            -> AntiTwinTriggerTest.test_a_restart_while_the_open_was_in_flight_never_resends
11 Risk Engine refusal                     -> AntiTwinTriggerTest.test_risk_engine_refusal_means_no_twin_and_a_known_flat_state
12 three strategies independent            -> ThreeStrategyIndependenceTest (here) + tests/test_rapid_fire.py TestAntiTwinEntryReturn
"""

import threading
import time
import unittest
from types import SimpleNamespace

from tests.test_bull_bear import ATRig
from trademate import antitwin_rule as rule
from trademate.antitwin_lane import AntiTwinLane

TICK = 0.00001


class RuleModesTest(unittest.TestCase):
    def test_active_mode_is_adverse_move_and_modes_are_explicit(self):
        self.assertEqual(rule.ACTIVE_MODE, rule.ADVERSE_MOVE)
        self.assertEqual(set(rule.MODES), {"adverse_move", "break_even_return"})
        with self.assertRaises(ValueError):
            rule.new_state("buy", 1.1, mode="both")

    def test_adverse_mode_fires_on_first_tick_beyond_spread_without_ever_being_in_profit(self):
        s = rule.new_state("buy", 1.1000)
        self.assertFalse(rule.observe(s, 1.09988, 1.10000, TICK))            # opening spread only
        self.assertTrue(rule.observe(s, 1.09986, 1.09998, TICK))             # one tick worse than where it started
        self.assertFalse(rule.observe(s, 1.0900, 1.0901, TICK))              # one reversal per state

    def test_break_even_mode_is_separate_and_not_combined(self):
        s = rule.new_state("buy", 1.1000, mode=rule.BREAK_EVEN_RETURN)
        self.assertFalse(rule.observe(s, 1.0990, 1.0991, TICK))              # adverse move alone does nothing in this mode
        be = rule.new_state("buy", 1.1000)
        self.assertFalse(rule.observe(be, 1.1005, 1.1006, TICK))             # profit alone does nothing in the active mode
        self.assertFalse(rule.observe(be, 1.1000, 1.1001, TICK))             # and neither does the return to break-even

    def test_sell_uses_the_ask_and_unusable_quotes_prove_nothing(self):
        s = rule.new_state("sell", 1.1000)
        self.assertFalse(rule.observe(s, None, None, TICK))
        self.assertFalse(rule.observe(s, 1.1010, 1.1000, TICK))              # crossed
        self.assertFalse(rule.observe(s, 1.0999, 1.1001, TICK))              # ask exactly at the opening level + spread
        self.assertTrue(rule.observe(s, 1.1000, 1.10022, TICK))
        self.assertFalse(rule.observe(rule.new_state("buy", 1.1), 1.0, 1.0001, 0))   # no tick size: cannot tell spread noise from movement

    def test_old_state_without_a_mode_follows_the_active_mode(self):
        s = {"side": "buy", "entry": 1.1, "sign": 1, "spread0": None, "up": False, "down": False, "lo": None, "hi": None, "triggered": False}
        self.assertTrue(rule.observe(s, 1.0990, 1.0991, TICK))
        self.assertEqual(s["mode"], rule.ADVERSE_MOVE)

    def test_latency_summary_reports_only_measured_values(self):
        lat = {"detect": 10.0, "close_sent": 10.01, "close_done": 10.2, "open_sent": 10.201, "open_done": 10.5, "quote_age": 0.9, "close_price": 1.0998}
        r = rule.latency_record(lat, side="buy", entry=1.1000, outcome="twin_opened")
        self.assertAlmostEqual(r["detect_to_open_ms"], 500.0, 1)
        self.assertLess(r["closed_vs_entry"], 0)                              # a loss is reported as a loss, never as break-even
        self.assertIsNotNone(rule.latency_summary([r])["note"])               # old quote -> advice on the EA inputs
        self.assertIsNone(rule.latency_record({}, side="buy", entry=1.1, outcome="x")["detect_to_open_ms"])


class LaneTest(unittest.TestCase):
    def build(self):
        r = ATRig()
        feed = SimpleNamespace(quote=lambda s: r.q, open_positions=lambda: list(r.positions), closed_info=lambda t: None)
        r.q = r.quote(1.1000)
        r.exec.last_quote = r.q
        lane = AntiTwinLane(lambda: r.at, feed, lambda s, tf: (r.SPEC, r.cfg), lambda q, cfg: True, None, clock=r.clock)
        return r, lane

    def test_lane_reverses_on_the_first_adverse_quote_in_one_iteration(self):
        r, lane = self.build()
        self.assertTrue(lane.run_once())
        self.assertEqual(r.exec.calls, [])
        r.q = r.quote(1.0997)
        r.exec.last_quote = r.q
        lane.run_once()
        self.assertEqual(r.exec.names(), ["close", "open"])
        self.assertEqual(r.exec.calls[1][1:3], ("sell", 0.03))

    def test_repeated_identical_quotes_never_reverse_twice(self):
        r, lane = self.build()
        r.q = r.quote(1.0997)
        r.exec.last_quote = r.q
        for _ in range(5):
            lane.run_once()
        self.assertEqual(r.exec.names(), ["close", "open"])

    def test_lane_is_idle_without_a_leg_and_off_does_nothing(self):
        r, lane = self.build()
        r.cfg = r.cfg.__class__(**{**r.cfg.__dict__, "anti_twin_enabled": False})
        r.q = r.quote(1.0990)
        lane.run_once()
        self.assertEqual(r.exec.calls, [])

    def test_lane_thread_starts_stops_and_restarts(self):
        r, lane = self.build()
        stop = threading.Event()
        lane.start(stop)
        r.q = r.quote(1.0997)
        r.exec.last_quote = r.q
        deadline = time.time() + 3
        while len(r.exec.calls) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(r.exec.names(), ["close", "open"])
        stop.set()
        time.sleep(0.15)
        self.assertFalse(lane.status()["running"])
        stop2 = threading.Event()
        lane.start(stop2)
        time.sleep(0.15)
        self.assertTrue(lane.status()["running"])
        stop2.set()


class ThreeStrategyIndependenceTest(unittest.TestCase):
    def test_each_strategy_owns_its_ledger_and_switch(self):
        a, b = ATRig(), ATRig()                                            # two independent AntiTwin instances (Candle Momentum / Bull-Bear ledgers)
        b.cfg = b.cfg.__class__(**{**b.cfg.__dict__, "anti_twin_enabled": False})
        a.tick(1.0997)
        b.tick(1.0997)
        self.assertEqual(a.exec.names(), ["close", "open"])
        self.assertEqual(b.exec.calls, [])
        self.assertIsNot(a.at, b.at)

    def test_rapid_fire_has_its_own_switch(self):
        from trademate.rapid_fire import models
        self.assertTrue(hasattr(models.RFConfig, "__dataclass_fields__"))
        self.assertTrue(any("anti_twin" in f for f in models.RFConfig.__dataclass_fields__))


class EndToEndTest(unittest.TestCase):
    """The real adapter + Executor/Risk gate + AntiTwin + lane against the EA-emulating bridge (tests/antitwin_latency.py)."""

    def test_reversal_with_delayed_mt5_execution_closes_first_then_opens_equal_volume(self):
        from tests.antitwin_latency import Harness
        import random
        h = Harness(snapshot_ms=100, poll_ms=100, broker_ms=60)
        try:
            r = h.one_run(random.Random(3), "buy")
            self.assertIsNotNone(r, "no reversal within 15 s")
            self.assertEqual(r["outcome"], "twin_opened")
            self.assertEqual(r["opens"], 2)                                    # the original + exactly one twin
            self.assertLess(r["closed_vs_entry"], 0)
            self.assertIsNotNone(r["detect_to_open_ms"])
            self.assertGreaterEqual(r["detect_to_open_ms"], r["detect_to_close_ms"])
            pos = [p for p in h.feed.open_positions()]
            self.assertEqual([(p.direction, p.volume) for p in pos], [("sell", 0.01)])
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
