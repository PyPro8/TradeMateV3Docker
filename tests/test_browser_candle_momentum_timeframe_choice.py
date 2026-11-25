"""The user chooses M1 or M5 for Candle Momentum from the real Command Centre interface (headless Chromium, real Flask app, fake bridge; no real MT5, no order).

    python3 -m unittest tests.test_browser_candle_momentum_timeframe_choice -v

Skipped (not passed) without Playwright / Chromium. Regression for the baseline defect where the timeframe selector offered only M5 for Candle Momentum.
"""

import time
import unittest

import app as app_module
from tests.browser_support import PLAYWRIGHT
from tests.test_browser_commandcentre import CommandCentreBase
from trademate.strategies.base import EXEC_TIMEFRAMES

TF_SELECT = '[data-scope="cc"] [data-tc="timeframe"]'


@unittest.skipUnless(PLAYWRIGHT, "SKIPPED: Playwright is not installed.")
class TimeframeChoiceUiTest(CommandCentreBase):
    def setUp(self):
        super().setUp()
        app_module.strategy_store.select("candle_momentum")
        app_module.selection_store.save(symbol="EURUSD", strategy_id="candle_momentum", timeframe="M5")
        self.addCleanup(app_module.strategy_store.select, "choch")

    def options(self):
        return self.page.evaluate("(sel) => Array.from(document.querySelector(sel).options).map(o => o.value)", TF_SELECT)

    def wait_saved(self, tf):
        deadline = time.time() + 8
        while time.time() < deadline:
            if app_module.selection_store.timeframe("candle_momentum") == tf:
                return
            time.sleep(0.1)
        self.fail(f"The selection store never recorded {tf}; it holds {app_module.selection_store.timeframe('candle_momentum')}.")

    def test_selector_offers_every_execution_timeframe_including_m1_and_m5(self):
        self.open_cc()
        self.assertEqual(self.options(), list(EXEC_TIMEFRAMES))
        self.assertEqual(self.page.locator(TF_SELECT).input_value(), "M5")
        self.assertFalse(self.page.is_disabled(TF_SELECT))

    def test_choosing_m1_then_m5_in_the_interface_is_saved_and_shown_without_restart(self):
        self.open_cc()
        self.page.select_option(TF_SELECT, "M1")
        self.wait_saved("M1")
        self.assertEqual(self.page.locator(TF_SELECT).input_value(), "M1")
        self.until("document.querySelector('[data-scope=\"cc\"] [data-tc=\"tfnote\"]').textContent.indexOf('M1 candle') >= 0")
        self.page.select_option(TF_SELECT, "M5")
        self.wait_saved("M5")
        self.assertEqual(self.page.locator(TF_SELECT).input_value(), "M5")
        self.until("document.querySelector('[data-scope=\"cc\"] [data-tc=\"tfnote\"]').textContent.indexOf('M5 candle') >= 0")

    def test_the_choice_survives_a_page_reload(self):
        self.open_cc()
        self.page.select_option(TF_SELECT, "M1")
        self.wait_saved("M1")
        self.open_cc()                                                                    # reload the page: the saved choice, not a default, is shown
        self.assertEqual(self.page.locator(TF_SELECT).input_value(), "M1")


if __name__ == "__main__":
    unittest.main()
