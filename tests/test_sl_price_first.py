"""Stop-loss calculator, PRICE-FIRST mode: SL price -> risk amount, plus limit judging. Pure maths.

    python3 -m unittest tests.test_sl_price_first -v
"""

import unittest

from tests.sl_fixtures import INSTRUMENTS, spec
from trademate.risk.sl_calculator import judge_limit, risk_from_sl

EUR, EUR_DIGITS, EUR_ENTRY = INSTRUMENTS["EURUSD"]


class KnownValues(unittest.TestCase):
    def test_buy(self):
        r = risk_from_sl("buy", 1.10012, 0.10, 1.09512, EUR, digits=5, currency="USD")
        self.assertTrue(r.valid, r.message)
        self.assertAlmostEqual(r.risk_amount, 50.0, places=6)
        self.assertEqual(r.stop_ticks, 500)
        self.assertAlmostEqual(r.loss_per_lot, 500.0, places=6)
        self.assertEqual(r.currency, "USD")

    def test_sell(self):
        r = risk_from_sl("sell", 1.10000, 0.10, 1.10500, EUR, digits=5)
        self.assertTrue(r.valid, r.message)
        self.assertAlmostEqual(r.risk_amount, 50.0, places=6)

    def test_risk_scales_linearly_with_volume_and_distance(self):
        one = risk_from_sl("buy", 1.10012, 0.10, 1.09512, EUR, digits=5).risk_amount
        self.assertAlmostEqual(risk_from_sl("buy", 1.10012, 0.20, 1.09512, EUR, digits=5).risk_amount, 2 * one, places=6)
        self.assertAlmostEqual(risk_from_sl("buy", 1.10012, 0.10, 1.09012, EUR, digits=5).risk_amount, 2 * one, places=6)

    def test_account_currency_is_only_a_label_money_comes_from_the_broker_tick_value(self):
        ngn = spec("EURUSD", 0.00001, 1500.0)                              # an NGN account: 1 tick = NGN 1500 per lot
        r = risk_from_sl("buy", 1.10012, 0.10, 1.09512, ngn, digits=5, currency="NGN")
        self.assertAlmostEqual(r.risk_amount, 75_000.0, places=4)
        self.assertEqual(r.currency, "NGN")

    def test_gold_and_index(self):
        g, gd, ge = INSTRUMENTS["XAUUSD"]
        self.assertAlmostEqual(risk_from_sl("buy", ge, 0.01, 1995.30, g, digits=gd).risk_amount, 5.0, places=6)
        i, idg, ie = INSTRUMENTS["US30"]
        self.assertAlmostEqual(risk_from_sl("sell", ie, 1.0, 38050.0, i, digits=idg).risk_amount, 50.0, places=6)   # 500 ticks * 0.1


class DirectionRules(unittest.TestCase):
    def test_wrong_side_is_invalid_not_flipped(self):
        for side, sl, word in (("buy", 1.11000, "BELOW"), ("buy", 1.10012, "BELOW"), ("sell", 1.09000, "ABOVE"), ("sell", 1.10000, "ABOVE")):
            r = risk_from_sl(side, 1.10000 if side == "sell" else 1.10012, 0.1, sl, EUR, digits=5)
            self.assertFalse(r.valid, (side, sl))
            self.assertIn(word, r.message)
            self.assertIsNone(r.risk_amount)

    def test_buy_and_sell_are_symmetric(self):
        for name, (sp, digits, entry) in INSTRUMENTS.items():
            d = sp.tick_size * 200
            b = risk_from_sl("buy", entry, 0.10, round(entry - d, digits), sp, digits=digits)
            s = risk_from_sl("sell", entry, 0.10, round(entry + d, digits), sp, digits=digits)
            self.assertTrue(b.valid and s.valid, (name, b.message, s.message))
            self.assertAlmostEqual(b.risk_amount, s.risk_amount, places=8, msg=name)


class PricePrecision(unittest.TestCase):
    def test_off_grid_price_is_refused_with_the_nearest_valid_stop_never_changed_silently(self):
        r = risk_from_sl("buy", 1.10012, 0.10, 1.095125, EUR)
        self.assertFalse(r.valid)
        self.assertIn("steps of", r.message)
        self.assertAlmostEqual(r.nearest_valid_stop, 1.09513, places=5)     # toward the entry: never wider than typed
        self.assertEqual(r.stop_loss, 1.095125)                             # what the user typed is echoed unchanged
        self.assertIsNone(r.risk_amount)
        s = risk_from_sl("sell", 1.10000, 0.10, 1.105005, EUR)
        self.assertAlmostEqual(s.nearest_valid_stop, 1.10500, places=5)

    def test_too_many_decimals_for_the_symbol_is_refused(self):
        r = risk_from_sl("buy", 100.0, 0.1, 99.0000000001, spec("X", 1e-10, 1.0), digits=2)
        self.assertFalse(r.valid)

    def test_coarse_tick_grids_work(self):
        gold5 = spec("XAUUSD5", 0.5, 50.0)                                  # a symbol whose tick (0.5) is coarser than its digits
        self.assertTrue(risk_from_sl("buy", 2000.5, 0.01, 1995.0, gold5, digits=2).valid)
        self.assertFalse(risk_from_sl("buy", 2000.5, 0.01, 1995.2, gold5, digits=2).valid)

    def test_stop_closer_than_the_broker_minimum_is_refused(self):
        r = risk_from_sl("buy", 1.10012, 0.10, 1.10005, EUR, digits=5, stops_level=10, point=0.00001)
        self.assertFalse(r.valid)
        self.assertIn("closer than this broker allows", r.message)

    def test_bad_inputs(self):
        for sl in (None, 0, -1, float("nan"), float("inf"), "1.09", True):
            r = risk_from_sl("buy", 1.10012, 0.1, sl, EUR, digits=5)
            self.assertFalse(r.valid, sl)
            self.assertIn("Stop-loss price", r.message)
            self.assertIsNone(r.risk_amount)
        self.assertFalse(risk_from_sl("buy", 1.10012, 0.015, 1.095, EUR).valid)
        self.assertFalse(risk_from_sl("buy", 1.10012, 0.1, 1.095, spec("X", 1e-5, None)).valid)


class LimitJudging(unittest.TestCase):
    def result(self, risk):
        r = risk_from_sl("buy", 1.10012, 0.10, 1.09512, EUR, digits=5, currency="USD")
        r.risk_amount = risk
        return r

    def test_within_and_above_the_per_trade_limit(self):
        ok = judge_limit(self.result(50.0), 10_000.0, 1.0)                   # limit = 100
        self.assertTrue(ok["checked"] and ok["ok"])
        self.assertAlmostEqual(ok["risk_percent"], 0.5)
        self.assertAlmostEqual(ok["max_risk_amount"], 100.0)
        over = judge_limit(self.result(150.0), 10_000.0, 1.0)
        self.assertFalse(over["ok"])
        self.assertIn("Risk Engine would reject", over["message"])

    def test_exactly_at_the_limit_is_within(self):
        self.assertTrue(judge_limit(self.result(100.0), 10_000.0, 1.0)["ok"])

    def test_limit_is_not_claimed_when_it_cannot_be_checked(self):
        for equity, pct in ((None, 1.0), (0, 1.0), (-5, 1.0), (10_000.0, None)):
            j = judge_limit(self.result(50.0), equity, pct)
            self.assertFalse(j["checked"])
            self.assertIsNone(j["ok"])
        bad = risk_from_sl("buy", 1.10012, 0.10, 1.2, EUR)
        self.assertFalse(judge_limit(bad, 10_000.0, 1.0)["checked"])


if __name__ == "__main__":
    unittest.main()
