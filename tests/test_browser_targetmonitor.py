"""The Dashboard's curved Target Monitor in a real browser (headless Chromium), with Autopilot's status answered by a fixed payload.
It must show real figures only, say so when there is no profit target, survive missing / failing data, work in both themes at phone width,
and never send anything but GETs. Skipped when Playwright is missing.

    python3 -m unittest tests.test_browser_targetmonitor -v
"""

import json
import unittest

from tests.browser_support import BrowserBase

BASE = {"ok": True, "state": "running", "armed": True, "mode": "profit", "mode_label": "Profit target", "value": 10000, "net_pnl": 7500, "currency": "NGN",
        "paused": False, "emergency_stopped": False, "trades": [], "events": [], "open_trades": 0, "completed_runs": 0, "progress": None}


class TargetMonitorTest(BrowserBase):
    def open(self, payload, status=200, width=1280, theme="dark"):
        self.page.set_viewport_size({"width": width, "height": 900})
        body = json.dumps(payload)
        self.page.route("**/api/autopilot/status*", lambda route, request=None: route.fulfill(status=status, content_type="application/json", body=body))
        self.page.goto(self.base + "/#dashboard")
        self.page.evaluate(f"document.documentElement.setAttribute('data-theme','{theme}')")
        self.until("document.querySelector('#target-monitor').dataset.kind !== 'loading'")

    def text(self, key):
        return self.page.inner_text(f'#target-monitor [data-tm="{key}"]')

    def test_positive_progress_shows_the_real_numbers_and_the_arc(self):
        self.open(BASE)
        self.assertIn("7,500.00 NGN", self.text("value"))
        self.assertIn("TARGET: 10,000.00 NGN", self.text("target"))
        self.assertEqual(self.text("headline"), "75% COMPLETE")
        self.assertIn("2,500.00 NGN REMAINING", self.text("remaining"))
        self.assertEqual(self.text("status"), "RUNNING")
        self.until("document.querySelector('#target-monitor .tm-prog').style.strokeDasharray.indexOf('75') === 0")
        self.assertEqual(self.page.get_attribute("#target-monitor [data-tm=meter]", "aria-valuenow"), "75")
        self.assertEqual(self.errors, [])

    def test_negative_profit_keeps_the_arc_empty_and_says_so_in_words(self):
        self.open({**BASE, "net_pnl": -1500, "state": "paused", "paused": True})
        self.assertIn("-1,500.00 NGN", self.text("value"))
        self.assertIn("BELOW START", self.text("headline"))
        self.assertEqual(self.text("status"), "PAUSED")
        self.assertEqual(self.page.evaluate("parseFloat(document.querySelector('#target-monitor .tm-prog').style.strokeDasharray)"), 0)
        self.assertEqual(self.page.get_attribute("#target-monitor", "data-negative"), "true")

    def test_reached_and_zero_progress(self):
        self.open({**BASE, "net_pnl": 10400, "state": "finished", "armed": False})
        self.assertEqual(self.text("headline"), "TARGET REACHED")
        self.assertEqual(self.text("remaining"), "TARGET MET")
        self.assertEqual(self.page.get_attribute("#target-monitor", "data-reached"), "true")

    def test_zero_progress(self):
        self.open({**BASE, "net_pnl": 0})
        self.assertEqual(self.text("headline"), "0% COMPLETE")
        self.assertIn("10,000.00 NGN REMAINING", self.text("remaining"))

    def test_another_stop_condition_means_no_target_and_none_is_invented(self):
        self.open({**BASE, "mode": "runs", "mode_label": "Number of runs", "value": 5})
        self.assertIn("No profit target", self.text("headline"))
        self.assertIn("TARGET: NOT SET", self.text("target"))
        self.assertEqual(self.text("remaining"), "")
        self.assertIn("Number of runs", self.text("detail"))

    def test_zero_target_is_reported_not_divided(self):
        self.open({**BASE, "value": 0})
        self.assertIn("zero or invalid", self.text("headline"))
        self.assertNotIn("NaN", self.page.inner_text("#target-monitor"))
        self.assertNotIn("Infinity", self.page.inner_text("#target-monitor"))

    def test_unreachable_autopilot_data_is_shown_as_unavailable(self):
        self.open({"ok": False}, status=500)
        self.assertEqual(self.text("status"), "DATA UNAVAILABLE")

    def test_account_balance_is_never_used_as_session_profit(self):
        self.open({**BASE, "net_pnl": 0})
        self.assertIn("0.00 NGN", self.text("value"))
        self.assertNotIn("1000", self.text("value").replace(",", ""))     # the stub account holds 1000 USD: it must not appear here

    def test_a_disconnected_broker_is_flagged(self):
        self.addCleanup(setattr, self.broker, "connected", True)
        self.open(BASE)
        self.broker.connected = False
        self.until("document.getElementById('mt5-state').textContent !== 'CONNECTED'")
        self.page.evaluate("TradeMateTargetMonitor.instance.refresh()")
        self.until("/broker is disconnected/i.test(document.querySelector('#target-monitor [data-tm=detail]').textContent)")

    def test_both_themes_at_320px_have_no_overflow_and_are_read_only(self):
        for theme in ("dark", "light"):
            self.requests.clear()
            self.open(BASE, width=320, theme=theme)
            self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth - window.innerWidth"), 0)
            box = self.page.locator("#target-monitor .tm-gauge").bounding_box()
            self.assertGreater(box["width"], 250)
            self.assertEqual([r for r in self.requests if r[0] != "GET" and "/api/watch" not in r[1]], [], "the monitor must never send anything but GETs")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
