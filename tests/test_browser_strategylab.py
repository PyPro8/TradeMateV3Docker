"""Strategy Lab foundation in a real browser (headless Chromium) against a STUB broker. No real MT5, no order.
Skipped (not passed) when Playwright / Chromium is missing.

    python3 -m unittest tests.test_browser_strategylab -v
"""

import unittest
from datetime import datetime, timedelta, timezone

from tests.browser_support import BrowserBase


class StrategyLabPage(BrowserBase):
    def open_lab(self, width=1280, height=900):
        self.page.set_viewport_size({"width": width, "height": height})
        self.page.goto(self.base + "/#strategylab")
        self.until("document.querySelector('#view-strategylab [data-cap=broker]').getAttribute('data-state') !== 'loading'")

    def state(self, cap):
        return self.page.evaluate(f"() => document.querySelector('#view-strategylab [data-cap={cap}]').getAttribute('data-state')")

    def test_navigation_reaches_the_page_and_sets_the_title(self):
        self.page.goto(self.base + "/")
        self.page.click("a.nav-link[data-view=strategylab]")
        self.until("!document.getElementById('view-strategylab').hidden")
        self.assertEqual(self.page.title(), "Command Centre \u00b7 TradeMate")
        self.assertEqual(self.page.get_attribute("a.nav-link[data-view=strategylab]", "aria-current"), "page")

    def test_the_page_shows_real_capability_states(self):
        self.open_lab()
        self.assertEqual(self.state("broker"), "Available")                      # the stub broker is connected
        for cap in ("selector", "analyse", "mtf", "decision", "setup", "watchdog", "notifications", "sound", "execution"):
            self.assertEqual(self.state(cap), "Available", cap)          # controlled execution (REVIEW TRADE -> CONFIRM & EXECUTE) now exists
        for sel in ("#lab-market", "#lab-analyse"):
            self.assertFalse(self.page.is_disabled(sel), sel)
        self.assertFalse(self.page.locator("#cc-execute-btn").is_visible())           # no execute control until the Risk Engine has approved a review
        self.assertTrue(self.page.locator("#cc-review").is_hidden())
        self.assertIn("No analysis yet", self.page.inner_text("#lab-decision-empty"))

    def test_a_disconnected_broker_is_shown_as_disconnected(self):
        self.addCleanup(setattr, self.broker, "connected", True)
        self.broker.connected = False
        self.open_lab()
        self.assertEqual(self.state("broker"), "Disconnected")
        self.assertIn("not connected", self.page.inner_text("#lab-status-line"))

    def test_the_page_sends_nothing_but_the_status_request(self):
        self.open_lab()
        posts = [r for r in self.requests if r[0] == "POST" and "/api/watch" not in r[1]]
        self.assertEqual(posts, [])
        self.assertTrue([r for r in self.requests if "/api/strategylab/status" in r[1]])
        self.assertFalse([r for r in self.requests if "/api/order" in r[1] or "/api/strategy/mtf" in r[1]])

    def test_market_search_and_analysis_use_real_read_only_routes(self):
        self.open_lab()
        self.page.locator("#lab-market").fill("EUR")
        self.page.wait_for_function("() => Array.from(document.querySelector('#lab-market-options').options).some(o => o.value === 'EURUSD')")
        self.page.locator("#lab-market").fill("EURUSD")
        with self.page.expect_response(lambda r: "/api/symbols/add" in r.url and r.request.method == "POST", timeout=15000):
            with self.page.expect_response(lambda r: "/api/strategy/analyse" in r.url and r.status == 200, timeout=15000):
                self.page.locator("#lab-analyse").click()
                self.until("document.querySelector('#lab-decision-body .lab-decision-badge') !== null")
        self.assertEqual(self.page.locator("#lab-decision-body .lab-decision-badge").count(), 1)
        self.assertEqual(self.page.locator("#lab-mtf-body .lab-timeframe-card").count(), 2)          # H1 (context) and M5 (setup): nothing else is read
        with self.page.expect_response(lambda r: "/api/strategylab/watchdog/start" in r.url and r.status == 202, timeout=15000):
            self.page.locator("#lab-watchdog").check()
        self.until("document.querySelector('#lab-watchdog').checked")
        # The stub's synthetic candles are misaligned and it has no M5 data, so the honest label is "temporarily unavailable", never a fake "Active".
        self.until("document.querySelector('#cc-wd-state').textContent.includes('CHoCH Trading')")
        label = self.page.inner_text("#cc-wd-state")
        self.assertTrue("Active" in label or "Temporarily unavailable" in label, label)
        self.assertIn("CHoCH Trading", label)                                        # bound to the selected strategy
        self.assertTrue(self.page.inner_text("#lab-watchdog-note").strip())
        with self.page.expect_response(lambda r: "/api/strategylab/watchdog/stop" in r.url and r.status == 200, timeout=15000):
            self.page.locator("#lab-watchdog").uncheck()
        self.until("!document.querySelector('#lab-watchdog').checked")
        self.assertFalse([r for r in self.requests if "/api/order" in r[1]])

    def test_saved_mt5_dropdown_lists_broker_watch_symbols_and_selects_one(self):
        self.open_lab()
        self.page.locator("#lab-market-dropdown").click()
        self.page.wait_for_function("() => document.querySelectorAll('#lab-market-saved [data-symbol]').length === 2")
        names = self.page.locator("#lab-market-saved [data-symbol]").evaluate_all("(els) => els.map(el => el.getAttribute('data-symbol'))")
        self.assertEqual(names, ["EURUSD", "XAUUSD"])
        self.page.locator("#lab-market-saved [data-symbol=XAUUSD]").click()
        self.assertEqual(self.page.locator("#lab-market").input_value(), "XAUUSD")
        self.assertEqual(self.page.get_attribute("#lab-market-dropdown", "aria-expanded"), "false")
        self.assertIn("XAUUSD", self.page.inner_text("#lab-summary-market"))
        self.assertTrue([r for r in self.requests if r[0] == "GET" and "/api/broker" in r[1]])

    def test_layout_is_usable_at_desktop_tablet_and_phone_widths(self):
        for width in (1280, 820, 390, 320):
            with self.subTest(width=width):
                self.open_lab(width)
                overflow = self.page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
                self.assertLessEqual(overflow, 1, f"horizontal scroll at {width}px")
                for sel in ("#lab-analyse", ".lab-decision", ".lab-rail"):
                    box = self.page.locator(sel).first.bounding_box()
                    self.assertIsNotNone(box, sel)
                    self.assertLessEqual(box["x"] + box["width"], width + 1, f"{sel} overflows at {width}px")
        self.open_lab(390)
        columns = self.page.evaluate("() => getComputedStyle(document.querySelector('.lab-layout')).gridTemplateColumns.split(' ').length")
        self.assertEqual(columns, 1)                                              # single column on a phone
        self.open_lab(1280)
        columns = self.page.evaluate("() => getComputedStyle(document.querySelector('.lab-layout')).gridTemplateColumns.split(' ').length")
        self.assertEqual(columns, 2)                                              # decision area beside the rail on a desktop

    def test_the_existing_strategy_page_is_unchanged_by_the_new_page(self):
        self.page.goto(self.base + "/#strategy")
        self.until("document.querySelector('#mtf-panel')")
        self.assertTrue(self.page.is_visible("#mtf-panel"))
        self.assertTrue(self.page.is_hidden("#view-strategylab"))

    def test_account_centre_shows_only_the_currently_verified_broker_identity(self):
        self.addCleanup(setattr, self.broker, "connected", True)           # this test disconnects the shared stub; later tests must not inherit that
        self.page.goto(self.base + "/#accountcenter")
        self.until("document.getElementById('account-centre-status').getAttribute('data-state') !== 'checking'")
        self.assertEqual(self.page.inner_text("#account-centre-login"), "1")
        self.assertEqual(self.page.inner_text("#account-centre-environment"), "DEMO")
        self.assertIn("identity verified", self.page.inner_text("#account-centre-status").lower())
        self.assertEqual(self.page.locator("#view-accountcenter button").count(), 0)
        self.assertIn("no account-login or account-switch command", self.page.inner_text("#view-accountcenter"))
        self.broker.connected = False
        self.until("document.getElementById('account-centre-login').textContent === '---'")
        self.assertEqual(self.page.inner_text("#account-centre-status"), "DISCONNECTED")

    def test_preferred_name_is_saved_cleared_and_used_in_the_greeting(self):
        self.page.goto(self.base + "/#settings")
        self.page.locator("#display-name-input").fill("Amina")
        self.page.locator("#display-name-save").click()
        self.page.goto(self.base + "/#strategylab")
        self.until("document.getElementById('lab-greeting').getAttribute('data-display-name') === 'Amina'")
        self.assertIn(", Amina.", self.page.inner_text("#lab-greeting"))
        self.page.goto(self.base + "/#settings")
        self.page.locator("#display-name-clear").click()
        self.page.goto(self.base + "/#strategylab")
        self.until("document.getElementById('lab-greeting').getAttribute('data-display-name') === ''")
        self.assertNotIn(", Amina.", self.page.inner_text("#lab-greeting"))


class WelcomeAndHierarchy(BrowserBase):
    """Part 2: the time-of-day greeting, welcome-back, compactness and the visual hierarchy. The browser clock and timezone are pinned."""

    def lab(self, utc_time, width=1280, height=900, ctx=None):
        ctx = ctx or self.browser.new_context(viewport={"width": width, "height": height}, timezone_id="UTC")
        self.addCleanup(ctx.close)
        ctx.clock.set_fixed_time(utc_time)
        page = ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("request", lambda r: self.requests.append((r.method, r.url, r.post_data)))
        page.goto(self.base + "/#strategylab")
        page.wait_for_function("() => document.getElementById('lab-greeting').getAttribute('data-part')")
        return ctx, page

    def test_the_greeting_follows_the_time_of_day(self):
        for hour, minute, expected in ((5, 0, "Good morning"), (11, 59, "Good morning"), (12, 0, "Good afternoon"), (17, 59, "Good afternoon"),
                                       (18, 0, "Good evening"), (23, 30, "Good evening"), (3, 15, "Good evening")):
            with self.subTest(hour=hour, minute=minute):
                _, page = self.lab(datetime(2026, 10, 21, hour, minute, tzinfo=timezone.utc))
                self.assertEqual(page.inner_text("#lab-greeting"), f"{expected}. Welcome to Command Centre.")

    def test_a_returning_visitor_is_welcomed_back_and_a_first_visit_is_not(self):
        t1 = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
        ctx, page = self.lab(t1)
        self.assertIn("Welcome to Command Centre.", page.inner_text("#lab-greeting"))
        self.assertEqual(page.get_attribute("#lab-greeting", "data-returning"), "false")
        ctx.clock.set_fixed_time(t1 + timedelta(hours=5))                  # a later visit in the same browser profile
        page.reload()
        page.wait_for_function("() => document.getElementById('lab-greeting').getAttribute('data-part')")
        self.assertEqual(page.inner_text("#lab-greeting"), "Good afternoon. Welcome back to Command Centre.")
        self.assertEqual(page.get_attribute("#lab-greeting", "data-returning"), "true")

    def test_the_greeting_is_compact_and_the_decision_area_stays_above_the_fold(self):
        _, page = self.lab(datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc), 1280, 900)
        welcome = page.locator(".lab-welcome").bounding_box()
        decision = page.locator(".lab-decision").bounding_box()
        self.assertLess(welcome["height"], 190, "the welcome banner must not dominate the page")
        start = page.locator("[data-scope='cc'] [data-tc='ap-start']").bounding_box()
        self.assertLess(start["y"] + start["height"], 900, "Start Autopilot must be visible without scrolling on a desktop")
        self.assertLess(page.locator("[data-scope='cc'] [data-tc='badge']").bounding_box()["y"], 600, "the live decision (in the Autopilot bar) must be above the fold")
        self.assertLess(page.locator(".cc-bar-panel").bounding_box()["y"], decision["y"], "the Autopilot bar sits above the analysis panels")
        _, phone = self.lab(datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc), 390, 800)
        self.assertLess(phone.locator(".lab-welcome").bounding_box()["height"], 260)
        start_phone = phone.locator("[data-scope='cc'] [data-tc='ap-start']").bounding_box()
        self.assertLess(start_phone["y"] + start_phone["height"], 800, "Start Autopilot must be visible without scrolling on a phone")
        self.assertEqual(phone.locator(".lab-steps").count(), 0)
        self.assertEqual(phone.locator(".lab-status-tile, .lab-metrics, .lab-steps").count(), 0)
        self.assertIn("Choose an instrument below", phone.inner_text(".lab-welcome"))

    def test_blocked_browser_storage_does_not_break_the_welcome(self):
        ctx = self.browser.new_context(viewport={"width": 1280, "height": 900}, timezone_id="UTC")
        ctx.add_init_script("""(() => {
          for (const method of ['getItem', 'setItem', 'removeItem']) {
            Storage.prototype[method] = function () { throw new Error('storage blocked'); };
          }
        })();""")
        _, page = self.lab(datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc), ctx=ctx)
        self.assertTrue(page.is_visible("#lab-greeting"))
        self.assertIn("Good morning", page.inner_text("#lab-greeting"))

    def test_visual_hierarchy_follows_the_workflow(self):
        _, page = self.lab(datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc))
        y = lambda sel: page.locator(sel).first.bounding_box()["y"]
        self.assertLess(y(".cc-bar-panel"), y(".lab-welcome"))       # the Autopilot command bar is the first thing on the page
        self.assertLess(y(".lab-welcome"), y(".lab-toolbar"))
        self.assertLess(y(".lab-toolbar"), y(".lab-decision"))
        self.assertLess(y(".lab-decision"), y("[aria-label='Multi-timeframe context']"))
        self.assertLess(y("[aria-label='Multi-timeframe context']"), y("[aria-label='Setup details']"))
        rail = page.evaluate("() => Array.from(document.querySelectorAll('.lab-rail > section')).map(s => s.getAttribute('aria-label'))")
        self.assertEqual(rail, ["Connection", "Monitoring", "Execution", "Notifications and sound"])
        self.assertIn("lab-primary", page.get_attribute(".lab-decision", "class"))
        self.assertEqual(page.locator(".lab-rail .lab-primary").count(), 0)

    def test_primary_controls_are_comfortable_touch_targets_on_a_phone(self):
        _, page = self.lab(datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc), 390, 800)
        self.assertGreaterEqual(page.locator("#lab-analyse").bounding_box()["height"], 40)
        self.assertGreaterEqual(page.locator("#lab-market").bounding_box()["height"], 36)

    def test_both_themes_keep_the_page_readable(self):
        ctx, page = self.lab(datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc))
        for theme in ("dark", "light"):
            page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)", theme)
            colour = page.evaluate("() => getComputedStyle(document.getElementById('lab-greeting')).color")
            background = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
            self.assertNotEqual(colour, background, theme)
            self.assertTrue(page.is_visible("#lab-greeting"), theme)

    def test_the_new_page_sends_no_orders_and_no_analysis_requests(self):
        _, page = self.lab(datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc))
        self.assertTrue([r for r in self.requests if "/api/strategylab/status" in r[1]], "the request log must really be capturing this page")
        self.assertEqual([r for r in self.requests if "/api/order" in r[1] or "/api/strategy/mtf" in r[1]], [])


if __name__ == "__main__":
    unittest.main()
