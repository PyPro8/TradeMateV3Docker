"""Strategy-to-Risk hand-off tests. No Flask server or broker writes are used."""
import unittest
from types import SimpleNamespace
from unittest import mock

from trademate.risk.engine import AccountRiskState
from trademate.risk.position_sizing import SymbolSpec
from trademate.risk.rules import RiskRules
from trademate.trade_pipeline import review_strategy_proposal


class Adapter:
    def __init__(self, bid=1.10000, ask=1.10012):
        self.info = SimpleNamespace(name="EURUSD", bid=bid, ask=ask, tick_size=0.00001,
                                    tick_value=1.0, tick_value_loss=1.0, tick_value_profit=1.0,
                                    volume_min=0.01, volume_max=100.0, volume_step=0.01, trade_mode=4)
    def get_symbol_spec(self, symbol):
        if symbol != "EURUSD":
            raise AssertionError("unexpected symbol")
        return self.info


def strategy(decision="BUY", sl=1.0950, tp=1.1100):
    return {"ok": True, "result": {"symbol": "EURUSD", "decision": decision, "rule_id": "test-rule",
            "selected_setup_family": "trend_continuation", "stop_loss": {"price": sl},
            "take_profit_1": {"price": tp} if tp is not None else None}}


class StrategyTradePipelineTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Adapter()
        self.state = AccountRiskState(equity=1000.0, balance=1000.0, currency="USD", start_of_day_balance=1000.0,
                                      todays_closed_pl=0.0, floating_pl=0.0, positions=(), day_start_source="test")
        self.rules = RiskRules(max_risk_per_trade_percent=1.0, max_daily_loss_percent=3.0,
                               max_total_active_risk_percent=3.0, max_positions=3)
        self.patcher = mock.patch("trademate.trade_pipeline.gather_state", return_value=self.state)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def review(self, result):
        return review_strategy_proposal(self.adapter, result, self.rules, 10.0, 1.0)

    def test_qualified_buy_passes_risk_review_without_sending_order(self):
        result = self.review(strategy("BUY"))
        self.assertTrue(result["approved"], result)
        self.assertEqual(result["stage"], "ready_for_confirmation")
        self.assertEqual(result["proposal"]["side"], "buy")
        self.assertGreater(result["proposal"]["volume"], 0)
        self.assertFalse(result["order_sent"])
        self.assertTrue(result["requires_fresh_review_before_execution"])

    def test_qualified_sell_uses_bid_and_sell_levels(self):
        result = self.review(strategy("SELL", sl=1.1050, tp=1.0900))
        self.assertTrue(result["approved"], result)
        self.assertEqual(result["proposal"]["side"], "sell")
        self.assertEqual(result["proposal"]["entry_reference"], self.adapter.info.bid)
        self.assertFalse(result["order_sent"])

    def test_watch_and_wait_never_become_trade_proposals(self):
        for decision in ("WATCH", "WAIT", None):
            with self.subTest(decision=decision):
                result = self.review(strategy(decision))
                self.assertFalse(result["approved"])
                self.assertEqual(result["stage"], "not_qualified")
                self.assertNotIn("proposal", result)
                self.assertFalse(result["order_sent"])

    def test_invalid_structural_stop_is_rejected_by_risk_engine(self):
        result = self.review(strategy("BUY", sl=1.1010, tp=1.1100))
        self.assertFalse(result["approved"])
        self.assertEqual(result["stage"], "rejected_risk")
        self.assertFalse(result["order_sent"])

    def test_unusable_quote_fails_closed(self):
        self.adapter.info.ask = None
        result = self.review(strategy("BUY"))
        self.assertFalse(result["approved"])
        self.assertEqual(result["stage"], "market_unavailable")
        self.assertFalse(result["order_sent"])

    def test_missing_account_risk_history_does_not_treat_daily_loss_as_zero(self):
        self.state = AccountRiskState(equity=1000.0, balance=1000.0, currency="USD", start_of_day_balance=None,
                                      todays_closed_pl=None, floating_pl=0.0, positions=(), history_error="clock unknown")
        with mock.patch("trademate.trade_pipeline.gather_state", return_value=self.state):
            result = self.review(strategy("BUY"))
        self.assertFalse(result["approved"])
        self.assertFalse(result["order_sent"])


if __name__ == "__main__":
    unittest.main()
