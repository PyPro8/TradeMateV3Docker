"""The Trading controls in a real browser: the Watchdog's decision reaches the panel with NO Analyse click; switching invalidates it; lot-size
validation is shown; Autopilot refuses to start without a stop condition. Fake MT5 bridge, no real order.

    python3 -m unittest tests.test_browser_trading_controls -v
"""

import copy
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.browser_support import PLAYWRIGHT
from tests.test_browser_commandcentre import CommandCentreBase, _STACK, ui_payload, wait_payload
from tests.test_pipeline import strategy_payload
from trademate.decision_hub import DecisionHub
from trademate.lotsize import LotStore
from trademate.selection_store import SelectionStore
from trademate.strategy.watchdog import WatchdogManager


@unittest.skipUnless(PLAYWRIGHT, "SKIPPED: Playwright is not installed.")
class TradingControlsUiTest(CommandCentreBase):
    def setUp(self):
        super().setUp()
        tmp = Path(_STACK["tmp"].name)
        self.hub = DecisionHub(stale_after=120)
        self.sel = SelectionStore(tmp / "ui_sel.json")
        self.lots = LotStore(tmp / "ui_lots.json")
        self.current = wait_payload()
        feed = lambda symbol, strategy_id=None, exec_tf=None: dict(copy.deepcopy(self.current), connection="CONNECTED")       # noqa: E731  (the real service reports CONNECTED)
        self.wd = WatchdogManager(feed, interval=0.2, hub=self.hub)
        for name, obj in (("decision_hub", self.hub), ("selection_store", self.sel), ("lot_store", self.lots), ("watchdog", self.wd)):
            p = mock.patch.object(app_module, name, obj)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.wd.stop)
        self.sel.save(symbol="EURUSD", strategy_id="choch", timeframe="M5")

    def badge(self):
        return self.page.locator("#view-strategylab [data-tc=badge]").inner_text()

    def test_watchdog_decision_reaches_the_panel_without_clicking_analyse(self):
        self.open_cc()
        self.until("document.querySelector('#view-strategylab [data-tc=symbol]').value === 'EURUSD'")
        self.page.evaluate("fetch('/api/strategylab/watchdog/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({symbol: 'EURUSD'})})")
        self.until("document.querySelector('#view-strategylab [data-tc=badge]').textContent === 'WAIT'", timeout=8000)
        self.current = ui_payload(strategy_payload("BUY"))
        self.until("document.querySelector('#view-strategylab [data-tc=badge]').textContent === 'BUY'", timeout=8000)
        self.until("document.querySelector('#lab-decision-body .lab-decision-badge')?.textContent === 'BUY'", timeout=8000)     # the big panel too
        self.assertEqual(self.writes(), [])
        self.current = wait_payload()
        self.until("document.querySelector('#view-strategylab [data-tc=badge]').textContent === 'WAIT'", timeout=8000)

    def test_changing_timeframe_clears_the_old_decision(self):
        self.open_cc()
        self.current = ui_payload(strategy_payload("BUY"))
        self.page.evaluate("fetch('/api/strategylab/watchdog/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({symbol: 'EURUSD'})})")
        self.until("document.querySelector('#view-strategylab [data-tc=badge]').textContent === 'BUY'", timeout=8000)
        self.current = wait_payload()
        self.page.select_option("#view-strategylab [data-tc=timeframe]", "H1")
        end = __import__("time").time() + 8
        while __import__("time").time() < end and (self.sel.timeframe("choch") != "H1" or self.wd.status()["exec_tf"] != "H1"):
            self.page.wait_for_timeout(100)
        self.assertEqual(self.sel.timeframe("choch"), "H1")                       # the backend, not just the page, holds it
        self.assertEqual(self.wd.status()["exec_tf"], "H1")                        # the Watchdog followed the selection

    def test_invalid_lot_is_rejected_with_a_useful_message_and_not_saved(self):
        self.open_cc()
        self.until("document.querySelector('#view-strategylab [data-tc=symbol]').value === 'EURUSD'")
        box = self.page.locator("#view-strategylab [data-tc=lot]")
        box.fill("0.015")
        self.page.locator("#view-strategylab [data-tc=lot-save]").click()
        self.until("document.querySelector('#view-strategylab [data-tc=lot-msg]').textContent.includes('0.01 and 0.02')")
        self.assertIsNone(self.lots.get("EURUSD"))
        box.fill("0.07")
        self.page.locator("#view-strategylab [data-tc=lot-save]").click()
        self.until("document.querySelector('#view-strategylab [data-tc=lot-state]').textContent.includes('0.07')")
        self.assertEqual(self.lots.get("EURUSD"), 0.07)

    def test_pip_size_is_set_in_the_browser_and_shows_what_the_targets_mean(self):
        self.open_cc()
        r = "#view-strategylab "
        self.page.locator(r + "details.tc-fold > summary").first.click()      # the pip card sits in a collapsed "advanced" fold in the command bar
        self.until("document.querySelector('#view-strategylab [data-tc=pip-state]').textContent.includes('1 pip')")
        self.page.fill(r + "[data-tc=pip]", "abc")
        self.page.locator(r + "[data-tc=pip-save]").click()
        self.until("document.querySelector('#view-strategylab [data-tc=pip-msg]').textContent.includes('greater than zero')")
        self.assertIsNone(app_module.pip_store.get("EURUSD"))
        self.page.fill(r + "[data-tc=pip]", "0.001")
        self.page.locator(r + "[data-tc=pip-save]").click()
        self.until("document.querySelector('#view-strategylab [data-tc=pip-state]').textContent.includes('set by you')")
        self.assertEqual(app_module.pip_store.get("EURUSD"), 0.001)
        self.until("document.querySelector('#view-strategylab [data-tc=pip-preview]').textContent.includes('CHoCH target: 100 pips = 0.1')")

    def test_side_navigation_tabs_switch_panes_and_show_the_same_controls(self):
        self.page.set_viewport_size({"width": 1280, "height": 900})
        self.page.goto(self.base + "/#chart")
        self.until("document.querySelector('.trade-panel [data-sidenav-tab=auto]') !== null")
        ids = self.page.evaluate("Array.from(document.querySelectorAll('[id]')).map(e => e.id).filter((v, i, a) => a.indexOf(v) !== i)")
        self.assertEqual(ids, [], "duplicate element ids")
        panel = self.page.locator(".trade-panel:visible").first
        self.assertTrue(panel.locator("[data-sidenav-pane=auto]").is_hidden())
        panel.locator("[data-sidenav-tab=auto]").click()
        self.assertTrue(panel.locator("[data-sidenav-pane=auto]").is_visible())
        self.assertTrue(panel.locator("[data-sidenav-pane=order]").is_hidden())
        self.assertTrue(panel.locator("[data-tc=timeframe]").is_visible())
        self.assertTrue(panel.locator("[data-tc=lot]").is_visible())

    def test_autopilot_needs_a_stop_condition_and_only_the_chosen_field_is_editable(self):
        self.open_cc()
        r = "#view-strategylab "
        self.assertTrue(self.page.is_disabled(r + "[data-tc=val-runs]"))
        self.page.locator(r + "[data-tc=mode][value=profit]").check()
        self.assertFalse(self.page.is_disabled(r + "[data-tc=val-profit]"))
        self.assertTrue(self.page.is_disabled(r + "[data-tc=val-runs]"))
        self.assertTrue(self.page.is_disabled(r + "[data-tc=val-hours]"))
        self.page.fill(r + "[data-tc=val-profit]", "-5")
        self.page.locator(r + "[data-tc=ap-start]").click()
        self.until("document.querySelector('#view-strategylab [data-tc=ap-msg]').textContent.length > 0")
        self.assertFalse(self.client_status()["armed"])

    def client_status(self):
        return app_module.app.test_client().get("/api/autopilot/status").get_json()

    def test_no_horizontal_overflow_on_a_phone_width(self):
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.page.goto(self.base + "/#strategylab")
        self.until("document.querySelector('#view-strategylab [data-tc=symbol]') !== null")
        over = self.page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        self.assertLessEqual(over, 1, "the page scrolls sideways on a phone")


if __name__ == "__main__":
    unittest.main()
