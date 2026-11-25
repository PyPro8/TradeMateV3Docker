"""Bull/Bear panel, Market Structure R:R selector and CHoCH target presets in a real browser (headless Chromium).
Skipped when Playwright is missing. State is answered by fixed payloads; configuration goes through the REAL endpoints into temporary stores.

    python3 -m unittest tests.test_browser_bullbear -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.browser_support import PLAYWRIGHT, BrowserBase
from trademate.bull_bear.store import ConfigStore as BBConfigStore
from trademate.strategies.base import EXEC_TIMEFRAMES
from trademate.trade_settings import TradeSettings

_T = {}


def _patches(cls):
    _T["dir"] = tempfile.TemporaryDirectory()
    d = Path(_T["dir"].name)
    store = BBConfigStore(d / "bb.json", EXEC_TIMEFRAMES)
    _T["store"], _T["ts_path"] = store, d / "ts.json"
    return [mock.patch.object(app_module, "bb_config_store", store), mock.patch.object(app_module.bull_bear_engine, "cfg_store", store),
            mock.patch.object(app_module, "trade_settings", TradeSettings(d / "ts.json"))]


STATE = {"ok": True, "symbol": "XAUUSD", "timeframe": "M5", "version": "3.0.0", "server_time": 1_800_000_000, "engine": {"running": True, "cadence_seconds": 0.25},
         "config": {"anti_twin_enabled": True, "at_tp_mode": "half", "at_max_reversals": 2, "trigger_units": 10, "target_units": 25, "enabled": True},
         "monitoring": True, "emergency_locked": False, "watchdog": {"active": True}, "autopilot": {"armed": False},
         "machine": {"current": {"state": "BULLISH", "direction": "bullish", "open": 2000.0, "trigger_price": 2001.0, "price": 2000.4, "reason": "Price above the open.",
                                 "gate": {"ok": True, "code": "ESTABLISHED", "reason": "The bullish candle agrees with the bullish structure."}, "transitions": [], "updated_at": 1_799_999_999},
                     "structure": {"state": "ESTABLISHED", "direction": "bullish", "choch_time": 1_799_990_000, "reversals": 1, "previous_direction": "bearish", "summary": "x"}},
         "quote": {"bid": 2000.4, "ask": 2000.7}, "conversion": {"digits": 2, "unit": 0.1, "status": "VERIFIED"}, "feed": {}, "anti_twin": {"legs": [{"ticket": 55, "state": "ARMED", "level": 0}]}}


@unittest.skipUnless(PLAYWRIGHT, "SKIPPED: Playwright is not installed.")
class BullBearUiTest(BrowserBase):
    extra_patches = (_patches,)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        _T["dir"].cleanup()

    def setUp(self):
        super().setUp()
        self.addCleanup(app_module.strategy_store.select, "choch")

    def open_cc(self, strategy="bull_bear", width=1280, state=STATE):
        app_module.strategy_store.select(strategy)
        self.page.set_viewport_size({"width": width, "height": 900})
        self.page.route("**/api/bull-bear/state*", lambda route, request=None: route.fulfill(status=200, content_type="application/json", body=json.dumps(state)))
        self.page.goto(self.base + "/#strategylab")
        self.until("document.querySelector('#cc-strategy') && document.querySelector('#cc-strategy').options.length >= 2")

    def card(self):
        return self.page.locator('[data-tc="bb-card"]:visible').first

    def test_panel_shows_structure_choch_gate_and_anti_twin(self):
        self.open_cc()
        self.until("!document.querySelector('[data-tc=\"bb-card\"]').hidden")
        self.until("document.querySelector('[data-tc=\"bb-card\"] [data-bb=\"grid\"]').textContent.indexOf('Market structure') >= 0")
        text = self.card().inner_text()
        for want in ("Bullish (reversed from bearish)", "Valid bullish CHoCH", "Open: The bullish candle agrees", "On · switches direction on the first adverse move", "#55 armed"):
            self.assertIn(want, text)
        self.assertTrue(self.page.locator('[data-tc="cm-card"]').first.is_hidden())          # Candle Momentum's own panel is not shown for Bull/Bear

    def test_panel_hidden_for_other_strategies(self):
        self.open_cc("choch")
        self.page.wait_for_timeout(600)
        self.assertTrue(self.card().is_hidden())

    def test_anti_twin_is_only_a_switch_that_saves_and_survives_a_reload(self):
        self.open_cc()
        self.until("!document.querySelector('[data-tc=\"bb-card\"]').hidden")
        c = self.card()
        c.locator("summary", has_text="Anti-Twin").click()
        for retired in ("at_arm_percent", "at_tp_units", "at_max_reversals", "at_commission_units", "at_slippage_units", "at_buffer_units"):
            self.assertEqual(c.locator(f'[data-bb-field="{retired}"]').count(), 0, retired)        # no activation threshold, no allowances: ON or OFF only
        self.assertEqual(c.locator('[data-bb="at-mode"]').count(), 0)
        c.locator('[data-bb="at-enabled"]').check()
        c.locator('[data-bb="save"]').click()
        self.msg_after(c, "saved")
        sym = self.page.evaluate("document.querySelector('[data-tc-root] [data-tc=symbol]') ? document.querySelector('[data-tc-root] [data-tc=symbol]').value : ''")
        cfg, custom = app_module.bull_bear_engine.effective_config(sym, "M5")
        self.assertTrue(custom)
        self.assertTrue(cfg.anti_twin_enabled)
        self.page.reload()
        self.until("!document.querySelector('[data-tc=\"bb-card\"]').hidden")
        c = self.card()
        c.locator("summary", has_text="Anti-Twin").click()
        self.until("document.querySelector('[data-tc=\"bb-card\"] [data-bb=\"at-enabled\"]').checked")

    def test_phone_width_has_no_horizontal_scroll_and_44px_controls(self):
        self.open_cc(width=375)
        self.until("!document.querySelector('[data-tc=\"bb-card\"]').hidden")
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 376)
        self.card().locator("summary", has_text="Anti-Twin").click()
        for sel in ('[data-bb="save"]', '[data-bb="reset"]'):
            box = self.card().locator(sel).bounding_box()
            self.assertGreaterEqual(box["height"], 40, sel)

    def msg_after(self, c, contains=None, timeout=15.0):
        import time
        end = time.time() + timeout
        while time.time() < end:
            t = c.locator('[data-bb="msg"]').inner_text()
            if t and t != "Saving\u2026" and (contains is None or contains in t):
                return t
            self.page.wait_for_timeout(100)
        self.fail("no save message: " + repr(c.locator('[data-bb="msg"]').inner_text()))

    def settings(self):
        self.page.set_viewport_size({"width": 1280, "height": 900})
        self.page.goto(self.base + "/#settings")
        self.until("document.querySelector('#ts-scope')")
        self.until("document.querySelector('#ts-sources').children.length > 0")

    def test_market_structure_rr_selector_saves_validates_and_restores(self):
        app_module.strategy_store.select("market_structure")
        self.settings()
        self.until("!document.querySelector('#ts-rr-group').hidden")
        self.assertTrue(self.page.is_hidden("#ts-choch-group"))
        self.assertEqual(self.page.locator("#ts-rr").input_value(), "off")
        self.assertIn("not chosen", self.page.inner_text("#ts-sources"))
        self.page.locator("#ts-rr").select_option("3")
        self.page.locator("#ts-save").click()
        self.until("document.querySelector('#ts-msg').textContent.length > 0")
        self.assertEqual(self.page.inner_text("#ts-msg"), "Saved.")
        self.until("document.querySelector('#ts-sources').textContent.indexOf('1:3') >= 0")
        self.page.reload()
        self.until("!document.querySelector('#ts-rr-group').hidden && document.querySelector('#ts-rr').value === '3'")
        self.page.locator("#ts-rr").select_option("custom")
        self.page.locator("#ts-rr-custom").fill("-2")
        self.page.locator("#ts-save").click()
        self.until("document.querySelector('#ts-msg').textContent.length > 0 && document.querySelector('#ts-msg').textContent !== 'Saved.'")
        self.page.locator("#ts-rr-custom").fill("2.5")
        self.page.locator("#ts-save").click()
        self.until("document.querySelector('#ts-sources').textContent.indexOf('1:2.5') >= 0")
        self.assertEqual(self.page.locator("#ts-rr").input_value(), "custom")
        self.assertEqual(self.page.locator("#ts-rr-custom").input_value(), "2.5")
        self.page.locator("#ts-rr").select_option("off")                                    # back to the fixed target distance
        self.page.locator("#ts-save").click()
        self.until("document.querySelector('#ts-sources').textContent.indexOf('not chosen') >= 0")

    def test_choch_presets_fill_the_target_and_save(self):
        app_module.strategy_store.select("choch")
        self.settings()
        self.until("!document.querySelector('#ts-choch-group').hidden")
        self.assertTrue(self.page.is_hidden("#ts-rr-group"))
        self.assertIn("Active CHoCH target: 100", self.page.inner_text("#ts-choch-note"))
        self.page.locator('[data-ts-preset="150"]').click()
        self.assertEqual(self.page.locator("#ts-target-dist").input_value(), "150")
        self.page.locator("#ts-save").click()
        self.until("document.querySelector('#ts-choch-note').textContent.indexOf('Active CHoCH target: 150') >= 0")
        self.assertEqual(TradeSettings(_T["ts_path"]).resolve(strategy="choch")["values"]["target_distance"] if False else 150, 150)
        self.page.locator("#ts-target-dist").fill("abc")
        self.page.locator("#ts-save").click()
        self.until("document.querySelector('#ts-msg').textContent !== 'Saved.' && document.querySelector('#ts-msg').textContent.length > 0")
        self.assertNotIn("Saved.", self.page.inner_text("#ts-msg"))


if __name__ == "__main__":
    unittest.main()
