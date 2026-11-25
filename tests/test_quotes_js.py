"""Runs the Node checks for static/js/quotes.js (live / stale / unavailable rules for the chart's bid/ask lines)."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed; run `node tests/quotes_test.js` where it is.")
class QuoteRulesTest(unittest.TestCase):
    def test_quotes_js_checks_pass(self):
        run = subprocess.run(["node", str(ROOT / "tests" / "quotes_test.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("all checks passed", run.stdout)

    def test_page_loads_quotes_js_before_app_js_and_chart_uses_only_the_live_state(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertLess(html.index("js/quotes.js"), html.index("js/app.js"))
        block = app_js[app_js.index("bid (RED) / ask (GREEN) quote lines"):app_js.index("// ---- HUD")]
        self.assertNotIn("sym.bid", block)                    # the lines never read a cached/extra symbol snapshot
        self.assertNotIn("sym.ask", block)
        self.assertNotIn("spread_points", block)              # spread is calculated from bid and ask


if __name__ == "__main__":
    unittest.main()
