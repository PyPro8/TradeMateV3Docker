"""Runs the Node checks for static/js/labgreeting.js (time-of-day greeting, welcome-back rule, tolerant storage, mounter).
If Node.js is missing the JavaScript test is SKIPPED (reported as skipped, never as passed)."""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed; run `node tests/labgreeting_test.js` where it is.")
class LabGreetingJsTest(unittest.TestCase):
    def test_labgreeting_js_checks_pass(self):
        run = subprocess.run(["node", str(ROOT / "tests" / "labgreeting_test.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("checks passed", run.stdout)

    def test_the_module_touches_no_market_data_or_orders(self):
        src = (ROOT / "static" / "js" / "labgreeting.js").read_text(encoding="utf-8")
        for bad in ("fetch(", "XMLHttpRequest", "/api/", "innerHTML", "eval("):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
