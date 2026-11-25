"""Phase 7A: Market Structure Engine unit tests (pure Python, no broker, no Flask).

    python3 -m unittest tests.test_structure_engine -v
"""

import math
import random
import unittest
from dataclasses import replace

from trademate.structure import StructureConfig, analyze_cached, analyze_market_structure
from trademate.structure import engine as engine_module

T0, STEP = 1_000_000, 3600


def series(closes, start=0, half=0.5):
    """Candles whose high/low are close +/- half and whose open is the previous close. Time = T0 + i*STEP."""
    out, prev = [], None
    for i, c in enumerate(closes, start):
        o = c if prev is None else prev
        h, l = max(o, c) + half, min(o, c) - half
        out.append([T0 + i * STEP, o, h, l, c])
        prev = c
    return out


def t(i):
    return T0 + i * STEP


CFG = StructureConfig(swing_left=2, swing_right=2)
# Up: peaks 14.5 / 15.5 / 16.5 (HH), troughs 9.5 / 10.5 / 11.5 (HL).
UP = [10, 12, 14, 12, 10, 12, 15, 13, 11, 13, 16, 14, 12, 13, 14]
DOWN = [30 - c for c in UP]                      # exact mirror: LH / LL
UP_THEN_BREAK_LOW = UP + [11]                    # closes under the last HL (11.5) -> bearish CHoCH
DOWN_THEN_BREAK_HIGH = [30 - c for c in UP_THEN_BREAK_LOW]


def swings_of(res, kind):
    return [s for s in res.swings if s.kind == kind]


class SwingDetectionTest(unittest.TestCase):
    def test_swing_highs_found_with_pivot_and_confirmation_candles(self):
        res = analyze_market_structure(series(UP), CFG)
        highs = swings_of(res, "high")
        self.assertEqual([h.pivot_time for h in highs], [t(2), t(6), t(10)])
        self.assertEqual([h.price for h in highs], [14.5, 15.5, 16.5])
        self.assertEqual([h.confirmed_time for h in highs], [t(4), t(8), t(12)])    # pivot + swing_right candles

    def test_swing_lows_found(self):
        res = analyze_market_structure(series(UP), CFG)
        lows = swings_of(res, "low")
        self.assertEqual([l.pivot_time for l in lows], [t(4), t(8), t(12)])
        self.assertEqual([l.price for l in lows], [9.5, 10.5, 11.5])
        self.assertEqual([l.confirmed_time for l in lows], [t(6), t(10), t(14)])

    def test_prices_and_times_come_from_the_input_candles(self):
        candles = series(UP)
        res = analyze_market_structure(candles, CFG)
        by_time = {c[0]: c for c in candles}
        for s in res.swings:
            self.assertIn(s.pivot_time, by_time)
            self.assertEqual(s.price, by_time[s.pivot_time][2 if s.kind == "high" else 3])
        for e in res.events:
            self.assertIn(e.candle_time, by_time)
            self.assertIn(e.reference_pivot_time, by_time)

    def test_left_and_right_widths_are_respected(self):
        # A one-candle wiggle is a pivot with 1/1 but not with 3/3.
        closes = [10, 12, 11, 12.5, 10, 9, 8, 7, 8, 9, 10]
        narrow = analyze_market_structure(series(closes), StructureConfig(swing_left=1, swing_right=1))
        wide = analyze_market_structure(series(closes), StructureConfig(swing_left=3, swing_right=3))
        self.assertGreater(len(narrow.swings), len(wide.swings))

    def test_equal_highs_earliest_candle_is_the_pivot(self):
        closes = [10, 12, 14, 14, 12, 10, 8, 9, 10]          # candles 2 and 3 have the same high
        highs = swings_of(analyze_market_structure(series(closes), CFG), "high")
        self.assertEqual([h.pivot_time for h in highs], [t(2)])

    def test_equal_lows_earliest_candle_is_the_pivot(self):
        closes = [20, 18, 16, 16, 18, 20, 22, 21, 20]
        lows = swings_of(analyze_market_structure(series(closes), CFG), "low")
        self.assertEqual([l.pivot_time for l in lows], [t(2)])

    def test_consecutive_pivots_of_one_type_are_kept_and_ordered(self):
        closes = [10, 12, 14, 12, 10, 12, 14, 12, 10, 12, 14, 12, 10]
        res = analyze_market_structure(series(closes), CFG)
        confirmations = [s.confirmed_index for s in res.swings]
        self.assertEqual(confirmations, sorted(confirmations))

    def test_outside_candle_can_be_high_and_low_pivot(self):
        candles = series([10, 11, 12, 11, 10, 11, 12, 11, 10])
        candles[4] = [t(4), 11, 20, 1, 10]                   # huge outside candle
        res = analyze_market_structure(candles, CFG)
        at4 = [s for s in res.swings if s.pivot_time == t(4)]
        self.assertEqual(sorted(s.kind for s in at4), ["high", "low"])

    def test_flat_market_has_no_swings(self):
        res = analyze_market_structure(series([10] * 30), CFG)
        self.assertEqual(res.swings, ())
        self.assertEqual(res.events, ())
        self.assertFalse(res.diagnostics.insufficient_data)


class ClassificationTest(unittest.TestCase):
    def test_higher_highs_and_higher_lows(self):
        res = analyze_market_structure(series(UP), CFG)
        self.assertEqual([s.classification for s in swings_of(res, "high")], [None, "HH", "HH"])
        self.assertEqual([s.classification for s in swings_of(res, "low")], [None, "HL", "HL"])

    def test_lower_highs_and_lower_lows(self):
        res = analyze_market_structure(series(DOWN), CFG)
        self.assertEqual([s.classification for s in swings_of(res, "high")], [None, "LH", "LH"])
        self.assertEqual([s.classification for s in swings_of(res, "low")], [None, "LL", "LL"])

    def test_first_swing_of_each_type_is_not_classified(self):
        res = analyze_market_structure(series(UP), CFG)
        self.assertIsNone(swings_of(res, "high")[0].classification)
        self.assertIsNone(swings_of(res, "low")[0].classification)

    def test_classification_compares_with_same_type_only(self):
        res = analyze_market_structure(series([10, 12, 14, 12, 10, 12, 13, 11, 9, 11, 12, 10, 8]), CFG)
        for s in res.swings:
            if s.classification:
                self.assertIn(s.classification, ("HH", "LH", "EQH") if s.kind == "high" else ("HL", "LL", "EQL"))

    def test_equal_swings_are_labelled_eq_not_hh_or_lh(self):
        res = analyze_market_structure(series([10, 12, 14, 12, 10, 12, 14, 12, 10, 12, 14, 12, 10]), CFG)
        highs = swings_of(res, "high")
        self.assertEqual([h.classification for h in highs], [None, "EQH", "EQH"])
        lows = swings_of(res, "low")
        self.assertTrue(all(l.classification in (None, "EQL") for l in lows))

    def test_equal_tolerance_makes_near_equal_swings_equal(self):
        closes = [10, 12, 14, 12, 10, 12, 14.3, 12, 10, 12, 14, 12, 10]
        exact = analyze_market_structure(series(closes), CFG)
        loose = analyze_market_structure(series(closes), replace(CFG, equal_tolerance=0.5))
        self.assertEqual(swings_of(exact, "high")[1].classification, "HH")
        self.assertEqual(swings_of(loose, "high")[1].classification, "EQH")


class BreakTest(unittest.TestCase):
    def test_bullish_bos(self):
        res = analyze_market_structure(series(UP), CFG)
        bos = [e for e in res.events if e.type == "BOS" and e.direction == "bullish"]
        self.assertEqual([e.candle_time for e in bos], [t(6), t(10)])
        second = bos[1]
        self.assertEqual(second.trend_before, "bullish")                  # continuation of an existing uptrend
        self.assertEqual(second.level, 15.5)
        self.assertEqual(second.reference_pivot_time, t(6))
        self.assertEqual(second.reference_pivot_confirmed_time, t(8))
        self.assertEqual(second.break_price, 16)
        self.assertTrue(second.confirmed)
        self.assertEqual(second.confirmed_time, t(10))
        self.assertEqual(res.trend, "bullish")

    def test_bearish_bos(self):
        res = analyze_market_structure(series(DOWN), CFG)
        bos = [e for e in res.events if e.type == "BOS" and e.direction == "bearish"]
        self.assertEqual([e.candle_time for e in bos], [t(6), t(10)])
        self.assertEqual(bos[1].trend_before, "bearish")
        self.assertEqual(res.trend, "bearish")

    def test_bearish_choch_after_uptrend(self):
        res = analyze_market_structure(series(UP_THEN_BREAK_LOW), CFG)
        last = res.events[-1]
        self.assertEqual((last.type, last.direction, last.trend_before), ("CHOCH", "bearish", "bullish"))
        self.assertEqual(last.candle_time, t(15))
        self.assertEqual(last.level, 11.5)                                 # the last higher low
        self.assertEqual(last.reference_pivot_time, t(12))
        self.assertEqual(res.trend, "bearish")

    def test_bullish_choch_after_downtrend(self):
        res = analyze_market_structure(series(DOWN_THEN_BREAK_HIGH), CFG)
        last = res.events[-1]
        self.assertEqual((last.type, last.direction, last.trend_before), ("CHOCH", "bullish", "bearish"))
        self.assertEqual(last.level, 18.5)
        self.assertEqual(res.trend, "bullish")

    def test_bos_and_choch_are_not_decided_by_direction_alone(self):
        up = analyze_market_structure(series(UP_THEN_BREAK_LOW), CFG)
        bull = {e.type for e in up.events if e.direction == "bullish"}
        bear = {e.type for e in up.events if e.direction == "bearish"}
        self.assertEqual(bull, {"BOS"})
        self.assertEqual(bear, {"CHOCH"})
        down = analyze_market_structure(series(DOWN_THEN_BREAK_HIGH), CFG)
        self.assertEqual({e.type for e in down.events if e.direction == "bearish"}, {"BOS"})
        self.assertEqual({e.type for e in down.events if e.direction == "bullish"}, {"CHOCH"})

    def test_first_break_has_no_prior_trend(self):
        first = analyze_market_structure(series(UP), CFG).events[0]
        self.assertIsNone(first.trend_before)
        self.assertEqual(first.type, "BOS")

    def test_close_mode_ignores_a_wick_that_closes_back_inside(self):
        candles = series(UP[:6]) + [[t(6), 12, 15, 12, 14]]            # high 15 > 14.5, close 14 < 14.5
        res = analyze_market_structure(candles, replace(CFG, break_mode="close"))
        self.assertEqual(res.events, ())

    def test_wick_mode_accepts_the_same_wick(self):
        candles = series(UP[:6]) + [[t(6), 12, 15, 12, 14]]
        res = analyze_market_structure(candles, replace(CFG, break_mode="wick"))
        self.assertEqual(len(res.events), 1)
        e = res.events[0]
        self.assertEqual((e.type, e.direction, e.break_price, e.break_mode), ("BOS", "bullish", 15, "wick"))

    def test_close_exactly_on_the_level_is_not_a_break(self):
        candles = series(UP[:6]) + [[t(6), 12, 14.6, 12, 14.5]]
        self.assertEqual(analyze_market_structure(candles, CFG).events, ())

    def test_a_level_is_reported_once_across_following_candles(self):
        closes = UP[:6] + [15, 16, 17, 18]                              # keeps closing above the old high
        res = analyze_market_structure(series(closes), CFG)
        ids = [e.id for e in res.events]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len([e for e in res.events if e.reference_pivot_time == t(2)]), 1)

    def test_every_event_has_the_documented_fields(self):
        d = analyze_market_structure(series(UP_THEN_BREAK_LOW), CFG).events[-1].to_dict()
        for key in ("type", "direction", "level", "reference_pivot_time", "reference_pivot_confirmed_time", "candle_time",
                    "confirmed_time", "confirmed", "break_price", "trend_before", "id"):
            self.assertIn(key, d)

    def test_same_candle_double_break_is_flagged_and_trend_left_undetermined(self):
        # Establish a high at 14.5 and a low at 9.5, then one huge outside candle takes both out (wick mode).
        # high 14.5 (pivot candle 2) and low 9.5 (pivot candle 4, confirmed on candle 6) are both live by candle 8
        candles = series([10, 12, 14, 12, 10, 12, 13, 12]) + [[t(8), 12, 30, -5, 12]]
        res = analyze_market_structure(candles, replace(CFG, break_mode="wick"))
        self.assertEqual(len(res.events), 2)
        self.assertTrue(all(e.same_candle_conflict for e in res.events))
        self.assertIsNone(res.trend)

    def test_active_levels_reported(self):
        res = analyze_market_structure(series(UP), CFG)
        self.assertEqual(res.active_high.price, 16.5)       # latest confirmed, not yet broken
        self.assertEqual(res.active_low.price, 11.5)


class TimingAndLookaheadTest(unittest.TestCase):
    def test_pivot_is_not_reported_before_its_right_side_exists(self):
        full = analyze_market_structure(series(UP), CFG)
        pivot = swings_of(full, "high")[0]                            # pivot candle 2, confirmed on candle 4
        before = analyze_market_structure(series(UP)[:pivot.confirmed_index], CFG)          # candles 0..3
        self.assertNotIn(pivot.pivot_time, [s.pivot_time for s in before.swings])
        at = analyze_market_structure(series(UP)[:pivot.confirmed_index + 1], CFG)           # candles 0..4
        self.assertIn(pivot.pivot_time, [s.pivot_time for s in at.swings])

    def test_confirmation_is_always_pivot_plus_swing_right(self):
        for right in (1, 2, 4):
            cfg = StructureConfig(swing_left=2, swing_right=right)
            for s in analyze_market_structure(_walk(300, 5), cfg).swings:
                self.assertEqual(s.confirmed_index - s.pivot_index, right)
                self.assertGreater(s.confirmed_time, s.pivot_time)

    def test_no_lookahead_prefix_results_equal_full_results(self):
        for mode in ("close", "wick"):
            cfg = StructureConfig(swing_left=3, swing_right=3, break_mode=mode)
            candles = _walk(160, 11)
            full = analyze_market_structure(candles, cfg)
            self.assertGreater(len(full.events), 3)
            for k in range(0, len(candles)):
                prefix = analyze_market_structure(candles[:k + 1], cfg)
                known_swings = [s for s in full.swings if s.confirmed_index <= k]
                known_events = [e for e in full.events if e.candle_index <= k]
                self.assertEqual(list(prefix.swings), known_swings, f"swings differ at prefix {k} ({mode})")
                self.assertEqual(list(prefix.events), known_events, f"events differ at prefix {k} ({mode})")

    def test_appending_candles_never_changes_earlier_results(self):
        candles = _walk(120, 3)
        a = analyze_market_structure(candles[:80], StructureConfig())
        b = analyze_market_structure(candles, StructureConfig())
        self.assertEqual(list(a.swings), [s for s in b.swings if s.confirmed_index <= 79])
        self.assertEqual(list(a.events), [e for e in b.events if e.candle_index <= 79])

    def test_forming_candle_results_are_marked_unconfirmed(self):
        candles = series(UP)
        res = analyze_market_structure(candles, replace(CFG, last_candle_closed=False))
        last = len(candles) - 1
        self.assertTrue(all(not s.confirmed for s in res.swings if s.confirmed_index == last))
        self.assertTrue(all(s.confirmed for s in res.swings if s.confirmed_index != last))
        brk = analyze_market_structure(series(UP_THEN_BREAK_LOW), replace(CFG, last_candle_closed=False)).events[-1]
        self.assertFalse(brk.confirmed)
        self.assertIsNone(brk.confirmed_time)
        # the same break on a closed candle is confirmed
        self.assertTrue(analyze_market_structure(series(UP_THEN_BREAK_LOW), CFG).events[-1].confirmed)


class DataHandlingTest(unittest.TestCase):
    def test_empty_and_none_inputs(self):
        for data in ([], None):
            res = analyze_market_structure(data, CFG)
            self.assertTrue(res.diagnostics.insufficient_data)
            self.assertEqual((res.swings, res.events, res.trend), ((), (), None))

    def test_insufficient_history_returns_nothing_and_says_so(self):
        res = analyze_market_structure(series(UP[:4]), CFG)               # needs 5
        self.assertTrue(res.diagnostics.insufficient_data)
        self.assertEqual(res.swings, ())
        self.assertTrue(any("at least 5" in w for w in res.diagnostics.warnings))
        self.assertFalse(analyze_market_structure(series(UP[:5]), CFG).diagnostics.insufficient_data)

    def test_invalid_values_are_excluded_and_reported_without_crashing(self):
        bad = [
            [t(100), float("nan"), 2, 1, 1.5],
            [t(101), 1, float("inf"), 1, 1],
            [t(102), None, 2, 1, 1.5],
            [t(103), "1", 2, 1, 1.5],
            [t(104), True, 2, 1, 1.5],
            [t(105), 1, 1, 2, 1],            # high < low
            [t(106), 5, 2, 1, 1.5],          # open above high
            [t(107), 1.5, 2, 1, 0.5],        # close below low
            [t(108), 1, 2],                  # too short
            "garbage",
            None,
        ]
        res = analyze_market_structure(series(UP) + bad, CFG)
        self.assertEqual(res.diagnostics.input_count, len(UP) + len(bad))
        self.assertEqual(res.diagnostics.used_count, len(UP))
        self.assertEqual(len(res.diagnostics.invalid), len(bad))
        self.assertTrue(all(i["reason"] for i in res.diagnostics.invalid))
        clean = analyze_market_structure(series(UP), CFG)
        self.assertEqual(res.swings, clean.swings)                       # junk never changes real results
        self.assertEqual(res.events, clean.events)

    def test_invalid_candles_never_become_swings_or_levels(self):
        poisoned = series(UP)
        poisoned[7] = [t(7), 1, 999, 0, 1]                              # valid shape, wildly high: it IS data
        poisoned[8] = [t(8), 13, 1e9, -1e9, float("nan")]               # NaN close: must be dropped
        res = analyze_market_structure(poisoned, CFG)
        self.assertTrue(all(math.isfinite(s.price) for s in res.swings))
        self.assertNotIn(1e9, [s.price for s in res.swings])

    def test_duplicate_candles_identical(self):
        candles = series(UP)
        res = analyze_market_structure(candles + [list(c) for c in candles[3:6]], CFG)
        self.assertEqual(res.diagnostics.duplicates_removed, 3)
        self.assertEqual(res.diagnostics.duplicates_conflicting, 0)
        self.assertEqual(res.swings, analyze_market_structure(candles, CFG).swings)

    def test_duplicate_timestamp_with_different_prices_keeps_the_last(self):
        candles = series(UP)
        newer = list(candles[2]); newer[2] = 15.0                         # the same bar, updated later (higher high)
        res = analyze_market_structure(candles + [newer], CFG)
        self.assertEqual(res.diagnostics.duplicates_conflicting, 1)
        self.assertEqual(swings_of(res, "high")[0].price, 15.0)           # the LAST occurrence won
        self.assertTrue(any("duplicate" in w for w in res.diagnostics.warnings))

    def test_out_of_order_timestamps_are_sorted_deterministically(self):
        candles = series(UP)
        shuffled = candles[:]
        random.Random(7).shuffle(shuffled)
        res = analyze_market_structure(shuffled, CFG)
        self.assertTrue(res.diagnostics.out_of_order)
        ordered = analyze_market_structure(candles, CFG)
        self.assertEqual(res.swings, ordered.swings)
        self.assertEqual(res.events, ordered.events)
        self.assertFalse(ordered.diagnostics.out_of_order)

    def test_accepts_objects_and_mappings_as_well_as_lists(self):
        from trademate.broker.base import Candle
        lists = series(UP)
        objs = [Candle(*c) for c in lists]
        maps = [dict(zip(("time", "open", "high", "low", "close"), c)) for c in lists]
        base = analyze_market_structure(lists, CFG)
        self.assertEqual(analyze_market_structure(objs, CFG), base)
        self.assertEqual(analyze_market_structure(maps, CFG), base)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = StructureConfig()
        self.assertEqual((c.swing_left, c.swing_right, c.break_mode, c.equal_tolerance), (3, 3, "close", 0.0))

    def test_invalid_config_is_rejected(self):
        for kwargs in ({"swing_left": 0}, {"swing_right": -1}, {"swing_left": 2.5}, {"swing_right": True},
                       {"swing_left": 51}, {"break_mode": "touch"}, {"equal_tolerance": -1}, {"equal_tolerance": float("nan")},
                       {"equal_tolerance": "x"}, {"last_candle_closed": "yes"}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                StructureConfig(**kwargs)

    def test_config_changes_results(self):
        candles = _walk(200, 2)
        a = analyze_market_structure(candles, StructureConfig(swing_left=2, swing_right=2))
        b = analyze_market_structure(candles, StructureConfig(swing_left=6, swing_right=6))
        self.assertGreater(len(a.swings), len(b.swings))


class DeterminismAndScopeTest(unittest.TestCase):
    def test_identical_input_gives_identical_output(self):
        candles = _walk(200, 4)
        self.assertEqual(analyze_market_structure(candles, StructureConfig()), analyze_market_structure(candles, StructureConfig()))

    def test_repeated_calculation_creates_no_duplicate_events(self):
        candles = _walk(250, 8)
        for _ in range(3):
            res = analyze_market_structure(candles, StructureConfig())
            ids = [e.id for e in res.events]
            self.assertEqual(len(ids), len(set(ids)))
            sw = [s.id for s in res.swings]
            self.assertEqual(len(sw), len(set(sw)))

    def test_each_swing_level_is_broken_at_most_once(self):
        res = analyze_market_structure(_walk(400, 9), StructureConfig(swing_left=2, swing_right=2))
        refs = [(e.direction, e.reference_pivot_time) for e in res.events]
        self.assertEqual(len(refs), len(set(refs)))

    def test_works_for_any_price_scale_and_timeframe(self):
        gold = [[c[0] * 5, c[1] * 200, c[2] * 200, c[3] * 200, c[4] * 200] for c in series(UP)]   # 5x bigger timestamps
        res = analyze_market_structure(gold, CFG)
        self.assertEqual([s.classification for s in swings_of(res, "high")], [None, "HH", "HH"])

    def test_output_contains_no_trade_fields(self):
        d = analyze_market_structure(series(UP_THEN_BREAK_LOW), CFG).to_dict()
        text = repr(d).lower()
        for word in ("stop_loss", "take_profit", "volume", "lots", "entry", "ticket", "position_size"):
            self.assertNotIn(word, text)

    def test_engine_module_imports_no_broker_or_web_code(self):
        with open(engine_module.__file__, encoding="utf-8") as f:
            source = f.read()
        for forbidden in ("flask", "trademate.broker", "execution", "RiskEngine", "place_order"):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from {forbidden}", source)

    def test_result_is_json_serialisable(self):
        import json
        json.dumps(analyze_market_structure(series(UP_THEN_BREAK_LOW), CFG).to_dict())

    def test_cache_returns_equal_results_and_stays_bounded(self):
        candles = _walk(100, 1)
        first = analyze_cached(candles, StructureConfig())
        self.assertEqual(first, analyze_market_structure(candles, StructureConfig()))
        self.assertIs(analyze_cached(candles, StructureConfig()), first)
        for seed in range(40):                                           # many different datasets
            analyze_cached(_walk(60, 100 + seed), StructureConfig())
        self.assertLessEqual(len(engine_module._CACHE), engine_module._CACHE_MAX)

    def test_cache_does_not_mix_configs(self):
        candles = _walk(150, 6)
        a = analyze_cached(candles, StructureConfig(swing_left=2, swing_right=2))
        b = analyze_cached(candles, StructureConfig(swing_left=6, swing_right=6))
        self.assertNotEqual(len(a.swings), len(b.swings))


def _walk(n, seed):
    """Deterministic random-walk candles (valid OHLC)."""
    rnd = random.Random(seed)
    price, out = 100.0, []
    for i in range(n):
        o = price
        price = max(1.0, price + rnd.uniform(-2, 2))
        h = max(o, price) + rnd.random()
        l = min(o, price) - rnd.random()
        out.append([T0 + i * STEP, o, h, l, price])
    return out


if __name__ == "__main__":
    unittest.main()
