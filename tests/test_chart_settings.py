"""Phase 7A: candlestick appearance + structure-visibility settings (store, validation and /api/chart/settings).

    python3 -m unittest tests.test_chart_settings -v
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from trademate.chart_settings import (ADDED_PRESETS, ALL_PRESETS, CUSTOM, LEGACY_PRESETS, PRESETS, THEME_PRESET_COLORS, ChartSettings, ChartSettingsStore, normalize_color, parse_update,
                                      preset_for_colors)

# Written out by hand from the Phase 7A specification, independent of the module's own table.
SPEC_PRESETS = {
    "classic": ("Classic Trading", "#26A69A", "#EF5350"),
    "purple_black": ("Purple and Black", "#800080", "#000000"),
    "emerald_crimson": ("Emerald and Crimson", "#10B981", "#DC2626"),
    "blue_orange": ("Blue and Orange", "#2962FF", "#FF6D00"),
    "white_black": ("White and Black", "#FFFFFF", "#000000"),
    "green_white": ("Green and White", "#00C853", "#FFFFFF"),
    "gold_navy": ("Gold and Navy", "#FFD700", "#001F3F"),
}

# Phase 7B added the liquidity settings; these are their defaults (the original assertions above are otherwise unchanged).
LIQUIDITY_DEFAULTS = {"liquidity_visible": False, "liquidity_tolerance_ticks": 5, "liquidity_sweep_mode": "wick", "liquidity_max_pools": 8}
# Phase 7C added fvg_visible (default hidden); the full-dict expectations below include it, every other assertion is unchanged.
FVG_DEFAULTS = {"fvg_visible": False}
# Phase 7C repair added order_blocks_visible and the chart's own theme ("auto" = follow the application); other assertions are unchanged.
OB_DEFAULTS = {"order_blocks_visible": False, "chart_theme": "auto"}


class ApiCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "chart_settings.json"
        self.store = ChartSettingsStore(self.path)
        patcher = mock.patch.object(app_module, "chart_store", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.test_client()

    def get(self):
        return self.client.get("/api/chart/settings").get_json()

    def post(self, body, **kwargs):
        return self.client.post("/api/chart/settings", json=body, **kwargs)

    def saved(self):
        return self.get()["settings"]


class PresetTableTest(unittest.TestCase):
    def test_three_recommended_presets_have_non_green_theme_variants(self):
        self.assertEqual([p[0] for p in ADDED_PRESETS], ["arctic_rose", "violet_ember", "silver_flame"])
        self.assertEqual(len(LEGACY_PRESETS), 9)
        for preset in ADDED_PRESETS:
            colors = THEME_PRESET_COLORS[preset[0]]
            self.assertEqual(set(colors), {"dark", "light"})
            for pair in colors.values():
                self.assertNotIn("#10B981", pair)
                self.assertNotIn("#00C853", pair)
                self.assertNotIn("#26A69A", pair)

    def test_legacy_presets_remain_supported_for_saved_settings(self):
        by_id = {p[0]: p for p in ALL_PRESETS}
        self.assertEqual(
            [p[0] for p in LEGACY_PRESETS],
            ["red_black", "classic", "purple_black", "emerald_crimson", "blue_orange",
             "white_black", "green_white", "gold_navy", "slate_rose"],
        )
        for pid, (name, bull, bear) in SPEC_PRESETS.items():
            self.assertEqual(by_id[pid][1:], (name, bull, bear), pid)

    def test_recommended_preset_variants_have_readable_contrast_on_both_themes(self):
        def luminance(hex_color):
            values = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        backgrounds = {"dark": "#060B17", "light": "#F3F6FC"}
        for palette in THEME_PRESET_COLORS.values():
            for theme, (bull, bear) in palette.items():
                for color in (bull, bear):
                    ratio = (max(luminance(color), luminance(backgrounds[theme])) + 0.05) / (
                        min(luminance(color), luminance(backgrounds[theme])) + 0.05)
                    self.assertGreaterEqual(ratio, 3, (theme, color, ratio))

    def test_purple_and_black(self):
        self.assertEqual(parse_update({"preset": "purple_black"}, ChartSettings()).to_dict(),
                         {"preset": "purple_black", "bull_color": "#800080", "bear_color": "#000000", "structure_visible": False,
                          "theme": "dark", "colors_chosen": True, **LIQUIDITY_DEFAULTS, **FVG_DEFAULTS, **OB_DEFAULTS})

    def test_preset_ids_and_colour_pairs_are_unique(self):
        self.assertEqual(len({p[0] for p in PRESETS}), len(PRESETS))
        self.assertEqual(len({(p[2], p[3]) for p in PRESETS}), len(PRESETS))

    def test_default_remains_red_black_and_structure_hidden(self):
        d = ChartSettings()
        self.assertEqual((d.preset, d.bull_color, d.bear_color, d.structure_visible), ("red_black", "#E53935", "#000000", False))


class ValidationTest(unittest.TestCase):
    def test_valid_colours_are_normalised(self):
        self.assertEqual(normalize_color("#abc"), "#AABBCC")
        self.assertEqual(normalize_color("#26a69a"), "#26A69A")

    def test_invalid_colours_are_rejected(self):
        for bad in ("red", "26A69A", "#12", "#12345", "#1234567", "#GGGGGG", "#fff;}body{display:none", "javascript:alert(1)",
                    "url(http://x)", "rgb(1,2,3)", "#fff\n", " #fff", "#fff ", "", None, 123, True, ["#fff"], {"a": 1}, "#ff0000; background:red"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                normalize_color(bad)

    def test_unknown_keys_and_wrong_types_are_rejected(self):
        for body in ({"theme": "blue"}, {"theme": None}, {"theme": ["light"]}, {"background": "#000000"}, {"bull_color": "#fff", "css": "x"}, [], "x", None,
                     {"structure_visible": "yes"}, {"structure_visible": 1}, {"preset": "nope"}, {"preset": 5}):
            with self.assertRaises(ValueError, msg=repr(body)):
                parse_update(body, ChartSettings())

    def test_preset_with_conflicting_colours_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_update({"preset": "classic", "bull_color": "#123456"}, ChartSettings())

    def test_preset_with_matching_colours_is_accepted(self):
        new = parse_update({"preset": "purple_black", "bull_color": "#800080", "bear_color": "#000"}, ChartSettings())
        self.assertEqual(new.preset, "purple_black")

    def test_preset_is_derived_from_colours_when_not_given(self):
        base = ChartSettings()
        self.assertEqual(parse_update({"bull_color": "#800080", "bear_color": "#000000"}, base).preset, "purple_black")
        self.assertEqual(parse_update({"bull_color": "#123456"}, base).preset, CUSTOM)        # bear is still the default's, so no preset matches
        self.assertEqual(preset_for_colors("#FFFFFF", "#000000"), "white_black")

    def test_explicit_custom_stays_custom_with_the_same_colours(self):
        new = parse_update({"preset": "custom"}, ChartSettings())
        self.assertEqual((new.preset, new.bull_color, new.bear_color), ("custom", "#E53935", "#000000"))

    def test_adaptive_presets_follow_application_and_pinned_chart_themes(self):
        for preset in ADDED_PRESETS:
            settings = parse_update({"preset": preset[0]}, ChartSettings())
            self.assertEqual((settings.bull_color, settings.bear_color), THEME_PRESET_COLORS[preset[0]]["dark"])
            settings = parse_update({"theme": "light"}, settings)
            self.assertEqual((settings.preset, settings.bull_color, settings.bear_color),
                             (preset[0], *THEME_PRESET_COLORS[preset[0]]["light"]))
            settings = parse_update({"theme": "dark"}, settings)
            self.assertEqual((settings.bull_color, settings.bear_color), THEME_PRESET_COLORS[preset[0]]["dark"])

        settings = parse_update({"preset": "arctic_rose", "theme": "light"}, ChartSettings())
        settings = parse_update({"chart_theme": "dark"}, settings)
        self.assertEqual((settings.bull_color, settings.bear_color), ("#55D6E8", "#FF6B8A"))

    def test_custom_colors_do_not_change_when_the_theme_changes(self):
        settings = parse_update({"bull_color": "#112233", "bear_color": "#445566"}, ChartSettings())
        settings = parse_update({"theme": "light"}, settings)
        self.assertEqual((settings.preset, settings.bull_color, settings.bear_color), ("custom", "#112233", "#445566"))

    def test_partial_update_keeps_everything_else(self):
        base = ChartSettings(preset="custom", bull_color="#112233", bear_color="#445566", structure_visible=True)
        new = parse_update({"bear_color": "#778899"}, base)
        self.assertEqual((new.bull_color, new.bear_color, new.structure_visible), ("#112233", "#778899", True))
        self.assertEqual(parse_update({"structure_visible": False}, base).bull_color, "#112233")


class StoreTest(unittest.TestCase):
    def test_round_trip_survives_a_new_store_instance(self):          # = application restart
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "chart_settings.json"
            ChartSettingsStore(path).update({"bull_color": "#123456", "bear_color": "#abcdef", "structure_visible": True})
            again = ChartSettingsStore(path).load()
            self.assertEqual(again.to_dict(), {"preset": "custom", "bull_color": "#123456", "bear_color": "#ABCDEF", "structure_visible": True,
                                           "theme": "dark", "colors_chosen": True, **LIQUIDITY_DEFAULTS, **FVG_DEFAULTS, **OB_DEFAULTS})

    def test_missing_file_gives_defaults_without_creating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChartSettingsStore(Path(tmp) / "x.json")
            self.assertEqual(store.load(), ChartSettings())
            self.assertFalse(store.path.exists())

    def test_corrupt_or_hostile_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            for text in ("{not json", "[]", '{"bull_color": "red"}', '{"bull_color": "#fff;}x{"}', ""):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(ChartSettingsStore(path).load(), ChartSettings(), text)

    def test_file_edited_into_a_preset_colour_conflict_keeps_the_colours(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            path.write_text(json.dumps({"preset": "classic", "bull_color": "#800080", "bear_color": "#000000"}), encoding="utf-8")
            loaded = ChartSettingsStore(path).load()
            self.assertEqual((loaded.preset, loaded.bull_color), ("purple_black", "#800080"))

    def test_rejected_update_changes_nothing_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChartSettingsStore(Path(tmp) / "x.json")
            store.update({"bull_color": "#112233"})
            before = store.path.read_bytes()
            with self.assertRaises(ValueError):
                store.update({"bull_color": "#445566", "bear_color": "nope"})
            self.assertEqual(store.path.read_bytes(), before)
            self.assertFalse(store.path.with_suffix(".tmp").exists())


class SettingsApiTest(ApiCase):
    def test_get_returns_defaults_and_all_presets(self):
        res = self.get()
        self.assertTrue(res["ok"])
        self.assertEqual(res["settings"]["preset"], "red_black")
        ids = [p["id"] for p in res["presets"]]
        self.assertEqual(ids, [p[0] for p in LEGACY_PRESETS + ADDED_PRESETS])
        added = [p for p in res["presets"] if p["id"] in {"arctic_rose", "violet_ember", "silver_flame"}]
        self.assertEqual(len(added), 3)
        self.assertTrue(all("theme_colors" not in p for p in res["presets"] if p["id"] not in {a["id"] for a in added}))
        self.assertEqual(set(added[0]["theme_colors"]), {"dark", "light"})

    def test_switching_theme_updates_the_adaptive_preset_colors(self):
        self.post({"preset": "arctic_rose"})
        light = self.post({"theme": "light"}).get_json()["settings"]
        self.assertEqual((light["preset"], light["bull_color"], light["bear_color"]), ("arctic_rose", "#007C91", "#C2185B"))
        dark = self.post({"theme": "dark"}).get_json()["settings"]
        self.assertEqual((dark["preset"], dark["bull_color"], dark["bear_color"]), ("arctic_rose", "#55D6E8", "#FF6B8A"))

    def test_chart_theme_override_controls_the_adaptive_palette(self):
        self.post({"preset": "arctic_rose", "theme": "light"})
        pinned_dark = self.post({"chart_theme": "dark"}).get_json()["settings"]
        self.assertEqual((pinned_dark["bull_color"], pinned_dark["bear_color"]), ("#55D6E8", "#FF6B8A"))
        pinned_light = self.post({"chart_theme": "light"}).get_json()["settings"]
        self.assertEqual((pinned_light["bull_color"], pinned_light["bear_color"]), ("#007C91", "#C2185B"))

    def test_selecting_a_preset_updates_both_colours(self):
        for pid, (_n, bull, bear) in SPEC_PRESETS.items():
            res = self.post({"preset": pid}).get_json()
            self.assertEqual((res["settings"]["preset"], res["settings"]["bull_color"], res["settings"]["bear_color"]), (pid, bull, bear))
            self.assertEqual(self.saved()["bull_color"], bull)

    def test_manual_bullish_change_works_and_switches_to_custom(self):
        self.post({"preset": "classic"})
        res = self.post({"bull_color": "#112233"}).get_json()["settings"]
        self.assertEqual((res["bull_color"], res["bear_color"], res["preset"]), ("#112233", "#EF5350", "custom"))

    def test_manual_bearish_change_is_independent(self):
        self.post({"preset": "classic"})
        res = self.post({"bear_color": "#0A0B0C"}).get_json()["settings"]
        self.assertEqual((res["bull_color"], res["bear_color"], res["preset"]), ("#26A69A", "#0A0B0C", "custom"))

    def test_manual_colours_that_equal_a_preset_select_that_preset(self):
        res = self.post({"bull_color": "#800080", "bear_color": "#000000"}).get_json()["settings"]
        self.assertEqual(res["preset"], "purple_black")

    def test_custom_colours_survive_a_page_refresh_and_restart(self):
        self.post({"bull_color": "#123456", "bear_color": "#ABCDEF"})
        self.assertEqual(self.saved()["bull_color"], "#123456")          # "page refresh": a fresh GET
        restarted = ChartSettingsStore(self.path)                           # "application restart": a fresh store
        with mock.patch.object(app_module, "chart_store", restarted):
            res = app.test_client().get("/api/chart/settings").get_json()["settings"]
        self.assertEqual((res["preset"], res["bull_color"], res["bear_color"]), ("custom", "#123456", "#ABCDEF"))

    def test_invalid_hex_is_rejected_with_a_structured_error(self):
        for bad in ("red", "#12", "#GGGGGG", "#fff;}body{", "javascript:alert(1)", None, 5):
            r = self.post({"bull_color": bad})
            self.assertEqual(r.status_code, 400, repr(bad))
            body = r.get_json()
            self.assertFalse(body["ok"])
            self.assertIn("hexadecimal", body["message"])
        self.assertFalse(self.path.exists())

    def test_malformed_update_does_not_corrupt_saved_settings(self):
        self.post({"bull_color": "#123456", "bear_color": "#654321", "structure_visible": True})
        before = self.path.read_bytes()
        for bad in ({"bull_color": "bogus"}, {"bear_color": "#12"}, {"preset": "nope"}, {"theme": "blue"}, {"structure_visible": "x"},
                    {"preset": "classic", "bull_color": "#000001"}):
            self.assertEqual(self.post(bad).status_code, 400, bad)
        self.assertEqual(self.client.post("/api/chart/settings", data="{broken", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/api/chart/settings", data="x=1").status_code, 400)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.saved()["bull_color"], "#123456")

    def test_a_half_valid_update_changes_nothing(self):
        self.post({"bull_color": "#123456"})
        r = self.post({"bull_color": "#AAAAAA", "bear_color": "bad"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.saved()["bull_color"], "#123456")

    def test_unrelated_settings_are_preserved(self):
        self.post({"structure_visible": True})
        self.post({"bull_color": "#102030"})
        self.assertTrue(self.saved()["structure_visible"])
        self.post({"structure_visible": False})
        self.assertEqual(self.saved()["bull_color"], "#102030")

    def test_risk_rules_are_untouched_by_chart_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            risk_file = Path(tmp) / "risk_rules.json"
            risk_file.write_text('{"max_positions": 2}', encoding="utf-8")
            before = risk_file.read_bytes()
            self.post({"bull_color": "#102030"})
            self.assertEqual(risk_file.read_bytes(), before)

    def test_default_theme_is_dark(self):
        self.assertEqual(self.saved()["theme"], "dark")

    def test_theme_can_be_saved_and_survives_refresh_and_restart(self):
        self.assertEqual(self.post({"theme": "light"}).get_json()["settings"]["theme"], "light")
        self.assertEqual(self.saved()["theme"], "light")                                  # refresh
        with mock.patch.object(app_module, "chart_store", ChartSettingsStore(self.path)):   # restart
            self.assertEqual(app.test_client().get("/api/chart/settings").get_json()["settings"]["theme"], "light")

    def test_legacy_preset_and_custom_colors_remain_unchanged_when_theme_switches(self):
        self.post({"preset": "purple_black"})
        for theme in ("light", "dark", "light"):
            saved = self.post({"theme": theme}).get_json()["settings"]
            self.assertEqual((saved["preset"], saved["bull_color"], saved["bear_color"]), ("purple_black", "#800080", "#000000"))
        self.post({"bull_color": "#123456", "bear_color": "#654321"})
        self.post({"theme": "dark"})
        s = self.saved()
        self.assertEqual((s["theme"], s["preset"], s["bull_color"], s["bear_color"]), ("dark", "custom", "#123456", "#654321"))

    def test_white_and_black_candles_are_kept_on_either_theme(self):
        self.post({"preset": "white_black"})
        for theme in ("light", "dark"):
            s = self.post({"theme": theme}).get_json()["settings"]
            self.assertEqual((s["bull_color"], s["bear_color"]), ("#FFFFFF", "#000000"))     # never swapped for the theme

    def test_changing_colours_or_preset_never_changes_the_theme(self):
        self.post({"theme": "light"})
        self.post({"preset": "gold_navy"})
        self.post({"bull_color": "#010203"})
        self.post({"structure_visible": True})
        self.assertEqual(self.saved()["theme"], "light")

    def test_invalid_theme_is_rejected_and_changes_nothing(self):
        self.post({"theme": "light", "bull_color": "#123456"})
        before = self.path.read_bytes()
        for bad in ("blue", "", "LIGHT", None, 1, True, ["dark"]):
            self.assertEqual(self.post({"theme": bad}).status_code, 400, repr(bad))
        self.assertEqual(self.path.read_bytes(), before)

    def test_get_reports_whether_anything_was_ever_saved(self):
        self.assertFalse(self.get()["stored"])
        self.post({"theme": "light"})
        self.assertTrue(self.get()["stored"])

    def test_one_bad_field_in_the_file_does_not_discard_the_rest(self):
        self.path.write_text(json.dumps({"bull_color": "#123456", "bear_color": "#654321", "theme": "neon", "structure_visible": True}), encoding="utf-8")
        s = self.saved()
        self.assertEqual((s["bull_color"], s["bear_color"], s["structure_visible"], s["theme"]), ("#123456", "#654321", True, "dark"))

    def test_cross_origin_post_is_blocked(self):
        r = self.post({"preset": "gold_navy"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.saved()["preset"], "red_black")

    def test_stored_file_never_contains_unnormalised_input(self):
        self.post({"bull_color": "#abc", "bear_color": "#DEF"})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for key in ("bull_color", "bear_color"):
            self.assertRegex(data[key], r"^#[0-9A-F]{6}$")


class FrontendWiringTest(unittest.TestCase):
    """Source-level checks of the page and scripts. Real-browser behaviour is in test_browser_phase7a.py."""

    @classmethod
    def setUpClass(cls):
        root = Path(app.static_folder)
        cls.js = (root / "js" / "app.js").read_text(encoding="utf-8")
        cls.css = (root / "css" / "style.css").read_text(encoding="utf-8")
        with app.test_client() as c:
            cls.html = c.get("/").get_data(as_text=True)

    def test_settings_section_has_every_required_control(self):
        for hook in ('id="candle-settings"', "Candlestick Appearance", 'id="cs-preset"', 'id="cs-swatches"', 'id="cs-bull-picker"',
                     'id="cs-bear-picker"', 'id="cs-bull-hex"', 'id="cs-bear-hex"', 'id="cs-warning"', 'id="cs-structure"',
                     'id="cs-preset-name"', 'type="color"'):
            self.assertIn(hook, self.html)

    def test_chart_has_a_structure_toggle_in_every_terminal(self):
        self.assertEqual(self.html.count('data-role="structure-btn"'), self.html.count("data-terminal>") )

    def test_structure_is_hidden_by_default_in_the_script(self):
        self.assertIn("state.structureVisible = false;", self.js)

    def test_only_user_actions_write_chart_settings(self):
        # The one and only place that POSTs chart settings is flushChartSettings, fed by user-triggered setters.
        self.assertEqual(len(re.findall(r"fetch\('/api/chart/settings', \{ method: 'POST'", self.js)), 1)
        self.assertEqual(len(re.findall(r"/api/chart/settings", self.js)), 2)      # GET load + POST save
        load_candles = self.js[self.js.index("function loadCandles"):self.js.index("setInterval(function () { loadCandles(false)")]
        self.assertNotIn("chart/settings", load_candles)
        self.assertNotIn("candleStyle", load_candles)                                # a refresh never touches the colours

    def test_theme_code_syncs_only_recommended_presets_to_the_chart_theme(self):
        start = self.js.index("function setTheme(")
        body = self.js[start:self.js.index("\n  }\n", start)]
        self.assertIn("syncPresetColorsToChartTheme(!!persist)", body)
        self.assertIn("csPending.theme", body)
        helper = self.js[self.js.index("function syncPresetColorsToChartTheme"):start]
        self.assertIn("theme_colors", helper)
        self.assertIn("csPending.bull_color", helper)
        self.assertIn("csPending.bear_color", helper)
        chart_theme = self.js[self.js.index("function setChartTheme"):self.js.index("function toggleChartTheme")]
        self.assertIn("syncPresetColorsToChartTheme(!!persist)", chart_theme)
        for word in ("candleStyle", "choosePreset"):
            self.assertNotIn(word, body)
        handler = self.js[self.js.index("inRole(root, 'theme-btn').addEventListener"):self.js.index("var structBtn")]
        for word in ("candleStyle", "setColors", "choosePreset"):
            self.assertNotIn(word, handler)
        # Phase 7C repair: the chart toolbar button changes only the CHART theme; the application-wide toggleTheme() belongs to the top bar
        self.assertIn("toggleChartTheme()", handler)
        self.assertNotIn("toggleTheme()", handler)
        self.assertNotIn("setTheme(", handler)

    def test_candle_colours_are_independent_of_trade_colours(self):
        series = self.js[self.js.index("var bw = Math.max(3"):self.js.index("drawStructure(ir, plotL, plotR, plotW, C);")]
        self.assertIn("candleStyle", series)
        self.assertNotIn("C.up", series)
        self.assertNotIn("C.down", series)
        # SL / TP lines keep their own colours
        self.assertIn("var slColor = C.down, tpColor = C.up;", self.js)

    def test_candle_colours_are_set_through_cssom_not_inline_style_attributes(self):
        self.assertNotIn("setAttribute('style'", self.js)
        self.assertNotIn(".innerHTML", self.js[self.js.index("Chart appearance"):self.js.index("Trade history")])

    def test_settings_panel_cannot_overflow_small_screens(self):
        self.assertIn("minmax(0, 1fr)", self.css[self.css.index("Phase 7A"):])
        self.assertIn("@media (max-width: 480px)", self.css[self.css.index("Phase 7A"):])

    def test_theme_switch_markup_and_light_palette_exist(self):
        self.assertIn('id="theme-toggle"', self.html)
        self.assertRegex(self.html, r'id="theme-toggle"[^>]*role="switch"')
        self.assertRegex(self.html, r'id="theme-toggle"[^>]*aria-label="[^"]+"')
        self.assertIn('id="theme-select"', self.html)
        self.assertNotRegex(self.html, r'id="theme-select"[^>]*disabled')
        self.assertIn('html[data-theme="light"]', self.css)
        for var in ("--bg:", "--panel:", "--text:", "--muted:", "--line:", "--field-bg:"):
            block = self.css[self.css.index('html[data-theme="light"] {'):]
            self.assertIn(var, block[:block.index("}")])

    def test_candle_colours_are_not_part_of_the_theme_css(self):
        light = self.css[self.css.index('html[data-theme="light"] {'):]
        for word in ("bull", "bear", "candle"):
            self.assertNotIn(word, light.split("/* ---------- Phase 7A")[0].lower().replace("cs-", ""))

    def test_csp_still_forbids_inline_scripts(self):
        with app.test_client() as c:
            csp = c.get("/").headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)


class LiquiditySettingsTest(unittest.TestCase):
    """Phase 7B: validation and persistence of the liquidity options (same store, same rules as the candle colours)."""

    def test_defaults_keep_the_chart_clean(self):
        d = ChartSettings().to_dict()
        for k, v in LIQUIDITY_DEFAULTS.items():
            self.assertEqual(d[k], v)

    def test_valid_values_are_applied_and_only_those(self):
        new = parse_update({"liquidity_visible": True, "liquidity_tolerance_ticks": 12, "liquidity_sweep_mode": "close",
                            "liquidity_max_pools": 3}, ChartSettings())
        self.assertEqual((new.liquidity_visible, new.liquidity_tolerance_ticks, new.liquidity_sweep_mode, new.liquidity_max_pools),
                         (True, 12, "close", 3))
        self.assertEqual((new.preset, new.theme, new.structure_visible), ("red_black", "dark", False))

    def test_invalid_values_are_refused_and_change_nothing(self):
        base = ChartSettings()
        for bad in ({"liquidity_visible": 1}, {"liquidity_visible": "yes"}, {"liquidity_tolerance_ticks": -1}, {"liquidity_tolerance_ticks": 501},
                    {"liquidity_tolerance_ticks": 1.5}, {"liquidity_tolerance_ticks": True}, {"liquidity_tolerance_ticks": "5"},
                    {"liquidity_sweep_mode": "tick"}, {"liquidity_sweep_mode": None}, {"liquidity_max_pools": 0}, {"liquidity_max_pools": 51},
                    {"liquidity_bogus": 1}):
            with self.assertRaises(ValueError, msg=str(bad)):
                parse_update(bad, base)

    def test_round_trip_and_one_bad_stored_value_does_not_discard_the_rest(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chart_settings.json"
            ChartSettingsStore(path).update({"liquidity_visible": True, "liquidity_tolerance_ticks": 9, "liquidity_sweep_mode": "close"})
            again = ChartSettingsStore(path).load()
            self.assertEqual((again.liquidity_visible, again.liquidity_tolerance_ticks, again.liquidity_sweep_mode), (True, 9, "close"))
            data = json.loads(path.read_text())
            data["liquidity_tolerance_ticks"] = 99999            # hand-edited garbage
            data["liquidity_sweep_mode"] = "banana"
            path.write_text(json.dumps(data))
            loaded = ChartSettingsStore(path).load()
            self.assertEqual((loaded.liquidity_tolerance_ticks, loaded.liquidity_sweep_mode), (5, "wick"))   # defaults for the bad ones only
            self.assertTrue(loaded.liquidity_visible)                                                        # the good one survives

    def test_theme_and_colour_changes_cannot_overwrite_the_liquidity_options(self):
        s = parse_update({"liquidity_visible": True, "liquidity_tolerance_ticks": 7}, ChartSettings())
        s = parse_update({"theme": "light", "preset": "gold_navy"}, s)
        self.assertEqual((s.liquidity_visible, s.liquidity_tolerance_ticks), (True, 7))


if __name__ == "__main__":
    unittest.main()
