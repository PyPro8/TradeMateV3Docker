"""Runs the Node checks for static/js/slcalc.js (the two-way stop-loss panel's logic) inside the normal test run."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed; run `node tests/slcalc_test.js` where it is.")
class SlPanelLogicTest(unittest.TestCase):
    def test_slcalc_js_checks_pass(self):
        run = subprocess.run(["node", str(ROOT / "tests" / "slcalc_test.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("all checks passed", run.stdout)

    def test_panel_is_wired_into_the_page_and_the_order_path(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        for role in ("sl-box", "sl-risk", "sl-out", "data-sl-mode=\"risk\"", "data-sl-mode=\"price\"", "data-sl-dir=\"buy\""):
            self.assertIn(role, html)
        self.assertLess(html.index("js/slcalc.js"), html.index("js/app.js"))
        self.assertIn("root._slCtl.gate(side)", app_js)                  # a risk-mode stop must pass the gate before any order is sent
        self.assertIn("TradeMateSL.attach", app_js)

    def test_the_panel_script_builds_no_html_from_server_text(self):
        js = (ROOT / "static" / "js" / "slcalc.js").read_text(encoding="utf-8")
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, js)


if __name__ == "__main__":
    unittest.main()
