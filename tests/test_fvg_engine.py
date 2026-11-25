"""Phase 7C Fair Value Gap engine: deterministic tests on hand-built candles (expected values follow from the written rules in
trademate/fvg/engine.py, not from whatever the engine prints). Pure Python: no broker, no Flask, no orders.

    python3 -m unittest tests.test_fvg_engine -v
"""

import ast
import json
import math
import random
import unittest
from collections import namedtuple
from pathlib import Path

from trademate.fvg import (BEARISH, BULLISH, DATA_INSUFFICIENT, DATA_INVALID, DATA_OK, FairValueGap, FVGConfig, FVGResult,
                           analyze_fvg)

T0, STEP = 1_760_000_000, 300          # realistic epoch seconds, M5 spacing


def t(i):
    return T0 + i * STEP


def bars(rows):
    """Candles from (open, high, low, close) rows; time = T0 + i*STEP."""
    return [[t(i), o, h, l, c] for i, (o, h, l, c) in enumerate(rows)]


# c1 high = 10, c3 low = 11  ->  bullish gap 10..11 (c2 is the big up candle)
BULL3 = [(9.5, 10, 9, 9.8), (9.8, 12, 9.7, 11.8), (11.8, 12.5, 11, 12)]
# c1 low = 10, c3 high = 9  ->  bearish gap 9..10
BEAR3 = [(10.5, 11, 10, 10.2), (10.2, 10.3, 8, 8.2), (8.2, 9, 7.5, 7.8)]
# overlapping ranges everywhere
FLAT3 = [(10, 11, 9, 10), (10, 11.5, 9.5, 10.5), (10.5, 11.2, 9.8, 10.1)]


def closed(rows, **kw):
    return analyze_fvg(bars(rows) if rows and not isinstance(rows[0], list) else rows, FVGConfig(last_candle_closed=True), **kw)


class Detection(unittest.TestCase):
    def test_valid_bullish_fvg(self):
        res = closed(BULL3)
        self.assertEqual(res.data_status, DATA_OK)
        self.assertEqual(len(res.gaps), 1)
        g = res.gaps[0]
        self.assertIsInstance(g, FairValueGap)
        self.assertEqual((g.direction, g.lower, g.upper, g.size), (BULLISH, 10, 11, 1))

    def test_valid_bearish_fvg(self):
        res = closed(BEAR3)
        self.assertEqual(len(res.gaps), 1)
        g = res.gaps[0]
        self.assertEqual((g.direction, g.lower, g.upper, g.size), (BEARISH, 9, 10, 1))

    def test_three_candles_with_no_gap(self):
        res = closed(FLAT3)
        self.assertEqual((res.gaps, res.data_status), ((), DATA_OK))

    def test_touching_boundaries_are_not_a_gap_in_either_direction(self):
        bull_touch = [(9.5, 10, 9, 9.8), (9.8, 12, 9.7, 11.8), (11.8, 12.5, 10, 12)]     # c3.low == c1.high == 10
        bear_touch = [(10.5, 11, 10, 10.2), (10.2, 10.3, 8, 8.2), (8.2, 10, 7.5, 7.8)]   # c3.high == c1.low == 10
        self.assertEqual(closed(bull_touch).gaps, ())
        self.assertEqual(closed(bear_touch).gaps, ())

    def test_the_smallest_possible_gap_counts_but_not_one_tick_short(self):
        just = [(9.5, 10, 9, 9.8), (9.8, 12, 9.7, 11.8), (11.8, 12.5, 10.00001, 12)]
        self.assertEqual([(g.lower, g.upper) for g in closed(just).gaps], [(10, 10.00001)])
        self.assertEqual(closed([(9.5, 10, 9, 9.8), (9.8, 12, 9.7, 11.8), (11.8, 12.5, 9.99999, 12)]).gaps, ())

    def test_the_middle_candle_does_not_decide_whether_a_gap_exists(self):
        # a tiny, unremarkable middle candle sitting inside c1's range: the outer two still leave a gap, so it is reported
        # (no displacement or quality rule exists) ...
        quiet = [(9.5, 10, 9, 9.8), (9.8, 9.9, 9.7, 9.8), (11.8, 12.5, 11, 12)]
        self.assertEqual(len(closed(quiet).gaps), 1)
        # ... and a huge middle candle does not create one when c1 and c3 overlap
        huge = [(10, 11, 9, 10), (10, 20, 1, 10), (10, 11.5, 9.5, 10)]
        self.assertEqual(closed(huge).gaps, ())

    def test_overlapping_windows_each_report_their_own_gap(self):
        # strongly rising: windows (0,1,2) and (1,2,3) both have c3.low above c1.high
        rows = [(1, 2, 1, 2), (2, 4, 2, 4), (4, 6, 3, 6), (6, 8, 5, 8)]
        res = closed(rows)
        self.assertEqual([(g.first_time, g.third_time, g.lower, g.upper) for g in res.gaps],
                         [(t(0), t(2), 2, 3), (t(1), t(3), 4, 5)])

    def test_a_bullish_and_a_bearish_gap_in_one_series(self):
        rows = BULL3 + [(12, 12.1, 4, 4.2), (4.2, 5, 3, 3.5)]      # c(1..3) = 9.8-bar, down move: window (2,3,4): c1.low 11 > c3.high 5
        res = closed(rows)
        self.assertEqual([(g.direction, g.first_time) for g in res.gaps], [(BULLISH, t(0)), (BEARISH, t(2))])
        self.assertEqual((res.gaps[1].lower, res.gaps[1].upper), (5, 11))

    def test_a_window_is_never_both_directions(self):
        rng = random.Random(7)
        for _ in range(300):
            rows = []
            for _i in range(3):
                lo = rng.uniform(1, 50)
                hi = lo + rng.uniform(0, 20)
                rows.append((lo, hi, lo, hi))
            res = closed(rows)
            self.assertLessEqual(len(res.gaps), 1)


class BoundariesAndTimestamps(unittest.TestCase):
    def test_bullish_boundaries_and_every_timestamp(self):
        g = closed([(1, 5, 1, 4), (4, 9, 4, 9), (9, 12, 7, 11)]).gaps[0]
        self.assertEqual((g.lower, g.upper), (5, 7))                               # c1.high .. c3.low
        self.assertEqual((g.first_time, g.middle_time, g.third_time), (t(0), t(1), t(2)))
        self.assertEqual(g.formed_time, t(2))                                      # knowable only once candle 3 exists
        self.assertEqual(g.confirmed_time, t(2))
        self.assertEqual((g.first_index, g.middle_index, g.third_index), (0, 1, 2))
        self.assertEqual(g.id, f"FVG-BULL-{t(0)}")

    def test_bearish_boundaries_and_every_timestamp(self):
        g = closed([(20, 21, 15, 16), (16, 16.5, 8, 9), (9, 11, 8, 10)]).gaps[0]
        self.assertEqual((g.lower, g.upper), (11, 15))                             # c3.high .. c1.low
        self.assertEqual((g.first_time, g.middle_time, g.third_time), (t(0), t(1), t(2)))
        self.assertEqual(g.id, f"FVG-BEAR-{t(0)}")

    def test_times_and_prices_come_from_the_input_not_from_the_engine(self):
        odd = [[1_700_000_017, 1, 5, 1, 4], [1_700_000_099, 4, 9, 4, 9], [1_700_000_500, 9, 12, 7, 11]]   # irregular spacing
        g = analyze_fvg(odd, FVGConfig(last_candle_closed=True)).gaps[0]
        self.assertEqual((g.first_time, g.middle_time, g.third_time), (1_700_000_017, 1_700_000_099, 1_700_000_500))

    def test_ids_are_plain_epoch_digits_and_distinct_for_distinct_times(self):
        a = analyze_fvg(bars(BULL3), FVGConfig(last_candle_closed=True)).gaps[0].id
        shifted = [[row[0] + 60] + row[1:] for row in bars(BULL3)]
        b = analyze_fvg(shifted, FVGConfig(last_candle_closed=True)).gaps[0].id
        self.assertRegex(a, r"^FVG-BULL-\d{10}$")
        self.assertNotEqual(a, b)

    def test_size_is_upper_minus_lower_and_positive(self):
        for g in closed(BULL3 + BEAR3).gaps:
            self.assertGreater(g.upper, g.lower)
            self.assertEqual(g.size, g.upper - g.lower)

    def test_symbol_and_timeframe_are_labels_only(self):
        with_labels = closed(BULL3, symbol="EURUSD", timeframe="M5")
        without = closed(BULL3)
        self.assertEqual((with_labels.symbol, with_labels.timeframe), ("EURUSD", "M5"))
        self.assertEqual((with_labels.gaps[0].symbol, with_labels.gaps[0].timeframe), ("EURUSD", "M5"))
        self.assertEqual((without.symbol, without.timeframe, without.gaps[0].symbol, without.gaps[0].timeframe), (None,) * 4)
        a, b = with_labels.gaps[0].to_dict(), without.gaps[0].to_dict()
        for k in ("symbol", "timeframe"):
            a.pop(k), b.pop(k)
        self.assertEqual(a, b)                                                      # labels never change detection
        with self.assertRaises(ValueError):
            analyze_fvg(bars(BULL3), symbol=123)
        with self.assertRaises(ValueError):
            analyze_fvg(bars(BULL3), timeframe=["M5"])


class InsufficientHistory(unittest.TestCase):
    def test_fewer_than_three_candles_is_reported_not_raised(self):
        for rows in ([], BULL3[:1], BULL3[:2]):
            res = closed(rows)
            self.assertEqual((res.gaps, res.data_status), ((), DATA_INSUFFICIENT), rows)
            self.assertIn("at least 3", res.reason)

    def test_none_and_empty_input_are_safe(self):
        self.assertEqual(analyze_fvg(None).data_status, DATA_INSUFFICIENT)
        self.assertEqual(analyze_fvg([]).gaps, ())

    def test_exactly_three_candles_is_enough(self):
        self.assertEqual(closed(BULL3).data_status, DATA_OK)

    def test_duplicates_that_collapse_below_three_are_insufficient(self):
        rows = bars(BULL3[:2])
        self.assertEqual(analyze_fvg(rows + rows, FVGConfig(last_candle_closed=True)).data_status, DATA_INSUFFICIENT)


class RepeatedAnalysis(unittest.TestCase):
    SERIES = BULL3 + BEAR3 + FLAT3

    def test_same_candles_twice_give_the_identical_result(self):
        c = bars(self.SERIES)
        a, b = analyze_fvg(c, FVGConfig(last_candle_closed=True)), analyze_fvg(c, FVGConfig(last_candle_closed=True))
        self.assertEqual(a, b)
        ids = [g.id for g in a.gaps]
        self.assertEqual(len(ids), len(set(ids)))

    def test_input_is_not_modified(self):
        c = bars(self.SERIES)
        before = json.dumps(c)
        analyze_fvg(c)
        self.assertEqual(json.dumps(c), before)

    def test_the_same_rows_supplied_twice_do_not_double_the_gaps(self):
        c = bars(self.SERIES)
        once = analyze_fvg(c, FVGConfig(last_candle_closed=True))
        twice = analyze_fvg(c + c, FVGConfig(last_candle_closed=True))
        self.assertEqual(twice.gaps, once.gaps)
        self.assertEqual(twice.diagnostics.duplicates_removed, len(c))
        self.assertEqual(twice.diagnostics.duplicates_conflicting, 0)
        self.assertEqual(twice.data_status, DATA_OK)

    def test_a_growing_series_never_duplicates_or_rewrites_earlier_gaps(self):
        full = bars(self.SERIES)
        seen = {}
        for n in range(1, len(full) + 1):
            res = analyze_fvg(full[:n], FVGConfig(last_candle_closed=True))
            ids = [g.id for g in res.gaps]
            self.assertEqual(len(ids), len(set(ids)), n)
            for g in res.gaps:
                if g.id in seen:
                    self.assertEqual(g, seen[g.id], f"gap {g.id} changed when more candles arrived (n={n})")
                seen[g.id] = g
        self.assertEqual(set(seen), {g.id for g in analyze_fvg(full, FVGConfig(last_candle_closed=True)).gaps})

    def test_no_lookahead_a_prefix_equals_the_full_run_restricted_to_that_prefix(self):
        rng = random.Random(11)
        rows, price = [], 100.0
        for _ in range(80):
            o = price
            c = o + rng.uniform(-3, 3)
            h, l = max(o, c) + rng.uniform(0, 1), min(o, c) - rng.uniform(0, 1)
            rows.append((o, h, l, c))
            price = c
        full = analyze_fvg(bars(rows), FVGConfig(last_candle_closed=True))
        for n in (3, 10, 33, 57, 80):
            prefix = analyze_fvg(bars(rows[:n]), FVGConfig(last_candle_closed=True))
            self.assertEqual(prefix.gaps, tuple(g for g in full.gaps if g.third_index < n), n)

    def test_revision_changes_when_a_candle_is_corrected(self):
        c = bars(BULL3)
        corrected = [list(r) for r in c]
        corrected[2][3] = 11.5                                                      # c3 low corrected 11 -> 11.5
        a, b = analyze_fvg(c), analyze_fvg(corrected)
        self.assertNotEqual(a.revision, b.revision)
        self.assertEqual(a.revision, analyze_fvg(c).revision)
        self.assertEqual(b.gaps[0].upper, 11.5)                                     # recomputed from the corrected data


class Ordering(unittest.TestCase):
    def test_shuffled_input_gives_the_same_gaps_as_ordered_input(self):
        ordered = bars(RepeatedAnalysis.SERIES)
        want = analyze_fvg(ordered, FVGConfig(last_candle_closed=True))
        for seed in range(10):
            shuffled = list(ordered)
            random.Random(seed).shuffle(shuffled)
            got = analyze_fvg(shuffled, FVGConfig(last_candle_closed=True))
            self.assertEqual(got.gaps, want.gaps, seed)
            if shuffled != ordered:
                self.assertTrue(got.diagnostics.out_of_order)

    def test_newest_first_input_does_not_flip_the_direction(self):
        # a bullish pattern supplied newest-first, read in the order given, would look like a bearish one
        res = analyze_fvg(list(reversed(bars(BULL3))), FVGConfig(last_candle_closed=True))
        self.assertEqual([(g.direction, g.lower, g.upper) for g in res.gaps], [(BULLISH, 10, 11)])
        self.assertTrue(res.diagnostics.out_of_order)
        self.assertTrue(any("out of order" in w for w in res.diagnostics.warnings))

    def test_ordered_input_is_not_flagged(self):
        self.assertFalse(closed(BULL3).diagnostics.out_of_order)


class InvalidData(unittest.TestCase):
    def assert_invalid(self, candles, *words):
        res = analyze_fvg(candles, FVGConfig(last_candle_closed=True))
        self.assertEqual((res.data_status, res.gaps), (DATA_INVALID, ()), candles)
        for w in words:
            self.assertIn(w, res.reason)
        return res

    def good(self):
        return bars(BULL3)

    def test_each_kind_of_bad_value_blocks_detection_and_is_reported(self):
        for bad in (None, "x", True, float("nan"), float("inf"), float("-inf")):
            c = self.good()
            c[1][2] = bad                                                           # the middle candle's high
            with self.subTest(bad=bad):
                res = self.assert_invalid(c, "impossible or missing")
                self.assertEqual(len(res.diagnostics.invalid), 1)
                self.assertEqual(res.diagnostics.invalid[0]["index"], 1)

    def test_high_below_low(self):
        c = self.good()
        c[1][2], c[1][3] = 8, 12
        self.assert_invalid(c)

    def test_open_or_close_outside_the_range(self):
        for col in (1, 4):
            c = self.good()
            c[1][col] = 99
            with self.subTest(col=col):
                self.assert_invalid(c)

    def test_zero_and_negative_prices_are_rejected(self):
        for bad in (0, -1, -0.5):
            c = bars([(5, 10, 0.5, 6), (6, 12, 5, 11.8), (11.8, 12.5, 11, 12)])
            c[0][3] = bad                                                           # low
            with self.subTest(bad=bad):
                res = analyze_fvg(c, FVGConfig(last_candle_closed=True))
                self.assertEqual((res.data_status, res.gaps), (DATA_INVALID, ()))
                self.assertEqual(res.non_positive_price_times, (t(0),))
                self.assertIn("zero or below", res.reason)

    def test_too_short_row_and_wrong_type_are_invalid_not_a_crash(self):
        for junk in ([t(1), 1, 2, 3], "candle", 42, {"time": t(1)}, object()):
            c = self.good() + [junk]
            with self.subTest(junk=junk):
                self.assert_invalid(c)

    def test_an_invalid_candle_in_the_middle_never_bridges_its_neighbours(self):
        # candles 0 and 2 would form a bullish gap with candle 3 if the broken candle 1 were silently dropped
        rows = bars([(9.5, 10, 9, 9.8), (0, 0, 0, 0), (11.8, 12.5, 11, 12)])
        self.assert_invalid(rows)
        rows[1] = [t(1), 1, float("nan"), 1, 1]
        self.assert_invalid(rows)

    def test_conflicting_duplicate_timestamps_block_detection(self):
        c = self.good()
        dup = list(c[1])
        dup[2] = 12.2                                                               # same time, different high
        res = self.assert_invalid(c + [dup], "conflicting")
        self.assertEqual(res.diagnostics.duplicates_conflicting, 1)

    def test_identical_duplicates_are_harmless(self):
        c = self.good()
        res = analyze_fvg(c + [list(c[1])], FVGConfig(last_candle_closed=True))
        self.assertEqual((res.data_status, len(res.gaps)), (DATA_OK, 1))
        self.assertEqual(res.diagnostics.duplicates_removed, 1)

    def test_invalid_data_takes_precedence_over_insufficient(self):
        res = analyze_fvg([[t(0), 1, float("nan"), 1, 1]])
        self.assertEqual(res.data_status, DATA_INVALID)

    def test_nothing_is_invented_for_missing_values(self):
        c = self.good()
        c[2][3] = None                                                              # missing low: not guessed from the close
        res = analyze_fvg(c, FVGConfig(last_candle_closed=True))
        self.assertEqual(res.gaps, ())
        self.assertEqual(res.candles_used, 2)

    def test_bad_config_raises(self):
        for bad in (1, "yes", None):
            with self.assertRaises(ValueError):
                analyze_fvg(bars(BULL3), FVGConfig(last_candle_closed=bad))


class InputShapes(unittest.TestCase):
    def test_mappings_objects_tuples_and_lists_are_equivalent(self):
        rows = bars(BULL3 + BEAR3)
        Candle = namedtuple("Candle", "time open high low close")
        class Obj:
            def __init__(self, r):
                self.time, self.open, self.high, self.low, self.close = r
        shapes = {"list": rows, "tuple": [tuple(r) for r in rows],
                  "dict": [dict(zip(("time", "open", "high", "low", "close"), r)) for r in rows],
                  "namedtuple": [Candle(*r) for r in rows], "object": [Obj(r) for r in rows],
                  "extra columns": [r + [123, 4] for r in rows]}
        want = analyze_fvg(rows, FVGConfig(last_candle_closed=True)).gaps
        # by the rule: window 0 bullish 10..11; window 2 (BULL3's last candle low 11 vs BEAR3's middle candle high 10.3) bearish
        # 10.3..11; window 3 (BEAR3 itself) bearish 9..10
        self.assertEqual([(g.direction, g.first_time, g.lower, g.upper) for g in want],
                         [(BULLISH, t(0), 10, 11), (BEARISH, t(2), 10.3, 11), (BEARISH, t(3), 9, 10)])
        for name, shape in shapes.items():
            with self.subTest(shape=name):
                self.assertEqual(analyze_fvg(shape, FVGConfig(last_candle_closed=True)).gaps, want)

    def test_the_projects_own_candle_class_works(self):
        from trademate.broker.base import Candle
        rows = [Candle(*r) for r in bars(BULL3)]
        self.assertEqual(len(analyze_fvg(rows, FVGConfig(last_candle_closed=True)).gaps), 1)


class FormingCandle(unittest.TestCase):
    def test_default_assumes_the_newest_candle_is_still_forming(self):
        self.assertFalse(FVGConfig().last_candle_closed)
        g = analyze_fvg(bars(BULL3)).gaps[0]
        self.assertFalse(g.confirmed)
        self.assertIsNone(g.confirmed_time)
        self.assertEqual((g.lower, g.upper), (10, 11))                              # still reported, clearly marked provisional

    def test_declaring_the_newest_candle_closed_confirms_it(self):
        g = analyze_fvg(bars(BULL3), FVGConfig(last_candle_closed=True)).gaps[0]
        self.assertTrue(g.confirmed)
        self.assertEqual(g.confirmed_time, t(2))

    def test_only_a_gap_that_depends_on_the_forming_candle_is_provisional(self):
        rows = BULL3 + [(12, 12.1, 4, 4.2), (4.2, 5, 3, 3.5)]                       # bullish (0,1,2) and bearish (2,3,4)
        res = analyze_fvg(bars(rows))                                               # candle 4 is forming
        self.assertEqual([(g.direction, g.confirmed) for g in res.gaps], [(BULLISH, True), (BEARISH, False)])
        res = analyze_fvg(bars(rows), FVGConfig(last_candle_closed=True))
        self.assertEqual([g.confirmed for g in res.gaps], [True, True])

    def test_a_forming_candle_that_would_complete_a_gap_is_not_confirmed_and_can_vanish(self):
        forming = bars(BULL3)
        g = analyze_fvg(forming).gaps[0]
        self.assertFalse(g.confirmed)
        forming[2][3] = 9.9                                                         # the candle's low falls back under c1.high
        self.assertEqual(analyze_fvg(forming).gaps, ())                             # the provisional gap is gone

    def test_candles_before_the_forming_one_are_unaffected_by_it(self):
        full = analyze_fvg(bars(BULL3 + FLAT3))
        before = analyze_fvg(bars(BULL3 + FLAT3[:2]))
        self.assertEqual([g for g in full.gaps if g.third_index < 5], list(before.gaps))


class IndependentReference(unittest.TestCase):
    """A deliberately naive re-implementation of the written rule, compared with the engine on many random series."""

    def test_random_series_match_a_brute_force_reference(self):
        rng = random.Random(2024)
        for trial in range(120):
            n = rng.randint(3, 40)
            rows, price = [], 50.0
            for _ in range(n):
                o = price
                c = max(1.0, o + rng.choice([-1, -0.5, 0, 0.5, 1]) * rng.randint(0, 6))
                h = max(o, c) + rng.choice([0, 0.5, 1])
                l = min(o, c) - rng.choice([0, 0.5, 1])
                l = max(l, 0.25)
                rows.append((o, h, l, c))
                price = c
            candles = bars(rows)
            expected = []
            for i in range(n - 2):
                a, c3 = rows[i], rows[i + 2]
                if c3[2] > a[1]:
                    expected.append(("bullish", a[1], c3[2], t(i)))
                elif c3[1] < a[2]:
                    expected.append(("bearish", c3[1], a[2], t(i)))
            got = [(g.direction, g.lower, g.upper, g.first_time) for g in analyze_fvg(candles, FVGConfig(last_candle_closed=True)).gaps]
            self.assertEqual(got, expected, f"trial {trial}")


class IsolationAndShape(unittest.TestCase):
    def test_result_is_json_serialisable(self):
        res = analyze_fvg(bars(BULL3 + BEAR3), FVGConfig(last_candle_closed=True), symbol="EURUSD", timeframe="M5")
        d = json.loads(json.dumps(res.to_dict()))
        self.assertEqual(d["data_status"], "ok")
        self.assertEqual([(g["direction"], g["lower"], g["upper"]) for g in d["gaps"]],
                         [("bullish", 10, 11), ("bearish", 10.3, 11), ("bearish", 9, 10)])
        self.assertEqual({"id", "direction", "upper", "lower", "size", "first_time", "middle_time", "third_time", "formed_time",
                          "confirmed", "confirmed_time", "symbol", "timeframe"} - set(d["gaps"][0]), set())
        self.assertTrue(all(math.isfinite(v) for g in d["gaps"] for v in (g["upper"], g["lower"], g["size"])))

    def test_results_are_immutable_value_objects(self):
        res = closed(BULL3)
        with self.assertRaises(Exception):
            res.gaps[0].upper = 99
        with self.assertRaises(Exception):
            res.data_status = "x"
        self.assertIsInstance(res, FVGResult)

    def test_there_is_no_quality_score_or_trade_field(self):
        fields = set(closed(BULL3).gaps[0].to_dict())
        for banned in ("score", "quality", "strength", "probability", "entry", "stop", "stop_loss", "target", "take_profit", "volume",
                       "lots", "risk", "signal", "approved"):
            self.assertNotIn(banned, fields)

    def test_the_engine_imports_nothing_about_trading_brokers_or_the_web(self):
        root = Path(__file__).resolve().parent.parent / "trademate" / "fvg"
        found = set()
        for path in root.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    found.add(node.module)
                elif isinstance(node, ast.Import):
                    found.update(a.name for a in node.names)
        for mod in found:
            self.assertFalse(mod.split(".")[0] in {"flask", "app", "requests", "sqlite3", "socket"}, mod)
            self.assertNotRegex(mod, r"broker|risk|execution_gate|journal|pending|app$")

    def test_only_the_analysis_and_api_layers_use_the_engine_never_trading_code(self):
        """Replaces the Task 1 "registry untouched" guard (Analysis and chart integration exist now, Phase 7C repair). The engine stays read-only
        analysis: referenced only by the analysis view and the chart API, never by broker, risk or execution code. Phase 8 added trademate/strategy/mtf.py (read-only analysis that proposes setups;
        tests.test_mtf_strategy.Boundaries proves that package never touches broker order, risk or execution code)."""
        root = Path(__file__).resolve().parent.parent
        users = sorted(str(p.relative_to(root)) for p in list((root / "trademate").rglob("*.py")) + [root / "app.py"]
                       if "trademate.fvg" in p.read_text(encoding="utf-8") and "fvg" not in p.parts)
        self.assertEqual(users, ["app.py", "trademate/analysis.py", "trademate/strategy/mtf.py"])      # Phase 8: the read-only multi-timeframe layer


if __name__ == "__main__":
    unittest.main()
