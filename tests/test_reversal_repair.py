"""v5.3 reliable-reversal repair: Anti-Twin (Candle Momentum / Bull-Bear AntiTwin) with a fake account that can lag, reject, time out and disagree.
Run:  python -m pytest tests/test_reversal_repair.py"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from tests.test_bull_bear import ATRig, FakeExec
from trademate.bull_bear.antitwin import (ABORTED_PREFLIGHT, AWAITING_CLOSE, CLOSED_EXTERNAL, EMERGENCY_CANCELLED, FAILED, OPENING, REFUSED_ST, SL_HIT, TWIN_OPENED,
                                          UNCERTAIN, WATCHING, AntiTwin, reversal_state)
from trademate.rapid_fire.executor import (EXECUTED, NOT_EXECUTED, REFUSED, UNCONFIRMED, UNKNOWN, Executor, Outcome, Preflight)


class LagExec(FakeExec):
    """FakeExec + a pre-flight, a journal reconciliation, and an account that can lag behind an accepted close."""

    def __init__(self, positions, **kw):
        super().__init__(positions, **kw)
        self.pf = Preflight(True, "ok")
        self.pf_calls = []
        self.rec = None
        self.rec_calls = 0
        self.vanish_after = None          # the position disappears from the account after this many fresh reads (an accepted-but-lagging close)
        self._reads = 0
        self.fill_volume = None           # partial fill
        self.wrong_side = False

    def preflight_open(self, symbol, side, volume, reference_stop, sl=None, tp=None, exclude_tickets=(), closing_at_loss=False):
        self.pf_calls.append((side, volume, tuple(exclude_tickets), closing_at_loss))
        return self.pf

    def reconcile(self, execution_id):
        self.rec_calls += 1
        return self.rec

    def close(self, ticket):
        out = super().close(ticket)
        if self.close_kind in (UNCONFIRMED, UNKNOWN):
            out.execution_id = "x-close"
        return out

    def open(self, symbol, side, volume, reference_stop, sl=None, tp=None):
        if self.open_kind == EXECUTED and (self.fill_volume or self.wrong_side):
            self.calls.append(("open", side, volume, sl, tp))
            self.next_ticket += 1
            price = self.last_quote.bid if side == "sell" else self.last_quote.ask
            pos = NS(ticket=self.next_ticket, symbol=symbol, direction=("buy" if side == "sell" else "sell") if self.wrong_side else side,
                     volume=self.fill_volume or volume, price_open=price, stop_loss=sl, take_profit=tp)
            self.positions.append(pos)
            return Outcome(EXECUTED, "open", pos.ticket, price, position=pos)
        out = super().open(symbol, side, volume, reference_stop, sl, tp)
        if out.kind in (UNCONFIRMED, UNKNOWN):
            out.execution_id = "x-open"
        return out


class T:
    @staticmethod
    def go(r):
        r.tick(1.0998 if r.pos.direction == "buy" else 1.1002)       # a BUY's bid falls / a SELL's ask rises: the first adverse tick


def rig(side="buy", volume=0.07, **kw):
    r = ATRig(side=side, volume=volume, **kw)
    r.exec = LagExec(r.positions)
    r.at.executor = r.exec
    return r


class ReversalTest(unittest.TestCase):
    # ---- 1-3 reversal, equal volume, recorded fill
    def test_buy_to_sell_equal_volume_verified(self):
        r = rig("buy", 0.07)
        T.go(r)
        self.assertEqual(r.exec.names(), ["close", "open"])
        _, side, vol, sl, tp = r.exec.calls[1]
        self.assertEqual((side, vol), ("sell", 0.07))
        leg = r.leg()
        self.assertEqual((leg["state"], leg["reversal_state"]), (TWIN_OPENED, "OPPOSITE_ORDER_CONFIRMED"))
        twin = r.at.legs[leg["twin"]]
        self.assertEqual((twin["side"], twin["volume"], twin["ticket"]), ("sell", 0.07, leg["twin_ticket"]))
        self.assertEqual(twin["entry"], leg["twin_fill"])
        self.assertEqual(r.exec.pf_calls[0], ("sell", 0.07, (500,), True))       # pre-flight asked first, original treated as closed

    def test_sell_to_buy_equal_volume_verified(self):
        r = rig("sell", 0.13)
        T.go(r)
        self.assertEqual(r.exec.calls[1][1:3], ("buy", 0.13))
        self.assertEqual(r.leg()["reversal_state"], "OPPOSITE_ORDER_CONFIRMED")
        self.assertEqual([p.direction for p in r.positions], ["buy"])

    def test_partial_fill_is_reported_and_not_topped_up(self):
        r = rig("buy", 0.10)
        r.exec.fill_volume = 0.06
        T.go(r)
        leg = r.leg()
        self.assertEqual(leg["state"], TWIN_OPENED)
        self.assertIn("PARTIAL FILL", leg["reason"])
        self.assertEqual(r.exec.names(), ["close", "open"])
        self.assertEqual(r.at.legs[leg["twin"]]["volume"], 0.06)

    def test_a_position_that_does_not_match_the_order_is_never_a_success(self):
        r = rig("buy")
        r.exec.wrong_side = True
        T.go(r)
        self.assertEqual((r.leg()["state"], r.leg()["reversal_state"]), (UNCERTAIN, "RECOVERY_REQUIRED"))
        self.assertIsNone(r.leg().get("twin"))

    # ---- pre-flight: nothing is closed when the reversal could not complete
    def test_failed_preflight_closes_nothing(self):
        for stage in ("volume", "stops", "risk", "protections"):
            r = rig("buy")
            r.exec.pf = Preflight(False, f"{stage} problem", stage)
            T.go(r)
            T.go(r)
            self.assertEqual(r.exec.names(), [], stage)                          # no close, no open
            self.assertEqual((r.leg()["state"], r.leg()["reversal_state"]), (ABORTED_PREFLIGHT, "REVERSAL_ABORTED"), stage)
            self.assertEqual(len(r.positions), 1)                                # the original is still there
            self.assertIn("NOT closed", r.leg()["reason"])

    def test_transient_preflight_waits_then_reverses_once(self):
        r = rig("buy")
        r.exec.pf = Preflight(False, "no data", "connection", retryable=True)
        T.go(r)
        self.assertEqual((r.exec.names(), r.leg()["state"]), ([], WATCHING))
        T.go(r)                                                                   # 1 s later: still inside nothing... pre-flight asked again (PREFLIGHT_RETRY_SECONDS = 1)
        r.exec.pf = Preflight(True, "ok")
        r.clock.advance(1.5)
        T.go(r)
        self.assertEqual(r.exec.names(), ["close", "open"])
        self.assertEqual(r.leg()["state"], TWIN_OPENED)

    # ---- the unconfirmed close (the diagnosed failure)
    def test_unconfirmed_close_waits_for_the_account_then_opens_the_opposite(self):
        r = rig("buy", 0.05)
        r.exec.close_kind = UNCONFIRMED
        r.exec.remove_on_close = False                                            # the account still lists it (lag)
        T.go(r)
        self.assertEqual((r.leg()["state"], r.leg()["reversal_state"]), (AWAITING_CLOSE, "CLOSE_REQUESTED"))
        self.assertEqual(r.exec.names(), ["close"])
        r.tick(1.0998)
        self.assertEqual(r.exec.names(), ["close"])                               # never re-sent, nothing opened yet
        r.positions[:] = [p for p in r.positions if p.ticket != 500]              # the account catches up
        r.tick(1.0998)
        self.assertEqual(r.exec.names(), ["close", "open"])
        self.assertEqual((r.exec.calls[1][1], r.exec.calls[1][2]), ("sell", 0.05))
        self.assertEqual(r.leg()["reversal_state"], "OPPOSITE_ORDER_CONFIRMED")

    def test_unconfirmed_close_that_the_broker_did_at_its_stop_is_not_reversed(self):
        r = rig("buy")
        r.exec.close_kind = UNKNOWN
        r.exec.remove_on_close = False
        T.go(r)
        r.positions[:] = []
        r.history[500] = {"reason": "sl"}
        r.tick(1.0998)
        self.assertEqual(r.leg()["state"], SL_HIT)
        self.assertEqual(r.exec.names(), ["close"])

    def test_close_never_proven_is_recovery_required_and_never_resent(self):
        r = rig("buy")
        r.exec.close_kind = UNCONFIRMED
        r.exec.remove_on_close = False
        T.go(r)
        for _ in range(20):
            r.tick(1.0998)
        self.assertEqual((r.leg()["state"], r.leg()["reversal_state"]), (UNCERTAIN, "RECOVERY_REQUIRED"))
        self.assertEqual(r.exec.names(), ["close"])

    def test_close_rejection_leaves_the_original_and_opens_nothing(self):
        r = rig("buy")
        r.exec.close_kind = NOT_EXECUTED
        for _ in range(12):
            T.go(r)
        self.assertNotIn("open", r.exec.names())
        self.assertEqual(r.leg()["state"], FAILED)
        self.assertEqual(r.leg()["reversal_state"], "REVERSAL_ABORTED")           # nothing was closed
        self.assertEqual(len(r.positions), 1)

    # ---- the opposite order
    def test_opposite_rejected_after_a_successful_close_is_reversal_failed(self):
        for kind, state in ((NOT_EXECUTED, FAILED), (REFUSED, REFUSED_ST)):
            r = rig("buy")
            r.exec.open_kind = kind
            for _ in range(14):
                T.go(r)
            self.assertEqual(r.leg()["state"], state)
            self.assertEqual(r.leg()["reversal_state"], "REVERSAL_FAILED")
            self.assertIn("REVERSAL FAILED", r.leg()["reason"])
            self.assertEqual(r.positions, [])
            self.assertLessEqual(r.exec.names().count("open"), 5)                 # bounded, no infinite retry

    def test_unconfirmed_open_is_reconciled_never_resent(self):
        r = rig("buy", 0.04)
        r.exec.open_kind = UNCONFIRMED
        T.go(r)
        self.assertEqual((r.leg()["state"], r.leg()["reversal_state"]), (OPENING, "OPPOSITE_ORDER_REQUESTED"))
        pos = NS(ticket=777, symbol="EURUSD", direction="sell", volume=0.04, price_open=1.0997, stop_loss=1.1047, take_profit=1.0987)
        r.exec.rec = {"state": "reconciled_executed", "ticket": 777}
        r.positions.append(pos)
        r.tick(1.0998)
        self.assertEqual(r.exec.names().count("open"), 1)
        self.assertEqual((r.leg()["state"], r.leg()["twin_ticket"], r.leg()["twin_fill"]), (TWIN_OPENED, 777, 1.0997))

    def test_unconfirmed_open_proven_not_executed_is_reversal_failed(self):
        r = rig("buy")
        r.exec.open_kind = UNKNOWN
        T.go(r)
        r.exec.rec = {"state": "reconciled_not_executed", "message": "no such order"}
        r.tick(1.0998)
        self.assertEqual((r.leg()["state"], r.leg()["reversal_state"]), (FAILED, "REVERSAL_FAILED"))
        self.assertEqual(r.exec.names().count("open"), 1)

    def test_unconfirmed_open_never_proven_is_recovery_required(self):
        r = rig("buy")
        r.exec.open_kind = UNCONFIRMED
        T.go(r)
        for _ in range(20):
            r.tick(1.0998)
        self.assertEqual((r.leg()["state"], r.leg()["reversal_state"]), (UNCERTAIN, "RECOVERY_REQUIRED"))
        self.assertEqual(r.exec.names().count("open"), 1)

    # ---- duplicates, emergency, independence
    def test_one_reversal_per_ticket(self):
        r = rig("buy")
        for _ in range(6):
            T.go(r)
        self.assertEqual(r.exec.names().count("close"), 1)
        self.assertEqual(r.exec.names().count("open"), 1)

    def test_emergency_lock_cancels_before_anything_is_sent(self):
        r = rig("buy", locked=True)
        T.go(r)
        self.assertEqual((r.exec.names(), r.leg()["state"]), ([], EMERGENCY_CANCELLED))

    def test_state_names(self):
        self.assertEqual(reversal_state({"state": "WATCHING"}), "ORIGINAL_POSITION_ACTIVE")
        self.assertEqual(reversal_state({"state": "CLOSING", "close_sent": False}), "REVERSAL_TRIGGERED")
        self.assertEqual(reversal_state({"state": "OPENING", "open_sent": False}), "CLOSE_CONFIRMED")
        self.assertEqual(reversal_state({"state": "FAILED", "closed_at": 5.0}), "REVERSAL_FAILED")


class ExecutorPreflightTest(unittest.TestCase):
    """The real Executor.preflight_open with a stub adapter: volume rules are exact, stops are judged, nothing is sent."""

    @staticmethod
    def info(**kw):
        d = dict(name="EURUSD", bid=1.1000, ask=1.1001, point=1e-5, digits=5, volume_min=0.01, volume_max=5.0, volume_step=0.01, stops_level=10, trade_mode=4, contract_size=100000)
        d.update(kw)
        return NS(**d)

    def make(self, info):
        adapter = NS(get_symbol_spec=lambda s: info)
        return Executor(adapter, lambda: None, lambda: 100.0)

    def test_invalid_volume_step_is_refused_with_the_broker_constraint(self):
        ex = self.make(self.info(volume_step=0.05))
        pf = ex.preflight_open("EURUSD", "sell", 0.07, 1.1050, sl=1.1050, tp=1.0990)
        self.assertFalse(pf.ok)
        self.assertEqual(pf.stage, "volume")

    def test_volume_above_broker_max_is_refused(self):
        pf = self.make(self.info(volume_max=0.05)).preflight_open("EURUSD", "sell", 0.07, 1.1050)
        self.assertEqual((pf.ok, pf.stage), (False, "volume"))

    def test_trade_mode_blocks(self):
        pf = self.make(self.info(trade_mode=1)).preflight_open("EURUSD", "sell", 0.07, 1.1050)   # long only
        self.assertEqual((pf.ok, pf.stage), (False, "symbol"))

    def test_tp_inside_stop_level_is_refused(self):
        pf = self.make(self.info(stops_level=200)).preflight_open("EURUSD", "sell", 0.01, 1.1100, sl=1.1100, tp=1.0999)
        self.assertEqual((pf.ok, pf.stage), (False, "stops"))

    def test_unreadable_spec_is_transient(self):
        from trademate.errors import BrokerUnavailableError
        def boom(s):
            raise BrokerUnavailableError("down")
        pf = Executor(NS(get_symbol_spec=boom), lambda: None, lambda: 100.0).preflight_open("EURUSD", "sell", 0.01, 1.1)
        self.assertEqual((pf.ok, pf.retryable, pf.stage), (False, True, "connection"))

    def test_whatif_protection_blocks_before_any_close(self):
        ex = self.make(self.info())
        ex._whatif = lambda loss: [{"name": "Consecutive losses", "passed": False, "detail": "would reach the limit"}] if loss else []
        pf = ex.preflight_open("EURUSD", "sell", 0.01, 1.1050, closing_at_loss=True)
        self.assertEqual((pf.ok, pf.stage), (False, "protections"))


if __name__ == "__main__":
    unittest.main()
