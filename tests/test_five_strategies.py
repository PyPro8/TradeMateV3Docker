"""The five active strategies: Candle Momentum, CHoCH Trading, Market Structure Trading, Bull/Bear Trading, Trendline Trading.

    python3 -m unittest tests.test_five_strategies -v

Simulated candles only: no MT5, no order. Candle Momentum's own signal logic is covered by tests/test_candle_momentum*.py and is not touched here.
"""

import inspect
import unittest

from trademate.strategies import bull_bear, market_structure, trendline
from trademate.strategies.base import StrategyParams
from trademate.strategies.registry import CATALOGUE, RETIRED, STRATEGY_IDS, evaluate_strategy_result, get_any_strategy, get_strategy, public_catalogue
from trademate.strategy import StrategyConfig, prepare_series

SEC = 300
T0 = 1_700_000_000 - (1_700_000_000 % SEC)
B = 1.1000
TOOLTIPS = {
    "candle_momentum": "Analyses candles in relation to their surrounding candle group to identify directional momentum and trading opportunities.",
    "choch": "Identifies a Change of Character in market structure and trades the resulting structural shift.",
    "market_structure": "Uses higher highs, higher lows, lower highs and lower lows to establish market direction and identify entries.",
    "bull_bear": "Trades candles that agree with established upward or downward movement.",
    "trendline": "Uses lines connecting relevant highs or lows to identify market direction, entries and targets.",
}


def zig(points, seg=8, rng=0.0002):
    closes = [points[0]]
    for a, b in zip(points, points[1:]):
        closes += [a + (b - a) * i / seg for i in range(1, seg + 1)]
    return rows_from(closes, rng)


def rows_from(closes, rng=0.0002, t0=T0):
    rows, prev = [], closes[0]
    for k, c in enumerate(closes):
        rows.append([t0 + k * SEC, prev, max(prev, c) + rng, min(prev, c) - rng, c])
        prev = c
    return rows


def run(sid, rows, pip=0.0001, tp=25.0, **kw):
    p = StrategyParams(exec_tf="M5", pip_size=pip, target_pips=tp, **kw)
    now = rows[-1][0] + SEC + 20
    series = {"M5": prepare_series([list(r) for r in rows], "M5", now)}
    clock = {"known": True, "source": "test", "detail": "", "now_server": now, "offset_seconds": 0}
    return evaluate_strategy_result(sid, "EURUSD", series, p, StrategyConfig(), clock)


BEAR = zig([B + .0070, B + .0050, B + .0100, B + .0060, B + .0090, B + .0030, B + .0070, B + .0010, B + .0040])      # LH, LH + LL, LL
BULL = zig([B + .0040, B + .0060, B + .0010, B + .0050, B + .0030, B + .0080, B + .0050, B + .0100, B + .0070])      # HH, HL structure ending after a HH


class CatalogueTest(unittest.TestCase):
    def test_exactly_five_active_strategies_in_order(self):
        self.assertEqual(STRATEGY_IDS, ("candle_momentum", "choch", "market_structure", "bull_bear", "trendline"))
        self.assertEqual([d["name"] for d in public_catalogue()], ["Candle Momentum", "CHoCH Trading", "Market Structure Trading", "Bull/Bear Trading", "Trendline Trading"])

    def test_retired_strategies_are_out_of_the_selector_but_still_resolve_for_history(self):
        for sid in ("trend", "ema_atr"):
            self.assertNotIn(sid, CATALOGUE)
            self.assertIsNone(get_strategy(sid))
            self.assertIsNotNone(get_any_strategy(sid))
            self.assertIn(sid, RETIRED)

    def test_every_strategy_has_its_exact_tooltip_text(self):
        for d in public_catalogue():
            self.assertEqual(d["tooltip"], TOOLTIPS[d["id"]], d["id"])

    def test_each_strategy_keeps_its_own_identity(self):
        rule_ids = {d.rule_id for d in CATALOGUE.values()}
        self.assertEqual(len(rule_ids), 5)
        self.assertEqual(len({d.version and d.id for d in CATALOGUE.values()}), 5)

    def test_trendline_and_market_structure_are_independent_modules(self):
        self.assertNotIn("market_structure", inspect.getsource(trendline))
        self.assertNotIn("trendline", inspect.getsource(market_structure).replace("Trendline", ""))
        for mod in (market_structure, bull_bear, trendline):                                  # none of the new strategies imports another strategy's logic
            src = inspect.getsource(mod)
            for other in ("strategies.choch", "strategies.trend ", "strategies.candle_momentum", "strategies import choch", "candle_momentum import"):
                self.assertNotIn(other, src)


class MarketStructureTest(unittest.TestCase):
    def test_bearish_structure_sells_without_extra_confirmation(self):
        r = run("market_structure", BEAR)
        self.assertEqual(r["decision"], "SELL", r["reasons"])
        self.assertEqual(r["context"]["direction"], "bearish")
        self.assertLess(r["take_profit_1"]["price"], r["entry"]["price"])
        self.assertGreater(r["stop_loss"]["price"], r["entry"]["price"])
        self.assertEqual(r["target"]["pips"], 25.0)                                          # the defined, configurable target
        self.assertEqual(r["risk_reward"]["min_rr"], 0.0)                                              # no extra reward-to-risk rule: the user's own target and stop decide it
        self.assertEqual(len(r["structure_lines"]), 1)                                       # the straight line across the relevant swing highs

    def test_bullish_structure_buys(self):
        r = run("market_structure", BULL)
        self.assertEqual(r["decision"], "BUY", r["reasons"])
        self.assertGreater(r["take_profit_1"]["price"], r["entry"]["price"])

    def test_target_is_configurable(self):
        self.assertEqual(run("market_structure", BEAR, tp=40.0)["target"]["pips"], 40.0)

    def test_contradictory_evidence_never_invents_a_direction(self):
        expanding = zig([B + .0050, B + .0060, B + .0040, B + .0080, B + .0020, B + .0100, B + .0000, B + .0090])      # higher highs AND lower lows
        r = run("market_structure", expanding)
        self.assertEqual(r["decision"], "WAIT")
        self.assertIsNone(r["direction"])

    def test_too_few_swings_waits(self):
        self.assertEqual(run("market_structure", rows_from([B + .0001 * i for i in range(70)]))["decision"], "WAIT")

    def test_an_old_structure_expires(self):
        stale = BEAR + rows_from([BEAR[-1][4]] * 30, t0=BEAR[-1][0] + SEC)
        self.assertEqual(run("market_structure", stale)["decision"], "WAIT")

    def test_unknown_pip_size_means_no_trade_not_a_guessed_target(self):
        r = run("market_structure", BEAR, pip=None)
        self.assertEqual(r["decision"], "WAIT")
        self.assertTrue(any("pip" in x.lower() for x in r["reasons"]))

    def test_same_structure_has_the_same_setup_identity(self):
        a, b = run("market_structure", BEAR), run("market_structure", BEAR)
        self.assertEqual(a["setup_candidates"][0]["candidate_id"], b["setup_candidates"][0]["candidate_id"])


def group(direction, n=5, step=0.0003):
    s = 1 if direction == "up" else -1
    closes = [B]
    for i in range(n):
        closes.append(closes[-1] + s * step)
    return closes


LEAD = [B + (0.0001 if i % 2 else -0.0001) for i in range(60)]      # a quiet lead-in so the 60-candle data rule is met; it carries no movement


class BullBearTest(unittest.TestCase):
    """Bull/Bear v3 REPLACED the v1 candle-group rules (deliberate, by the user's decision): the decision now comes from the live structure-gated engine
    (tests/test_bull_bear.py). Here: the old group fixtures must NOT produce a snapshot trade any more, and the snapshot hook is an honest WAIT."""

    def rows(self, closes):
        return rows_from(LEAD + [LEAD[-1]] + closes)

    def test_the_old_candle_group_rules_are_retired(self):
        base = group("up", 6)
        for closes in (base + [base[-1] + 0.0004, base[-1] + 0.0008], group("down", 6) + [group("down", 6)[-1] - 0.0004, group("down", 6)[-1] - 0.0008]):
            r = run("bull_bear", self.rows(closes))
            self.assertNotIn(r["decision"], ("BUY", "SELL"), "a snapshot must never decide Bull/Bear: only the live engine does")
            self.assertTrue(any("live engine" in x for x in r["reasons"]), r["reasons"])

    def test_rule_id_and_version_identify_the_new_engine(self):
        d = get_strategy("bull_bear")
        self.assertEqual((d.rule_id, d.version), ("BULL-BEAR-v3", "3.0.0"))


class TrendlineTest(unittest.TestCase):
    DESC = zig([B + .0100, B + .0040, B + .0090, B + .0030, B + .0080, B + .0020, B + .0060], seg=12)       # lower highs on a falling resistance line
    ASC = zig([B + .0000, B + .0060, B + .0020, B + .0070, B + .0040, B + .0080, B + .0060], seg=12)

    def test_descending_highs_sell_and_target_the_indicated_level(self):
        r = run("trendline", self.DESC, min_rr=0.1)
        self.assertEqual(r["decision"], "SELL", r["reasons"])
        line = r["trendlines"][0]
        self.assertEqual(line["kind"], "resistance")
        self.assertTrue(line["intact"])
        self.assertAlmostEqual(r["take_profit_1"]["price"], line["target_level"], places=9)
        self.assertLess(r["take_profit_1"]["price"], r["entry"]["price"])
        self.assertGreater(r["stop_loss"]["price"], r["entry"]["price"])

    def test_ascending_lows_buy(self):
        r = run("trendline", self.ASC, min_rr=0.1)
        self.assertEqual(r["decision"], "BUY", r["reasons"])
        self.assertGreater(r["take_profit_1"]["price"], r["entry"]["price"])

    def test_a_close_above_the_resistance_line_breaks_it(self):
        broken = self.DESC + rows_from([self.DESC[-1][4] + 0.0100], t0=self.DESC[-1][0] + SEC)
        self.assertEqual(run("trendline", broken, min_rr=0.1)["decision"], "WAIT")

    def test_required_reward_to_risk_is_enforced(self):
        r = run("trendline", self.DESC, min_rr=50.0)
        self.assertEqual(r["decision"], "WAIT")
        self.assertTrue(any("reward-to-risk" in x.lower() for x in r["reasons"]))

    def test_no_trendline_waits(self):
        self.assertEqual(run("trendline", rows_from([B + .0001 * i for i in range(70)]))["decision"], "WAIT")


if __name__ == "__main__":
    unittest.main()
