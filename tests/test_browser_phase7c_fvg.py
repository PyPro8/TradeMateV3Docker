"""Phase 7C task 2: fair value gap zones on the chart and the independent overlay controls, in real Chromium with deterministic candles.

    python3 -m unittest tests.test_browser_phase7c_fvg -v        (SKIPPED, not passed, when Playwright/Chromium is missing)

The page is served by the real Flask app with a READ-ONLY stub broker (it has no order method), so no test can place an order.
The zones are checked in PIXELS: the edge of each zone must sit where the neighbouring candle's wick ends, which ties the drawn
boundary to the real price scale and the real candle positions without trusting the chart's own coordinate code.
"""

import json
import math
import unittest

from tests.browser_support import BrowserBase, QuoteStub, _shift
from tests.test_browser_phase7a import AUDIT_JS
from tests.test_browser_phase7b_liquidity import ROWS as LONG_ROWS
from tests.test_structure_regressions import mk

# (high, low, close). Hand-checked: candles 0,1,2 -> BULLISH gap 10..11 (c1 high 10, c3 low 11);
# candles 3,4,5 -> BEARISH gap 9..11 (c1 low 11, c3 high 9); candles 2,3,4 merely touch (11 == 11): no gap.
ROWS = [(10, 9, 9.5), (12, 9.8, 12), (12.5, 11, 12), (11.8, 11, 11.2), (11, 8, 8.2), (9, 7.5, 8), (9.5, 8, 9)]
# M15: windows (2,3,4) and (3,4,5) are bullish gaps 10.9..11 and 11.2..12.5; everything else overlaps.
ROWS_M15 = [(10, 9, 9.5), (10.5, 9.2, 10.4), (10.9, 9.8, 10.6), (11.2, 10.4, 11.0), (14, 11, 13.8), (14.5, 12.5, 14.2), (14.3, 13, 13.9)]

BULL = {"dark": (0x4C, 0x8D, 0xFF), "light": (0x1F, 0x5F, 0xD1)}
BEAR = {"dark": (0xC1, 0x6B, 0xFF), "light": (0x8A, 0x2B, 0xC9)}

PIX_JS = """(a) => { const cv = document.querySelector('#view-chart canvas'), ctx = cv.getContext('2d'), k = cv.width / cv.getBoundingClientRect().width;
  return a.points.map(([x, y]) => { const d = ctx.getImageData(Math.round(x * k), Math.round(y * k), 1, 1).data; return [d[0], d[1], d[2], d[3]]; }); }"""


class FvgStub(QuoteStub):
    def __init__(self):
        super().__init__()
        bad = mk(ROWS)
        bad[1] = [bad[1][0], bad[1][1], 5.0, 20.0, bad[1][4]]                      # high below low: the engine must refuse this series
        # M5 is long enough for the Analysis page to find structure and liquidity (7 candles are honestly "insufficient data" there)
        self.series_by["EURUSD"].update({"H1": mk(ROWS), "M15": mk(ROWS_M15), "H4": bad, "D1": mk(ROWS[:2]), "M5": mk(LONG_ROWS)})
        self.series_by["XAUUSD"].update({"H1": _shift(mk(ROWS), 1900.0), "M15": _shift(mk(ROWS[:6]), 1900.0)})   # M15 ends ON the bearish gap's third candle
        # quotes away from every zone boundary, so the bid / ask lines never coincide with an edge being measured
        self.quotes["EURUSD"] = (8.4, 8.5)
        self.quotes["XAUUSD"] = (1908.4, 1908.5)


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


class FvgBrowserTest(BrowserBase):
    broker_factory = FvgStub

    def setUp(self):
        super().setUp()
        self.broker.connected = True
        self.set_settings(structure_visible=False, liquidity_visible=False, fvg_visible=False, theme="dark")

    # ---------------- helpers ----------------
    def set_settings(self, **kw):
        """Save settings on the server, then reload so the page starts from them (it reads them once at load)."""
        self.page.goto(self.base + "/")
        self.page.evaluate("""async (b) => { await fetch('/api/chart/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)}); }""", kw)
        self.page.reload()
        self.until("window.TradeMateApp && document.getElementById('cs-fvg') && !document.getElementById('cs-fvg').disabled")

    def saved(self):
        return self.page.evaluate("async () => (await (await fetch('/api/chart/settings')).json()).settings")

    def frame(self):
        frames = [f for f in self.page.evaluate("() => window.TradeMateApp.fvgFrame()") if f]
        return frames[-1] if frames else None

    def wait_rects(self, n):
        self.until(f"window.TradeMateApp.fvgFrame().some(f => f && f.rects.length === {n})")
        return self.frame()

    def overlays(self):
        return self.page.evaluate("() => window.TradeMateApp.overlays()")

    def data(self):
        return self.page.evaluate("() => window.TradeMateApp.analysisData()")

    def pixels(self, points):
        return self.page.evaluate(PIX_JS, {"points": points})

    def go(self, symbol, tf):
        with self.page.expect_response(lambda r: "/api/candles" in r.url and f"timeframe={tf}" in r.url and symbol in r.url and r.status == 200, timeout=15000):
            self.page.evaluate("([s, t]) => window.TradeMateApp.openChart(s, t, {})", [symbol, tf])
        self.until(f"window.TradeMateApp.analysisData().candleKey === '{symbol}|{tf}'")

    def show_fvg_chart(self, **extra):
        self.set_settings(fvg_visible=True, **extra)
        self.open_chart()
        return self.wait_rects(2)

    @staticmethod
    def y_of(frame, price):
        p, r = frame["plot"], frame["range"]
        return p["t"] + (r["hi"] - price) / (r["hi"] - r["lo"]) * (p["b"] - p["t"])

    @staticmethod
    def x_of(frame, index):
        p, ir = frame["plot"], frame["ir"]
        return p["l"] + (p["r"] - p["l"]) - (ir["R"] - index) * ir["s"] - ir["s"] / 2

    def candle_extreme(self, frame, index, y_expected, zone_rgb, top, span=14):
        """Row of the topmost (top=True) or bottom-most candle pixel in a candle's wick column near y_expected, ignoring zone colours."""
        cx = round(self.x_of(frame, index))
        pts = [(x, y) for y in range(int(y_expected - span), int(y_expected + span) + 1) for x in (cx - 1, cx, cx + 1)]
        rows = [y for (x, y), (r, g, b, a) in zip(pts, self.pixels(pts)) if a >= 100 and dist((r, g, b), zone_rgb) > 70]
        self.assertTrue(rows, f"no candle pixels near y={y_expected:.1f} for candle {index}")
        return min(rows) if top else max(rows)

    # ================= default state =================
    def test_default_chart_is_clean_but_the_fvg_data_is_already_fetched_and_kept(self):
        self.open_chart()
        self.until("window.TradeMateApp.analysisData().fvg")
        self.assertEqual(self.overlays(), {"structure": False, "liquidity": False, "fvg": False, "order_blocks": False})
        self.assertIsNone(self.frame())                                           # nothing drawn
        self.assertEqual(self.data()["fvg"], {"key": "EURUSD|H1", "available": True, "gaps": 2})   # ...but the analysis is there
        urls = [r[1] for r in self.requests if "/api/candles" in r[1]]
        self.assertTrue(urls and all("fvg=1" in u for u in urls))
        self.assertFalse([u for u in urls if "liquidity=1" in u])                 # liquidity keeps its existing lazy behaviour

    # ================= correct boundaries, timestamps, price coordinates =================
    def test_bullish_and_bearish_zones_sit_exactly_at_the_engines_boundaries(self):
        f = self.show_fvg_chart()
        by = {r["direction"]: r for r in f["rects"]}
        bull, bear = by["bullish"], by["bearish"]
        self.assertEqual((bull["lower"], bull["upper"], bull["from"], bull["to"], bull["confirmed"]), (10, 11, 0, 2, True))
        self.assertEqual((bear["lower"], bear["upper"], bear["from"], bear["to"], bear["confirmed"]), (9, 11, 3, 5, True))
        for r in (bull, bear):                                                    # price -> y through the real scale
            self.assertAlmostEqual(r["yTop"], self.y_of(f, r["upper"]), delta=0.01)
            self.assertAlmostEqual(r["yBottom"], self.y_of(f, r["lower"]), delta=0.01)
            self.assertGreater(r["yBottom"], r["yTop"])

    def test_the_drawn_edges_line_up_with_the_neighbouring_candles_wicks(self):
        f = self.show_fvg_chart()
        by = {r["direction"]: r for r in f["rects"]}
        # bullish: lower edge = candle 0's high, upper edge = candle 2's low
        bull, rgb = by["bullish"], BULL["dark"]
        self.assertAlmostEqual(self.candle_extreme(f, 0, bull["yBottom"], rgb, top=True), bull["yBottom"], delta=2.5)
        self.assertAlmostEqual(self.candle_extreme(f, 2, bull["yTop"], rgb, top=False), bull["yTop"], delta=2.5)
        # bearish: upper edge = candle 3's low, lower edge = candle 5's high
        bear, rgb = by["bearish"], BEAR["dark"]
        self.assertAlmostEqual(self.candle_extreme(f, 3, bear["yTop"], rgb, top=False), bear["yTop"], delta=2.5)
        self.assertAlmostEqual(self.candle_extreme(f, 5, bear["yBottom"], rgb, top=True), bear["yBottom"], delta=2.5)

    def test_the_zone_spans_candle_one_to_candle_three_by_timestamp(self):
        f = self.show_fvg_chart()
        by = {r["direction"]: r for r in f["rects"]}
        half = max(3, f["ir"]["s"] * 0.35)
        for r, (a, c) in ((by["bullish"], (0, 2)), (by["bearish"], (3, 5))):
            self.assertAlmostEqual(r["x1"], self.x_of(f, a) - half, delta=0.01)
            self.assertAlmostEqual(r["x2"], self.x_of(f, c) + half, delta=0.01)
        # and the real candles are where the zone says: the wick of candle 0 is drawn at the zone's left candle position
        bull = by["bullish"]
        y = self.y_of(f, 9.5)                                                     # inside candle 0's wick (low 9 .. high 10)
        cols = range(int(bull["x1"]) - 4, int(bull["x1"]) + 14)
        px = self.pixels([(x, y) for x in cols])
        hits = [x for x, (r, g, b, a) in zip(cols, px) if a >= 100 and dist((r, g, b), BULL["dark"]) > 70]
        self.assertTrue(hits)
        self.assertAlmostEqual(sum(hits) / len(hits), bull["x1"] + max(3, f["ir"]["s"] * 0.35), delta=2.0)

    def test_zone_fill_is_drawn_inside_and_not_outside_the_boundaries(self):
        f = self.show_fvg_chart()
        bull = next(r for r in f["rects"] if r["direction"] == "bullish")
        cx = self.x_of(f, 0) + 2.5                                                # beside candle 0's column (a grid line can run through the column itself)
        mid = (bull["yTop"] + bull["yBottom"]) / 2
        inside, above, below = self.pixels([(cx, mid), (cx, bull["yTop"] - 12), (cx, bull["yBottom"] + 12)])
        self.assertTrue(40 <= inside[3] <= 80, inside)                            # translucent fill (~22%)
        self.assertLess(dist(inside[:3], BULL["dark"]), 14)
        self.assertLess(above[3], 40, above)                                      # nothing above the upper boundary

    # ================= unconfirmed vs confirmed =================
    def test_a_gap_completed_by_the_still_forming_candle_is_drawn_differently_and_marked(self):
        self.set_settings(fvg_visible=True)
        self.open_chart()
        confirmed = self.wait_rects(2)
        bear0 = next(r for r in confirmed["rects"] if r["direction"] == "bearish")
        conf_alpha = self.pixels([(self.x_of(confirmed, 3) + 2.5, (bear0["yTop"] + bear0["yBottom"]) / 2)])[0][3]
        self.go("XAUUSD", "M15")                                                  # 6 candles: ends ON the bearish gap's third candle
        f = self.wait_rects(2)
        by = {r["direction"]: r for r in f["rects"]}
        self.assertEqual((by["bullish"]["confirmed"], by["bullish"]["lower"], by["bullish"]["upper"]), (True, 1910, 1911))   # not affected by the forming candle
        r = by["bearish"]
        self.assertEqual((r["confirmed"], r["lower"], r["upper"]), (False, 1909, 1911))
        self.assertEqual(self.data()["fvg"], {"key": "XAUUSD|M15", "available": True, "gaps": 2})
        mid = (r["yTop"] + r["yBottom"]) / 2
        self.assertLess(self.pixels([(self.x_of(f, 3) + 2.5, mid)])[0][3], conf_alpha - 15)   # fainter fill than a confirmed zone
        pts = [(x, r["yTop"] + 0.5) for x in range(int(r["x1"]) + 2, int(r["x2"]) - 2)]  # the top edge is dashed: some pixels are empty
        alphas = [p[3] for p in self.pixels(pts)]
        self.assertTrue(any(a > 150 for a in alphas) and any(a < 100 for a in alphas), alphas)
        # the legend that explains dashed zones is on the page, next to the switch
        self.assertIn("still forming", self.page.evaluate("() => document.getElementById('cs-fvg').parentElement.innerText"))

    # ================= refresh / symbol / timeframe =================
    def test_repeated_refreshes_replace_the_zones_and_never_stack_them(self):
        f = self.show_fvg_chart()
        bull = next(r for r in f["rects"] if r["direction"] == "bullish")
        pt = (self.x_of(f, 0) + 2.5, (bull["yTop"] + bull["yBottom"]) / 2)
        first_alpha = self.pixels([pt])[0][3]
        for _ in range(4):
            with self.page.expect_response(lambda r: "/api/candles" in r.url and r.status == 200, timeout=15000):
                self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
            self.page.wait_for_timeout(150)
        f2 = self.wait_rects(2)
        self.assertEqual(self.page.evaluate("() => window.TradeMateApp.fvgDrawn()"), {"gaps": 2, "key": "EURUSD|H1"})
        self.assertEqual([(r["id"], r["upper"], r["lower"]) for r in f2["rects"]], [(r["id"], r["upper"], r["lower"]) for r in f["rects"]])
        self.assertEqual(len({r["id"] for r in f2["rects"]}), 2)
        self.assertAlmostEqual(self.pixels([pt])[0][3], first_alpha, delta=3)     # a zone drawn twice would be visibly more opaque

    def test_changing_timeframe_and_symbol_replaces_the_zones_with_the_new_ones(self):
        f = self.show_fvg_chart()
        self.assertEqual({(r["direction"], r["lower"], r["upper"]) for r in f["rects"]}, {("bullish", 10, 11), ("bearish", 9, 11)})
        self.go("EURUSD", "M15")                                                  # a different candle series: two bullish gaps
        f = self.wait_rects(2)
        self.assertEqual(f["key"], "EURUSD|M15")
        self.assertEqual([(r["direction"], r["lower"], r["upper"], r["from"], r["to"]) for r in f["rects"]],
                         [("bullish", 10.9, 11, 2, 4), ("bullish", 11.2, 12.5, 3, 5)])
        for r in f["rects"]:
            self.assertAlmostEqual(r["yTop"], self.y_of(f, r["upper"]), delta=0.01)
            self.assertAlmostEqual(r["yBottom"], self.y_of(f, r["lower"]), delta=0.01)
        self.go("XAUUSD", "H1")                                                   # another symbol, prices ~1900
        f = self.wait_rects(2)
        self.assertEqual(f["key"], "XAUUSD|H1")
        self.assertEqual({(r["direction"], r["lower"], r["upper"]) for r in f["rects"]}, {("bullish", 1910, 1911), ("bearish", 1909, 1911)})
        self.go("EURUSD", "H1")                                                   # and back: the originals, not a pile of old and new
        f = self.wait_rects(2)
        self.assertEqual({(r["direction"], r["lower"], r["upper"]) for r in f["rects"]}, {("bullish", 10, 11), ("bearish", 9, 11)})

    def test_no_zones_of_the_previous_symbol_are_drawn_while_the_new_one_is_loading(self):
        self.show_fvg_chart()
        # hold the XAUUSD answer back: until it arrives the chart must not draw EURUSD zones over (empty) XAUUSD candles
        held = []
        self.page.route(lambda url: "/api/candles" in url and "XAUUSD" in url, lambda route: held.append(route))
        self.page.evaluate("() => window.TradeMateApp.openChart('XAUUSD', 'H1', {})")
        self.until("true")
        self.page.wait_for_timeout(600)
        self.assertTrue(held, "the XAUUSD request was not held")
        self.assertIsNone(self.frame())                                           # no EURUSD zone over the unloaded XAUUSD chart
        for route in held:
            route.continue_()
        self.until("window.TradeMateApp.fvgFrame().some(f => f && f.key === 'XAUUSD|H1')", timeout=20)

    # ================= the controls =================
    def test_every_combination_of_the_three_overlays_works_and_each_button_is_independent(self):
        self.open_chart()
        btn = {k: f"#view-chart [data-role={k}-btn]" for k in ("structure", "liquidity", "fvg")}
        state = {"structure": False, "liquidity": False, "fvg": False, "order_blocks": False}      # (the 4th overlay stays off here; see the repair module)
        order = [None, "structure", "liquidity", "structure", "fvg", "structure", "liquidity", "structure"]   # Gray code: one flip each, all 8 states
        seen = set()
        for flip in order:
            if flip:
                self.page.click(btn[flip])
                state[flip] = not state[flip]
            seen.add(tuple(state.values()))
            self.until(f"JSON.stringify(window.TradeMateApp.overlays()) === '{json.dumps(state, separators=(',', ':'))}'")
            for k, sel in btn.items():
                self.assertEqual(self.page.get_attribute(sel, "aria-pressed"), "true" if state[k] else "false", (k, state))
            if state["fvg"]:
                self.wait_rects(2)
            else:
                self.assertIsNone(self.frame(), state)                            # hidden = nothing drawn
            if state["liquidity"]:
                self.until("window.TradeMateApp.liquidityDrawn() !== null", timeout=20)
            self.assertEqual(self.page.get_attribute("#view-chart [data-role=overlays-btn]", "aria-pressed"), "true" if any(state.values()) else "false")
        self.assertEqual(len(seen), 8)                                            # including "all three" and "none"
        self.page.wait_for_timeout(1200)
        saved = self.saved()
        self.assertEqual((saved["structure_visible"], saved["liquidity_visible"], saved["fvg_visible"], saved["order_blocks_visible"]), tuple(state.values()))

    def test_hiding_an_overlay_keeps_its_analysis_data_and_the_other_overlays(self):
        self.set_settings(structure_visible=True, liquidity_visible=True, fvg_visible=True)
        self.open_chart()
        self.wait_rects(2)
        self.until("window.TradeMateApp.analysisData().liquidity && window.TradeMateApp.analysisData().structure")
        before = self.data()
        self.page.click("#view-chart [data-role=fvg-btn]")                        # hide FVG only
        self.until("!window.TradeMateApp.overlays().fvg")
        self.assertIsNone(self.frame())
        self.assertEqual(self.overlays(), {"structure": True, "liquidity": True, "fvg": False, "order_blocks": False})
        self.assertEqual(self.data()["fvg"], before["fvg"])                       # the FVG analysis is still held
        self.assertEqual(self.data()["structure"], before["structure"])
        self.assertEqual(self.data()["liquidity"], before["liquidity"])
        with self.page.expect_response(lambda r: "/api/candles" in r.url and r.status == 200, timeout=15000):
            self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
        self.page.wait_for_timeout(200)
        self.assertEqual(self.data()["fvg"], {"key": "EURUSD|H1", "available": True, "gaps": 2})   # still refreshed while hidden
        self.page.click("#view-chart [data-role=fvg-btn]")                        # and showing it again needs no new fetch to appear
        self.wait_rects(2)

    def test_hide_all_stops_drawing_only_and_never_the_analysis(self):
        self.set_settings(structure_visible=True, liquidity_visible=True, fvg_visible=True)
        self.open_chart()
        self.wait_rects(2)
        self.until("window.TradeMateApp.analysisData().liquidity")
        n_requests = len(self.requests)
        self.page.click("#view-chart [data-role=overlays-btn]")                   # one click: hide everything
        self.until("JSON.stringify(window.TradeMateApp.overlays()) === '{\"structure\":false,\"liquidity\":false,\"fvg\":false,\"order_blocks\":false}'")
        self.assertIsNone(self.frame())
        self.assertEqual(self.page.get_attribute("#view-chart [data-role=overlays-btn]", "aria-pressed"), "false")
        with self.page.expect_response(lambda r: "/api/candles" in r.url and r.status == 200, timeout=15000):
            self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
        self.page.wait_for_timeout(200)
        d = self.data()
        self.assertTrue(d["structure"] and d["fvg"] and d["fvg"]["gaps"] == 2)    # structure and FVG are still analysed while hidden
        urls = [r[1] for r in self.requests[n_requests:] if "/api/candles" in r[1]]
        self.assertTrue(urls and all("structure=1" in u and "fvg=1" in u for u in urls))
        # the Analysis engines themselves are untouched: the Analysis page's own endpoint still returns structure AND liquidity
        res = self.page.evaluate("async () => (await fetch('/api/analysis?symbol=EURUSD&timeframe=M5')).json()")
        self.assertTrue(res["ok"] and res["liquidity"]["available"] and res["structure"]["swings"])
        self.page.click("#view-chart [data-role=overlays-btn]")                   # one click again: all three back
        self.until("JSON.stringify(window.TradeMateApp.overlays()) === '{\"structure\":true,\"liquidity\":true,\"fvg\":true,\"order_blocks\":true}'")
        self.wait_rects(2)

    def test_settings_panel_has_labelled_switches_show_all_hide_all_and_they_persist(self):
        self.page.goto(self.base + "/#settings")
        self.until("document.getElementById('cs-fvg') && !document.getElementById('cs-fvg').disabled")
        text = self.page.evaluate("() => document.getElementById('view-settings').innerText")
        for needed in ("Analysis overlays on the chart", "Show all", "Hide all", "fair value gaps", "market structure", "liquidity",
                       "not a prediction or a trade signal"):
            self.assertIn(needed.lower(), text.lower())
        for fake in ("premium", "discount"):                                      # (Order Blocks exist since the Phase 7C repair; these two still do not)
            self.assertNotIn(fake, text.lower())                                  # nothing for features that do not exist
        boxes = ("cs-structure", "cs-liquidity", "cs-fvg")
        checked = lambda: [self.page.is_checked("#" + b) for b in boxes]
        self.assertEqual(checked(), [False, False, False])
        self.page.check("#cs-fvg")                                                # one switch changes only itself
        self.assertEqual(checked(), [False, False, True])
        self.page.click("#cs-overlays-show")
        self.assertEqual(checked(), [True, True, True])
        self.page.wait_for_timeout(1200)
        self.page.reload()
        self.until("document.getElementById('cs-fvg') && !document.getElementById('cs-fvg').disabled")
        self.assertEqual(checked(), [True, True, True])                           # saved by the server: survives a reload / restart of the browser
        self.page.uncheck("#cs-liquidity")
        self.assertEqual(checked(), [True, False, True])
        self.page.click("#cs-overlays-hide")
        self.assertEqual(checked(), [False, False, False])
        self.page.wait_for_timeout(1200)
        saved = self.saved()
        self.assertEqual((saved["structure_visible"], saved["liquidity_visible"], saved["fvg_visible"]), (False, False, False))

    def test_toolbar_and_settings_stay_in_step_and_the_choice_survives_a_reload(self):
        self.open_chart()
        self.page.click("#view-chart [data-role=fvg-btn]")
        self.wait_rects(2)
        self.page.wait_for_timeout(1200)
        self.page.goto(self.base + "/#settings")
        self.until("document.getElementById('cs-fvg') && document.getElementById('cs-fvg').checked")   # same switch, other place
        self.page.reload()
        self.open_chart()
        self.assertTrue(self.page.evaluate("() => window.TradeMateApp.fvgVisible()"))
        self.wait_rects(2)

    def test_a_failed_save_is_shown_to_the_user_and_does_not_pretend_to_persist(self):
        self.page.goto(self.base + "/#settings")
        self.until("document.getElementById('cs-fvg') && !document.getElementById('cs-fvg').disabled")
        self.page.route("**/api/chart/settings", lambda route: route.fulfill(status=400, content_type="application/json",
                                                                              body=json.dumps({"ok": False, "message": "refused for the test"}))
                        if route.request.method == "POST" else route.continue_())
        self.page.check("#cs-fvg")
        self.until("!document.getElementById('cs-fvg').checked")                  # the switch goes back to what is really saved
        self.assertFalse(self.saved()["fvg_visible"])

    # ================= invalid / unavailable data =================
    def test_the_engine_refusing_the_candles_draws_nothing_and_the_chart_still_works(self):
        self.set_settings(fvg_visible=True)
        self.open_chart()
        self.wait_rects(2)
        self.go("EURUSD", "H4")                                                   # this series has an impossible candle
        self.assertEqual(self.data()["fvg"], {"key": "EURUSD|H4", "available": False, "gaps": 0})
        self.assertIsNone(self.frame())
        self.assertTrue(self.page.evaluate("() => document.querySelector('#view-chart [data-role=chart-msg]').hidden"))   # candles are still shown
        self.go("EURUSD", "D1")                                                   # only two candles: not enough for a gap
        self.assertEqual(self.data()["fvg"]["available"], False)
        self.assertIsNone(self.frame())
        self.go("EURUSD", "H1")                                                   # and it recovers by itself
        self.wait_rects(2)

    def test_garbage_in_the_fvg_block_never_crashes_the_chart(self):
        garbage = [None, "x", 5, [], {}, {"available": True}, {"available": True, "gaps": "x"}, {"available": True, "gaps": [None, 5, "x", {}]},
                   {"available": True, "gaps": [{"direction": "bullish", "upper": "a", "lower": 1, "first_time": 1, "third_time": 2}]},
                   {"available": True, "gaps": [{"direction": "bullish", "upper": 11, "lower": 10, "first_time": 5, "third_time": 7}]},
                   {"available": False, "reason": "x", "gaps": [{"direction": "bullish", "upper": 11, "lower": 10}]}]
        self.set_settings(fvg_visible=True)
        index = {"i": 0}

        def patch(route):
            res = route.fetch()
            body = res.json()
            body["fvg"] = garbage[index["i"] % len(garbage)]
            if garbage[index["i"] % len(garbage)] is None:
                body.pop("fvg", None)
            route.fulfill(response=res, body=json.dumps(body))

        self.page.route(lambda url: "/api/candles" in url, patch)
        self.open_chart()
        for i in range(len(garbage)):
            index["i"] = i
            with self.page.expect_response(lambda r: "/api/candles" in r.url and r.status == 200, timeout=15000):
                self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
            self.page.wait_for_timeout(120)
            frame = self.frame()
            self.assertTrue(frame is None or frame["rects"] == [], (i, garbage[i]))     # never a fabricated zone
            self.assertGreater(self.page.evaluate("() => document.querySelector('#view-chart canvas').width"), 100)
            self.assertTrue(self.page.evaluate("() => document.querySelector('#view-chart [data-role=chart-msg]').hidden"), i)   # candles still drawn
        # (tearDown also fails the test on any JavaScript error raised during all of this)

    def test_a_failing_fvg_engine_on_the_server_does_not_take_the_candles_down(self):
        self.set_settings(fvg_visible=True, structure_visible=True)
        def failing(route):
            body = route.fetch().json()
            body["fvg"] = {"available": False, "gaps": [], "reason": "Fair value gaps could not be analysed because of an internal error (see the log)."}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        self.page.route(lambda url: "/api/candles" in url, failing)
        self.open_chart()
        self.until("window.TradeMateApp.analysisData().structure")
        self.assertEqual(self.data()["fvg"], {"key": "EURUSD|H1", "available": False, "gaps": 0})
        self.assertIsNone(self.frame())

    # ================= trade levels and chart interactions are preserved =================
    def test_trade_level_lines_and_chart_interactions_still_work_with_every_overlay_on(self):
        self.set_settings(structure_visible=True, liquidity_visible=True, fvg_visible=True)
        self.open_chart()
        f = self.wait_rects(2)
        self.until("window.TradeMateApp.liquidityDrawn() !== null", timeout=20)
        # a draft stop-loss line (red, dashed) is still drawn at its real price, above the zones
        self.page.fill("#view-chart [data-role=sl]", "7.9")
        self.page.dispatch_event("#view-chart [data-role=sl]", "input")
        self.page.wait_for_timeout(200)
        f = self.frame()
        y = self.y_of(f, 7.9)
        row = self.pixels([(x, y) for x in range(int(f["plot"]["l"]) + 20, int(f["plot"]["l"]) + 220)])
        self.assertGreater(sum(1 for r, g, b, a in row if a > 150 and r > 200 and g < 130 and b < 140), 20, "the draft stop-loss line is missing")
        self.assertEqual(self.page.evaluate("() => document.querySelector('#view-chart [data-role=sl]').value"), "7.9")
        self.assertEqual(self.page.evaluate("() => window.TradeMateApp.fvgFrame().filter(Boolean).pop().rects.length"), 2)   # zones unaffected
        # zoom in: the candles get wider and the zones follow them
        before = {r["direction"]: r for r in self.frame()["rects"]}
        self.page.click("#view-chart [data-zoom=in]")
        self.page.wait_for_timeout(200)
        after = self.frame()
        self.assertGreater(after["ir"]["s"], f["ir"]["s"])
        wide = {r["direction"]: r for r in after["rects"]}
        self.assertGreater(wide["bullish"]["x2"] - wide["bullish"]["x1"], before["bullish"]["x2"] - before["bullish"]["x1"])
        self.assertAlmostEqual(wide["bullish"]["yTop"], self.y_of(after, 11), delta=0.01)
        # pan by dragging: the zones move with the candles, sideways, by the same distance
        box = self.page.evaluate("() => { const r = document.querySelector('#view-chart canvas').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }")
        x0, y0 = box[0] + 200, box[1] + box[3] * 0.5
        x_before = {r["direction"]: r["x1"] for r in self.frame()["rects"]}
        self.page.mouse.move(x0, y0)
        self.page.mouse.down()
        self.page.mouse.move(x0 - 60, y0, steps=6)
        self.page.mouse.up()
        self.page.wait_for_timeout(200)
        x_after = {r["direction"]: r["x1"] for r in self.frame()["rects"]}
        for k in x_before:
            self.assertAlmostEqual(x_before[k] - x_after[k], 60, delta=2.0, msg=k)
        # no order was created by any of this
        self.assertFalse([r for r in self.requests if r[0] == "POST" and ("/api/order" in r[1] or "/api/position" in r[1] or "/api/pending" in r[1])])

    # ================= theme and small screens =================
    def test_light_theme_uses_the_light_palette_and_stays_readable(self):
        self.set_settings(fvg_visible=True, theme="light")
        self.open_chart()
        f = self.wait_rects(2)
        self.assertEqual(f["theme"], "light")
        self.assertEqual(tuple(round(v) for v in self.hex_rgb(f["colors"]["bullish"])), BULL["light"])
        self.assertEqual(tuple(round(v) for v in self.hex_rgb(f["colors"]["bearish"])), BEAR["light"])
        bull = next(r for r in f["rects"] if r["direction"] == "bullish")
        inside = self.pixels([(self.x_of(f, 0) + 2.5, (bull["yTop"] + bull["yBottom"]) / 2)])[0]
        self.assertLess(dist(inside[:3], BULL["light"]), 14)
        self.assertTrue(40 <= inside[3] <= 80)

    @staticmethod
    def hex_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    def test_the_settings_text_is_readable_in_both_themes(self):
        self.page.goto(self.base + "/#settings")
        self.until("document.getElementById('cs-fvg') && !document.getElementById('cs-fvg').disabled")
        for theme in ("dark", "light"):
            self.page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)", theme)
            bad = [x for x in self.page.evaluate(AUDIT_JS) if not x["disabled"] and "btn-primary" not in x["cls"]]
            self.assertEqual(bad, [], theme)

    def test_on_a_phone_the_new_buttons_are_reachable_and_the_page_does_not_overflow(self):
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.open_chart()
        self.assertLessEqual(self.page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth"), 1)
        for role in ("fvg-btn", "overlays-btn"):
            sel = f"#view-chart [data-role={role}]"
            self.page.locator(sel).scroll_into_view_if_needed()
            box = self.page.locator(sel).bounding_box()
            self.assertGreaterEqual(min(box["width"], box["height"]), 36)         # a finger-sized target
        self.page.locator("#view-chart [data-role=fvg-btn]").scroll_into_view_if_needed()
        self.page.click("#view-chart [data-role=fvg-btn]")
        self.assertTrue(self.page.evaluate("() => window.TradeMateApp.fvgVisible()"))
        self.wait_rects(2)
        self.assertLessEqual(self.page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth"), 1)


if __name__ == "__main__":
    unittest.main()
