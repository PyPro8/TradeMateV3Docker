"""Execution-cost (spread) policy. Pure tests: no broker, nothing is ever sent."""
import unittest

from trademate.execution_cost import SPREAD_INVALID, SPREAD_TOO_WIDE, assess_spread


class ExecutionCostTests(unittest.TestCase):
    def test_reproduction_of_the_reported_rejection_spread_12_stop_11(self):
        # The report: "The spread 12 exceeds 0.25 x the live stop distance (2.75)". 2.75 / 0.25 = 11 = the live stop distance.
        # Bid, ask, entry and stop are all PRICES, so the old comparison was unit-consistent: the spread was genuinely larger than the whole stop.
        entry_ask, bid = 30012.0, 30000.0               # spread 12 price units
        stop = entry_ask - 11.0                          # stop distance 11
        old_limit = 0.25 * abs(entry_ask - stop)
        self.assertAlmostEqual(old_limit, 2.75)
        r = assess_spread("buy", entry_ask, stop, entry_ask + 10.0, bid, entry_ask, pip_size=1.0)
        self.assertEqual([c for c, _ in r["problems"]], [SPREAD_TOO_WIDE])
        self.assertIn("whole stop distance", r["problems"][0][1])
        self.assertIn("not a unit mismatch", r["problems"][0][1])
        self.assertAlmostEqual(r["facts"]["spread_units"], 12.0)
        self.assertAlmostEqual(r["facts"]["stop_distance_units"], 11.0)

    def test_the_old_rule_would_have_blocked_a_workable_trade_but_the_new_policy_allows_it(self):
        ask, bid = 30012.0, 30009.0                      # spread 3 vs stop 11 and target 10: 27% of the stop, 30% of the target
        r = assess_spread("buy", ask, ask - 11.0, ask + 10.0, bid, ask, pip_size=1.0)
        self.assertEqual(r["problems"], [])
        self.assertGreater(0.25 * 11.0, 2.75 - 1e-9)     # the old limit was 2.75: 3 > 2.75 was rejected before

    def test_spread_as_large_as_the_target_blocks_even_with_a_wide_stop(self):
        ask, bid = 1.10012, 1.10002
        r = assess_spread("buy", ask, ask - 0.0050, ask + 0.0010, bid, ask, pip_size=0.0001)   # spread 1 pip, target 10 pips -> fine
        self.assertEqual(r["problems"], [])
        r = assess_spread("buy", ask, ask - 0.0050, ask + 0.0001, bid, ask, pip_size=0.0001)   # target 1 pip = the spread
        self.assertEqual(len(r["problems"]), 1)
        self.assertIn("whole target distance", r["problems"][0][1])

    def test_low_spread_forex_has_no_problem_and_no_warning(self):
        r = assess_spread("sell", 1.10000, 1.10100, 1.09000, 1.10000, 1.10001, pip_size=0.0001)
        self.assertEqual((r["problems"], r["warnings"]), ([], []))

    def test_sell_side_is_symmetric(self):
        bid, ask = 30000.0, 30012.0
        r = assess_spread("sell", bid, bid + 11.0, bid - 10.0, bid, ask, pip_size=1.0)
        self.assertEqual([c for c, _ in r["problems"]], [SPREAD_TOO_WIDE])

    def test_inconsistent_quote_is_invalid_not_guessed(self):
        r = assess_spread("buy", 1.1, 1.0, 1.2, 1.1002, 1.1000)
        self.assertEqual([c for c, _ in r["problems"]], [SPREAD_INVALID])

    def test_optional_user_ratio_is_labelled_as_the_users_own(self):
        r = assess_spread("buy", 100.0, 90.0, 120.0, 99.0, 100.0, user_max_fraction=0.05)
        self.assertEqual(len(r["problems"]), 1)
        self.assertIn("Your own spread limit", r["problems"][0][1])
        self.assertEqual(assess_spread("buy", 100.0, 90.0, 120.0, 99.0, 100.0)["problems"], [])

    def test_a_half_stop_spread_is_allowed_but_warned(self):
        r = assess_spread("buy", 100.0, 90.0, 120.0, 95.0, 100.0)
        self.assertEqual(r["problems"], [])
        self.assertTrue(r["warnings"])


if __name__ == "__main__":
    unittest.main()
