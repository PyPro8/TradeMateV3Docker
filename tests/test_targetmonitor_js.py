"""Runs the Node checks for static/js/targetmonitor.js (Target Monitor model) as part of the normal test run."""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed; run `node tests/targetmonitor_test.js` where it is.")
class TargetMonitorModelTest(unittest.TestCase):
    def test_targetmonitor_js_checks_pass(self):
        run = subprocess.run(["node", str(ROOT / "tests" / "targetmonitor_test.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("all checks passed", run.stdout)


if __name__ == "__main__":
    unittest.main()
