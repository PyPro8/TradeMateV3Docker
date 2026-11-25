import os
import unittest


class Env(unittest.TestCase):
    def test_real_order_opt_in_is_scrubbed_and_test_run_marked(self):
        self.assertIsNone(os.environ.get("TRADEMATE_REAL_DEMO_ORDER"))
        self.assertEqual(os.environ.get("TRADEMATE_TEST_RUN"), "1")
