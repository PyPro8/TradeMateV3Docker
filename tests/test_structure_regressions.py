"""Phase 7A.1: Market Structure Engine regression cases for situations that are easy to get wrong.

Pure Python (no broker, no Flask). Each case uses hand-built candles so the expected answer follows from the written rules in
docs/MARKET_STRUCTURE.md, not from whatever the engine happens to print.

    python3 -m unittest tests.test_structure_regressions -v
"""

import math
import unittest
from dataclasses import replace

from tests.test_structure_engine import CFG, DOWN, STEP, T0, UP, UP_THEN_BREAK_LOW, _walk, series, t
from trademate.structure import StructureConfig, analyze_cached, analyze_market_structure


def mk(rows):
    """Candles from (high, low, close) rows. open = previous close clamped into [low, high] (structure only uses high / low /
    close). Time = T0 + i*STEP."""
    out, prev = [], None
    for i, (h, l, c) in enumerate(rows):
        o = c if prev is None else min(max(prev, l), h)
        assert l <= c <= h, f"bad row {i}: {(h, l, c)}"
        out.append([T0 + i * STEP, o, h, l, c])
        prev = c
    return out


def kinds(res, kind):
    return [s for s in res.swings if s.kind == kind]


class RangingMarketTest(unittest.TestCase):
    # Two equal pivot highs (10) and two equal pivot lows (6); every close stays inside the range.
    RANGE = [(8, 6, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 6, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 6, 7), (9, 7, 8), (10, 8, 9)]

    def test_repeated_equal_highs_and_lows_are_eq_not_hh_ll(self):
        res = analyze_market_structure(mk(self.RANGE), CFG)
        highs, lows = kinds(res, "high"), kinds(res, "low")
        self.assertEqual([s.price for s in highs[:2]], [10, 10])
        self.assertEqual(highs[1].classification, "EQH")
        self.assertEqual(lows[1].classification, "EQL")
        self.assertNotIn("HH", [s.classification for s in highs])
        self.assertNotIn("LL", [s.classification for s in lows])

    def test_closing_inside_the_range_creates_no_break_and_no_trend(self):
        res = analyze_market_structure(mk(self.RANGE), CFG)
        self.assertEqual(res.events, ())
        self.assertIsNone(res.trend)
        self.assertIsNotNone(res.active_high)             # the range edge is still an UNBROKEN reference level
        self.assertEqual(res.active_high.price, 10)

    def test_a_wick_through_the_range_edge_breaks_only_in_wick_mode(self):
        rows = self.RANGE + [(10.4, 8, 9.5)]              # high 10.4 > 10, close 9.5 back inside
        close_mode = analyze_market_structure(mk(rows), CFG)
        wick_mode = analyze_market_structure(mk(rows), replace(CFG, break_mode="wick"))
        self.assertEqual(close_mode.events, ())
        self.assertEqual([(e.type, e.direction, e.level) for e in wick_mode.events], [("BOS", "bullish", 10)])

    def test_equal_tolerance_treats_near_equal_swings_as_equal(self):
        rows = [(8, 6, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 6, 7), (9, 7, 8), (10.05, 8, 9), (9, 7, 8), (8, 6, 7)]
        strict = analyze_market_structure(mk(rows), CFG)
        loose = analyze_market_structure(mk(rows), replace(CFG, equal_tolerance=0.1))
        self.assertEqual(kinds(strict, "high")[1].classification, "HH")
        self.assertEqual(kinds(loose, "high")[1].classification, "EQH")


class SuccessiveSwingsTest(unittest.TestCase):
    STAIRS = [10, 12, 14, 12, 10, 12, 15, 13, 11, 13, 16, 14, 12, 14, 17, 15, 13, 15, 18, 16, 14, 16, 19]

    def test_each_successive_break_references_a_distinct_newer_level_and_is_reported_once(self):
        res = analyze_market_structure(series(self.STAIRS), CFG)
        self.assertGreaterEqual(len(res.events), 3)
        self.assertTrue(all(e.direction == "bullish" and e.type == "BOS" for e in res.events))
        self.assertEqual(res.events[0].trend_before, None)
        self.assertTrue(all(e.trend_before == "bullish" for e in res.events[1:]))
        levels = [e.level for e in res.events]
        self.assertEqual(levels, sorted(set(levels)))                      # strictly increasing, no repeats
        self.assertEqual(len({e.reference_pivot_time for e in res.events}), len(res.events))
        self.assertEqual(len({e.id for e in res.events}), len(res.events))
        self.assertEqual(res.trend, "bullish")

    def test_highs_and_lows_both_climb(self):
        res = analyze_market_structure(series(self.STAIRS), CFG)
        self.assertTrue(all(s.classification == "HH" for s in kinds(res, "high")[1:]))
        self.assertTrue(all(s.classification == "HL" for s in kinds(res, "low")[1:]))


class SupersededLevelTest(unittest.TestCase):
    # A pivot high at 20, then a LOWER pivot high at 18. A close at 19 is above 18 but below 20.
    ROWS = [(14, 12, 13), (16, 13, 15), (20, 15, 19), (17, 14, 15), (15, 12, 13), (16, 13, 15), (18, 15, 17),
            (17, 14, 15), (16, 13, 14), (19.5, 15, 19)]

    def test_newer_swing_supersedes_the_older_unbroken_level(self):
        res = analyze_market_structure(mk(self.ROWS), CFG)
        highs = kinds(res, "high")
        self.assertEqual([s.price for s in highs], [20, 18])
        self.assertEqual(highs[1].classification, "LH")
        self.assertEqual(len(res.events), 1)
        e = res.events[0]
        self.assertEqual((e.type, e.direction, e.level, e.reference_pivot_time), ("BOS", "bullish", 18, t(6)))
        self.assertEqual(e.candle_time, t(9))
        self.assertIsNone(res.active_high)                                 # 18 consumed; the superseded 20 is NOT revived

    def test_without_the_close_nothing_is_reported_for_the_old_level(self):
        res = analyze_market_structure(mk(self.ROWS[:-1]), CFG)
        self.assertEqual(res.events, ())
        self.assertEqual(res.active_high.price, 18)


class ProtectedStructureStateTest(unittest.TestCase):
    def test_breaking_a_minor_lower_high_is_not_a_bullish_bos(self):
        # The 15.5 pivot is an LH inside an established bullish structure. It is crossed before the
        # actual HH continuation level at 16.5; that minor cross must not be promoted to a BOS.
        rows = series(UP + [15, 14, 15, 16, 17])
        res = analyze_market_structure(rows, CFG)
        highs = kinds(res, "high")
        self.assertIn(("LH", 15.5), [(s.classification, s.price) for s in highs])
        self.assertEqual([(e.type, e.direction, e.level, e.candle_time) for e in res.events],
                         [("BOS", "bullish", 14.5, t(6)), ("BOS", "bullish", 15.5, t(10)),
                          ("BOS", "bullish", 16.5, t(19))])
        self.assertEqual(res.events[-1].reference_role, "continuation_swing")

    def test_opposing_break_without_a_protected_higher_low_does_not_manufacture_choch(self):
        # The first bullish BOS has not yet established a confirmed HL. Breaking the only unclassified
        # range low is insufficient evidence for a bearish CHoCH.
        rows = series(UP[:7] + [8])
        res = analyze_market_structure(rows, CFG)
        self.assertEqual([(e.type, e.direction) for e in res.events], [("BOS", "bullish")])
        self.assertEqual(res.trend, "bullish")
        self.assertIsNone(res.protected_low)

    def test_provisional_break_does_not_consume_protected_or_continuation_references(self):
        closed = series(UP)
        forming = closed + [[t(len(closed)), 14, 14.2, 8, 8.5]]
        res = analyze_market_structure(forming, replace(CFG, last_candle_closed=False))
        self.assertEqual(res.trend_confirmed, "bullish")
        self.assertEqual(res.trend, "bearish")
        self.assertFalse(res.events[-1].confirmed)
        self.assertEqual(res.events[-1].trend_after, "bearish")
        self.assertEqual(res.events[-1].reference_role, "protected_swing")
        self.assertIsNotNone(res.protected_low)

    def test_multi_event_h1_analogue_uses_protected_references_and_coherent_transitions(self):
        # Deterministic synthetic analogue only; this is not a reconstruction of live CRASH_100 candles.
        closes = UP + [9, 11, 13, 11, 9, 11, 12, 10, 8, 10, 12, 10, 8, 10, 13, 11, 9, 11, 14, 12, 10, 12, 15, 13, 11, 13, 9]
        res = analyze_market_structure(series(closes), CFG)
        actual = [(e.type, e.direction) for e in res.events]
        self.assertEqual(actual, [
            ("BOS", "bullish"), ("BOS", "bullish"), ("CHOCH", "bearish"), ("BOS", "bearish"),
            ("CHOCH", "bullish"), ("BOS", "bullish"), ("BOS", "bullish"), ("CHOCH", "bearish"),
        ])
        by_pivot = {s.pivot_time: s for s in res.swings}
        expected = [
            (t(6), 14.5, t(2), None, None, "bullish", "bootstrap"),
            (t(10), 15.5, t(6), "HH", "bullish", "bullish", "continuation_swing"),
            (t(15), 11.5, t(12), "HL", "bullish", "bearish", "protected_swing"),
            (t(23), 8.5, t(19), "EQL", "bearish", "bearish", "continuation_swing"),
            (t(29), 12.5, t(21), "LH", "bearish", "bullish", "protected_swing"),
            (t(33), 13.5, t(29), "HH", "bullish", "bullish", "continuation_swing"),
            (t(37), 14.5, t(33), "HH", "bullish", "bullish", "continuation_swing"),
            (t(41), 9.5, t(35), "HL", "bullish", "bearish", "protected_swing"),
        ]
        self.assertEqual(len(res.events), len(expected))
        for e, (event_time, level, ref_time, ref_class, before, after, role) in zip(res.events, expected):
            self.assertEqual((e.candle_time, e.level, e.reference_pivot_time, e.trend_before, e.trend_after,
                              e.confirmed, e.reference_role),
                             (event_time, level, ref_time, before, after, True, role))
            self.assertTrue(e.reason)
            self.assertEqual(by_pivot[e.reference_pivot_time].classification, ref_class)
        for e in res.events:
            self.assertEqual(e.trend_after, e.direction)
            if e.type == "CHOCH":
                self.assertEqual(e.reference_role, "protected_swing")
                reference = by_pivot[e.reference_pivot_time]
                self.assertEqual(reference.classification, "HL" if e.direction == "bearish" else "LH")
                self.assertIn("protected swing", e.reason)
        self.assertEqual(res.events[4].trend_before, "bearish")
        self.assertEqual(res.events[4].trend_after, "bullish")
        self.assertEqual(res.events[5].type, "BOS")
        self.assertEqual(res.events[5].direction, "bullish")
        self.assertEqual(res.events[5].trend_before, "bullish")
        intervening_hhs = [s for s in res.swings if s.kind == "high" and s.classification == "HH"
                           and res.events[-2].candle_time < s.confirmed_time < res.events[-1].candle_time]
        self.assertTrue(intervening_hhs)


class DevelopingBreakTest(unittest.TestCase):
    """The newest candle is still forming: a break on it must never be presented as settled."""

    def test_forming_candle_break_is_unconfirmed_and_trend_confirmed_ignores_it(self):
        closed = analyze_market_structure(series(UP_THEN_BREAK_LOW), replace(CFG, last_candle_closed=True))
        forming = analyze_market_structure(series(UP_THEN_BREAK_LOW), replace(CFG, last_candle_closed=False))
        last_closed, last_forming = closed.events[-1], forming.events[-1]
        self.assertEqual((last_closed.type, last_closed.direction), ("CHOCH", "bearish"))
        self.assertTrue(last_closed.confirmed)
        self.assertEqual((last_forming.type, last_forming.direction), ("CHOCH", "bearish"))
        self.assertFalse(last_forming.confirmed)
        self.assertIsNone(last_forming.confirmed_time)
        self.assertEqual(closed.trend, "bearish")
        self.assertEqual(closed.trend_confirmed, "bearish")
        self.assertEqual(forming.trend, "bearish")                         # includes the developing break ...
        self.assertEqual(forming.trend_confirmed, "bullish")               # ... but the confirmed trend does not

    def test_trend_confirmed_equals_trend_when_the_forming_candle_breaks_nothing(self):
        res = analyze_market_structure(series(UP), replace(CFG, last_candle_closed=False))
        self.assertEqual(res.trend_confirmed, res.trend)

    def test_choch_label_is_a_structural_classification_only(self):
        res = analyze_market_structure(series(UP_THEN_BREAK_LOW), CFG)
        self.assertEqual(res.events[-1].type, "CHOCH")
        for forbidden in ("signal", "reversal", "entry", "stop_loss", "take_profit", "probability", "setup"):
            self.assertNotIn(forbidden, res.events[-1].to_dict())


class DuplicateAndOrderingTest(unittest.TestCase):
    def test_duplicate_refresh_does_not_duplicate_structure(self):
        base = series(UP_THEN_BREAK_LOW)
        once = analyze_market_structure(base, CFG)
        twice = analyze_market_structure(base + base, CFG)
        self.assertEqual([e.id for e in twice.events], [e.id for e in once.events])
        self.assertEqual([s.id for s in twice.swings], [s.id for s in once.swings])
        self.assertEqual(twice.diagnostics.duplicates_removed, len(base))

    def test_reverse_ordered_candles_give_the_same_structure(self):
        base = series(UP_THEN_BREAK_LOW)
        fwd, rev = analyze_market_structure(base, CFG), analyze_market_structure(list(reversed(base)), CFG)
        self.assertEqual([e.id for e in rev.events], [e.id for e in fwd.events])
        self.assertTrue(rev.diagnostics.out_of_order)

    def test_invalid_timestamps_are_excluded_and_reported_never_guessed(self):
        base = series(UP)
        for bad in (None, "x", True, math.nan, math.inf, -math.inf):
            rows = [list(r) for r in base]
            rows[5][0] = bad
            res = analyze_market_structure(rows, CFG)
            self.assertEqual(res.diagnostics.used_count, len(base) - 1, bad)
            self.assertEqual([x["index"] for x in res.diagnostics.invalid], [5], bad)

    def test_insufficient_history_is_flagged_not_analysed(self):
        for n in range(0, CFG.min_candles):
            res = analyze_market_structure(series(UP)[:n], CFG)
            self.assertTrue(res.diagnostics.insufficient_data, n)
            self.assertEqual((res.swings, res.events, res.trend), ((), (), None))
        self.assertFalse(analyze_market_structure(series(UP)[:CFG.min_candles], CFG).diagnostics.insufficient_data)


class PrefixInvariantTest(unittest.TestCase):
    """Cutting the data short must never reveal or change anything that was already settled."""

    def test_forming_candle_run_equals_closed_run_except_the_newest_candle(self):
        for mode in ("close", "wick"):
            cfg = replace(CFG, break_mode=mode)
            data = _walk(70, 11)
            for k in range(cfg.min_candles, len(data) + 1):
                prefix = data[:k]
                closed = analyze_market_structure(prefix, replace(cfg, last_candle_closed=True))
                forming = analyze_market_structure(prefix, replace(cfg, last_candle_closed=False))
                last = prefix[-1][0]
                self.assertEqual([s.id for s in forming.swings], [s.id for s in closed.swings], (mode, k))
                settled = [e.id for e in closed.events if e.candle_time != last]
                self.assertEqual([e.id for e in forming.events if e.confirmed], settled, (mode, k))
                self.assertTrue(all(not e.confirmed for e in forming.events if e.candle_time == last), (mode, k))

    def test_events_never_change_once_their_candle_has_closed(self):
        data = _walk(80, 3)
        full = {e.id: e for e in analyze_market_structure(data, CFG).events}
        for k in range(CFG.min_candles, len(data)):
            for e in analyze_market_structure(data[:k], CFG).events:
                self.assertEqual(full[e.id], e, k)               # same id AND same content in the longer run


class IsolationTest(unittest.TestCase):
    def test_interleaved_symbols_and_timeframes_never_leak_into_each_other(self):
        eur_h1, gold_m15 = _walk(90, 21), _walk(90, 5)
        cfg_a, cfg_b = CFG, replace(CFG, break_mode="wick", swing_left=3, swing_right=3)
        expect_a = analyze_market_structure(eur_h1, cfg_a)
        expect_b = analyze_market_structure(gold_m15, cfg_b)
        for _ in range(3):                                           # interleave cached calls
            got_b = analyze_cached(gold_m15, cfg_b)
            got_a = analyze_cached(eur_h1, cfg_a)
            self.assertEqual(got_a.to_dict(), expect_a.to_dict())
            self.assertEqual(got_b.to_dict(), expect_b.to_dict())
        self.assertNotEqual(expect_a.to_dict()["events"], expect_b.to_dict()["events"])

    def test_one_series_two_configs_do_not_share_a_cache_entry(self):
        data = _walk(90, 21)
        a = analyze_cached(data, replace(CFG, break_mode="close"))
        b = analyze_cached(data, replace(CFG, break_mode="wick"))
        self.assertEqual(a.config.break_mode, "close")
        self.assertEqual(b.config.break_mode, "wick")


if __name__ == "__main__":
    unittest.main()
