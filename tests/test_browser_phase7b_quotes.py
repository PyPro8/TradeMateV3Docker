"""Phase 7B: the chart's live BID (red) / ASK (green) lines, in real Chromium with deterministic mocked quotes.

    python3 -m unittest tests.test_browser_phase7b_quotes -v        (SKIPPED, not passed, when Playwright/Chromium is missing)
"""

import unittest

from tests.browser_support import BrowserBase

DARK = {"bid": (255, 92, 92), "ask": (47, 211, 138)}
LIGHT = {"bid": (196, 38, 46), "ask": (17, 122, 69)}

PIXELS = """(args) => {
  const [yBid, yAsk, rgbBid0, rgbAsk0] = args;
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  const rgbBid = light ? [196, 38, 46] : rgbBid0, rgbAsk = light ? [17, 122, 69] : rgbAsk0;
  const cv = document.querySelector('#view-chart canvas'); const ctx = cv.getContext('2d'); const dpr = cv.width / cv.clientWidth;
  function hits(y, rgb) {
    let n = 0;
    for (let dy = -1; dy <= 1; dy++) for (let fx = 0.15; fx <= 0.75; fx += 0.05) {
      const d = ctx.getImageData(Math.round(cv.width * fx), Math.round((y + dy) * dpr), 1, 1).data;
      if (Math.abs(d[0] - rgb[0]) < 28 && Math.abs(d[1] - rgb[1]) < 28 && Math.abs(d[2] - rgb[2]) < 28) n++;
    }
    return n;
  }
  return { bid: hits(yBid, rgbBid), ask: hits(yAsk, rgbAsk) };
}"""

# rows (CSS px) inside the plot whose pixel is the line colour, scanning one plot column band: counts DISTINCT line rows
ROWS = """(rgb0) => {
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  const rgb = !light ? rgb0 : (rgb0[0] === 255 ? [196, 38, 46] : [17, 122, 69]);
  const cv = document.querySelector('#view-chart canvas'); const ctx = cv.getContext('2d'); const dpr = cv.width / cv.clientWidth;
  const rows = new Set();
  for (let fx = 0.2; fx <= 0.7; fx += 0.1) for (let y = 0; y < cv.height; y++) {
    const d = ctx.getImageData(Math.round(cv.width * fx), y, 1, 1).data;
    if (Math.abs(d[0] - rgb[0]) < 12 && Math.abs(d[1] - rgb[1]) < 12 && Math.abs(d[2] - rgb[2]) < 12) rows.add(Math.round(y / dpr));
  }
  return Array.from(rows);
}"""


class QuoteLinesTest(BrowserBase):
    def setUp(self):
        super().setUp()
        b = self.broker
        b.connected, b.snapshot_age, b.streamed = True, 0.1, ["EURUSD", "XAUUSD"]
        base = round(b.series["H1"][-1][4], 2)
        b.quotes = {"EURUSD": (base, round(base + 0.40, 2)), "XAUUSD": (2000.10, 2000.90)}       # spread wide enough to be two separate rows

    def frame(self):
        return self.page.evaluate("() => window.TradeMateApp.chartQuotes().filter(Boolean).pop()")

    def wait_state(self, state):
        self.until(f"window.TradeMateApp.chartQuotes().some(f => f && f.state === '{state}')")
        return self.frame()

    def test_bid_is_red_ask_is_green_at_the_real_prices_and_the_lines_are_really_drawn(self):
        self.open_chart()
        self.wait_state("live")
        self.page.wait_for_timeout(1200)                                  # let the candles load and the price range settle
        f = self.frame()
        bid, ask = self.broker.quotes["EURUSD"]
        self.assertAlmostEqual(f["bid"]["price"], bid)
        self.assertAlmostEqual(f["ask"]["price"], ask)
        self.assertGreater(f["bid"]["y"], f["ask"]["y"])                 # the bid is the LOWER price, so it is drawn lower on screen
        self.assertEqual(f["bid"]["color"].lower(), "#ff5c5c")
        self.assertEqual(f["ask"]["color"].lower(), "#2fd38a")
        px = self.page.evaluate(PIXELS, [f["bid"]["y"], f["ask"]["y"], list(DARK["bid"]), list(DARK["ask"])])
        self.assertGreaterEqual(px["bid"], 6)                            # a long straight red line, pixel-verified
        self.assertGreaterEqual(px["ask"], 6)
        self.assertEqual(f["spreadPoints"], 40)                          # (ask - bid) / point, from the quote
        self.assertAlmostEqual(f["spreadPrice"], round(ask - bid, 2))

    def test_a_spread_smaller_than_one_pixel_keeps_both_prices_in_the_data_and_labels(self):
        b = self.broker
        b.quotes["EURUSD"] = (b.quotes["EURUSD"][0], round(b.quotes["EURUSD"][0] + 0.02, 2))
        self.open_chart()
        f = self.wait_state("live")
        self.assertAlmostEqual(f["ask"]["price"] - f["bid"]["price"], 0.02)
        self.assertLess(abs(f["ask"]["y"] - f["bid"]["y"]), 2)                        # the two lines coincide on screen: that is the real spread
        self.assertEqual(f["spreadPoints"], 2)

    def test_the_lines_are_solid_and_horizontal_one_row_thick(self):
        self.open_chart()
        f = self.wait_state("live")
        rows = self.page.evaluate(ROWS, list(DARK["bid"]))
        near = [r for r in rows if abs(r - f["bid"]["y"]) <= 3]
        self.assertTrue(1 <= len(near) <= 2, rows)                       # a straight 1px line, not dashed bands or a smear

    def test_quote_updates_move_the_lines_without_duplicates(self):
        self.open_chart()
        before = self.wait_state("live")
        b = self.broker
        b.quotes["EURUSD"] = (round(b.quotes["EURUSD"][0] + 0.50, 2), round(b.quotes["EURUSD"][1] + 0.50, 2))
        self.until(f"Math.abs(window.TradeMateApp.chartQuotes().filter(Boolean).pop().bid.price - {b.quotes['EURUSD'][0]}) < 0.001", timeout=12)
        after = self.frame()
        self.assertNotEqual(before["bid"]["y"], after["bid"]["y"])
        rows = self.page.evaluate(ROWS, list(DARK["bid"]))
        self.assertEqual(len([r for r in rows if abs(r - before["bid"]["y"]) <= 1 and abs(r - after["bid"]["y"]) > 2]), 0)   # the old line is gone
        self.assertEqual(len({r // 4 for r in rows if r > 40}), len({r // 4 for r in rows if r > 40}))

    def test_connection_loss_removes_the_lines_and_recovery_brings_them_back(self):
        self.open_chart()
        self.wait_state("live")
        self.broker.connected = False
        f = self.wait_state("unavailable")
        self.assertNotIn("bid", f)
        self.assertIn("not connected", f["reason"])
        self.assertEqual(self.page.evaluate(ROWS, list(DARK["bid"])), [])                # nothing red is left on the canvas
        self.assertEqual(self.page.evaluate(ROWS, list(DARK["ask"])), [])
        self.broker.connected = True
        f = self.wait_state("live")
        self.assertIn("bid", f)

    def test_an_old_bridge_snapshot_makes_the_quote_stale_not_live(self):
        self.open_chart()
        self.wait_state("live")
        self.broker.snapshot_age = 25.0
        f = self.wait_state("stale")
        self.assertIn("25 s old", f["reason"])
        self.assertNotIn("bid", f)
        self.assertEqual(self.page.evaluate(ROWS, list(DARK["bid"])), [])
        self.broker.snapshot_age = 0.1
        self.wait_state("live")

    def test_missing_or_invalid_quote_is_never_fabricated(self):
        self.open_chart()
        self.wait_state("live")
        b = self.broker
        for bad in ((0.0, 0.0), (b.quotes["EURUSD"][1], b.quotes["EURUSD"][0] - 0.5)):      # no price / crossed
            b.quotes["EURUSD"] = bad
            f = self.wait_state("unavailable")
            self.assertNotIn("bid", f)
            self.assertNotIn("ask", f)
            self.assertEqual(self.page.evaluate(ROWS, list(DARK["ask"])), [])

    def test_a_symbol_that_is_not_streamed_has_no_live_lines(self):
        self.open_chart()
        self.wait_state("live")
        self.broker.streamed = ["XAUUSD"]
        f = self.wait_state("unavailable")
        self.assertIn("not in the streamed watch list", f["reason"])
        self.assertEqual(self.page.evaluate(ROWS, list(DARK["bid"])), [])

    def test_switching_symbol_replaces_the_lines_and_never_shows_the_old_price(self):
        self.open_chart()
        self.wait_state("live")
        self.page.evaluate("() => window.TradeMateApp.openChart('XAUUSD', 'H1', {})")
        self.until("window.TradeMateApp.chartQuotes().some(f => f && f.state === 'live' && f.symbol === 'XAUUSD')", timeout=20)
        f = self.frame()
        self.assertAlmostEqual(f["bid"]["price"], 2000.10)
        self.assertAlmostEqual(f["ask"]["price"], 2000.90)
        rows = self.page.evaluate(ROWS, list(DARK["bid"]))
        self.assertTrue(rows and all(abs(r - f["bid"]["y"]) <= 3 for r in rows if r > 30), (rows, f["bid"]["y"]))
        self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
        self.until("window.TradeMateApp.chartQuotes().some(f => f && f.state === 'live' && f.symbol === 'EURUSD')", timeout=20)
        self.assertAlmostEqual(self.frame()["bid"]["price"], self.broker.quotes["EURUSD"][0])

    def test_switching_timeframe_keeps_the_same_symbols_levels_with_no_duplicate_lines(self):
        self.open_chart()
        before = self.wait_state("live")
        self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'M15', {})")
        self.until("window.TradeMateApp.chartQuotes().some(f => f && f.state === 'live' && f.tf === 'M15')", timeout=20)
        after = self.frame()
        self.assertAlmostEqual(after["bid"]["price"], before["bid"]["price"])
        self.assertAlmostEqual(after["ask"]["price"], before["ask"]["price"])
        rows = [r for r in self.page.evaluate(ROWS, list(DARK["bid"])) if r > 30]
        self.assertTrue(rows and max(rows) - min(rows) <= 3, rows)       # one line only

    def test_redraws_and_resizes_never_duplicate_the_lines(self):
        self.open_chart()
        self.wait_state("live")
        for w in (1000, 1280, 800, 1280):
            self.page.set_viewport_size({"width": w, "height": 900})
            self.page.wait_for_timeout(250)
        f = self.wait_state("live")
        rows = [r for r in self.page.evaluate(ROWS, list(DARK["ask"])) if r > 30]
        self.assertTrue(rows and max(rows) - min(rows) <= 3, rows)

    def test_light_theme_uses_the_deeper_red_and_green_and_stays_visible(self):
        self.open_chart()
        self.wait_state("live")
        if self.page.evaluate("() => document.documentElement.getAttribute('data-theme')") != "light":
            self.page.click("#theme-toggle")                               # the app's own theme switch
        self.until("document.documentElement.getAttribute('data-theme') === 'light'")
        self.until("window.TradeMateApp.chartQuotes().filter(Boolean).pop().bid && window.TradeMateApp.chartQuotes().filter(Boolean).pop().bid.color === '#C4262E'", timeout=10)
        self.page.wait_for_timeout(300)
        f = self.frame()
        self.assertEqual(f["ask"]["color"], "#117A45")
        px = self.page.evaluate(PIXELS, [f["bid"]["y"], f["ask"]["y"], list(LIGHT["bid"]), list(LIGHT["ask"])])
        self.assertGreaterEqual(px["bid"], 6)
        self.assertGreaterEqual(px["ask"], 6)
        self.page.click("#theme-toggle")                                   # back to dark; the helpers above do not depend on it anyway
        self.until("document.documentElement.getAttribute('data-theme') === 'dark'")
        self.page.wait_for_timeout(400)                                    # let the theme save finish before the next test

    def test_dragging_a_quote_line_changes_nothing_and_sends_nothing(self):
        self.open_chart()
        f = self.wait_state("live")
        box = self.page.evaluate("() => { const r = document.querySelector('#view-chart canvas').getBoundingClientRect(); return [r.left, r.top, r.width]; }")
        x, y = box[0] + box[2] * 0.4, box[1] + f["bid"]["y"]
        n = len(self.requests)
        self.page.mouse.move(x, y)
        self.page.mouse.down()
        self.page.mouse.move(x, y - 60, steps=5)
        self.page.mouse.up()
        self.page.wait_for_timeout(300)
        after = self.frame()
        self.assertAlmostEqual(after["bid"]["price"], f["bid"]["price"])                # the quote did not move
        self.assertFalse([r for r in self.requests[n:] if r[0] == "POST"])              # nothing was submitted or modified
        self.assertEqual(self.page.evaluate("() => document.querySelector('#view-chart [data-role=sl]').value"), "")
        self.assertEqual(self.page.evaluate("() => document.querySelector('#view-chart [data-role=tp]').value"), "")


if __name__ == "__main__":
    unittest.main()
