"""Multi-timeframe Strategy Engine: data layer (closed vs forming candles, data quality and native H4).

Pure functions, deterministic fixtures, no MT5, no orders.

    python3 -m unittest tests.test_mtf_timeframes -v
"""

import unittest
from pathlib import Path

from tests.mtf_fixtures import E, NOW, zigzag_end
from tests.test_structure_engine import CFG, UP_THEN_BREAK_LOW, series
from trademate.broker import mt5_adapter
from trademate.analysis import build_analysis
from trademate.strategy import DataRules, StrategyConfig, prepare_series
from trademate.strategy.mtf import analyze_timeframe
from trademate.strategy.timeframes import (DEGRADED, INSUFFICIENT, MISALIGNED, OK, STALE, UNAVAILABLE, PreparedSeries)

ROOT = Path(__file__).resolve().parent.parent
SMALL = DataRules(min_candles={"M5": 10, "M15": 10, "H1": 10, "H4": 2})


def flat(n, sec, end=E, price=100.0):
    """n flat candles; the last one OPENS at end - sec."""
    return [[end - (n - i) * sec, price, price + 0.5, price - 0.5, price] for i in range(n)]


def codes(series):
    return {i.code: i.severity for i in series.issues}


class ClosedAndFormingCandles(unittest.TestCase):
    def test_a_candle_is_closed_once_its_length_has_elapsed(self):
        rows = flat(30, 300) + [[E, 100, 101, 99, 100.5]]           # one extra candle opened exactly at E
        s = prepare_series(rows, "M5", E + 120, SMALL)               # 2 min into it: still forming
        self.assertEqual(len(s.closed), 30)
        self.assertEqual(s.forming[0], E)
        s2 = prepare_series(rows, "M5", E + 300, SMALL)              # exactly at its close: closed
        self.assertEqual(len(s2.closed), 31)
        self.assertIsNone(s2.forming)

    def test_the_forming_candle_is_never_in_the_closed_series(self):
        rows = flat(30, 300) + [[E, 100, 101, 99, 100.5]]
        s = prepare_series(rows, "M5", E + 10, SMALL)
        self.assertNotIn(E, [r[0] for r in s.closed])
        self.assertEqual(s.status, OK)

    def test_unknown_broker_clock_treats_the_newest_candle_as_forming_and_blocks_confirmation(self):
        s = prepare_series(flat(30, 300), "M5", None, SMALL)
        self.assertEqual(len(s.closed), 29)
        self.assertIsNotNone(s.forming)
        self.assertEqual(codes(s).get("clock_unknown"), "block")
        self.assertEqual(s.status, DEGRADED)
        self.assertFalse(s.clock_known)

    def test_a_forming_candle_cannot_complete_a_structural_break(self):
        """Uptrend, then the FORMING candle closes far below the swing low. The closed-candle bias must be unchanged and the break is
        reported only as provisional intrabar evidence."""
        up = zigzag_end([100, 106, 103, 112, 109, 118, 115, 124, 121, 130], 6, 300, end=E)
        closed_series = prepare_series(up, "M5", NOW, SMALL)
        base = analyze_timeframe(closed_series, StrategyConfig())
        self.assertEqual(base.bias, "bullish")
        crash = up + [[E, 130.0, 130.1, 90.0, 90.5]]                  # forming candle, opened at E, deep below every swing low
        s = prepare_series(crash, "M5", E + 60, SMALL)
        a = analyze_timeframe(s, StrategyConfig())
        self.assertEqual(a.bias, "bullish")
        self.assertEqual(a.last_close, base.last_close)
        self.assertTrue(a.intrabar["present"])
        self.assertEqual(a.intrabar["direction"], "bearish")
        self.assertIn("PROVISIONAL", a.intrabar["note"])
        self.assertEqual(a.reversal["stage"], "possible")              # merely possible: nothing closed has broken
        self.assertTrue(a.reversal["provisional"])

    def test_analysis_and_strategy_share_facts_for_identical_closed_candles_and_settings(self):
        rows = series(UP_THEN_BREAK_LOW)
        prepared = PreparedSeries(timeframe="H1", status=OK, reason="", closed=tuple(tuple(row) for row in rows))
        cfg = StrategyConfig(swing_left=CFG.swing_left, swing_right=CFG.swing_right, break_mode=CFG.break_mode,
                             equal_tolerance=CFG.equal_tolerance)
        strategy = analyze_timeframe(prepared, cfg, symbol="EURUSD")
        analysis = build_analysis("EURUSD", "H1", rows, cfg.structure(), digits=2)
        shown = {event["id"]: event for event in analysis["structure"]["recent_events"]}
        facts = {event["id"]: event for event in strategy.events}

        self.assertEqual(strategy.bias, analysis["structure"]["trend"]["confirmed"])
        self.assertEqual(strategy.confirmed_direction, analysis["structure"]["trend"]["confirmed"])
        self.assertEqual(strategy.structure_config, analysis["config"])
        self.assertEqual(set(shown), set(facts))
        for event_id in shown:
            self.assertEqual((shown[event_id]["type"], shown[event_id]["direction"], shown[event_id]["level"],
                              shown[event_id]["reference_pivot_time"], shown[event_id]["trend_before"],
                              shown[event_id]["trend_after"], shown[event_id]["confirmed"]),
                             (facts[event_id]["type"], facts[event_id]["direction"], facts[event_id]["level"],
                              facts[event_id]["reference_pivot_time"], facts[event_id]["trend_before"],
                              facts[event_id]["trend_after"], facts[event_id]["confirmed"]))
            self.assertEqual((shown[event_id]["symbol"], shown[event_id]["timeframe"]),
                             (facts[event_id]["symbol"], facts[event_id]["timeframe"]))


class DataQuality(unittest.TestCase):
    def test_clean_series_is_ok(self):
        s = prepare_series(flat(40, 900), "M15", NOW, SMALL)
        self.assertEqual(s.status, OK)
        self.assertEqual(s.newest_closed_time, E - 900)
        self.assertEqual(s.lag_seconds, 30)

    def test_no_candles_is_unavailable(self):
        for rows in (None, []):
            self.assertEqual(prepare_series(rows, "M5", NOW, SMALL).status, UNAVAILABLE)

    def test_insufficient_history(self):
        s = prepare_series(flat(5, 300), "M5", NOW, SMALL)
        self.assertEqual(s.status, INSUFFICIENT)
        self.assertIn("at least 10", s.reason)
        a = analyze_timeframe(s, StrategyConfig())
        self.assertFalse(a.analyzed)

    def test_stale_candles(self):
        s = prepare_series(flat(40, 300, end=E - 3600), "M5", NOW, SMALL)     # newest closed an hour ago: 12 candle lengths
        self.assertEqual(s.status, STALE)
        self.assertGreater(s.lag_seconds, 2 * 300)
        self.assertFalse(analyze_timeframe(s, StrategyConfig()).analyzed)

    def test_a_missing_recent_candle_blocks_confirmation(self):
        rows = [r for r in flat(40, 300) if r[0] != E - 5 * 300]
        s = prepare_series(rows, "M5", NOW, SMALL)
        self.assertEqual(s.status, DEGRADED)
        self.assertEqual(codes(s).get("recent_gap"), "block")
        self.assertEqual(s.gap_count_recent, 1)

    def test_an_old_gap_is_reported_but_does_not_block(self):
        rows = [r for r in flat(60, 300) if r[0] != E - 50 * 300]
        s = prepare_series(rows, "M5", NOW, SMALL)
        self.assertEqual(s.status, OK)
        self.assertEqual(codes(s).get("older_gaps"), "info")

    def test_identical_duplicates_are_removed_and_harmless(self):
        rows = flat(40, 300)
        s = prepare_series(rows[:11] + [rows[10]] + rows[11:], "M5", NOW, SMALL)     # the repeat sits right next to the original
        self.assertEqual(len(s.closed), 40)
        self.assertEqual(s.status, OK)
        self.assertEqual(codes(s)["duplicate_candles"], "info")

    def test_conflicting_duplicates_block_confirmation(self):
        rows = flat(40, 300)
        clash = list(rows[10])
        clash[2] += 3
        s = prepare_series(rows + [clash], "M5", NOW, SMALL)
        self.assertEqual(s.status, DEGRADED)
        self.assertEqual(codes(s)["duplicate_candles"], "block")

    def test_out_of_order_candles_are_sorted_and_block_confirmation(self):
        rows = flat(40, 300)
        rows[5], rows[6] = rows[6], rows[5]
        s = prepare_series(rows, "M5", NOW, SMALL)
        self.assertEqual([r[0] for r in s.closed], sorted(r[0] for r in s.closed))
        self.assertEqual(s.status, DEGRADED)
        self.assertEqual(codes(s)["out_of_order"], "block")

    def test_invalid_candles_are_excluded_and_reported(self):
        rows = flat(40, 300)
        rows[7] = [rows[7][0], 100, 99, 101, 100]                     # high below low
        s = prepare_series(rows, "M5", NOW, SMALL)
        self.assertEqual(len(s.closed), 39)
        self.assertEqual(s.status, DEGRADED)

    def test_misaligned_timestamps_are_refused(self):
        rows = [[r[0] + 7, *r[1:]] for r in flat(40, 300)]
        s = prepare_series(rows, "M5", NOW, SMALL)
        self.assertEqual(s.status, MISALIGNED)
        self.assertEqual(s.closed, ())

    def test_candles_from_the_future_of_the_broker_clock_block_confirmation(self):
        rows = flat(40, 300) + [[E + 3000, 100, 101, 99, 100]]
        s = prepare_series(rows, "M5", NOW, SMALL)
        self.assertEqual(codes(s).get("future_candle"), "block")

    def test_unknown_timeframe_raises(self):
        with self.assertRaises(ValueError):
            prepare_series(flat(10, 300), "H2", NOW, SMALL)


class NativeH4(unittest.TestCase):
    def test_h4_is_native_in_both_adapter_and_bridge(self):
        bridge = (ROOT / "mt5" / "TradeMateBridge.mq5").read_text(encoding="utf-8")
        self.assertIn("H4", mt5_adapter.TIMEFRAMES)
        self.assertIn('tf == "H4"', bridge)
        self.assertIn("PERIOD_H4", bridge)

    def test_h4_uses_a_four_hour_grid_and_closed_candle_rules(self):
        rows = flat(4, 14400)
        series = prepare_series(rows, "H4", NOW, SMALL)
        self.assertEqual(series.status, OK)
        self.assertEqual(len(series.closed), 4)
        self.assertTrue(all(row[0] % 14400 == 0 for row in series.closed))
        self.assertEqual(series.newest_closed_time, E - 14400)

    def test_forming_h4_bar_is_not_used_for_structure(self):
        rows = flat(4, 14400) + [[E, 100, 130, 80, 125]]
        series = prepare_series(rows, "H4", E + 3600, SMALL)
        self.assertEqual(len(series.closed), 4)
        self.assertEqual(series.forming[0], E)
        self.assertEqual(series.timeframe, "H4")

    def test_native_h4_off_grid_timestamps_are_refused(self):
        rows = [[row[0] + 60, *row[1:]] for row in flat(4, 14400)]
        series = prepare_series(rows, "H4", NOW, SMALL)
        self.assertEqual(series.status, MISALIGNED)
        self.assertEqual(series.closed, ())

    def test_native_h4_stale_and_insufficient_data_are_reported(self):
        rows = flat(4, 14400)
        self.assertEqual(prepare_series(rows, "H4", E + 14400 * 4, SMALL).status, STALE)
        rules = DataRules(min_candles={"H4": 30})
        self.assertEqual(prepare_series(rows, "H4", NOW, rules).status, INSUFFICIENT)


if __name__ == "__main__":
    unittest.main()
