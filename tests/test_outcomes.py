"""Structured outcomes: every way a write can end has its own machine-readable `outcome` and honest `order_submitted`.

SIMULATED (fake bridge / mocks). None of this places a real order.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.test_phase6 import GOOD, Phase6Base
from trademate.errors import BrokerUnavailableError, OrderRejectedError
from trademate.journal import ExecutionJournal
from trademate.risk import RiskRules


class OutcomeContract(Phase6Base):
    def test_executed(self):
        r = self.order()
        self.assertEqual((r["state"], r["outcome"], r["order_submitted"]), ("executed", "executed", True))
        self.assertEqual(r["request"]["symbol"], "EURUSD")
        self.assertEqual(r["request"]["side"], "buy")

    def test_risk_rejected_carries_the_figures_and_never_reaches_mt5(self):
        self.set_rules(RiskRules(max_risk_per_trade_percent=0.1, max_daily_loss_percent=3.0,
                                 max_total_active_risk_percent=3.0, max_positions=3))
        r = self.order()
        self.assertEqual((r["state"], r["outcome"], r["order_submitted"]), ("rejected_risk", "risk_rejected", False))
        k = r["risk"]
        self.assertGreater(k["requested_risk_amount"], k["max_risk_amount"])
        self.assertAlmostEqual(k["max_risk_amount"], 1.0, places=2)               # 0.1 % of the 1000 USD fake account
        self.assertEqual(k["currency"], "USD")
        self.assertTrue(k["reasons"])
        self.assertEqual(self.open_cmds(), [])

    def test_missing_stop_loss_is_invalid_request(self):
        r = self.client.post("/api/order", json={k: v for k, v in GOOD.items() if k != "sl"}).get_json()
        self.assertEqual((r["outcome"], r["order_submitted"]), ("invalid_request", False))

    def test_bad_symbol_and_volume_are_invalid_request(self):
        for over in ({"symbol": ""}, {"volume": 0}, {"volume": "x"}, {"side": "hold"}):
            r = self.order(**over)
            self.assertEqual((r["outcome"], r["order_submitted"]), ("invalid_request", False), over)
        self.assertEqual(self.open_cmds(), [])

    def test_broker_rejection_is_not_called_a_risk_rejection_and_carries_the_retcode(self):
        self.bridge.reject_with = (10019, "No money")
        r = self.order()
        self.assertEqual((r["state"], r["outcome"], r["order_submitted"]), ("failed", "broker_rejected", True))
        self.assertEqual(r["broker"]["retcode"], 10019)
        self.assertEqual(r["broker"]["message"], "No money")
        self.assertNotEqual(r["outcome"], "risk_rejected")
        self.assertEqual(self.client.get("/api/broker").get_json()["positions"], [])     # no false position

    def test_execution_engine_refusal_before_sending(self):
        with mock.patch.object(self.adapter, "place_order", side_effect=OrderRejectedError("Trading is disabled.")):
            r = self.order()
        self.assertEqual((r["outcome"], r["order_submitted"]), ("execution_refused", False))

    def test_mt5_connection_failure_with_the_engine_unable_to_read_mt5(self):
        with mock.patch("trademate.execution_gate.gather_state", side_effect=BrokerUnavailableError("MT5 is not connected.")):
            r = self.order()
        self.assertEqual((r["outcome"], r["order_submitted"]), ("connection_failed", False))
        self.assertEqual(self.open_cmds(), [])

    def test_mt5_connection_failure_at_send_time(self):
        with mock.patch.object(self.adapter, "place_order", side_effect=BrokerUnavailableError("MT5 stopped.")):
            r = self.order()
        self.assertEqual((r["outcome"], r["order_submitted"]), ("connection_failed", False))

    def test_unknown_outcome_is_unresolved_with_resubmission_disabled(self):
        self.bridge.swallow_commands = True
        r = self.order()
        self.assertEqual((r["state"], r["outcome"], r["order_submitted"]), ("unknown", "unknown", None))
        self.assertEqual(r["automatic_resubmission"], "DISABLED")
        self.assertIn("Reconcile", r["next_action"])
        sent = len(self.open_cmds())
        again = self.order()                                                   # a blind retry must not reach the bridge
        self.assertNotEqual(again["outcome"], "executed")
        self.assertEqual(len(self.open_cmds()), sent)

    def test_modify_and_close_use_the_same_contract(self):
        t = self.order()["result"]["ticket"]
        m = self.client.post(f"/api/position/{t}/modify", json={"sl": 1.0900}).get_json()
        self.assertEqual((m["outcome"], m["request"]["ticket"], m["request"]["action"]), ("executed", t, "modify"))
        bad = self.client.post(f"/api/position/{t}/modify", json={"sl": 1.5}).get_json()
        self.assertEqual((bad["state"], bad["order_submitted"]), ("failed", False))
        c = self.client.post(f"/api/position/{t}/close", json={}).get_json()
        self.assertEqual((c["outcome"], c["request"]["action"]), ("executed", "close"))

    def test_legacy_fields_still_present(self):
        r = self.order()
        for key in ("state", "message", "result", "execution_id"):
            self.assertIn(key, r)


class SqliteCleanup(unittest.TestCase):
    """The journal used `with sqlite3.connect() as db`, which commits but never CLOSES. Python 3.13+ reports that as an
    'unclosed database' ResourceWarning. This test fails if any connection is left open, on every Python version."""

    def test_every_journal_connection_is_closed(self):
        opened = []
        real = sqlite3.connect

        def tracking(*a, **k):
            c = real(*a, **k)
            opened.append(c)
            return c

        with tempfile.TemporaryDirectory() as d, mock.patch("trademate.journal.sqlite3.connect", tracking):
            j = ExecutionJournal(Path(d) / "j.sqlite3")
            eid = j.begin("place", "EURUSD", "buy", 0.01, 1.09, 1.11)
            j.update(eid, state="rejected", message="x")
            j.get(eid)
            j.recent()
            j.unresolved()
            j.in_flight()
            j.settle_stale(0.0)
            self.assertGreaterEqual(len(opened), 7)
            for c in opened:
                with self.assertRaises(sqlite3.ProgrammingError):          # closed connections refuse to be used
                    c.execute("select 1")

    def test_transaction_behaviour_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            j = ExecutionJournal(Path(d) / "j.sqlite3")
            eid = j.begin("place", "EURUSD", "buy", 0.01, 1.09, 1.11)
            j.update(eid, state="rejected")
            self.assertEqual(j.get(eid)["state"], "rejected")              # committed and readable from a fresh connection
            with self.assertRaises(Exception):
                j.update(eid, bogus_field=1)                               # rejected before touching the database
            self.assertEqual(j.get(eid)["state"], "rejected")

    def test_no_resource_warning_when_journal_is_used_and_collected(self):
        import gc
        import warnings
        with tempfile.TemporaryDirectory() as d, warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            j = ExecutionJournal(Path(d) / "j.sqlite3")
            j.begin("place", "EURUSD", "buy", 0.01, 1.09, 1.11)
            del j
            gc.collect()
        self.assertEqual([w for w in seen if issubclass(w.category, ResourceWarning)], [])


if __name__ == "__main__":
    unittest.main()
