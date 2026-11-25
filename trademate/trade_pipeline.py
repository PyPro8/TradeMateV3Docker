"""Controlled hand-off from a qualified strategy result to the existing Risk Engine.

This module is a preview/coordinator only. It never sends orders. Any later execution must
re-evaluate market data and risk, then use the existing /api/order -> gate_order path.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from trademate.errors import BrokerError
from trademate.risk.account_state import gather_state, to_spec
from trademate.risk.engine import ACCEPTED, TradeProposal, evaluate_trade
from trademate.risk.rules import RiskRules

TRADE_DECISIONS = {"BUY": "buy", "SELL": "sell"}


def review_strategy_proposal(adapter, strategy_response: dict[str, Any], rules: RiskRules,
                             max_volume: float | None, risk_percent: float) -> dict[str, Any]:
    """Risk-review a BUY/SELL strategy result at the current executable quote.

    The strategy's structural SL/TP are preserved. Entry is refreshed from MT5's ask/bid because
    the strategy's displayed entry reference is not a guaranteed market fill. No approval is
    persisted: execution must perform a new review immediately before sending.
    """
    if not isinstance(strategy_response, dict) or not strategy_response.get("ok"):
        return {"ok": False, "stage": "strategy_unavailable", "approved": False,
                "message": "A successful strategy evaluation is required before risk review.", "order_sent": False}
    result = strategy_response.get("result")
    if not isinstance(result, dict):
        return {"ok": False, "stage": "strategy_unavailable", "approved": False,
                "message": "The strategy response did not contain a usable result.", "order_sent": False}
    decision = result.get("decision")
    direction = TRADE_DECISIONS.get(decision)
    if direction is None:
        return {"ok": True, "stage": "not_qualified", "approved": False,
                "message": f"Strategy decision is {decision or 'unknown'}; only a qualified BUY or SELL can be risk-reviewed.",
                "strategy_decision": decision, "order_sent": False}
    symbol = result.get("symbol")
    sl = (result.get("stop_loss") or {}).get("price")
    tp = (result.get("take_profit_1") or {}).get("price")
    if not isinstance(symbol, str) or not symbol.strip() or not isinstance(sl, (int, float)) or isinstance(sl, bool):
        return {"ok": False, "stage": "invalid_proposal", "approved": False,
                "message": "The qualified strategy result is missing its symbol or structural stop-loss.", "order_sent": False}
    try:
        info = adapter.get_symbol_spec(symbol)
        entry = info.ask if direction == "buy" else info.bid
        if not isinstance(entry, (int, float)) or isinstance(entry, bool) or entry <= 0:
            return {"ok": False, "stage": "market_unavailable", "approved": False,
                    "message": f"No current executable quote is available for {symbol}.", "order_sent": False}
        state = gather_state(adapter)
        proposal = TradeProposal(symbol=info.name, direction=direction, entry=float(entry), stop_loss=float(sl),
                                 take_profit=float(tp) if isinstance(tp, (int, float)) and not isinstance(tp, bool) else None,
                                 strategy_risk_percent=float(risk_percent))
        risk = evaluate_trade(state, proposal, to_spec(info), rules, max_volume)
    except (BrokerError, NotImplementedError) as exc:
        return {"ok": False, "stage": "market_unavailable", "approved": False,
                "message": f"Current broker/account data is unavailable; the proposal was not approved. {exc}",
                "order_sent": False}
    except Exception:
        # Keep internal details in server logs at the API boundary; never fail open.
        raise
    payload = risk.to_dict()
    approved = risk.decision == ACCEPTED and risk.approved_volume is not None and risk.approved_volume > 0
    return {
        "ok": True,
        "stage": "ready_for_confirmation" if approved else "rejected_risk",
        "approved": approved,
        "message": ("Strategy proposal passed the current Risk Engine review. This is not an execution approval; "
                    "the order must be re-evaluated immediately before submission." if approved else
                    (risk.rejection_reason or "The Risk Engine did not approve this proposal.")),
        "strategy_decision": decision,
        "strategy_rule_id": result.get("rule_id"),
        "setup_family": result.get("selected_setup_family"),
        "proposal": {"symbol": info.name, "side": direction, "entry_reference": float(entry), "volume": risk.approved_volume,
                     "sl": float(sl), "tp": proposal.take_profit, "risk_percent": risk.trade_risk_percent,
                     "risk_amount": risk.trade_risk_amount, "currency": state.currency},
        "risk": payload,
        "order_sent": False,
        "requires_fresh_review_before_execution": True,
    }
