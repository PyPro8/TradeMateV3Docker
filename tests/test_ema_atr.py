"""EMA 20/50 + ATR strategy: focused tests. Simulated candles and the in-memory fake bridge only: NO real order can be placed.

    python3 -m unittest tests.test_ema_atr -v
"""

import dataclasses
import unittest
from unittest import mock

import app as app_module
from tests.test_fixed_target_execution import FixedTargetBase
from tests.test_pipeline import strategy_payload
from trademate import pipeline
from trademate.decision_hub import DecisionHub
from trademate.strategies import ema_atr
from trademate.strategies.base import StrategyParams
from trademate.strategies.registry import CATALOGUE, STRATEGY_IDS, evaluate_strategy_result, get_strategy
from trademate.strategies.service import evaluate_selected, strategy_params
from trademate.strategy import StrategyConfig, prepare_series
from trademate.strategy.watchdog import WatchdogManager

SEC = 300
T0 = 1_700_000_000 - (1_700_000_000 % SEC)
CFG = StrategyConfig()
P = StrategyParams(exec_tf="M5", tick_size=0.00001)


def candles(closes, t0=T0, scale=1.0, shift=0.0, rng=0.0004):
    """Closed M5 rows from closes: open = previous close, high/low a fixed distance beyond the body."""
    rows, prev = [], closes[0]
    for k, c in enumerate(closes):
        c = c * scale + shift
        o = prev
        rows.append([t0 + k * SEC, o, max(o, c) + rng, min(o, c) - rng, c])
        prev = c
    return rows


def ramp(a, b, n):
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def crosses(rows, fast=20, slow=50):
    cl = [r[4] for r in rows]
    f, s = ema_atr.ema(cl, fast), ema_atr.ema(cl, slow)
    out = []
    for i in range(slow, len(rows)):
        if f[i - 1] < s[i - 1] and f[i] > s[i]:
            out.append((i, "bullish"))
        if f[i - 1] > s[i - 1] and f[i] < s[i]:
            out.append((i, "bearish"))
    return out


def v_shape(base=1.1000):
    """Falls 180 candles then rises 100: one bullish crossover on the way up, after more than the 150 candles of history the strategy needs."""
    return candles(ramp(base + 0.0300, base, 180) + ramp(base, base + 0.0300, 100)[1:])


def inverted_v(base=1.1000):
    return candles(ramp(base, base + 0.0300, 180) + ramp(base + 0.0300, base, 100)[1:])


def evaluate(rows, params=P, now=None, symbol="EURUSD", clock_now=None):
    now = now if now is not None else rows[-1][0] + SEC + 20
    series = {"M5": prepare_series([list(r) for r in rows], "M5", now)}
    clock = {"known": True, "source": "test", "detail": "", "now_server": clock_now if clock_now is not None else now, "offset_seconds": 0}
    return evaluate_strategy_result("ema_atr", symbol, series, params, CFG, clock), series


def at_cross(rows, which):
    idx = next(i for i, d in crosses(rows) if d == which)
    return idx, rows[:idx + 1]


# ---------------------------------------------------------------------------------------------------------------- indicators
class IndicatorMathTest(unittest.TestCase):
    def test_ema_known_values(self):
        out = ema_atr.ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertEqual(out[2:], [2.0, 3.0, 4.0])             # seed = SMA(1,2,3) = 2; alpha 0.5

    def test_atr_known_values_wilder(self):
        rows = [[0, 10, 11, 9, 10], [1, 10, 12, 9, 11], [2, 11, 13, 10, 12], [3, 12, 14, 11, 13], [4, 13, 16, 12, 15]]
        # TR: 3, 3 (h-l 3; |13-11| 2), 3, 4  -> ATR(3) at index 3 = 3.0; at index 4 = (3*2 + 4)/3
        out = ema_atr.atr(rows, 3)
        self.assertEqual(out[:3], [None, None, None])
        self.assertAlmostEqual(out[3], (3 + 3 + 3) / 3)
        self.assertAlmostEqual(out[4], (out[3] * 2 + 4) / 3)

    def test_atr_uses_gaps_and_is_price_unit_not_pips(self):
        rows = [[0, 100, 101, 99, 100], [1, 105, 106, 104, 105], [2, 105, 106, 104, 105]]   # a gap up: TR = max(2, |106-100|, |104-100|) = 6
        self.assertAlmostEqual(ema_atr.atr(rows, 1)[1], 6.0)

    def test_flat_market_atr_is_zero(self):
        flat = [[i, 5, 5, 5, 5] for i in range(30)]
        self.assertEqual(ema_atr.atr(flat, 14)[-1], 0.0)

    def test_too_short_series_gives_nothing(self):
        self.assertEqual(ema_atr.ema([1, 2], 5), [None, None])
        self.assertTrue(all(v is None for v in ema_atr.atr([[0, 1, 1, 1, 1]] * 5, 14)))


# ---------------------------------------------------------------------------------------------------------------- direction filter
class DirectionFilterTest(unittest.TestCase):
    def test_ema20_above_ema50_longs_only_but_no_entry_without_crossover(self):
        rows = candles(ramp(1.10, 1.14, 200))
        res, _ = evaluate(rows)
        self.assertEqual(res["decision"], "WAIT")
        self.assertEqual(res["context"]["label"], "Longs only")
        self.assertGreater(res["indicators"]["ema_fast"], res["indicators"]["ema_slow"])
        self.assertIsNone(res["indicators"]["crossover"])

    def test_ema20_below_ema50_shorts_only_but_no_entry_without_crossover(self):
        rows = candles(ramp(1.14, 1.10, 200))
        res, _ = evaluate(rows)
        self.assertEqual(res["decision"], "WAIT")
        self.assertEqual(res["context"]["label"], "Shorts only")
        self.assertLess(res["indicators"]["ema_fast"], res["indicators"]["ema_slow"])

    def test_equal_emas_wait(self):
        rows = candles([1.1] * 200)
        res, _ = evaluate(rows)
        self.assertEqual(res["decision"], "WAIT")
        self.assertEqual(res["context"]["label"], "Wait")


# ---------------------------------------------------------------------------------------------------------------- crossover
class CrossoverTest(unittest.TestCase):
    def setUp(self):
        self.up = v_shape()
        self.dn = inverted_v()

    def test_bullish_crossover_gives_buy(self):
        idx, rows = at_cross(self.up, "bullish")
        res, _ = evaluate(rows)
        self.assertEqual(res["decision"], "BUY", res["reasons"])
        self.assertEqual(res["direction"], "bullish")
        self.assertEqual(res["indicators"]["crossover"], "bullish")
        self.assertEqual(res["indicators"]["signal_candle_time"], rows[-1][0])
        self.assertEqual(res["setup_status"], "confirmed")
        self.assertEqual(res["strategy"]["id"], "ema_atr")

    def test_bearish_crossover_gives_sell(self):
        idx, rows = at_cross(self.dn, "bearish")
        res, _ = evaluate(rows)
        self.assertEqual(res["decision"], "SELL", res["reasons"])
        self.assertEqual(res["indicators"]["crossover"], "bearish")

    def test_the_crossover_is_signalled_once_and_not_on_later_candles(self):
        idx, rows = at_cross(self.up, "bullish")
        first = evaluate(rows)[0]
        self.assertEqual(first["decision"], "BUY")
        for extra in (1, 2, 10):
            later = evaluate(self.up[:idx + 1 + extra])[0]
            self.assertEqual(later["decision"], "WAIT", f"{extra} candle(s) after the crossover")
            self.assertEqual(later["context"]["label"], "Longs only")
        before = evaluate(self.up[:idx])[0]
        self.assertEqual(before["decision"], "WAIT")

    def test_same_crossover_is_the_same_setup_every_time(self):
        idx, rows = at_cross(self.up, "bullish")
        a = evaluate(rows)[0]["setup_candidates"][0]["candidate_id"]
        b = evaluate(rows, now=rows[-1][0] + SEC + 200)[0]["setup_candidates"][0]["candidate_id"]
        self.assertEqual(a, b)                                  # repeated looks at one candle: one identity (the ledger / Watchdog dedupe on it)
        idx2, rows2 = at_cross(self.dn, "bearish")
        self.assertNotEqual(a, evaluate(rows2)[0]["setup_candidates"][0]["candidate_id"])

    def test_only_one_crossover_per_cross_in_a_whipsaw(self):
        whip = candles(ramp(1.12, 1.10, 80) + ramp(1.10, 1.12, 80)[1:] + ramp(1.12, 1.10, 80)[1:] + ramp(1.10, 1.12, 80)[1:])
        sigs = [i for i, _ in crosses(whip) if i + 1 >= ema_atr.min_candles(P)]          # a cross before the history requirement is met is correctly NOT signalled
        buys = [i for i in range(60, len(whip)) if evaluate(whip[:i + 1])[0]["decision"] in ("BUY", "SELL")]
        self.assertEqual(buys, sigs)                            # exactly one signal per crossover candle, none in between


# ---------------------------------------------------------------------------------------------------------------- exits
class ExitLevelsTest(unittest.TestCase):
    def check(self, rows, side):
        res, _ = evaluate(rows, params=StrategyParams(exec_tf="M5"))        # no tick snapping: exact 2x / 4x
        a = ema_atr.atr(rows, 14)[-1]
        entry = rows[-1][4]
        self.assertEqual(res["decision"], side, res["reasons"])
        self.assertAlmostEqual(res["indicators"]["atr"], a)
        self.assertAlmostEqual(res["entry"]["price"], entry)
        sgn = 1 if side == "BUY" else -1
        self.assertAlmostEqual(res["stop_loss"]["price"], entry - sgn * 2.0 * a, places=9)
        self.assertAlmostEqual(res["take_profit_1"]["price"], entry + sgn * 4.0 * a, places=9)
        self.assertAlmostEqual(res["risk_reward"]["rr_tp1"], 2.0, places=6)
        return res

    def test_buy_stop_below_target_above(self):
        res = self.check(at_cross(v_shape(), "bullish")[1], "BUY")
        self.assertLess(res["stop_loss"]["price"], res["entry"]["price"])
        self.assertGreater(res["take_profit_1"]["price"], res["entry"]["price"])

    def test_sell_stop_above_target_below(self):
        res = self.check(at_cross(inverted_v(), "bearish")[1], "SELL")
        self.assertGreater(res["stop_loss"]["price"], res["entry"]["price"])
        self.assertLess(res["take_profit_1"]["price"], res["entry"]["price"])

    def test_works_in_price_units_on_a_non_forex_scale(self):
        rows = candles(ramp(1000.0, 1030.0, 180)[::-1] + ramp(1000.0, 1030.0, 100)[1:], rng=0.4)      # an index-like price level, no pip
        idx, cut = at_cross(rows, "bullish")
        res, _ = evaluate(cut, params=StrategyParams(exec_tf="M5", tick_size=0.001), symbol="Boom 100 Index")
        self.assertEqual(res["decision"], "BUY", res["reasons"])
        self.assertAlmostEqual(res["entry"]["price"] - res["stop_loss"]["price"], 2 * res["indicators"]["atr"], places=2)

    def test_prices_snap_to_the_tick_without_being_closer(self):
        rows = at_cross(v_shape(), "bullish")[1]
        raw, _ = evaluate(rows, params=StrategyParams(exec_tf="M5"))
        snapped, _ = evaluate(rows, params=StrategyParams(exec_tf="M5", tick_size=0.0005))
        self.assertLessEqual(snapped["stop_loss"]["price"], raw["stop_loss"]["price"] + 1e-12)          # BUY stop never closer
        self.assertGreaterEqual(snapped["take_profit_1"]["price"], raw["take_profit_1"]["price"] - 1e-12)
        for key in ("stop_loss", "take_profit_1"):
            self.assertAlmostEqual(snapped[key]["price"] / 0.0005, round(snapped[key]["price"] / 0.0005), places=6)

    def test_configurable_period_and_multipliers(self):
        rows = at_cross(v_shape(), "bullish")[1]
        p = StrategyParams(exec_tf="M5", atr_period=7, atr_sl_mult=1.5, atr_tp_mult=3.0)
        res, _ = evaluate(rows, params=p)
        a = ema_atr.atr(rows, 7)[-1]
        self.assertAlmostEqual(res["indicators"]["atr"], a)
        self.assertAlmostEqual(res["entry"]["price"] - res["stop_loss"]["price"], 1.5 * a, places=9)


# ---------------------------------------------------------------------------------------------------------------- data safety
class DataSafetyTest(unittest.TestCase):
    def test_insufficient_history_is_refused(self):
        idx, rows = at_cross(v_shape(), "bullish")
        res, _ = evaluate(rows[-60:])                          # 60 closed candles < 150 needed
        self.assertNotIn(res["decision"], ("BUY", "SELL"))
        self.assertTrue(any("history" in r.lower() or "insufficient" in r.lower() or "enough" in r.lower() for r in res["reasons"]), res["reasons"])

    def test_exactly_the_minimum_is_accepted_and_one_less_is_not(self):
        need = ema_atr.min_candles(P)
        self.assertEqual(need, 150)
        res, _ = evaluate(candles(ramp(1.10, 1.14, need - 1)))
        self.assertNotIn(res["decision"], ("BUY", "SELL"))
        self.assertTrue(any("history" in g["id"] or "data" in g["id"] for g in res["gates"] if not g["passed"]))

    def test_incomplete_candle_values_are_refused(self):
        idx, rows = at_cross(v_shape(), "bullish")
        for bad in (float("nan"), None):
            broken = [list(r) for r in rows]
            broken[-30][2] = bad
            res, _ = evaluate(broken)
            self.assertNotIn(res["decision"], ("BUY", "SELL"), bad)

    def test_inconsistent_candle_is_refused(self):
        idx, rows = at_cross(v_shape(), "bullish")
        broken = [list(r) for r in rows]
        broken[-40][2], broken[-40][3] = broken[-40][3], broken[-40][2]          # high below low
        res, _ = evaluate(broken)
        self.assertNotIn(res["decision"], ("BUY", "SELL"))

    def test_a_crossover_that_exists_only_in_the_forming_candle_is_never_a_signal(self):
        idx, rows = at_cross(v_shape(), "bullish")
        before = rows[:-1]                                      # the candle BEFORE the crossover: still WAIT on closed data
        forming = [list(r) for r in before] + [list(rows[-1])]
        now = forming[-1][0] + 100                              # that last candle is still forming at this moment
        res, series = evaluate(forming, now=now)
        self.assertIsNotNone(series["M5"].forming)
        self.assertEqual(res["decision"], "WAIT")
        self.assertEqual(len(series["M5"].closed), len(before))

    def test_a_candle_not_yet_closed_by_the_clock_is_refused(self):
        idx, rows = at_cross(v_shape(), "bullish")
        now = rows[-1][0] + SEC + 20
        series = {"M5": prepare_series([list(r) for r in rows], "M5", now)}
        series["M5"] = dataclasses.replace(series["M5"], newest_closed_end=now + 100)
        clock = {"known": True, "source": "test", "detail": "", "now_server": now, "offset_seconds": 0}
        res = evaluate_strategy_result("ema_atr", "EURUSD", series, P, CFG, clock)
        self.assertNotIn(res["decision"], ("BUY", "SELL"))

    def test_stale_or_missing_data_never_trades(self):
        idx, rows = at_cross(v_shape(), "bullish")
        res, _ = evaluate(rows, now=rows[-1][0] + 20 * SEC)
        self.assertNotIn(res["decision"], ("BUY", "SELL"))
        res = evaluate_strategy_result("ema_atr", "EURUSD", {}, P, CFG, {"known": False})
        self.assertNotIn(res["decision"], ("BUY", "SELL"))

    def test_zero_or_invalid_atr_is_refused(self):
        idx, rows = at_cross(v_shape(), "bullish")
        for bad in (0.0, float("nan"), -1.0, None):
            with mock.patch.object(ema_atr, "atr", lambda r, p, bad=bad: [bad] * len(r)):
                res, _ = evaluate(rows)
            self.assertNotIn(res["decision"], ("BUY", "SELL"), bad)
            self.assertTrue(any(g["id"] == "atr_valid" and not g["passed"] for g in res["gates"]), bad)

    def test_invalid_settings_are_refused_not_replaced(self):
        idx, rows = at_cross(v_shape(), "bullish")
        for p in (StrategyParams(exec_tf="M5", ema_fast=50, ema_slow=20), StrategyParams(exec_tf="M5", atr_sl_mult=0.0),
                  StrategyParams(exec_tf="M5", atr_period=0), StrategyParams(exec_tf="M5", atr_tp_mult=float("nan"))):
            res, _ = evaluate(rows, params=p)
            self.assertNotIn(res["decision"], ("BUY", "SELL"), p)

    def test_uses_the_selected_timeframe(self):
        idx, rows = at_cross(v_shape(), "bullish")
        now = rows[-1][0] + SEC + 20
        res = evaluate_strategy_result("ema_atr", "EURUSD", {"M5": prepare_series([list(r) for r in rows], "M5", now)}, StrategyParams(exec_tf="M15"), CFG,
                                       {"known": True, "source": "test", "detail": "", "now_server": now, "offset_seconds": 0})
        self.assertNotIn(res["decision"], ("BUY", "SELL"))      # M15 data was not supplied: it does not fall back to M5
        self.assertEqual(res["execution_timeframe"], "M15")


# ---------------------------------------------------------------------------------------------------------------- integration
class RegistrationAndServiceTest(unittest.TestCase):
    def test_retired_from_the_selector_but_still_resolvable_for_history(self):
        from trademate.strategies.registry import RETIRED, get_any_strategy
        self.assertNotIn("ema_atr", STRATEGY_IDS)                                            # no longer one of the five selectable strategies
        self.assertIsNone(get_strategy("ema_atr"))
        d = get_any_strategy("ema_atr")                                                      # but its definition and code are kept (old decisions, history, shared indicators)
        self.assertIs(d, RETIRED["ema_atr"])
        self.assertEqual((d.name, d.rule_id), ("EMA 20/50 + ATR", "EMA-ATR-v1"))
        self.assertFalse(d.uses_context)
        self.assertEqual(list(CATALOGUE), ["candle_momentum", "choch", "market_structure", "bull_bear", "trendline"])

    def test_service_reads_settings_and_the_selected_timeframe(self):
        from trademate.config import Settings, StrategySettings
        st = Settings(strategy=StrategySettings(atr_period=9, atr_sl_multiplier=1.5, atr_tp_multiplier=3.0, ema_fast_period=10, ema_slow_period=30))
        from trademate.strategies.registry import RETIRED
        p = strategy_params(st, 0.00001, RETIRED["ema_atr"], "H1")
        self.assertEqual((p.exec_tf, p.atr_period, p.atr_sl_mult, p.atr_tp_mult, p.ema_fast, p.ema_slow, p.tick_size), ("H1", 9, 1.5, 3.0, 10, 30, 0.00001))
        other = strategy_params(st, 0.00001, CATALOGUE["choch"], "H1")
        self.assertEqual((other.atr_period, other.ema_fast, other.tick_size), (14, 20, None))      # the other strategies see none of it

    def test_result_has_everything_the_spec_lists(self):
        idx, rows = at_cross(v_shape(), "bullish")
        res, _ = evaluate(rows)
        for key in ("decision", "direction", "entry", "stop_loss", "take_profit_1", "risk_reward", "indicators", "reasons", "gates", "setup_candidates", "setup_invalidation"):
            self.assertIn(key, res)
        for key in ("ema_fast", "ema_slow", "atr", "crossover"):
            self.assertIsNotNone(res["indicators"][key])
        self.assertIn("crossed above", res["setup_candidates"][0]["premise"])

    def test_config_rejects_bad_ema_atr_settings(self):
        import tempfile
        from pathlib import Path
        from trademate.config import ConfigError, load_config
        base = Path("config/settings.toml").read_text(encoding="utf-8")
        for key, bad in (("ema_fast_period = 20", "ema_fast_period = 60"), ("atr_period = 14", "atr_period = 0"), ("atr_sl_multiplier = 2.0", "atr_sl_multiplier = 0"),
                         ("atr_tp_multiplier = 4.0", 'atr_tp_multiplier = "x"')):
            with tempfile.TemporaryDirectory() as d:
                f = Path(d) / "s.toml"
                f.write_text(base.replace(key, bad), encoding="utf-8")
                with self.assertRaises(ConfigError, msg=bad):
                    load_config(f)

    def test_watchdog_and_hub_carry_the_real_result(self):
        idx, rows = at_cross(v_shape(), "bullish")
        res, _ = evaluate(rows)
        hub = DecisionHub(stale_after=120)
        wd = WatchdogManager(lambda symbol, strategy_id=None, exec_tf=None: {"ok": True, "connection": "CONNECTED", "result": res}, interval=0.05, hub=hub)
        self.addCleanup(wd.stop)
        wd.start("EURUSD", "ema_atr", "M5")
        import time
        end = time.time() + 3
        while time.time() < end and not (hub.get("EURUSD", "ema_atr", "M5") or {}).get("decision"):
            time.sleep(0.02)
        rec = hub.get("EURUSD", "ema_atr", "M5")
        self.assertEqual(rec["decision"], "BUY")
        self.assertEqual(rec["result"]["indicators"]["crossover"], "bullish")


class PipelineWithRiskEngineTest(FixedTargetBase):
    """The real pipeline + Risk Engine + fake bridge. The fake bridge records orders; nothing real is sent."""

    def setUp(self):
        super().setUp()
        # EMA 20/50 + ATR is retired from the selector, so it cannot be selected any more. These tests use it only as a vehicle for the pipeline's generic guarantees,
        # so the pipeline is told that it is "the selected strategy" (as it would be for an old session or a historical approval).
        for target, value in (("selected_id", "ema_atr"),):
            p = mock.patch.object(app_module.strategy_store, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def result_at_live_price(self, which="bullish"):
        _, ask = self.live()
        base = v_shape() if which == "bullish" else inverted_v()
        idx, rows = at_cross(base, which)
        shift = ask - rows[-1][4]
        rows = candles([r[4] for r in rows], shift=shift)
        res, _ = evaluate(rows, params=StrategyParams(exec_tf="M5", tick_size=0.00001))
        return res

    def feed(self, res):
        self.holder.payload = {"ok": True, "connection": "CONNECTED", "result": res, "strategy": res["strategy"]}

    def test_result_is_accepted_by_the_pipeline_qualifier(self):
        res = self.result_at_live_price()
        self.assertEqual(res["decision"], "BUY", res["reasons"])
        q = pipeline.qualify({"ok": True, "connection": "CONNECTED", "result": res}, "ema_atr")
        self.assertTrue(q.qualified, q.reasons)
        self.assertEqual(q.proposal.strategy_id, "ema_atr")
        self.assertIsNone(q.proposal.target_pips)            # ATR exits are price levels: no pip target machinery

    def test_risk_approved_order_goes_through_the_existing_path(self):
        res = self.result_at_live_price()
        self.feed(res)
        self.lots.set("EURUSD", 0.04)
        r = self.propose()
        self.assertTrue(r["approved"], r)
        out = self.execute(self.approval_id(r))
        self.assertEqual(out["outcome"], "executed", out)
        w = self.writes()
        self.assertEqual(len(w), 1)
        self.assertAlmostEqual(float(w[0]["volume"]), 0.04)
        self.assertAlmostEqual(float(w[0]["sl"]), res["stop_loss"]["price"], places=4)
        self.assertAlmostEqual(float(w[0]["tp"]), res["take_profit_1"]["price"], places=4)

    def test_risk_engine_rejection_never_reaches_the_broker(self):
        self.feed(self.result_at_live_price())
        self.lots.set("EURUSD", 50.0)                         # valid for the instrument, far beyond the risk limits
        r = self.propose()
        self.assertFalse(r["approved"])
        self.assertEqual(self.writes(), [])

    def test_invalid_lot_is_refused_before_risk_or_broker(self):
        self.feed(self.result_at_live_price())
        self.lots.set("EURUSD", 0.015)
        r = self.propose()
        self.assertFalse(r["approved"])
        self.assertEqual(r["code"], pipeline.VOLUME_INVALID)
        self.assertEqual(self.writes(), [])

    def test_same_crossover_cannot_create_a_second_order(self):
        res = self.result_at_live_price()
        self.feed(res)
        self.lots.set("EURUSD", 0.04)
        self.execute(self.approval_id(self.approved()))
        again = self.propose()
        self.assertFalse(again["approved"])
        self.assertEqual(again["code"], pipeline.DUPLICATE_SETUP)
        self.assertEqual(len(self.writes()), 1)

    def test_selection_route_refuses_the_retired_strategy_and_lists_the_five(self):
        r = self.client.post("/api/selection", json={"symbol": "EURUSD", "strategy": "ema_atr", "timeframe": "H1"})
        self.assertEqual(r.status_code, 400, r.get_json())                                   # retired: cannot be selected any more
        cat = self.client.get("/api/strategy/catalogue").get_json()
        self.assertNotIn("ema_atr", [s["id"] for s in cat["strategies"]])
        self.assertEqual(len(cat["strategies"]), 5)


if __name__ == "__main__":
    unittest.main()
