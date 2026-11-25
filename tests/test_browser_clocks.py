"""Two clocks and the candle countdown in real headless Chromium (SKIPPED, not passed, when Playwright/Chromium is missing).

A read-only stub broker stands in for MT5: it has no order method, so nothing can be sent anywhere. Its broker clock is UTC+3 and its
newest candle is always the one that is open on that clock, so the expected countdown can be computed independently in Python.

    python3 -m unittest tests.test_browser_clocks -v
"""

import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from tests.browser_support import BrowserBase, QuoteStub
from trademate import brokertime
from trademate.broker.base import Candle
from trademate.errors import BrokerUnavailableError

OFFSET = 3 * 3600
SECS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}


class ClockStub(QuoteStub):
    def __init__(self):
        super().__init__()
        self.offset = OFFSET
        self.lag = 0                      # seconds the newest candle is behind the open one (market closed / no ticks)
        self.probe_fails = False

    def get_candles(self, symbol, timeframe, count=300):
        tf = timeframe.upper()
        if tf == "M1":                    # the server's clock probe
            if self.probe_fails:
                raise BrokerUnavailableError("MT5 did not answer.")
            return [Candle((int(time.time()) + self.offset) // 60 * 60, 1.0, 1.0, 1.0, 1.0)]
        rows = self.series_by[symbol][tf]
        secs = SECS[tf]
        last_open = (int(time.time()) + self.offset) // secs * secs - self.lag
        n = len(rows)
        return [Candle(last_open - (n - 1 - i) * secs, *r[1:]) for i, r in enumerate(rows)]


def remaining(tf, lag=0):
    secs = SECS[tf]
    server_now = time.time() + OFFSET
    return secs - (server_now % secs)


def to_seconds(text):
    parts = [int(p) for p in text.split(":")]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


class ClocksAndCountdownTest(BrowserBase):
    broker_factory = ClockStub

    def setUp(self):
        super().setUp()
        b = self.broker
        b.connected, b.snapshot_age, b.lag, b.probe_fails = True, 0.1, 0, False
        brokertime.DEFAULT_RESOLVER.reset()

    def tearDown(self):
        brokertime.DEFAULT_RESOLVER.reset()
        super().tearDown()

    def text(self, selector):
        return self.page.inner_text(selector).strip()

    def countdown(self):
        return self.page.evaluate("() => { const e = document.querySelector('#view-chart [data-role=candle-countdown]');"
                                  " return { state: e.getAttribute('data-state'), text: e.querySelector('[data-role=candle-countdown-text]').textContent }; }")

    def wait_countdown(self, state):
        self.until(f"document.querySelector('#view-chart [data-role=candle-countdown]').getAttribute('data-state') === '{state}'", timeout=20)
        return self.countdown()

    # ----------------------------------------------------------------------------------------------- clocks
    def test_two_labelled_clocks_exist_once_and_tick_without_reloading(self):
        self.open_chart()
        self.assertEqual(self.page.locator("[data-clock='local']").count(), 1)
        self.assertEqual(self.page.locator("[data-clock='eastern']").count(), 1)
        self.assertIn("LOCAL", self.text("#clock-local"))
        self.assertIn("NEW YORK", self.text("#clock-eastern"))
        self.assertRegex(self.text("#clock-eastern [data-clock-time]"), r"^\d\d:\d\d:\d\d$")
        self.assertIn(self.text("#clock-eastern [data-clock-zone]"), ("EST", "EDT"))
        first = self.text("#clock-eastern [data-clock-time]")
        self.page.evaluate("() => { window.__marker = 'same page'; }")
        self.page.wait_for_timeout(2300)
        self.assertNotEqual(self.text("#clock-eastern [data-clock-time]"), first, "the clock must advance")
        self.assertEqual(self.page.evaluate("() => window.__marker"), "same page", "the page was not reloaded")

    def test_eastern_time_matches_an_independent_conversion(self):
        self.open_chart()
        for _ in range(3):
            shown = self.text("#clock-eastern [data-clock-time]")
            now = time.time()
            ok = {datetime.fromtimestamp(now + d, ZoneInfo("America/New_York")).strftime("%H:%M:%S") for d in (-2, -1, 0, 1)}
            self.assertIn(shown, ok)
            zone = datetime.fromtimestamp(now, ZoneInfo("America/New_York")).strftime("%Z")
            self.assertEqual(self.text("#clock-eastern [data-clock-zone]"), zone)
            self.page.wait_for_timeout(700)

    def test_local_clock_uses_the_browsers_zone_and_is_labelled(self):
        self.open_chart()
        title = self.page.get_attribute("#clock-local", "title")
        zone = self.page.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone")
        self.assertIn(zone, title)
        self.assertIn("Eastern Time", self.page.get_attribute("#clock-eastern", "title"))

    # ----------------------------------------------------------------------------------------------- countdown
    def test_countdown_matches_the_broker_clock_and_counts_down(self):
        self.open_chart()
        self.wait_countdown("live")
        c1 = self.countdown()
        self.assertEqual(c1["state"], "live")
        self.assertLessEqual(abs(to_seconds(c1["text"]) - remaining("H1")), 3, c1)
        self.page.wait_for_timeout(2200)
        c2 = self.countdown()
        self.assertLess(to_seconds(c2["text"]), to_seconds(c1["text"]), "must count down")
        self.assertLessEqual(abs(to_seconds(c2["text"]) - remaining("H1")), 3, c2)

    def test_changing_timeframe_changes_the_countdown(self):
        self.open_chart()
        self.wait_countdown("live")
        self.page.click("#view-chart [data-tf='M15'] >> visible=true")
        self.until("window.TradeMateApp.selection().tf === 'M15'")
        self.page.wait_for_function(
            "() => { const t = document.querySelector('#view-chart [data-role=candle-countdown-text]').textContent;"
            " const p = t.split(':').map(Number); return p.length === 2 && p[0] * 60 + p[1] <= 900; }", timeout=20000)
        c = self.countdown()
        self.assertEqual(c["state"], "live")
        self.assertLessEqual(abs(to_seconds(c["text"]) - remaining("M15")), 4, c)

    def test_changing_symbol_never_shows_the_old_symbols_number(self):
        self.open_chart()
        self.wait_countdown("live")
        self.broker.streamed = ["EURUSD", "XAUUSD"]
        self.page.evaluate("() => window.TradeMateApp.openChart('XAUUSD', 'H1', {})")
        self.until("window.TradeMateApp.selection().symbol === 'XAUUSD'")
        c = self.wait_countdown("live")
        self.assertLessEqual(abs(to_seconds(c["text"]) - remaining("H1")), 4, c)

    def test_market_closed_shows_a_dash_not_a_number(self):
        self.broker.lag = 3 * 3600                       # the newest candle opened 3 hours ago and no new one has appeared
        self.open_chart()
        c = self.wait_countdown("closed")
        self.assertEqual(c["text"], "\u2014")
        self.assertIn("No new candle", self.page.get_attribute("#view-chart [data-role=candle-countdown]", "title"))

    def test_unknown_broker_time_shows_a_dash_not_a_guess(self):
        self.broker.probe_fails = True
        self.open_chart()
        c = self.wait_countdown("unavailable")
        self.assertEqual(c["text"], "\u2014")

    def test_disconnect_makes_the_countdown_stale(self):
        self.open_chart()
        self.wait_countdown("live")
        self.broker.connected = False
        c = self.wait_countdown("stale")
        self.assertEqual(c["text"], "\u2014")
        self.broker.connected = True

    def test_the_countdown_does_not_reload_the_chart_data(self):
        self.open_chart()
        self.wait_countdown("live")
        before = len([r for r in self.requests if "/api/candles" in r[1]])
        before_time = len([r for r in self.requests if "/api/market/time" in r[1]])
        self.page.wait_for_timeout(5000)
        candles = len([r for r in self.requests if "/api/candles" in r[1]]) - before
        times = len([r for r in self.requests if "/api/market/time" in r[1]]) - before_time
        self.assertLessEqual(candles, 2, "the 5-second candle poll only: the 1-second countdown must not fetch candles")
        self.assertLessEqual(times, 1, "the broker clock is fetched about every 30 seconds, not every second")


if __name__ == "__main__":
    unittest.main()
