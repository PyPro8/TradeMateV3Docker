"""The Candle Momentum live panel in a real browser (headless Chromium), served by the real Flask app.

The engine behind the panel is the real one, fed by the deterministic fake feed (tests/fake_mt5_bridge.py); the rest of the stack is the fake bridge. No real MT5,
no real order. Skipped (not passed) without Playwright / Chromium.

    python3 -m unittest tests.test_browser_candle_momentum -v
"""

import unittest
from unittest import mock

import app as app_module
from tests.browser_support import PLAYWRIGHT
from tests.fake_mt5_bridge import FakeClock, FakeFeed
from tests.test_browser_commandcentre import CommandCentreBase
from trademate.candle_momentum.diagnostics import EventLog
from trademate.candle_momentum.engine import CandleMomentumEngine
from trademate.candle_momentum.store import ConfigStore, StateStore
from trademate.strategies.base import EXEC_TIMEFRAMES


@unittest.skipUnless(PLAYWRIGHT, "SKIPPED: Playwright is not installed.")
class CandleMomentumPanelTest(CommandCentreBase):
    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.clock = FakeClock()
        self.feed = FakeFeed(self.clock)
        self.cfg_store = ConfigStore(Path(self._d.name) / "cfg.json", EXEC_TIMEFRAMES)
        self.engine = CandleMomentumEngine(self.feed, app_module.settings, self.cfg_store, StateStore(None), EventLog(None), hub=app_module.decision_hub, clock=self.clock, threaded=False)
        self.engine.start("EURUSD", "M5")
        self.feed._candle("EURUSD", "M5")
        for bid in (1.10000, 1.10040):
            self.feed.tick(bid)
            self.clock.advance(0.25)
            self.engine.step()
        for p in (mock.patch.object(app_module, "candle_engine", self.engine), mock.patch.object(app_module, "cm_config_store", self.cfg_store)):
            p.start()
            self.addCleanup(p.stop)
        app_module.selection_store.save(symbol="EURUSD", timeframe="M5")
        self.addCleanup(app_module.strategy_store.select, "choch")

    def card(self):
        return self.page.locator('[data-scope="cc"] [data-tc="cm-card"]')

    def row(self, label):
        return self.page.evaluate("""(label) => { const c = document.querySelector('[data-scope="cc"] [data-tc="cm-card"]');
            const r = Array.from(c.querySelectorAll('.tc-cm-row')).find(x => x.querySelector('dt').textContent === label); return r ? r.querySelector('dd').textContent : null; }""", label)

    def test_panel_is_hidden_for_other_strategies(self):
        self.open_cc()
        self.assertEqual(self.card().count(), 1)
        self.assertFalse(self.card().is_visible())

    def test_panel_shows_the_live_engine_state(self):
        app_module.strategy_store.select("candle_momentum")
        self.open_cc()
        self.until("document.querySelector('[data-scope=\"cc\"] [data-tc=\"cm-card\"]') && !document.querySelector('[data-scope=\"cc\"] [data-tc=\"cm-card\"]').hidden")
        self.until("document.querySelectorAll('[data-scope=\"cc\"] [data-tc=\"cm-card\"] .tc-cm-row').length >= 15")
        self.assertTrue(self.card().is_visible())
        self.assertEqual(self.row("Strategy"), "Candle Momentum")
        self.assertEqual(self.row("Candle open"), "1.10000")
        self.assertEqual(self.row("Trigger price"), "1.10100")
        self.assertEqual(self.row("Candle direction"), "bullish")
        self.assertEqual(self.row("Lifecycle state"), "Bullish")
        self.assertEqual(self.row("Distance remaining to trigger"), "6 units")
        self.assertEqual(self.row("Actual entry price"), "Not executed")
        self.assertIn("every 0.25 s", self.row("Engine"))
        self.assertIn("VERIFIED", self.row("Instrument conversion"))
        self.assertIn("Watchdog", self.card().inner_text())
        self.assertEqual(self.page.locator('[data-scope="cc"] [data-cm="chip"]').inner_text(), "Bullish")

    def test_main_decision_card_and_candle_card_agree_because_they_share_one_record(self):
        app_module.strategy_store.select("candle_momentum")
        self.feed.tick(1.10105)                                       # trigger crossed: a validated, executable signal
        self.clock.advance(0.25)
        self.engine.step()
        self.open_cc()
        self.until("document.querySelector('[data-scope=\"cc\"] [data-tc=\"badge\"]').textContent.trim() === 'BUY'")
        self.until("document.querySelector('[data-scope=\"cc\"] [data-cm=\"chip\"]').textContent === 'Risk Engine pending'")
        self.assertEqual(self.page.locator('[data-scope="cc"] [data-tc="badge"]').inner_text().strip(), "BUY")
        self.assertEqual(self.row("Lifecycle state"), "Risk Engine pending")
        # the opportunity then disappears (price falls back inside the trigger before anything executes): BOTH cards must leave BUY together
        self.feed.tick(1.10050)
        self.clock.advance(0.25)
        self.engine.step()
        self.until("document.querySelector('[data-scope=\"cc\"] [data-tc=\"badge\"]').textContent.trim() !== 'BUY'", )
        self.until("document.querySelector('[data-scope=\"cc\"] [data-cm=\"chip\"]').textContent !== 'Risk Engine pending'")
        self.assertNotEqual(self.page.locator('[data-scope="cc"] [data-tc="badge"]').inner_text().strip(), "BUY")

    def test_a_price_far_beyond_the_trigger_is_still_proposed_not_missed(self):
        app_module.strategy_store.select("candle_momentum")
        self.feed.tick(1.10180)                                       # 8 pips beyond the trigger: formerly MISSED by the percentage rule
        self.clock.advance(0.25)
        self.engine.step()
        self.open_cc()
        self.until("document.querySelector('[data-scope=\"cc\"] [data-cm=\"chip\"]').textContent === 'Risk Engine pending'")
        self.assertNotIn("MISSED", self.page.locator('[data-scope="cc"] [data-cm="why"]').inner_text())
        self.assertEqual(self.page.locator('[data-scope="cc"] [data-tc="badge"]').inner_text().strip(), "BUY")

    def test_settings_save_through_the_panel_and_reach_the_engine_config(self):
        app_module.strategy_store.select("candle_momentum")
        self.open_cc()
        self.until("document.querySelector('[data-scope=\"cc\"] [data-cm-field=\"trigger_units\"]').value !== ''")
        self.page.locator('[data-scope="cc"] .tc-cm-details').first.locator("summary").click()
        self.page.locator('[data-scope="cc"] [data-cm-field="trigger_units"]').fill("5")
        self.page.locator('[data-scope="cc"] [data-cm-field="target_units"]').fill("12")
        self.page.locator('[data-scope="cc"] [data-cm="save"]').click()
        self.until("document.querySelector('[data-scope=\"cc\"] [data-cm=\"msg\"]').textContent.includes('saved')")
        cfg, custom = self.cfg_store.get(self.engine.effective_config("EURUSD", "M5")[0])
        self.assertEqual((cfg.trigger_units, cfg.target_units, custom), (5.0, 12.0, True))
        self.until("document.querySelector('[data-scope=\"cc\"] [data-tc=\"cm-card\"] .tc-cm-row dd') !== null")
        self.until("Array.from(document.querySelectorAll('[data-scope=\"cc\"] .tc-cm-row')).some(r => r.textContent.includes('Trigger distance') && r.textContent.includes('5 units'))")

    def test_an_invalid_setting_is_refused_with_a_message(self):
        app_module.strategy_store.select("candle_momentum")
        self.open_cc()
        self.until("document.querySelector('[data-scope=\"cc\"] [data-cm-field=\"trigger_units\"]').value !== ''")
        self.page.locator('[data-scope="cc"] .tc-cm-details').first.locator("summary").click()
        self.page.locator('[data-scope="cc"] [data-cm-field="trigger_units"]').fill("0")
        self.page.locator('[data-scope="cc"] [data-cm="save"]').click()
        self.until("document.querySelector('[data-scope=\"cc\"] [data-cm=\"msg\"]').textContent.includes('above 0')")
        self.assertEqual(self.engine.effective_config("EURUSD", "M5")[0].trigger_units, 10.0)

    def test_panel_is_usable_on_a_phone_width_without_horizontal_scroll(self):
        app_module.strategy_store.select("candle_momentum")
        self.open_cc(width=390, height=800)
        self.until("document.querySelectorAll('[data-scope=\"cc\"] [data-tc=\"cm-card\"] .tc-cm-row').length >= 15")
        overflow = self.page.evaluate("() => { const c = document.querySelector('[data-scope=\"cc\"] [data-tc=\"cm-card\"]'); return c.scrollWidth - c.clientWidth; }")
        self.assertLessEqual(overflow, 1)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
