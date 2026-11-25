"""The Strategy page's multi-timeframe panel in a real browser (headless Chromium) against a STUB broker with fixed, hand-checked
candles. No real MT5, no order, no live account. Skipped (not passed) when Playwright / Chromium is missing.

    python3 -m unittest tests.test_browser_mtf -v
"""

import dataclasses
import time as real_time
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.browser_support import BrowserBase, PLAYWRIGHT, QuoteStub
from tests.mtf_fixtures import NOW, buy_rows
from trademate.broker.base import Candle
from trademate.config import StrategySettings
from trademate.errors import BrokerUnavailableError

ROOT = Path(__file__).resolve().parent.parent


class FixedClock:
    """app.py's `time` module with time() frozen at the fixture's 'now'."""

    @staticmethod
    def time():
        return NOW

    def __getattr__(self, name):
        return getattr(real_time, name)


class MtfBroker(QuoteStub):
    def __init__(self):
        super().__init__()
        self.mtf = buy_rows()

    def get_candles(self, symbol, timeframe, count=300):
        tf = timeframe.upper()
        if tf == "M1":
            t = int(NOW - 1) // 60 * 60
            return [Candle(t - 60 * (9 - i), 100.0, 100.5, 99.5, 100.0) for i in range(10)]
        if symbol == "EURUSD" and tf in self.mtf:
            return [Candle(*c) for c in self.mtf[tf]]
        raise BrokerUnavailableError("no candles")


def patches(cls):
    return [mock.patch.object(app_module, "time", FixedClock()),
            mock.patch.object(app_module, "settings", dataclasses.replace(app_module.settings, strategy=StrategySettings(trigger_lookback=24)))]


class StrategyPanelTest(BrowserBase):
    broker_factory = MtfBroker
    extra_patches = (patches,)

    def open_panel(self):
        self.page.goto(self.base + "/#strategy")
        self.until("document.querySelector('#mtf-symbol') && document.querySelector('#mtf-symbol').options.length > 0")
        self.page.select_option("#mtf-symbol", "EURUSD")

    def orders_sent(self):
        """Every POST except the app's own watch-list sync (/api/watch), which every page load makes and which trades nothing."""
        return [r for r in self.requests if r[0] == "POST" and "/api/watch" not in r[1]]

    def test_the_panel_shows_a_buy_with_levels_and_gates_and_sends_nothing(self):
        self.broker.connected = True
        self.open_panel()
        self.page.click("#mtf-run")
        self.until("document.querySelector('#mtf-out .an-status[data-decision=BUY]')")
        text = self.page.inner_text("#mtf-out")
        for needle in ("Structural stop loss", "Take profit 1", "Setup invalidation", "Qualification gates", "PASS trigger", "H4",
                       "Reversal stage"):
            self.assertIn(needle, text)
        for tf in ("H4", "H1", "M15", "M5"):
            self.assertIn(tf, text)
        self.assertEqual(self.orders_sent(), [], "the panel must never POST anything (no order, no risk call)")
        forbidden = ("/api/order", "/api/pending", "/api/position", "/api/risk/evaluate", "/api/risk/size", "/api/risk/sl")
        self.assertEqual([r for r in self.requests if any(f in r[1] for f in forbidden)], [])
        self.assertTrue([r for r in self.requests if "/api/strategy/mtf" in r[1]])

    def test_a_disconnected_broker_shows_wait_and_unavailable_data(self):
        self.addCleanup(setattr, self.broker, "connected", True)
        self.open_panel()                                   # symbols are loaded while connected ...
        self.broker.connected = False                       # ... then the broker goes away
        self.page.click("#mtf-run")
        self.until("document.querySelector('#mtf-out .an-status[data-decision=WAIT]')")
        self.assertIn("unavailable", self.page.inner_text("#mtf-out"))
        self.assertEqual(self.page.locator("#mtf-out >> text=Proposed levels").count(), 0)

    def test_the_script_never_builds_html_from_server_text(self):
        src = (ROOT / "static" / "js" / "mtf.js").read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
