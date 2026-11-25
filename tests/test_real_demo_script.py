"""Tests for trademate/broker/real_demo_execution_test.py (the gated REAL demo order script).

SIMULATED: these run the script against tests/fake_bridge.py, so they place NO real order. They prove the script's
gates, reports and verification logic. They do NOT prove anything about the real Headway account: that only happens when
YOU run the script with MT5 connected (see TESTING.md).
"""
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.test_phase6 import Phase6Base
from trademate.broker import ConnectionState, real_demo_execution_test as script
from trademate.broker.base import ConnectionStatus
from trademate.risk import RiskRules

AUTH = {script.ENV_NAME: script.ENV_VALUE}


class RealDemoScript(Phase6Base):
    def setUp(self):
        super().setUp()
        self.data = Path(self._tmp.name)

    def run_script(self, *argv, environ=AUTH, flag=True, expect=None):
        args = list(argv) + ["--yes"]
        if flag:
            args.append("--place-real-demo-order")
        if expect:
            args += ["--expect-server", expect]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = script.main(args, app_module=app_module, input_fn=lambda *_: "", data_dir=self.data, environ=environ)
        return code, out.getvalue()

    def kinds(self, name):
        return [c for c in self.bridge.commands_seen if c.get("cmd") == name]

    # ---- authorization ----
    def test_refuses_without_both_the_env_variable_and_the_flag(self):
        for environ, flag in (({}, True), (AUTH, False), ({}, False), ({script.ENV_NAME: "yes"}, True)):
            code, out = self.run_script("EURUSD", environ=environ, flag=flag)
            self.assertEqual(code, 2, out)
            self.assertIn("NOT AUTHORIZED", out)
            self.assertIn("Order submitted to MT5: NO", out)
        self.assertEqual(self.bridge.commands_seen, [])                   # not even a read was sent

    # ---- account / connection gates ----
    def test_live_account_is_refused(self):
        real = self.adapter.get_account()
        with mock.patch.object(self.adapter, "get_account", return_value=real.__class__(**{**real.to_dict(), "environment": "real"})):
            code, out = self.run_script("EURUSD")
        self.assertEqual(code, 1)
        self.assertIn("ACCOUNT NOT VERIFIED AS DEMO", out)
        self.assertEqual(self.kinds("open"), [])

    def test_unverifiable_account_is_refused(self):
        real = self.adapter.get_account()
        with mock.patch.object(self.adapter, "get_account", return_value=real.__class__(**{**real.to_dict(), "environment": ""})):
            code, out = self.run_script("EURUSD")
        self.assertEqual(code, 1)
        self.assertIn("NOT VERIFIED AS DEMO", out)
        self.assertEqual(self.kinds("open"), [])

    def test_wrong_server_is_refused(self):
        code, out = self.run_script("EURUSD", expect="Some-Other-Server")
        self.assertEqual(code, 1)
        self.assertIn("SERVER NOT VERIFIED", out)
        self.assertIn("Some-Other-Server", out)
        self.assertEqual(self.kinds("open"), [])

    def test_configured_default_is_headway_demo(self):
        self.assertEqual(app_module.settings.mt5.expected_server, "Headway-Demo")

    def test_mt5_connection_failure(self):
        down = ConnectionStatus(ConnectionState.DISCONNECTED, "MT5 is not running.")
        with mock.patch.object(self.adapter, "get_connection_status", return_value=down):
            code, out = self.run_script("EURUSD")
        self.assertEqual(code, 1)
        self.assertIn("MT5 CONNECTION FAILURE", out)
        self.assertIn("MT5 is not running.", out)
        self.assertEqual(self.kinds("open"), [])

    def test_invalid_symbol_is_refused(self):
        code, out = self.run_script("NOT_A_SYMBOL")
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds("open"), [])

    # ---- the order itself ----
    def test_risk_rejection_report_has_the_real_figures_and_sends_nothing(self):
        self.set_rules(RiskRules(max_risk_per_trade_percent=0.01, max_daily_loss_percent=3.0,
                                 max_total_active_risk_percent=3.0, max_positions=3))
        code, out = self.run_script("EURUSD")
        self.assertEqual(code, 1)
        self.assertIn("[TRADEMATE REJECTED]", out)
        self.assertIn("Requested risk:", out)
        self.assertIn("Maximum permitted risk:", out)
        self.assertIn("Order submitted to MT5: NO", out)
        self.assertEqual(self.kinds("open"), [])

    def test_broker_rejection_report_has_the_retcode_and_is_not_a_risk_rejection(self):
        self.bridge.reject_with = (10019, "No money")
        code, out = self.run_script("EURUSD")
        self.assertEqual(code, 1)
        self.assertIn("[MT5/BROKER REJECTED]", out)
        self.assertIn("Retcode: 10019", out)
        self.assertIn("Reason: No money", out)
        self.assertIn("Order submitted: YES", out)
        self.assertIn("Broker accepted: NO", out)
        self.assertNotIn("TRADEMATE REJECTED", out)
        self.assertEqual(self.client.get("/api/broker").get_json()["positions"], [])

    def test_unknown_outcome_sends_exactly_one_order_and_never_retries(self):
        self.bridge.swallow_commands = True
        code, out = self.run_script("EURUSD")
        self.assertEqual(code, 1)
        self.assertIn("[EXECUTION OUTCOME UNKNOWN]", out)
        self.assertIn("Automatic resubmission: DISABLED", out)
        self.assertIn("Blind retry: NOT PERMITTED", out)
        self.assertEqual(len(self.kinds("open")), 1)
        self.assertEqual(self.kinds("close"), [])

    def test_success_verifies_the_position_and_leaves_it_open(self):
        self.bridge.positions[900] = {"symbol": "EURUSD", "type": "sell", "volume": 0.02, "price_open": 1.1, "sl": 1.1005,
                                      "tp": 1.099, "time": 0, "magic": 0, "comment": "pre-existing"}
        self.bridge.write_snapshot()
        code, out = self.run_script("EURUSD")
        self.assertEqual(code, 0, out)
        for text in ("[REAL DEMO TRADE VERIFIED]", "Account environment: DEMO", "Broker/server:", "Ticket: 1001", "Volume: 0.01",
                     "Entry price:", "Stop-loss:", "Take-profit:", "Position status: OPEN AND VERIFIED",
                     "MT5 confirmation: PASS", "Position reconciliation: PASS", "Chart overlay data: PASS",
                     "left OPEN on purpose"):
            self.assertIn(text, out)
        self.assertEqual(len(self.kinds("open")), 1)
        self.assertEqual(self.kinds("close"), [])                                    # NEVER closes anything
        tickets = {p["ticket"] for p in self.client.get("/api/broker").get_json()["positions"]}
        self.assertEqual(tickets, {900, 1001})                                        # pre-existing untouched, test order open
        self.assertFalse((self.data / "live_phase6.lock").exists())

    def test_identification_never_claims_a_pre_existing_ticket_as_its_own(self):
        before_ticket = 1001                                                          # the fake bridge's NEXT ticket number
        self.bridge.positions[before_ticket] = {"symbol": "EURUSD", "type": "buy", "volume": 0.01, "price_open": 1.1,
                                                "sl": 1.0995, "tp": 1.101, "time": 0, "magic": 0, "comment": "manual"}
        self.bridge.write_snapshot()
        code, out = self.run_script("EURUSD")
        self.assertNotIn("REAL DEMO TRADE VERIFIED", out)                             # a ticket seen before the order cannot be ours
        self.assertEqual(self.kinds("close"), [])

    def test_missing_stop_loss_at_the_broker_is_a_discrepancy_not_a_pass(self):
        real = script._positions

        def strip(client):
            return {t: {**p, "stop_loss": None} for t, p in real(client).items()}

        with mock.patch.object(script, "_positions", strip):
            code, out = self.run_script("EURUSD")
        self.assertEqual(code, 1)
        self.assertIn("NOT FULLY VERIFIED", out)
        self.assertIn("Discrepancy: stop-loss", out)
        self.assertNotIn("[REAL DEMO TRADE VERIFIED]", out)
        self.assertEqual(self.kinds("close"), [])                                     # reported, never "fixed" by closing

    def test_position_missing_from_mt5_is_not_reported_as_verified(self):
        with mock.patch.object(script, "_positions", lambda client: {}), mock.patch.object(script.time, "sleep", lambda s: None):
            code, out = self.run_script("EURUSD")
        self.assertEqual(code, 1)
        self.assertIn("POSITION NOT YET CONFIRMED", out)
        self.assertIn("do NOT send this order again", out)
        self.assertEqual(len(self.kinds("open")), 1)


if __name__ == "__main__":
    unittest.main()
