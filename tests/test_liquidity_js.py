"""Runs the Node checks for static/js/liquidity.js (chart items for the liquidity overlay) inside the normal test run."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed; run `node tests/liquidity_js_test.js` where it is.")
class LiquidityItemsTest(unittest.TestCase):
    def test_liquidity_js_checks_pass(self):
        run = subprocess.run(["node", str(ROOT / "tests" / "liquidity_js_test.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("all checks passed", run.stdout)

    def test_page_wiring_and_separation_from_trade_and_quote_levels(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertLess(html.index("js/liquidity.js"), html.index("js/app.js"))
        block = app_js[app_js.index("function drawLiquidity"):app_js.index("function drawStructure")]
        for banned in ("draftLines", "drag", "/api/order", "sym.bid", "C.bid", "C.ask"):
            self.assertNotIn(banned, block)                        # not draggable, not an order level, not a quote line
        self.assertIn("LIQ_COLORS", app_js)
        for color in ("#FF5C5C", "#2FD38A", "#C4262E", "#117A45"):  # amber / cyan families only: never the bid (red) / ask (green) colours
            self.assertNotIn(color.lower(), app_js[app_js.index("var LIQ_COLORS"):app_js.index("var LIQ_COLORS") + 200].lower())


if __name__ == "__main__":
    unittest.main()
