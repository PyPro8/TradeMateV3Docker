"""Rapid Fire's execution layer. It adds NO new order path: it uses the same two things every other TradeMate order uses.

  OPEN   the central Risk gate (`execution_gate.gate_order`, the Risk Engine + protections) judges the EXACT order, then
         `adapter.place_order()` (journaled, reconciled, fail-closed) sends it.
  CLOSE  `adapter.close_position()` (journaled; closure is confirmed by the account, not assumed).

The outcome is classified exactly like the application's own `_execute()`: executed / unconfirmed / unknown / not_executed. 'not_executed' is used
ONLY when the order provably did not happen. Anything uncertain is 'unknown' and is never retried blindly.

No broker-side stop-loss or take-profit is sent. The loss distance given to the Risk gate is a reference used for risk accounting only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from trademate.broker.validation import check_stops
from trademate.errors import (BrokerError, BrokerRejectedError, BrokerUnavailableError, ExecutionUnconfirmedError, OrderRejectedError)
from trademate.execution_gate import gate_order
from trademate.lotsize import validate_for_info

logger = logging.getLogger("trademate.rapid_fire")

EXECUTED, UNCONFIRMED, UNKNOWN, NOT_EXECUTED, REFUSED = "executed", "unconfirmed", "unknown", "not_executed", "refused"


@dataclass
class Outcome:
    kind: str
    message: str
    ticket: int | None = None
    price: float | None = None
    execution_id: str | None = None
    position: object | None = None
    retryable: bool = False          # a refusal / failure that may be tried again later (never for UNKNOWN)
    risk: dict | None = None

    @property
    def executed(self) -> bool:
        return self.kind == EXECUTED

    @property
    def uncertain(self) -> bool:
        return self.kind in (UNCONFIRMED, UNKNOWN)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "ticket": self.ticket, "price": self.price, "execution_id": self.execution_id,
                "retryable": self.retryable}


@dataclass
class Preflight:
    """The answer to "could this opposite order be sent right now?", asked BEFORE the original position is closed. Nothing is sent to MT5 to get it."""
    ok: bool
    message: str
    stage: str = ""                  # "" when ok; else connection | symbol | volume | stops | protections | risk
    retryable: bool = False          # the cause is transient (no data, no connection): the caller may ask again on the next quote
    details: dict | None = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "stage": self.stage, "retryable": self.retryable, "details": self.details}


class Executor:
    def __init__(self, adapter, rules_provider, max_volume_provider, reversal_whatif=None):
        self.adapter = adapter
        self._rules = rules_provider
        self._max_volume = max_volume_provider
        self._whatif = reversal_whatif          # optional: (closing_at_loss: bool) -> [{name, passed, detail}] protections that the CLOSE ITSELF could trip

    # ---- PRE-FLIGHT (read-only) ----
    def preflight_open(self, symbol: str, side: str, volume: float, reference_stop: float, sl: float | None = None, tp: float | None = None,
                       exclude_tickets=(), closing_at_loss: bool = False) -> Preflight:
        """Everything that would make the opposite order fail AFTER the original was closed, checked BEFORE it is closed. Reads only.
        It applies the same rules as open(): the broker's volume rules (exact, never rounded), the trade mode, the stop-level distance of the
        levels that will be sent, and the central Risk gate with `exclude_tickets` treated as already closed (the Risk Engine's own rules decide)."""
        try:
            info = self.adapter.get_symbol_spec(symbol)
        except (BrokerError, NotImplementedError) as exc:
            return Preflight(False, f"The instrument specification could not be read ({exc}).", "connection", retryable=True)
        mode = getattr(info, "trade_mode", None)
        if mode is not None and mode not in (4, 1 if side == "buy" else 2):
            return Preflight(False, f"{info.name} does not allow {side} orders right now (trade mode {mode}).", "symbol")
        try:
            cap = self._max_volume()
        except Exception:                                    # noqa: BLE001 - a broken provider must not hide the other checks
            cap = None
        chk = validate_for_info(volume, info, cap)
        if not chk.ok:
            return Preflight(False, f"Equal volume {volume:g} is not possible for {info.name}: {chk.message}", "volume", details=chk.to_dict())
        reference = info.bid if side == "buy" else info.ask  # the price a position of this side would close at, exactly as place_order judges the stops
        try:
            check_stops(info, side, reference, sl, tp)
        except OrderRejectedError as exc:
            return Preflight(False, str(exc), "stops", details={"stops_level": getattr(info, "stops_level", None), "sl": sl, "tp": tp})
        if self._whatif is not None:
            try:
                failed = [c for c in self._whatif(closing_at_loss) if not c.get("passed")]
            except Exception:                                # noqa: BLE001 - fail closed: unknown protections = no reversal
                return Preflight(False, "The loss protections could not be evaluated, so the reversal was not started.", "protections", retryable=True)
            if failed:
                return Preflight(False, " ".join(c.get("detail", "") for c in failed).strip() or "A safety protection would block the opposite order.", "protections",
                                 details={"checks": failed})
        try:
            review = gate_order(self.adapter, self._rules(), cap, symbol, side, volume, reference_stop, tp, exclude_tickets=tuple(exclude_tickets))
        except Exception:                                    # noqa: BLE001 - the gate itself fails closed
            logger.exception("Pre-flight: the Risk gate crashed")
            return Preflight(False, "The Risk Engine could not evaluate the opposite order, so the reversal was not started.", "risk", retryable=True)
        if not review.approved:
            return Preflight(False, review.message, "risk", retryable=review.cause == "connection", details={"stage": review.stage, "decision": review.decision})
        return Preflight(True, "The opposite order would be accepted (volume, stops and Risk Engine checked with the original treated as closed).")

    # ---- OPEN ----
    def open(self, symbol: str, side: str, volume: float, reference_stop: float, sl: float | None = None, tp: float | None = None) -> Outcome:
        try:
            review = gate_order(self.adapter, self._rules(), self._max_volume(), symbol, side, volume, reference_stop, tp)
        except Exception:                                    # noqa: BLE001 - the gate itself fails closed; this is a last guard
            logger.exception("Rapid Fire: the Risk gate crashed; nothing was sent")
            return Outcome(REFUSED, "The Risk Engine could not evaluate this order, so nothing was sent.", retryable=False)
        if not review.approved:
            return Outcome(REFUSED, review.message, retryable=review.cause == "connection",
                           risk={"stage": review.stage, "decision": review.decision})
        return self._run(lambda: self.adapter.place_order(symbol, side, review.approved_volume, sl, tp))

    # ---- CLOSE ----
    def close(self, ticket: int) -> Outcome:
        return self._run(lambda: self.adapter.close_position(ticket))

    # ---- shared classification (mirrors app._execute) ----
    def _run(self, action) -> Outcome:
        try:
            r = action()
        except BrokerRejectedError as exc:
            return Outcome(NOT_EXECUTED, str(exc), retryable=True)
        except OrderRejectedError as exc:
            return Outcome(NOT_EXECUTED, str(exc), retryable=True)
        except ExecutionUnconfirmedError as exc:
            return self._unknown(str(exc), exc.execution_id)
        except BrokerUnavailableError as exc:
            return Outcome(NOT_EXECUTED, str(exc), retryable=True)
        except BrokerError as exc:
            return Outcome(NOT_EXECUTED, str(exc), retryable=True)
        except Exception:                                    # noqa: BLE001 - a bug: say the outcome may be live if something was in flight
            logger.exception("Rapid Fire: unhandled error while executing")
            settled = []
            try:
                settled = self.adapter.settle_in_flight()
            except Exception:                                # noqa: BLE001
                logger.exception("Rapid Fire: could not settle the in-flight record")
            if settled:
                return Outcome(UNKNOWN, "TradeMate hit an internal error while an order was in flight. The outcome is UNKNOWN.", execution_id=settled[-1].get("id"))
            return Outcome(NOT_EXECUTED, "Internal error before anything was sent. See logs/trademate.log.", retryable=False)
        if r.confirmed:
            return Outcome(EXECUTED, r.message, r.ticket, r.price, r.execution_id, r.position)
        return Outcome(UNCONFIRMED, r.message, r.ticket, r.price, r.execution_id, r.position)

    def _unknown(self, message: str, execution_id: str | None) -> Outcome:
        rec = None
        if execution_id:
            try:
                rec = self.adapter.reconcile(execution_id)   # read-only check against the real account
            except Exception:                                # noqa: BLE001
                logger.warning("Rapid Fire: could not reconcile %s right now", execution_id)
        if rec and rec.get("state") == "reconciled_executed":
            return Outcome(EXECUTED, rec.get("message") or message, rec.get("ticket"), None, execution_id)
        if rec and rec.get("state") == "reconciled_not_executed":
            return Outcome(NOT_EXECUTED, rec.get("message") or message, execution_id=execution_id, retryable=True)
        return Outcome(UNKNOWN, message, execution_id=execution_id)

    def reconcile(self, execution_id: str) -> dict | None:
        try:
            return self.adapter.reconcile(execution_id)
        except Exception:                                    # noqa: BLE001
            return None
