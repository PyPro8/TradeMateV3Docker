"""Stop-loss calculator, RISK-FIRST mode: risk amount -> SL price. Pure maths, no broker, no Flask.

    python3 -m unittest tests.test_sl_risk_first -v
"""

import random
import unittest

from tests.sl_fixtures import INSTRUMENTS, spec
from trademate.risk.engine import loss_per_lot_at_stop
from trademate.risk.sl_calculator import risk_from_sl, sl_from_risk

EUR, EUR_DIGITS, EUR_ENTRY = INSTRUMENTS["EURUSD"]


class KnownValues(unittest.TestCase):
    def test_buy_exact_fit(self):
        r = sl_from_risk("buy", 1.10012, 0.10, 50.0, EUR, digits=5, currency="USD")
        self.assertTrue(r.valid, r.message)
        self.assertAlmostEqual(r.stop_loss, 1.09512, places=5)       # 500 ticks below
        self.assertAlmostEqual(r.risk_amount, 50.0, places=6)
        self.assertEqual(r.stop_ticks, 500)
        self.assertAlmostEqual(r.difference, 0.0, places=9)
        self.assertEqual(r.currency, "USD")

    def test_sell_exact_fit_is_the_mirror(self):
        r = sl_from_risk("sell", 1.10000, 0.10, 50.0, EUR, digits=5, currency="USD")
        self.assertTrue(r.valid, r.message)
        self.assertAlmostEqual(r.stop_loss, 1.10500, places=5)
        self.assertAlmostEqual(r.risk_amount, 50.0, places=6)

    def test_requested_and_final_risk_are_different_fields(self):
        r = sl_from_risk("buy", 1.10012, 0.10, 50.05, EUR, digits=5)
        self.assertAlmostEqual(r.requested_risk, 50.05)
        self.assertAlmostEqual(r.risk_amount, 50.0, places=6)         # the grid can only LOWER the risk
        self.assertAlmostEqual(r.difference, 0.05, places=6)
        self.assertTrue(any("less than the 50.05 requested" in n for n in r.notes))

    def test_gold(self):
        spec_, digits, entry = INSTRUMENTS["XAUUSD"]
        r = sl_from_risk("buy", entry, 0.01, 5.0, spec_, digits=digits)
        self.assertAlmostEqual(r.stop_loss, 1995.30, places=2)
        self.assertAlmostEqual(r.risk_amount, 5.0, places=6)


class NeverExceedsTheRequest(unittest.TestCase):
    def test_final_risk_is_never_above_the_requested_risk_on_any_instrument_or_side(self):
        rng = random.Random(7)
        checked = 0
        for name, (sp, digits, entry) in INSTRUMENTS.items():
            for side in ("buy", "sell"):
                for _ in range(400):
                    vol = round(rng.choice([0.01, 0.02, 0.05, 0.1, 0.37, 1.0, 2.5]), 2)
                    risk = round(rng.uniform(0.5, 800), rng.choice([0, 1, 2, 4]))
                    r = sl_from_risk(side, entry, vol, risk, sp, digits=digits)
                    if not r.valid:
                        continue
                    checked += 1
                    self.assertLessEqual(r.risk_amount, risk + 1e-9, (name, side, vol, risk))
                    self.assertGreaterEqual(r.difference, -1e-9)
                    # at most ONE tick of money is left on the table (the grid step), never more
                    one_tick = loss_per_lot_at_stop(sp, sp.tick_size) * vol
                    self.assertLess(risk - r.risk_amount, one_tick + 1e-9, (name, side, vol, risk))
                    # protective side of the entry, on the tick grid
                    self.assertTrue(r.stop_loss < entry if side == "buy" else r.stop_loss > entry)
        self.assertGreater(checked, 2000)

    def test_round_trip_price_mode_agrees_exactly(self):
        rng = random.Random(11)
        for name, (sp, digits, entry) in INSTRUMENTS.items():
            for side in ("buy", "sell"):
                for _ in range(100):
                    vol = round(rng.choice([0.01, 0.1, 0.5, 1.0]), 2)
                    risk = rng.uniform(1, 500)
                    a = sl_from_risk(side, entry, vol, risk, sp, digits=digits)
                    if not a.valid:
                        continue
                    b = risk_from_sl(side, entry, vol, a.stop_loss, sp, digits=digits)
                    self.assertTrue(b.valid, (name, side, b.message))
                    self.assertAlmostEqual(b.risk_amount, a.risk_amount, places=9)
                    self.assertLessEqual(b.risk_amount, risk + 1e-9)


class SymmetryAndRounding(unittest.TestCase):
    def test_buy_and_sell_give_mirrored_distances_and_equal_risk(self):
        for name, (sp, digits, entry) in INSTRUMENTS.items():
            b = sl_from_risk("buy", entry, 0.10, 37.5, sp, digits=digits)
            s = sl_from_risk("sell", entry, 0.10, 37.5, sp, digits=digits)
            self.assertTrue(b.valid and s.valid, name)
            self.assertAlmostEqual(b.stop_distance, s.stop_distance, places=8, msg=name)
            self.assertAlmostEqual(b.risk_amount, s.risk_amount, places=9, msg=name)
            self.assertAlmostEqual(entry - b.stop_loss, s.stop_loss - entry, places=8, msg=name)

    def test_buy_stop_is_rounded_toward_the_entry_never_away(self):
        r = sl_from_risk("buy", 1.10012, 0.10, 50.009, EUR, digits=5)    # 500.09 ticks: exact stop is a hair below the grid
        self.assertLess(r.stop_loss, 1.10012)
        self.assertGreaterEqual(r.stop_loss, 1.10012 - 500.09 * 1e-5 - 1e-12)

    def test_cross_pair_uses_the_loss_tick_value_not_the_gain_value(self):
        sp, digits, entry = INSTRUMENTS["EURGBP"]
        r = sl_from_risk("buy", entry, 0.10, 127.0, sp, digits=digits)
        self.assertEqual(r.stop_ticks, 1000)                             # 127 / (1.27 * 0.1) = 1000 ticks (gain value 1.26 would give 1007)
        self.assertAlmostEqual(r.risk_amount, 127.0, places=6)


class Refusals(unittest.TestCase):
    def test_risk_smaller_than_one_tick_is_refused_never_enlarged(self):
        r = sl_from_risk("buy", 1.10012, 0.10, 0.05, EUR, digits=5)
        self.assertFalse(r.valid)
        self.assertIn("single tick", r.message)
        self.assertIsNone(r.stop_loss)
        self.assertIsNone(r.risk_amount)

    def test_broker_minimum_stop_distance_is_respected_and_risk_is_not_increased(self):
        r = sl_from_risk("buy", 1.10012, 0.10, 0.5, EUR, digits=5, stops_level=10, point=0.00001, currency="USD")
        self.assertFalse(r.valid)
        self.assertIn("at least 10 points", r.message)
        self.assertIn("Nothing was increased", r.message)
        ok = sl_from_risk("buy", 1.10012, 0.10, 1.0, EUR, digits=5, stops_level=10, point=0.00001)
        self.assertTrue(ok.valid)                                          # exactly the minimum distance is allowed

    def test_absurdly_large_risk_cannot_put_the_stop_at_or_below_zero(self):
        r = sl_from_risk("buy", 1.10012, 0.01, 10_000_000, EUR, digits=5)
        self.assertFalse(r.valid)
        self.assertIn("zero", r.message)

    def test_bad_inputs_give_a_reason_and_no_numbers(self):
        cases = [
            ("hold", 1.1, 0.1, 10, EUR, "BUY or SELL"), (None, 1.1, 0.1, 10, EUR, "BUY or SELL"),
            ("buy", None, 0.1, 10, EUR, "Entry"), ("buy", 0, 0.1, 10, EUR, "Entry"), ("buy", float("nan"), 0.1, 10, EUR, "Entry"),
            ("buy", 1.1, 0, 10, EUR, "Volume"), ("buy", 1.1, -1, 10, EUR, "Volume"), ("buy", 1.1, True, 10, EUR, "Volume"),
            ("buy", 1.1, float("inf"), 10, EUR, "Volume"), ("buy", 1.1, 0.1, 0, EUR, "Risk amount"),
            ("buy", 1.1, 0.1, -5, EUR, "Risk amount"), ("buy", 1.1, 0.1, None, EUR, "Risk amount"),
            ("buy", 1.1, 0.1, "10", EUR, "Risk amount"), ("buy", 1.1, 0.1, 10, None, "specification"),
            ("buy", 1.1, 0.015, 10, EUR, "multiple of"), ("buy", 1.1, 500, 10, EUR, "outside"),
        ]
        for side, entry, vol, risk, sp, word in cases:
            r = sl_from_risk(side, entry, vol, risk, sp, digits=5)
            self.assertFalse(r.valid, (side, entry, vol, risk))
            self.assertIn(word, r.message, (side, entry, vol, risk))
            self.assertIsNone(r.stop_loss)
            self.assertIsNone(r.risk_amount)

    def test_missing_instrument_data_is_reported_not_guessed(self):
        for sp, word in ((spec("X", None, 1.0), "tick size"), (spec("X", 0.01, None), "tick value"), (spec("X", 0.01, 0.0), "tick value"),
                         (spec("X", 0.0, 1.0), "tick size")):
            r = sl_from_risk("buy", 100.0, 0.1, 10, sp)
            self.assertFalse(r.valid)
            self.assertIn(word, r.message)

    def test_trade_mode_restrictions(self):
        self.assertIn("disabled", sl_from_risk("buy", 1.1, 0.1, 10, spec("X", 1e-5, 1.0, trade_mode=0)).message)
        self.assertIn("close-only", sl_from_risk("buy", 1.1, 0.1, 10, spec("X", 1e-5, 1.0, trade_mode=3)).message)
        self.assertIn("BUY (long) only", sl_from_risk("sell", 1.1, 0.1, 10, spec("X", 1e-5, 1.0, trade_mode=1)).message)
        self.assertIn("SELL (short) only", sl_from_risk("buy", 1.1, 0.1, 10, spec("X", 1e-5, 1.0, trade_mode=2)).message)


if __name__ == "__main__":
    unittest.main()
