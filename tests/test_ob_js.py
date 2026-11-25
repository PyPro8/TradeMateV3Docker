"""Runs the Node checks for static/js/ob.js (chart items for the order block overlay) inside the normal test run."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed; run `node tests/ob_js_test.js` where it is.")
class OrderBlockItemsTest(unittest.TestCase):
    def test_ob_js_checks_pass(self):
        run = subprocess.run(["node", str(ROOT / "tests" / "ob_js_test.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("all checks passed", run.stdout)


if __name__ == "__main__":
    unittest.main()
