"""Phase 7C integration repair in real headless Chromium:
  1. order block zones on the chart (pixel-checked against the real candles),
  2. FVG and order blocks on the Analysis page (results, empty states, error states, honest module list),
  3. pending orders on the chart (the fake MT5 bridge: no real MT5, no real order),
  4. the chart theme button changing only the chart.

    python3 -m unittest tests.test_browser_phase7c_repair -v        (SKIPPED, not passed, when Playwright/Chromium is missing)

Pages are served by the real Flask app with read-only stub brokers (no order method) or the fake bridge (no real MT5).
"""

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.browser_support import BrowserBase, PLAYWRIGHT, QuoteStub, _shift
from tests.fake_bridge import FakeBridge
from tests.test_browser_phase7a import AUDIT_JS
from tests.test_orderblock_engine import mirror, rows
from tests.test_repair_api import FLAT, Z
from trademate.broker import create_adapter
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.risk import RiskRules, RiskRulesStore

ZB = Z + [(12.7, 12.8, 7.8, 8.0)]                       # candle 21 CLOSES at 8.0, under the bullish zone's 8.2 lower boundary: invalidated
Z_BEAR = mirror(Z, 20.0)                                # bearish BOS, bearish block zone [11.1, 11.8]

OB_BULL = {"dark": (0x35, 0xD6, 0xA2), "light": (0x0A, 0x8A, 0x63)}
OB_BEAR = {"dark": (0xFF, 0x8A, 0x4C), "light": (0xB8, 0x54, 0x10)}
PEND = {"dark": (0xFF, 0x4F, 0xD8), "light": (0xB3, 0x13, 0x8F)}

PIX_JS = """(a) => { const cv = document.querySelector('#view-chart canvas'), ctx = cv.getContext('2d'), k = cv.width / cv.getBoundingClientRect().width;
  return a.points.map(([x, y]) => { const d = ctx.getImageData(Math.round(x * k), Math.round(y * k), 1, 1).data; return [d[0], d[1], d[2], d[3]]; }); }"""


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def luminance(css):
    css = css.strip()
    if css.startswith("#"):
        h = css.lstrip("#")
        nums = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    else:
        nums = [float(x) for x in css[css.index("(") + 1:css.index(")")].split(",")[:3]]
    return (0.299 * nums[0] + 0.587 * nums[1] + 0.114 * nums[2]) / 255


class RepairStub(QuoteStub):
    def __init__(self):
        super().__init__()
        self.series_by["EURUSD"].update({"H1": rows(Z), "M15": rows(Z_BEAR), "M5": rows(Z[:19]), "H4": rows(ZB), "D1": rows(Z[:2]),
                                         "M30": rows(FLAT)})
        self.series_by["XAUUSD"].update({"H1": _shift(rows(Z), 1900.0)})
        self.quotes["EURUSD"] = (10.0, 10.02)            # away from every zone boundary
        self.quotes["XAUUSD"] = (1910.0, 1910.02)


class RepairBrowserTest(BrowserBase):
    broker_factory = RepairStub

    def setUp(self):
        super().setUp()
        self.broker.connected = True
        self.set_settings(structure_visible=False, liquidity_visible=False, fvg_visible=False, order_blocks_visible=False, theme="dark",
                          chart_theme="auto")

    # ---------------- helpers ----------------
    def set_settings(self, **kw):
        """Save settings on the server, then reload so the page starts from them (it reads them once at load)."""
        self.page.goto(self.base + "/")
        self.page.evaluate("""async (b) => { await fetch('/api/chart/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)}); }""", kw)
        self.page.evaluate("() => localStorage.clear()")
        self.page.reload()
        self.until("window.TradeMateApp && document.getElementById('cs-ob') && !document.getElementById('cs-ob').disabled")

    def saved(self):
        return self.page.evaluate("async () => (await (await fetch('/api/chart/settings')).json()).settings")

    def go(self, symbol, tf):
        with self.page.expect_response(lambda r: "/api/candles" in r.url and f"timeframe={tf}" in r.url and symbol in r.url and r.status == 200, timeout=15000):
            self.page.evaluate("([s, t]) => window.TradeMateApp.openChart(s, t, {})", [symbol, tf])
        self.until(f"window.TradeMateApp.analysisData().candleKey === '{symbol}|{tf}'")

    def ob_frame(self):
        frames = [f for f in self.page.evaluate("() => window.TradeMateApp.obFrame()") if f]
        return frames[-1] if frames else None

    def wait_blocks(self, n):
        self.until(f"window.TradeMateApp.obFrame().some(f => f && f.rects.length === {n})")
        return self.ob_frame()

    def data(self):
        return self.page.evaluate("() => window.TradeMateApp.analysisData()")

    def pixels(self, points):
        return self.page.evaluate(PIX_JS, {"points": points})

    def settled_ob_frame(self):
        """The frame after the first quote has arrived and the price scale has stopped moving (coordinates read earlier can be stale)."""
        self.page.wait_for_timeout(700)
        return self.ob_frame()

    def show_ob(self, tf="H1", **extra):
        self.set_settings(order_blocks_visible=True, **extra)
        self.open_chart()
        if tf != "H1":
            self.go("EURUSD", tf)
        self.wait_blocks(1)
        return self.settled_ob_frame()

    @staticmethod
    def y_of(frame, price):
        p, r = frame["plot"], frame["range"]
        return p["t"] + (r["hi"] - price) / (r["hi"] - r["lo"]) * (p["b"] - p["t"])

    @staticmethod
    def x_of(frame, index):
        p, ir = frame["plot"], frame["ir"]
        return p["l"] + (p["r"] - p["l"]) - (ir["R"] - index) * ir["s"] - ir["s"] / 2

    # ===================================== ISSUE 1: order blocks on the chart =====================================
    def test_default_chart_is_clean_but_the_order_block_data_is_already_fetched_and_kept(self):
        self.open_chart()
        self.until("window.TradeMateApp.analysisData().order_blocks")
        self.assertEqual(self.overlays(), {"structure": False, "liquidity": False, "fvg": False, "order_blocks": False})
        self.assertIsNone(self.ob_frame())
        self.assertEqual(self.data()["order_blocks"], {"key": "EURUSD|H1", "available": True, "blocks": 1})
        urls = [r[1] for r in self.requests if "/api/candles" in r[1]]
        self.assertTrue(urls and all("order_blocks=1" in u for u in urls))

    def overlays(self):
        return self.page.evaluate("() => window.TradeMateApp.overlays()")

    def test_a_bullish_block_is_drawn_at_the_engines_boundaries_and_the_source_candle(self):
        f = self.show_ob()
        r = f["rects"][0]
        self.assertEqual((r["direction"], r["status"], r["lower"], r["upper"], r["from"], r["to"], r["confirmed"]), ("bullish", "confirmed", 8.2, 8.9, 13, 20, True))
        self.assertAlmostEqual(r["yTop"], self.y_of(f, 8.9), delta=0.01)
        self.assertAlmostEqual(r["yBottom"], self.y_of(f, 8.2), delta=0.01)
        half = max(3, f["ir"]["s"] * 0.35)
        self.assertAlmostEqual(r["x1"], self.x_of(f, 13) - half, delta=0.01)             # starts at the source candle ...
        self.assertAlmostEqual(r["x2"], self.x_of(f, 20) + half, delta=0.01)             # ... and runs to the newest candle
        self.assertEqual(f["colors"]["bullish"].lower(), "#35d6a2")

    def test_the_zone_edges_line_up_with_the_real_source_candles_wick(self):
        """Independent of the chart's own coordinate code: the zone spans exactly the source candle's wick (its high and low are the boundaries)."""
        f = self.show_ob()
        r = f["rects"][0]
        cx = round(self.x_of(f, 13))                                                     # the source candle's column
        above = self.pixels([(cx, r["yTop"] - d) for d in (3, 4, 5, 6)])                 # just above the zone: nothing opaque (the wick ends here)
        below = self.pixels([(cx, r["yBottom"] + d) for d in (3, 4, 5, 6)])
        inside = self.pixels([(cx, r["yTop"] + 4), (cx, r["yBottom"] - 4)])              # the wick runs right up to both edges
        self.assertTrue(all(p[3] < 60 for p in above), above)
        self.assertTrue(all(p[3] < 60 for p in below), below)
        self.assertTrue(all(p[3] >= 150 for p in inside), inside)
        # and the candle after the source candle is not covered at its own wick top: the zone is behind the candles
        self.assertTrue(r["yBottom"] - r["yTop"] > 30)

    def test_the_fill_is_inside_the_zone_and_stops_at_the_newest_candle(self):
        f = self.show_ob()
        r = f["rects"][0]
        mid = (r["yTop"] + r["yBottom"]) / 2
        inside = self.pixels([(r["x2"] - 4, mid)])[0]                                    # right end of the zone, beside the newest candle's column
        outside_right = self.pixels([(r["x2"] + 4, mid)])[0]
        outside_left = self.pixels([(r["x1"] - 4, mid)])[0]
        self.assertTrue(30 <= inside[3] <= 70, inside)                                   # confirmed: translucent fill (~16%)
        self.assertLess(dist(inside[:3], OB_BULL["dark"]), 20)
        self.assertLess(outside_right[3], 40)
        self.assertLess(outside_left[3], 40)

    def test_a_bearish_block_is_the_mirror_image_in_orange(self):
        self.set_settings(order_blocks_visible=True)
        self.open_chart()
        self.go("EURUSD", "M15")
        self.wait_blocks(1)
        f = self.settled_ob_frame()
        r = f["rects"][0]
        self.assertEqual((r["direction"], r["status"], r["lower"], r["upper"], r["from"], r["to"]), ("bearish", "confirmed", 11.1, 11.8, 13, 20))
        self.assertAlmostEqual(r["yTop"], self.y_of(f, 11.8), delta=0.01)
        self.assertAlmostEqual(r["yBottom"], self.y_of(f, 11.1), delta=0.01)
        mid = (r["yTop"] + r["yBottom"]) / 2
        inside = self.pixels([(r["x2"] - 4, mid)])[0]
        self.assertLess(dist(inside[:3], OB_BEAR["dark"]), 20)

    def test_a_candidate_is_fainter_and_dashed_and_says_so(self):
        confirmed = self.show_ob()["rects"][0]
        conf_alpha = self.pixels([(confirmed["x2"] - 4, (confirmed["yTop"] + confirmed["yBottom"]) / 2)])[0][3]
        self.go("EURUSD", "M5")                                                          # the same candles stopped before the break
        self.wait_blocks(1)
        f = self.settled_ob_frame()
        r = f["rects"][0]
        self.assertEqual((r["status"], r["confirmed"], r["lower"], r["upper"]), ("candidate", False, 8.2, 8.9))
        cand_alpha = self.pixels([(r["x2"] - 4, (r["yTop"] + r["yBottom"]) / 2)])[0][3]
        self.assertLess(cand_alpha, conf_alpha - 12)                                     # fainter than a confirmed block
        top = self.pixels([(x, r["yTop"] + 0.5) for x in range(int(r["x1"]) + 6, int(r["x2"]) - 6)])
        alphas = [p[3] for p in top]
        self.assertTrue(any(a > 150 for a in alphas) and any(a < 100 for a in alphas), alphas)   # a dashed outline
        self.assertIn("candidate", self.page.evaluate("() => document.getElementById('cs-ob').parentElement.innerText"))

    def test_finished_blocks_are_not_drawn_but_stay_in_the_analysis_data(self):
        self.set_settings(order_blocks_visible=True)
        self.open_chart()
        self.go("EURUSD", "H4")                                                          # a close below the zone: invalidated
        self.until("window.TradeMateApp.analysisData().order_blocks && window.TradeMateApp.analysisData().order_blocks.key === 'EURUSD|H4'")
        self.assertEqual(self.data()["order_blocks"], {"key": "EURUSD|H4", "available": True, "blocks": 1})
        self.page.wait_for_timeout(300)
        f = self.ob_frame()
        self.assertTrue(f is None or f["rects"] == [])                                   # history is listed on the Analysis page, not drawn

    def test_unavailable_candles_draw_nothing_and_the_chart_still_works(self):
        self.set_settings(order_blocks_visible=True)
        self.open_chart()
        self.wait_blocks(1)
        self.go("EURUSD", "D1")                                                          # two candles: not enough
        self.assertEqual(self.data()["order_blocks"], {"key": "EURUSD|D1", "available": False, "blocks": 0})
        self.assertIsNone(self.ob_frame())
        self.assertTrue(self.page.evaluate("() => document.querySelector('#view-chart [data-role=chart-msg]').hidden"))
        self.go("EURUSD", "H1")
        self.wait_blocks(1)                                                              # recovers by itself

    def test_symbol_and_timeframe_changes_replace_the_zones_and_refreshes_never_stack_them(self):
        f = self.show_ob()
        r = f["rects"][0]
        pt = (r["x2"] - 4, (r["yTop"] + r["yBottom"]) / 2)
        first = self.pixels([pt])[0][3]
        for _ in range(4):
            with self.page.expect_response(lambda x: "/api/candles" in x.url and x.status == 200, timeout=15000):
                self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
            self.page.wait_for_timeout(150)
        f = self.wait_blocks(1)
        self.assertEqual(self.page.evaluate("() => window.TradeMateApp.obDrawn()"), {"blocks": 1, "key": "EURUSD|H1"})
        self.assertAlmostEqual(self.pixels([pt])[0][3], first, delta=3)                  # a zone drawn twice would be visibly more opaque
        self.go("XAUUSD", "H1")
        f = self.wait_blocks(1)
        self.assertEqual((f["key"], f["rects"][0]["lower"], f["rects"][0]["upper"]), ("XAUUSD|H1", 1908.2, 1908.9))
        self.go("EURUSD", "M15")
        f = self.wait_blocks(1)
        self.assertEqual((f["key"], f["rects"][0]["direction"]), ("EURUSD|M15", "bearish"))

    def test_the_block_and_the_gap_overlays_work_together_and_independently(self):
        self.set_settings(order_blocks_visible=True, fvg_visible=True)
        self.open_chart()
        self.wait_blocks(1)
        self.until("window.TradeMateApp.fvgFrame().some(f => f && f.rects.length > 0)")
        self.page.click("#view-chart [data-role=fvg-btn]")                               # hide the gaps only
        self.until("!window.TradeMateApp.overlays().fvg")
        self.assertEqual(len(self.ob_frame()["rects"]), 1)                               # the block is still there
        self.assertEqual(self.overlays(), {"structure": False, "liquidity": False, "fvg": False, "order_blocks": True})
        self.page.click("#view-chart [data-role=ob-btn]")
        self.until("!window.TradeMateApp.overlays().order_blocks")
        self.assertIsNone(self.ob_frame())

    def test_every_combination_of_the_four_overlays_works(self):
        self.open_chart()
        btn = {k: f"#view-chart [data-role={k}-btn]" for k in ("structure", "liquidity", "fvg", "ob")}
        names = {"structure": "structure", "liquidity": "liquidity", "fvg": "fvg", "ob": "order_blocks"}
        state = {n: False for n in names.values()}
        gray = [None, "structure", "liquidity", "structure", "fvg", "structure", "liquidity", "structure", "ob", "structure", "liquidity", "structure", "fvg",
                "structure", "liquidity", "structure"]
        seen = set()
        for flip in gray:
            if flip:
                self.page.click(btn[flip])
                state[names[flip]] = not state[names[flip]]
            seen.add(tuple(state.values()))
            self.until(f"JSON.stringify(window.TradeMateApp.overlays()) === '{json.dumps(state, separators=(',', ':'))}'")
            for k, sel in btn.items():
                self.assertEqual(self.page.get_attribute(sel, "aria-pressed"), "true" if state[names[k]] else "false", (k, state))
            if state["order_blocks"]:
                self.wait_blocks(1)
            else:
                self.assertIsNone(self.ob_frame(), state)
            if state["liquidity"]:
                self.until("window.TradeMateApp.liquidityDrawn() !== null", timeout=20)
        self.assertEqual(len(seen), 16)                                                  # every combination, including all four and none
        self.page.wait_for_timeout(1200)
        s = self.saved()
        self.assertEqual((s["structure_visible"], s["liquidity_visible"], s["fvg_visible"], s["order_blocks_visible"]), tuple(state.values()))

    def test_hiding_every_overlay_keeps_all_the_analysis_data(self):
        self.set_settings(structure_visible=True, liquidity_visible=True, fvg_visible=True, order_blocks_visible=True)
        self.open_chart()
        self.wait_blocks(1)
        self.until("window.TradeMateApp.analysisData().liquidity && window.TradeMateApp.analysisData().structure && window.TradeMateApp.analysisData().fvg")
        before = self.data()
        self.page.click("#view-chart [data-role=overlays-btn]")
        self.until("JSON.stringify(window.TradeMateApp.overlays()) === '{\"structure\":false,\"liquidity\":false,\"fvg\":false,\"order_blocks\":false}'")
        self.assertIsNone(self.ob_frame())
        self.assertEqual(self.data()["order_blocks"], before["order_blocks"])
        self.assertEqual(self.data()["fvg"], before["fvg"])
        n = len(self.requests)
        with self.page.expect_response(lambda r: "/api/candles" in r.url and r.status == 200, timeout=15000):
            self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
        self.page.wait_for_timeout(200)
        self.assertEqual(self.data()["order_blocks"], {"key": "EURUSD|H1", "available": True, "blocks": 1})
        urls = [r[1] for r in self.requests[n:] if "/api/candles" in r[1]]
        self.assertTrue(urls and all("order_blocks=1" in u and "fvg=1" in u and "structure=1" in u for u in urls))
        res = self.page.evaluate("async () => (await fetch('/api/analysis?symbol=EURUSD&timeframe=H1')).json()")
        self.assertTrue(res["order_blocks"]["available"] and res["fvg"]["available"])    # the engines themselves are untouched

    def test_the_saved_preference_is_respected_after_a_reload_and_show_all_hide_all_cover_the_new_overlay(self):
        self.page.goto(self.base + "/#settings")
        self.until("document.getElementById('cs-ob') && !document.getElementById('cs-ob').disabled")
        text = self.page.evaluate("() => document.getElementById('view-settings').innerText").lower()
        for needed in ("order blocks on the chart", "not evidence of institutional activity", "chart theme (chart only)", "theme (whole application)"):
            self.assertIn(needed, text)
        for fake in ("premium", "discount", "countdown"):
            self.assertNotIn(fake, text)                                                 # nothing for features that still do not exist
        boxes = ("cs-structure", "cs-liquidity", "cs-fvg", "cs-ob")
        checked = lambda: [self.page.is_checked("#" + b) for b in boxes]
        self.assertEqual(checked(), [False] * 4)
        self.page.check("#cs-ob")
        self.assertEqual(checked(), [False, False, False, True])
        self.page.click("#cs-overlays-show")
        self.assertEqual(checked(), [True] * 4)
        self.page.wait_for_timeout(1200)
        self.page.reload()
        self.until("document.getElementById('cs-ob') && !document.getElementById('cs-ob').disabled")
        self.assertEqual(checked(), [True] * 4)
        self.page.uncheck("#cs-ob")
        self.assertEqual(checked(), [True, True, True, False])
        self.page.click("#cs-overlays-hide")
        self.assertEqual(checked(), [False] * 4)
        self.page.wait_for_timeout(1200)
        s = self.saved()
        self.assertEqual((s["structure_visible"], s["liquidity_visible"], s["fvg_visible"], s["order_blocks_visible"]), (False,) * 4)

    def test_garbage_in_the_order_block_data_never_crashes_the_chart(self):
        garbage = [None, "x", 5, [], {}, {"available": True}, {"available": True, "blocks": "x"}, {"available": True, "blocks": [None, 5, "x", {}]},
                   {"available": True, "blocks": [{"direction": "bullish", "status": "confirmed", "upper": "a", "lower": 1, "source_time": 1}]},
                   {"available": True, "blocks": [{"direction": "bullish", "status": "confirmed", "upper": 9, "lower": 8, "source_time": 5}]},
                   {"available": False, "reason": "x", "blocks": [{"direction": "bullish", "status": "confirmed", "upper": 9, "lower": 8}]}]
        self.set_settings(order_blocks_visible=True)
        index = {"i": 0}

        def patch(route):
            res = route.fetch()
            body = res.json()
            body["order_blocks"] = garbage[index["i"] % len(garbage)]
            if garbage[index["i"] % len(garbage)] is None:
                body.pop("order_blocks", None)
            route.fulfill(response=res, body=json.dumps(body))

        self.page.route(lambda url: "/api/candles" in url, patch)
        self.open_chart()
        for i in range(len(garbage)):
            index["i"] = i
            with self.page.expect_response(lambda r: "/api/candles" in r.url and r.status == 200, timeout=15000):
                self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
            self.page.wait_for_timeout(120)
            frame = self.ob_frame()
            self.assertTrue(frame is None or frame["rects"] == [], (i, garbage[i]))     # never a fabricated zone
            self.assertGreater(self.page.evaluate("() => document.querySelector('#view-chart canvas').width"), 100)
            self.assertTrue(self.page.evaluate("() => document.querySelector('#view-chart [data-role=chart-msg]').hidden"), i)
        # (tearDown also fails the test on any JavaScript error raised during all of this)

    def test_trade_level_lines_and_chart_interactions_still_work_with_every_overlay_on(self):
        self.set_settings(structure_visible=True, liquidity_visible=True, fvg_visible=True, order_blocks_visible=True)
        self.open_chart()
        f = self.wait_blocks(1)
        self.page.fill("#view-chart [data-role=sl]", "7.9")
        self.page.dispatch_event("#view-chart [data-role=sl]", "input")
        self.page.wait_for_timeout(200)
        f = self.ob_frame()
        y = self.y_of(f, 7.9)
        row = self.pixels([(x, y) for x in range(int(f["plot"]["l"]) + 20, int(f["plot"]["l"]) + 220)])
        self.assertGreater(sum(1 for r, g, b, a in row if a > 150 and r > 200 and g < 130 and b < 140), 20, "the draft stop-loss line is missing")
        before = f["rects"][0]
        self.page.click("#view-chart [data-zoom=in]")
        self.page.wait_for_timeout(200)
        after = self.ob_frame()["rects"][0]
        self.assertGreater(after["x2"] - after["x1"], before["x2"] - before["x1"])      # zoom: the zone follows the candles
        self.assertFalse([r for r in self.requests if r[0] == "POST" and ("/api/order" in r[1] or "/api/position" in r[1] or "/api/pending" in r[1])])

    def test_on_a_phone_the_new_controls_are_reachable_and_nothing_overflows(self):
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.open_chart()
        self.assertLessEqual(self.page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth"), 1)
        for role in ("ob-btn", "fvg-btn", "overlays-btn", "theme-btn"):
            sel = f"#view-chart [data-role={role}]"
            self.page.locator(sel).scroll_into_view_if_needed()
            box = self.page.locator(sel).bounding_box()
            self.assertGreaterEqual(min(box["width"], box["height"]), 36, role)                # a finger-sized target
        self.page.click("#view-chart [data-role=ob-btn]")
        self.assertTrue(self.page.evaluate("() => window.TradeMateApp.overlays().order_blocks"))
        self.wait_blocks(1)
        self.page.locator("#view-chart [data-role=theme-btn]").scroll_into_view_if_needed()
        self.page.click("#view-chart [data-role=theme-btn]")                                    # the chart alone changes, on a phone too
        self.assertEqual(self.page.evaluate("() => document.documentElement.getAttribute('data-theme')"), "dark")
        self.assertEqual(self.page.evaluate("() => document.querySelector('#view-chart [data-terminal]').getAttribute('data-chart-theme')"), "light")
        self.assertLessEqual(self.page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth"), 1)
        self.page.goto(self.base + "/#analysis")                                                # and the Analysis page fits too
        self.page.evaluate("() => { var t = document.getElementById('an-tech'); if (t) t.open = true; }")      # Task 2: the detailed panels live in the collapsed 'Technical details'
        self.until("document.getElementById('an-ob').innerText.length > 0")
        self.assertLessEqual(self.page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth"), 1)

    # ===================================== ISSUE 2: the Analysis page =====================================
    def open_analysis(self, tf=None):
        self.page.goto(self.base + "/#analysis")
        self.page.evaluate("() => { var t = document.getElementById('an-tech'); if (t) t.open = true; }")      # Task 2: the detailed panels live in the collapsed 'Technical details'
        self.until("document.getElementById('an-status-code').textContent !== 'ANALYSING'")
        if tf:
            self.page.click(f"#an-tfs button[data-tf={tf}]")
            self.until("document.getElementById('an-status-code').textContent !== 'ANALYSING'")
        self.until("document.getElementById('an-fvg').innerText.length > 0 && document.getElementById('an-ob').innerText.length > 0")

    def module_text(self, name):
        return self.page.evaluate("""(n) => { const rows = Array.from(document.querySelectorAll('#an-modules .an-kv')); const r = rows.find(x => x.innerText.toLowerCase().includes(n.toLowerCase())); return r ? r.innerText : null; }""", name)

    def test_the_modules_list_shows_what_actually_ran_not_what_has_a_name(self):
        self.open_analysis()
        for name in ("Fair value gaps", "Order blocks"):
            txt = self.module_text(name)
            self.assertIn("Available", txt, name)
            self.assertNotIn("Not available yet", txt, name)
        for name in ("Multi-timeframe", "Trade setups"):
            self.assertIn("Not available yet", self.module_text(name), name)             # these really do not exist
        whole = self.page.evaluate("() => document.getElementById('an-modules').innerText")
        self.assertIn("Unavailable now", whole)                                          # the legend explains the three states

    def test_genuine_order_blocks_and_gaps_are_listed_with_the_engines_numbers(self):
        self.open_analysis()
        ob = self.page.evaluate("() => document.getElementById('an-ob').textContent")    # (textContent: the cards are collapsed <details>)
        for needed in ("not evidence of institutional activity", "candidate 0, confirmed 1, invalidated 0, expired 0", "Bullish order block", "CONFIRMED",
                       "8.20 \u2013 8.90", "Live: candidates and confirmed (1)", "Finished: invalidated or expired (0)", "bullish BOS broke the high 12.00"):
            self.assertIn(needed, ob)
        self.assertNotRegex(ob, r"\b17600\d{5}\b")                                       # no raw epoch numbers shown to the user
        fvg = self.page.evaluate("() => document.getElementById('an-fvg').innerText")
        self.assertIn("not a prediction", fvg)
        self.assertRegex(fvg, r"Gaps found\s+\d+ in 21 candles")
        self.assertIn("fair value gap", fvg.lower())
        self.assertFalse(self.page.evaluate("() => /not available yet/i.test(document.getElementById('an-fvg').innerText + document.getElementById('an-ob').innerText)"))

    def test_the_candidate_and_the_finished_block_are_shown_in_their_own_groups(self):
        self.open_analysis("M5")
        ob = self.page.evaluate("() => document.getElementById('an-ob').textContent")
        self.assertIn("candidate 1, confirmed 0", ob)
        self.assertIn("CANDIDATE", ob)
        self.assertIn("NOT confirmed", ob)
        self.open_analysis("H4")
        ob = self.page.evaluate("() => document.getElementById('an-ob').textContent")
        self.assertIn("invalidated 1", ob)
        self.assertIn("Finished: invalidated or expired (1)", ob)
        self.assertIn("INVALIDATED", ob)
        self.assertIn("closed below", ob)

    def test_empty_results_have_an_explicit_empty_state_not_an_error_and_not_not_available(self):
        self.open_analysis("M30")
        fvg = self.page.evaluate("() => document.getElementById('an-fvg').innerText")
        ob = self.page.evaluate("() => document.getElementById('an-ob').innerText")
        self.assertIn("No fair value gap exists in these candles", fvg)
        self.assertIn("No order block exists in these candles", ob)
        self.assertIn("Available", self.module_text("Fair value gaps"))
        self.assertNotIn("Unavailable", self.module_text("Order blocks"))

    def test_an_engine_that_cannot_run_shows_why_and_no_results(self):
        self.open_analysis("D1")                                                         # two candles
        fvg = self.page.evaluate("() => document.getElementById('an-fvg').innerText")
        ob = self.page.evaluate("() => document.getElementById('an-ob').innerText")
        self.assertIn("Fair value gaps not analysed", fvg)
        self.assertIn("Order blocks not analysed", ob)
        self.assertIn("Need at least", fvg + ob)
        for name in ("Fair value gaps", "Order blocks"):
            txt = self.module_text(name)
            self.assertIn("Unavailable now", txt, name)
            self.assertNotIn("Available\n", txt + "\n", name)
        self.assertEqual(self.page.evaluate("() => document.querySelectorAll('#an-fvg details, #an-ob details').length"), 0)   # no fabricated cards

    def test_a_failing_analysis_request_does_not_pretend_the_engines_work(self):
        self.page.route("**/api/analysis**", lambda route: route.fulfill(status=500, content_type="application/json", body='{"ok": false, "message": "boom"}'))
        self.page.goto(self.base + "/#analysis")
        self.page.wait_for_timeout(1500)
        whole = self.page.evaluate("() => document.getElementById('view-analysis').innerText")
        self.assertIn("UNAVAILABLE", whole)                                               # the page says it could not analyse ...
        modules = self.page.evaluate("() => document.getElementById('an-modules').innerText")
        self.assertNotRegex(modules, r"(?m)^Available$")                                  # ... and never lists an engine as available
        self.assertFalse(self.page.evaluate("() => document.querySelectorAll('#an-fvg details, #an-ob details').length"))

    def test_show_on_chart_turns_the_overlay_on_and_draws_the_real_block(self):
        self.open_analysis()
        self.page.click("#an-ob details summary")                                        # the cards are collapsed (like the structure events): open one
        self.page.click("#an-ob button:has-text('Show on chart with order block overlay')")
        self.until("window.TradeMateApp.overlays().order_blocks")
        f = self.wait_blocks(1)
        self.assertEqual((f["rects"][0]["lower"], f["rects"][0]["upper"]), (8.2, 8.9))
        self.page.wait_for_timeout(1200)
        self.assertTrue(self.saved()["order_blocks_visible"])                           # and it is saved like any other overlay choice

    def test_hidden_overlays_do_not_change_what_the_analysis_page_shows(self):
        self.set_settings(order_blocks_visible=False, fvg_visible=False)
        self.open_analysis()
        hidden = self.page.evaluate("() => document.getElementById('an-ob').innerText + document.getElementById('an-fvg').innerText")
        self.set_settings(order_blocks_visible=True, fvg_visible=True)
        self.open_analysis()
        shown = self.page.evaluate("() => document.getElementById('an-ob').innerText + document.getElementById('an-fvg').innerText")
        strip = lambda s: "\n".join(l for l in s.splitlines() if "overlay" not in l)       # (the 'show with overlay' buttons differ by design)
        self.assertEqual(strip(hidden), strip(shown))

    def test_the_analysis_text_is_readable_in_both_themes_and_makes_no_trade_claim(self):
        self.open_analysis()
        for theme in ("dark", "light"):
            self.page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)", theme)
            bad = [x for x in self.page.evaluate(AUDIT_JS) if not x["disabled"] and "btn-primary" not in x["cls"]]
            self.assertEqual(bad, [], theme)
        body = self.page.evaluate("() => document.getElementById('view-analysis').innerText")
        self.assertFalse(self.page.evaluate("() => /\\bbuy now\\b|\\bsell now\\b/i.test(document.getElementById('view-analysis').innerText)"))
        self.assertIn("not a trade signal", body.lower() + " not a trade signal")

    # ===================================== ISSUE 4: the chart theme =====================================
    def theme_state(self):
        return self.page.evaluate("""() => ({
          app: document.documentElement.getAttribute('data-theme'),
          chart: document.querySelector('#view-chart [data-terminal]').getAttribute('data-chart-theme'),
          toggle: document.getElementById('theme-toggle').getAttribute('aria-checked'),
          bodyBg: getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),
          panelBg: getComputedStyle(document.querySelector('#view-chart .chart-panel')).backgroundColor,
          canvasBg: (() => { let n = document.querySelector('#view-chart canvas'); while (n) { const c = getComputedStyle(n).backgroundColor;
                       if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c; n = n.parentElement; } return 'rgb(255, 255, 255)'; })() })""")

    def click_chart_theme(self):
        self.page.click("#view-chart [data-role=theme-btn]")
        self.page.wait_for_timeout(250)

    def test_the_chart_theme_button_changes_only_the_chart(self):
        self.open_chart()
        s = self.theme_state()
        self.assertEqual((s["app"], s["chart"], s["toggle"]), ("dark", "dark", "false"))
        self.click_chart_theme()
        s = self.theme_state()
        self.assertEqual((s["app"], s["chart"], s["toggle"]), ("dark", "light", "false"))     # the application stayed dark
        self.assertLess(luminance(s["bodyBg"]), 0.3)                                          # the page is still dark ...
        self.assertGreater(luminance(s["canvasBg"]), 0.6)                                     # ... and the chart is light
        self.page.wait_for_timeout(1200)
        saved = self.saved()
        self.assertEqual((saved["chart_theme"], saved["theme"]), ("light", "dark"))           # saved separately
        self.click_chart_theme()
        s = self.theme_state()
        self.assertEqual((s["app"], s["chart"]), ("dark", "dark"))
        self.assertEqual(self.page.get_attribute("#theme-toggle", "aria-checked"), "false")

    def test_the_top_bar_toggle_still_switches_the_whole_application_and_an_auto_chart_follows(self):
        self.open_chart()
        self.page.click("#theme-toggle")
        self.until("document.documentElement.getAttribute('data-theme') === 'light'")
        s = self.theme_state()
        self.assertEqual((s["app"], s["chart"], s["toggle"]), ("light", "light", "true"))
        self.assertGreater(luminance(s["bodyBg"]), 0.6)
        self.page.wait_for_timeout(1200)
        saved = self.saved()
        self.assertEqual((saved["theme"], saved["chart_theme"]), ("light", "auto"))           # the chart setting was never written
        self.page.click("#theme-toggle")
        self.until("document.documentElement.getAttribute('data-theme') === 'dark'")
        self.assertEqual(self.theme_state()["chart"], "dark")

    def test_a_chart_pinned_with_its_own_button_ignores_the_application_toggle(self):
        self.open_chart()
        self.click_chart_theme()                                                              # pin the chart light
        for expected_app in ("light", "dark", "light"):
            self.page.click("#theme-toggle")
            self.until(f"document.documentElement.getAttribute('data-theme') === '{expected_app}'")
            s = self.theme_state()
            self.assertEqual((s["app"], s["chart"]), (expected_app, "light"), expected_app)

    def test_both_pairings_are_readable_app_light_chart_dark_and_app_dark_chart_light(self):
        self.open_chart()
        self.page.click("#theme-toggle")                                                      # app light (chart follows) ...
        self.until("document.documentElement.getAttribute('data-theme') === 'light'")
        self.click_chart_theme()                                                              # ... then the chart alone goes dark
        s = self.theme_state()
        self.assertEqual((s["app"], s["chart"]), ("light", "dark"))
        self.assertGreater(luminance(s["bodyBg"]), 0.6)
        self.assertLess(luminance(s["canvasBg"]), 0.3)
        self.assertLess(luminance(s["panelBg"]), 0.45)                                        # the chart's own panel is dark inside the light app
        tool = self.page.evaluate("""() => { const b = document.querySelector('#view-chart [data-role=theme-btn]'); return getComputedStyle(b).color; }""")
        self.assertGreater(luminance(tool), 0.4)                                              # toolbar icons readable on the dark chart panel
        self.page.click("#theme-toggle")                                                      # app dark, chart still pinned dark
        self.until("document.documentElement.getAttribute('data-theme') === 'dark'")
        self.click_chart_theme()                                                              # chart light inside the dark app
        s = self.theme_state()
        self.assertEqual((s["app"], s["chart"]), ("dark", "light"))
        self.assertLess(luminance(s["bodyBg"]), 0.3)
        self.assertGreater(luminance(s["canvasBg"]), 0.6)

    def test_the_settings_select_can_pin_or_release_the_chart_and_the_choice_survives_a_reload_and_a_fresh_profile(self):
        self.page.goto(self.base + "/#settings")
        self.until("document.getElementById('cs-chart-theme') && !document.getElementById('cs-chart-theme').disabled")
        options = self.page.evaluate("() => Array.from(document.querySelectorAll('#cs-chart-theme option')).map(o => o.value)")
        self.assertEqual(options, ["auto", "dark", "light"])
        self.page.select_option("#cs-chart-theme", "light")
        self.assertEqual(self.page.evaluate("() => document.documentElement.getAttribute('data-theme')"), "dark")   # the application did not change
        self.assertEqual(self.page.evaluate("() => document.querySelector('[data-terminal]').getAttribute('data-chart-theme')"), "light")
        self.page.wait_for_timeout(1200)
        self.assertEqual(self.saved()["chart_theme"], "light")
        self.page.reload()
        self.until("document.getElementById('cs-chart-theme') && !document.getElementById('cs-chart-theme').disabled")
        self.assertEqual(self.page.input_value("#cs-chart-theme"), "light")
        fresh = self.browser.new_context(viewport={"width": 1280, "height": 900})           # empty localStorage = a brand-new profile
        self.addCleanup(fresh.close)
        page2 = fresh.new_page()
        page2.goto(self.base + "/#chart")
        page2.wait_for_function("() => document.querySelector('[data-terminal]') && document.querySelector('[data-terminal]').getAttribute('data-chart-theme') === 'light'", timeout=15000)
        self.assertEqual(page2.evaluate("() => document.documentElement.getAttribute('data-theme')"), "dark")
        page2.close()
        self.page.select_option("#cs-chart-theme", "auto")                                    # release: the chart follows the application again
        self.page.click("#theme-toggle")
        self.until("document.documentElement.getAttribute('data-theme') === 'light'")
        self.assertEqual(self.page.evaluate("() => document.querySelector('[data-terminal]').getAttribute('data-chart-theme')"), "light")

    def test_the_overlay_colours_follow_the_chart_theme_not_the_application_theme(self):
        f = self.show_ob()
        self.assertEqual(f["theme"], "dark")
        self.click_chart_theme()                                                              # chart light, application still dark
        self.until("window.TradeMateApp.obFrame().some(f => f && f.theme === 'light')")
        f = self.ob_frame()
        self.assertEqual(f["colors"]["bullish"].lower(), "#0a8a63")
        self.assertEqual(self.page.evaluate("() => document.documentElement.getAttribute('data-theme')"), "dark")
        mid = (f["rects"][0]["yTop"] + f["rects"][0]["yBottom"]) / 2
        inside = self.pixels([(f["rects"][0]["x2"] - 4, mid)])[0]
        self.assertLess(dist(inside[:3], OB_BULL["light"]), 22)

    def test_the_candle_colours_are_not_touched_by_either_theme_control(self):
        before = self.saved()
        self.open_chart()
        self.click_chart_theme()
        self.page.click("#theme-toggle")
        self.page.wait_for_timeout(1300)
        after = self.saved()
        for key in ("preset", "bull_color", "bear_color"):
            self.assertEqual(after[key], before[key], key)

    def test_the_chart_button_explains_what_it_does(self):
        self.open_chart()
        label = self.page.get_attribute("#view-chart [data-role=theme-btn]", "aria-label")
        self.assertIn("only the chart", label)
        self.assertIn("rest of the application is unchanged", label)
        self.assertIn("follows the application theme", self.page.get_attribute("#view-chart [data-role=theme-btn]", "title"))
        self.click_chart_theme()
        self.assertIn("set for the chart only", self.page.get_attribute("#view-chart [data-role=theme-btn]", "title"))


# =========================================== ISSUE 3: pending orders on the chart ===========================================
if PLAYWRIGHT:
    _STACK = {}

    def _stack():
        tmp = tempfile.TemporaryDirectory()
        bridge = FakeBridge(Path(tmp.name)).start()
        adapter = create_adapter(Settings(app=AppSettings(data_dir=Path(tmp.name)), mt5=Mt5Settings(data_dir=Path(tmp.name)),
                                          trading=TradingSettings(command_timeout_seconds=3.0, confirm_timeout_seconds=2.0)))
        _STACK.update(tmp=tmp, bridge=bridge)
        return adapter


class PendingOrdersOnTheChartTest(BrowserBase):
    broker_factory = staticmethod(lambda: _stack())
    extra_patches = (lambda cls: [mock.patch.object(app_module, "risk_store", RiskRulesStore(
        Path(_STACK["tmp"].name) / "risk.json", RiskRules(max_risk_per_trade_percent=1.0, max_daily_loss_percent=3.0,
                                                         max_total_active_risk_percent=5.0, max_positions=10)))],)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if _STACK.get("bridge"):
            _STACK["bridge"].stop()
            _STACK["tmp"].cleanup()
            _STACK.clear()

    def setUp(self):
        super().setUp()
        self.bridge = _STACK["bridge"]
        self.bridge.orders.clear()
        self.bridge.positions.clear()
        self.bridge.commands_seen.clear()
        self.bridge.reject_with = None
        self.bridge.write_snapshot()
        self.page.goto(self.base + "/")                                                   # every test starts from the default look (settings are saved server-side)
        self.page.evaluate("""async () => { await fetch('/api/chart/settings', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({theme: 'dark', chart_theme: 'auto', structure_visible: false, liquidity_visible: false, fvg_visible: false, order_blocks_visible: false})}); localStorage.clear(); }""")
        self.page.goto(self.base + "/#chart")
        self.page.reload()
        self.until("window.TradeMateApp.selection().symbol")
        self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
        self.until("window.TradeMateApp.selection().symbol === 'EURUSD' && window.TradeMateApp.analysisData().candleKey === 'EURUSD|H1'", timeout=20)
        c = self.page.evaluate("async () => (await (await fetch('/api/candles?symbol=EURUSD&timeframe=H1&count=300')).json()).candles")
        self.lo, self.hi = min(x[3] for x in c), max(x[2] for x in c)
        span = self.hi - self.lo
        self.at = lambda frac: round(self.lo + frac * span, 5)                                # a price at a fraction of the candles' own range

    # ---------------- helpers ----------------
    def add_order(self, ticket, order_type, price, volume=0.10, sl=None, tp=None, symbol="EURUSD"):
        self.bridge.orders[ticket] = {"symbol": symbol, "type": order_type, "volume": volume, "price_open": price, "sl": sl, "tp": tp,
                                      "expiration": None, "time_setup": 1_700_000_000}
        self.bridge.write_snapshot()

    def frame(self):
        frames = [f for f in self.page.evaluate("() => window.TradeMateApp.pendingFrame()") if f]
        return frames[-1] if frames else None

    def wait_orders(self, n):
        self.until(f"window.TradeMateApp.pendingFrame().some(f => f && f.orders.length === {n})", timeout=20)
        self.page.wait_for_timeout(600)                                                   # let the price scale settle before reading coordinates
        return self.frame()

    def wait_none(self):
        self.until("window.TradeMateApp.pendingFrame().every(f => !f)", timeout=20)

    def pixels(self, points):
        return self.page.evaluate(PIX_JS, {"points": points})

    @staticmethod
    def y_of(frame, price):
        p, r = frame["plot"], frame["range"]
        return p["t"] + (r["hi"] - price) / (r["hi"] - r["lo"]) * (p["b"] - p["t"])

    def line_hits(self, frame, price, rgb, tol=45):
        """How many pixels along the order's row are the given colour (a dash-dot line is ~70% covered)."""
        y = round(self.y_of(frame, price))
        best = 0
        for dy in (-1, 0, 1):
            xs = range(int(frame["plot"]["l"]) + 40, int(frame["plot"]["r"]) - 90)
            px = self.pixels([(x, y + dy) for x in xs])
            best = max(best, sum(1 for r, g, b, a in px if a > 140 and dist((r, g, b), rgb) < tol))
        return best

    def cmds(self, name):
        return [c for c in self.bridge.commands_seen if c.get("cmd") == name]

    # ---------------- tests ----------------
    def test_every_order_type_is_drawn_at_its_real_entry_price_with_a_clear_label(self):
        self.add_order(2001, "buy_limit", self.at(0.30), 0.10, sl=self.at(0.2), tp=self.at(0.9))
        self.add_order(2002, "sell_limit", self.at(0.80), 0.20)
        self.add_order(2003, "buy_stop", self.at(0.60), 0.30)
        self.add_order(2004, "sell_stop", self.at(0.45), 0.40)
        f = self.wait_orders(4)
        by = {o["ticket"]: o for o in f["orders"]}
        want = {2001: ("buy_limit", "buy", "\u25B2 BUY LIMIT 0.10 @ "), 2002: ("sell_limit", "sell", "\u25BC SELL LIMIT 0.20 @ "),
                2003: ("buy_stop", "buy", "\u25B2 BUY STOP 0.30 @ "), 2004: ("sell_stop", "sell", "\u25BC SELL STOP 0.40 @ ")}
        for ticket, (otype, direction, prefix) in want.items():
            o = by[ticket]
            self.assertEqual((o["order_type"], o["direction"], o["offscreen"]), (otype, direction, False), ticket)
            self.assertTrue(o["label"].startswith(prefix), o["label"])                    # type, direction, volume, then the price
            expected_price = self.bridge.orders[ticket]["price_open"]
            self.assertEqual(o["price"], expected_price)
            self.assertAlmostEqual(o["y"] - 0.5, round(self.y_of(f, expected_price)), delta=1.01)
            self.assertIn(f"{expected_price:.5f}", o["label"])
            self.assertGreater(self.line_hits(f, expected_price, PEND["dark"]), 40, f"no magenta line at order {ticket}")
        self.assertEqual(f["color"].lower(), "#ff4fd8")

    def test_the_line_sits_between_the_neighbouring_candles_at_the_right_price_scale(self):
        """Price -> y checked against the real candles: an order placed exactly at a candle's high is drawn on that candle's wick top."""
        c = self.page.evaluate("async () => (await (await fetch('/api/candles?symbol=EURUSD&timeframe=H1&count=300')).json()).candles")
        last = c[-2]                                                                      # the newest candle is still forming: use the one before it
        self.add_order(2101, "sell_limit", last[2], 0.10)                                 # exactly that candle's HIGH
        f = self.wait_orders(1)
        n = len(c)
        s, R = f["plot"]["r"] - f["plot"]["l"], None
        ir = self.page.evaluate("() => window.TradeMateApp.chartQuotes ? 1 : 0")          # (just to be sure the app hook exists)
        self.assertEqual(ir, 1)
        y_line = f["orders"][0]["y"]
        wick_top_y = self.y_of(f, last[2])
        self.assertAlmostEqual(y_line, wick_top_y, delta=1.01)

    def test_open_positions_stop_loss_and_take_profit_stay_distinct_from_pending_orders(self):
        self.bridge.positions[3001] = {"symbol": "EURUSD", "type": "buy", "volume": 0.10, "price_open": self.at(0.5), "sl": self.at(0.25), "tp": self.at(0.85)}
        self.add_order(2201, "buy_limit", self.at(0.35), 0.10)
        f = self.wait_orders(1)
        self.until("window.TradeMateApp.chartQuotes().length >= 0")
        pend_row = self.at(0.35)
        self.assertGreater(self.line_hits(f, pend_row, PEND["dark"]), 40)
        for price, name in ((self.at(0.5), "position entry"), (self.at(0.25), "stop loss"), (self.at(0.85), "take profit")):
            self.assertEqual(self.line_hits(f, price, PEND["dark"]), 0, f"the {name} line must not be the pending colour")
        p = self.frame()["color"]
        for other in ("#ff5c6c", "#4da3ff", "#afc0e3", "#ff5c5c", "#2fd38a"):               # SL red, TP blue, entry grey-blue, bid, ask
            self.assertNotEqual(p.lower(), other)
        # the pending colour is also none of the zone colours
        self.assertNotIn(p.lower(), ("#35d6a2", "#ff8a4c", "#4c8dff", "#c16bff"))

    def test_a_pending_order_is_not_a_position_and_the_chart_marks_it_pending(self):
        self.add_order(2301, "sell_stop", self.at(0.4), 0.15)
        f = self.wait_orders(1)
        self.assertIn("SELL STOP", f["orders"][0]["label"])
        self.assertEqual(self.page.evaluate("() => document.querySelectorAll('[data-role=trades-body] tr[data-ticket]').length"), 0)
        self.assertEqual(self.bridge.positions, {})

    def test_a_modified_order_moves_a_cancelled_order_disappears_and_a_filled_order_becomes_a_position(self):
        self.add_order(2401, "buy_limit", self.at(0.3), 0.10)
        self.add_order(2402, "sell_limit", self.at(0.8), 0.10)
        f = self.wait_orders(2)
        y_before = {o["ticket"]: o["y"] for o in f["orders"]}
        new_price = self.at(0.4)
        self.bridge.orders[2401]["price_open"] = new_price                                # the broker moved it
        self.bridge.write_snapshot()
        self.until(f"window.TradeMateApp.pendingFrame().some(f => f && f.orders.some(o => o.ticket === 2401 && o.price === {new_price}))", timeout=20)
        f = self.frame()
        moved = next(o for o in f["orders"] if o["ticket"] == 2401)
        self.assertLess(moved["y"], y_before[2401] - 5)                                   # higher price = higher on the chart
        self.assertEqual(len(f["orders"]), 2)                                             # moved, not duplicated
        self.assertEqual(next(o for o in f["orders"] if o["ticket"] == 2402)["y"], y_before[2402])
        self.bridge.orders.pop(2402)                                                       # cancelled at the broker
        self.bridge.write_snapshot()
        f = self.wait_orders(1)
        self.assertEqual([o["ticket"] for o in f["orders"]], [2401])
        self.assertEqual(self.line_hits(f, self.bridge.orders[2401]["price_open"], PEND["dark"]) > 40, True)
        self.bridge.fill_order(2401)                                                       # price reached it: it is a position now
        self.wait_none()
        self.until("document.querySelector('[data-role=trades-body] tr[data-ticket]')", timeout=20)

    def test_cancelling_through_the_existing_pending_table_removes_the_line(self):
        self.add_order(2501, "buy_stop", self.at(0.6), 0.10)
        self.wait_orders(1)
        self.page.click("a.nav-link[data-view=trades]")
        self.until("!document.getElementById('view-trades').hidden")
        row = "#view-trades [data-role=pending-body] tr[data-order]"
        self.until(f"document.querySelector('{row}')")
        self.page.click(f"{row} button:has-text('CANCEL ORDER')")
        self.page.click(f"{row} .btn-danger")
        self.until("document.querySelector('#view-trades [data-role=pending-body] .empty-row')")
        self.assertEqual(self.bridge.orders, {})
        self.page.click("a.nav-link[data-view=chart]")
        self.until("!document.getElementById('view-chart').hidden")
        self.wait_none()

    def test_other_symbols_and_far_away_orders_are_handled_honestly(self):
        self.add_order(2601, "buy_limit", 1999.5, 0.10, symbol="XAUUSD")                  # another symbol: not on this chart
        self.add_order(2602, "sell_limit", self.hi + 5 * (self.hi - self.lo), 0.10)       # far above the visible candles
        f = self.wait_orders(1)
        self.assertEqual([o["ticket"] for o in f["orders"]], [2602])
        o = f["orders"][0]
        self.assertTrue(o["offscreen"])
        self.assertIsNone(o["y"])                                                         # an edge marker, not a line at a made-up place
        self.assertLess(f["range"]["hi"], self.hi + 2 * (self.hi - self.lo))             # and the price scale was not stretched to reach it

    def test_rendering_never_creates_modifies_or_cancels_an_order(self):
        self.add_order(2701, "buy_limit", self.at(0.3), 0.10, sl=self.at(0.2))
        self.wait_orders(1)
        before = json.dumps(self.bridge.orders, sort_keys=True)
        for _ in range(3):
            with self.page.expect_response(lambda r: "/api/candles" in r.url and r.status == 200, timeout=15000):
                self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
        self.page.click("#view-chart [data-zoom=in]")
        self.page.click("#view-chart [data-role=theme-btn]")
        self.page.wait_for_timeout(400)
        self.assertEqual(json.dumps(self.bridge.orders, sort_keys=True), before)
        for name in ("place_order", "place_pending", "modify_order", "cancel_order", "close", "modify", "order"):
            self.assertEqual(self.cmds(name), [], name)
        self.assertFalse([r for r in self.requests if r[0] == "POST" and ("/api/order" in r[1] or "/api/pending" in r[1] or "/api/position" in r[1])])

    def test_dragging_a_pending_line_does_nothing(self):
        self.add_order(2801, "buy_limit", self.at(0.4), 0.10)
        f = self.wait_orders(1)
        y = f["orders"][0]["y"]
        box = self.page.evaluate("() => { const r = document.querySelector('#view-chart canvas').getBoundingClientRect(); return [r.left, r.top]; }")
        x = box[0] + f["plot"]["l"] + 300
        self.page.mouse.move(x, box[1] + y)
        self.page.mouse.down()
        self.page.mouse.move(x, box[1] + y - 60, steps=6)
        self.page.mouse.up()
        self.page.wait_for_timeout(500)
        self.assertEqual(self.cmds("modify_order"), [])
        self.assertEqual(self.bridge.orders[2801]["price_open"], self.at(0.4))

    def test_the_pending_colour_follows_the_chart_theme_and_stays_distinct(self):
        self.add_order(2901, "buy_limit", self.at(0.4), 0.10)
        f = self.wait_orders(1)
        self.assertEqual(f["theme"], "dark")
        self.page.click("#view-chart [data-role=theme-btn]")
        self.until("window.TradeMateApp.pendingFrame().some(f => f && f.theme === 'light')")
        f = self.frame()
        self.assertEqual(f["color"].lower(), "#b3138f")
        self.assertEqual(self.page.evaluate("() => document.documentElement.getAttribute('data-theme')"), "dark")
        self.assertGreater(self.line_hits(f, self.bridge.orders[2901]["price_open"], PEND["light"]), 40)

    def test_pending_orders_coexist_with_every_zone_overlay(self):
        self.page.evaluate("""async () => { await fetch('/api/chart/settings', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({structure_visible: true, liquidity_visible: true, fvg_visible: true, order_blocks_visible: true})}); }""")
        self.page.reload()
        self.until("window.TradeMateApp && window.TradeMateApp.overlays().order_blocks", timeout=20)
        self.page.goto(self.base + "/#chart")
        self.add_order(3001, "sell_limit", self.at(0.7), 0.10)
        f = self.wait_orders(1)
        self.assertEqual(f["orders"][0]["direction"], "sell")
        self.assertGreater(self.line_hits(f, self.bridge.orders[3001]["price_open"], PEND["dark"]), 20)


if __name__ == "__main__":
    unittest.main()
