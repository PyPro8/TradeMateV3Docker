"""Risk Engine decision (v5.2): one central ACCEPTED / REJECTED for a proposed trade.

Pure logic: it receives an AccountRiskState (built from MT5 data by account_state.py), a TradeProposal (later: the
Strategy Engine's output; for now simulated), the user's RiskRules and the MT5 symbol spec. It places nothing.

Checks, in order. EVERY check is evaluated and listed; the final reason is the first one that failed:
    1 daily loss lock      today's loss has reached the daily limit  -> TRADING LOCKED
    2 per-trade risk       proposed risk <= max risk per trade
    3 remaining daily      proposed risk <= what is left of today's allowance
    4 position count       open positions < max simultaneous positions
    5 total active risk    existing active risk + proposed risk <= max total active risk
    6 broker constraints   position sizing on the broker's min / max / step (and a valid SL for the direction)
Effective max risk for the trade = the strictest of per-trade, remaining daily and remaining active risk.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from trademate.risk.position_sizing import (RiskLimits, SymbolSpec, TradeRequest, calculate_position_size)
from trademate.risk.rules import RiskRules

_PROTECTIONS = {"provider": None}


def set_protections_provider(fn) -> None:
    """Install the central protections (emergency lock, consecutive losses): fn() -> [{name, passed, detail}]. Consulted by EVERY evaluate_trade() call,
    so no strategy, AI feature or execution route can reach an approval without them. None = no extra checks (the baseline behaviour)."""
    _PROTECTIONS["provider"] = fn


ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
EPS = 1e-9


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _within(value: float, limit: float) -> bool:
    return value <= limit + EPS * max(1.0, abs(limit))


def loss_per_lot_at_stop(spec: SymbolSpec | None, distance: float) -> float | None:
    """Money lost for 1 lot when price moves `distance` against the trade, from the broker's tick data."""
    if spec is None or not (_num(spec.tick_size) and spec.tick_size > 0) or not _num(distance) or distance < 0:
        return None
    tick_value = spec.tick_value_loss if _num(spec.tick_value_loss) and spec.tick_value_loss > 0 else spec.tick_value
    if not (_num(tick_value) and tick_value > 0):
        return None
    return (distance / spec.tick_size) * tick_value


@dataclass(frozen=True)
class OpenPositionRisk:
    """What an open position can still lose if its stop-loss is hit, measured from its open price."""

    ticket: int
    symbol: str
    direction: str
    volume: float | None
    price_open: float | None
    stop_loss: float | None
    risk_amount: float | None  # loss if the SL is hit (NOT the same thing as floating_pl). None = cannot be calculated
    note: str = ""  # why it cannot be calculated
    take_profit: float | None = None
    floating_pl: float | None = None  # MT5's live profit + swap for the position

    def to_dict(self) -> dict:
        return asdict(self)


def position_risk(ticket, symbol, direction, volume, price_open, stop_loss, spec: SymbolSpec | None,
                  take_profit=None, floating_pl=None) -> OpenPositionRisk:
    base = dict(ticket=ticket, symbol=symbol, direction=direction, volume=volume, price_open=price_open, stop_loss=stop_loss,
                take_profit=take_profit, floating_pl=floating_pl)
    if not _num(stop_loss) or stop_loss <= 0:
        return OpenPositionRisk(**base, risk_amount=None, note="no stop-loss")
    if not (_num(volume) and volume > 0 and _num(price_open) and direction in ("buy", "sell")):
        return OpenPositionRisk(**base, risk_amount=None, note="incomplete position data")
    # Signed distance to the stop: positive = the stop is on the losing side of the open price.
    adverse = (price_open - stop_loss) if direction == "buy" else (stop_loss - price_open)
    if adverse <= 0:  # stop already at/after break-even: nothing at risk from the open price
        return OpenPositionRisk(**base, risk_amount=0.0)
    lpl = loss_per_lot_at_stop(spec, adverse)
    if lpl is None:
        return OpenPositionRisk(**base, risk_amount=None, note="no MT5 tick data for the symbol")
    return OpenPositionRisk(**base, risk_amount=lpl * volume)


@dataclass(frozen=True)
class AccountRiskState:
    equity: float | None
    balance: float | None
    currency: str
    start_of_day_balance: float | None  # balance minus today's closed results
    todays_closed_pl: float | None  # net of closed trades since the broker's day start; None = history unavailable
    floating_pl: float | None  # open positions' profit + swap
    positions: tuple = ()  # tuple[OpenPositionRisk, ...]
    day_start_source: str = ""
    history_error: str = ""  # why the daily loss is unknown (history, clock or data problem); "" = nothing wrong
    day_start: int | None = None  # start of the broker's trading day, on the broker clock (MT5 epoch)
    server_offset_seconds: int | None = None  # broker clock minus UTC
    next_reset_utc: int | None = None  # UTC epoch when the daily counters next start again


@dataclass(frozen=True)
class TradeProposal:
    """What the future Strategy Engine will hand to the Risk Engine. For now it is simulated."""

    symbol: str
    direction: str
    entry: float | None
    stop_loss: float | None
    take_profit: float | None = None
    strategy_risk_percent: float | None = None  # risk the strategy wants, % of equity
    volume: float | None = None  # optional: judge a given size instead of calculating one


@dataclass(frozen=True)
class RiskDecision:
    """The pre-trade validation result. Phase 6 (execution) is meant to use `trade` as is, without recalculating risk.

    It is a DECISION ONLY: `order_sent` is always False; nothing in the Risk Engine can send an order."""

    decision: str  # ACCEPTED / REJECTED
    rejection_reason: str | None  # the first failed check
    rejection_reasons: list = field(default_factory=list)  # EVERY failed check
    trading_locked: bool = False
    ready_for_execution: bool = False  # True only for ACCEPTED. It does NOT mean an order was sent.
    order_sent: bool = False
    approved_volume: float | None = None  # final approved size (None unless ACCEPTED)
    ideal_volume: float | None = None  # exact size for the requested risk, before broker rules
    broker_valid_volume: float | None = None  # ideal rounded DOWN to the broker's step, within min / max
    trade_risk_amount: float | None = None  # money at risk at the SL for the size that would be used (rounded-down size)
    trade_risk_percent: float | None = None  # same, % of equity
    requested_risk_amount: float | None = None  # what the strategy asked for
    requested_risk_percent: float | None = None
    effective_max_risk_percent: float | None = None
    effective_max_risk_amount: float | None = None
    limiting_rule: str | None = None
    checks: list = field(default_factory=list)  # [{name, passed, status, detail}]
    sizing: dict | None = None
    proposal: dict | None = None
    state: dict | None = None
    trade: dict | None = None  # the execution-ready record

    def to_dict(self) -> dict:
        return asdict(self)


def trade_param_problems(direction, entry, sl, tp, risk_percent, volume) -> list[str]:
    """Everything wrong with the strategy's numbers. An empty list means the parameters are usable."""
    out = []
    if direction not in ("buy", "sell"):
        out.append("Direction must be BUY or SELL.")
    if not _num(entry) or entry <= 0:
        out.append("Entry price is missing or invalid.")
    if not _num(sl) or sl <= 0:
        out.append("Stop-loss is missing or invalid.")
    if _num(entry) and _num(sl) and entry > 0 and sl > 0:
        if entry == sl:
            out.append("Stop distance is zero: the stop-loss equals the entry price.")
        elif direction == "buy" and sl >= entry:
            out.append("Invalid BUY: the stop-loss must be BELOW the entry price.")
        elif direction == "sell" and sl <= entry:
            out.append("Invalid SELL: the stop-loss must be ABOVE the entry price.")
    if tp is not None:
        if not _num(tp) or tp <= 0:
            out.append("Take-profit is invalid.")
        elif _num(entry) and entry > 0:
            if direction == "buy" and tp <= entry:
                out.append("Invalid BUY: the take-profit must be ABOVE the entry price.")
            elif direction == "sell" and tp >= entry:
                out.append("Invalid SELL: the take-profit must be BELOW the entry price.")
    if not (_num(volume) and volume > 0) and not (_num(risk_percent) and risk_percent > 0):
        out.append("Strategy risk percentage is missing or not greater than 0.")
    return out


def summarize_state(state: AccountRiskState, rules: RiskRules) -> dict:
    """Plain numbers for the UI and for the checks. Money first, percentages for display."""
    eq, start = state.equity, state.start_of_day_balance
    known = state.todays_closed_pl is not None and _num(state.floating_pl) and _num(start) and _num(eq)
    todays_pl = (state.todays_closed_pl + state.floating_pl) if known else None
    todays_loss = max(0.0, -todays_pl) if todays_pl is not None else None
    daily_limit = start * rules.max_daily_loss_percent / 100.0 if _num(start) else None
    remaining = max(0.0, daily_limit - todays_loss) if (daily_limit is not None and todays_loss is not None) else None
    locked = bool(todays_loss is not None and daily_limit is not None and not (todays_loss + EPS * max(1.0, daily_limit) < daily_limit))
    calc = [p for p in state.positions if p.risk_amount is not None]
    unknown = [p for p in state.positions if p.risk_amount is None]
    active = sum(p.risk_amount for p in calc)
    active_limit = eq * rules.max_total_active_risk_percent / 100.0 if _num(eq) else None
    pct = lambda v, base: (v / base * 100.0) if (v is not None and _num(base) and base > 0) else None
    return {
        "equity": eq, "balance": state.balance, "currency": state.currency,
        "start_of_day_balance": start, "day_start_source": state.day_start_source,
        "day_start": state.day_start, "server_offset_seconds": state.server_offset_seconds, "next_reset_utc": state.next_reset_utc,
        "todays_closed_pl": state.todays_closed_pl, "floating_pl": state.floating_pl, "todays_pl": todays_pl,
        "todays_loss": todays_loss, "todays_loss_percent": pct(todays_loss, start),
        "daily_limit_amount": daily_limit, "daily_remaining": remaining, "daily_remaining_percent": pct(remaining, start),
        "daily_locked": locked, "history_error": state.history_error,
        "open_positions": len(state.positions),
        "active_risk": active, "active_risk_percent": pct(active, eq), "active_risk_complete": not unknown,
        "active_risk_limit_amount": active_limit,
        "active_risk_remaining": max(0.0, active_limit - active) if active_limit is not None else None,
        "positions": [p.to_dict() for p in state.positions],
        "unprotected": [f"#{p.ticket} {p.symbol} ({p.note})" for p in unknown],
        "rules": rules.to_dict(),
    }


def evaluate_trade(state: AccountRiskState, proposal: TradeProposal, spec: SymbolSpec | None, rules: RiskRules,
                   max_order_volume: float | None = None) -> RiskDecision:
    s = summarize_state(state, rules)
    eq = state.equity
    pdict = asdict(proposal)
    if not (_num(eq) and eq > 0):
        why = "Account equity is not available from MT5."
        return RiskDecision(REJECTED, why, [why], proposal=pdict, state=s)

    direction = (proposal.direction or "").strip().lower() if isinstance(proposal.direction, str) else ""
    entry, sl, tp = proposal.entry, proposal.stop_loss, proposal.take_profit
    given = proposal.volume if _num(proposal.volume) and proposal.volume > 0 else None
    problems = trade_param_problems(direction, entry, sl, tp, proposal.strategy_risk_percent, given)
    distance = abs(entry - sl) if not problems and _num(entry) and _num(sl) else None
    lpl = loss_per_lot_at_stop(spec, distance) if distance else None

    # ---- requested risk, then the size that satisfies it ----
    sizing = None
    if given is not None:
        requested = given * lpl if lpl is not None else None
    elif _num(proposal.strategy_risk_percent) and proposal.strategy_risk_percent > 0:
        requested = eq * proposal.strategy_risk_percent / 100.0
    else:
        requested = None
    ideal = broker_volume = None
    broker_problem = None
    if problems:
        broker_problem = "Not evaluated: fix the trade parameters first."
    elif given is not None:
        broker_volume, broker_problem = _check_given_volume(proposal, spec, direction, max_order_volume)
        ideal = given
        if lpl is None and broker_problem is None:
            broker_problem = f"MT5 tick data is not available for '{proposal.symbol}'."
    else:
        # The 5.1 maths. Its own per-trade cap is lifted: the user's rules are judged by the checks below.
        sizing = calculate_position_size(eq, TradeRequest(proposal.symbol, direction, entry, sl, proposal.strategy_risk_percent),
                                         spec, RiskLimits(max_risk_percent=100.0, max_volume=max_order_volume))
        ideal, broker_volume, broker_problem = sizing.calculated_volume, sizing.normalized_volume, sizing.rejection_reason
    # The risk that counts is the risk of the size that would really be traded (rounded down), not the ideal one.
    actual = None
    if broker_volume is not None and lpl is not None:
        actual = broker_volume * lpl
    elif sizing is not None and sizing.estimated_loss is not None:
        actual = sizing.estimated_loss
    amount = actual if actual is not None else requested  # used by the checks
    req_pct = requested / eq * 100.0 if requested is not None else None
    act_pct = actual / eq * 100.0 if actual is not None else None

    checks = []

    def check(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "status": "PASS" if passed else "FAIL", "detail": detail})

    cur = state.currency or ""
    fm = lambda v: f"{v:,.2f} {cur}".strip()
    unknown = "Not evaluated: the trade's risk is unknown."

    check("Trade parameters", not problems, " ".join(problems) if problems else
          f"{direction.upper()} {proposal.symbol}: entry, stop-loss" + (" and take-profit" if tp is not None else "") + " are valid.")
    # daily loss lock
    if s["todays_loss"] is None:
        check("Daily loss limit", False, "Today's loss cannot be determined (the broker's trading day, today's closed trades or the open positions' results are unavailable), so the daily loss limit cannot be verified. "
              + (state.history_error or "").strip())
        locked = False
    else:
        locked = s["daily_locked"]
        check("Daily loss limit", not locked,
              (f"Daily loss limit reached. Lost {fm(s['todays_loss'])} ({s['todays_loss_percent']:.2f}%) of the "
               f"{fm(s['daily_limit_amount'])} ({rules.max_daily_loss_percent:g}%) allowed today.") if locked else
              f"Lost {fm(s['todays_loss'])} of {fm(s['daily_limit_amount'])} allowed today.")
    # risk per trade (judged on what the strategy REQUESTED, so rounding down can never hide an over-limit request)
    max_trade_amount = eq * rules.max_risk_per_trade_percent / 100.0
    if requested is None:
        check("Risk per trade", False, unknown)
    else:
        check("Risk per trade", _within(requested, max_trade_amount),
              f"Requested risk {req_pct:.2f}% ({fm(requested)}); maximum {rules.max_risk_per_trade_percent:g}% ({fm(max_trade_amount)})."
              + (f" Actual at the rounded-down size: {act_pct:.2f}% ({fm(actual)})." if actual is not None else ""))
    # remaining daily
    if amount is None:
        check("Remaining daily risk", False, unknown)
    elif s["daily_remaining"] is None:
        check("Remaining daily risk", False, "Unknown (see daily loss limit).")
    else:
        check("Remaining daily risk", _within(amount, s["daily_remaining"]),
              f"Trade risk {fm(amount)}; remaining today {fm(s['daily_remaining'])} ({s['daily_remaining_percent']:.2f}%).")
    # positions
    n = s["open_positions"]
    check("Simultaneous positions", n < rules.max_positions, f"Current: {n}, Maximum: {rules.max_positions}.")
    # total active risk
    if not s["active_risk_complete"]:
        check("Total active risk", False, "Active risk cannot be calculated: " + "; ".join(s["unprotected"]) + ". Set a stop-loss on it first.")
    elif amount is None:
        check("Total active risk", False, unknown)
    else:
        total = s["active_risk"] + amount
        check("Total active risk", _within(total, s["active_risk_limit_amount"]),
              f"Existing {fm(s['active_risk'])} ({(s['active_risk_percent'] or 0):.2f}%) + new {fm(amount)} ({amount / eq * 100:.2f}%) = "
              f"{total / eq * 100:.2f}%; maximum {rules.max_total_active_risk_percent:g}%.")
    # central protections: emergency lock, consecutive losses
    provider = _PROTECTIONS["provider"]
    protection_failed = False
    if provider is not None:
        try:
            extra = provider()
        except Exception as exc:                                                       # noqa: BLE001 - if the protections cannot be read, fail CLOSED
            extra = [{"name": "Safety protections", "passed": False, "detail": f"The emergency-lock / consecutive-loss protections could not be read ({type(exc).__name__}); no order is approved."}]
        for c in extra:
            check(c["name"], c["passed"], c["detail"])
            protection_failed = protection_failed or not c["passed"]
    # broker constraints
    check("Broker constraints", broker_problem is None and broker_volume is not None,
          broker_problem or f"{broker_volume:g} lots is valid for this symbol's min / max / step.")

    # ---- effective maximum risk: the strictest rule ----
    candidates = [(max_trade_amount, "Max risk per trade")]
    if s["daily_remaining"] is not None:
        candidates.append((s["daily_remaining"], "Remaining daily loss"))
    if s["active_risk_complete"]:
        candidates.append((s["active_risk_remaining"], "Remaining active risk"))
    eff_amount, limiting = min(candidates, key=lambda c: c[0])

    failures = [c for c in checks if not c["passed"]]
    reasons_lead = {"Risk per trade": "Trade exceeds maximum risk per trade.",
                    "Remaining daily risk": "Trade risk exceeds the remaining daily loss allowance.",
                    "Simultaneous positions": "Maximum simultaneous position limit reached.",
                    "Total active risk": "Total active risk would exceed the maximum."}

    def reason(c):
        lead = reasons_lead.get(c["name"], "")
        return (lead + " " + c["detail"]).strip() if lead else c["detail"]

    accepted = not failures
    final = broker_volume if accepted else None
    trade = {
        "symbol": proposal.symbol, "direction": direction, "entry": entry, "stop_loss": sl, "take_profit": tp,
        "risk_percent_requested": req_pct, "risk_amount_requested": requested,
        "risk_percent": act_pct, "risk_amount": actual,
        "ideal_volume": ideal, "broker_valid_volume": broker_volume, "approved_volume": final,
        "account_equity_used": eq, "account_balance_used": state.balance, "currency": state.currency,
        "limits": rules.to_dict(), "decision": ACCEPTED if accepted else REJECTED,
        "checks": checks, "rejection_reasons": [reason(c) for c in failures],
        "order_sent": False,
    }
    return RiskDecision(
        decision=ACCEPTED if accepted else REJECTED, rejection_reason=reason(failures[0]) if failures else None,
        rejection_reasons=[reason(c) for c in failures], trading_locked=locked or protection_failed, ready_for_execution=accepted, order_sent=False,
        approved_volume=final, ideal_volume=ideal, broker_valid_volume=broker_volume,
        trade_risk_amount=actual if actual is not None else requested, trade_risk_percent=act_pct if actual is not None else req_pct,
        requested_risk_amount=requested, requested_risk_percent=req_pct,
        effective_max_risk_percent=eff_amount / eq * 100.0, effective_max_risk_amount=eff_amount, limiting_rule=limiting,
        checks=checks, sizing=sizing.to_dict() if sizing else None, proposal=pdict, state=s, trade=trade)


def _check_given_volume(p: TradeProposal, spec: SymbolSpec | None, direction: str, cap: float | None):
    if direction not in ("buy", "sell") or not _num(p.entry) or not _num(p.stop_loss) or p.entry == p.stop_loss:
        return None, "Entry and stop-loss must be valid prices."
    if (direction == "buy" and p.stop_loss >= p.entry) or (direction == "sell" and p.stop_loss <= p.entry):
        return None, f"Invalid {direction.upper()}: the stop-loss is on the wrong side of the entry."
    if spec is None or not all(_num(v) and v > 0 for v in (spec.volume_min, spec.volume_max, spec.volume_step)):
        return None, "MT5 reported no valid volume limits for this symbol."
    v = p.volume
    if v < spec.volume_min or v > spec.volume_max:
        return None, f"Volume {v:g} is outside the broker range {spec.volume_min:g} to {spec.volume_max:g}."
    if cap is not None and v > cap:
        return None, f"Volume {v:g} is above TradeMate's per-order maximum of {cap:g}."
    steps = (Decimal(str(v)) - Decimal(str(spec.volume_min))) / Decimal(str(spec.volume_step))
    if steps != steps.to_integral_value():
        return None, f"Volume {v:g} does not fit the broker's volume step of {spec.volume_step:g}."
    return v, None
