"""Task 2 in a real browser (headless Chromium), against a STUB / FAKE broker. No real MT5, no real order, no live account.

    python3 -m unittest tests.test_browser_task2 -v        (SKIPPED, not passed, when Playwright/Chromium is missing)
"""

import unittest

import tests.test_browser_phase7c_repair as repair
from tests.browser_support import BrowserBase, PLAYWRIGHT

FAKE_ORDER_COMMANDS = {"open", "open_pending", "modify_pending", "cancel_pending", "close", "modify"}


class ChartPageTest(BrowserBase):
    """Objectives B, C, D on the chart page (stub broker: it can only read)."""

    def setUp(self):
        super().setUp()
        self.open_chart()

    def theme(self):
        return self.page.evaluate("() => document.documentElement.getAttribute('data-theme')")

    # ---- C ----
    def test_the_oversized_buy_and_sell_buttons_are_gone_and_the_order_controls_remain(self):
        for role in ("quick-buy", "quick-sell"):
            self.assertEqual(self.page.locator(f"[data-role='{role}']").count(), 0)
        self.assertEqual(self.page.locator("#view-chart .trade-btn").count(), 0)
        for role in ("panel-buy", "panel-sell", "lot"):
            self.assertGreaterEqual(self.page.locator(f"#view-chart [data-role='{role}'] >> visible=true").count(), 1, role)

    def test_the_trade_panel_buy_still_reaches_the_review_step_and_cancel_sends_nothing(self):
        self.until("window.TradeMateApp.selection().symbol")
        self.until("!document.querySelector('#view-chart [data-role=panel-buy]').disabled", timeout=20)
        self.page.fill("#view-chart [data-role=lot]", "0.01")
        self.page.click("#view-chart [data-role=panel-buy]")
        self.page.wait_for_timeout(800)
        sent = [r for r in self.requests if r[0] == "POST" and ("/api/order" in r[1] or "/api/position" in r[1])]
        self.assertEqual(sent, [], "opening the review / refusal step must not send an order")

    # ---- B ----
    def test_default_candles_keep_the_red_and_black_palette(self):
        self.until("window.TradeMateApp.candleStyle().preset")
        self.assertEqual(self.page.evaluate("() => window.TradeMateApp.candleStyle()"),
                         {"preset": "red_black", "bull": "#E53935", "bear": "#000000"})
        pix = self.page.evaluate("""() => { const cv = document.querySelector('#view-chart canvas'), ctx = cv.getContext('2d');
            const d = ctx.getImageData(0, 0, cv.width, cv.height).data; let red = 0, teal = 0;
            for (let i = 0; i < d.length; i += 4) { const r = d[i], g = d[i + 1], b = d[i + 2];
              if (Math.abs(r - 229) < 30 && Math.abs(g - 57) < 40 && Math.abs(b - 53) < 40) red++;
              if (Math.abs(r - 38) < 25 && Math.abs(g - 166) < 25 && Math.abs(b - 154) < 25) teal++;
            }
            return { red, teal }; }""")
        self.assertGreater(pix["red"], 50, "bullish candles must keep the Red palette color")
        self.assertEqual(pix["teal"], 0, "the green default must be gone")

    def test_settings_page_shows_the_existing_default_and_all_choices(self):
        self.page.goto(self.base + "/#settings")
        self.until("document.querySelector('#cs-preset')")
        self.until("document.querySelector('#cs-bull-hex').value.toUpperCase() === '#E53935'")
        self.assertEqual(self.page.input_value("#cs-bear-hex").upper(), "#000000")
        self.assertEqual(self.page.input_value("#cs-preset"), "red_black")
        self.assertEqual(self.page.locator(".cs-swatch").count(), 12)
        self.assertEqual(self.page.locator("#cs-preset option[value='arctic_rose']").count(), 1)

    # ---- D ----
    def test_the_chart_theme_button_changes_only_the_chart_and_survives_reload_and_navigation(self):
        app_theme = self.theme()
        self.until("window.TradeMateApp.chartQuotes().some(f => f)")
        before = self.page.evaluate("() => window.TradeMateApp.chartQuotes().filter(f => f)[0].theme")
        self.page.click("#view-chart [data-role=theme-btn]")
        self.until(f"window.TradeMateApp.chartQuotes().filter(f => f)[0].theme !== '{before}'")
        self.assertEqual(self.theme(), app_theme, "the application theme must not change")
        chart_theme = self.page.evaluate("() => window.TradeMateApp.chartQuotes().filter(f => f)[0].theme")
        self.page.goto(self.base + "/#dashboard")
        self.page.goto(self.base + "/#chart")
        self.page.reload()
        self.until("window.TradeMateApp.chartQuotes().some(f => f)")
        self.assertEqual(self.theme(), app_theme)
        self.until(f"window.TradeMateApp.chartQuotes().filter(f => f)[0].theme === '{chart_theme}'")

    def test_the_global_toggle_still_changes_the_application_theme(self):
        start = self.theme()
        self.page.click("#theme-toggle")
        self.until(f"document.documentElement.getAttribute('data-theme') !== '{start}'")


class ChartZonesTest(BrowserBase):
    """Objective E: zones on the chart are the engine's zones at the engine's prices, and stale / unavailable data says so."""

    def setUp(self):
        super().setUp()
        self.open_chart()
        self.until("window.TradeMateApp.analysisData().candleKey === 'EURUSD|H1'", timeout=20)

    def engine(self):
        return self.page.evaluate("""async () => (await (await fetch('/api/candles?symbol=EURUSD&timeframe=H1&count=300&fvg=1&order_blocks=1&structure=1')).json())""")

    def test_drawn_fvg_and_order_block_zones_are_at_the_engines_prices_for_this_symbol_and_timeframe(self):
        self.page.evaluate("async () => { await fetch('/api/chart/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({fvg_visible: true, order_blocks_visible: true})}); }")
        self.page.reload()
        self.until("window.TradeMateApp.fvgVisible() && window.TradeMateApp.obVisible()", timeout=20)
        self.until("window.TradeMateApp.fvgFrame().some(f => f && f.rects.length) || window.TradeMateApp.obFrame().some(f => f && f.rects.length)", timeout=20)
        data = self.engine()
        gaps = {g["id"]: g for g in data["fvg"]["gaps"]}
        blocks = {b["id"]: b for b in data["order_blocks"]["blocks"]}
        drawn_f = [r for f in self.page.evaluate("() => window.TradeMateApp.fvgFrame()") if f for r in f["rects"]]
        drawn_o = [r for f in self.page.evaluate("() => window.TradeMateApp.obFrame()") if f for r in f["rects"]]
        self.assertTrue(drawn_f or drawn_o)
        for r in drawn_f:
            self.assertEqual((r["lower"], r["upper"]), (gaps[r["id"]]["lower"], gaps[r["id"]]["upper"]))
        for r in drawn_o:
            self.assertEqual((r["lower"], r["upper"]), (blocks[r["id"]]["lower"], blocks[r["id"]]["upper"]))
            self.assertEqual(r["status"], blocks[r["id"]]["status"])
        self.assertTrue(all(f["key"] == "EURUSD|H1" for f in self.page.evaluate("() => window.TradeMateApp.fvgFrame()") if f))

    def test_stale_chart_data_hides_the_zones_and_says_so(self):
        self.page.evaluate("async () => { await fetch('/api/chart/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({fvg_visible: true, order_blocks_visible: true})}); }")
        self.page.reload()
        self.until("window.TradeMateApp.fvgFrame().some(f => f && f.rects.length) || window.TradeMateApp.obFrame().some(f => f && f.rects.length)", timeout=20)
        self.page.route("**/api/candles**", lambda route: route.abort())           # candle refreshes now fail
        self.until("window.TradeMateApp.zoneNotices().some(n => n.some(t => t.indexOf('not current') >= 0))", timeout=45)
        self.until("window.TradeMateApp.fvgFrame().every(f => !f) && window.TradeMateApp.obFrame().every(f => !f)", timeout=5)

    def test_an_unavailable_engine_result_shows_a_message_not_silence(self):
        def stub(route):
            resp = route.fetch()
            body = resp.json()
            body["fvg"] = {"available": False, "gaps": [], "reason": "test: not enough candles"}
            route.fulfill(response=resp, json=body)
        self.page.route("**/api/candles**", stub)
        self.page.evaluate("async () => { await fetch('/api/chart/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({fvg_visible: true})}); }")
        self.page.reload()
        self.until("window.TradeMateApp.zoneNotices().some(n => n.some(t => t.indexOf('Fair value gaps unavailable') >= 0 && t.indexOf('not enough candles') >= 0))", timeout=25)
        self.assertTrue(all(not f for f in self.page.evaluate("() => window.TradeMateApp.fvgFrame()")))


class AnalysisPageTest(BrowserBase):
    """Objective A in the browser."""

    def open_analysis(self):
        with self.page.expect_response(lambda r: "/api/analysis" in r.url and r.status == 200, timeout=20000):
            self.page.goto(self.base + "/#analysis")
        self.until("document.getElementById('an-summary').getAttribute('data-state') === 'ready'", timeout=20)

    def test_five_plain_sections_are_shown_and_the_technical_detail_is_collapsed(self):
        self.open_analysis()
        titles = [t.strip() for t in self.page.locator("#an-summary .panel-title").all_text_contents()]
        self.assertEqual(titles, ["1. Structure direction & quality", "2. Liquidity", "3. Fair value gaps", "4. Order blocks", "5. Overall interpretation"])
        badge = self.page.inner_text("#an-dir-badge").split(" ", 1)[-1]
        # The badge shows the confirmed direction and the evidence quality separately ("Bearish direction · aligned evidence"), or
        # the bare headline "Unclear" when no direction is confirmed.
        self.assertRegex(badge, r"^((Bullish|Bearish) direction \u00b7 (aligned|developing|early|mixed) evidence|Unclear)$")
        self.assertIn(self.page.inner_text("#an-overall-badge").split(" ", 1)[-1],
                      {"Bullish", "Bearish", "Mixed", "Not enough evidence"})
        self.assertFalse(self.page.evaluate("() => document.getElementById('an-tech').open"))
        self.assertFalse(self.page.locator("#an-structure").is_visible())
        self.page.click("#an-tech > summary")
        self.until("document.getElementById('an-tech').open")
        self.assertTrue(self.page.locator("#an-structure").is_visible())

    def test_the_overall_section_lists_what_is_uncertain_and_never_a_trade_signal(self):
        self.open_analysis()
        text = self.page.inner_text("#an-sum-overall").lower()
        self.assertIn("what remains uncertain", text)
        self.assertIn("not advice", text)
        self.assertNotIn("buy now", text)
        self.assertNotIn("sell now", text)

    def test_zone_prices_on_the_page_are_the_engines_prices(self):
        self.open_analysis()
        data = self.page.evaluate("async () => (await (await fetch('/api/analysis?symbol=EURUSD&timeframe=H1')).json())")
        shown = self.page.locator("#an-sum-fvg .an-zone-range, #an-sum-ob .an-zone-range").all_inner_texts()
        for side in ("bullish", "bearish"):
            for z in data["summary"]["fvg"][side] + data["summary"]["order_blocks"][side]:
                self.assertTrue(any(z["range_text"] in s for s in shown), z["range_text"])

    def test_a_broker_outage_shows_unavailable_in_every_section(self):
        self.open_analysis()
        self.broker.connected = False
        self.page.click("#an-refresh")
        self.until("document.getElementById('an-summary').getAttribute('data-state') === 'unavailable'", timeout=20)
        for box in ("an-sum-direction", "an-sum-liquidity", "an-sum-fvg", "an-sum-ob", "an-sum-overall"):
            self.assertIn("Unavailable", self.page.inner_text(f"#{box}"))
        self.broker.connected = True


_PEND = repair.PendingOrdersOnTheChartTest if PLAYWRIGHT else object


class PendingOverlayTest(_PEND):
    """Objective F (fake bridge). Inherits the existing fixture; the parent's own tests are not re-run here."""

    def test_rendering_pending_orders_never_sends_a_broker_command(self):
        self.add_order(9101, "buy_limit", self.at(0.30), sl=self.at(0.25))
        self.add_order(9102, "sell_stop", self.at(0.60))
        frame = self.wait_orders(2)
        self.assertEqual({o["ticket"] for o in frame["orders"]}, {9101, 9102})
        self.page.wait_for_timeout(1500)
        self.assertEqual([c for c in self.bridge.commands_seen if c.get("cmd") in FAKE_ORDER_COMMANDS], [])
        posts = [r for r in self.requests if r[0] == "POST" and ("/api/order" in r[1] or "/api/position" in r[1])]
        self.assertEqual(posts, [])
        self.assertEqual(set(self.bridge.orders), {9101, 9102}, "the orders are untouched")

    def test_a_cancelled_order_disappears_from_the_chart(self):
        self.add_order(9103, "buy_limit", self.at(0.30))
        self.wait_orders(1)
        self.bridge.orders.pop(9103)
        self.bridge.write_snapshot()
        self.wait_none()

    def test_orders_are_not_shown_as_current_when_broker_updates_stop(self):
        self.add_order(9104, "buy_limit", self.at(0.30))
        self.wait_orders(1)
        self.page.route("**/api/broker", lambda route: None)                       # the poll hangs: no further successful update
        self.until("window.TradeMateApp.pendingNotice().some(n => n && n.indexOf('no fresh broker data') >= 0)", timeout=30)
        self.assertTrue(all(not f for f in self.page.evaluate("() => window.TradeMateApp.pendingFrame()")))
        self.assertEqual([c for c in self.bridge.commands_seen if c.get("cmd") in FAKE_ORDER_COMMANDS], [])


del _PEND                                         # the alias would make the loader collect (and re-run) the parent's own tests here
if PLAYWRIGHT:                                   # keep the parent's inherited tests out of this class
    for _name in [n for n in dir(PendingOverlayTest) if n.startswith("test_") and n not in vars(PendingOverlayTest)]:
        setattr(PendingOverlayTest, _name, None)


if __name__ == "__main__":
    unittest.main()
