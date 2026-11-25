"""Intelligent lot-size adjustment after an "insufficient funds / margin" rejection (MT5 retcode 10019, "NOT ENOUGH MONEY").

The aim is to REDUCE the position when that is possible, never to increase risk and never to force a trade.
plan_reduced_lot() is pure. It returns the largest lot that satisfies ALL of:
  * strictly smaller than the lot that was just rejected, and different from every lot already rejected for this setup (the same order is never re-sent);
  * the monetary risk at the intended stop-loss stays within the allowed risk amount (recalculated from the broker's tick size / tick value);
  * the margin it needs fits the CURRENT free margin (minus a safety buffer) when the margin requirement per lot is known;
  * within the broker's volume minimum / maximum, rounded DOWN to the broker's volume step.
When the margin requirement per lot is not available from the bridge, it does NOT invent one: it falls back to a bounded reduction (`fallback_factor`, default 0.5)
and says so. If even the minimum lot cannot satisfy the conditions, the trade is rejected with a clear explanation.
The caller (app.py) re-reads the account, runs the new size through the full Risk Engine (daily loss, consecutive losses, emergency lock, per-trade and total risk,
broker limits) and retries only when it is approved, at most `lot_adjust_max_attempts` times.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, Decimal

NO_MONEY_RETCODES = {10019}
NO_MONEY_WORDS = ("not enough money", "no money", "insufficient margin", "insufficient funds", "not enough margin")


def is_insufficient_funds(retcode, message) -> bool:
    return (isinstance(retcode, int) and retcode in NO_MONEY_RETCODES) or any(w in str(message or "").lower() for w in NO_MONEY_WORDS)


def _pos(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def floor_to_step(value: float, vmin: float, step: float) -> float:
    d, lo, st = Decimal(str(value)), Decimal(str(vmin)), Decimal(str(step))
    if d < lo:
        return 0.0
    return float(lo + ((d - lo) / st).to_integral_value(rounding=ROUND_DOWN) * st)


@dataclass
class LotPlan:
    ok: bool
    original_lot: float
    adjusted_lot: float | None = None
    reason: str = ""
    margin_known: bool = False
    risk_at_adjusted: float | None = None
    margin_at_adjusted: float | None = None
    limits: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def plan_reduced_lot(*, original_lot, volume_min, volume_max, volume_step, stop_distance, tick_size, tick_value_loss, max_risk_amount,
                     free_margin, margin_per_lot, failed_lots=(), buffer_fraction=0.10, fallback_factor=0.5) -> LotPlan:
    fail = lambda why, **kw: LotPlan(False, original_lot, None, why, **kw)
    if not all(_pos(v) for v in (original_lot, volume_min, volume_max, volume_step)):
        return fail("The broker's volume limits are not available, so no safe smaller lot can be calculated.")
    if not (_pos(stop_distance) and _pos(tick_size) and _pos(tick_value_loss)):
        return fail("The stop distance or the broker's tick size / tick value is unavailable, so the monetary risk cannot be recalculated.")
    loss_per_lot = stop_distance / tick_size * tick_value_loss
    caps, limits = [], {}
    smaller = min([original_lot, *failed_lots])
    limits["below_rejected_lot"] = smaller
    caps.append(smaller - volume_step)                                    # strictly smaller than anything already rejected
    if _pos(max_risk_amount):
        limits["risk_limit_lot"] = max_risk_amount / loss_per_lot
        caps.append(limits["risk_limit_lot"])
    margin_known = _pos(margin_per_lot) and _pos(free_margin)
    if margin_known:
        limits["margin_limit_lot"] = free_margin * (1.0 - buffer_fraction) / margin_per_lot
        caps.append(limits["margin_limit_lot"])
    else:
        limits["bounded_reduction_lot"] = smaller * fallback_factor
        caps.append(limits["bounded_reduction_lot"])
    lot = floor_to_step(min(min(caps), volume_max), volume_min, volume_step)
    while lot and (lot in failed_lots or lot >= original_lot):
        lot = floor_to_step(lot - volume_step, volume_min, volume_step)
    if not lot:
        if margin_known:
            need = volume_min * margin_per_lot
            return fail(f"Not enough money: the minimum lot {volume_min:g} needs {need:,.2f} margin but only {free_margin:,.2f} is free "
                        f"(buffer {buffer_fraction:.0%} kept). The trade is rejected; no order was forced.", margin_known=True, limits=limits)
        return fail(f"Not enough money: no lot between the broker minimum {volume_min:g} and the rejected {smaller:g} satisfies the risk limit and a bounded reduction. "
                    "The margin requirement per lot is not available from the bridge. The trade is rejected; no order was forced.", limits=limits)
    why = ("margin requirement and free margin" if margin_known else "bounded reduction (the margin requirement per lot is not available from the bridge)")
    others = [v for k, v in limits.items() if k not in ("risk_limit_lot", "below_rejected_lot")]
    if "risk_limit_lot" in limits and limits["risk_limit_lot"] < smaller and all(limits["risk_limit_lot"] <= o + 1e-12 for o in others):
        why += " and the allowed risk at the stop-loss"
    return LotPlan(True, original_lot, lot, f"Insufficient funds for {original_lot:g} lots; reduced to {lot:g} using {why}.", margin_known, lot * loss_per_lot,
                   lot * margin_per_lot if margin_known else None, limits)
