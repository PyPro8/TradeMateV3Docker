"""Market Structure: configurable risk-to-reward.  CHoCH: configurable target distance.   (Bull/Bear and Anti-Twin have their own files.)

    python3 -m unittest tests.test_strategy_enhancements -v

Simulated candles and the real pipeline calculation only: no MT5, no order.
"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.strategy_fixtures import CFG, CLOCK, h1_up, m5_choch_down, m5_choch_up, prepared
from tests.test_five_strategies import BEAR, BULL, run as ms_run
from trademate.config import load_config as load_settings
from trademate.pipeline import qualify, resolve_levels
from trademate.strategies.base import StrategyParams
from trademate.strategies.registry import evaluate_strategy_result, get_strategy
from trademate.strategies.service import strategy_params
from trademate.trade_settings import TradeSettings


def _price_dist(r):
    return abs(r["take_profit_1"]["price"] - r["entry"]["price"]), abs(r["entry"]["price"] - r["stop_loss"]["price"])


def rr_pips(rr, stop_pips):
    return rr * stop_pips


class MarketStructureRiskRewardTest(unittest.TestCase):
    def check(self, rows, rr, side, **kw):
        r = ms_run("market_structure", rows, risk_reward=rr, **kw)
        self.assertEqual(r["decision"], side, r["reasons"])
        tp_dist, sl_dist = _price_dist(r)
        self.assertAlmostEqual(tp_dist, rr * sl_dist, places=6, msg=f"1:{rr}")
        self.assertAlmostEqual(r["risk_reward"]["rr_tp1"], rr, places=6)
        self.assertEqual(r["target"]["risk_reward"], rr)
        self.assertIn(f"1:{rr:g}", r["take_profit_1"]["basis"])           # the ratio is shown in the trade information
        return r

    def test_presets_1_to_4_for_sell_and_buy(self):
        for rr in (1, 2, 3, 4):
            self.check(BEAR, rr, "SELL")
            self.check(BULL, rr, "BUY")

    def test_custom_ratio(self):
        self.check(BEAR, 2.5, "SELL")
        self.check(BULL, 0.5, "BUY")
        self.check(BULL, 7.25, "BUY")

    def test_the_hundred_point_stop_example_gives_a_three_hundred_point_target(self):
        r = self.check(BEAR, 3, "SELL", stop_pips=20.0)              # pip 0.0001: the stop is capped to 20 pips, so a 1:3 target is 60 pips (the 100 -> 300 example, scaled)
        tp_dist, sl_dist = _price_dist(r)
        self.assertAlmostEqual(sl_dist / 0.0001, 20.0, places=4)
        self.assertAlmostEqual(tp_dist / 0.0001, 60.0, places=4)
        self.assertAlmostEqual(r["target"]["pips"], 60.0, places=4)
        self.assertAlmostEqual(rr_pips(3, 100.0), 300.0)               # and the arithmetic itself, for a 100-point stop

    def test_the_ratio_follows_the_final_capped_stop_not_the_structural_one(self):
        wide = self.check(BEAR, 3, "SELL", stop_pips=10000.0)
        capped = self.check(BEAR, 3, "SELL", stop_pips=20.0)
        self.assertAlmostEqual(abs(capped["entry"]["price"] - capped["stop_loss"]["price"]) / 0.0001, 20.0, places=4)
        self.assertAlmostEqual(_price_dist(capped)[0] / 0.0001, 60.0, places=4)
        self.assertGreater(_price_dist(wide)[1], _price_dist(capped)[1])

    def test_without_a_chosen_ratio_the_fixed_pip_target_is_exactly_as_before(self):
        r = ms_run("market_structure", BEAR, tp=25.0)
        self.assertAlmostEqual(_price_dist(r)[0], 25 * 0.0001, places=6)
        self.assertNotIn("risk_reward", r["target"])
        self.assertEqual(r["stop_loss"]["price"], ms_run("market_structure", BEAR, risk_reward=3)["stop_loss"]["price"])   # the stop calculation is untouched

    def test_structure_detection_is_untouched_by_the_ratio(self):
        a, b = ms_run("market_structure", BEAR, tp=25.0), ms_run("market_structure", BEAR, risk_reward=4)
        for key in ("decision", "direction"):
            self.assertEqual(a[key], b[key])
        self.assertEqual(a["context"]["label"], b["context"]["label"])
        self.assertEqual(a["entry"]["price"], b["entry"]["price"])

    def test_unknown_pip_size_means_no_trade_never_a_guess(self):
        r = ms_run("market_structure", BEAR, pip=None, risk_reward=3)
        self.assertNotIn(r["decision"], ("BUY", "SELL"))

    def test_the_proposal_carries_a_pip_target_the_pipeline_measures_from_the_actual_entry(self):
        r = self.check(BEAR, 3, "SELL", stop_pips=20.0)
        q = qualify({"ok": True, "result": r}, "market_structure")
        self.assertTrue(q.qualified, q.reasons)
        self.assertAlmostEqual(q.proposal.target_pips, 60.0, places=4)
        from types import SimpleNamespace
        live = r["entry"]["price"] - 0.0003                         # the executable quote is a little different from the reference close
        _sl, tp, problem = resolve_levels(q.proposal, SimpleNamespace(tick_size=0.00001, point=0.00001, stops_level=0), live)
        self.assertIsNone(problem)
        self.assertAlmostEqual(live - tp, 60 * 0.0001, places=5)

    def test_validation_of_the_saved_ratio(self):
        ts = TradeSettings(Path(tempfile.mkdtemp()) / 'ts.json')
        for ok in ("1", 2, "3", 4.0, "5", "2.5", 0.1, 100):
            self.assertEqual(ts.save("strategy", {"risk_reward": ok}, strategy="market_structure")["values"]["risk_reward"], float(ok))
        for bad in (0, -1, "abc", "", None, float("nan"), float("inf"), 0.05, 101, True, [3]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ts.save("strategy", {"risk_reward": bad}, strategy="market_structure")

    def test_saved_ratio_persists_across_restart_and_old_files_still_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "trade_settings.json"
            path.write_text('{"global": {"lot": 0.05}, "strategy": {"choch": {"target_distance": 150}}}')           # a file from before this feature
            ts = TradeSettings(path)
            self.assertEqual(ts.resolve(strategy="market_structure")["values"]["risk_reward"], None)
            self.assertEqual(ts.resolve(strategy="market_structure")["values"]["lot"], 0.05)
            ts.save("strategy", {"risk_reward": 3}, strategy="market_structure")
            again = TradeSettings(path)
            self.assertEqual(again.resolve(strategy="market_structure")["values"]["risk_reward"], 3.0)
            self.assertEqual(again.resolve(strategy="market_structure")["sources"]["risk_reward"], "strategy")
            self.assertEqual(again.resolve(strategy="choch")["values"]["target_distance"], 150.0)                 # nothing else was disturbed
            self.assertIsNone(again.resolve(strategy="choch")["values"]["risk_reward"])
            again.clear("strategy", None, "market_structure", ["risk_reward"])
            self.assertIsNone(TradeSettings(path).resolve(strategy="market_structure")["values"]["risk_reward"])

    def test_params_use_the_ratio_for_market_structure_only(self):
        st = load_settings()
        ms = strategy_params(st, 0.00001, get_strategy("market_structure"), "M5", None, {"risk_reward": 3.0})
        self.assertEqual(ms.risk_reward, 3.0)
        self.assertIsNone(strategy_params(st, 0.00001, get_strategy("market_structure"), "M5", None, {}).risk_reward)
        for other in ("choch", "candle_momentum", "trendline"):
            self.assertIsNone(strategy_params(st, 0.00001, get_strategy(other), "M5", None, {"risk_reward": 3.0}).risk_reward, other)


class ChochTargetDistanceTest(unittest.TestCase):
    """The distance is the user's setting. No 100-point number overrides it anywhere between the setting and the order."""

    def run_choch(self, rows, params):
        now = rows[-1][0] + 300 + 30
        return evaluate_strategy_result("choch", "TEST", prepared(rows, h1_up(), now), params, CFG, dict(CLOCK, now_server=now))

    BASE = dict(exec_tf="M5", pip_size=0.01, max_extension_fraction=1000.0)

    def test_custom_distances_buy(self):
        for pips in (50, 100, 150, 200, 137.5):
            r = self.run_choch(m5_choch_up(), StrategyParams(target_pips=float(pips), **self.BASE))
            self.assertEqual(r["decision"], "BUY", (pips, r["reasons"]))
            self.assertAlmostEqual(r["take_profit_1"]["price"] - r["entry"]["price"], pips * 0.01, places=6)
            self.assertEqual(r["target"]["pips"], pips)

    def test_custom_distances_sell(self):
        found = False
        for pips in (50, 100, 150, 200, 137.5):
            r = self.run_choch(m5_choch_down(), StrategyParams(target_pips=float(pips), **self.BASE))
            if r["decision"] == "SELL":
                found = True
                self.assertAlmostEqual(r["entry"]["price"] - r["take_profit_1"]["price"], pips * 0.01, places=6)
        self.assertTrue(found, "the bearish CHoCH fixture must produce a SELL for the target maths to be checked")

    def test_the_stop_is_not_changed_by_the_distance(self):
        a = self.run_choch(m5_choch_up(), StrategyParams(target_pips=50.0, **self.BASE))
        b = self.run_choch(m5_choch_up(), StrategyParams(target_pips=200.0, **self.BASE))
        self.assertEqual(a["stop_loss"]["price"], b["stop_loss"]["price"])
        self.assertEqual(a["entry"]["price"], b["entry"]["price"])

    def test_saved_distance_wins_over_settings_and_over_the_hundred_default(self):
        st = load_settings()
        self.assertEqual(st.strategy.choch_target_pips, 100.0)                                    # the shipped default, not a hard limit
        d = get_strategy("choch")
        self.assertEqual(strategy_params(st, 0.00001, d, "M5", None, {}).target_pips, 100.0)
        for pips in (50, 150, 200, 321.5):
            self.assertEqual(strategy_params(st, 0.00001, d, "M5", None, {"target_distance": pips}).target_pips, float(pips))
        st2 = replace(st, strategy=replace(st.strategy, choch_target_pips=75.0))                 # a settings.toml change is honoured when nothing is saved
        self.assertEqual(strategy_params(st2, 0.00001, d, "M5", None, {}).target_pips, 75.0)

    def test_the_distance_reaches_the_order_unchanged(self):
        from types import SimpleNamespace
        for pips in (50, 150, 200):
            r = self.run_choch(m5_choch_up(), StrategyParams(target_pips=float(pips), **self.BASE))
            q = qualify({"ok": True, "result": r}, "choch")
            self.assertTrue(q.qualified, q.reasons)
            self.assertEqual(q.proposal.target_pips, float(pips))
            live = r["entry"]["price"] + 0.05
            _sl, tp, _problem = resolve_levels(q.proposal, SimpleNamespace(tick_size=0.01, point=0.01, stops_level=0), live)
            self.assertAlmostEqual(tp - live, pips * 0.01, places=6)

    def test_validation_and_persistence_of_the_saved_distance(self):
        ts = TradeSettings(Path(tempfile.mkdtemp()) / 'ts.json')
        for bad in (0, -5, "x", "", float("nan"), float("inf"), True, 1_000_001):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ts.save("strategy", {"target_distance": bad}, strategy="choch")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ts.json"
            TradeSettings(path).save("strategy", {"target_distance": 150}, strategy="choch")
            self.assertEqual(TradeSettings(path).resolve(strategy="choch")["values"]["target_distance"], 150.0)
            self.assertEqual(TradeSettings(path).saved_only(strategy="choch"), {"target_distance": 150.0})

    def test_no_hundred_literal_in_the_choch_signal_path(self):
        import inspect
        from trademate.strategies import choch
        import ast
        tree = ast.parse(inspect.getsource(choch))
        nums = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)]
        self.assertNotIn(100, nums, "CHoCH evaluation must not contain a hardcoded 100")


if __name__ == "__main__":
    unittest.main()
