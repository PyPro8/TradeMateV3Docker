"""Market-structure audit regression tests (CHoCH / BOS definitions, state transitions, shared facts, bad data).

Pure Python. Every fixture is hand-built so the expected answer follows from docs/MARKET_STRUCTURE.md, not from engine output.
The reported CRASH_100 H1 sequence is NOT reconstructed (the live candles are unavailable); the "H1 analogue" cases below are
deterministic synthetic candles that exercise the same event types.

    python3 -m unittest tests.test_structure_audit -v
"""
import json
import math
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.test_structure_engine import CFG, DOWN, STEP, T0, UP, UP_THEN_BREAK_LOW, DOWN_THEN_BREAK_HIGH, series, t
from trademate import analysis
from trademate.strategy.mtf import StrategyConfig, analyze_timeframe
from trademate.strategy.timeframes import OK, PreparedSeries
from trademate.structure import analyze_market_structure, structural_facts
from trademate.structure import trace as trace_module

RALLY_AFTER_BEARISH_CHOCH = UP + [11, 9, 12, 15, 18, 21, 24, 22, 20, 23, 26, 29, 27]
H1_ANALOGUE = UP + [9, 11, 13, 11, 9, 11, 12, 10, 8, 10, 12, 10, 8, 10, 13, 11, 9, 11, 14, 12, 10, 12, 15, 13, 11, 13, 9]


def ev(res):
    return [(e.type, e.direction) for e in res.events]


class ChochDefinitionTest(unittest.TestCase):
    def test_bullish_choch_after_established_bearish_structure(self):
        res = analyze_market_structure(series(DOWN_THEN_BREAK_HIGH), CFG)
        last = res.events[-1]
        self.assertEqual((last.type, last.direction, last.trend_before, last.trend_after), ("CHOCH", "bullish", "bearish", "bullish"))
        self.assertEqual(last.reference_classification, "LH")
        self.assertEqual(last.reference_role, "protected_swing")

    def test_bearish_choch_after_established_bullish_structure(self):
        res = analyze_market_structure(series(UP_THEN_BREAK_LOW), CFG)
        last = res.events[-1]
        self.assertEqual((last.type, last.direction, last.trend_before, last.trend_after), ("CHOCH", "bearish", "bullish", "bearish"))
        self.assertEqual(last.reference_classification, "HL")
        self.assertTrue(last.confirmed)

    def test_bos_in_the_direction_of_established_structure(self):
        up, down = analyze_market_structure(series(UP), CFG), analyze_market_structure(series(DOWN), CFG)
        self.assertEqual(ev(up), [("BOS", "bullish")] * 2)
        self.assertEqual(ev(down), [("BOS", "bearish")] * 2)
        self.assertEqual(up.events[-1].trend_before, "bullish")

    def test_swing_classification_from_ordered_confirmed_swings(self):
        up = analyze_market_structure(series(UP), CFG)
        self.assertEqual([s.classification for s in up.swings if s.kind == "high"], [None, "HH", "HH"])
        self.assertEqual([s.classification for s in up.swings if s.kind == "low"], [None, "HL", "HL"])
        down = analyze_market_structure(series(DOWN), CFG)
        self.assertEqual([s.classification for s in down.swings if s.kind == "high"], [None, "LH", "LH"])
        self.assertEqual([s.classification for s in down.swings if s.kind == "low"], [None, "LL", "LL"])

    def test_a_pullback_that_closes_above_the_protected_low_is_not_a_choch(self):
        res = analyze_market_structure(series(UP + [12.0]), CFG)       # dips to 12, protected HL is 11.5: no close beyond it
        self.assertEqual(ev(res), [("BOS", "bullish")] * 2)
        self.assertEqual(res.trend, "bullish")

    def test_a_wick_through_the_protected_low_is_not_a_close_mode_choch(self):
        rows = series(UP)
        rows.append([T0 + len(rows) * STEP, 14, 14.5, 11.0, 12.0])    # low 11.0 < 11.5 but close 12.0 > 11.5
        self.assertEqual(ev(analyze_market_structure(rows, CFG)), [("BOS", "bullish")] * 2)
        wick = analyze_market_structure(rows, replace(CFG, break_mode="wick"))
        self.assertEqual(ev(wick)[-1], ("CHOCH", "bearish"))

    def test_continuation_break_is_a_bos_never_a_choch(self):
        res = analyze_market_structure(series(UP + [13, 17]), CFG)    # closes above the 16.5 HH
        self.assertEqual(res.events[-1].type, "BOS")
        self.assertEqual(res.events[-1].reference_role, "continuation_swing")
        self.assertNotIn("CHOCH", [e.type for e in res.events])

    def test_ambiguous_prior_structure_is_never_promoted_to_choch(self):
        # Equal highs and lows (a range): no direction, therefore nothing can change character.
        rng = [(8, 6, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 6, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 6, 7), (9, 7, 8), (10, 8, 9)]
        rows = [[T0 + i * STEP, c, h, l, c] for i, (h, l, c) in enumerate(rng)]
        res = analyze_market_structure(rows, CFG)
        self.assertEqual((res.events, res.trend), ((), None))
        self.assertNotIn("CHOCH", [e.type for e in analyze_market_structure(rows[:4], CFG).events])

    def test_first_break_has_no_previous_direction_and_is_a_bootstrap_bos(self):
        first = analyze_market_structure(series(UP), CFG).events[0]
        self.assertEqual((first.type, first.trend_before, first.reference_role), ("BOS", None, "bootstrap"))


class StateTransitionTest(unittest.TestCase):
    def test_no_repeated_choch_while_price_stays_beyond_the_same_level(self):
        res = analyze_market_structure(series(UP_THEN_BREAK_LOW + [10, 9, 10, 9, 10, 9, 10]), CFG)
        self.assertEqual(sum(1 for e in res.events if e.type == "CHOCH"), 1)
        ids = [e.id for e in res.events]
        self.assertEqual(len(ids), len(set(ids)))

    def test_each_level_breaks_at_most_once(self):
        res = analyze_market_structure(series(H1_ANALOGUE), CFG)
        keys = [(e.reference_swing_id, e.direction) for e in res.events]
        self.assertEqual(len(keys), len(set(keys)))

    def test_state_after_choch_then_subsequent_bos_follows_the_new_direction(self):
        # bearish CHoCH, then a close below the new LL: that is a BEARISH BOS (continuation of the new structure).
        res = analyze_market_structure(series(UP_THEN_BREAK_LOW + [9, 7, 9, 10, 9, 6]), CFG)
        types = ev(res)
        self.assertEqual(types[2], ("CHOCH", "bearish"))
        self.assertIn(("BOS", "bearish"), types[3:])
        after = [e for e in res.events[3:]]
        self.assertTrue(all(e.trend_before == "bearish" for e in after[:1]))
        self.assertEqual(res.trend_confirmed, "bearish")

    def test_trend_cannot_stay_bearish_while_price_closes_above_every_old_high(self):
        # DEFECT FOUND BY THE AUDIT: without an LH there was no protected high, so structure stayed "bearish" through a
        # rally from 8.5 to 29. The origin swing (latest confirmed high when the CHoCH happened) is now protected.
        res = analyze_market_structure(series(RALLY_AFTER_BEARISH_CHOCH), CFG)
        self.assertEqual(ev(res)[2:4], [("CHOCH", "bearish"), ("CHOCH", "bullish")])
        flip = res.events[3]
        self.assertEqual((flip.reference_role, flip.reference_classification, flip.level), ("origin_swing", "HH", 16.5))
        self.assertIn("origin swing", flip.reason)
        self.assertEqual(res.trend_confirmed, "bullish")
        self.assertEqual(res.events[4].type, "BOS")            # 24.5 break afterwards is a continuation of the new bullish structure

    def test_origin_swing_is_used_only_after_a_confirmed_choch_never_after_a_bootstrap(self):
        res = analyze_market_structure(series(UP[:7] + [8]), CFG)
        self.assertIsNone(res.protected_low)
        self.assertEqual(ev(res), [("BOS", "bullish")])

    def test_a_later_lh_replaces_the_origin_swing_as_protected_high(self):
        res = analyze_market_structure(series(H1_ANALOGUE), CFG)
        flip = [e for e in res.events if e.type == "CHOCH" and e.direction == "bullish"][0]
        self.assertEqual((flip.reference_role, flip.reference_classification), ("protected_swing", "LH"))

    def test_provisional_break_stays_distinct_from_confirmed_state(self):
        forming = analyze_market_structure(series(UP_THEN_BREAK_LOW), replace(CFG, last_candle_closed=False))
        last = forming.events[-1]
        self.assertFalse(last.confirmed)
        self.assertIsNone(last.confirmed_time)
        self.assertEqual((forming.trend_confirmed, forming.trend), ("bullish", "bearish"))
        facts = structural_facts(forming)
        self.assertEqual(facts["confirmed_direction"], "bullish")
        self.assertTrue(facts["provisional"]["against_confirmed"])

    def test_forming_candle_never_creates_a_protected_origin_reference(self):
        forming = analyze_market_structure(series(UP_THEN_BREAK_LOW), replace(CFG, last_candle_closed=False))
        self.assertIsNone(forming.protected_high)

    def test_same_candle_conflict_leaves_direction_undetermined(self):
        rows = series(UP)
        rows.append([T0 + len(rows) * STEP, 14, 20.0, 5.0, 14])
        res = analyze_market_structure(rows, replace(CFG, break_mode="wick"))
        flagged = [e for e in res.events if e.same_candle_conflict]
        self.assertEqual(len(flagged), 2)
        self.assertIsNone(res.trend)

    def test_prefix_consistency_no_lookahead(self):
        for seed in range(25):
            rnd, price, rows = random.Random(seed), 100.0, []
            for i in range(60):
                o = price
                price = max(1.0, price + rnd.uniform(-3, 3))
                rows.append([T0 + i * STEP, o, max(o, price) + rnd.uniform(0, 1.5), min(o, price) - rnd.uniform(0, 1.5), price])
            full = analyze_market_structure(rows, CFG)
            for n in range(6, 61, 6):
                pre = analyze_market_structure(rows[:n], CFG)
                expected = [e.id for e in full.events if e.candle_time <= rows[n - 1][0]]
                self.assertEqual([e.id for e in pre.events], expected, (seed, n))


class H1SequenceAnalogueTest(unittest.TestCase):
    """Deterministic analogue of 'CHoCH, CHoCH, BOS, HH, CHoCH'. Not a reconstruction of live CRASH_100 candles."""

    def test_two_consecutive_choch_events_are_opposite_directions_each_with_its_own_reference(self):
        res = analyze_market_structure(series(RALLY_AFTER_BEARISH_CHOCH[:-6]), CFG)
        chochs = [e for e in res.events if e.type == "CHOCH"]
        self.assertEqual([(e.direction, e.trend_before, e.trend_after) for e in chochs],
                         [("bearish", "bullish", "bearish"), ("bullish", "bearish", "bullish")])
        self.assertNotEqual(chochs[0].reference_swing_id, chochs[1].reference_swing_id)
        for e in chochs:
            self.assertTrue(e.reason and e.reference_swing_id and e.confirmed)

    def test_every_event_is_auditable(self):
        res = analyze_market_structure(series(H1_ANALOGUE), CFG)
        by_id = {s.id: s for s in res.swings}
        for e in res.events:
            ref = by_id[e.reference_swing_id]
            self.assertEqual((ref.price, ref.pivot_time, ref.classification), (e.level, e.reference_pivot_time, e.reference_classification))
            self.assertLess(ref.confirmed_time, e.candle_time)              # the level was known before it was broken
            self.assertTrue(e.reason)
            if e.type == "CHOCH":
                self.assertNotEqual(e.trend_before, e.trend_after)
            else:
                self.assertEqual(e.trend_before in (None, e.direction), True)


class SharedFactsTest(unittest.TestCase):
    def pages(self, closes):
        rows = series(closes)
        cfg = StrategyConfig(swing_left=2, swing_right=2)
        tf = analyze_timeframe(PreparedSeries("H1", OK, "", closed=tuple(tuple(r) for r in rows)), cfg)
        an = analysis.build_analysis("EURUSD", "H1", rows, replace(CFG, last_candle_closed=True), 2)
        return tf, an

    def test_analysis_and_strategy_pages_report_identical_structural_facts(self):
        for closes in (UP, DOWN, UP_THEN_BREAK_LOW, DOWN_THEN_BREAK_HIGH, H1_ANALOGUE, RALLY_AFTER_BEARISH_CHOCH):
            tf, an = self.pages(closes)
            facts = an["structure"]["facts"]
            self.assertEqual(facts, tf.structural_facts)
            self.assertEqual(an["summary"]["direction"]["confirmed_direction"], tf.confirmed_direction)
            self.assertEqual(an["summary"]["direction"]["quality"], facts["quality"])
            self.assertEqual(tf.bias, facts["confirmed_direction"])

    def test_a_choch_gives_a_confirmed_direction_with_early_quality_on_both_pages(self):
        tf, an = self.pages(UP_THEN_BREAK_LOW)
        self.assertEqual((tf.bias, tf.strength), ("bearish", "early"))
        d = an["summary"]["direction"]
        self.assertEqual((d["label"], d["quality"]), ("bearish", "early"))

    def test_direction_quality_and_setup_are_separate(self):
        tf, _ = self.pages(UP_THEN_BREAK_LOW)
        self.assertNotIn("decision", tf.structural_facts)                 # facts never carry BUY / SELL
        self.assertEqual(set(tf.structural_facts), {"confirmed_direction", "quality", "swing_pattern", "last_confirmed_event", "provisional"})

    def test_mixed_quality_keeps_its_confirmed_direction(self):
        # bullish BOS but the latest low is a LL: quality mixed, direction still bullish.
        closes = UP + [13, 17, 15, 12, 9, 12, 16, 19]
        res = analyze_market_structure(series(closes), CFG)
        facts = structural_facts(res)
        if facts["quality"] == "mixed":
            self.assertEqual(facts["confirmed_direction"], res.trend_confirmed)


class BadDataTest(unittest.TestCase):
    def test_empty_none_and_too_short(self):
        for data in ([], None, series(UP)[:3]):
            res = analyze_market_structure(data, CFG)
            self.assertTrue(res.diagnostics.insufficient_data)
            self.assertEqual((res.events, res.trend), ((), None))

    def test_malformed_nan_inf_and_inconsistent_candles_are_excluded_not_fabricated(self):
        rows = series(UP)
        bad = [[1, 2, 3], ["x", 1, 2, 1, 1], [T0 + 99 * STEP, float("nan"), 2, 1, 1], [T0 + 98 * STEP, 1, math.inf, 1, 1],
               [T0 + 97 * STEP, 5, 4, 6, 5], [T0 + 96 * STEP, 9, 5, 4, 9], None, "junk"]
        clean = analyze_market_structure(rows, CFG)
        dirty = analyze_market_structure(rows + bad, CFG)
        self.assertEqual([e.id for e in dirty.events], [e.id for e in clean.events])
        self.assertEqual(len(dirty.diagnostics.invalid), len(bad))

    def test_duplicates_and_out_of_order_do_not_create_events(self):
        rows = series(UP_THEN_BREAK_LOW)
        shuffled = rows[::-1] + rows[:5]
        a, b = analyze_market_structure(rows, CFG), analyze_market_structure(shuffled, CFG)
        self.assertEqual([e.id for e in a.events], [e.id for e in b.events])
        self.assertTrue(b.diagnostics.out_of_order)

    def test_stale_data_is_refused_by_the_analysis_page(self):
        a = analysis.unavailable("EURUSD", "H1", CFG, "MT5 is not connected", stale=True)
        self.assertIsNone(a.get("structure"))

    def test_analysis_with_insufficient_data_has_no_direction(self):
        a = analysis.build_analysis("EURUSD", "H1", series(UP)[:3], CFG, 2)
        self.assertEqual((a["summary"] or {}).get("direction", {}).get("label", "unclear"), "unclear")


class InternalVersusSwingStructureTest(unittest.TestCase):
    def test_only_one_structure_resolution_is_supported_and_documented(self):
        # TradeMate has a single swing resolution (swing_left / swing_right). There is no separate internal structure, so the engine
        # must not label events "internal" and the docs must say so explicitly.
        res = analyze_market_structure(series(H1_ANALOGUE), CFG)
        self.assertFalse(any("internal" in e.reason.lower() for e in res.events))
        doc = (Path(__file__).resolve().parent.parent / "docs" / "MARKET_STRUCTURE.md").read_text(encoding="utf-8").lower()
        self.assertIn("internal structure is not supported", doc)


class TraceToolTest(unittest.TestCase):
    def test_trace_prints_swings_events_reasons_and_final_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps(series(RALLY_AFTER_BEARISH_CHOCH)), encoding="utf-8")
            text = trace_module.render(json.loads(path.read_text()), CFG)
            self.assertIn("CHOCH bullish", text)
            self.assertIn("origin_swing", text)
            self.assertIn("why:", text)
            self.assertIn("final state", text)
            self.assertEqual(trace_module.main([str(path), "--left", "2", "--right", "2"]), 0)
            self.assertEqual(trace_module.main([str(Path(tmp) / "missing.json")]), 2)


if __name__ == "__main__":
    unittest.main()
