"""Deterministic regime and setup-family discovery tests; no broker, network, or orders."""

import unittest

from tests.mtf_fixtures import E, NOW, four, ta
from trademate.strategy.regime import (RANGE, TRANSITION, TREND_BEAR, TREND_BULL, UNCERTAIN, VOLATILITY,
                                       estimate_regimes)
from trademate.strategy.setups import discover_candidates
from trademate.strategy.timeframes import DataRules, prepare_series
from trademate.strategy.mtf import BEAR, BULL, ESTABLISHED, R_CONFIRMED, R_DEVELOPING


def regime_map(series, analyses):
    return {item["classification"]: item for item in estimate_regimes(series, analyses)}


def prepared_m15(closes):
    step = 900
    rows = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_price = previous
        spread = 1.0
        rows.append([E - (len(closes) - index) * step, open_price, max(open_price, close) + spread,
                     min(open_price, close) - spread, close])
        previous = close
    rules = DataRules(min_candles={"M5": 10, "M15": 10, "H1": 10, "H4": 10})
    return prepare_series(rows, "M15", NOW, rules)


class RegimeClassification(unittest.TestCase):
    def test_bullish_and_bearish_higher_timeframe_trends_are_distinct(self):
        bullish = regime_map(None, four())
        bearish_analyses = {tf: ta(tf, BEAR) for tf in ("H4", "H1", "M15", "M5")}
        bearish = regime_map(None, bearish_analyses)
        self.assertTrue(bullish[TREND_BULL]["active"])
        self.assertEqual(bullish[TREND_BULL]["certainty"], "high")
        self.assertFalse(bullish[TREND_BEAR]["active"])
        self.assertTrue(bearish[TREND_BEAR]["active"])
        self.assertFalse(bearish[TREND_BULL]["active"])
        self.assertIn("not a probability", bullish[TREND_BULL]["certainty_basis"].lower())

    def test_conflicting_higher_timeframes_are_retained_as_transition_and_uncertainty(self):
        analyses = four(h4=ta("H4", BEAR), h1=ta("H1", BULL, ESTABLISHED, R_DEVELOPING, BULL))
        regimes = regime_map(None, analyses)
        self.assertTrue(regimes[TRANSITION]["active"])
        self.assertTrue(regimes[UNCERTAIN]["active"])
        self.assertFalse(regimes[TREND_BULL]["active"])
        self.assertFalse(regimes[TREND_BEAR]["active"])
        self.assertTrue(any("H4 is bearish" in text for text in regimes[TRANSITION]["conflicting_evidence"]))

    def test_ranging_market_uses_closed_candle_path_efficiency_and_width(self):
        closes = [100 + (1 if index % 4 == 0 else -1 if index % 4 == 2 else 0) for index in range(40)]
        regimes = regime_map({"M15": prepared_m15(closes)}, four(
            h4=ta("H4", "neutral"), h1=ta("H1", "neutral"), m15=ta("M15", "neutral"), m5=ta("M5", "neutral")))
        self.assertEqual(prepared_m15(closes).status, "ok")
        self.assertTrue(regimes[RANGE]["active"])
        self.assertIn("M15", regimes[RANGE]["timeframe"])
        self.assertIn("close-path efficiency", regimes[RANGE]["certainty_basis"])

    def test_elevated_volatility_is_detected_without_erasing_trend_context(self):
        step = 900
        rows = []
        for index in range(50):
            opened = E - (50 - index) * step
            width = 1.0 if index >= 40 else 0.2
            rows.append([opened, 100, 100 + width, 100 - width, 100])
        series = {"M15": prepare_series(rows, "M15", NOW, DataRules(min_candles={"M15": 10}))}
        regimes = regime_map(series, four())
        self.assertTrue(regimes[VOLATILITY]["active"])
        self.assertIn("M15", regimes[VOLATILITY]["timeframe"])
        self.assertTrue(regimes[TREND_BULL]["active"])
        self.assertTrue(regimes[VOLATILITY]["conflicting_evidence"])

    def test_missing_data_is_explicit_and_never_substituted(self):
        analyses = four(h4=ta("H4", analyzed=False, status="unavailable"))
        regimes = regime_map(None, analyses)
        self.assertTrue(regimes[UNCERTAIN]["active"])
        self.assertTrue(any("H4" in item and "no other timeframe is substituted" in item
                            for item in regimes[UNCERTAIN]["limitations"]))
        self.assertEqual(regimes[TREND_BULL]["certainty"], "moderate")


class CandidateDiscovery(unittest.TestCase):
    def test_pullback_candidate_carries_evidence_missing_confirmation_and_invalidation(self):
        analyses = four(m5=ta("M5", BEAR, "early"))
        analyses["M15"].zones = [{"id": "OB-1", "kind": "order_block", "direction": BULL,
                                  "lower": 98.0, "upper": 99.0, "time": 100.0}]
        candidates = discover_candidates(analyses, estimate_regimes(None, analyses))
        pullback = next(item for item in candidates if item["setup_family"] == "pullback_continuation")
        self.assertEqual(pullback["direction"], BULL)
        self.assertTrue(any("M15" in item for item in pullback["supporting_evidence"]))
        self.assertTrue(pullback["missing_evidence"])
        self.assertTrue(pullback["invalidation_conditions"])
        self.assertFalse(pullback["execution_eligible"])

    def test_confirmed_sweep_is_not_a_reaction_without_a_later_closed_structure_event(self):
        analyses = four()
        analyses["M15"].liquidity["sweeps"] = [{"side": "SSL", "level": 98.0, "time": 100.0,
                                                 "close_beyond": False, "confirmed": True}]
        candidates = discover_candidates(analyses, estimate_regimes(None, analyses))
        sweep = next(item for item in candidates if item["setup_family"] == "liquidity_sweep_reaction")
        self.assertEqual(sweep["direction"], BULL)
        self.assertTrue(any("later closed bullish structural reaction" in item for item in sweep["missing_evidence"]))

    def test_breakout_retest_requires_a_closed_retest_back_beyond_the_level(self):
        analyses = four()
        analyses["M15"].events = [{"type": "BOS", "direction": BULL, "level": 101.0,
                                   "candle_time": 100.0, "confirmed": True}]
        analyses["M15"].rows = ((200.0, 102.0, 102.2, 100.5, 101.5),)
        candidates = discover_candidates(analyses, estimate_regimes(None, analyses))
        breakout = next(item for item in candidates if item["setup_family"] == "breakout_retest")
        self.assertEqual(breakout["direction"], BULL)
        self.assertIn("retested 101.0", breakout["supporting_evidence"][0])

    def test_confirmed_reversal_and_countertrend_premises_are_separately_reported(self):
        h1 = ta("H1", BULL, ESTABLISHED, R_CONFIRMED, BULL)
        analyses = four(h4=ta("H4", BEAR), h1=h1, m15=ta("M15", BULL, ESTABLISHED))
        regimes = estimate_regimes(None, analyses)
        candidates = discover_candidates(analyses, regimes)
        families = {item["setup_family"] for item in candidates}
        self.assertIn("structural_reversal", families)
        self.assertIn("countertrend_opportunity", families)

    def test_range_candidate_is_discovery_only_and_has_a_disqualifier(self):
        closes = [100 + (1 if index % 4 == 0 else -1 if index % 4 == 2 else 0) for index in range(40)]
        analyses = four(h4=ta("H4", "neutral"), h1=ta("H1", "neutral"),
                        m15=ta("M15", "neutral"), m5=ta("M5", "neutral"))
        regimes = estimate_regimes({"M15": prepared_m15(closes)}, analyses)
        candidate = next(item for item in discover_candidates(analyses, regimes)
                         if item["setup_family"] == "range_bound_opportunity")
        self.assertEqual(candidate["direction"], "neutral")
        self.assertFalse(candidate["execution_eligible"])
        self.assertTrue(candidate["invalidation_conditions"])


if __name__ == "__main__":
    unittest.main()
