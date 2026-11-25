"""Central instrument engine: every asset class, conversion consistency, verification, volume, Crash 100 convention."""
import time
import unittest
from types import SimpleNamespace as NS

from trademate.instruments import build_spec, classify


def info(name, digits, point, tick=None, tv=None, path="", vmin=0.01, vmax=100, vstep=0.01, bid=100.0, ask=100.5, **kw):
    return NS(name=name, digits=digits, point=point, tick_size=tick if tick is not None else point, tick_value=tv, tick_value_profit=None,
              tick_value_loss=None, path=path, description="", volume_min=vmin, volume_max=vmax, volume_step=vstep, contract_size=1, stops_level=0,
              freeze_level=0, currency_base="", currency_profit="USD", currency_margin="USD", bid=bid, ask=ask, **kw)


CASES = {   # name, digits, point, tick, tick_value, path, expected class, expected unit
    "EURUSD": (info("EURUSD", 5, 0.00001, 0.00001, 1.0, "Forex\\Majors"), "forex_major", 0.0001),
    "USDJPY": (info("USDJPY", 3, 0.001, 0.001, 0.0066, "Forex\\Majors"), "forex_jpy", 0.01),
    "XAUUSD": (info("XAUUSD", 2, 0.01, 0.01, 0.01, "Metals"), "metal", 0.10),
    "BTCUSD": (info("BTCUSD", 2, 0.01, 0.01, 0.01, "Crypto"), "crypto", 1.0),
    "US30": (info("US30", 1, 0.1, 0.1, 0.1, "Indices"), "index", 1.0),
    "NAS100": (info("NAS100", 1, 0.1, 0.1, 0.1, "Indices"), "index", 1.0),
    "Crash 500 Index": (info("Crash 500 Index", 3, 0.001, 0.001, 0.001, "Synthetic"), "synthetic", 1.0),
    "Boom 1000 Index": (info("Boom 1000 Index", 3, 0.001, 0.001, 0.001, "Synthetic"), "synthetic", 1.0),
}


def crash100(tv=0.001, tick=0.001, **kw):
    return info("Crash 100 Index", 3, 0.001, tick, tv, "Synthetic", vmin=0.01, vmax=50, vstep=0.01, **kw)


class AssetClasses(unittest.TestCase):
    def test_classification_and_units(self):
        for name, (i, cls, unit) in CASES.items():
            with self.subTest(name):
                self.assertEqual(classify(i.name, i.path), cls)
                s = build_spec(i)
                self.assertEqual(s.asset_class, cls)
                self.assertAlmostEqual(s.unit, unit, places=10)
                self.assertTrue(s.verified, s.problems)

    def test_user_examples(self):
        eur = build_spec(CASES["EURUSD"][0]); jpy = build_spec(CASES["USDJPY"][0]); xau = build_spec(CASES["XAUUSD"][0])
        btc = build_spec(CASES["BTCUSD"][0]); us30 = build_spec(CASES["US30"][0]); crash = build_spec(CASES["Crash 500 Index"][0])
        self.assertAlmostEqual(eur.price_to_units(1.08510 - 1.08500), 1.0, places=9)
        self.assertAlmostEqual(jpy.price_to_units(154.210 - 154.200), 1.0, places=9)
        self.assertAlmostEqual(xau.price_to_units(2650.20 - 2650.10), 1.0, places=9)       # 0.10 price = 1 unit
        self.assertAlmostEqual(btc.price_to_units(95510 - 95500), 10.0, places=9)
        self.assertAlmostEqual(us30.price_to_units(19860 - 19850), 10.0, places=9)
        self.assertAlmostEqual(crash.price_to_units(4510 - 4500), 10.0, places=9)

    def test_round_trip(self):
        for name, (i, _, _) in CASES.items():
            s = build_spec(i)
            for u in (1, 2.5, 10, 100):
                self.assertAlmostEqual(s.price_to_units(s.units_to_price(u)), u, places=9, msg=name)

    def test_gold_unit_override_to_0_01(self):
        s = build_spec(CASES["XAUUSD"][0], {"XAUUSD": 0.01})
        self.assertEqual((s.unit, s.unit_source), (0.01, "override"))
        self.assertAlmostEqual(s.units_to_price(10), 0.10)

    def test_unknown_class_is_unverified_not_guessed(self):
        s = build_spec(info("ACMECORP", 2, 0.01, 0.01, 0.01, "Stocks\\US"))
        self.assertEqual(s.asset_class, "unknown")
        self.assertFalse(s.verified)
        self.assertIsNone(s.unit)
        with self.assertRaises(ValueError):
            s.units_to_price(10)
        self.assertFalse(s.to_pip_definition().ok)

    def test_points_ticks_and_unit_are_distinct(self):
        s = build_spec(CASES["EURUSD"][0])                    # point 0.00001, unit 0.0001
        self.assertAlmostEqual(s.price_to_points(0.0001), 10.0)
        self.assertAlmostEqual(s.price_to_units(0.0001), 1.0)
        i = info("US30", 1, 0.1, tick=0.5, tv=0.5, path="Indices")  # tick coarser than point
        t = build_spec(i)
        self.assertAlmostEqual(t.price_to_ticks(5.0), 10.0)
        self.assertAlmostEqual(t.price_to_points(5.0), 50.0)


class Money(unittest.TestCase):
    def test_crash100_confirmed_convention(self):
        s = build_spec(crash100())                            # tick 0.001 worth 0.001 per lot => 1.00 per 1.00 price per lot
        self.assertTrue(s.verified, s.problems)
        for lots, per_pip in ((0.01, 0.01), (0.05, 0.05), (0.06, 0.06), (0.10, 0.10), (0.50, 0.50), (1.00, 1.00)):
            self.assertAlmostEqual(s.money_per_unit(lots), per_pip, places=9)
        self.assertAlmostEqual(s.money_per_unit(0.01) * 10, 0.10)
        self.assertAlmostEqual(s.money_per_unit(0.06) * 10, 0.60)
        self.assertAlmostEqual(s.money_per_unit(0.10) * 10, 1.00)
        self.assertAlmostEqual(s.money_for_distance(10.0, 0.06), 0.60)

    def test_crash100_convention_contradicted_by_broker_blocks_execution(self):
        s = build_spec(crash100(tv=0.002))                    # broker says 2.00 per pip per lot: user's convention NOT reproduced
        self.assertFalse(s.verified)
        self.assertTrue(any("NOT reproduced" in p for p in s.problems))

    def test_crash100_convention_not_applied_to_crash500(self):
        s = build_spec(info("Crash 500 Index", 3, 0.001, 0.001, 0.002, "Synthetic"))
        self.assertTrue(s.verified)
        self.assertAlmostEqual(s.money_per_unit(1.0), 2.0)    # uses the broker's own value, not the Crash 100 table
        self.assertIsNone(s.expected_money_per_unit_lot)

    def test_crash100_unverifiable_without_tick_value(self):
        self.assertFalse(build_spec(crash100(tv=None)).verified)

    def test_forex_money(self):
        s = build_spec(CASES["EURUSD"][0])                    # tick value 1.0 per 0.00001 per lot => 10 USD per pip per lot
        self.assertAlmostEqual(s.money_per_unit(1.0), 10.0)
        self.assertAlmostEqual(s.money_per_unit(0.1), 1.0)

    def test_loss_side_tick_value(self):
        i = info("USDJPY", 3, 0.001, 0.001, 0.0066, "Forex"); i.tick_value_loss = 0.0070
        s = build_spec(i)
        self.assertGreater(s.money_per_unit(1.0, loss=True), s.money_per_unit(1.0))

    def test_money_requires_tick_value(self):
        s = build_spec(info("US30", 1, 0.1, 0.1, None, "Indices"))
        self.assertFalse(s.verified)
        with self.assertRaises(ValueError):
            s.money_per_unit(0.1)


class Levels(unittest.TestCase):
    def test_targets_and_stops_never_closer_than_asked(self):
        s = build_spec(info("EURUSD", 5, 0.00001, 0.00005, 0.5, "Forex"))     # tick 5 points
        tp = s.target_price("buy", 1.08503, 10); sl = s.stop_price("buy", 1.08503, 10)
        self.assertGreaterEqual(tp - 1.08503, 0.0010 - 1e-12); self.assertGreaterEqual(1.08503 - sl, 0.0010 - 1e-12)
        tp = s.target_price("sell", 1.08503, 10); sl = s.stop_price("sell", 1.08503, 10)
        self.assertGreaterEqual(1.08503 - tp, 0.0010 - 1e-12); self.assertGreaterEqual(sl - 1.08503, 0.0010 - 1e-12)

    def test_crash_target_matches_display(self):
        s = build_spec(crash100())
        self.assertAlmostEqual(s.target_price("buy", 4500.0, 10), 4510.0)
        self.assertAlmostEqual(s.stop_price("sell", 4500.0, 10), 4510.0)

    def test_bad_inputs(self):
        s = build_spec(crash100())
        for bad in (0, -1, float("nan"), "x"):
            with self.assertRaises(ValueError):
                s.units_to_price(bad)
        with self.assertRaises(ValueError):
            s.target_price("hold", 1.0, 1)


class Volume(unittest.TestCase):
    def test_validation_never_rounds(self):
        s = build_spec(crash100())
        self.assertTrue(s.validate_volume(0.06)[0])
        self.assertTrue(s.validate_volume(0.01)[0])
        self.assertFalse(s.validate_volume(0.005)[0])
        self.assertFalse(s.validate_volume(51)[0])
        self.assertFalse(s.validate_volume(0.015)[0])
        self.assertFalse(s.validate_volume(0)[0])
        self.assertFalse(s.validate_volume(None)[0])

    def test_unknown_limits_fail_closed(self):
        s = build_spec(info("US30", 1, 0.1, 0.1, 0.1, "Indices", vmin=None, vmax=None, vstep=None))
        self.assertFalse(s.validate_volume(0.1)[0])
        self.assertFalse(s.verified)


class Verification(unittest.TestCase):
    def test_stale_spec_blocks(self):
        s = build_spec(crash100(), now=time.time() - 1000)
        self.assertTrue(any("stale" in r for r in s.blocking_reasons()))
        self.assertEqual(build_spec(crash100()).blocking_reasons(), [])

    def test_inconsistent_quotes_and_ticks(self):
        self.assertFalse(build_spec(crash100(bid=10, ask=9)).verified)
        self.assertFalse(build_spec(info("US30", 1, 0.1, 0.01, 0.1, "Indices")).verified)

    def test_unit_not_multiple_of_tick(self):
        s = build_spec(CASES["EURUSD"][0], {"EURUSD": 0.000075})
        self.assertFalse(s.verified)

    def test_bad_override(self):
        self.assertFalse(build_spec(CASES["EURUSD"][0], {"EURUSD": -1}).verified)

    def test_unit_usable_for_decisions_even_when_money_info_is_missing(self):
        s = build_spec(info("Crash 500 Index", 3, 0.001, 0.001, None, "Synthetic"))      # no tick value: cannot execute, can still evaluate
        self.assertFalse(s.verified)
        self.assertTrue(s.to_pip_definition().ok)

    def test_requested_symbol_used_when_broker_object_has_no_name(self):
        i = info("", 5, 0.00001, 0.00001, 1.0, "")
        self.assertEqual(build_spec(i, {"EURUSD": 0.0001}, symbol="EURUSD").unit, 0.0001)

    def test_pip_definition_adapter(self):
        p = build_spec(CASES["BTCUSD"][0]).to_pip_definition()
        self.assertTrue(p.ok); self.assertEqual(p.size, 1.0)


if __name__ == "__main__":
    unittest.main()
