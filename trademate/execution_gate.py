"""Phase 6: the gate between an order request and the MT5 bridge.

Every NEW order is judged here by the existing Phase 5 Risk Engine (`evaluate_trade`) BEFORE the adapter is called.
Nothing in this module sends anything to MT5: it only reads (account, positions, symbol spec) and decides.

    review_order()   -> a full review: parameters, executable price, Risk Engine decision, estimates, warnings
    gate_order()     -> the same review, reduced to "may this exact order be sent, and with which volume?"

Rules kept here:
  * Fail closed. If MT5 data or the symbol spec cannot be read, the order is NOT approved.
  * The entry/SL/TP of the request are never changed to force an approval.
  * Volume is never changed silently. If the requested volume is too risky, the review may SUGGEST a smaller one
    (`resize`), which the user must submit explicitly; the submitted volume is then judged again from scratch.
  * Estimates are estimates: a market order fills at the broker's price, not at the reviewed one.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from trademate.errors import BrokerError
from trademate.risk.account_state import gather_state, to_spec
from trademate.risk.engine import ACCEPTED, TradeProposal, evaluate_trade, loss_per_lot_at_stop
from trademate.risk.rules import RiskRules

logger = logging.getLogger("trademate.risk.gate")

# Review stages shown to the user.
READY = "ready"                  # approved by the Risk Engine, may be submitted
REJECTED_INPUT = "invalid"       # malformed or unsupported request: never reached the Risk Engine
REJECTED_RISK = "rejected_risk"  # judged and refused by the Risk Engine
UNAVAILABLE = "unavailable"      # MT5 data missing: cannot judge, so cannot approve

# Failed checks that a smaller volume can cure. Anything else is a hard rule.
_VOLUME_CURABLE = {"Risk per trade", "Remaining daily risk", "Total active risk"}


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def clean_request(symbol, side, volume, sl, tp) -> tuple[dict | None, str | None]:
    """Strict server-side parsing. Returns (clean values, None) or (None, why it was refused)."""
    if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 32 or not symbol.strip().isprintable():
        return None, "A valid symbol is required."
    side = side.strip().lower() if isinstance(side, str) else ""
    if side not in ("buy", "sell"):
        return None, "Direction must be BUY or SELL."
    if not _num(volume) or volume <= 0:
        return None, "Volume must be a finite number above 0."
    for name, v in (("Stop-loss", sl), ("Take-profit", tp)):
        if v is not None and (not _num(v) or v <= 0):
            return None, f"{name} must be a finite price above 0."
    return {"symbol": symbol.strip(), "side": side, "volume": float(volume),
            "sl": None if sl is None else float(sl), "tp": None if tp is None else float(tp)}, None


@dataclass
class OrderReview:
    stage: str
    approved: bool
    message: str
    order: dict | None = None            # what would be sent: symbol, side, volume, entry (reference), sl, tp
    approved_volume: float | None = None
    estimates: dict | None = None        # risk / reward at SL / TP, reward-to-risk (estimates, not guarantees)
    decision: dict | None = None         # the Risk Engine's own decision, untouched
    resize: dict | None = None           # a smaller volume the Risk Engine WOULD approve (never applied automatically)
    cause: str | None = None             # for stage 'unavailable': 'connection' (MT5 data) or 'internal' (a crash)
    warnings: list = field(default_factory=list)
    currency: str = ""

    def to_dict(self) -> dict:
        return {"stage": self.stage, "approved": self.approved, "message": self.message, "order": self.order,
                "approved_volume": self.approved_volume, "estimates": self.estimates, "decision": self.decision,
                "resize": self.resize, "warnings": self.warnings, "currency": self.currency, "order_sent": False,
                "cause": self.cause}


def _estimates(info, side, entry, sl, tp, volume) -> dict:
    """Money at SL / TP from the broker's own tick data. None when it cannot be calculated: never a guess."""
    out = {"risk_amount": None, "reward_amount": None, "reward_to_risk": None,
           "note": "Estimates from the reference price. A market order fills at the broker's price; swap, commission "
                   "and slippage are not included."}
    tick = getattr(info, "tick_size", None)
    if not (_num(tick) and tick > 0) or not _num(entry):
        return out
    loss_tv = info.tick_value_loss if _num(getattr(info, "tick_value_loss", None)) and info.tick_value_loss > 0 else info.tick_value
    win_tv = info.tick_value_profit if _num(getattr(info, "tick_value_profit", None)) and info.tick_value_profit > 0 else info.tick_value
    if sl is not None and _num(loss_tv) and loss_tv > 0:
        out["risk_amount"] = abs(entry - sl) / tick * loss_tv * volume
    if tp is not None and _num(win_tv) and win_tv > 0:
        out["reward_amount"] = abs(tp - entry) / tick * win_tv * volume
    if out["risk_amount"] and out["reward_amount"] is not None:
        out["reward_to_risk"] = out["reward_amount"] / out["risk_amount"]
    return out


def review_order(adapter, rules: RiskRules, max_volume: float | None, symbol, side, volume, sl, tp,
                 target_risk_percent: float | None = None, exclude_tickets=()) -> OrderReview:
    """`target_risk_percent` is the PREFERRED risk for a resize suggestion. The hard ceiling is always the Risk Engine's
    own effective maximum; the target can only lower the suggestion, never raise it above that ceiling.

    `exclude_tickets`: open positions the Risk Engine must treat as ALREADY CLOSED. Only the Anti-Twin PRE-FLIGHT uses it (it asks "would the opposite order be
    approved once the original is gone?" BEFORE the original is closed). Nothing else passes it, and an order is never sent from a review made with it."""
    req, problem = clean_request(symbol, side, volume, sl, tp)
    if req is None:
        return OrderReview(REJECTED_INPUT, False, problem)
    if req["sl"] is None:
        return OrderReview(REJECTED_INPUT, False,
                           "A stop-loss is required: the Risk Engine cannot size or approve a trade without one.",
                           order=req)
    try:
        info = adapter.get_symbol_spec(req["symbol"])
        state = _state_with_pending(adapter) if _reports_pending(adapter) else gather_state(adapter)
    except (BrokerError, NotImplementedError) as exc:
        return OrderReview(UNAVAILABLE, False, (str(exc) or "MT5 data is not available.")
                           + " The order was not approved and nothing was sent.", order=req, cause="connection")
    if exclude_tickets:
        gone = set(exclude_tickets)
        state = _replace_positions(state, tuple(p for p in state.positions if p.ticket not in gone))
    # The price a market order would use right now: ask for BUY, bid for SELL.
    entry = info.ask if req["side"] == "buy" else info.bid
    if not _num(entry) or entry <= 0:
        return OrderReview(UNAVAILABLE, False, f"No live price for {req['symbol']}: the order was not approved.", order=req, cause="connection")
    req = {**req, "entry": entry}
    return _finish_review(info, state, rules, max_volume, req, entry, target_risk_percent)


def _replace_positions(state, positions):
    from dataclasses import replace as _replace
    return _replace(state, positions=positions)


def _reports_pending(adapter) -> bool:
    """True when the broker reports pending orders: they then count as potential exposure for market orders too."""
    check = getattr(adapter, "supports_pending_orders", None)
    try:
        return bool(check()) if callable(check) else False
    except (BrokerError, NotImplementedError):
        return False


def _finish_review(info, state, rules, max_volume, req, entry, target_risk_percent) -> OrderReview:
    """Risk Engine judgement shared by market and pending orders. `req` = the cleaned request, `entry` = the price risk is measured from."""
    spec = to_spec(info)

    proposal = TradeProposal(symbol=info.name, direction=req["side"], entry=entry, stop_loss=req["sl"],
                             take_profit=req["tp"], volume=req["volume"])
    decision = evaluate_trade(state, proposal, spec, rules, max_volume)
    dd = decision.to_dict()
    cur = state.currency or ""
    est = _estimates(info, req["side"], entry, req["sl"], req["tp"], req["volume"])
    warnings = []
    if est["risk_amount"] is None:
        warnings.append("Monetary risk could not be calculated from MT5's tick data.")
    if est["reward_to_risk"] is not None and est["reward_to_risk"] < 1:
        warnings.append(f"Reward-to-risk is {est['reward_to_risk']:.2f}: the reward is smaller than the risk.")
    if req["tp"] is None:
        warnings.append("No take-profit set.")

    if decision.decision == ACCEPTED:
        return OrderReview(READY, True, "Approved by the Risk Engine.", order=req, approved_volume=decision.approved_volume,
                           estimates=est, decision=dd, warnings=warnings, currency=cur)

    # Rejected. Offer a smaller volume only when the failures are all volume-curable.
    failed = {c["name"] for c in decision.checks if not c["passed"]}
    resize = None
    if failed and failed <= _VOLUME_CURABLE and decision.effective_max_risk_percent:
        aim = decision.effective_max_risk_percent
        if _num(target_risk_percent) and target_risk_percent > 0:
            aim = min(aim, target_risk_percent)
        smaller = evaluate_trade(state, TradeProposal(symbol=info.name, direction=req["side"], entry=entry,
                                                      stop_loss=req["sl"], take_profit=req["tp"],
                                                      strategy_risk_percent=aim),
                                 spec, rules, max_volume)
        if smaller.decision == ACCEPTED and smaller.approved_volume and smaller.approved_volume < req["volume"]:
            resize = {"volume": smaller.approved_volume, "risk_amount": smaller.trade_risk_amount,
                      "risk_percent": smaller.trade_risk_percent, "target_risk_percent": aim,
                      "max_risk_percent": decision.effective_max_risk_percent,
                      "estimates": _estimates(info, req["side"], entry, req["sl"], req["tp"], smaller.approved_volume),
                      "note": "Entry, stop-loss and take-profit are unchanged. Only the volume is smaller."}
    return OrderReview(REJECTED_RISK, False, decision.rejection_reason or "Rejected by the Risk Engine.", order=req,
                       estimates=est, decision=dd, resize=resize, warnings=warnings, currency=cur)


def safe_review(*args, **kwargs) -> OrderReview:
    """Any unexpected failure inside the review means NOT approved. An exception can never become an approval."""
    try:
        return review_order(*args, **kwargs)
    except Exception:  # noqa: BLE001 - deliberate: fail closed, log the details, tell the user plainly
        logger.exception("Risk review crashed; the order was not approved")
        return OrderReview(UNAVAILABLE, False, "The Risk Engine could not evaluate this order, so it was not approved. "
                           "Nothing was sent to MT5. See logs/trademate.log.", cause="internal")


def gate_order(adapter, rules, max_volume, symbol, side, volume, sl, tp, target_risk_percent=None, exclude_tickets=()) -> OrderReview:
    """Used by /api/order. Approved only when the Risk Engine approved EXACTLY the requested volume."""
    review = safe_review(adapter, rules, max_volume, symbol, side, volume, sl, tp, target_risk_percent, exclude_tickets=exclude_tickets)
    if review.approved and (review.decision or {}).get("decision") != ACCEPTED:   # unusable / inconsistent result
        review.approved, review.stage = False, UNAVAILABLE
        review.message = "The Risk Engine returned an unusable result, so the order was not approved."
        review.cause = "internal"
    if review.approved and review.approved_volume is not None and abs(review.approved_volume - float(volume)) > 1e-12:
        review.approved = False
        review.stage = REJECTED_RISK
        review.message = "The approved volume differs from the requested volume. Review the order again."
    return review


# ---- existing positions: report breaches, never act on them ----

def portfolio_breaches(summary: dict, rules: RiskRules) -> list[str]:
    """Plain-language list of limits the CURRENT account state breaks. Reports only; closes nothing."""
    out = []
    if summary.get("daily_locked"):
        out.append("Daily loss limit reached: trading is locked.")
    if summary.get("active_risk_complete") and summary.get("active_risk_limit_amount") is not None \
            and summary["active_risk"] > summary["active_risk_limit_amount"] + 1e-9:
        out.append(f"Total active risk {summary['active_risk_percent']:.2f}% is above the {rules.max_total_active_risk_percent:g}% limit.")
    if summary.get("open_positions", 0) > rules.max_positions:
        out.append(f"{summary['open_positions']} open positions; the limit is {rules.max_positions}.")
    out.extend(f"Active risk unknown: {u}" for u in summary.get("unprotected", []))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Pending orders (Phase 7B). Same Risk Engine, same fail-closed rules, measured from the PENDING price.
# ---------------------------------------------------------------------------------------------------------------------
def _state_with_pending(adapter):
    """The Risk Engine's account view PLUS every pending order as a potential position (worst case: all of them fill).
    That makes pending orders count toward the open-position limit and the total active risk, using the engine's own maths."""
    from dataclasses import replace as _replace
    from trademate.risk.engine import position_risk
    state = gather_state(adapter)
    extra = []
    for o in adapter.get_orders():
        spec = to_spec(adapter.get_symbol_spec(o.symbol))                      # a failure here propagates: fail closed
        extra.append(position_risk(o.ticket, o.symbol, o.direction, o.volume, o.price, o.stop_loss, spec, o.take_profit, 0.0))
    return _replace(state, positions=state.positions + tuple(extra))


def review_pending(adapter, rules: RiskRules, max_volume, order_type, symbol, volume, price, sl, tp, expiration=None,
                   target_risk_percent=None) -> OrderReview:
    """Review a PENDING order. Nothing is sent. Order of checks: type / price / stops / volume / expiry (server-side, never adjusted),
    then the Risk Engine measured from the PENDING price, with all existing pending orders counted as potential positions."""
    import time as _time
    from trademate.errors import OrderRejectedError
    from trademate.pending import PENDING_TYPES, check_type, validate_pending
    try:
        side = check_type(order_type)
    except OrderRejectedError as exc:
        return OrderReview(REJECTED_INPUT, False, str(exc))
    req, problem = clean_request(symbol, side, volume, sl, tp)
    if req is None:
        return OrderReview(REJECTED_INPUT, False, problem)
    if req["sl"] is None:
        return OrderReview(REJECTED_INPUT, False, "A stop-loss is required: the Risk Engine cannot size or approve a trade without one.", order=req)
    try:
        if not adapter.supports_pending_orders():
            return OrderReview(UNAVAILABLE, False, "Pending orders need the updated TradeMateBridge EA (0.5.0 or newer); the running EA does not "
                               "report support. The order was not approved and nothing was sent.", order=req, cause="connection")
        info = adapter.get_symbol_spec(req["symbol"])
        state = _state_with_pending(adapter)
    except (BrokerError, NotImplementedError) as exc:
        return OrderReview(UNAVAILABLE, False, (str(exc) or "MT5 data is not available.") + " The order was not approved and nothing was sent.",
                           order=req, cause="connection")
    try:
        clean = validate_pending(info, order_type, req["volume"], price, req["sl"], req["tp"], expiration, max_volume, _time.time())
    except OrderRejectedError as exc:
        return OrderReview(REJECTED_INPUT, False, str(exc), order={**req, "order_type": order_type, "price": price})
    req = {**req, "volume": clean.volume, "sl": clean.sl, "tp": clean.tp, "order_type": order_type, "price": clean.price,
           "entry": clean.price, "expiration": clean.expiration}
    review = _finish_review(info, state, rules, max_volume, req, clean.price, target_risk_percent)
    review.warnings.append("Pending order: it opens no position until price reaches the entry. The fill price may then differ (gaps, slippage).")
    return review


def gate_pending(adapter, rules, max_volume, order_type, symbol, volume, price, sl, tp, expiration=None, target_risk_percent=None) -> OrderReview:
    """Used by the pending-order route: approved only when the Risk Engine approved EXACTLY the requested volume. Fails closed."""
    try:
        review = review_pending(adapter, rules, max_volume, order_type, symbol, volume, price, sl, tp, expiration, target_risk_percent)
    except Exception:  # noqa: BLE001
        logger.exception("Pending-order risk review crashed; the order was not approved")
        return OrderReview(UNAVAILABLE, False, "The Risk Engine could not evaluate this order, so it was not approved. Nothing was sent.", cause="internal")
    if review.approved and (review.decision or {}).get("decision") != ACCEPTED:
        review.approved, review.stage, review.cause = False, UNAVAILABLE, "internal"
        review.message = "The Risk Engine returned an unusable result, so the order was not approved."
    if review.approved and review.approved_volume is not None and abs(review.approved_volume - float(volume)) > 1e-12:
        review.approved, review.stage = False, REJECTED_RISK
        review.message = "The approved volume differs from the requested volume. Review the order again."
    return review
