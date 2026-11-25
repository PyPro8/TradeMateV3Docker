"""Task 2 regression tests (no browser, no broker): default candle colours, the plain-language Analysis summary, derived FVG fill
status, and the engine -> payload data flow.

    python3 -m unittest tests.test_task2_analysis_and_defaults -v
"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.test_structure_engine import CFG, DOWN, UP, UP_THEN_BREAK_LOW, series
from trademate import analysis, analysis_summary as summ
from trademate.broker.base import Candle, ConnectionState, ConnectionStatus
from trademate.chart_settings import DEFAULT_PRESET, PRESET_BY_ID, ChartSettingsStore

FORMING = replace(CFG, last_candle_closed=False)
CLOSED = replace(CFG, last_candle_closed=True)


# ------------------------------------------------------------------------------------------------ Objective B
class DefaultCandleColoursTest(unittest.TestCase):
    def store(self, content=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "cs.json"
        if content is not None:
            path.write_text(json.dumps(content), encoding="utf-8")
        return ChartSettingsStore(path), path

    def test_the_default_remains_red_and_black(self):
        s, _ = self.store()
        cur = s.load()
        self.assertEqual(DEFAULT_PRESET, "red_black")
        self.assertEqual((cur.bull_color, cur.bear_color), ("#E53935", "#000000"))
        r, g, b = (int(cur.bull_color[i:i + 2], 16) for i in (1, 3, 5))
        self.assertTrue(r > 180 and g < 90 and b < 90, "the bullish default must remain clearly red")
        self.assertFalse(cur.colors_chosen)

    def test_an_old_file_that_still_holds_the_previous_default_gets_the_new_default(self):
        old = {"preset": "classic", "bull_color": "#26A69A", "bear_color": "#EF5350", "structure_visible": True, "fvg_visible": True}
        s, _ = self.store(old)
        cur = s.load()
        self.assertEqual((cur.bull_color, cur.bear_color), ("#E53935", "#000000"))
        self.assertTrue(cur.structure_visible and cur.fvg_visible, "other saved preferences are untouched")

    def test_colours_the_user_chose_are_kept_even_in_an_old_file(self):
        s, _ = self.store({"preset": "custom", "bull_color": "#112233", "bear_color": "#AABBCC"})
        cur = s.load()
        self.assertEqual((cur.bull_color, cur.bear_color), ("#112233", "#AABBCC"))
        self.assertTrue(cur.colors_chosen)

    def test_choosing_classic_now_is_remembered_and_survives_a_reload(self):
        s, path = self.store()
        s.update({"preset": "classic"})
        again = ChartSettingsStore(path).load()
        self.assertEqual(again.preset, "classic")
        self.assertEqual((again.bull_color, again.bear_color), (PRESET_BY_ID["classic"][2], PRESET_BY_ID["classic"][3]))
        self.assertTrue(again.colors_chosen)

    def test_saving_an_unrelated_preference_does_not_turn_the_default_into_a_choice(self):
        s, path = self.store()
        s.update({"structure_visible": False})
        again = ChartSettingsStore(path).load()
        self.assertFalse(again.colors_chosen)
        self.assertEqual(again.preset, "red_black")

    def test_the_colour_pair_is_in_the_settings_api_and_the_page_defaults_match(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(app_module, "chart_store", ChartSettingsStore(Path(tmp) / "cs.json")):
            body = app.test_client().get("/api/chart/settings").get_json()
        self.assertEqual(body["settings"]["bull_color"], "#E53935")
        self.assertEqual(body["settings"]["bear_color"], "#000000")
        self.assertIn("red_black", [p["id"] for p in body["presets"]])
        self.assertIn("arctic_rose", [p["id"] for p in body["presets"]])
        html = (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text(encoding="utf-8").lower()
        self.assertIn('value="#e53935"', html)
        self.assertNotIn('value="#26a69a"', html)
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("preset: 'red_black', bull: '#E53935', bear: '#000000'", js)


# ------------------------------------------------------------------------------------------------ Objective A: direction
class DirectionTest(unittest.TestCase):
    def summary(self, closes, cfg):
        return analysis.build_analysis("EURUSD", "H1", series(closes), cfg, 2)["summary"]

    def test_agreeing_structure_reads_bullish_or_bearish(self):
        bullish = self.summary(UP, FORMING)["direction"]
        bearish = self.summary(DOWN, FORMING)["direction"]
        self.assertEqual(bullish["label"], "bullish")
        self.assertEqual(bullish["confirmed_direction"], "bullish")
        self.assertEqual(bullish["quality"], "aligned")
        self.assertEqual(bearish["label"], "bearish")
        self.assertEqual(bearish["confirmed_direction"], "bearish")
        self.assertEqual(bearish["quality"], "aligned")

    def test_a_forming_break_against_the_trend_is_provisional_and_does_not_change_confirmed_direction(self):
        # Part 7 of the structure audit: direction, quality and provisional evidence are separate facts. The forming-candle break is
        # reported as provisional; the confirmed direction stays bullish and the OVERALL verdict is still "mixed".
        s = self.summary(UP_THEN_BREAK_LOW, FORMING)
        d = s["direction"]
        self.assertEqual(d["label"], "bullish")
        self.assertEqual(d["confirmed_direction"], "bullish")
        self.assertEqual(d["provisional"]["direction"], "bearish")
        self.assertTrue(any("unconfirmed" in line for line in d["context"]))
        self.assertEqual(s["overall"]["verdict"], "mixed")

    def test_a_confirmed_choch_is_never_presented_as_a_confirmed_reversal(self):
        s = self.summary(UP_THEN_BREAK_LOW, CLOSED)
        d = s["direction"]
        # The confirmed direction is the engine's (bearish); the CHoCH is flagged as early evidence, not as a settled reversal.
        self.assertEqual(d["label"], "bearish")
        self.assertEqual(d["confirmed_direction"], "bearish")
        self.assertEqual(d["quality"], "early")
        self.assertIsNotNone(d["caveat"])
        event = d["events"][0]
        self.assertEqual(event["type"], "CHOCH")
        self.assertIn("not a confirmed reversal", event["meaning"])
        everything = json.dumps(s).lower()
        self.assertNotIn("confirmed reversal.", everything.replace("not a confirmed reversal", ""))
        self.assertNotIn("reversal confirmed", everything)

    def test_every_event_has_a_plain_meaning_and_real_prices(self):
        a = analysis.build_analysis("EURUSD", "H1", series(UP_THEN_BREAK_LOW), CLOSED, 2)
        engine_levels = {e["level_text"] for e in a["structure"]["recent_events"]}
        for e in a["summary"]["direction"]["events"]:
            self.assertIn(e["level_text"], engine_levels)
            self.assertTrue(e["meaning"])

    def test_too_little_data_is_unclear_and_insufficient_never_a_direction(self):
        a = analysis.build_analysis("EURUSD", "H1", series(UP)[:3], FORMING)
        s = a["summary"]
        self.assertEqual(s["direction"]["label"], "unclear")
        self.assertEqual(s["overall"]["verdict"], "insufficient")
        self.assertTrue(s["overall"]["insufficient"])
        self.assertIn("not enough evidence", s["overall"]["text"].lower())

    def test_no_candles_gives_no_summary_at_all(self):
        a = analysis.build_analysis("EURUSD", "H1", [], FORMING)
        self.assertNotIn("summary", a)


# ------------------------------------------------------------------------------------------------ liquidity evidence
def sweep(side, close_beyond, after=None, same=()):
    return {"side": side, "close_beyond": close_beyond, "level_text": "1.0", "candle_time": 100, "candle": {"label": "t"},
            "structure": {"after": after, "same_candle": list(same), "before": None}}


def ev(kind, direction):
    return {"type": kind, "direction": direction, "confirmed": True}


class SweepEvidenceTest(unittest.TestCase):
    def test_choch_against_the_sweep_is_reversal_evidence_but_not_proof(self):
        r = summ.sweep_evidence(sweep("BSL", False, ev("CHOCH", "bearish")))
        self.assertEqual((r["verdict"], r["direction"]), ("reversal", "bearish"))
        self.assertIn("not proof", r["text"])

    def test_bos_after_a_sweep_is_continuation_evidence(self):
        r = summ.sweep_evidence(sweep("SSL", True, ev("BOS", "bearish")))
        self.assertEqual((r["verdict"], r["direction"]), ("continuation", "bearish"))

    def test_choch_in_the_direction_of_the_sweep_is_not_called_reversal(self):
        r = summ.sweep_evidence(sweep("BSL", True, ev("CHOCH", "bullish")))
        self.assertEqual(r["verdict"], "continuation")

    def test_no_follow_through_means_no_evidence_even_when_price_closed_back_inside(self):
        r = summ.sweep_evidence(sweep("BSL", False))
        self.assertEqual((r["verdict"], r["direction"]), ("none", None))
        self.assertIn("not confirmed", r["text"])

    def test_a_same_candle_break_is_unclear_because_the_order_is_unknowable(self):
        r = summ.sweep_evidence(sweep("BSL", True, None, [ev("BOS", "bullish")]))
        self.assertEqual(r["verdict"], "unclear")


# ------------------------------------------------------------------------------------------------ Objective E: FVG status
class Bar:
    def __init__(self, t, o, h, l, c):
        self.time, self.open, self.high, self.low, self.close = t, o, h, l, c


BULL_GAP = {"direction": "bullish", "upper": 10.0, "lower": 8.0, "third_index": 2}
BEAR_GAP = {"direction": "bearish", "upper": 10.0, "lower": 8.0, "third_index": 2}


def bars_after(*lows_highs):
    out = [Bar(i, 5, 6, 4, 5) for i in range(3)]
    for i, (low, high) in enumerate(lows_highs, start=3):
        out.append(Bar(i, 12, high, low, 12))
    return out


class FvgFillStatusTest(unittest.TestCase):
    def test_untouched_when_later_candles_stay_clear_of_the_gap(self):
        f = analysis.fvg_fill_status(BULL_GAP, bars_after((11, 13), (10.5, 13)))
        self.assertEqual((f["status"], f["percent"]), ("untouched", 0))

    def test_a_bullish_gap_is_partly_filled_when_price_enters_but_does_not_cross_it(self):
        f = analysis.fvg_fill_status(BULL_GAP, bars_after((9, 13), (10.5, 13)))
        self.assertEqual((f["status"], f["percent"]), ("partial", 50))

    def test_a_bullish_gap_is_filled_when_a_wick_trades_through_the_whole_gap(self):
        f = analysis.fvg_fill_status(BULL_GAP, bars_after((7.9, 13), (11, 13)))
        self.assertEqual((f["status"], f["percent"]), ("filled", 100))

    def test_bearish_gaps_fill_from_below(self):
        self.assertEqual(analysis.fvg_fill_status(BEAR_GAP, bars_after((5, 7.9)))["status"], "untouched")
        self.assertEqual(analysis.fvg_fill_status(BEAR_GAP, bars_after((5, 9)))["percent"], 50)
        self.assertEqual(analysis.fvg_fill_status(BEAR_GAP, bars_after((5, 10.1)))["status"], "filled")

    def test_only_candles_after_candle_three_count(self):
        bars = bars_after((11, 13))
        bars[1] = Bar(1, 5, 6, 1, 5)                      # a candle BEFORE the gap completed dipped into it; it must not count
        self.assertEqual(analysis.fvg_fill_status(BULL_GAP, bars)["status"], "untouched")

    def test_a_fill_made_only_by_the_newest_candle_is_provisional(self):
        f = analysis.fvg_fill_status(BULL_GAP, bars_after((11, 13), (9, 13)))
        self.assertTrue(f["provisional"])
        f2 = analysis.fvg_fill_status(BULL_GAP, bars_after((9, 13), (11, 13)))
        self.assertFalse(f2["provisional"])


class RelevantZonesTest(unittest.TestCase):
    def test_nearest_zones_win_and_price_inside_counts_as_zero_away(self):
        zones = [{"id": "far", "lower": 0.0, "upper": 1.0}, {"id": "near", "lower": 6.0, "upper": 6.5}, {"id": "in", "lower": 4.0, "upper": 6.0}]
        self.assertEqual([z["id"] for z in summ.nearest(zones, 5.0, 2)], ["in", "near"])

    def test_location_words(self):
        self.assertEqual(summ.location_of(1, 2, 1.5), "inside")
        self.assertEqual(summ.location_of(1, 2, 3), "above")
        self.assertEqual(summ.location_of(1, 2, 0), "below")
        self.assertIsNone(summ.location_of(1, 2, None))


# ------------------------------------------------------------------------------------------------ data flow engine -> payload
class DataFlowTest(unittest.TestCase):
    def walk(self, seed=3, n=300):
        import random
        rnd = random.Random(seed)
        rows, p = [], 1.1
        for i in range(n):
            o = p
            c = o + rnd.gauss(0, 0.0008) + (0.0002 if (i // 60) % 2 == 0 else -0.0002)
            rows.append([1_760_000_000 + i * 3600, o, max(o, c) + abs(rnd.gauss(0, 0.0004)), min(o, c) - abs(rnd.gauss(0, 0.0004)), c, 100])
            p = c
        return rows

    def test_every_zone_in_the_summary_is_an_engine_zone_at_the_engine_price(self):
        a = analysis.build_analysis("EURUSD", "H1", self.walk(), CFG, 5)
        fvg_all = {g["id"]: g for g in a["fvg"]["gaps"]}
        shown = [z for side in ("bullish", "bearish") for z in a["summary"]["fvg"][side]]
        self.assertTrue(shown, "the fixture must produce at least one relevant FVG")
        engine_ids = {g["id"] for side in a["fvg"]["relevant"].values() for g in side}
        for z in shown:
            self.assertIn(z["id"], engine_ids)
        for side in a["fvg"]["relevant"].values():
            for g in side:
                self.assertTrue(g["lower"] < g["upper"])
        del fvg_all
        live = {b["id"]: b for b in a["order_blocks"]["live"]}
        for side in ("bullish", "bearish"):
            for z in a["summary"]["order_blocks"][side]:
                b = live[z["id"]]
                self.assertEqual((z["lower"], z["upper"]), (b["lower"], b["upper"]))
                self.assertEqual(z["status_code"], b["status"], "order block status is the engine's own")

    def test_shifting_every_price_shifts_every_zone_nothing_is_hard_coded(self):
        rows = self.walk()
        shifted = [[r[0], r[1] + 5, r[2] + 5, r[3] + 5, r[4] + 5, r[5]] for r in rows]
        a, b = analysis.build_analysis("EURUSD", "H1", rows, CFG, 5), analysis.build_analysis("EURUSD", "H1", shifted, CFG, 5)
        for kind in ("fvg", "order_blocks"):
            for side in ("bullish", "bearish"):
                za, zb = a["summary"][kind][side], b["summary"][kind][side]
                self.assertEqual(len(za), len(zb))
                for x, y in zip(za, zb):
                    self.assertAlmostEqual(y["lower"] - x["lower"], 5, places=6)
                    self.assertAlmostEqual(y["upper"] - x["upper"], 5, places=6)

    def test_fill_status_matches_the_actual_later_candles(self):
        rows = self.walk()
        a = analysis.build_analysis("EURUSD", "H1", rows, CFG, 5)
        closes = {r[0]: r for r in rows}
        for side in a["fvg"]["relevant"].values():
            for g in side:
                later = [r for r in rows if r[0] > g["third"]["time"]]
                if g["direction"] == "bullish":
                    reached = any(r[3] <= g["upper"] for r in later)
                else:
                    reached = any(r[2] >= g["lower"] for r in later)
                self.assertEqual(g["fill"]["status"] != "untouched", reached)
        del closes

    def test_fill_counts_add_up_to_the_total(self):
        a = analysis.build_analysis("EURUSD", "H1", self.walk(), CFG, 5)
        self.assertEqual(sum(a["fvg"]["fill_counts"].values()), a["fvg"]["total"])

    def test_an_unavailable_engine_gives_an_unavailable_section_never_zones(self):
        a = analysis.build_analysis("EURUSD", "H1", self.walk(), CFG, 5)
        a["fvg"] = analysis.fvg_unavailable("test: engine down")
        a["order_blocks"] = analysis.order_blocks_unavailable("test: engine down")
        s = summ.build_summary(a, 1.1, [])
        for key in ("fvg", "order_blocks"):
            self.assertFalse(s[key]["available"])
            self.assertEqual((s[key]["bullish"], s[key]["bearish"]), ([], []))
            self.assertIn("engine down", s[key]["reason"])
        self.assertTrue(any("unavailable" in u for u in s["overall"]["uncertain"]))

    def test_a_summary_failure_can_never_break_the_analysis(self):
        with mock.patch.object(summ, "_build_summary", side_effect=RuntimeError("boom")), self.assertLogs("trademate.analysis", "ERROR"):
            a = analysis.build_analysis("EURUSD", "H1", series(UP), FORMING, 2)
        self.assertTrue(a["ok"])
        self.assertIsNone(a["summary"])
        self.assertEqual(a["status"]["code"], "READY")


class ApiTest(unittest.TestCase):
    class Broker:
        def __init__(self):
            self.rows = [Candle(*r[:5]) for r in DataFlowTest().walk()]

        def get_connection_status(self):
            return ConnectionStatus(ConnectionState.CONNECTED, "ok", 0.1)

        def get_candles(self, symbol, timeframe, count=300):
            return self.rows

        def get_symbol_spec(self, symbol):
            from trademate.errors import BrokerUnavailableError
            raise BrokerUnavailableError("no spec")

    def test_api_returns_the_summary_for_the_requested_symbol_and_timeframe(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(app_module, "adapter", self.Broker()), \
                mock.patch.object(app_module, "chart_store", ChartSettingsStore(Path(tmp) / "cs.json")):
            body = app.test_client().get("/api/analysis?symbol=EURUSD&timeframe=H1").get_json()
        self.assertEqual((body["symbol"], body["timeframe"]), ("EURUSD", "H1"))
        s = body["summary"]
        self.assertEqual(set(s), {"reference_price", "reference_note", "direction", "liquidity", "fvg", "order_blocks", "overall"})
        self.assertIn(s["direction"]["label"], {"bullish", "bearish", "mixed", "unclear"})
        self.assertFalse(s["liquidity"]["available"], "no tick size from this broker: liquidity says so instead of inventing levels")
        self.assertIn("tick size", s["liquidity"]["reason"])

    def test_a_stale_or_missing_feed_returns_no_summary(self):
        class Down(self.Broker):
            def get_connection_status(self):
                return ConnectionStatus(ConnectionState.DISCONNECTED, "MT5 is not running.")
        with mock.patch.object(app_module, "adapter", Down()):
            body = app.test_client().get("/api/analysis?symbol=EURUSD&timeframe=H1").get_json()
        self.assertNotIn("summary", body)
        self.assertEqual(body["status"]["code"], "UNAVAILABLE")


class FrontendWiringTest(unittest.TestCase):
    """Source-level guards for the browser fixes (the behaviour itself is exercised in tests.test_browser_task2)."""
    js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js").read_text(encoding="utf-8")

    def test_pending_chart_settings_are_flushed_when_the_page_is_hidden_through_the_single_post_site(self):
        self.assertIn("addEventListener('pagehide'", self.js)
        self.assertIn("keepalive: onExit === true", self.js)
        self.assertIn("trademate.chartThemeUnsaved", self.js)          # a choice the server never confirmed is re-sent on the next load
        import re
        self.assertEqual(len(re.findall(r"fetch\('/api/chart/settings', \{ method: 'POST'", self.js)), 1)

    def test_zone_and_order_overlays_are_gated_on_fresh_data(self):
        self.assertIn("!zonesFresh()", self.js)
        self.assertIn("function ordersFresh()", self.js)
        self.assertIn("ordersFresh()) return []", self.js)
        self.assertIn("Pending orders hidden: no fresh broker data", self.js)
        self.assertIn("Zones hidden: chart data is not current", self.js)

    def test_drawing_an_overlay_never_calls_an_order_endpoint(self):
        start = self.js.index("(function drawPendingOrders()")
        block = self.js[start:start + 6000]
        for bad in ("/api/order", "/api/position", "fetch(", "placeOrder("):
            self.assertNotIn(bad, block)


if __name__ == "__main__":
    unittest.main()
