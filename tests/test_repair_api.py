"""Phase 7C integration repair: order blocks in /api/candles, FVG + order blocks on the Analysis page, the saved overlay / chart-theme
preferences, and source-level guarantees for the pending-order and theme code.

Stub feeds (read-only: no order method exists on them), temporary settings file. No MT5, no browser, no orders.

    python3 -m unittest tests.test_repair_api -v
"""

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.test_analysis import Feed
from tests.test_liquidity_api import LiqFeed
from tests.test_orderblock_engine import mirror, rows
from trademate.analysis import ENGINE_MODULES, MODULES, module_list
from trademate.broker.base import ConnectionState, ConnectionStatus
from trademate.chart_settings import ChartSettings, ChartSettingsStore, parse_update
from trademate.fvg import FVGConfig, analyze_fvg
from trademate.orderblocks import OrderBlockConfig, analyze_order_blocks
from trademate.structure import analyze_cached

ROOT = Path(__file__).resolve().parent.parent
T0, STEP = 1_760_000_000, 300


def t(i):
    return T0 + i * STEP


# Fixture Z (hand-derived for the project's default swing_left = swing_right = 3; see tests/test_orderblock_engine.py for the method):
#   swing HIGH at candle 6 (12.0, known at 9); swing LOW at candle 13 (8.2, known at 16); candle 19 closes at 12.4 > 12.0 -> bullish BOS.
#   Source candle 13 is bearish (8.7 -> 8.3): bullish order block, zone [8.2, 8.9]; candidate from candle 16, confirmed at candle 19.
Z = [(9.0, 9.2, 8.9, 9.1), (9.1, 9.5, 9.0, 9.4), (9.4, 9.9, 9.3, 9.8), (9.8, 10.4, 9.7, 10.3), (10.3, 11.0, 10.2, 10.9),
     (10.9, 11.6, 10.8, 11.5), (11.5, 12.0, 11.4, 11.9), (11.9, 11.95, 11.2, 11.3), (11.3, 11.4, 10.6, 10.7), (10.7, 10.8, 10.0, 10.1),
     (10.1, 10.2, 9.5, 9.6), (9.6, 9.7, 9.0, 9.1), (9.1, 9.2, 8.6, 8.7), (8.7, 8.9, 8.2, 8.3), (8.3, 9.0, 8.25, 8.9),
     (8.9, 9.6, 8.8, 9.5), (9.5, 10.3, 9.4, 10.2), (10.2, 11.0, 10.1, 10.9), (10.9, 11.7, 10.8, 11.6), (11.6, 12.5, 11.5, 12.4),
     (12.4, 12.8, 12.2, 12.7)]
Z_BEAR = mirror(Z, 20.0)            # bearish BOS at candle 19, bearish block source candle 13, zone [11.1, 11.8]
FLAT = [(10, 10.5, 9.5, 10)] * 30   # valid candles, no swing, no gap


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

    def candles(self, c, query="symbol=EURUSD&timeframe=H1&order_blocks=1&structure=1"):
        return c.get("/api/candles?" + query).get_json()

    def analysis(self, c, query="symbol=EURUSD&timeframe=H1"):
        return c.get("/api/analysis?" + query).get_json()


# ======================================== ISSUE 1: order blocks on the chart ========================================
class OrderBlocksInChartPayload(Base):
    def test_only_added_when_asked_for(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z)}))
        self.assertNotIn("order_blocks", self.candles(c, "symbol=EURUSD&timeframe=H1&structure=1"))
        self.assertNotIn("order_blocks", self.candles(c, "symbol=EURUSD&timeframe=H1&order_blocks=0"))
        self.assertIn("order_blocks", self.candles(c))

    def test_a_bullish_block_with_the_engines_exact_boundaries_and_times(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z)}))
        res = self.candles(c)["order_blocks"]
        self.assertTrue(res["available"])
        self.assertEqual(res["data_status"], "ok")
        self.assertEqual(len(res["blocks"]), 1)
        b = res["blocks"][0]
        self.assertEqual((b["direction"], b["status"], b["lower"], b["upper"]), ("bullish", "confirmed", 8.2, 8.9))
        self.assertEqual((b["source_time"], b["identified_time"], b["confirmed_time"]), (t(13), t(16), t(19)))
        self.assertEqual((b["confirming_event_type"], b["confirming_event_time"], b["reference_level"]), ("BOS", t(19), 12.0))
        self.assertFalse(b["provisional"])
        self.assertEqual((res["symbol"], res["timeframe"]), ("EURUSD", "H1"))

    def test_a_bearish_block_is_the_mirror_image(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z_BEAR)}))
        b = self.candles(c)["order_blocks"]["blocks"][0]
        self.assertEqual((b["direction"], b["status"], b["lower"], b["upper"], b["source_time"]), ("bearish", "confirmed", 11.1, 11.8, t(13)))

    def test_before_the_break_it_is_only_a_candidate(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z[:19])}))
        b = self.candles(c)["order_blocks"]["blocks"][0]
        self.assertEqual((b["status"], b["confirmed_time"], b["confirming_event_id"]), ("candidate", None, None))

    def test_a_close_through_the_zone_marks_it_invalidated_and_the_payload_still_says_so(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z + [(12.7, 12.8, 7.8, 8.0)])}))
        b = self.candles(c)["order_blocks"]["blocks"][0]
        self.assertEqual((b["status"], b["confirmed_time"], b["invalidated_time"]), ("invalidated", t(19), t(21)))

    def test_it_is_literally_the_engines_result_for_the_same_candles_and_structure(self):
        data = rows(Z)
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): data}))
        got = self.candles(c)["order_blocks"]
        with app.test_request_context("/api/candles?symbol=EURUSD&timeframe=H1"):
            cfg = app_module._structure_config(app_module.request.args)
        direct = analyze_order_blocks(data, analyze_cached(data, cfg), OrderBlockConfig(), symbol="EURUSD", timeframe="H1").to_dict()
        self.assertEqual({k: v for k, v in got.items() if k != "available"}, direct)

    def test_it_does_not_depend_on_structure_being_requested(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z)}))
        alone = self.candles(c, "symbol=EURUSD&timeframe=H1&order_blocks=1")
        both = self.candles(c)
        self.assertNotIn("structure", alone)
        self.assertIn("structure", both)
        self.assertEqual(alone["order_blocks"], both["order_blocks"])

    def test_it_uses_the_same_structure_parameters_as_the_chart_request(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z)}))
        wide = self.candles(c, "symbol=EURUSD&timeframe=H1&order_blocks=1&structure=1&swing_left=8&swing_right=8")["order_blocks"]
        self.assertEqual(wide["blocks"], [])                                     # Z has no 8/8 swings (the high has only 6 candles to its left): nothing is invented
        self.assertTrue(wide["available"])
        near = self.candles(c, "symbol=EURUSD&timeframe=H1&order_blocks=1&structure=1&swing_left=1&swing_right=1")["order_blocks"]
        self.assertTrue(near["blocks"])
        self.assertNotEqual([b["id"] for b in near["blocks"]], [])

    def test_hiding_the_overlay_never_changes_the_analysis(self):
        feed = LiqFeed({("EURUSD", "H1"): rows(Z)})
        c, store = self.use(feed, order_blocks_visible=False)
        hidden = self.candles(c)["order_blocks"]
        store.update({"order_blocks_visible": True})
        self.assertEqual(hidden, self.candles(c)["order_blocks"])
        self.assertEqual(len(hidden["blocks"]), 1)

    def test_repeated_requests_are_identical_with_unique_ids(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z)}))
        a, b = self.candles(c)["order_blocks"], self.candles(c)["order_blocks"]
        self.assertEqual(a, b)
        self.assertEqual(len({x["id"] for x in a["blocks"]}), len(a["blocks"]))

    def test_unavailable_data_gives_no_blocks_a_reason_and_a_working_chart(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z[:2])}))
        res = self.candles(c)
        self.assertTrue(res["ok"])
        self.assertFalse(res["order_blocks"]["available"])
        self.assertEqual(res["order_blocks"]["blocks"], [])
        self.assertIn("Need at least", res["order_blocks"]["reason"])
        bad = rows(Z)
        bad[5] = [bad[5][0], bad[5][1], 5.0, 20.0, bad[5][4]]                   # high below low
        c2, _ = self.use(LiqFeed({("EURUSD", "H1"): bad}))
        res = self.candles(c2)
        self.assertTrue(res["ok"] and len(res["candles"]) == 21)
        self.assertFalse(res["order_blocks"]["available"])
        self.assertEqual(res["order_blocks"]["blocks"], [])

    def test_an_engine_failure_is_reported_never_raised_and_leaks_nothing(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z)}))
        with mock.patch("trademate.orderblocks.analyze_order_blocks", side_effect=RuntimeError("boom")), self.assertLogs("trademate.web", level="ERROR"):
            res = self.candles(c, "symbol=EURUSD&timeframe=H1&order_blocks=1&structure=1&fvg=1")
        self.assertTrue(res["ok"] and "structure" in res)
        self.assertTrue(res["fvg"]["available"])                                  # the other engines are untouched
        self.assertFalse(res["order_blocks"]["available"])
        self.assertIn("internal error", res["order_blocks"]["reason"])
        self.assertNotIn("boom", str(res["order_blocks"]))

    def test_a_bad_parameter_is_a_clean_unavailable_not_a_crash(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z)}))
        res = self.candles(c, "symbol=EURUSD&timeframe=H1&order_blocks=1&swing_left=abc")
        self.assertIsNotNone(res)                                                 # the route answers (400-style or unavailable), never a stack trace


# ======================================== ISSUE 2: the Analysis page ========================================
class AnalysisPageIntegration(Base):
    def test_fvg_and_order_blocks_are_the_engines_own_results(self):
        data = rows(Z)
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): data}))
        a = self.analysis(c)
        direct_fvg = analyze_fvg(data, FVGConfig(last_candle_closed=False), symbol="EURUSD", timeframe="H1").to_dict()
        self.assertTrue(a["fvg"]["available"])
        self.assertEqual(a["fvg"]["total"], len(direct_fvg["gaps"]))
        shown = {g["id"]: g for g in a["fvg"]["gaps"]}
        for g in direct_fvg["gaps"][-10:]:                                        # boundaries and direction match the engine exactly
            self.assertEqual((shown[g["id"]]["upper"], shown[g["id"]]["lower"], shown[g["id"]]["direction"]), (g["upper"], g["lower"], g["direction"]))
        ob = a["order_blocks"]
        self.assertTrue(ob["available"])
        self.assertEqual((ob["total"], ob["counts"]), (1, {"candidate": 0, "confirmed": 1, "invalidated": 0, "expired": 0}))
        b = ob["live"][0]
        self.assertEqual((b["direction"], b["status"], b["lower_text"], b["upper_text"], b["state"]), ("bullish", "confirmed", "8.20", "8.90", "confirmed"))
        self.assertIn("Confirmed", b["explanation"]["lines"][0])
        self.assertEqual(b["confirming_event"]["type"], "BOS")

    def test_the_module_list_says_available_only_for_engines_that_ran(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z)}))
        mods = {m["id"]: m for m in self.analysis(c)["modules"]}
        for ran in ("structure", "liquidity", "fvg", "order_blocks"):
            self.assertEqual(mods[ran]["status"], "available", ran)
        for missing in ("multi_timeframe", "setups"):                             # these really do not exist
            self.assertEqual(mods[missing]["status"], "not_available")

    def test_empty_results_are_available_with_nothing_listed(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(FLAT)}))
        a = self.analysis(c)
        self.assertEqual((a["fvg"]["available"], a["fvg"]["total"], a["fvg"]["gaps"]), (True, 0, []))
        self.assertEqual((a["order_blocks"]["available"], a["order_blocks"]["total"], a["order_blocks"]["live"]), (True, 0, []))
        mods = {m["id"]: m["status"] for m in a["modules"]}
        self.assertEqual((mods["fvg"], mods["order_blocks"]), ("available", "available"))       # "found nothing" is not "not available"

    def test_too_few_candles_is_an_error_state_with_the_reason_and_no_results(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z[:2])}))
        a = self.analysis(c)
        for key in ("fvg", "order_blocks"):
            self.assertFalse(a[key]["available"], key)
            self.assertIn("Not analysed", a[key]["reason"])
            self.assertNotIn("gaps" if key == "fvg" else "live", a[key])          # nothing fabricated to fill the gap
        mods = {m["id"]: m for m in a["modules"]}
        for key in ("fvg", "order_blocks"):
            self.assertEqual(mods[key]["status"], "unavailable")
            self.assertTrue(mods[key]["reason"])

    def test_an_invalid_candle_is_an_error_state_for_both_engines(self):
        bad = rows(Z)
        bad[5] = [bad[5][0], bad[5][1], 5.0, 20.0, bad[5][4]]
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): bad}))
        a = self.analysis(c)
        self.assertEqual(a["status"]["code"], "INVALID DATA")
        self.assertFalse(a["fvg"]["available"])
        self.assertIn("impossible or missing", a["fvg"]["reason"])
        self.assertFalse(a["order_blocks"]["available"])

    def test_a_disconnected_feed_is_reported_for_every_engine(self):
        down = ConnectionStatus(ConnectionState.DISCONNECTED, "MT5 is not running.")
        c, _ = self.use(Feed({("EURUSD", "H1"): rows(Z)}, status=down))
        a = self.analysis(c)
        for key in ("fvg", "order_blocks", "liquidity"):
            self.assertFalse(a[key]["available"], key)
            self.assertIn("MT5 is not running", a[key]["reason"], key)
        self.assertTrue(all(m["status"] in ("unavailable", "available", "not_available") for m in a["modules"]))
        self.assertEqual({m["id"]: m["status"] for m in a["modules"]}["fvg"], "unavailable")

    def test_one_engine_crashing_changes_nothing_else_and_leaks_nothing(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z)}))
        good = self.analysis(c)
        with mock.patch("trademate.analysis.analyze_fvg", side_effect=RuntimeError("boom")), self.assertLogs("trademate.analysis", level="ERROR"):
            a = self.analysis(c)
        self.assertEqual(a["status"]["code"], good["status"]["code"])
        self.assertEqual(a["structure"], good["structure"])
        self.assertEqual(a["order_blocks"], good["order_blocks"])
        self.assertFalse(a["fvg"]["available"])
        self.assertIn("internal error", a["fvg"]["reason"])
        self.assertNotIn("boom", str(a))
        with mock.patch("trademate.analysis.analyze_order_blocks", side_effect=RuntimeError("boom")), self.assertLogs("trademate.analysis", level="ERROR"):
            a = self.analysis(c)
        self.assertEqual(a["fvg"], good["fvg"])
        self.assertFalse(a["order_blocks"]["available"])
        self.assertNotIn("boom", str(a))
        self.assertEqual({m["id"]: m["status"] for m in a["modules"]}["order_blocks"], "unavailable")

    def test_hidden_overlays_do_not_change_the_analysis_page(self):
        feed = LiqFeed({("EURUSD", "H1"): rows(Z)})
        c, store = self.use(feed, structure_visible=False, liquidity_visible=False, fvg_visible=False, order_blocks_visible=False)
        hidden = self.analysis(c)
        store.update({"structure_visible": True, "liquidity_visible": True, "fvg_visible": True, "order_blocks_visible": True})
        shown = self.analysis(c)
        for key in ("fvg", "order_blocks", "structure", "liquidity", "modules"):
            self.assertEqual(hidden[key], shown[key], key)

    def test_the_page_is_read_only_and_makes_no_trade_claim(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows(Z)}))
        a = self.analysis(c)
        self.assertFalse(a["status"]["is_trade_signal"])
        self.assertIn("not evidence of institutional activity", a["order_blocks"]["note"])
        self.assertIn("not a prediction", a["fvg"]["note"])
        body = str(a["fvg"]) + str(a["order_blocks"])
        for word in ("buy now", "sell now", "take profit", "stop loss", "guarantee"):
            self.assertNotIn(word, body.lower())

    def test_module_list_decides_status_from_the_view_not_from_the_name(self):
        by = lambda payload: {m["id"]: m for m in module_list(payload)}                  # noqa: E731
        m = by({"fvg": {"available": True}, "order_blocks": {"available": False, "reason": "because"}})
        self.assertEqual((m["fvg"]["status"], m["fvg"]["reason"]), ("available", ""))
        self.assertEqual((m["order_blocks"]["status"], m["order_blocks"]["reason"]), ("unavailable", "because"))
        self.assertEqual((m["liquidity"]["status"], m["liquidity"]["reason"]), ("unavailable", "Not analysed in this response."))   # missing view
        self.assertEqual(by({"fvg": {"available": "yes"}})["fvg"]["status"], "unavailable")                                        # only exactly True counts
        self.assertEqual(by({"fvg": "garbage"})["fvg"]["status"], "unavailable")
        self.assertEqual({x["id"] for x in MODULES if x["status"] != "not_available"} - {"structure"}, set(ENGINE_MODULES))
        self.assertTrue(all(x["status"] == "unavailable" for x in MODULES if x["id"] in ENGINE_MODULES))     # no engine is claimed before it has run


# ======================================== saved preferences ========================================
class OverlayAndThemePreferences(unittest.TestCase):
    def test_defaults_keep_the_chart_clean_and_the_chart_theme_following_the_application(self):
        d = ChartSettings()
        self.assertEqual((d.order_blocks_visible, d.chart_theme, d.theme), (False, "auto", "dark"))

    def test_every_combination_of_the_four_overlays_is_independent(self):
        for s in (False, True):
            for l in (False, True):
                for f in (False, True):
                    for o in (False, True):
                        got = parse_update({"structure_visible": s, "liquidity_visible": l, "fvg_visible": f, "order_blocks_visible": o}, ChartSettings())
                        self.assertEqual((got.structure_visible, got.liquidity_visible, got.fvg_visible, got.order_blocks_visible), (s, l, f, o))
                        flipped = parse_update({"order_blocks_visible": not o}, got)
                        self.assertEqual((flipped.structure_visible, flipped.liquidity_visible, flipped.fvg_visible), (s, l, f))

    def test_invalid_values_are_refused_and_nothing_changes(self):
        base = ChartSettings(structure_visible=True)
        for bad in (1, "yes", None, [], {}):
            with self.assertRaises(ValueError):
                parse_update({"order_blocks_visible": bad}, base)
        for bad in ("blue", "", None, 1, True, "LIGHT"):
            with self.assertRaises(ValueError):
                parse_update({"chart_theme": bad}, base)
        with self.assertRaises(ValueError):
            parse_update({"structure_visible": False, "chart_theme": "pink"}, base)          # all or nothing

    def test_the_chart_theme_never_changes_the_application_theme_or_the_candle_colours(self):
        base = ChartSettings(theme="dark", preset="classic")
        for mode in ("light", "dark", "auto"):
            got = parse_update({"chart_theme": mode}, base)
            self.assertEqual((got.chart_theme, got.theme, got.preset, got.bull_color, got.bear_color), (mode, "dark", base.preset, base.bull_color, base.bear_color))
        got = parse_update({"theme": "light"}, parse_update({"chart_theme": "dark"}, base))
        self.assertEqual((got.theme, got.chart_theme), ("light", "dark"))                   # and the reverse: the app theme leaves the chart theme alone

    def test_persists_across_a_new_store_and_old_files_still_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cs.json"
            ChartSettingsStore(path).update({"order_blocks_visible": True, "chart_theme": "light", "theme": "dark"})
            again = ChartSettingsStore(path).load()
            self.assertEqual((again.order_blocks_visible, again.chart_theme, again.theme), (True, "light", "dark"))
            path.write_text('{"preset": "classic", "theme": "light", "fvg_visible": true}', encoding="utf-8")   # a file from before this repair
            old = ChartSettingsStore(path).load()
            self.assertEqual((old.order_blocks_visible, old.chart_theme, old.theme, old.fvg_visible), (False, "auto", "light", True))
            path.write_text('{"chart_theme": "neon", "order_blocks_visible": "yes", "theme": "light"}', encoding="utf-8")
            with self.assertLogs("trademate.chart", level="WARNING") as logged:
                bad = ChartSettingsStore(path).load()
            self.assertEqual((bad.order_blocks_visible, bad.chart_theme, bad.theme), (False, "auto", "light"))     # hand-edited junk falls back, others kept
            self.assertTrue(any("Ignoring invalid" in line for line in logged.output))                             # and says so in the log

    def test_the_settings_api_saves_everything_in_one_atomic_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChartSettingsStore(Path(tmp) / "cs.json")
            with mock.patch.object(app_module, "chart_store", store):
                client = app.test_client()
                ok = client.post("/api/chart/settings", json={"order_blocks_visible": True, "chart_theme": "light", "fvg_visible": True})
                self.assertEqual(ok.status_code, 200)
                s = client.get("/api/chart/settings").get_json()["settings"]
                self.assertEqual((s["order_blocks_visible"], s["chart_theme"], s["fvg_visible"], s["theme"]), (True, "light", True, "dark"))
                bad = client.post("/api/chart/settings", json={"order_blocks_visible": False, "chart_theme": "pink"})
                self.assertEqual(bad.status_code, 400)
                s = client.get("/api/chart/settings").get_json()["settings"]
                self.assertEqual((s["order_blocks_visible"], s["chart_theme"]), (True, "light"))   # nothing changed


# ======================================== source-level guarantees (ISSUES 1, 3, 4) ========================================
def code_only(text):
    """JavaScript with comments removed, so a banned word in an explanatory comment is not mistaken for code."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)(^|\s)//.*$", "", text)


class PageWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        cls.ob_js = (ROOT / "static" / "js" / "ob.js").read_text(encoding="utf-8")
        cls.analysis_js = (ROOT / "static" / "js" / "analysis.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    def block(self, start, end):
        return self.js[self.js.index(start):self.js.index(end)]

    # ---- order blocks ----
    def test_the_candle_request_always_asks_for_order_blocks_whether_or_not_they_are_shown(self):
        url = re.search(r"fetch\('/api/candles\?[^;]*?\{ cache", self.js).group(0)
        before_condition = url.split("(state.liquidityVisible")[0]
        self.assertIn("order_blocks=1", before_condition)
        self.assertIn("fvg=1", before_condition)
        self.assertNotIn("obVisible", url)
        self.assertTrue(before_condition.rstrip(" +").endswith("structure=1'"))                    # (pinned by test_structure_api as well)

    def test_order_block_drawing_is_gated_on_visibility_key_and_data_and_is_not_an_order_level(self):
        block = code_only(self.block("function drawOB", "function drawFVG"))
        self.assertLess(block.index("!state.obVisible"), block.index("ctx.save()"))
        self.assertIn("ob.key !== state.candleKey", block)
        for banned in ("draftLines", "drag", "/api/order", "sym.bid", "C.bid", "C.ask", "slInput", "tpInput", "pending", "fetch(", "addEventListener"):
            self.assertNotIn(banned, block)

    def test_order_blocks_are_drawn_behind_the_candles_and_the_gaps(self):
        draw = self.js[self.js.index("this.draw = function"):]
        self.assertLess(draw.index("drawOB(ir"), draw.index("drawFVG(ir"))
        self.assertLess(draw.index("drawFVG(ir"), draw.index("var bw = Math.max"))

    def test_ob_js_only_maps_it_never_detects_or_invents(self):
        self.assertNotIn("fetch(", self.ob_js)
        self.assertNotRegex(self.ob_js, r"\[\s*[1-4]\s*\]")                                    # never reads a candle's open / high / low / close
        self.assertNotRegex(self.ob_js, r"\.(high|low|open|close)\b")
        for word in ("Math.min", "Math.max", "Math.random"):
            self.assertNotIn(word, self.ob_js)

    def test_no_hardcoded_market_data_or_demo_zones_anywhere_in_the_shipped_scripts(self):
        for name, text in (("ob.js", self.ob_js), ("analysis.js", self.analysis_js), ("app.js", self.js)):
            self.assertNotRegex(text, r"(?i)demo\s*(zone|block|gap|order)|sample\s*(zone|block|gap)|fake\s*(zone|block|gap|order)|mock\s*(zone|block)", name)

    def test_every_overlay_colour_is_distinct_from_every_other_overlay_and_the_quote_lines(self):
        def hexes(name):
            start = self.js.index(f"var {name}")
            return re.findall(r"#[0-9a-fA-F]{6}", self.js[start:self.js.index(";", start)].lower())
        groups = {n: hexes(n) for n in ("OB_COLORS", "FVG_COLORS", "LIQ_COLORS", "STRUCT_COLORS", "PEND_COLORS")}
        new_overlays = ("OB_COLORS", "FVG_COLORS", "PEND_COLORS")             # added by Phase 7C; the older ones keep their pre-existing palette
        quotes = [c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}", self.js[self.js.index("var CHART_COLORS"):self.js.index("var LIQ_COLORS")])]
        seen = {}
        for name, colours in groups.items():
            self.assertTrue(colours, name)
            for colour in colours:
                self.assertNotIn(colour, seen, f"{colour} is used by both {name} and {seen.get(colour)}")
                seen[colour] = name
                if name in new_overlays:
                    self.assertNotIn(colour, quotes, f"{name} reuses a quote / trade colour {colour}")

    def test_the_controls_exist_and_the_script_order_is_right(self):
        for needle in ('data-role="ob-btn"', 'data-role="overlays-btn"', 'id="cs-ob"', 'id="cs-chart-theme"', 'id="an-fvg"', 'id="an-ob"'):
            self.assertIn(needle, self.html)
        self.assertLess(self.html.index("js/ob.js"), self.html.index("js/app.js"))

    def test_no_control_for_features_that_still_do_not_exist(self):
        low = self.html.lower()
        for fake in ("premium", "discount"):
            self.assertNotIn(fake, low)
        # The candle countdown WAS a not-yet-built feature when this test was written; it is real now (static/js/clocks.js,
        # tests/test_clock_js.py, tests/test_browser_clocks.py). What must still never exist is a placeholder for it.
        for placeholder in ("countdown coming", "countdown (soon)", "countdown not implemented", "countdown is not available yet"):
            self.assertNotIn(placeholder, low)
        self.assertEqual(low.count('data-role="candle-countdown"'), low.count("data-terminal>"))

    # ---- pending orders ----
    def test_pending_orders_are_read_from_the_brokers_list_and_never_sent_anywhere(self):
        start = self.js.index("function symOrders")
        self.assertIn("state.orders", self.js[start:start + 500])                              # the broker's own list from /api/state
        block = code_only(self.block("(function drawPendingOrders()", "// Lay the cards out"))
        for banned in ("fetch(", "/api/", "runWrite", "addEventListener", "draftLines", "setSL", "setTP", "XMLHttp", "localStorage"):
            self.assertNotIn(banned, block)
        self.assertIn("symOrders()", block)
        self.assertIn("PEND_LABEL", block)

    def test_pending_orders_are_not_draggable_lines(self):
        start = self.js.index("function lines()")
        lines_fn = self.js[start:self.js.index("\n    }\n", start)]
        self.assertGreater(len(lines_fn), 300)
        self.assertNotIn("symOrders", lines_fn)                                                # lines() feeds hit-testing and dragging: pending orders are not in it

    def test_pending_order_line_style_is_unique(self):
        self.assertEqual(self.js.count("[14, 4, 2, 4]"), 1)
        pend = self.block("(function drawPendingOrders()", "// Lay the cards out")
        self.assertIn("[14, 4, 2, 4]", pend)
        self.assertNotIn("C.entry", pend)                                                      # not the open-position entry style
        self.assertNotIn("C.down", pend)
        self.assertNotIn("C.up", pend)

    def test_pending_orders_are_part_of_the_visible_price_range(self):
        self.assertIn("symOrders().forEach(function (o) { include(o.price); });", self.js)

    # ---- chart theme ----
    def test_the_chart_button_changes_only_the_chart_theme(self):
        handler = self.block("inRole(root, 'theme-btn').addEventListener", "var structBtn")
        self.assertIn("toggleChartTheme()", handler)
        for forbidden in ("toggleTheme()", "setTheme(", "data-theme", "state.theme"):
            self.assertNotIn(forbidden, handler)

    def test_chart_theme_code_never_writes_the_application_theme(self):
        body = self.block("function setChartTheme(", "function applyChartSettings(")
        for forbidden in ("state.theme =", "setAttribute('data-theme'", "saveLocal('trademate.chartTheme'", "csPending.theme", "toggleTheme", "setTheme("):
            self.assertNotIn(forbidden, body)
        self.assertIn("csPending.chart_theme", body)
        app_body = self.block("function setTheme(", "function setChartTheme(")
        self.assertNotIn("chartThemeSetting =", app_body)                                       # and the application theme never writes the chart's own setting

    def test_the_chart_reads_the_effective_chart_theme_not_the_application_theme(self):
        start = self.js.index("function Terminal")
        end = self.js.index("TradeMateApp", start) if "TradeMateApp" in self.js[start:] else len(self.js)
        terminal = self.js[start:self.js.index("/* ---- application theme", start)] if "/* ---- application theme" in self.js[start:] else self.js[start:end]
        self.assertNotIn("state.theme", terminal)                                              # every chart read goes through chartTheme()
        self.assertIn("chartTheme()", terminal)

    def test_the_global_theme_controls_are_preserved(self):
        self.assertIn("$('theme-toggle').addEventListener('click', toggleTheme)", self.js)
        self.assertIn("$('theme-select').addEventListener('change'", self.js)
        self.assertIn('id="theme-toggle"', self.html)
        self.assertIn('id="theme-select"', self.html)
        self.assertIn('data-role="theme-btn"', self.html)

    def test_the_chart_theme_has_css_for_both_pairings(self):
        css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
        self.assertIn('[data-chart-theme="light"]', css)                                       # app dark + chart light (the original design)
        self.assertIn('html[data-theme="light"] [data-chart-theme="dark"]', css)               # app light + chart dark (added in this repair)

    # ---- the Analysis page ----
    def test_the_analysis_page_never_hardcodes_fvg_or_order_block_availability(self):
        self.assertIn("m.status === 'unavailable'", self.analysis_js)
        self.assertNotRegex(self.analysis_js, r"(?i)fvg[^\n]{0,80}not available yet|order block[^\n]{0,80}not available yet")
        self.assertIn("renderFvg(a); renderOrderBlocks(a);", self.analysis_js)
        self.assertIn("'Fair value gaps not analysed: '", self.analysis_js)
        self.assertIn("'Order blocks not analysed: '", self.analysis_js)

    def test_server_text_is_placed_with_textcontent_only(self):
        self.assertNotRegex(self.analysis_js, r"innerHTML|insertAdjacentHTML|document\.write")


if __name__ == "__main__":
    unittest.main()
