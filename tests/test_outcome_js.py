"""Runs the Node checks for static/js/outcome.js (how each execution outcome is shown) in the normal test run."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed; run `node tests/outcome_test.js` where it is.")
class OutcomeDisplayTest(unittest.TestCase):
    def test_outcome_js_checks_pass(self):
        run = subprocess.run(["node", str(ROOT / "tests" / "outcome_test.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("all checks passed", run.stdout)

    def test_page_loads_outcome_js_and_runwrite_uses_it(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("js/outcome.js", html)
        self.assertLess(html.index("js/outcome.js"), html.index("js/app.js"))      # loaded first
        self.assertIn("TradeMateOutcome.describe", app_js)


if __name__ == "__main__":
    unittest.main()
