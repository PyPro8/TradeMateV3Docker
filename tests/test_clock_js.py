"""Runs the Node checks for static/js/clocks.js (Eastern Time EST/EDT, configured zone, candle countdown, DOM mounter) and checks
the page markup: exactly one local clock, exactly one Eastern clock, one countdown per chart toolbar, no duplicates.
If Node.js is missing the JavaScript test is SKIPPED (reported as skipped, never as passed)."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed; run `node tests/clock_test.js` where it is.")
class ClocksJsTest(unittest.TestCase):
    def test_clock_js_checks_pass(self):
        run = subprocess.run(["node", str(ROOT / "tests" / "clock_test.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("all", run.stdout)
        self.assertIn("checks passed", run.stdout)


class ClockMarkupTest(unittest.TestCase):
    def html(self):
        return app.test_client().get("/").get_data(as_text=True)

    def test_exactly_one_of_each_clock(self):
        html = self.html()
        self.assertEqual(len(re.findall(r'data-clock="local"', html)), 1)
        self.assertEqual(len(re.findall(r'data-clock="eastern"', html)), 1)
        self.assertIn("NEW YORK", html)
        self.assertIn("LOCAL", html)

    def test_one_countdown_per_chart_toolbar(self):
        html = self.html()
        self.assertEqual(len(re.findall(r'data-role="candle-countdown"', html)), html.count("data-terminal>"))
        self.assertGreaterEqual(html.count('data-role="candle-countdown"'), 1)

    def test_script_is_loaded_before_app_js(self):
        html = self.html()
        self.assertLess(html.index("js/clocks.js"), html.index("js/app.js"))

    def test_clock_script_is_served(self):
        r = app.test_client().get("/static/js/clocks.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"TradeMateClocks", r.data)
        r.close()

    def test_configured_ui_timezone_reaches_the_page(self):
        import dataclasses
        new = dataclasses.replace(app_module.settings, ui=dataclasses.replace(app_module.settings.ui, timezone="Asia/Manila"))
        with mock.patch.object(app_module, "settings", new):
            html = app.test_client().get("/").get_data(as_text=True)
        self.assertIn('data-clock-zone-config="Asia/Manila"', html)


if __name__ == "__main__":
    unittest.main()
