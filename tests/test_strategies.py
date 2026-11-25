"""The two selectable strategies (CHoCH Trading, Trend Continuation): registry, validated persisted selection, H1/M5-only qualification,
independence, semantics of BUY/SELL/WATCH/WAIT, strategy switching with pending proposals, Watchdog binding, Autopilot honesty.

Candles only: no MT5, no network, no randomness, no orders. Simulated results do not prove live-market behaviour or profitability.

    python3 -m unittest tests.test_strategies -v
"""

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.mtf_fixtures import NOW, ReadOnlyFeed, zigzag_end
from tests.strategy_fixtures import (PARAMS, SEC, append_closes, forming_candle, h1_down, h1_mixed, h1_one_swing, h1_range, h1_up, m5_choch_down,
                                     m5_choch_up, m5_trend_down, m5_trend_up, run, run_now)
from trademate import strategies
from trademate.config import PipSettings, Settings, StrategySettings
from trademate.strategies import base, choch, trend
from trademate.strategies.base import StrategyParams, geometry_problem
from trademate.strategies.registry import CATALOGUE, STRATEGY_IDS, get_strategy, public_catalogue
from trademate.strategies.selection import StrategySelectionStore
from trademate.strategies.service import evaluate_selected
from trademate.strategy.watchdog import WatchdogManager

M5 = SEC["M5"]
CHOCH_BASE = [180, 170, 176, 164, 170, 158, 164, 152, 158, 146, 152, 140, 146, 134]


def trade_geometry_ok(r):
    e, s, t = r["entry"]["price"], r["stop_loss"]["price"], r["take_profit_1"]["price"]
    return (s < e < t) if r["decision"] == "BUY" else (t < e < s)


# =====================================================================================================================
class RegistryTest(unittest.TestCase):
    def test_four_strategies_in_dropdown_order(self):          # was "exactly two", then three: Candle Momentum and EMA 20/50 + ATR were added (docs/)
        self.assertEqual(STRATEGY_IDS, ("candle_momentum", "choch", "market_structure", "bull_bear", "trendline"))
        self.assertEqual([c["name"] for c in public_catalogue()], ["Candle Momentum", "CHoCH Trading", "Market Structure Trading", "Bull/Bear Trading", "Trendline Trading"])

    def test_context_strategies_declare_h1_and_m5_candle_momentum_one_timeframe(self):
        for c in public_catalogue():
            if c["id"] in ("candle_momentum", "market_structure", "bull_bear", "trendline"):         # single-timeframe strategies: only the user's execution timeframe (default M5)
                self.assertEqual(c["timeframes"], ["M5"])
                self.assertFalse(c["uses_context"])
            else:
                self.assertEqual(c["timeframes"], ["H1", "M5"])
                self.assertEqual(c["timeframe_roles"], {"H1": "market context", "M5": "setup and confirmation"})
            self.assertRegex(c["version"], r"^\d+\.\d+\.\d+$")
            self.assertTrue(c["rule_id"] and c["summary"] and c["description"])
        self.assertEqual(base.REQUIRED_TIMEFRAMES, ("H1", "M5"))

    def test_only_exact_registered_identifiers_resolve(self):
        for bad in ("CHOCH", " choch", "choch ", "mtf", "MTF-ZONE-REACTION-v1", "", None, 1, ["choch"], {"id": "choch"}, "choch\n", "../trend"):
            self.assertIsNone(get_strategy(bad), repr(bad))
        self.assertIs(get_strategy("choch"), CATALOGUE["choch"])


class SelectionStoreTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.path = Path(self._t.name) / "strategy_selection.json"

    def test_default_until_a_choice_exists(self):
        s = StrategySelectionStore(self.path, "choch")
        self.assertEqual(s.load(), {"id": "choch", "stored": False, "problem": None})

    def test_unregistered_default_is_refused(self):
        with self.assertRaises(ValueError):
            StrategySelectionStore(self.path, "mtf")

    def test_select_validates_persists_and_recovers_after_restart(self):
        s = StrategySelectionStore(self.path, "choch")
        ok, _, prev = s.select("market_structure")
        self.assertEqual((ok, prev, s.selected_id()), (True, "choch", "market_structure"))
        self.assertEqual(json.loads(self.path.read_text()), {"strategy": "market_structure"})
        again = StrategySelectionStore(self.path, "choch")                       # a new process
        self.assertEqual(again.load(), {"id": "market_structure", "stored": True, "problem": None})

    def test_arbitrary_identifiers_are_rejected_and_change_nothing(self):
        s = StrategySelectionStore(self.path, "choch")
        s.select("market_structure")
        for bad in ("TREND", "mtf", "", None, 5, "trend ", {"strategy": "market_structure"}):
            ok, msg, _ = s.select(bad)
            self.assertFalse(ok, repr(bad))
            self.assertIn("Unknown strategy", msg)
        self.assertEqual(s.selected_id(), "market_structure")

    def test_corrupt_or_unregistered_file_falls_back_and_says_so(self):
        for content in ("{not json", json.dumps({"strategy": "mtf"}), json.dumps(["market_structure"]), json.dumps({"strategy": 7})):
            self.path.write_text(content)
            r = StrategySelectionStore(self.path, "choch").load()
            self.assertEqual((r["id"], r["stored"]), ("choch", False), content)
            self.assertTrue(r["problem"], content)

    def test_epoch_and_listeners_only_on_a_real_change(self):
        s = StrategySelectionStore(self.path, "choch")
        seen = []
        s.on_change(lambda old, new: seen.append((old, new)))
        s.select("choch")
        self.assertEqual((s.epoch, seen), (0, []))
        s.select("market_structure")
        s.select("market_structure")
        s.select("choch")
        self.assertEqual((s.epoch, seen), (2, [("choch", "market_structure"), ("market_structure", "choch")]))

    def test_a_failing_listener_does_not_undo_the_selection(self):
        s = StrategySelectionStore(self.path, "choch")
        s.on_change(lambda o, n: 1 / 0)
        self.assertTrue(s.select("market_structure")[0])
        self.assertEqual(s.selected_id(), "market_structure")

    def test_unwritable_location_changes_nothing(self):
        blocker = Path(self._t.name) / "file"
        blocker.write_text("x")
        s = StrategySelectionStore(blocker / "sel.json", "choch")
        ok, msg, _ = s.select("market_structure")
        self.assertFalse(ok)
        self.assertEqual(s.selected_id(), "choch")


# =====================================================================================================================
class ChochStrategyTest(unittest.TestCase):
    def test_bullish_choch_with_h1_aligned_is_a_buy_with_valid_levels(self):
        r = run("choch", m5_choch_up(), h1_up())
        self.assertEqual((r["decision"], r["direction"], r["setup_status"]), ("BUY", "bullish", "confirmed"))
        self.assertTrue(trade_geometry_ok(r))
        self.assertEqual(r["context"]["relation"], "aligned")
        self.assertGreaterEqual(r["take_profit_1"]["rr"], PARAMS.min_rr)
        self.assertEqual(r["risk_reward"]["min_rr"], PARAMS.min_rr)
        self.assertEqual(r["strategy"]["id"], "choch")
        self.assertEqual(r["selected_setup_family"], "choch")
        self.assertEqual(r["setup_invalidation"]["level"], r["stop_loss"]["price"])
        self.assertTrue(any(c["execution_eligible"] for c in r["setup_candidates"]))

    def test_bearish_choch_is_a_sell(self):
        r = run("choch", m5_choch_down(), h1_down())
        self.assertEqual((r["decision"], r["direction"]), ("SELL", "bearish"))
        self.assertTrue(trade_geometry_ok(r))

    def test_h1_informs_but_does_not_veto_a_counter_choch(self):
        aligned = run("choch", m5_choch_up(), h1_up())
        counter = run("choch", m5_choch_up(), h1_down())
        self.assertEqual(counter["decision"], "BUY")                           # NOT vetoed
        self.assertEqual(counter["context"]["relation"], "counter")
        self.assertEqual(counter["risk_reward"]["min_rr"], PARAMS.counter_min_rr)   # the one explicit restriction
        self.assertGreaterEqual(counter["take_profit_1"]["rr"], PARAMS.counter_min_rr)
        self.assertGreater(counter["take_profit_1"]["price"], aligned["take_profit_1"]["price"])
        self.assertTrue(any("Counter-H1" in e for e in counter["confirmation_evidence"]))

    def test_counter_choch_that_cannot_reach_the_higher_rr_stays_watch(self):
        strict = StrategyParams(counter_min_rr=9.0)
        r = run("choch", m5_choch_up(), h1_down(), params=strict)
        self.assertEqual(r["decision"], "WATCH")
        self.assertTrue(any("9R" in m or "9" in m for m in r["missing_evidence"]))
        self.assertIsNone(r["entry"])

    def test_unclear_h1_is_reported_not_treated_as_a_veto_or_as_evidence(self):
        r = run("choch", m5_choch_up(), h1_range())
        self.assertEqual((r["decision"], r["context"]["relation"]), ("BUY", "unclear"))
        self.assertEqual(r["risk_reward"]["min_rr"], PARAMS.min_rr)

    def test_no_choch_is_wait(self):
        rows = zigzag_end([100, 110, 100, 110, 100, 110, 100, 110, 100, 110, 100, 110, 100, 105], 6, M5)
        r = run("choch", rows, h1_up())
        self.assertEqual((r["decision"], r["direction"], r["setup_candidates"]), ("WAIT", None, []))
        self.assertIsNone(r["entry"])

    def test_a_bos_is_not_a_choch(self):
        rows = zigzag_end([100, 110, 105, 115, 110, 120, 115, 125, 120, 130, 125, 135, 130, 140], 6, M5)
        r = run("choch", rows, h1_up())
        self.assertEqual(r["decision"], "WAIT")
        self.assertIn("BOS", " ".join(r["reasons"]))

    def test_provisional_break_on_the_forming_candle_is_only_watch(self):
        closed = zigzag_end(CHOCH_BASE + [143], 6, M5)
        rows = forming_candle(closed, 143, 150, 142.9, 149)
        r = run("choch", rows, h1_up(), now=rows[-1][0] + 100)
        self.assertEqual((r["decision"], r["direction"], r["setup_status"]), ("WATCH", "bullish", "developing"))
        self.assertIn("provisional", " ".join(r["reasons"]).lower())
        self.assertFalse(any(c["execution_eligible"] for c in r["setup_candidates"]))
        self.assertIsNone(r["entry"])

    def test_an_expired_choch_is_wait(self):
        base_rows = zigzag_end(CHOCH_BASE + [148], 6, M5)
        r = run_now("choch", append_closes(base_rows, [148 + 0.1 * i for i in range(1, 15)]), h1_up())
        self.assertEqual((r["decision"], r["setup_status"]), ("WAIT", "invalidated"))
        self.assertIn("expired", r["reasons"][0])

    def test_age_limit_is_configurable_and_exact(self):
        base_rows = zigzag_end(CHOCH_BASE + [148], 6, M5)
        rows = append_closes(base_rows, [148.1, 148.2, 148.3])            # 3 candles after the CHoCH candle
        self.assertEqual(run_now("choch", rows, h1_up(), StrategyParams(signal_max_age=3))["decision"], "BUY")
        self.assertEqual(run_now("choch", rows, h1_up(), StrategyParams(signal_max_age=2))["decision"], "WAIT")

    def test_a_choch_that_failed_is_wait(self):
        base_rows = zigzag_end(CHOCH_BASE + [148], 6, M5)
        r = run_now("choch", append_closes(base_rows, [147, 145]), h1_up())
        self.assertEqual((r["decision"], r["setup_status"]), ("WAIT", "invalidated"))
        self.assertIn("failed", r["reasons"][0])

    def test_sell_failure_is_the_mirror(self):
        base_rows = m5_choch_down()
        r = run_now("choch", append_closes(base_rows, [base_rows[-1][4] - 2.0, 175.0]), h1_down())
        self.assertEqual(r["decision"], "WAIT")

    def test_no_structural_target_with_enough_reward_is_watch_not_buy(self):
        r = run("choch", m5_choch_up(final=170.0), h1_up())
        self.assertEqual(r["decision"], "WATCH")
        self.assertTrue(r["missing_evidence"])
        self.assertIsNone(r["take_profit_1"])
        self.assertFalse(any(c["execution_eligible"] for c in r["setup_candidates"]))

    def test_choch_needs_neither_order_block_fvg_nor_sweep(self):
        r = run("choch", m5_choch_up(), h1_up())
        self.assertEqual(r["decision"], "BUY")
        text = json.dumps(r["gates"]).lower()
        for banned in ("order block", "fair value", "fvg", "sweep"):
            self.assertNotIn(banned, text)

    def test_stages_are_reported_separately(self):
        r = run("choch", m5_choch_up(), h1_up())
        self.assertEqual(set(r["stages"]), {"context", "setup_candidate", "confirmation", "trade_levels"})
        self.assertTrue(all(v["state"] == "met" for v in r["stages"].values()))
        w = run("choch", zigzag_end([100, 110] * 7 + [100, 105], 6, M5), h1_up())
        self.assertEqual(w["stages"]["setup_candidate"]["state"], "not_met")
        self.assertEqual(w["stages"]["trade_levels"]["state"], "not_evaluated")


class TrendStrategyTest(unittest.TestCase):
    def test_bullish_trend_pullback_resumption_is_a_buy(self):
        r = run("trend", m5_trend_up(), h1_up())
        self.assertEqual((r["decision"], r["direction"], r["setup_status"]), ("BUY", "bullish", "confirmed"))
        self.assertTrue(trade_geometry_ok(r))
        self.assertLess(r["stop_loss"]["price"], 162.0)                       # beyond the pullback extreme
        self.assertGreaterEqual(r["take_profit_1"]["rr"], PARAMS.min_rr)
        self.assertEqual(r["strategy"]["id"], "trend")
        self.assertEqual(r["selected_setup_family"], "trend")
        self.assertIn("h1_bullish", r["context"]["state"])

    def test_bearish_trend_is_a_sell(self):
        r = run("trend", m5_trend_down(), h1_down())
        self.assertEqual((r["decision"], r["direction"]), ("SELL", "bearish"))
        self.assertTrue(trade_geometry_ok(r))

    def test_h1_trend_definition_uses_confirmed_swings(self):
        from tests.strategy_fixtures import CFG
        from trademate.strategy import prepare_series
        from trademate.strategy.mtf import analyze_timeframe
        for rows, expected in ((h1_up(), "bullish"), (h1_down(), "bearish"), (h1_range(), None), (h1_mixed(), None), (h1_one_swing(), None)):
            a = analyze_timeframe(prepare_series([list(r) for r in rows], "H1", NOW), CFG, "T")
            self.assertEqual(trend.h1_trend(a)["direction"], expected)

    def test_unclear_h1_means_wait_with_the_reason(self):
        for rows in (h1_range(), h1_mixed(), h1_one_swing()):
            r = run("trend", m5_trend_up(), rows)
            self.assertEqual((r["decision"], r["direction"], r["entry"]), ("WAIT", None, None))
            self.assertIn("No H1 trend", r["reasons"][0])
            self.assertEqual(r["stages"]["context"]["state"], "not_met")

    def test_a_broken_h1_structure_is_unclear(self):
        rows = h1_up()
        broken = [list(x) for x in rows[:-6]] + [list(x) for x in zigzag_end([172, 150, 120, 110], 2, SEC["H1"], end=rows[-1][0] + SEC["H1"])]
        a = __import__("trademate.strategy.mtf", fromlist=["x"]).analyze_timeframe(
            __import__("trademate.strategy", fromlist=["x"]).prepare_series(broken, "H1", NOW), __import__("tests.strategy_fixtures", fromlist=["x"]).CFG, "T")
        self.assertIsNone(trend.h1_trend(a)["direction"])

    def test_a_shallow_move_is_not_a_pullback(self):
        r = run("trend", m5_trend_up(pullback=168.0), h1_up())
        self.assertEqual(r["decision"], "WAIT")
        self.assertIn("no pullback", r["reasons"][0])

    def test_a_pullback_deeper_than_the_impulse_is_not_a_pullback(self):
        r = run("trend", m5_trend_up(pullback=150.0), h1_up())
        self.assertEqual((r["decision"], r["setup_status"]), ("WAIT", "invalidated"))

    def test_pullback_without_resumption_is_watch(self):
        r = run("trend", m5_trend_up(final=166.0), h1_up())
        self.assertEqual((r["decision"], r["direction"], r["setup_status"]), ("WATCH", "bullish", "developing"))
        self.assertIsNone(r["entry"])
        self.assertTrue(any("closed M5 candle above" in m for m in r["missing_evidence"]))
        self.assertFalse(any(c["execution_eligible"] for c in r["setup_candidates"]))

    def test_resumption_on_the_forming_candle_is_provisional_watch(self):
        rows = forming_candle(m5_trend_up(final=168.0), 168, 172, 167.9, 171)
        r = run("trend", rows, h1_up(), now=rows[-1][0] + 100)
        self.assertEqual(r["decision"], "WATCH")
        self.assertIn("forming candle", " ".join(r["reasons"]))

    def test_an_old_resumption_expires(self):
        r = run_now("trend", append_closes(m5_trend_up(), [172 + 0.5 * i for i in range(1, 16)]), h1_up())
        self.assertEqual((r["decision"], r["setup_status"]), ("WAIT", "invalidated"))
        self.assertIn("expired", r["reasons"][0])

    def test_a_failed_breakout_is_wait(self):
        r = run_now("trend", append_closes(m5_trend_up(), [171.5, 169.9]), h1_up())
        self.assertEqual((r["decision"], r["setup_status"]), ("WAIT", "invalidated"))
        self.assertIn("failed", r["reasons"][0])

    def test_late_entry_with_too_little_reward_stays_watch(self):
        r = run_now("trend", append_closes(m5_trend_up(), [172.5, 173.0, 173.5, 174.0, 174.5]), h1_up())
        self.assertEqual(r["decision"], "WATCH")
        self.assertIn("No target gives at least", r["reasons"][0])

    def test_measured_move_target_is_labelled_as_an_assumption(self):
        with mock.patch.object(trend, "structural_levels", return_value=[]):              # no structural level ahead: only the measured move remains
            r = run("trend", m5_trend_up(), h1_up(), params=StrategyParams(min_rr=0.1, counter_min_rr=0.1))
        self.assertEqual(r["decision"], "BUY")
        self.assertIn("measured move", r["take_profit_1"]["basis"])
        self.assertIn("assumption", r["take_profit_1"]["basis"])

    def test_trend_needs_no_choch_ob_fvg_or_sweep(self):
        r = run("trend", m5_trend_up(), h1_up())
        self.assertEqual(r["decision"], "BUY")
        text = json.dumps(r["gates"]).lower()
        for banned in ("choch", "order block", "fvg", "fair value", "sweep"):
            self.assertNotIn(banned, text)


# =====================================================================================================================
class SharedGuaranteesTest(unittest.TestCase):
    """Rules that must hold for BOTH strategies."""

    SCENARIOS = {
        "choch": [(m5_choch_up(), h1_up()), (m5_choch_down(), h1_down()), (m5_choch_up(), h1_down()), (m5_choch_up(), h1_range())],
        "trend": [(m5_trend_up(), h1_up()), (m5_trend_down(), h1_down()), (m5_trend_up(final=166.0), h1_up()), (m5_trend_up(), h1_range()),
                  (m5_trend_up(pullback=168.0), h1_up())],
    }

    def all_results(self):
        for sid, cases in self.SCENARIOS.items():
            for m5, h1 in cases:
                yield sid, run(sid, m5, h1)

    def test_only_h1_and_m5_are_ever_read_or_reported(self):
        for sid, r in self.all_results():
            self.assertEqual(set(r["data"]), {"H1", "M5"}, sid)
            self.assertEqual(set(r["timeframes"]) <= {"H1", "M5"}, True, sid)
            self.assertEqual(r["strategy"]["timeframes"], ["H1", "M5"])
            self.assertNotIn("M15", json.dumps(r["gates"]) + json.dumps(r["context"]))
            self.assertNotIn("H4", json.dumps(r["gates"]) + json.dumps(r["context"]))

    def test_a_series_dict_with_only_h1_and_m5_is_sufficient(self):
        from tests.strategy_fixtures import CFG, CLOCK, prepared
        from trademate.strategies.registry import evaluate_strategy_result
        series = prepared(m5_trend_up(), h1_up())
        self.assertEqual(set(series), {"H1", "M5"})
        self.assertEqual(evaluate_strategy_result("trend", "T", series, PARAMS, CFG, CLOCK)["decision"], "BUY")
        self.assertEqual(evaluate_strategy_result("choch", "T", prepared(m5_choch_up(), h1_up()), PARAMS, CFG, CLOCK)["decision"], "BUY")

    def test_buy_and_sell_always_have_correct_geometry_and_enough_reward(self):
        n = 0
        for sid, r in self.all_results():
            if r["decision"] in ("BUY", "SELL"):
                n += 1
                self.assertTrue(trade_geometry_ok(r), sid)
                self.assertGreaterEqual(r["take_profit_1"]["rr"], r["risk_reward"]["min_rr"])
                self.assertEqual(r["setup_status"], "confirmed")
                self.assertTrue(all(g["passed"] for g in r["gates"]))
        self.assertGreaterEqual(n, 6)

    def test_watch_and_wait_never_carry_levels_or_eligible_candidates(self):
        n = 0
        for sid, r in self.all_results():
            if r["decision"] in ("WATCH", "WAIT"):
                n += 1
                for key in ("entry", "stop_loss", "take_profit_1", "risk_reward", "setup_invalidation"):
                    self.assertIsNone(r[key], (sid, key))
                self.assertFalse(any(c["execution_eligible"] for c in r["setup_candidates"]))
                self.assertIsNone(r["selected_setup_family"])
                self.assertTrue(r["reasons"])
        self.assertGreaterEqual(n, 3)

    def test_stale_missing_or_unusable_data_is_never_a_direction(self):
        cases = {"M5 stale": dict(now=NOW + 3000), "all stale": dict(now=NOW + 9000)}
        for sid in ("choch", "trend"):                       # the H1-context strategies; Candle Momentum has its own data tests in test_candle_and_choch_targets
            m5 = m5_choch_up() if sid == "choch" else m5_trend_up()
            for label, kw in cases.items():
                r = run(sid, m5, h1_up(), **kw)
                self.assertEqual((r["decision"], r["direction"], r["entry"]), ("WAIT", None, None), (sid, label))
                self.assertEqual(r["context"]["state"], "data_unavailable")
                self.assertEqual(r["setup_candidates"], [])
                self.assertEqual(r["stages"]["setup_candidate"]["state"], "not_evaluated")
            r = run(sid, m5, h1_up()[:-3])                                      # H1 lagging while M5 is current
            self.assertEqual((r["decision"], r["direction"]), ("WAIT", None))
            self.assertIn("H1", r["reasons"][0])
            r = run(sid, m5[:30], h1_up())                                      # too few M5 candles
            self.assertEqual((r["decision"], r["direction"]), ("WAIT", None))

    def test_no_h1_or_m5_at_all_is_wait(self):
        from tests.strategy_fixtures import CFG, CLOCK
        from trademate.strategies.registry import evaluate_strategy_result
        for sid in STRATEGY_IDS:
            r = evaluate_strategy_result(sid, "T", {}, PARAMS, CFG, CLOCK)
            self.assertEqual((r["decision"], r["direction"]), ("WAIT", None))

    def test_geometry_validator(self):
        self.assertIsNone(geometry_problem("bullish", 100.0, 99.0, 102.0))
        self.assertIsNone(geometry_problem("bearish", 100.0, 101.0, 98.0))
        for args in (("bullish", 100.0, 101.0, 102.0), ("bullish", 100.0, 99.0, 99.5), ("bearish", 100.0, 99.0, 98.0), ("bearish", 100.0, 101.0, 102.0),
                     ("bullish", float("nan"), 99.0, 102.0), ("bullish", 100.0, None, 102.0), ("bullish", 0.0, -1.0, 2.0), ("bullish", 100.0, 100.0, 102.0)):
            self.assertIsNotNone(geometry_problem(*args), args)

    def test_the_strategies_are_independent(self):
        for module, other in ((choch, "trend"), (trend, "choch")):
            tree = ast.parse(inspect.getsource(module))
            imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            self.assertFalse(any(other in name for name in imported), f"{module.__name__} must not import {other}")
        self.assertIn("choch", [d.id for d in CATALOGUE.values()])
        # the trend decision is identical whether or not the M5 series happens to contain a CHoCH: it never reads that signal
        a = run("trend", m5_trend_up(), h1_up())
        self.assertEqual(a["decision"], "BUY")
        self.assertFalse(a["strategy"]["emits_choch_alerts"])

    def test_strategies_do_not_import_risk_sizing_or_execution(self):
        for path in Path(strategies.__file__).parent.glob("*.py"):
            tree = ast.parse(path.read_text())
            names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            for banned in ("trademate.risk.engine", "trademate.risk.position_sizing", "trademate.execution_gate", "trademate.pipeline", "trademate.broker.mt5_adapter"):
                self.assertNotIn(banned, names, f"{path.name} imports {banned}")
            calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            for banned in ("place_order", "place_pending_order", "close_position", "modify_position", "cancel_pending_order"):
                self.assertNotIn(banned, calls, f"{path.name} calls {banned}")

    def test_same_input_same_output(self):
        a, b = run("choch", m5_choch_up(), h1_up()), run("choch", m5_choch_up(), h1_up())
        self.assertEqual(a, b)

    def test_candidate_ids_are_stable_and_differ_between_strategies(self):
        a1 = run("choch", m5_choch_up(), h1_up())["setup_candidates"][0]["candidate_id"]
        a2 = run("choch", m5_choch_up(), h1_up())["setup_candidates"][0]["candidate_id"]
        b = run("trend", m5_trend_up(), h1_up())["setup_candidates"][0]["candidate_id"]
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b)


# =====================================================================================================================
# The fixtures' prices move about 2 price units. The production CHoCH target is 100 pips (tests/test_candle_and_choch_targets.py); here a wide target keeps
# these legacy-shaped scenarios about what they test (feed, plumbing, routing), not about the pip distance.
WIDE = Settings(strategy=StrategySettings(choch_target_pips=5000.0), pips=PipSettings(overrides={"EURUSD": 0.01}))


class ServiceTest(unittest.TestCase):
    def feed(self, m5, h1, **kw):
        return ReadOnlyFeed({"M5": m5, "H1": h1}, **kw)

    def test_only_h1_and_m5_candles_are_requested(self):
        for sid, m5 in (("choch", m5_choch_up()), ("trend", m5_trend_up())):
            feed = self.feed(m5, h1_up())
            out = evaluate_selected(feed, sid, "EURUSD", WIDE, now_utc=NOW)
            self.assertEqual(out["result"]["decision"], "BUY", sid)
            self.assertEqual(out["connection"], "CONNECTED")
            self.assertEqual(out["strategy"]["id"], sid)
            self.assertEqual({tf for tf in feed.requested} - {"M1"}, {"M5", "H1"})       # M1 is only the broker-clock probe
            self.assertNotIn("M15", feed.requested)
            self.assertNotIn("H4", feed.requested)

    def test_unregistered_strategy_evaluates_nothing(self):
        feed = self.feed(m5_choch_up(), h1_up())
        out = evaluate_selected(feed, "mtf", "EURUSD", Settings(), now_utc=NOW)
        self.assertFalse(out["ok"])
        self.assertIn("No valid strategy", out["message"])
        self.assertEqual(feed.requested, [])

    def test_disconnected_broker_gives_an_honest_wait(self):
        from trademate.broker.base import ConnectionState, ConnectionStatus
        feed = self.feed(m5_choch_up(), h1_up(), status=ConnectionStatus(ConnectionState.DISCONNECTED, "MT5 is not running"))
        out = evaluate_selected(feed, "choch", "EURUSD", Settings(), now_utc=NOW)
        self.assertEqual((out["result"]["decision"], out["result"]["direction"], out["connection"]), ("WAIT", None, "DISCONNECTED"))

    def test_a_missing_timeframe_is_unavailable_not_neutral(self):
        out = evaluate_selected(self.feed(m5_choch_up(), h1_up(), fail=("H1",)), "choch", "EURUSD", Settings(), now_utc=NOW)
        self.assertEqual((out["result"]["decision"], out["result"]["direction"]), ("WAIT", None))
        self.assertIn("H1", out["result"]["reasons"][0])

    def test_the_feed_has_no_order_methods(self):
        feed = self.feed(m5_choch_up(), h1_up())
        evaluate_selected(feed, "trend", "EURUSD", Settings(), now_utc=NOW)               # any trading attribute access raises AssertionError

    def test_settings_validation(self):
        from trademate.config import ConfigError, load_config
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "s.toml"
            for bad in ('default_strategy = "mtf"', "counter_min_rr = 1.0", "signal_max_age = 0", "pullback_min_retrace = 1.5", "signal_max_age = 2.5"):
                p.write_text("[strategy]\n" + bad + "\n")
                with self.assertRaises(ConfigError, msg=bad):
                    load_config(p)
            p.write_text('[strategy]\ndefault_strategy = "market_structure"\ncounter_min_rr = 3\n')
            cfg = load_config(p).strategy
            self.assertEqual((cfg.default_strategy, cfg.counter_min_rr), ("market_structure", 3.0))
            p.write_text('[strategy]\ndefault_strategy = "ema_atr"\n')                                  # a retired strategy can no longer be the default selection
            with self.assertRaises(ConfigError):
                load_config(p)


# =====================================================================================================================
class Holder:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        return self.fn(*a, **k)


class RoutesBase(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        tmp = Path(self._t.name)
        self.store = StrategySelectionStore(tmp / "strategy_selection.json", "choch")
        from trademate import pipeline
        self.approvals = pipeline.ApprovalStore(tmp / "log.jsonl")
        self.wd = WatchdogManager(lambda symbol, strategy_id=None: {"ok": True, "connection": "CONNECTED", "result": {}}, interval=3600)
        patches = [mock.patch.object(app_module, "strategy_store", self.store), mock.patch.object(app_module, "pipeline_store", self.approvals),
                   mock.patch.object(app_module, "watchdog", self.wd)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.store.on_change(app_module._on_strategy_change)
        self.client = app.test_client()
        self.addCleanup(self.wd.stop)

    def select(self, sid):
        return self.client.post("/api/strategy/select", json={"strategy": sid})


class StrategyRoutesTest(RoutesBase):
    def test_catalogue_lists_both_strategies_and_the_selection(self):
        d = self.client.get("/api/strategy/catalogue").get_json()
        self.assertEqual([s["id"] for s in d["strategies"]], ["candle_momentum", "choch", "market_structure", "bull_bear", "trendline"])
        self.assertEqual((d["selected_id"], d["timeframes"], d["stored"]), ("choch", ["H1", "M5"], False))

    def test_select_validates_against_the_registry(self):
        for bad in ("mtf", "CHOCH", "", None, 3, ["market_structure"], "trend; DROP"):
            r = self.select(bad)
            self.assertEqual(r.status_code, 400, repr(bad))
            self.assertFalse(r.get_json()["ok"])
        self.assertEqual(self.store.selected_id(), "choch")
        r = self.select("market_structure")
        self.assertEqual((r.status_code, r.get_json()["selected_id"], r.get_json()["previous_id"]), (200, "market_structure", "choch"))

    def test_select_requires_json_from_our_origin(self):
        r = self.client.post("/api/strategy/select", json={"strategy": "market_structure"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.store.selected_id(), "choch")
        self.assertEqual(self.client.post("/api/strategy/select", data="strategy=trend").status_code, 400)

    def test_selection_survives_a_restart(self):
        self.select("market_structure")
        fresh = StrategySelectionStore(self.store.path, "choch")
        self.assertEqual(fresh.selected_id(), "market_structure")

    def test_analyse_runs_exactly_the_selected_strategy(self):
        spy = Holder(lambda adapter, sid, symbol, settings, now_utc=None, **_kw: {"ok": True, "connection": "CONNECTED", "strategy": {"id": sid}, "result": {"decision": "WAIT"}})
        with mock.patch.object(app_module, "evaluate_selected", spy):
            self.client.get("/api/strategy/analyse?symbol=EURUSD")
            self.select("market_structure")
            d = self.client.get("/api/strategy/analyse?symbol=EURUSD").get_json()
        self.assertEqual([c[0][1] for c in spy.calls], ["choch", "market_structure"])
        self.assertEqual(d["strategy"]["id"], "market_structure")

    def test_analyse_validates_input(self):
        for q in ("", "?symbol=", "?symbol=" + "X" * 40):
            self.assertEqual(self.client.get("/api/strategy/analyse" + q).status_code, 400)

    def test_analyse_fails_safely_without_a_valid_selection(self):
        with mock.patch.object(app_module, "_selected_strategy_id", return_value="mtf"):
            r = self.client.get("/api/strategy/analyse?symbol=EURUSD")
        self.assertEqual(r.status_code, 409)
        self.assertIn("No valid strategy", r.get_json()["message"])

    def test_a_result_that_finishes_after_a_strategy_switch_is_discarded(self):
        def slow(adapter, sid, symbol, settings, now_utc=None, **_kw):
            self.select("market_structure")                                                  # the user switches while the analysis runs
            return {"ok": True, "connection": "CONNECTED", "strategy": {"id": sid}, "result": {"decision": "BUY"}}
        with mock.patch.object(app_module, "evaluate_selected", slow):
            r = self.client.get("/api/strategy/analyse?symbol=EURUSD")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["stale"])
        self.assertNotIn("result", r.get_json())

    def test_trade_review_uses_the_selected_strategy(self):
        spy = Holder(lambda adapter, sid, symbol, settings, now_utc=None, **_kw: {"ok": True, "result": {"decision": "WAIT"}})
        with mock.patch.object(app_module, "evaluate_selected", spy):
            self.select("market_structure")
            d = self.client.get("/api/strategy/mtf/trade-review?symbol=EURUSD").get_json()
        self.assertEqual(spy.calls[0][0][1], "market_structure")
        self.assertFalse(d["approved"])
        self.assertFalse(d["order_sent"])

    def test_legacy_mtf_route_still_exists_but_is_not_selectable(self):
        self.assertIn("/api/strategy/mtf", {r.rule for r in app.url_map.iter_rules()})
        self.assertEqual(self.select("mtf").status_code, 400)


class AutopilotRouteHonestyTest(RoutesBase):
    """Autopilot now exists (trademate/autopilot.py). What must stay true: it is off by default and cannot be armed without a valid config."""

    def test_autopilot_is_off_by_default_and_refuses_invalid_start(self):
        d = self.client.get("/api/autopilot/status").get_json()
        self.assertEqual((d["available"], d["armed"]), (True, False))
        for body in ({}, {"mode": "runs", "value": 0}, {"mode": "bogus", "value": 1}):
            self.assertEqual(self.client.post("/api/autopilot/start", json=body).status_code, 400)
        self.assertFalse(self.client.get("/api/autopilot/status").get_json()["armed"])
        for method in ("put", "delete"):
            self.assertEqual(getattr(self.client, method)("/api/autopilot/status", json={"armed": True}).status_code, 405)

    def test_capability_registry_says_available(self):
        from trademate import strategylab
        self.assertEqual({c["id"]: c["state"] for c in strategylab.capabilities(True)}["autopilot"], strategylab.AVAILABLE)


# =====================================================================================================================
class PendingProposalOnStrategySwitchTest(unittest.TestCase):
    """A proposal made under one strategy must never be executed after the user selects the other."""

    def setUp(self):
        from tests.test_pipeline import PipelineBase
        self.base = PipelineBase("run")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)

    def test_switching_cancels_the_pending_approval_and_nothing_is_sent(self):
        b = self.base
        r = b.approved()
        self.assertEqual(r["approval"]["summary"]["strategy_id"], "choch")
        sw = b.client.post("/api/strategy/select", json={"strategy": "market_structure"}).get_json()
        self.assertEqual(sw["selected_id"], "market_structure")
        self.assertEqual(b.store.get(r["approval"]["id"]).status, "invalidated")
        out = b.execute(r["approval"]["id"])
        self.assertEqual(out["refusal"]["code"], "approval_invalidated")
        self.assertFalse(out["order_submitted"])
        self.assertEqual(b.writes(), [])

    def test_a_stale_approval_is_refused_even_if_the_store_was_not_cleared(self):
        b = self.base
        r = b.approved()
        with mock.patch.object(app_module, "_selected_strategy_id", return_value="market_structure"):
            out = b.execute(r["approval"]["id"])
        self.assertEqual(out["refusal"]["code"], "strategy_changed")
        self.assertEqual(b.writes(), [])
        self.assertEqual(b.store.get(r["approval"]["id"]).status, "invalidated")

    def test_a_result_from_another_strategy_is_refused_at_proposal_time(self):
        b = self.base
        b.client.post("/api/strategy/select", json={"strategy": "market_structure"})
        r = b.propose()                                     # the (patched) evaluator still answers as CHoCH
        self.assertEqual((r["approved"], r["code"]), (False, "strategy_mismatch"))
        self.assertEqual(b.writes(), [])

    def test_switching_back_does_not_resurrect_an_old_approval(self):
        b = self.base
        r = b.approved()
        b.client.post("/api/strategy/select", json={"strategy": "market_structure"})
        b.client.post("/api/strategy/select", json={"strategy": "choch"})
        out = b.execute(r["approval"]["id"])
        self.assertEqual(out["refusal"]["code"], "approval_invalidated")
        self.assertEqual(b.writes(), [])

    def test_reselecting_the_same_strategy_keeps_the_approval(self):
        b = self.base
        r = b.approved()
        b.client.post("/api/strategy/select", json={"strategy": "choch"})
        self.assertEqual(b.store.get(r["approval"]["id"]).status, "issued")

    def test_proposals_and_trades_carry_strategy_identity(self):
        b = self.base
        r = b.approved()
        self.assertEqual((r["qualification"]["proposal"]["strategy_id"], r["qualification"]["proposal"]["strategy_version"]), ("choch", "1.0.0"))
        self.assertEqual(r["approval"]["summary"]["strategy_version"], "1.0.0")
        out = b.execute(r["approval"]["id"])
        self.assertEqual(out["outcome"], "executed")
        log = [json.loads(x) for x in (Path(b._tmp.name) / "pipeline_log.jsonl").read_text().splitlines()]
        self.assertEqual(log[-1]["core"]["strategy_id"], "choch")
        self.assertEqual(log[-1]["approval"]["summary"]["strategy_id"], "choch")


class RealStrategyThroughPipelineTest(unittest.TestCase):
    """The ACTUAL strategy results (not hand-made payloads) through qualification and the real Risk Engine."""

    class Broker(ReadOnlyFeed):
        def get_symbol_spec(self, symbol):
            from trademate.broker.base import SymbolInfo
            return SymbolInfo(name=symbol, description="", digits=2, point=0.01, spread_points=10, contract_size=1.0, volume_min=0.01, volume_max=100.0,
                              volume_step=0.01, currency_base="X", currency_profit="USD", bid=self.bid, ask=self.ask, trade_mode=4, tick_size=0.01,
                              tick_value=0.01, tick_value_profit=0.01, tick_value_loss=0.01)

        def get_account(self):
            from trademate.broker.base import AccountInfo
            return AccountInfo(1, "srv", "b", "demo", "USD", 100, 10000.0, 10000.0, 0.0, 10000.0, None, 0.0)

        def get_positions(self):
            return []

        def get_orders(self):
            return []

        def supports_pending_orders(self):
            return False

        def get_history(self, days=3, limit=500):
            return []

    def run_pipeline(self, sid, m5, h1, bid, ask):
        from trademate import pipeline
        from trademate.risk import RiskRules
        feed = self.Broker({"M5": m5, "H1": h1})
        feed.bid, feed.ask = bid, ask
        evaluate = lambda s: evaluate_selected(feed, sid, s, WIDE, now_utc=NOW)
        return pipeline.propose(feed, Settings(), RiskRules(1.0, 3.0, 3.0, 3), 100.0, "EURUSD", pipeline.ApprovalStore(), evaluate=evaluate, now=NOW,
                                strategy_id=sid)

    def test_choch_buy_traverses_strategy_risk_and_eligibility(self):
        rep = self.run_pipeline("choch", m5_choch_up(), h1_up(), 147.95, 148.0)
        self.assertTrue(rep.ok, (rep.code, rep.message, rep.reasons))
        self.assertEqual(rep.approval["summary"]["strategy_id"], "choch")
        self.assertLessEqual(rep.approval["summary"]["risk_percent"], 1.0)

    def test_trend_buy_and_sell_traverse_the_pipeline(self):
        buy = self.run_pipeline("trend", m5_trend_up(), h1_up(), 171.95, 172.0)
        self.assertTrue(buy.ok, (buy.code, buy.message, buy.reasons))
        sell = self.run_pipeline("trend", m5_trend_down(), h1_down(), 88.0, 88.05)
        self.assertTrue(sell.ok, (sell.code, sell.message, sell.reasons))
        self.assertEqual(sell.approval["direction"], "sell")

    def test_watch_and_wait_from_the_real_strategies_never_reach_risk(self):
        for sid, m5 in (("choch", zigzag_end([100, 110] * 7 + [100, 105], 6, M5)), ("trend", m5_trend_up(final=166.0))):
            with mock.patch("trademate.pipeline.evaluate_trade") as risk:
                rep = self.run_pipeline(sid, m5, h1_up(), 100.0, 100.05)
            risk.assert_not_called()
            self.assertFalse(rep.ok)
            self.assertEqual(rep.code, "not_qualified")

    def test_a_strategy_result_labelled_for_the_other_strategy_is_refused(self):
        from trademate import pipeline
        payload = {"ok": True, "result": run("choch", m5_choch_up(), h1_up()), "connection": "CONNECTED"}
        self.assertTrue(pipeline.qualify(payload, "choch").qualified)
        q = pipeline.qualify(payload, "trend")
        self.assertEqual((q.qualified, q.code), (False, pipeline.STRATEGY_MISMATCH))
        self.assertEqual(pipeline.qualify({"ok": True, "result": {**payload["result"], "strategy": None}}, "choch").code, pipeline.STRATEGY_MISMATCH)

    def test_qualify_requires_only_the_strategys_own_timeframes(self):
        from trademate import pipeline
        payload = {"ok": True, "result": run("trend", m5_trend_up(), h1_up()), "connection": "CONNECTED"}
        self.assertEqual(set(payload["result"]["data"]), {"H1", "M5"})
        self.assertTrue(pipeline.qualify(payload, "trend").qualified)                 # no M15 / H4 data needed or consulted
        payload["result"]["data"]["H1"]["status"] = "stale"
        self.assertEqual(pipeline.qualify(payload, "trend").code, pipeline.DATA_UNUSABLE)


# =====================================================================================================================
class WatchdogStrategyTest(unittest.TestCase):
    def manager(self, evaluate):
        wd = WatchdogManager(evaluate, interval=3600)
        self.addCleanup(wd.stop)
        return wd

    def test_watchdog_is_bound_to_the_strategy_it_started_with(self):
        seen = []

        def ev(symbol, strategy_id=None):
            seen.append(strategy_id)
            return {"ok": True, "connection": "CONNECTED", "result": run(strategy_id, m5_choch_up() if strategy_id == "choch" else m5_trend_up(), h1_up())}

        wd = self.manager(ev)
        wd.start("TEST", "trend")
        for _ in range(100):
            if seen:
                break
            import time
            time.sleep(0.02)
        self.assertEqual(seen[0], "trend")
        self.assertEqual(wd.status()["strategy_id"], "trend")

    def test_a_result_from_another_strategy_stops_monitoring_and_creates_no_events(self):
        import time

        def ev(symbol, strategy_id=None):
            return {"ok": True, "connection": "CONNECTED", "result": run("choch", m5_choch_up(), h1_up())}

        wd = self.manager(ev)
        wd.start("TEST", "trend")
        for _ in range(150):
            time.sleep(0.02)
            if not wd.status()["active"]:
                break
        st = wd.status()
        self.assertFalse(st["active"])
        self.assertEqual(st["events"], [])
        self.assertEqual(st["candidate_events"], [])

    def test_strategy_change_stops_the_watchdog_and_forgets_candidates(self):
        import time
        wd = self.manager(lambda symbol, strategy_id=None: {"ok": True, "connection": "CONNECTED", "result": run("trend", m5_trend_up(final=166.0), h1_up())})
        wd.start("TEST", "trend")
        for _ in range(150):
            time.sleep(0.02)
            if wd.status()["candidate_states"]:
                break
        self.assertTrue(wd.status()["candidate_states"])
        wd.reset_for_strategy_change()
        for _ in range(150):
            time.sleep(0.02)
            if not wd.status()["active"]:
                break
        st = wd.status()
        self.assertEqual((st["active"], st["candidate_states"], st["candidate_events"]), (False, [], []))

    def test_choch_alerts_only_for_the_choch_strategy_and_only_on_m5(self):
        wd = WatchdogManager(lambda s: None, interval=3600)
        r = run("choch", m5_choch_up(), h1_up())
        ev = wd._new_choch_events("TEST", r, None)
        self.assertTrue(ev)
        self.assertTrue(all(e["timeframe"] == "M5" and e["strategy_id"] == "choch" for e in ev))
        self.assertEqual(wd._new_choch_events("TEST", r, None), [])                      # deduplicated
        t = run("trend", m5_trend_up(), h1_up())
        self.assertEqual(WatchdogManager(lambda s: None, interval=3600)._new_choch_events("TEST", t, None), [])

    def test_setup_events_carry_the_strategy_and_are_deduplicated(self):
        wd = WatchdogManager(lambda s: None, interval=3600)
        r = run("trend", m5_trend_up(), h1_up())
        e = wd._new_setup_event("TEST", r)
        self.assertEqual((e["strategy_id"], e["strategy_version"], e["setup_family"], e["decision"]), ("trend", "1.0.0", "trend", "BUY"))
        self.assertIsNone(wd._new_setup_event("TEST", r))
        self.assertIsNone(wd._new_setup_event("TEST", run("trend", m5_trend_up(final=166.0), h1_up())))

    def test_stale_data_creates_no_setup_alert(self):
        wd = WatchdogManager(lambda s: None, interval=3600)
        self.assertIsNone(wd._new_setup_event("TEST", run("choch", m5_choch_up(), h1_up(), now=NOW + 9000)))


class WatchdogRouteStrategyTest(RoutesBase):
    def test_start_binds_the_selected_strategy_and_a_switch_stops_it(self):
        import time
        info = mock.Mock()
        info.name = "EURUSD"
        with mock.patch.object(app_module.adapter, "get_connection_status", return_value=mock.Mock(state=app_module.ConnectionState.CONNECTED, message="ok")), \
                mock.patch.object(app_module.adapter, "get_symbol_info", return_value=info):
            self.select("market_structure")
            r = self.client.post("/api/strategylab/watchdog/start", json={"symbol": "EURUSD"})
            self.assertEqual(r.status_code, 202)
            self.assertEqual(r.get_json()["watchdog"]["strategy_id"], "market_structure")
            self.select("choch")
            for _ in range(150):
                time.sleep(0.02)
                if not self.wd.status()["active"]:
                    break
        self.assertFalse(self.wd.status()["active"])
        self.assertIsNone(self.wd.status()["strategy_id"])


if __name__ == "__main__":
    unittest.main()
