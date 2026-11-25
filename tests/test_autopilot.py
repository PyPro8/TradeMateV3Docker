"""Autopilot rules with in-memory fakes (no MT5, no orders):  python3 -m unittest tests.test_autopilot -v"""

import tempfile
import unittest
import time
from pathlib import Path

from trademate.autopilot import Autopilot, check_stop, validate_stop_config
from trademate.decision_hub import DecisionHub

SEL = ("EURUSD", "candle_momentum", "M5")


def result(decision="BUY", cid="C1"):
    return {"decision": decision, "setup_candidates": [{"candidate_id": cid, "execution_eligible": decision in ("BUY", "SELL")}]}


class Rig:
    """A broker + pipeline stand-in. `execute` opens a 'position'; tests close it by editing open_/closed."""

    def __init__(self, tmp, **kw):
        self.t = [1_000_000.0]
        self.hub = DecisionHub(stale_after=1e9, clock=lambda: self.t[0])
        self.sel = SEL
        self.lot = 0.1
        self.open_, self.closed = set(), {}
        self.proposals, self.executions = [], []
        self.approve = True
        self.next_ticket = 100
        self.exec_outcome = "executed"
        self.precheck_msg = None
        self.ap = Autopilot(Path(tmp) / "ap.json", selection=lambda: self.sel, hub_get=self.hub.get, lot_for=lambda s: self.lot, propose=self.propose,
                            execute=self.execute, positions=lambda: set(self.open_), history=lambda: [{"ticket": k, **v} for k, v in self.closed.items()],
                            precheck=lambda: self.precheck_msg, max_decision_age=30, now=lambda: self.t[0], **kw)

    def propose(self, symbol, sid, tf, volume):
        self.proposals.append((symbol, sid, tf, volume))
        return {"approved": self.approve, "approval_id": f"A{len(self.proposals)}", "message": None if self.approve else "Risk Engine: daily loss limit"}

    def execute(self, approval_id):
        self.executions.append(approval_id)
        if self.exec_outcome != "executed":
            return {"outcome": self.exec_outcome, "ticket": None, "message": "unconfirmed", "order_sent": True}
        self.next_ticket += 1
        self.open_.add(self.next_ticket)
        return {"outcome": "executed", "ticket": self.next_ticket, "message": "ok", "order_sent": True}

    def decide(self, decision="BUY", cid="C1", state="fresh"):
        self.hub.publish(self.hub.begin(), *self.sel, state=state, result=result(decision, cid) if state == "fresh" else None)

    def close_all(self, net):
        for tk in list(self.open_):
            self.open_.discard(tk)
            self.closed[tk] = {"net": net, "time_close": self.t[0]}


class StopConfigTest(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(validate_stop_config("runs", 5)[2], 5)
        self.assertEqual(validate_stop_config("runs", "5")[2], 5)
        self.assertEqual(validate_stop_config("hours", 10)[2], 10.0)
        self.assertEqual(validate_stop_config("profit", "25.5")[2], 25.5)
        for mode, val in (("runs", 0), ("runs", 2.5), ("runs", -1), ("runs", 10 ** 6), ("runs", "x"), ("runs", True), ("profit", 0), ("profit", -5), ("profit", float("nan")),
                          ("hours", 0), ("hours", 10 ** 6), ("hours", None), ("nonsense", 5), (None, 5)):
            self.assertFalse(validate_stop_config(mode, val)[0], (mode, val))

    def test_pure_checks(self):
        self.assertTrue(check_stop("runs", 3, 0, 0, 3, 0)[0])
        self.assertFalse(check_stop("runs", 3, 0, 0, 2, 999)[0])         # profit is irrelevant to the runs mode
        self.assertTrue(check_stop("profit", 50, 0, 0, 0, 50.0)[0])
        self.assertFalse(check_stop("profit", 50, 0, 10 ** 9, 99, 49.99)[0])
        self.assertTrue(check_stop("hours", 10, 0, 36000, 0, 0)[0])
        self.assertFalse(check_stop("hours", 10, 0, 35999, 0, 0)[0])


class AutopilotTest(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.r = Rig(self._d.name)

    def start(self, mode="runs", value=2):
        ok, msg = self.r.ap.start(mode, value, spawn=False)
        self.assertTrue(ok, msg)

    def test_cannot_start_without_lot_or_with_failed_precheck_or_bad_config(self):
        self.r.lot = None
        self.assertFalse(self.r.ap.start("runs", 2, spawn=False)[0])
        self.r.lot = 0.1
        self.r.precheck_msg = "Trading is disabled"
        self.assertEqual(self.r.ap.start("runs", 2, spawn=False)[1], "Trading is disabled")
        self.r.precheck_msg = None
        self.assertFalse(self.r.ap.start("runs", 0, spawn=False)[0])
        self.assertFalse(self.r.ap.status()["armed"])

    def test_wait_or_missing_or_stale_decision_never_trades(self):
        self.start()
        self.r.ap.tick()
        self.r.decide("WAIT")
        self.r.ap.tick()
        self.r.decide("BUY", state="error")
        self.r.ap.tick()
        self.assertEqual(self.r.proposals, [])

    def test_old_decision_is_not_executed(self):
        self.start()
        self.r.decide("BUY")
        self.r.t[0] += 31
        self.r.ap.tick()
        self.assertEqual(self.r.proposals, [])

    def test_buy_is_proposed_with_the_validated_lot_then_executed(self):
        self.start()
        self.r.decide("BUY")
        self.r.ap.tick()
        self.assertEqual(self.r.proposals, [("EURUSD", "candle_momentum", "M5", 0.1)])
        self.assertEqual(len(self.r.executions), 1)
        self.assertEqual(self.r.ap.status()["open_trades"], 1)

    def test_risk_rejection_never_reaches_execution_and_setup_is_not_retried(self):
        self.start()
        self.r.approve = False
        self.r.decide("BUY")
        self.r.ap.tick()
        self.r.ap.tick()
        self.assertEqual(self.r.executions, [])
        self.assertEqual(len(self.r.proposals), 1)                       # one attempt per setup
        self.assertIn("Risk Engine", self.r.ap.status()["message"])

    def test_same_setup_never_twice_even_after_the_trade_closes(self):
        self.start("runs", 5)
        self.r.decide("BUY", "C1")
        self.r.ap.tick()
        self.r.close_all(5.0)
        self.r.ap.tick()
        self.r.ap.tick()
        self.assertEqual(len(self.r.executions), 1)
        self.r.decide("BUY", "C2")                                       # the next candle
        self.r.ap.tick()
        self.assertEqual(len(self.r.executions), 2)

    def test_one_open_trade_at_a_time(self):
        self.start("runs", 5)
        self.r.decide("BUY", "C1")
        self.r.ap.tick()
        self.r.decide("BUY", "C2")
        self.r.ap.tick()
        self.assertEqual(len(self.r.executions), 1)

    def test_runs_mode_stops_after_n_completed_runs_and_opens_nothing_more(self):
        self.start("runs", 2)
        for i in (1, 2):
            self.r.decide("BUY", f"C{i}")
            self.r.ap.tick()
            self.r.close_all(-3.0 if i == 1 else 8.0)                  # SL then TP: both count as runs
            self.r.ap.tick()
        st = self.r.ap.status()
        self.assertEqual(st["state"], "finished")
        self.assertEqual(st["completed_runs"], 2)
        self.assertIn("2 of 2 runs", st["stopped_reason"])
        self.r.decide("BUY", "C3")
        self.r.ap.tick()
        self.assertEqual(len(self.r.executions), 2)

    def test_unresolved_signals_do_not_count_as_runs(self):
        self.start("runs", 1)
        for i in range(5):
            self.r.decide("WAIT", f"C{i}")
            self.r.ap.tick()
        self.assertEqual(self.r.ap.status()["completed_runs"], 0)

    def test_profit_mode_uses_net_realised_pnl_only(self):
        self.start("profit", 10.0)
        self.r.decide("BUY", "C1")
        self.r.ap.tick()
        self.r.close_all(6.0)
        self.r.ap.tick()
        self.assertEqual(self.r.ap.status()["state"], "running")
        self.assertEqual(self.r.ap.status()["progress"]["current"], 6.0)
        self.r.decide("BUY", "C2")
        self.r.ap.tick()
        self.r.close_all(4.5)
        self.r.ap.tick()
        st = self.r.ap.status()
        self.assertEqual(st["state"], "finished")
        self.assertEqual(st["net_pnl"], 10.5)
        self.r.decide("BUY", "C3")
        self.r.ap.tick()
        self.assertEqual(len(self.r.executions), 2)

    def test_profit_mode_ignores_losses_for_stopping_but_tracks_them(self):
        self.start("profit", 10.0)
        self.r.decide("BUY", "C1")
        self.r.ap.tick()
        self.r.close_all(-20.0)
        self.r.ap.tick()
        self.assertEqual(self.r.ap.status()["state"], "running")
        self.assertEqual(self.r.ap.status()["net_pnl"], -20.0)

    def test_elapsed_mode_stops_new_entries_but_keeps_managing_the_open_one(self):
        self.start("hours", 1)
        self.r.decide("BUY", "C1")
        self.r.ap.tick()
        self.r.t[0] += 3601
        self.r.ap.tick()
        st = self.r.ap.status()
        self.assertEqual(st["state"], "finished")
        self.assertEqual(st["open_trades"], 1)                           # not abandoned
        self.assertIn("keeps its stop-loss", st["message"])
        self.r.decide("BUY", "C2")
        self.r.ap.tick()
        self.assertEqual(len(self.r.executions), 1)                      # nothing new after expiry
        self.r.close_all(2.0)
        self.r.ap.tick()
        self.assertEqual(self.r.ap.status()["open_trades"], 0)           # the open position is still settled and counted
        self.assertEqual(self.r.ap.status()["completed_runs"], 1)

    def test_elapsed_time_survives_restart_and_never_resumes_by_itself(self):
        self.start("hours", 2)
        self.r.t[0] += 3600
        r2 = Rig(self._d.name)                                           # a "restart": same state file
        r2.t[0] = self.r.t[0]
        st = r2.ap.status()
        self.assertEqual(st["state"], "interrupted")
        self.assertFalse(st["armed"])
        r2.decide("BUY")
        r2.ap.tick()
        self.assertEqual(r2.proposals, [])
        ok, _ = r2.ap.start("hours", 2, resume=True, spawn=False)
        self.assertTrue(ok)
        self.assertAlmostEqual(r2.ap.status()["progress"]["current"], 1.0, places=2)   # the clock was NOT reset

    def test_emergency_stop_blocks_entries_immediately_and_keeps_positions(self):
        self.start("runs", 5)
        self.r.decide("BUY", "C1")
        self.r.ap.tick()
        self.r.ap.stop("Emergency stop pressed", emergency=True)
        self.r.decide("BUY", "C2")
        self.r.ap.tick()
        self.assertEqual(len(self.r.executions), 1)
        self.assertEqual(self.r.ap.status()["state"], "emergency_stopped")
        self.assertEqual(self.r.ap.status()["open_trades"], 1)

    def test_selection_change_after_start_is_not_traded(self):
        self.start()
        self.r.sel = ("GBPUSD", "candle_momentum", "M5")
        self.r.hub.publish(self.r.hub.begin(), *self.r.sel, state="fresh", result=result("BUY", "X"))
        self.r.ap.tick()
        self.assertEqual(self.r.proposals, [])

    def test_unconfirmed_order_blocks_autopilot(self):
        self.start()
        self.r.exec_outcome = "accepted_unconfirmed"
        self.r.decide("BUY")
        self.r.ap.tick()
        self.assertEqual(self.r.ap.status()["state"], "blocked")
        self.assertFalse(self.r.ap.status()["armed"])

    def test_stop_then_start_again_creates_a_live_worker(self):
        # Exercise the real worker lifecycle with a short poll interval.
        r = Rig(self._d.name, poll_seconds=0.02)
        ok, msg = r.ap.start("runs", 2, spawn=True)
        self.assertTrue(ok, msg)
        first = r.ap._thread
        self.assertIsNotNone(first)
        r.ap.stop()
        ok, msg = r.ap.start("runs", 2, spawn=True)
        self.assertTrue(ok, msg)
        second = r.ap._thread
        self.assertIsNotNone(second)
        # Give the worker a chance to enter its loop, then verify it is alive
        # while the session is armed. The test uses only fake broker adapters.
        second.join(0.01)
        self.assertTrue(r.ap.status()["armed"])
        self.assertIsNotNone(r.ap._thread)
        r.ap.stop()
        r.ap.shutdown()
        if r.ap._thread is not None:
            r.ap._thread.join(1.0)

    def test_worker_reference_is_cleared_when_idle_worker_retires(self):
        r = Rig(self._d.name, poll_seconds=0.01)
        ok, msg = r.ap.start("runs", 2, spawn=True)
        self.assertTrue(ok, msg)
        r.ap.stop()
        deadline = time.monotonic() + 1.0
        while r.ap._thread is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNone(r.ap._thread)
        # A subsequent start must be able to create a fresh worker.
        ok, msg = r.ap.start("runs", 2, spawn=True)
        self.assertTrue(ok, msg)
        self.assertIsNotNone(r.ap._thread)
        r.ap.stop()
        r.ap.shutdown()
        if r.ap._thread is not None:
            r.ap._thread.join(1.0)

    def test_cannot_change_mode_while_running(self):
        self.start("runs", 2)
        self.assertFalse(self.r.ap.start("hours", 1, spawn=False)[0])

    def test_settle_fails_closed_when_broker_unreadable(self):
        self.start("runs", 3)
        self.r.decide("BUY", "C1")
        self.r.ap.tick()
        self.r.ap._positions = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        self.r.decide("BUY", "C2")
        self.r.ap.tick()
        self.assertEqual(len(self.r.executions), 1)


if __name__ == "__main__":
    unittest.main()
