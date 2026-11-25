"""Phase 5 / Jigsaw 5.1 tests: risk-based position sizing. No order is ever placed.

Part 1 tests the pure engine with hand-computed broker specs (EURUSD, XAUUSD, USDJPY, cross pair).
Part 2 tests the Flask route against tests/fake_bridge.py. Run:  python -m unittest tests.test_risk_sizing -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.fake_bridge import FakeBridge
from trademate.broker import create_adapter
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.risk import RiskLimits, SymbolSpec, TradeRequest, calculate_position_size

EURUSD = SymbolSpec("EURUSD", 1e-5, 1.0, 1.0, 0.01, 100.0, 0.01, 4)
XAUUSD = SymbolSpec("XAUUSD", 0.01, 1.0, 1.0, 0.01, 50.0, 0.01, 4)        # 1 lot: $1 per 0.01 -> $100 per 1.00
USDJPY = SymbolSpec("USDJPY", 0.001, 0.6667, 0.6667, 0.01, 100.0, 0.01, 4)
CROSS = SymbolSpec("CROSS", 1e-5, 0.9, 0.85, 0.01, 100.0, 0.01, 4)        # loss uses its own tick value
STEP_05 = SymbolSpec("STEP", 0.01, 1.0, 1.0, 0.5, 100.0, 0.5, 4)          # min 0.5, step 0.5
EQUITY = 52000.0


def size(spec, direction, entry, sl, pct=1.0, equity=EQUITY, limits=None, symbol=None):
    return calculate_position_size(equity, TradeRequest(symbol or (spec.name if spec else "X"), direction, entry, sl, pct),
                                   spec, limits or RiskLimits(max_risk_percent=5.0))


class BasicCalculationTest(unittest.TestCase):
    def test_1_basic_gold(self):
        r = size(XAUUSD, "buy", 2650, 2640)                      # 1% of 52000 = 520; 10.00 move = $1000/lot
        self.assertEqual(r.validation_status, "VALID")
        self.assertAlmostEqual(r.risk_amount, 520.0)
        self.assertAlmostEqual(r.stop_distance, 10.0)
        self.assertAlmostEqual(r.loss_per_lot, 1000.0)
        self.assertAlmostEqual(r.calculated_volume, 0.52)
        self.assertAlmostEqual(r.normalized_volume, 0.52)
        self.assertAlmostEqual(r.estimated_loss, 520.0)

    def test_1_basic_eurusd_and_jpy(self):
        r = size(EURUSD, "sell", 1.1000, 1.1050, equity=10000)   # $100 risk, 50 pips = $500/lot -> 0.20
        self.assertAlmostEqual(r.normalized_volume, 0.20)
        self.assertAlmostEqual(r.estimated_loss, 100.0, places=6)
        r = size(USDJPY, "buy", 150.0, 149.5, equity=10000)      # 500 ticks * 0.6667 = 333.35/lot -> 0.29 (floored)
        self.assertAlmostEqual(r.normalized_volume, 0.29)
        self.assertLessEqual(r.estimated_loss, r.risk_amount)
        self.assertGreater(r.estimated_loss, r.risk_amount * 0.95)

    def test_estimated_loss_never_exceeds_allowed_risk(self):
        for entry, sl in ((2650, 2641.37), (2650, 2633.91), (2650, 2649.13)):
            r = size(XAUUSD, "buy", entry, sl)
            if r.validation_status == "VALID":
                self.assertLessEqual(r.estimated_loss, r.risk_amount + 1e-9)

    def test_loss_uses_loss_tick_value_for_cross_pairs(self):
        r = size(CROSS, "buy", 1.1000, 1.0990, equity=10000)     # 100 ticks * 0.85 = 85/lot; risk 100 -> 1.17
        self.assertAlmostEqual(r.loss_per_lot, 85.0)
        self.assertAlmostEqual(r.normalized_volume, 1.17)

    def test_falls_back_to_plain_tick_value(self):
        spec = SymbolSpec("OLD", 1e-5, 1.0, None, 0.01, 100.0, 0.01, 4)
        self.assertAlmostEqual(size(spec, "buy", 1.1, 1.099, equity=10000).loss_per_lot, 100.0)

    def test_2_lower_risk_gives_smaller_size(self):
        self.assertLess(size(XAUUSD, "buy", 2650, 2640, 0.5).normalized_volume, size(XAUUSD, "buy", 2650, 2640, 1.0).normalized_volume)
        self.assertAlmostEqual(size(XAUUSD, "buy", 2650, 2640, 0.5).normalized_volume, 0.26)

    def test_3_higher_risk_gives_bigger_size(self):
        self.assertAlmostEqual(size(XAUUSD, "buy", 2650, 2640, 2.0).normalized_volume, 1.04)

    def test_3_higher_risk_is_limited_by_broker_maximum(self):
        r = size(SymbolSpec("G", 0.01, 1.0, 1.0, 0.01, 1.0, 0.01, 4), "buy", 2650, 2640, 2.0)
        self.assertEqual(r.validation_status, "REJECTED")
        self.assertIn("maximum", r.rejection_reason)

    def test_4_wider_sl_gives_smaller_size(self):
        self.assertLess(size(XAUUSD, "buy", 2650, 2620).normalized_volume, size(XAUUSD, "buy", 2650, 2640).normalized_volume)

    def test_5_tighter_sl_gives_bigger_size(self):
        self.assertGreater(size(XAUUSD, "buy", 2650, 2645).normalized_volume, size(XAUUSD, "buy", 2650, 2640).normalized_volume)

    def test_sell_is_symmetric(self):
        self.assertAlmostEqual(size(XAUUSD, "sell", 2640, 2650).normalized_volume, 0.52)


class ValidationTest(unittest.TestCase):
    def rejected(self, result, text):
        self.assertEqual(result.validation_status, "REJECTED")
        self.assertIsNone(result.normalized_volume)
        self.assertIn(text.lower(), result.rejection_reason.lower())

    def test_invalid_risk(self):
        self.rejected(size(XAUUSD, "buy", 2650, 2640, 0), "greater than 0")
        self.rejected(size(XAUUSD, "buy", 2650, 2640, -1), "greater than 0")
        self.rejected(size(XAUUSD, "buy", 2650, 2640, None), "missing")
        self.rejected(size(XAUUSD, "buy", 2650, 2640, 6.0), "above the configured maximum")
        self.rejected(size(XAUUSD, "buy", 2650, 2640, float("nan")), "missing")

    def test_missing_or_equal_prices(self):
        self.rejected(size(XAUUSD, "buy", None, 2640), "entry")
        self.rejected(size(XAUUSD, "buy", 2650, None), "stop-loss")
        self.rejected(size(XAUUSD, "buy", 2650, 2650), "cannot equal")

    def test_6_invalid_buy(self):
        self.rejected(size(XAUUSD, "buy", 2650, 2660), "below the entry")

    def test_7_invalid_sell(self):
        self.rejected(size(XAUUSD, "sell", 2650, 2640), "above the entry")

    def test_invalid_direction(self):
        self.rejected(size(XAUUSD, "hold", 2650, 2640), "BUY or SELL")

    def test_unavailable_symbol_or_spec(self):
        self.rejected(size(None, "buy", 2650, 2640, symbol="NOPE"), "no mt5 specification")
        self.rejected(size(SymbolSpec("Z", None, 1.0, 1.0, 0.01, 1.0, 0.01), "buy", 2650, 2640), "tick size")
        self.rejected(size(SymbolSpec("Z", 0.01, None, None, 0.01, 1.0, 0.01), "buy", 2650, 2640), "tick value")
        self.rejected(size(SymbolSpec("Z", 0.01, 1.0, 1.0, None, 1.0, 0.01), "buy", 2650, 2640), "volume limits")
        self.rejected(size(SymbolSpec("Z", 0.01, 1.0, 1.0, 0.01, 100.0, 0.01, 0), "buy", 2650, 2640), "disabled")
        self.rejected(size(SymbolSpec("Z", 0.01, 1.0, 1.0, 0.01, 100.0, 0.01, 1), "sell", 2650, 2640 + 20), "long")

    def test_equity_unavailable(self):
        self.rejected(size(XAUUSD, "buy", 2650, 2640, equity=None), "equity")
        self.rejected(size(XAUUSD, "buy", 2650, 2640, equity=0), "equity")

    def test_8_below_minimum_is_rejected_not_rounded_up(self):
        r = size(XAUUSD, "buy", 2650, 2000, equity=1000)         # 650.00 move = $65,000/lot; $10 risk -> 0.00015 lots
        self.rejected(r, "below the broker minimum")
        self.assertLess(r.calculated_volume, 0.01)

    def test_8_above_maximum_is_rejected(self):
        self.rejected(size(XAUUSD, "buy", 2650, 2649.9, equity=1e6, limits=RiskLimits(5.0)), "broker maximum")

    def test_8_trademate_per_order_cap(self):
        r = size(XAUUSD, "buy", 2650, 2640, limits=RiskLimits(5.0, max_volume=0.4))
        self.rejected(r, "max_volume")

    def test_8_step_rounding_down_on_the_brokers_grid(self):
        r = size(STEP_05, "buy", 100.0, 99.0, equity=10000)      # 100 ticks=$100/lot, risk $100 -> exactly 1.0
        self.assertAlmostEqual(r.normalized_volume, 1.0)
        r = size(STEP_05, "buy", 100.0, 99.0, equity=17000)      # risk $170 -> 1.7 -> 1.5 (floored to step)
        self.assertAlmostEqual(r.calculated_volume, 1.7)
        self.assertAlmostEqual(r.normalized_volume, 1.5)
        self.assertLessEqual(r.estimated_loss, r.risk_amount)

    def test_8_step_grid_starts_at_minimum(self):
        spec = SymbolSpec("ODD", 0.01, 1.0, 1.0, 0.10, 10.0, 0.05, 4)   # valid: 0.10, 0.15, 0.20 ...
        r = size(spec, "buy", 100.0, 99.0, equity=17400)                # 1.74 lots/100 = ... risk 174, 100/lot -> 1.74
        self.assertAlmostEqual(r.normalized_volume, 1.70)

    def test_no_float_drift_on_step(self):
        r = size(SymbolSpec("F", 0.01, 1.0, 1.0, 0.01, 100.0, 0.01, 4), "buy", 100.0, 99.0, equity=3000)  # 0.3 exact
        self.assertEqual(r.normalized_volume, 0.3)

    def test_result_is_json_ready(self):
        d = size(XAUUSD, "buy", 2650, 2640).to_dict()
        for key in ("symbol", "direction", "entry_price", "stop_loss", "stop_distance", "risk_percent", "risk_amount",
                    "calculated_volume", "normalized_volume", "estimated_loss", "validation_status", "rejection_reason"):
            self.assertIn(key, d)


class RiskRouteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bridge = FakeBridge(Path(tmp.name)).start()
        self.addCleanup(self.bridge.stop)
        settings = Settings(app=AppSettings(data_dir=Path(tmp.name)), mt5=Mt5Settings(data_dir=Path(tmp.name)),
                            trading=TradingSettings(command_timeout_seconds=2.0, confirm_timeout_seconds=1.5))
        from trademate.risk import RiskRules, RiskRulesStore
        store = RiskRulesStore(Path(tmp.name) / "risk_rules.json", RiskRules(max_risk_per_trade_percent=2.0))
        for patcher in (mock.patch.object(app_module, "adapter", create_adapter(settings)),
                        mock.patch.object(app_module, "risk_store", store)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = app.test_client()

    def ask(self, **kw):
        body = {"symbol": "EURUSD", "direction": "buy", "entry": 1.1000, "stop_loss": 1.0950, "risk_percent": 1.0, **kw}
        return self.client.post("/api/risk/size", json=body).get_json()

    def test_uses_real_account_equity_and_symbol_spec(self):
        res = self.ask()["result"]                               # fake account: equity 1000 -> $10; 50 pips = $500/lot
        self.assertEqual(res["validation_status"], "VALID")
        self.assertAlmostEqual(res["risk_amount"], 10.0)
        self.assertAlmostEqual(res["normalized_volume"], 0.02)
        self.assertAlmostEqual(res["estimated_loss"], 10.0, places=6)

    def test_lower_and_higher_risk_through_the_route(self):
        self.assertAlmostEqual(self.ask(risk_percent=0.5)["result"]["normalized_volume"], 0.01)   # $5 / $500
        self.assertAlmostEqual(self.ask(risk_percent=2.0)["result"]["normalized_volume"], 0.04)   # $20 / $500

    def test_invalid_buy_and_sell_rejected(self):
        self.assertEqual(self.ask(direction="buy", entry=2650, stop_loss=2660)["result"]["validation_status"], "REJECTED")
        self.assertEqual(self.ask(direction="sell", entry=2650, stop_loss=2640)["result"]["validation_status"], "REJECTED")

    def test_risk_above_configured_max_rejected(self):
        self.assertIn("maximum", self.ask(risk_percent=50)["result"]["rejection_reason"])

    def test_unknown_symbol_gives_a_clear_reason(self):
        res = self.ask(symbol="NOPE")["result"]
        self.assertEqual(res["validation_status"], "REJECTED")
        self.assertIn("NOPE", res["rejection_reason"])

    def test_text_inputs_from_the_form_are_accepted_and_garbage_rejected(self):
        self.assertEqual(self.ask(entry="1.1000", stop_loss="1.0950", risk_percent="1")["result"]["validation_status"], "VALID")
        self.assertEqual(self.ask(entry="abc")["result"]["validation_status"], "REJECTED")

    def test_sends_no_order_and_changes_nothing(self):
        self.ask()
        self.assertEqual([c["cmd"] for c in self.bridge.commands_seen if c["cmd"] in ("open", "modify", "close")], [])
        self.assertEqual(self.bridge.positions, {})

    def test_cross_origin_post_blocked(self):
        r = self.client.post("/api/risk/size", json={}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
