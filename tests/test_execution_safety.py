"""Execution-state handling: what happens when the answer is lost, an internal error strikes after a send,
or the journal fails. Run against tests/fake_bridge.py, which can execute-without-answering (lost response),
swallow a command (took it, did nothing, never answered) or ignore commands (MT5 not running).

The rule under test: an order that MAY be live is never reported as "failed", is never resent blindly, and is
always reconcilable against the real account state.
"""

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.fake_bridge import FakeBridge
from trademate.broker import create_adapter
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.errors import BrokerUnavailableError, ExecutionUnconfirmedError, OrderRejectedError
from trademate.journal import JournalError


class SafetyBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bridge = FakeBridge(Path(self._tmp.name)).start()
        self.addCleanup(self.bridge.stop)
        trading = TradingSettings(command_timeout_seconds=1.0, confirm_timeout_seconds=0.6)
        self.adapter = create_adapter(Settings(app=AppSettings(data_dir=Path(self._tmp.name)),
                                               mt5=Mt5Settings(data_dir=Path(self._tmp.name)), trading=trading))
        self.journal = self.adapter.journal
        patcher = mock.patch.object(app_module, "adapter", self.adapter)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.test_client()

    def last(self):
        return self.journal.recent(1)[0]

    def sent_count(self):
        """Number of WRITE commands (open/modify/close) that reached MT5. Read-only data requests do not count."""
        return len([c for c in self.bridge.commands_seen if c["cmd"] in ("open", "modify", "close")])


class JournalStatesTest(SafetyBase):
    def test_confirmed_order_is_journaled_with_its_command(self):
        result = self.adapter.place_order("EURUSD", "buy", 0.1, 1.095, 1.105)
        rec = self.journal.get(result.execution_id)
        self.assertEqual((rec["state"], rec["stage"], rec["action"]), ("confirmed", "answered", "place"))
        self.assertEqual(rec["ticket"], result.ticket)
        self.assertEqual(rec["cmd_id"], self.bridge.commands_seen[-1]["id"])

    def test_broker_rejection_is_recorded_as_rejected(self):
        self.bridge.reject_with = (10019, "No money")
        with self.assertRaises(OrderRejectedError):
            self.adapter.place_order("EURUSD", "buy", 0.1)
        self.assertEqual(self.last()["state"], "rejected")
        self.assertEqual(self.journal.unresolved(), [])

    def test_command_nobody_picked_up_is_withdrawn_and_marked_not_sent(self):
        self.bridge.ignore_commands = True
        with self.assertRaises(BrokerUnavailableError):
            self.adapter.place_order("EURUSD", "buy", 0.1)
        self.assertEqual(self.last()["state"], "not_sent")
        self.assertEqual(list(Path(self._tmp.name).glob("trademate_cmd_*.txt")), [])   # file really withdrawn

    def test_validation_failure_never_reaches_mt5(self):
        before = self.sent_count()
        with self.assertRaises(OrderRejectedError):
            self.adapter.place_order("EURUSD", "buy", 50)     # above max_volume
        self.assertEqual(self.sent_count(), before)
        self.assertEqual(self.last()["state"], "rejected")


class LostResponseTest(SafetyBase):
    def test_executed_but_answer_lost_is_unknown_and_reconciles_to_executed(self):
        self.bridge.silent = True                              # MT5 executes, the answer never arrives
        with self.assertRaises(ExecutionUnconfirmedError) as ctx:
            self.adapter.place_order("EURUSD", "buy", 0.1)
        exec_id = ctx.exception.execution_id
        self.assertEqual(self.journal.get(exec_id)["state"], "unknown")
        self.assertEqual(self.journal.get(exec_id)["stage"], "received")      # the bridge DID take the command
        self.assertEqual(len(self.bridge.positions), 1)                        # and it really executed
        rec = self.adapter.reconcile(exec_id)
        self.assertEqual(rec["state"], "reconciled_executed")
        self.assertEqual(rec["ticket"], list(self.bridge.positions)[0])

    def test_lost_answer_is_never_resent_while_unresolved(self):
        self.bridge.swallow_commands = True                    # took the command, did nothing, said nothing
        with self.assertRaises(ExecutionUnconfirmedError) as ctx:
            self.adapter.place_order("EURUSD", "buy", 0.1)
        exec_id = ctx.exception.execution_id
        sent = self.sent_count()
        rec = self.adapter.reconcile(exec_id)
        self.assertEqual(rec["state"], "unknown")              # nothing provable yet: stays unresolved
        with self.assertRaises(OrderRejectedError) as blocked:  # the identical order is refused...
            self.adapter.place_order("EURUSD", "buy", 0.1)
        self.assertIn("outcome is still unknown", str(blocked.exception))
        self.assertEqual(self.sent_count(), sent)              # ...and NOTHING was sent to MT5
        self.bridge.swallow_commands = False
        self.assertTrue(self.adapter.place_order("EURUSD", "sell", 0.1).confirmed)   # a different order is not blocked

    def test_user_can_resolve_then_the_same_order_is_allowed_again(self):
        self.bridge.swallow_commands = True
        with self.assertRaises(ExecutionUnconfirmedError) as ctx:
            self.adapter.place_order("EURUSD", "buy", 0.1)
        self.bridge.swallow_commands = False
        self.adapter.resolve(ctx.exception.execution_id, "not_executed")
        self.assertEqual(self.journal.unresolved(), [])
        self.assertTrue(self.adapter.place_order("EURUSD", "buy", 0.1).confirmed)

    def test_lost_close_answer_reconciles_against_the_account(self):
        ticket = self.adapter.place_order("EURUSD", "buy", 0.1).ticket
        self.bridge.silent = True
        with self.assertRaises(ExecutionUnconfirmedError) as ctx:
            self.adapter.close_position(ticket)
        self.assertEqual(self.adapter.reconcile(ctx.exception.execution_id)["state"], "reconciled_executed")
        self.assertEqual(self.bridge.positions, {})

    def test_lost_modify_answer_that_did_not_apply_reconciles_to_not_executed_after_expiry(self):
        ticket = self.adapter.place_order("EURUSD", "buy", 0.1).ticket
        self.bridge.swallow_commands = True
        with self.assertRaises(ExecutionUnconfirmedError) as ctx:
            self.adapter.modify_position(ticket, 1.09, None, True, False)
        exec_id = ctx.exception.execution_id
        self.assertEqual(self.adapter.reconcile(exec_id)["state"], "unknown")        # still inside the command's life
        self.journal._run("UPDATE executions SET expires_at = 0 WHERE id = ?", (exec_id,))   # time passes
        self.assertEqual(self.adapter.reconcile(exec_id)["state"], "reconciled_not_executed")

    def test_http_lost_response_reports_unknown_not_failed(self):
        self.bridge.swallow_commands = True
        r = self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 0.1, "sl": 1.0995})
        body = r.get_json()
        self.assertEqual((r.status_code, body["state"]), (200, "unknown"))
        self.assertTrue(body["execution_id"])
        self.assertEqual(self.client.get("/api/broker").get_json()["executions_unresolved"][0]["id"], body["execution_id"])
        again = self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 0.1, "sl": 1.0995}).get_json()
        self.assertEqual(again["state"], "failed")
        self.assertIn("still unknown", again["message"])


class InternalErrorTest(SafetyBase):
    def test_bug_after_send_is_logged_journaled_and_reported_unknown_never_a_bare_500(self):
        with mock.patch.object(type(self.adapter), "_wait_for", side_effect=RuntimeError("boom")):
            with self.assertLogs("trademate.broker.mt5", level="ERROR") as logs:
                with self.assertRaises(ExecutionUnconfirmedError) as ctx:
                    self.adapter.place_order("EURUSD", "buy", 0.1)
        self.assertTrue(any("boom" in line or "RuntimeError" in line for line in logs.output))   # not swallowed
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)
        self.assertEqual(self.journal.get(ctx.exception.execution_id)["state"], "unknown")
        self.assertEqual(len(self.bridge.positions), 1)                                         # the order really is live

    def test_http_bug_after_send_reconciles_instead_of_returning_500(self):
        with mock.patch.object(type(self.adapter), "_wait_for", side_effect=RuntimeError("boom")), \
                self.assertLogs("trademate.broker.mt5", level="ERROR"):
            r = self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 0.1, "sl": 1.0995})
        body = r.get_json()
        self.assertEqual((r.status_code, body["state"]), (200, "executed"))     # the account itself proves it
        self.assertIn("DID execute", body["message"])

    def test_bug_before_send_is_a_clear_failure_and_nothing_was_sent(self):
        with mock.patch("trademate.broker.mt5_adapter.check_volume", side_effect=RuntimeError("bad")), \
                self.assertLogs("trademate.broker.mt5", level="ERROR"):
            r = self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 0.1, "sl": 1.0995})
        body = r.get_json()
        self.assertEqual((r.status_code, body["state"]), (500, "failed"))
        self.assertIn("No order was sent", body["message"])
        self.assertEqual(self.sent_count(), 0)
        self.assertEqual(self.last()["state"], "not_sent")

    def test_escaped_error_with_a_write_in_flight_is_unknown(self):
        def explode(*a, **k):
            self.journal.begin("place", symbol="EURUSD", side="buy", volume=0.1)   # a command is "in flight"
            raise RuntimeError("escaped")
        with mock.patch.object(self.adapter, "place_order", side_effect=explode), \
                self.assertLogs("trademate.web", level="ERROR"):
            r = self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 0.1, "sl": 1.0995})
        self.assertEqual((r.status_code, r.get_json()["state"]), (500, "unknown"))
        self.assertEqual(len(self.journal.unresolved()), 1)

    def test_journal_failure_before_send_fails_closed(self):
        with mock.patch.object(self.journal, "begin", side_effect=JournalError("disk full")), \
                self.assertLogs("trademate.broker.mt5", level="ERROR"):
            r = self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 0.1, "sl": 1.0995})
        self.assertEqual(r.get_json()["state"], "failed")
        self.assertEqual(self.sent_count(), 0)
        self.assertEqual(self.bridge.positions, {})

    def test_stage_callback_errors_cannot_break_an_order(self):
        with mock.patch.object(self.journal, "update", side_effect=JournalError("locked")), \
                self.assertLogs("trademate.broker.mt5", level="ERROR"):
            self.assertTrue(self.adapter.place_order("EURUSD", "buy", 0.1).confirmed)


class DeleteLocalHistoryTest(SafetyBase):
    def seed(self):
        self.adapter.place_order("EURUSD", "buy", 0.1)
        ticket = self.adapter.place_order("EURUSD", "sell", 0.2).ticket
        self.adapter.close_position(ticket)
        self.bridge.history[-1]["time_close"] = 1_800_000_000

    def test_requires_explicit_confirmation(self):
        self.seed()
        r = self.client.post("/api/local-history/clear", json={}).get_json()
        self.assertFalse(r["ok"])
        self.assertEqual(self.journal.count(), 3)
        self.assertEqual(self.client.post("/api/local-history/clear", json={"confirm": "yes"}).get_json()["ok"], False)

    def test_delete_is_local_only_and_never_talks_to_mt5(self):
        self.seed()
        all_commands, open_positions, history = len(self.bridge.commands_seen), dict(self.bridge.positions), list(self.bridge.history)
        r = self.client.post("/api/local-history/clear", json={"confirm": "DELETE"}).get_json()
        self.assertEqual((r["ok"], r["deleted"]), (True, 3))
        self.assertEqual(self.journal.count(), 0)
        self.assertEqual(len(self.bridge.commands_seen), all_commands)  # no command of ANY kind reached MT5
        self.assertEqual(self.bridge.positions, open_positions)         # nothing closed or changed
        self.assertEqual(self.bridge.history, history)                  # the broker's history is untouched
        self.assertEqual(self.client.get("/api/executions").get_json()["executions"], [])

    def test_unresolved_orders_are_counted_when_forgotten(self):
        self.bridge.swallow_commands = True
        self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 0.1, "sl": 1.0995})
        r = self.client.post("/api/local-history/clear", json={"confirm": "DELETE"}).get_json()
        self.assertEqual(r["unresolved_forgotten"], 1)
        self.assertEqual(self.client.get("/api/broker").get_json()["executions_unresolved"], [])

    def test_mt5_history_view_is_hidden_locally_but_still_available(self):
        self.seed()
        shown = self.client.get("/api/history?days=365").get_json()
        self.assertEqual(len(shown["trades"]), 1)
        newest = max(t["time_close"] for t in shown["trades"])
        self.client.post("/api/local-history/clear", json={"confirm": "DELETE", "hide_mt5_view_before": newest})
        self.assertEqual(self.client.get("/api/history?days=365").get_json()["trades"], [])
        everything = self.client.get("/api/history?days=365&all=1").get_json()
        self.assertEqual(len(everything["trades"]), 1)                  # still there at the broker
        self.assertEqual(self.client.get("/api/history?days=365").get_json()["hidden_count"], 1)


if __name__ == "__main__":
    unittest.main()
