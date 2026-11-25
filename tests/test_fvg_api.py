"""Phase 7C task 2: fair value gaps in /api/candles, the saved overlay-visibility setting, and the page wiring.

Stub feed (read-only: it has no order method), temporary settings file. No MT5, no browser, no orders.

    python3 -m unittest tests.test_fvg_api -v
"""

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.test_liquidity_api import LiqFeed
from tests.test_structure_regressions import T0, STEP, mk
from trademate.broker.base import Candle
from trademate.chart_settings import ChartSettings, ChartSettingsStore, parse_update
from trademate.fvg import FVGConfig, analyze_fvg

ROOT = Path(__file__).resolve().parent.parent

# (high, low, close). Hand-checked against the rule (strict, c1 / c3 of three consecutive candles):
#   candles 0,1,2: c1 high 10 < c3 low 11            -> BULLISH gap 10..11
#   candles 3,4,5: c1 low 11 > c3 high 9             -> BEARISH gap 9..11
#   candles 2,3,4: c1 low 11 == c3 high 11 (touching) -> no gap;  every other window overlaps -> no gap
ROWS = [(10, 9, 9.5), (12, 9.8, 12), (12.5, 11, 12), (11.8, 11, 11.2), (11, 8, 8.2), (9, 7.5, 8), (9.5, 8, 9)]


def t(i):
    return T0 + i * STEP


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

    def candles(self, c, query="symbol=EURUSD&timeframe=H1&fvg=1"):
        return c.get("/api/candles?" + query).get_json()


class FvgInChartPayload(Base):
    def test_fvg_is_only_added_when_asked_for(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS)}))
        self.assertNotIn("fvg", self.candles(c, "symbol=EURUSD&timeframe=H1"))
        self.assertNotIn("fvg", self.candles(c, "symbol=EURUSD&timeframe=H1&structure=1&fvg=0"))
        self.assertIn("fvg", self.candles(c))

    def test_it_does_not_depend_on_structure_or_liquidity_being_requested(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS)}))
        alone = self.candles(c)
        both = self.candles(c, "symbol=EURUSD&timeframe=H1&fvg=1&structure=1&liquidity=1")
        self.assertEqual(alone["fvg"], both["fvg"])
        self.assertNotIn("structure", alone)
        self.assertIn("structure", both)

    def test_the_boundaries_and_timestamps_are_the_engines_own(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS)}))
        fvg = self.candles(c)["fvg"]
        self.assertTrue(fvg["available"])
        self.assertEqual(fvg["data_status"], "ok")
        got = [(g["direction"], g["lower"], g["upper"], g["first_time"], g["middle_time"], g["third_time"]) for g in fvg["gaps"]]
        self.assertEqual(got, [("bullish", 10, 11, t(0), t(1), t(2)), ("bearish", 9, 11, t(3), t(4), t(5))])
        # and it is literally the engine's result for the same candles (labels included), not a second implementation
        direct = analyze_fvg(mk(ROWS), FVGConfig(last_candle_closed=False), symbol="EURUSD", timeframe="H1").to_dict()
        self.assertEqual({k: v for k, v in fvg.items() if k != "available"}, direct)

    def test_touching_candles_and_overlaps_are_not_reported(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS)}))
        firsts = [g["first_time"] for g in self.candles(c)["fvg"]["gaps"]]
        self.assertNotIn(t(2), firsts)                                    # candles 2,3,4 touch exactly: not a gap
        self.assertEqual(len(firsts), 2)

    def test_the_newest_candle_is_treated_as_still_forming(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS[:6])}))      # series ends on candle 5 = the third candle of the bearish gap
        gaps = {g["direction"]: g for g in self.candles(c)["fvg"]["gaps"]}
        self.assertTrue(gaps["bullish"]["confirmed"])
        self.assertEqual(gaps["bullish"]["confirmed_time"], t(2))
        self.assertFalse(gaps["bearish"]["confirmed"])                    # depends on the forming candle: never silently confirmed
        self.assertIsNone(gaps["bearish"]["confirmed_time"])
        c2, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS)}))         # one more candle has arrived
        done = {g["direction"]: g for g in self.candles(c2)["fvg"]["gaps"]}
        self.assertTrue(done["bearish"]["confirmed"])                     # the same gap is now confirmed

    def test_symbol_and_timeframe_are_labels_and_each_series_gets_its_own_result(self):
        flat = mk([(10, 9, 9.5)] * 6)
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS), ("XAUUSD", "M15"): flat}))
        a = self.candles(c)["fvg"]
        b = self.candles(c, "symbol=XAUUSD&timeframe=M15&fvg=1")["fvg"]
        self.assertEqual((a["symbol"], a["timeframe"]), ("EURUSD", "H1"))
        self.assertEqual((b["symbol"], b["timeframe"]), ("XAUUSD", "M15"))
        self.assertEqual(len(a["gaps"]), 2)
        self.assertEqual((b["available"], b["gaps"]), (True, []))        # valid data, simply no gap

    def test_repeated_requests_return_identical_results_with_unique_ids(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS)}))
        first, second = self.candles(c)["fvg"], self.candles(c)["fvg"]
        self.assertEqual(first, second)
        ids = [g["id"] for g in first["gaps"]]
        self.assertEqual(len(ids), len(set(ids)))


class FvgUnavailableData(Base):
    def test_too_few_candles_gives_no_gaps_and_a_reason(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS[:2])}))
        res = self.candles(c)
        self.assertTrue(res["ok"])                                        # the chart itself still works
        self.assertFalse(res["fvg"]["available"])
        self.assertEqual(res["fvg"]["gaps"], [])
        self.assertEqual(res["fvg"]["data_status"], "insufficient")
        self.assertIn("at least 3", res["fvg"]["reason"])

    def test_an_invalid_candle_gives_no_gaps_rather_than_a_guess(self):
        rows = mk(ROWS)
        rows[1] = [rows[1][0], rows[1][1], 5.0, 20.0, rows[1][4]]        # high below low
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows}))
        res = self.candles(c)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["candles"]), 7)
        self.assertFalse(res["fvg"]["available"])
        self.assertEqual((res["fvg"]["gaps"], res["fvg"]["data_status"]), ([], "invalid"))

    def test_zero_prices_are_not_charted_as_gaps(self):
        rows = mk(ROWS)
        rows[0] = [rows[0][0], 0.0, 0.0, 0.0, 0.0]
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): rows}))
        fvg = self.candles(c)["fvg"]
        self.assertFalse(fvg["available"])
        self.assertEqual(fvg["gaps"], [])

    def test_an_engine_failure_is_reported_and_never_breaks_the_chart(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS)}))
        with mock.patch("trademate.fvg.analyze_fvg", side_effect=RuntimeError("boom")), self.assertLogs("trademate.web", level="ERROR"):
            res = self.candles(c, "symbol=EURUSD&timeframe=H1&fvg=1&structure=1")
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["candles"]), 7)
        self.assertIn("structure", res)                                   # the other analysis is untouched
        self.assertFalse(res["fvg"]["available"])
        self.assertEqual(res["fvg"]["gaps"], [])
        self.assertIn("internal error", res["fvg"]["reason"])
        self.assertNotIn("boom", str(res["fvg"]))                         # no internals leaked to the page

    def test_a_bad_timeframe_label_is_a_clean_error_not_a_crash(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS)}))
        res = c.get("/api/candles?symbol=EURUSD&timeframe=NOPE&fvg=1").get_json()
        self.assertFalse(res["ok"])
        self.assertEqual(res["candles"], [])


class HidingNeverStopsAnalysis(Base):
    """The saved visibility only decides what is DRAWN. No server analysis consults it."""

    def test_the_fvg_result_is_identical_whether_the_overlay_is_shown_or_hidden(self):
        feed = LiqFeed({("EURUSD", "H1"): mk(ROWS)})
        c, store = self.use(feed, fvg_visible=False)
        hidden = self.candles(c)["fvg"]
        store.update({"fvg_visible": True})
        shown = self.candles(c)["fvg"]
        self.assertEqual(hidden, shown)
        self.assertEqual(len(hidden["gaps"]), 2)

    def test_the_analysis_page_still_has_structure_and_liquidity_with_every_overlay_hidden(self):
        feed = LiqFeed({("EURUSD", "H1"): mk(ROWS * 3)})
        q = "/api/analysis?symbol=EURUSD&timeframe=H1&swing_left=2&swing_right=2"
        c, store = self.use(feed, structure_visible=False, liquidity_visible=False, fvg_visible=False)
        hidden = c.get(q).get_json()
        store.update({"structure_visible": True, "liquidity_visible": True, "fvg_visible": True})
        shown = c.get(q).get_json()
        for payload in (hidden, shown):
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["liquidity"]["available"])
            self.assertIn("swings", payload["structure"])
        self.assertEqual(hidden["structure"], shown["structure"])
        self.assertEqual(hidden["liquidity"], shown["liquidity"])

    def test_structure_is_still_computed_for_the_chart_when_its_overlay_is_hidden(self):
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS * 3)}), structure_visible=False, liquidity_visible=False, fvg_visible=False)
        res = self.candles(c, "symbol=EURUSD&timeframe=H1&structure=1&fvg=1")
        self.assertIn("structure", res)
        self.assertIn("fvg", res)

    def test_the_analysis_page_lists_fvg_as_available_only_when_the_engine_ran(self):
        """Updated in the Phase 7C integration repair (Task 2 put FVGs on the chart only; the Analysis page now shows the real result)."""
        c, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS * 3)}))
        payload = c.get("/api/analysis?symbol=EURUSD&timeframe=H1").get_json()
        self.assertEqual(next(m for m in payload["modules"] if m["id"] == "fvg")["status"], "available")
        self.assertTrue(payload["fvg"]["available"])
        c2, _ = self.use(LiqFeed({("EURUSD", "H1"): mk(ROWS[:2])}))                 # too few candles: the engine refuses, the page says why
        bad = c2.get("/api/analysis?symbol=EURUSD&timeframe=H1").get_json()
        mod = next(m for m in bad["modules"] if m["id"] == "fvg")
        self.assertEqual(mod["status"], "unavailable")
        self.assertTrue(mod["reason"])


class OverlayPreferences(unittest.TestCase):
    def test_default_hidden_so_the_chart_stays_clean(self):
        self.assertFalse(ChartSettings().fvg_visible)

    def test_valid_values_apply_and_nothing_else_changes(self):
        base = ChartSettings(structure_visible=True, liquidity_visible=True)
        new = parse_update({"fvg_visible": True}, base)
        self.assertEqual((new.structure_visible, new.liquidity_visible, new.fvg_visible), (True, True, True))
        self.assertEqual((new.preset, new.theme, new.liquidity_sweep_mode), (base.preset, base.theme, base.liquidity_sweep_mode))

    def test_each_overlay_is_independent_in_every_combination(self):
        for s in (False, True):
            for l in (False, True):
                for f in (False, True):
                    got = parse_update({"structure_visible": s, "liquidity_visible": l, "fvg_visible": f}, ChartSettings())
                    self.assertEqual((got.structure_visible, got.liquidity_visible, got.fvg_visible), (s, l, f))
                    # and changing one leaves the other two alone
                    flipped = parse_update({"fvg_visible": not f}, got)
                    self.assertEqual((flipped.structure_visible, flipped.liquidity_visible), (s, l))

    def test_invalid_values_are_refused_and_change_nothing(self):
        base = ChartSettings(structure_visible=True)
        for bad in (1, 0, "yes", "true", None, [], {}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_update({"fvg_visible": bad}, base)
        with self.assertRaises(ValueError):
            parse_update({"structure_visible": False, "fvg_visible": "x"}, base)      # all or nothing

    def test_persists_across_a_new_store_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "chart_settings.json"
            ChartSettingsStore(path).update({"structure_visible": True, "liquidity_visible": False, "fvg_visible": True})
            again = ChartSettingsStore(path).load()
            self.assertEqual((again.structure_visible, again.liquidity_visible, again.fvg_visible), (True, False, True))

    def test_a_hand_edited_bad_value_falls_back_to_hidden_without_losing_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cs.json"
            path.write_text('{"fvg_visible": "yes", "structure_visible": true, "liquidity_visible": true}', encoding="utf-8")
            loaded = ChartSettingsStore(path).load()
            self.assertEqual((loaded.fvg_visible, loaded.structure_visible, loaded.liquidity_visible), (False, True, True))

    def test_an_old_settings_file_without_the_key_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cs.json"
            path.write_text('{"preset": "classic", "structure_visible": true, "theme": "light"}', encoding="utf-8")
            loaded = ChartSettingsStore(path).load()
            self.assertEqual((loaded.fvg_visible, loaded.structure_visible, loaded.theme), (False, True, "light"))

    def test_the_settings_api_saves_all_three_in_one_atomic_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChartSettingsStore(Path(tmp) / "cs.json")
            with mock.patch.object(app_module, "chart_store", store):
                client = app.test_client()
                ok = client.post("/api/chart/settings", json={"structure_visible": True, "liquidity_visible": True, "fvg_visible": True})
                self.assertEqual(ok.status_code, 200)
                s = client.get("/api/chart/settings").get_json()["settings"]
                self.assertEqual((s["structure_visible"], s["liquidity_visible"], s["fvg_visible"]), (True, True, True))
                bad = client.post("/api/chart/settings", json={"structure_visible": False, "fvg_visible": "on"})
                self.assertEqual(bad.status_code, 400)
                s = client.get("/api/chart/settings").get_json()["settings"]
                self.assertEqual((s["structure_visible"], s["liquidity_visible"], s["fvg_visible"]), (True, True, True))   # nothing changed


class PageWiring(unittest.TestCase):
    """Source-level guarantees that are cheap to check and expensive to regress."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        cls.fvg_js = (ROOT / "static" / "js" / "fvg.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    def test_the_candle_request_always_asks_for_fvg_whether_or_not_it_is_shown(self):
        url = re.search(r"fetch\('/api/candles\?[^;]*?\{ cache", self.js).group(0)
        before_condition = url.split("(state.liquidityVisible")[0]
        self.assertIn("fvg=1", before_condition)                       # not inside any visibility condition
        self.assertNotIn("fvgVisible", url)

    def test_drawing_is_gated_on_visibility_and_on_the_candles_on_screen(self):
        block = self.js[self.js.index("function drawFVG"):self.js.index("this.draw = function")]
        self.assertIn("fv.key !== state.candleKey", block)            # data of another symbol / timeframe is never drawn
        self.assertLess(block.index("!state.fvgVisible"), block.index("ctx.save()"))

    def test_zones_are_not_order_or_quote_levels(self):
        block = self.js[self.js.index("function drawFVG"):self.js.index("this.draw = function")]
        for banned in ("draftLines", "drag", "/api/order", "sym.bid", "C.bid", "C.ask", "slInput", "tpInput", "pending"):
            self.assertNotIn(banned, block)
        self.assertNotIn("fetch(", block)

    def test_fvg_js_only_maps_it_never_detects(self):
        self.assertNotIn("fetch(", self.fvg_js)
        self.assertNotRegex(self.fvg_js, r"\[\s*[1-4]\s*\]")           # never reads a candle's open / high / low / close
        self.assertNotRegex(self.fvg_js, r"\.(high|low|open|close)\b")       # (g.lower / g.upper are the engine's own fields)
        for word in ("Math.min", "Math.max"):
            self.assertNotIn(word, self.fvg_js)

    def test_zones_are_drawn_behind_the_candles(self):
        draw = self.js[self.js.index("this.draw = function"):]
        self.assertLess(draw.index("drawFVG(ir"), draw.index("var bw = Math.max"))
        self.assertLess(draw.index("drawFVG(ir"), draw.index("drawStructure(ir"))

    def test_the_colours_are_distinct_from_the_other_overlays_and_the_quote_lines(self):
        start = self.js.index("var FVG_COLORS")
        fvg = self.js[start:self.js.index(";", start)].lower()
        liq = self.js[self.js.index("var LIQ_COLORS"):self.js.index("var LIQ_COLORS") + 200].lower()
        for color in re.findall(r"#[0-9a-f]{6}", fvg):
            self.assertNotIn(color, liq)
        for quote_color in ("#ff5c5c", "#2fd38a", "#c4262e", "#117a45"):    # bid red / ask green, both themes
            self.assertNotIn(quote_color, fvg)

    def test_the_controls_exist_in_the_toolbar_and_in_settings_and_script_order_is_right(self):
        for needle in ('data-role="structure-btn"', 'data-role="liquidity-btn"', 'data-role="fvg-btn"', 'data-role="overlays-btn"',
                       'id="cs-structure"', 'id="cs-liquidity"', 'id="cs-fvg"', 'id="cs-overlays-show"', 'id="cs-overlays-hide"'):
            self.assertIn(needle, self.html)
        self.assertLess(self.html.index("js/fvg.js"), self.html.index("js/app.js"))

    def test_no_fake_controls_for_features_that_do_not_exist(self):
        low = self.html.lower()
        for fake in ("premium", "discount"):                          # Order Blocks exist now (Phase 7C repair); Premium/Discount still do not
            self.assertNotIn(fake, low)

    def test_no_browser_storage_is_used_for_the_overlay_preferences(self):
        block = self.js[self.js.index("function setFvgVisible"):self.js.index("function setLiquidityOption")]
        self.assertNotIn("localStorage", block)                        # the saved copy is the server file, like the other overlays


if __name__ == "__main__":
    unittest.main()
