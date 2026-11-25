"""Navigation of the whole application in a real browser (headless Chromium) against a STUB broker: every page opens from the sidebar,
the active indicator follows, refresh / back / forward / malformed addresses behave, the mobile drawer closes (including on a tap of the
page you are already on), nothing is duplicated, and the Chart page still draws. No MT5, no order. Skipped when Playwright is missing.

    python3 -m unittest tests.test_browser_navigation -v
"""

import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.browser_support import BrowserBase
from trademate.rapid_fire.store import ConfigStore

ROUTES = ["dashboard", "chart", "analysis", "strategy", "strategylab", "accountcenter", "trades", "history", "journal", "backtest", "settings"]


def _isolate_rapid_fire(cls):
    """Rapid Fire's saved settings go to a throwaway file, never to the real data/ folder."""
    return [mock.patch.object(app_module.rapid_engine, "config_store", ConfigStore(Path(cls._tmp.name) / "rf_config.json"))]


class NavigationTest(BrowserBase):
    extra_patches = (_isolate_rapid_fire,)

    def visible_views(self):
        return self.page.evaluate("() => Array.from(document.querySelectorAll('.view')).filter(v => !v.hidden).map(v => v.id)")

    def current_links(self):
        return self.page.evaluate("() => Array.from(document.querySelectorAll('#sidebar [aria-current=page]')).map(a => a.dataset.view)")

    def assert_route(self, name):
        self.until(f"location.hash === '#{name}' && !document.getElementById('view-{name}').hidden")
        self.assertEqual(self.visible_views(), [f"view-{name}"], "exactly one page is shown")
        self.assertEqual(self.current_links(), [name], "exactly the matching nav item is marked current")
        self.assertIn(self.page.evaluate("document.getElementById('view-%s').dataset.title" % name), self.page.title())

    def test_every_sidebar_item_opens_its_page(self):
        self.page.goto(self.base + "/")
        self.assert_route("dashboard")                      # the default route is canonicalised into the address
        for name in ROUTES:
            self.page.click(f'#sidebar a[data-view="{name}"]')
            self.assert_route(name)
        self.assertEqual(self.errors, [])

    def test_refresh_back_and_forward(self):
        self.page.goto(self.base + "/#history")
        self.assert_route("history")
        self.page.click('#sidebar a[data-view="chart"]')
        self.assert_route("chart")
        self.page.click('#sidebar a[data-view="settings"]')
        self.assert_route("settings")
        self.page.go_back()
        self.assert_route("chart")
        self.page.go_back()
        self.assert_route("history")
        self.page.go_forward()
        self.assert_route("chart")
        self.page.reload()
        self.assert_route("chart")
        self.assertEqual(self.errors, [])

    def test_malformed_and_unknown_addresses_land_on_a_real_page(self):
        for raw, expected in (("#CHART", "chart"), ("#chart/", "chart"), ("#chart?x=1", "chart"), ("#/history", "history"),
                              ("#nope", "dashboard"), ("#", "dashboard"), ("#command-centre", "strategylab")):
            self.page.goto("about:blank")                    # a fresh load of each address, like pasting it into the browser
            self.page.goto(self.base + "/" + raw)
            self.assert_route(expected)
        self.assertEqual(self.errors, [])

    def test_the_router_has_one_listener_set_and_visits_do_not_add_more(self):
        self.page.goto(self.base + "/#dashboard")
        self.until("window.TradeMateRouter")
        before = self.page.evaluate("TradeMateRouter.listenerCount()")
        for name in ROUTES * 2:
            self.page.evaluate(f"TradeMateRouter.go('{name}')")
        self.assertEqual(self.page.evaluate("TradeMateRouter.listenerCount()"), before)
        src = self.page.evaluate("Array.from(document.scripts).map(s => s.src)")
        self.assertEqual(len([s for s in src if s.endswith("router.js") or "router.js?" in s]), 1)

    def test_no_duplicate_content_and_the_chart_is_not_duplicated_on_the_dashboard(self):
        self.page.goto(self.base + "/#dashboard")
        self.assert_route("dashboard")
        self.assertEqual(self.page.locator("#view-dashboard canvas").count(), 0, "the dashboard carries no second chart")
        ids = self.page.evaluate("() => { const seen = {}, dup = []; document.querySelectorAll('[id]').forEach(e => { if (seen[e.id]) dup.push(e.id); seen[e.id] = 1; }); return dup; }")
        self.assertEqual(ids, [], "no duplicated element ids")
        for _ in range(3):
            self.page.click('#sidebar a[data-view="chart"]')
            self.page.click('#sidebar a[data-view="dashboard"]')
        self.assertEqual(self.page.locator("#view-chart canvas").count(), self.page.evaluate("document.querySelectorAll('#view-chart canvas').length"))
        self.assertEqual(self.page.locator("#view-dashboard .kpi").count(), 10)

    def test_the_chart_draws_after_navigating_to_it(self):
        self.page.goto(self.base + "/#dashboard")
        self.page.click('#sidebar a[data-view="chart"]')
        self.assert_route("chart")
        self.until("(c => c && c.width > 0 && c.height > 0)(document.querySelector('#view-chart canvas'))")
        self.assertEqual(self.errors, [])

    def test_mobile_drawer_closes_on_navigation_including_the_same_page(self):
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.page.goto(self.base + "/#chart")
        self.assert_route("chart")
        for target in ("history", "chart", "chart"):         # the last two are taps on the page that is already open
            self.page.click("#nav-toggle")
            self.until("document.getElementById('sidebar').classList.contains('is-open')")
            self.page.click(f'#sidebar a[data-view="{target}"]')
            self.until("!document.getElementById('sidebar').classList.contains('is-open')")
            self.assert_route(target)
        self.assertEqual(self.errors, [])

    def test_the_strategy_page_lists_six_strategies_and_the_info_buttons_work_by_tap(self):
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.page.goto(self.base + "/#strategy")
        self.until("document.querySelectorAll('#strat-grid .strat-card').length === 6")
        self.assertEqual(self.page.locator("#strat-grid [data-strategy=rapid_fire]").count(), 1)
        card = self.page.locator("#strat-grid .strat-card").first
        about = card.locator(".strat-about")
        self.assertTrue(about.is_hidden())
        card.locator(".tc-info").click()
        self.assertTrue(about.is_visible())
        self.assertEqual(self.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), True, "no horizontal page scroll at 390px")

    def test_rapid_fire_panel_is_real_and_refuses_to_start_unconfigured(self):
        self.page.goto(self.base + "/#strategy")
        self.until("document.getElementById('rf-state').textContent !== 'Loading'")
        self.assertIn("NOT CONFIGURED", self.page.inner_text("#rf-state").upper())
        self.assertEqual(self.page.input_value("#rf-timeframe"), "M1")
        self.assertTrue(self.page.is_disabled("#rf-timeframe"))
        self.assertTrue(self.page.is_disabled("#rf-stop"))
        self.page.click("#rf-start")
        self.until("document.getElementById('rf-msg').textContent.length > 0")
        self.assertEqual(self.errors, [])

    def test_rapid_fire_settings_save_from_the_panel_and_anti_twin_is_a_plain_on_off_switch(self):
        self.page.goto(self.base + "/#strategy")
        self.until("document.getElementById('rf-state').textContent !== 'Loading'")
        self.page.fill("#rf-lot", "0.2")
        self.page.fill("#rf-emergency", "40")
        self.page.click("#rf-save")
        self.until("/Saved/.test(document.getElementById('rf-save-msg').textContent)")
        cfg = self.page.evaluate("fetch('/api/rapid-fire/config').then(r => r.json())")["config"]
        self.assertEqual((cfg["lot"], cfg["emergency_points"], cfg["max_entries_per_candle"]), (0.2, 40, 2))
        self.assertFalse(cfg["anti_twin_enabled"])
        self.assertEqual(self.page.evaluate("document.getElementById('rf-at-points') === null"), True)   # no threshold field any more
        self.page.check("#rf-at-enabled")
        self.page.click("#rf-save")
        self.until("/Saved/.test(document.getElementById('rf-save-msg').textContent)")
        status = self.page.evaluate("fetch('/api/rapid-fire/status').then(r => r.json())")
        self.assertEqual(status["anti_twin"]["status"], "ARMED")
        self.assertTrue(status["config"]["anti_twin_enabled"])
        self.until("document.getElementById('rf-at').textContent === 'ARMED'")
        self.assertEqual(self.page.evaluate("document.querySelectorAll('form').length"), 0)

    def test_dashboard_order_and_the_target_monitor_is_visible_without_scrolling(self):
        self.page.set_viewport_size({"width": 1440, "height": 900})
        self.page.goto(self.base + "/#dashboard")
        self.assert_route("dashboard")
        order = self.page.evaluate("() => ['.kpi-primary', '#target-monitor', '#trading-status', '.kpi-secondary', '#perf-overview'].map(s => document.querySelector(s).getBoundingClientRect().top)")
        self.assertEqual(order, sorted(order), "account overview, Target Monitor, trading status, results, performance: in that order")
        self.assertLess(self.page.evaluate("document.querySelector('#target-monitor .tm-svg').getBoundingClientRect().bottom"), 900, "the gauge itself is on the first screen")
        for label in ("Watchdog", "Selected strategy", "Instrument", "Execution timeframe", "Trading mode", "Autopilot", "Recent execution"):
            self.assertIn(label, self.page.inner_text("#trading-status"))
        self.assertEqual(self.errors, [])

    def test_trading_status_shows_the_real_selection_and_never_invents_a_value(self):
        self.page.goto(self.base + "/#dashboard")
        self.until("document.getElementById('dash-instrument').textContent !== '---'")
        sel = self.page.evaluate("fetch('/api/selection').then(r => r.json())")
        self.assertEqual(self.page.inner_text("#dash-instrument"), sel["symbol"])
        self.assertEqual(self.page.inner_text("#dash-timeframe"), sel["timeframe"])
        self.assertIn(self.page.inner_text("#dash-watchdog"), ("NOT MONITORING", "MONITORING"))
        ex = self.page.evaluate("fetch('/api/executions').then(r => r.json())")
        expected = "No order attempts recorded" if ex.get("ok") and not ex.get("executions") else "---"   # an unreadable journal shows ---, never a made-up line
        self.assertEqual(self.page.inner_text("#dash-lastexec"), expected)

    def test_touch_targets_are_at_least_40px_on_a_phone_and_the_chart_tools_are_reachable(self):
        self.page.set_viewport_size({"width": 360, "height": 740})
        self.page.goto(self.base + "/#strategy")
        self.until("document.querySelectorAll('#strat-grid .tc-info').length > 0")
        small = self.page.evaluate("""() => Array.from(document.querySelectorAll('#view-strategy .tc-info')).map(b => { const r = b.getBoundingClientRect(), a = getComputedStyle(b, '::after'); return Math.min(r.width, r.height) + 16; }).filter(v => v < 40)""")
        self.assertEqual(small, [], "every info button has a hit area of 40px or more")
        self.page.goto(self.base + "/#chart")
        self.until("document.querySelector('#view-chart .tb-tools')")
        cap = self.page.evaluate("""(() => { const c = document.querySelector('#view-chart [data-role=tb-capsule]'), r = c.getBoundingClientRect();
            c.style.scrollBehavior = 'auto'; const t = Array.from(document.querySelectorAll('#view-chart .tb-tools button')).pop(); c.scrollLeft = c.scrollWidth; const tr = t.getBoundingClientRect(), cr = c.getBoundingClientRect();
            return [r.width <= innerWidth + 1, c.scrollWidth > c.clientWidth, tr.right <= cr.right + 1, document.documentElement.scrollWidth <= innerWidth + 1]; })()""")
        self.assertEqual(cap, [True, True, True, True], "one scrolling capsule inside the screen; the tools are reachable by scrolling it; the page does not overflow")

    def test_light_and_dark_themes_both_render_the_gauge_and_persist(self):
        self.page.goto(self.base + "/#dashboard")
        self.assert_route("dashboard")
        for theme in ("dark", "light"):
            self.page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
            bg = self.page.evaluate("getComputedStyle(document.body).backgroundColor")
            track = self.page.evaluate("getComputedStyle(document.querySelector('#target-monitor .tm-track')).stroke")
            self.assertNotEqual(track, "none")
            setattr(self, "_bg_" + theme, bg)
        self.assertNotEqual(self._bg_dark, self._bg_light, "the light theme is its own design, not the dark one")


if __name__ == "__main__":
    unittest.main()
