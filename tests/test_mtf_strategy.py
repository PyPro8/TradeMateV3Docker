"""Multi-timeframe Strategy Engine: context rules, qualification gates, end-to-end decisions and safety boundaries.

Deterministic fixtures, real analysis engines, no MT5, no orders.

    python3 -m unittest tests.test_mtf_strategy -v
"""

import ast
import math
import unittest
from pathlib import Path
from unittest import mock

from tests.mtf_fixtures import (DEFAULT_CFG, E, NOW, buy_rows, build_series, four, ladder, sell_rows, ta, zigzag_end)
from trademate.strategy import (BUY, SELL, WAIT, WATCH, DataRules, StrategyConfig, evaluate, pick_targets, resolve_context,
                                structural_stop)
from trademate.strategy import mtf
from trademate.strategy.mtf import BEAR, BULL, EARLY, ESTABLISHED, MIXED, NEUTRAL, R_CONFIRMED, R_DEVELOPING, R_POSSIBLE
from trademate.strategy.timeframes import STALE, prepare_series


class ContextRules(unittest.TestCase):
    def test_four_timeframes_aligned_bullish(self):
        c = resolve_context(four())
        self.assertEqual(c.state, "aligned_bullish")
        self.assertEqual((c.direction, c.permits), (BULL, "with_trend"))

    def test_four_timeframes_aligned_bearish(self):
        c = resolve_context(four(*[ta(tf, BEAR) for tf in ("H4", "H1", "M15", "M5")]))
        self.assertEqual(c.state, "aligned_bearish")
        self.assertEqual(c.direction, BEAR)

    def test_agreement_with_weak_structure_is_not_strong_alignment(self):
        c = resolve_context(four(m15=ta("M15", BULL, EARLY)))
        self.assertEqual(c.state, "agreeing_bullish")
        self.assertIn("M15 is early", c.summary)

    def test_higher_timeframe_bullish_with_lower_timeframe_bearish_pullback(self):
        c = resolve_context(four(m15=ta("M15", BEAR, EARLY, R_DEVELOPING, BEAR), m5=ta("M5", BEAR)))
        self.assertEqual(c.state, "htf_bullish_ltf_bearish_pullback")
        self.assertEqual((c.direction, c.permits), (BULL, "with_trend"))           # the idea stays WITH the higher timeframes

    def test_higher_timeframe_bearish_with_lower_timeframe_bullish_pullback(self):
        c = resolve_context(four(h4=ta("H4", BEAR), h1=ta("H1", BEAR), m15=ta("M15", BULL, EARLY), m5=ta("M5", BULL)))
        self.assertEqual(c.state, "htf_bearish_ltf_bullish_pullback")
        self.assertEqual(c.direction, BEAR)

    def test_possible_reversal_without_confirmation_is_a_directional_watch_candidate(self):
        c = resolve_context(four(h1=ta("H1", BULL, MIXED, R_POSSIBLE, BEAR), m15=ta("M15", BEAR), m5=ta("M5", BEAR)))
        self.assertEqual(c.state, "possible_reversal_unconfirmed")
        self.assertEqual((c.direction, c.permits), (BEAR, "developing_reversal"))

    def test_h1_against_h4_with_developing_reversal_is_a_watch_candidate(self):
        c = resolve_context(four(h4=ta("H4", BEAR), h1=ta("H1", BULL, EARLY, R_DEVELOPING, BULL)))
        self.assertEqual(c.state, "possible_reversal_unconfirmed")
        self.assertEqual((c.direction, c.permits), (BULL, "developing_reversal"))

    def test_h1_against_h4_without_reversal_evidence_remains_conflicting(self):
        c = resolve_context(four(h4=ta("H4", BEAR), h1=ta("H1", BULL, EARLY)))
        self.assertEqual(c.state, "conflicting")
        self.assertIsNone(c.direction)

    def test_confirmed_h1_reversal_against_h4_allows_a_counter_h4_idea_only_with_stricter_evidence(self):
        c = resolve_context(four(h4=ta("H4", BEAR), h1=ta("H1", BULL, ESTABLISHED, R_CONFIRMED, BULL)))
        self.assertEqual(c.state, "confirmed_reversal_vs_htf")
        self.assertEqual((c.direction, c.permits), (BULL, "counter_htf"))

    def test_no_higher_timeframe_direction_is_inconclusive(self):
        c = resolve_context(four(h4=ta("H4", NEUTRAL), h1=ta("H1", NEUTRAL)))
        self.assertEqual(c.state, "inconclusive")
        self.assertIsNone(c.direction)

    def test_it_is_not_a_majority_vote(self):
        """Three timeframes bullish (M5, M15, H1) against a bearish H4 is NOT a bullish call."""
        c = resolve_context(four(h4=ta("H4", BEAR)))
        self.assertEqual(c.state, "conflicting")
        self.assertIsNone(c.direction)

    def test_an_unanalysed_timeframe_does_not_erase_available_context(self):
        for tf in ("M5", "M15", "H1", "H4"):
            with self.subTest(missing=tf):
                an = four()
                an[tf] = ta(tf, analyzed=False, status="unavailable")
                c = resolve_context(an)
                self.assertNotEqual(c.state, "data_unavailable")
                self.assertEqual(c.direction, BULL)
                self.assertIn(tf, c.summary)

    def test_no_analysed_timeframes_are_data_unavailable(self):
        an = four()
        for tf in ("M5", "M15", "H1", "H4"):
            an[tf] = ta(tf, analyzed=False, status="unavailable")
        self.assertEqual(resolve_context(an).state, "data_unavailable")


class EndToEnd(unittest.TestCase):
    """Real engines on hand-checked fixtures."""

    def run_rows(self, rows, now=NOW, cfg=DEFAULT_CFG, **kw):
        return evaluate("EURUSD", build_series(rows, now), cfg, clock={"now_server": now}, **kw)

    def test_reference_scenario_is_a_confirmed_buy_with_all_levels(self):
        r = self.run_rows(buy_rows())
        self.assertEqual((r.decision, r.direction, r.setup_status), (BUY, BULL, "confirmed"))
        self.assertIsNotNone(r.selected_setup_family)
        self.assertTrue(any(c["status"] == "qualified" and c["execution_eligible"]
                            for c in r.setup_candidates))
        self.assertTrue(all(g["passed"] for g in r.gates), r.gates)
        entry, sl, tp1 = r.entry["price"], r.stop_loss["price"], r.take_profit_1["price"]
        self.assertLess(sl, entry)
        self.assertGreater(tp1, entry)
        self.assertGreaterEqual(r.risk_reward["rr_tp1"], DEFAULT_CFG.min_rr)
        self.assertAlmostEqual(r.risk_reward["risk"], entry - sl)
        self.assertLess(r.entry["zone"]["lower"], r.entry["zone"]["upper"])
        self.assertLess(sl, r.entry["zone"]["lower"] + 1e-9)             # the stop sits at or beyond the far edge of the zone
        if r.take_profit_2:
            self.assertGreater(r.take_profit_2["price"], tp1)

    def test_invalidation_stop_and_trigger_are_three_different_things(self):
        r = self.run_rows(buy_rows())
        inv, setup_inv, sl = r.structural_invalidation["level"], r.setup_invalidation["level"], r.stop_loss["price"]
        self.assertIsNotNone(inv)
        self.assertEqual(r.structural_invalidation["timeframe"], "H1")
        self.assertEqual(r.setup_invalidation["timeframe"], "M15")
        self.assertNotEqual(inv, sl)
        self.assertLess(inv, r.entry["price"])
        trigger = [e for e in r.timeframes["M5"]["events"] if e["direction"] == BULL][-1]
        self.assertNotIn(trigger["level"], (inv, sl))

    def test_mirrored_scenario_is_a_confirmed_sell(self):
        r = self.run_rows(sell_rows())
        self.assertEqual((r.decision, r.direction), (SELL, BEAR))
        self.assertGreater(r.stop_loss["price"], r.entry["price"])
        self.assertLess(r.take_profit_1["price"], r.entry["price"])

    def test_buy_proposal_is_for_the_risk_engine_and_carries_no_size(self):
        r = self.run_rows(buy_rows())
        p = r.to_trade_proposal()
        self.assertEqual((p.direction, p.entry, p.stop_loss, p.take_profit), ("buy", r.entry["price"], r.stop_loss["price"], r.take_profit_1["price"]))
        self.assertIsNone(p.volume)
        self.assertIsNone(p.strategy_risk_percent)
        s = self.run_rows(sell_rows()).to_trade_proposal()
        self.assertEqual(s.direction, "sell")

    def test_evaluating_never_calls_the_risk_or_execution_engines(self):
        with mock.patch("trademate.risk.engine.evaluate_trade") as risk, mock.patch("trademate.execution_gate.gate_order") as gate:
            self.run_rows(buy_rows()).to_trade_proposal()
        risk.assert_not_called()
        gate.assert_not_called()

    def test_default_lookback_of_twelve_candles_keeps_the_same_setup_at_watch(self):
        """The zone reaction is 22 M5 candles old: with the default 12-candle window it is too old to qualify, so the setup is WATCH."""
        r = self.run_rows(buy_rows(), cfg=StrategyConfig())
        self.assertEqual(r.decision, WATCH)
        self.assertIsNone(r.entry)
        self.assertIsNone(r.stop_loss)
        self.assertIsNone(r.take_profit_1)
        self.assertTrue(any("reached it" in x for x in r.reasons))

    def test_four_agreeing_timeframes_do_not_make_a_trade(self):
        rows = {"M5": zigzag_end(ladder(100, 14), 8, 300), "M15": zigzag_end(ladder(100, 14), 8, 900),
                "H1": zigzag_end(ladder(60, 14), 15, 3600), "H4": zigzag_end(ladder(60, 14), 15, 14400)}
        r = self.run_rows(rows)
        self.assertEqual(r.context["htf_direction"], BULL)
        self.assertEqual({r.timeframes[tf]["bias"] for tf in ("M5", "M15", "H1", "H4")}, {BULL})
        self.assertIn(r.decision, (WAIT, WATCH))
        self.assertNotEqual(r.setup_status, "confirmed")
        self.assertIsNone(r.entry)
        self.assertIsNone(r.stop_loss)

    def test_forming_trigger_candle_does_not_qualify_the_setup(self):
        """Cut the M5 series at the candle that makes the bullish CHoCH. While that candle is forming it must not count; once it has
        closed the very same data qualifies."""
        rows = buy_rows()
        trigger_open = next(c[0] for c in rows["M5"] if c[0] == E - 900)
        rows["M5"] = [c for c in rows["M5"] if c[0] <= trigger_open]
        forming = self.run_rows(rows, now=trigger_open + 100)
        self.assertTrue(forming.data["M5"]["forming_candle_present"])
        self.assertFalse(next(g for g in forming.gates if g["id"] == "trigger")["passed"])
        self.assertNotIn(forming.decision, (BUY, SELL))
        self.assertTrue(any("PROVISIONAL" in n for n in forming.notes))
        closed = evaluate("EURUSD", build_series(rows, E - 600), DEFAULT_CFG)
        self.assertFalse(closed.data["M5"]["forming_candle_present"])
        self.assertTrue(next(g for g in closed.gates if g["id"] == "trigger")["passed"])

    def test_stale_data_waits(self):
        r = self.run_rows(buy_rows(), now=NOW + 6 * 3600)
        self.assertEqual(r.decision, WAIT)
        self.assertNotEqual(r.context["state"], "data_unavailable")
        self.assertEqual(r.data["M5"]["status"], STALE)

    def test_missing_entry_or_context_data_preserves_candidates_but_never_qualifies(self):
        for tf in ("M5", "M15", "H1", "H4"):
            with self.subTest(missing=tf):
                rows = buy_rows()
                series = build_series(rows)
                series[tf] = prepare_series([], tf, NOW)
                r = evaluate("EURUSD", series, DEFAULT_CFG)
                expected = WATCH if tf in ("M5", "H1", "H4") else WAIT
                self.assertEqual(r.decision, expected)
                self.assertTrue(any(tf in g["detail"] for g in r.gates if g["id"] == "data"))
                self.assertIsNone(r.entry)
                self.assertIsNone(r.to_trade_proposal())
                if expected == WATCH:
                    self.assertTrue(r.setup_candidates)

    def test_missing_native_h4_data_makes_the_strategy_wait(self):
        series = build_series(buy_rows())
        series["H1"] = prepare_series([], "H1", NOW)
        series["H4"] = prepare_series([], "H4", NOW)
        r = evaluate("EURUSD", series, DEFAULT_CFG)
        self.assertEqual(r.decision, WAIT)
        self.assertEqual(r.data["H4"]["status"], "unavailable")

    def test_insufficient_history_waits(self):
        rows = buy_rows()
        rows["M5"] = rows["M5"][-20:]
        r = self.run_rows(rows)
        self.assertEqual(r.decision, WATCH)
        self.assertEqual(r.data["M5"]["status"], "insufficient")
        self.assertIsNone(r.to_trade_proposal())

    def test_degraded_data_never_produces_a_confirmed_setup(self):
        rows = buy_rows()
        rows["M15"] = [x for x in rows["M15"] if x[0] != rows["M15"][-3][0]]          # a missing recent M15 candle
        r = self.run_rows(rows)
        self.assertEqual(r.data["M15"]["status"], "degraded")
        self.assertNotIn(r.decision, (BUY, SELL))
        self.assertEqual(r.decision, WATCH)
        self.assertIsNone(r.entry)

    def test_unknown_clock_never_produces_a_confirmed_setup(self):
        r = evaluate("EURUSD", build_series(buy_rows(), None), DEFAULT_CFG)
        self.assertNotIn(r.decision, (BUY, SELL))

    def test_out_of_order_and_duplicate_candles_block_confirmation(self):
        rows = buy_rows()
        m5 = list(rows["M5"])
        m5[-10], m5[-11] = m5[-11], m5[-10]
        rows["M5"] = m5
        r = self.run_rows(rows)
        self.assertNotIn(r.decision, (BUY, SELL))
        rows = buy_rows()
        clash = list(rows["M5"][-5])
        clash[2] += 1
        rows["M5"] = rows["M5"] + [clash]
        self.assertNotIn(self.run_rows(rows).decision, (BUY, SELL))


class Gates(unittest.TestCase):
    def test_pure_stop_helper_rejects_a_stop_on_the_wrong_side(self):
        self.assertEqual(structural_stop(BULL, 100.0, 98.0, 0.5), (97.5, ""))
        self.assertEqual(structural_stop(BEAR, 100.0, 102.0, 0.5), (102.5, ""))
        for d, entry, raw in ((BULL, 100.0, 100.0), (BULL, 100.0, 101.0), (BEAR, 100.0, 100.0), (BEAR, 100.0, 99.0)):
            sl, why = structural_stop(d, entry, raw)
            self.assertIsNone(sl, (d, entry, raw))
            self.assertTrue(why)
        for bad in (math.nan, math.inf, None):
            self.assertIsNone(structural_stop(BULL, bad, 98.0)[0])
            self.assertIsNone(structural_stop(BULL, 100.0, bad)[0])

    def test_targets_skip_levels_that_do_not_pay_the_minimum_reward_to_risk(self):
        levels = [(101.0, "near"), (104.0, "mid"), (110.0, "far"), (95.0, "behind entry")]
        tp1, tp2, skipped = pick_targets(BULL, 100.0, 98.0, levels, 1.5)         # risk 2 -> need >= 3
        self.assertEqual((tp1["price"], tp2["price"]), (104.0, 110.0))
        self.assertEqual([s["price"] for s in skipped], [101.0])
        self.assertEqual(pick_targets(BEAR, 100.0, 102.0, [(99.0, "x")], 1.5)[0], None)
        self.assertIsNone(pick_targets(BULL, 100.0, 98.0, [], 1.5)[0])

    def test_a_setup_with_no_valid_structural_stop_stays_at_watch(self):
        """Degenerate case: the zone starts at the entry and the last candle closed on its own low, so the structural extreme equals the
        entry and a stop would have no distance. No stop is invented."""
        an = self.synthetic_setup()
        an["M15"].zones[0].update(lower=100.0, upper=101.0)
        an["M5"].rows = ((1000.0, 101, 101.5, 100.5, 101.0), (1300.0, 101.0, 101.2, 100.0, 100.8), (1600.0, 100.0, 100.0, 100.0, 100.0))
        an["M5"].last_close = 100.0
        r = evaluate("EURUSD", None, StrategyConfig(), analyses=an)
        stop = next(g for g in r.gates if g["id"] == "stop_loss")
        self.assertTrue(next(g for g in r.gates if g["id"] == "trigger")["passed"])
        self.assertFalse(stop["passed"])
        self.assertIn("No valid structural stop", stop["detail"])
        self.assertEqual(r.decision, WATCH)
        self.assertIsNone(r.stop_loss)
        self.assertIsNone(r.entry)
        self.assertIsNone(r.take_profit_1)

    def test_no_zone_means_wait_with_the_missing_evidence_named(self):
        an = self.synthetic_setup()
        an["M15"].zones = []
        r = evaluate("EURUSD", None, StrategyConfig(), analyses=an)
        self.assertEqual(r.decision, WAIT)
        self.assertTrue(any("M15 zone" in m for m in r.missing_evidence))

    def test_invalidated_zones_are_reported_as_an_invalidated_setup(self):
        an = self.synthetic_setup()
        an["M15"].zones, an["M15"].zones_invalidated_recent = [], 2
        r = evaluate("EURUSD", None, StrategyConfig(), analyses=an)
        self.assertEqual((r.decision, r.setup_status), (WAIT, "invalidated"))

    def test_no_target_with_enough_reward_keeps_watch(self):
        an = self.synthetic_setup(highs=[101.0])
        r = evaluate("EURUSD", None, StrategyConfig(), analyses=an)
        self.assertEqual(r.decision, WATCH)
        self.assertFalse(next(g for g in r.gates if g["id"] == "target")["passed"])

    def test_synthetic_setup_itself_qualifies(self):
        r = evaluate("EURUSD", None, StrategyConfig(), analyses=self.synthetic_setup())
        self.assertEqual(r.decision, BUY, r.gates)

    def test_counter_htf_idea_needs_an_established_m15_in_the_new_direction(self):
        an = self.synthetic_setup()
        an["H4"] = ta("H4", BEAR)
        an["H1"] = ta("H1", BULL, ESTABLISHED, R_CONFIRMED, BULL)
        an["M15"].strength = EARLY
        r = evaluate("EURUSD", None, StrategyConfig(), analyses=an)
        self.assertEqual(r.context["state"], "confirmed_reversal_vs_htf")
        self.assertNotIn(r.decision, (BUY, SELL))
        an["M15"].strength = ESTABLISHED
        self.assertEqual(evaluate("EURUSD", None, StrategyConfig(), analyses=an).decision, BUY)

    def test_possible_reversal_is_watch_and_never_a_trade(self):
        an = self.synthetic_setup()
        an["H1"] = ta("H1", BULL, MIXED, R_POSSIBLE, BEAR)
        an["M15"] = ta("M15", BEAR, EARLY, R_DEVELOPING, BEAR)
        an["M5"].bias = BEAR
        r = evaluate("EURUSD", None, StrategyConfig(), analyses=an)
        self.assertEqual((r.decision, r.setup_status, r.direction), (WATCH, "developing", BEAR))
        self.assertIsNone(r.entry)

    def test_conflicting_higher_timeframes_wait(self):
        r = evaluate("EURUSD", None, StrategyConfig(), analyses=four(h4=ta("H4", BEAR)))
        self.assertEqual((r.decision, r.setup_status), (WAIT, "inconclusive"))

    @staticmethod
    def synthetic_setup(highs=(110.0,)):
        """Bullish context, an M15 zone 98-99.5, an M5 reaction at the zone and a closed bullish M5 CHoCH after it. Entry 100, stop 97.x."""
        an = four()
        an["M15"].zones = [{"kind": "order_block", "id": "OB-T", "direction": BULL, "lower": 98.0, "upper": 99.5, "status": "confirmed",
                            "time": 1.0, "timeframe": "M15"}]
        an["M15"].swing_highs = [{"price": p, "time": 1.0, "classification": "HH"} for p in highs]
        an["M5"].rows = ((1000.0, 101, 101.5, 99, 99.2), (1300.0, 99.2, 99.4, 97.5, 98.0), (1600.0, 98.0, 100.2, 97.9, 100.0))
        an["M5"].last_close = 100.0
        an["M5"].events = [{"type": "CHOCH", "direction": BULL, "level": 99.0, "candle_time": 1600.0, "confirmed": True, "trend_before": BEAR}]
        an["M5"].bias = BULL
        return an


class ResultShapeAndLevels(unittest.TestCase):
    KEYS = {"symbol", "primary_execution_timeframe", "decision", "direction", "setup_status", "context", "timeframes", "data", "entry",
            "stop_loss", "take_profit_1", "take_profit_2", "risk_reward", "structural_invalidation", "setup_invalidation", "reversal",
            "market_regimes", "setup_candidates", "selected_setup_family", "gates", "reasons", "missing_evidence",
            "confirmation_evidence", "notes", "clock", "rule_id", "disclaimer"}

    def test_result_contains_every_required_section(self):
        for r in (evaluate("X", build_series(buy_rows()), DEFAULT_CFG), evaluate("X", build_series(buy_rows(), NOW + 99999), DEFAULT_CFG)):
            d = r.to_dict()
            self.assertTrue(self.KEYS <= set(d), self.KEYS - set(d))
            self.assertEqual(set(d["timeframes"]), {"M5", "M15", "H1", "H4"})
            self.assertEqual(set(d["data"]), {"M5", "M15", "H1", "H4"})
            self.assertEqual(d["primary_execution_timeframe"], "M5")
            self.assertIn("no win rate is implied", d["disclaimer"])

    def test_validation_snapshot_keeps_decision_evidence_without_claiming_an_outcome(self):
        snapshot = evaluate("X", build_series(buy_rows()), DEFAULT_CFG).to_dict()["validation_snapshot"]
        self.assertEqual(snapshot["schema"], "trademate.strategy.validation.v1")
        self.assertEqual(snapshot["decision"], BUY)
        self.assertFalse(snapshot["execution_linked"])
        self.assertIsNone(snapshot["execution_outcome"])
        self.assertIsNotNone(snapshot["proposed_levels"]["entry_reference"]["price"])
        self.assertIn("realised outcome", snapshot["unavailable_outcome_metrics"])

    def test_every_timeframe_reports_bias_data_status_and_freshness(self):
        d = evaluate("X", build_series(buy_rows()), DEFAULT_CFG).to_dict()
        for tf in ("M5", "M15", "H1", "H4"):
            self.assertIn(d["timeframes"][tf]["bias"], (BULL, BEAR, NEUTRAL))
            self.assertEqual(d["data"][tf]["status"], "ok")
            self.assertIsNotNone(d["data"][tf]["lag_seconds"])

    def test_no_trade_levels_are_invented_when_nothing_qualifies(self):
        d = evaluate("X", build_series(buy_rows(), NOW + 99999), DEFAULT_CFG).to_dict()
        for k in ("entry", "stop_loss", "take_profit_1", "take_profit_2", "risk_reward", "setup_invalidation"):
            self.assertIsNone(d[k], k)

    def test_invalidation_level_is_none_when_the_structure_has_no_protective_swing(self):
        d = evaluate("X", None, StrategyConfig(), analyses=four(h1=ta("H1", BULL), h4=ta("H4", BULL))).to_dict()
        self.assertIsNone(d["structural_invalidation"]["level"])
        self.assertIn("basis", d["structural_invalidation"])

    def test_reversal_conditions_name_real_levels_only(self):
        r = evaluate("X", build_series(buy_rows()), DEFAULT_CFG)
        lvl = r.structural_invalidation["level"]
        self.assertTrue(any(f"{lvl:g}" in c for c in r.reversal["conditions"]))
        self.assertEqual(r.reversal["stage"], "none")
        self.assertTrue(any("does not exist until it forms" in c for c in r.reversal["conditions"]))

    def analyse_h1(self, points):
        s = prepare_series(zigzag_end(points, 6, 3600), "H1", NOW, DataRules(min_candles={"H1": 10}))
        return mtf.analyze_timeframe(s, StrategyConfig())

    def test_a_closed_choch_without_follow_through_is_a_developing_reversal(self):
        a = self.analyse_h1([100, 110, 104, 120, 113, 130, 122, 140, 131, 118, 124])
        self.assertEqual(a.last_event["type"], "CHOCH")
        self.assertEqual((a.bias, a.strength), (BEAR, EARLY))
        self.assertEqual((a.reversal["stage"], a.reversal["toward"]), (R_DEVELOPING, BEAR))
        self.assertTrue(a.reversal["conditions"])

    def test_a_choch_followed_by_a_closed_bos_is_a_confirmed_reversal(self):
        a = self.analyse_h1([100, 110, 104, 120, 113, 130, 122, 140, 131, 118, 126, 116])
        self.assertEqual([e["type"] for e in a.events[-2:]], ["CHOCH", "BOS"])
        self.assertEqual((a.bias, a.strength), (BEAR, ESTABLISHED))
        self.assertEqual((a.reversal["stage"], a.reversal["toward"]), (R_CONFIRMED, BEAR))
        self.assertIsNotNone(a.invalidation["level"])

    def test_opposing_swings_in_an_uptrend_make_a_reversal_merely_possible(self):
        a = self.analyse_h1([100, 110, 104, 120, 113, 130, 122, 140, 131, 136, 133, 135, 134])   # BOS up, then lower highs; the low holds
        self.assertEqual((a.bias, a.strength), (BULL, MIXED))
        self.assertEqual((a.reversal["stage"], a.reversal["toward"]), (R_POSSIBLE, BEAR))
        self.assertTrue(any("merely possible" in e for e in a.reversal["evidence"]))
        self.assertIsNotNone(a.invalidation["level"])                       # the swing low whose close-through would start the reversal

    def test_config_is_validated(self):
        for bad in (dict(min_rr=0), dict(min_rr=math.nan), dict(sl_buffer=-1), dict(trigger_lookback=0), dict(swing_left=0)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                StrategyConfig(**bad).validate()


class Boundaries(unittest.TestCase):
    """The Strategy Engine proposes; the Risk Engine sizes and the Execution Engine executes. Source-level proof it cannot do either."""

    ROOT = Path(__file__).resolve().parent.parent / "trademate" / "strategy"
    ORDER_METHODS = {"place_order", "modify_position", "close_position", "place_pending_order", "cancel_pending_order", "modify_pending_order"}
    FORBIDDEN_MODULES = ("trademate.execution_gate", "trademate.journal", "trademate.pending", "trademate.broker.mt5_adapter",
                         "trademate.broker.mt5_commands", "trademate.risk.position_sizing", "trademate.risk.sl_calculator", "flask", "sqlite3",
                         "socket", "requests", "subprocess")

    def trees(self):
        for path in sorted(self.ROOT.glob("*.py")):
            yield path.name, ast.parse(path.read_text(encoding="utf-8"))

    def test_the_package_imports_no_execution_journal_sizing_or_web_code(self):
        found = {}
        for name, tree in self.trees():
            for node in ast.walk(tree):
                mods = [node.module] if isinstance(node, ast.ImportFrom) and node.module else [a.name for a in node.names] if isinstance(node, ast.Import) else []
                for m in mods:
                    found.setdefault(m, set()).add(name)
        for mod in found:
            for bad in self.FORBIDDEN_MODULES:
                self.assertFalse(mod == bad or mod.startswith(bad + "."), f"{mod} imported by {found[mod]}")
        # the only risk-engine code it touches: the proposal data class (mtf.py) and the broker-clock helper (service.py)
        risk = {m: n for m, n in found.items() if m.startswith("trademate.risk")}
        self.assertEqual(risk, {"trademate.risk.engine": {"mtf.py"}, "trademate.risk.account_state": {"service.py"}})

    def test_no_order_method_is_referenced_anywhere_in_the_package(self):
        for name, tree in self.trees():
            for node in ast.walk(tree):
                ident = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
                self.assertNotIn(ident, self.ORDER_METHODS, f"{name} references {ident}")

    def test_the_pure_engine_modules_do_not_import_the_broker_at_all(self):
        for name in ("mtf.py", "timeframes.py"):
            tree = ast.parse((self.ROOT / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn("broker", node.module, f"{name} imports {node.module}")


if __name__ == "__main__":
    unittest.main()
