"""Phase 6 tests: the Risk Engine gates every new order before it can reach the bridge.

AUTOMATED ONLY. These run against tests/fake_bridge.py (a stand-in for the MT5 EA). They prove TradeMate's own
behaviour. They do NOT prove real MT5 / Headway behaviour: that needs the manual DEMO checklist in TESTING.md.
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
from trademate.risk import RiskRules, RiskRulesStore

# Fake account: 1000 USD, EURUSD bid 1.1 / ask 1.10012, tick 1e-5, tick value 1.0 per lot.
# Risk of a BUY at ask with SL 1.0950 for 0.01 lot = (0.00512 / 1e-5) * 1.0 * 0.01 = about 5.12 USD = 0.51 %.
GOOD = {"symbol": "EURUSD", "side": "buy", "volume": 0.01, "sl": 1.0950, "tp": 1.1100}


class Phase6Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bridge = FakeBridge(Path(self._tmp.name)).start()
        self.addCleanup(self.bridge.stop)
        trading = TradingSettings(command_timeout_seconds=2.0, confirm_timeout_seconds=1.5)
        self.adapter = create_adapter(Settings(app=AppSettings(data_dir=Path(self._tmp.name)),
                                               mt5=Mt5Settings(data_dir=Path(self._tmp.name)), trading=trading))
        self.set_rules(RiskRules(max_risk_per_trade_percent=1.0, max_daily_loss_percent=3.0,
                                 max_total_active_risk_percent=3.0, max_positions=3))
        patcher = mock.patch.object(app_module, "adapter", self.adapter)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.test_client()

    def set_rules(self, rules):
        store = RiskRulesStore(Path(self._tmp.name) / "risk_rules.json", rules)
        p = mock.patch.object(app_module, "risk_store", store)
        p.start()
        self.addCleanup(p.stop)

    def open_cmds(self):
        return [c for c in self.bridge.commands_seen if c.get("cmd") == "open"]

    def order(self, **over):
        return self.client.post("/api/order", json={**GOOD, **over}).get_json()


class TestA_RiskApproval(Phase6Base):
    def test_valid_order_is_judged_by_the_risk_engine_before_any_command(self):
        order = []
        real = app_module.gate_order

        def spy(*a, **k):
            order.append(("gate", len(self.open_cmds())))   # no open command may exist when the gate runs
            return real(*a, **k)

        with mock.patch.object(app_module, "gate_order", spy):
            r = self.order()
        self.assertEqual(order, [("gate", 0)])
        self.assertEqual(r["state"], "executed")
        self.assertEqual(len(self.open_cmds()), 1)

    def test_review_endpoint_sends_nothing_and_reports_estimates(self):
        r = self.client.post("/api/order/review", json=GOOD).get_json()
        self.assertTrue(r["ok"] and r["approved"])
        self.assertEqual(r["stage"], "ready")
        self.assertFalse(r["order_sent"])
        self.assertEqual(self.open_cmds(), [])
        self.assertAlmostEqual(r["estimates"]["risk_amount"], 5.12, places=1)
        self.assertGreater(r["estimates"]["reward_amount"], r["estimates"]["risk_amount"])
        self.assertEqual(r["currency"], "USD")
        self.assertEqual(r["decision"]["decision"], "ACCEPTED")


class TestB_RiskRejection(Phase6Base):
    def test_hard_limit_violation_never_reaches_the_bridge(self):
        self.set_rules(RiskRules(max_risk_per_trade_percent=0.1, max_daily_loss_percent=3.0,
                                 max_total_active_risk_percent=3.0, max_positions=3))
        r = self.order()
        self.assertEqual(r["state"], "rejected_risk")
        self.assertFalse(r["order_sent"])
        self.assertEqual(self.open_cmds(), [])
        self.assertEqual(self.client.get("/api/broker").get_json()["positions"], [])

    def test_position_count_limit_is_a_hard_rule_with_no_resize_offer(self):
        self.set_rules(RiskRules(max_risk_per_trade_percent=1.0, max_daily_loss_percent=3.0,
                                 max_total_active_risk_percent=3.0, max_positions=1))
        self.assertEqual(self.order()["state"], "executed")
        r = self.order()
        self.assertEqual(r["state"], "rejected_risk")
        self.assertIsNone(r["review"]["resize"])
        self.assertEqual(len(self.open_cmds()), 1)

    def test_missing_stop_loss_is_refused(self):
        body = {k: v for k, v in GOOD.items() if k != "sl"}
        r = self.client.post("/api/order", json=body).get_json()
        self.assertEqual(r["state"], "failed")
        self.assertIn("stop-loss", r["message"].lower())
        self.assertEqual(self.open_cmds(), [])

    def test_mt5_data_unavailable_fails_closed(self):
        with mock.patch.object(app_module, "gate_order", wraps=app_module.gate_order), \
                mock.patch("trademate.execution_gate.gather_state", side_effect=app_module.BrokerError("MT5 is not connected.")):
            r = self.order()
        self.assertEqual(r["state"], "failed")
        self.assertEqual(r["review"]["stage"], "unavailable")
        self.assertEqual(self.open_cmds(), [])


class TestRiskUnavailable(Phase6Base):
    def test_crash_inside_the_risk_engine_blocks_execution(self):
        with mock.patch("trademate.execution_gate.evaluate_trade", side_effect=RuntimeError("boom")):
            for url in ("/api/order", "/api/order/review"):
                r = self.client.post(url, json=GOOD).get_json()
                self.assertFalse(r.get("approved", False))
                self.assertNotEqual(r["state" if "state" in r else "stage"], "executed")
                self.assertNotIn("boom", str(r))                     # no internals leaked to the browser
        self.assertEqual(self.open_cmds(), [])

    def test_unusable_engine_result_blocks_execution(self):
        real = app_module.gate_order

        class Junk:
            decision, approved_volume, checks, effective_max_risk_percent = "MAYBE", 0.01, [], None
            def to_dict(self): return {"decision": "MAYBE"}
            trade_risk_amount = trade_risk_percent = None
        with mock.patch("trademate.execution_gate.evaluate_trade", return_value=Junk()):
            r = self.order()
        self.assertEqual(r["state"], "failed")
        self.assertEqual(self.open_cmds(), [])


class TestMinimumVolumeNeverForcesRisk(Phase6Base):
    def test_broker_minimum_above_the_risk_ceiling_is_rejected_with_no_resize_offer(self):
        wide = {**GOOD, "sl": 1.0000}                  # 0.01 lot (the broker minimum) would risk about 10 % against 1 %
        review = self.client.post("/api/order/review", json=wide).get_json()
        self.assertFalse(review["approved"])
        self.assertIsNone(review["resize"])            # nothing smaller exists: the engine must not round UP
        r = self.order(sl=1.0000)
        self.assertEqual(r["state"], "rejected_risk")
        self.assertEqual(self.open_cmds(), [])


class TestTargetVersusMaximum(Phase6Base):
    def test_resize_aims_at_the_target_not_the_ceiling(self):
        # ceiling 3 %, target 1 % (settings default). 0.14 lot risks about 7 %: over the ceiling.
        self.set_rules(RiskRules(max_risk_per_trade_percent=3.0, max_daily_loss_percent=10.0,
                                 max_total_active_risk_percent=10.0, max_positions=3))
        review = self.client.post("/api/order/review", json={**GOOD, "volume": 0.14}).get_json()
        self.assertFalse(review["approved"])
        rs = review["resize"]
        self.assertAlmostEqual(rs["target_risk_percent"], 1.0)
        self.assertAlmostEqual(rs["max_risk_percent"], 3.0, places=6)
        self.assertLessEqual(rs["risk_percent"], 1.0 + 1e-9)       # aimed at the target, well under the ceiling
        self.assertGreater(rs["volume"], 0)


class TestC_Resizing(Phase6Base):
    def test_oversized_volume_gets_a_suggested_smaller_volume_and_is_not_sent(self):
        big = {**GOOD, "volume": 0.05}                       # about 2.56 % risk against a 1 % limit
        review = self.client.post("/api/order/review", json=big).get_json()
        self.assertFalse(review["approved"])
        self.assertEqual(review["stage"], "rejected_risk")
        rs = review["resize"]
        self.assertIsNotNone(rs)
        self.assertLess(rs["volume"], 0.05)
        self.assertLessEqual(rs["risk_percent"], 1.0 + 1e-9)
        self.assertAlmostEqual(rs["risk_amount"], rs["estimates"]["risk_amount"], places=6)   # risk recalculated for the new size
        # Entry reference, SL and TP of the request are untouched by the resize.
        self.assertEqual((review["order"]["sl"], review["order"]["tp"]), (GOOD["sl"], GOOD["tp"]))
        self.assertEqual(self.open_cmds(), [])                # submitting the big one never happened

    def test_submitting_the_suggested_volume_is_judged_again_and_sent_unchanged(self):
        review = self.client.post("/api/order/review", json={**GOOD, "volume": 0.05}).get_json()
        vol = review["resize"]["volume"]
        r = self.order(volume=vol)
        self.assertEqual(r["state"], "executed")
        sent = self.open_cmds()[0]
        self.assertEqual((float(sent["volume"]), float(sent["sl"]), float(sent["tp"])), (vol, GOOD["sl"], GOOD["tp"]))

    def test_oversized_order_submitted_directly_is_refused_not_silently_shrunk(self):
        r = self.order(volume=0.05)
        self.assertEqual(r["state"], "rejected_risk")
        self.assertEqual(self.open_cmds(), [])


class TestD_InvalidParameters(Phase6Base):
    def test_invalid_requests_never_reach_the_bridge(self):
        bad = [
            {"side": "hold"}, {"side": None}, {"side": 5},
            {"volume": 0}, {"volume": -0.01}, {"volume": "0.01"}, {"volume": True}, {"volume": None},
            {"sl": 1.1050},                                   # BUY with SL above price
            {"sl": 0}, {"sl": -1.0}, {"tp": 1.0900},          # BUY with TP below price
            {"symbol": ""}, {"symbol": None}, {"symbol": "X" * 100}, {"symbol": "EUR\nUSD"},
        ]
        for over in bad:
            with self.subTest(over=over):
                r = self.order(**over)
                self.assertIn(r["state"], ("failed", "rejected_risk"), over)
                self.assertFalse(r.get("order_sent", False))
        self.assertEqual(self.open_cmds(), [])

    def test_non_finite_numbers_in_raw_json_are_refused(self):
        for raw in ('{"symbol":"EURUSD","side":"buy","volume":NaN,"sl":1.095}',
                    '{"symbol":"EURUSD","side":"buy","volume":0.01,"sl":Infinity}',
                    '{"symbol":"EURUSD","side":"buy","volume":0.01,"sl":1.095,"tp":-Infinity}'):
            with self.subTest(raw=raw):
                r = self.client.post("/api/order", data=raw, content_type="application/json").get_json()
                self.assertEqual(r["state"], "failed")
        self.assertEqual(self.open_cmds(), [])

    def test_off_step_volume_is_refused(self):
        r = self.order(volume=0.015)
        self.assertNotEqual(r["state"], "executed")
        self.assertEqual(self.open_cmds(), [])

    def test_modify_rejects_non_finite_levels(self):
        ticket = self.order()["result"]["ticket"]
        for raw in ('{"sl":NaN}', '{"tp":Infinity}'):
            r = self.client.post(f"/api/position/{ticket}/modify", data=raw, content_type="application/json").get_json()
            self.assertEqual(r["state"], "failed")


class TestF_BrokerRejection(Phase6Base):
    def test_broker_rejection_is_reported_and_no_position_appears(self):
        self.bridge.reject_with = (10019, "No money")
        r = self.order()
        self.assertEqual(r["state"], "failed")
        self.assertIn("No money", r["message"])
        self.assertEqual(self.client.get("/api/broker").get_json()["positions"], [])


class TestJ_DuplicateProtection(Phase6Base):
    def test_lost_answer_is_unknown_and_a_retry_sends_nothing_new(self):
        self.bridge.swallow_commands = True
        first = self.order()
        self.assertEqual(first["state"], "unknown")
        sent = len(self.open_cmds())
        again = self.order()
        self.assertEqual(again["state"], "failed")
        self.assertIn("still unknown", again["message"])
        self.assertEqual(len(self.open_cmds()), sent)         # the retry never reached the bridge


class TestCloseAndSync(Phase6Base):
    def test_confirmed_close_updates_positions(self):
        ticket = self.order()["result"]["ticket"]
        r = self.client.post(f"/api/position/{ticket}/close", json={}).get_json()
        self.assertEqual(r["state"], "executed")
        self.assertEqual(self.client.get("/api/broker").get_json()["positions"], [])

    def test_uncertain_close_stays_listed_and_is_not_sent_twice(self):
        ticket = self.order()["result"]["ticket"]
        self.bridge.swallow_commands = True
        r = self.client.post(f"/api/position/{ticket}/close", json={}).get_json()
        self.assertEqual(r["state"], "unknown")
        closes = len([c for c in self.bridge.commands_seen if c.get("cmd") == "close"])
        pos = self.client.get("/api/broker").get_json()
        self.assertEqual([p["ticket"] for p in pos["positions"]], [ticket])           # not removed just because we asked
        self.assertTrue(pos["executions_unresolved"])
        again = self.client.post(f"/api/position/{ticket}/close", json={}).get_json()
        self.assertEqual(again["state"], "failed")
        self.assertEqual(len([c for c in self.bridge.commands_seen if c.get("cmd") == "close"]), closes)

    def test_floating_pl_comes_from_synchronised_mt5_data_per_position(self):
        self.bridge.positions[11] = {"symbol": "EURUSD", "type": "buy", "volume": 0.1, "price_open": 1.0990,
                                     "sl": 0.0, "tp": 0.0, "time": 0, "magic": 0, "comment": ""}
        self.bridge.positions[12] = {"symbol": "EURUSD", "type": "sell", "volume": 0.1, "price_open": 1.0990,
                                     "sl": 0.0, "tp": 0.0, "time": 0, "magic": 0, "comment": ""}
        self.bridge.write_snapshot()
        pos = {p["ticket"]: p for p in self.client.get("/api/broker").get_json()["positions"]}
        self.assertGreater(pos[11]["profit"], 0)          # BUY above entry
        self.assertLess(pos[12]["profit"], 0)             # SELL above entry: the opposite sign, not mixed up
        self.assertEqual((pos[11]["direction"], pos[12]["direction"]), ("buy", "sell"))


class TestModifyRiskReport(Phase6Base):
    def test_modify_response_carries_the_risk_report_and_the_broker_values(self):
        ticket = self.order()["result"]["ticket"]
        r = self.client.post(f"/api/position/{ticket}/modify", json={"sl": 1.0900}).get_json()
        self.assertEqual(r["state"], "executed")
        self.assertEqual(r["result"]["position"]["stop_loss"], 1.09)             # what the broker holds, not what we asked
        self.assertEqual(r["result"]["position"]["volume"], 0.01)                # volume untouched
        self.assertIn("breaches", r["risk_report"])
        self.assertGreater(r["risk_report"]["active_risk"], 5.12)                # a wider SL raises the exposure

    def test_existing_breach_is_reported_and_nothing_is_closed(self):
        ticket = self.order()["result"]["ticket"]
        self.set_rules(RiskRules(max_risk_per_trade_percent=1.0, max_daily_loss_percent=3.0,
                                 max_total_active_risk_percent=0.1, max_positions=3))
        state = self.client.get("/api/risk/state").get_json()["state"]
        self.assertTrue(any("above the 0.1% limit" in b for b in state["breaches"]))
        self.assertEqual([p["ticket"] for p in self.client.get("/api/broker").get_json()["positions"]], [ticket])
        self.assertFalse([c for c in self.bridge.commands_seen if c.get("cmd") == "close"])

    def test_position_opened_elsewhere_is_listed_and_counted_by_risk(self):
        self.bridge.positions[777] = {"symbol": "EURUSD", "type": "sell", "volume": 0.02, "price_open": 1.1,
                                      "sl": 1.105, "tp": 0.0, "time": 0, "magic": 0, "comment": "mobile"}
        self.bridge.write_snapshot()
        pos = self.client.get("/api/broker").get_json()["positions"]
        self.assertEqual([p["ticket"] for p in pos], [777])
        st = self.client.get("/api/risk/state").get_json()["state"]
        self.assertEqual(st["open_positions"], 1)
        self.assertGreater(st["active_risk"], 0)


if __name__ == "__main__":
    unittest.main()
