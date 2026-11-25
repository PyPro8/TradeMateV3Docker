"""Phase 7B: liquidity in /api/candles and /api/analysis, and its settings. Stub feed (read-only), no broker, no orders.

    python3 -m unittest tests.test_liquidity_api -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.test_analysis import Feed
from tests.test_liquidity_engine import RANGE, SweepDetection, mk, pid
from trademate.chart_settings import ChartSettingsStore

WICK = mk(SweepDetection.WICK)            # equal highs 10 swept by a wick on candle 11 (close back inside), equal lows 6 unswept


class LiqFeed(Feed):
    """Feed whose symbol spec can lack a tick size, to prove liquidity fails safe instead of guessing one."""

    def __init__(self, data, tick=0.01):
        super().__init__(data)
        self.tick = tick

    def get_symbol_spec(self, symbol):
        return mock.Mock(digits=2, tick_size=self.tick)


class Base(unittest.TestCase):
    def use(self, feed, **settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ChartSettingsStore(Path(tmp.name) / "cs.json")
        if settings:
            store.update(settings)
        for target, value in (("adapter", feed), ("chart_store", store)):
            p = mock.patch.object(app_module, target, value)
            p.start()
            self.addCleanup(p.stop)
        return app.test_client(), store

    def candles(self, c, **extra):
        q = "&".join(f"{k}={v}" for k, v in {"symbol": "EURUSD", "timeframe": "H1", "structure": 1, "liquidity": 1, "swing_left": 2,
                                              "swing_right": 2, **extra}.items())
        return c.get("/api/candles?" + q).get_json()


class ChartPayload(Base):
    def test_liquidity_is_only_added_when_asked_for_and_never_without_structure(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): WICK}))
        self.assertNotIn("liquidity", self.candles(c, liquidity=0))
        self.assertNotIn("liquidity", c.get("/api/candles?symbol=EURUSD&timeframe=H1&liquidity=1").get_json())
        liq = self.candles(c)["liquidity"]
        self.assertTrue(liq["available"])
        self.assertIn("not a view of actual orders", liq["note"])

    def test_pools_and_the_sweep_match_the_engine_for_the_saved_settings(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): WICK}))
        liq = self.candles(c)["liquidity"]
        ids = {p["id"] for p in liq["pools"]}
        self.assertEqual(ids, {pid("BSL", 2), pid("SSL", 4)})
        bsl = next(p for p in liq["pools"] if p["side"] == "BSL")
        self.assertEqual((bsl["kind"], bsl["state"], bsl["level"]), ("equal", "SWEPT", 10))
        self.assertEqual(len(liq["sweeps"]), 1)
        self.assertEqual((liq["sweeps"][0]["mode"], liq["sweeps"][0]["close_beyond"]), ("wick", False))

    def test_saved_settings_change_the_analysis_and_persist(self):
        c, store = self.use(LiqFeed({("EURUSD", "H1"): WICK}), liquidity_sweep_mode="close")
        self.assertEqual(self.candles(c)["liquidity"]["sweeps"], [])                              # close mode: the wick alone is not a sweep
        store.update({"liquidity_sweep_mode": "wick"})
        self.assertEqual(len(self.candles(c)["liquidity"]["sweeps"]), 1)
        rows = [(8, 6, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 6, 7), (9, 7, 8), (10.05, 8, 9), (9, 7, 8), (8, 6, 7)]
        c2, store2 = self.use(LiqFeed({("EURUSD", "H1"): mk(rows)}), liquidity_tolerance_ticks=1)      # 1 tick = 0.01: 10 and 10.05 are NOT equal
        self.assertEqual(len([p for p in self.candles(c2)["liquidity"]["pools"] if p["side"] == "BSL"]), 2)
        store2.update({"liquidity_tolerance_ticks": 10})                                                # 10 ticks = 0.10: equal
        pools = [p for p in self.candles(c2)["liquidity"]["pools"] if p["side"] == "BSL"]
        self.assertEqual((len(pools), pools[0]["kind"], pools[0]["level"]), (1, "equal", 10.05))
        self.assertEqual(ChartSettingsStore(store2.path).load().liquidity_tolerance_ticks, 10)         # persisted for a new store instance

    def test_tolerance_follows_the_symbols_own_tick_size(self):
        rows = [(8, 6, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 6, 7), (9, 7, 8), (10.05, 8, 9), (9, 7, 8), (8, 6, 7)]
        for tick, equal in ((0.01, True), (0.001, False)):                 # 10 ticks: 0.10 vs 0.01 price units
            c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(rows)}, tick=tick), liquidity_tolerance_ticks=10)
            pools = [p for p in self.candles(c)["liquidity"]["pools"] if p["side"] == "BSL"]
            self.assertEqual(len(pools) == 1, equal, tick)

    def test_missing_tick_size_fails_safe_and_fabricates_nothing(self):
        for tick in (None, 0, -1, float("nan")):
            c, _ = self.use(LiqFeed({("EURUSD", "H1"): WICK}, tick=tick))
            res = self.candles(c)
            self.assertTrue(res["ok"])                                       # candles and structure still work
            self.assertIn("structure", res)
            self.assertFalse(res["liquidity"]["available"])
            self.assertNotIn("pools", res["liquidity"])
            self.assertIn("tick size", res["liquidity"]["reason"])

    def test_a_failing_spec_read_does_not_break_the_chart(self):
        feed = LiqFeed({("EURUSD", "H1"): WICK})
        feed.get_symbol_spec = mock.Mock(side_effect=app_module.BrokerError("no spec"))
        c, _ = self.use(feed)
        res = self.candles(c)
        self.assertTrue(res["ok"] and len(res["candles"]) == len(WICK))
        self.assertFalse(res["liquidity"]["available"])


class AnalysisPayload(Base):
    def get(self, c):
        return c.get("/api/analysis?symbol=EURUSD&timeframe=H1&swing_left=2&swing_right=2").get_json()

    def test_analysis_carries_pools_sweeps_and_explanations(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): WICK}))
        a = self.get(c)
        liq = a["liquidity"]
        self.assertTrue(liq["available"])
        self.assertEqual({p["id"] for p in liq["ssl"]}, {pid("SSL", 4)})              # the unswept sell-side pool is active
        self.assertEqual(liq["bsl"], [])                                              # the buy-side pool was swept
        self.assertEqual([p["id"] for p in liq["swept"]], [pid("BSL", 2)])
        sw = liq["sweeps"][0]
        text = " ".join(sw["explanation"]["lines"])
        for needed in ("Pool swept", "Crossing candle", "Detection mode: wick", "closed back inside", "not a reversal signal", "No structural break"):
            self.assertIn(needed, text)
        self.assertEqual(sw["state"], "confirmed" if sw["confirmed"] else "developing")
        self.assertEqual([m["status"] for m in a["modules"] if m["id"] == "liquidity"], ["available"])

    def test_facts_rules_and_watch_stay_separate_and_conditional(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): WICK}))
        i = self.get(c)["interpretation"]
        self.assertEqual(set(i), {"observed_facts", "rule_based", "watch", "setups"})
        self.assertTrue(any("liquidity pool(s) were inferred" in f for f in i["observed_facts"]))
        self.assertTrue(any("sweep" in r and "not mean a reversal" in r for r in i["rule_based"]))
        for line in i["watch"][:-1]:
            self.assertTrue(line.startswith("If ") or line.startswith("The forming candle must"), line)
        self.assertIn("not trade signals", i["watch"][-1])                            # the closing disclaimer is still last
        self.assertFalse(i["setups"]["supported"])
        body = json.dumps(i).lower()
        for banned in ("buy now", "sell now", "entry at", "take profit", "stop loss", "will reverse", "hidden orders are"):
            self.assertNotIn(banned, body)

    def test_a_failed_liquidity_analysis_changes_nothing_else_and_approves_nothing(self):
        ok_client, _ = self.use(LiqFeed({("EURUSD", "H1"): WICK}))
        good = self.get(ok_client)
        bad_client, _ = self.use(LiqFeed({("EURUSD", "H1"): WICK}, tick=None))
        bad = self.get(bad_client)
        self.assertFalse(bad["liquidity"]["available"])
        self.assertEqual(bad["status"]["code"], good["status"]["code"])               # structure status unaffected
        self.assertEqual(bad["structure"]["swings"], good["structure"]["swings"])
        self.assertFalse(bad["status"]["is_trade_signal"])
        # (Phase 7C repair: an engine that exists but could not run is "unavailable" and says why; "not_available" means it does not exist.)
        self.assertEqual([m["status"] for m in bad["modules"] if m["id"] == "liquidity"], ["unavailable"])
        self.assertTrue([m["reason"] for m in bad["modules"] if m["id"] == "liquidity"][0])
        ok_client, _ = self.use(LiqFeed({("EURUSD", "H1"): WICK}))                     # the good feed again (use() patches the shared adapter)
        with mock.patch("trademate.analysis.analyze_liquidity", side_effect=RuntimeError("boom")):
            crashed = self.get(ok_client)
        self.assertEqual(crashed["status"]["code"], good["status"]["code"])            # an engine crash cannot break or change the structure analysis
        self.assertFalse(crashed["liquidity"]["available"])
        self.assertIn("internal error", crashed["liquidity"]["reason"])
        self.assertNotIn("boom", json.dumps(crashed))                                  # no internals leak to the browser
        with mock.patch("trademate.liquidity.analyze_liquidity", side_effect=RuntimeError("boom")):
            chart = self.candles(ok_client)
        self.assertTrue(chart["ok"] and "structure" in chart)
        self.assertFalse(chart["liquidity"]["available"])

    def test_bad_data_is_not_given_liquidity(self):
        rows = [list(r) for r in WICK]
        rows[4][2] = float("nan")
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows}))
        a = self.get(c)
        self.assertEqual(a["status"]["code"], "INVALID DATA")
        self.assertFalse(a["liquidity"]["available"])
        c2, _ = self.use(LiqFeed({}))
        self.assertFalse(self.get(c2)["liquidity"]["available"])                      # no candles: nothing analysed, nothing invented

    def test_two_symbols_and_timeframes_never_share_liquidity(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): WICK, ("XAUUSD", "M15"): mk(RANGE)}))
        a = c.get("/api/analysis?symbol=EURUSD&timeframe=H1&swing_left=2&swing_right=2").get_json()["liquidity"]
        b = c.get("/api/analysis?symbol=XAUUSD&timeframe=M15&swing_left=2&swing_right=2").get_json()["liquidity"]
        self.assertEqual(len(a["sweeps"]), 1)
        self.assertEqual(b["sweeps"], [])
        self.assertNotEqual(a["revision"], b["revision"])


class SettingsRoute(Base):
    def test_settings_api_validates_and_persists_liquidity_options(self):
        c, store = self.use(LiqFeed({}))
        ok = c.post("/api/chart/settings", json={"liquidity_visible": True, "liquidity_tolerance_ticks": 9, "liquidity_sweep_mode": "close"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(c.get("/api/chart/settings").get_json()["settings"]["liquidity_tolerance_ticks"], 9)
        for bad in ({"liquidity_tolerance_ticks": -3}, {"liquidity_sweep_mode": "x"}, {"liquidity_visible": "on"}, {"liquidity_max_pools": 99}):
            r = c.post("/api/chart/settings", json=bad)
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(store.load().liquidity_tolerance_ticks, 9)                    # a bad request changed nothing


if __name__ == "__main__":
    unittest.main()
