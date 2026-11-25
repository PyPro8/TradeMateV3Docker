"""Protective-stop and target money calculator (preview only: it never sends or authorises an order).

Money per lot comes ONLY from the broker's own tick size and tick values (already in ACCOUNT currency, so no FX table and no hard-coded pip value):
    price distance = distance in strategy units x unit size        (unit size from trademate.instruments: verified, never guessed)
    gain  = distance / tick_size x tick_value_profit x lots        loss = distance / tick_size x tick_value_loss x lots
If any required broker value is missing, inconsistent or unverified the result has trusted = False, NO money figures, and says why: it is never "approximately right".
Reverse: given the most the user will risk (account currency) and a stop distance, the allowed lot = max risk / loss per lot, rounded DOWN to the broker's volume step,
within min / max. If even the minimum lot risks more than allowed, no lot is offered and the minimum lot's risk is shown.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, Decimal

from trademate.lot_adjust import floor_to_step


def _pos(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def calculate(*, volume=None, target_units=None, stop_units=None, unit_size=None, tick_size=None, tick_value_profit=None, tick_value_loss=None, balance=None, equity=None,
              currency="", volume_min=None, volume_max=None, volume_step=None, digits=None, verified=True, problems=(), max_risk_amount=None) -> dict:
    reasons = []
    if not verified:
        reasons.append("The instrument's pip/point conversion is UNVERIFIED: " + " ".join(problems or ["no reason given."]))
    if not _pos(unit_size):
        reasons.append("The pip/point size of this instrument is unknown.")
    if not _pos(tick_size):
        reasons.append("The broker did not report the tick size.")
    if not _pos(tick_value_loss):
        reasons.append("The broker did not report the tick value.")
    if not currency:
        reasons.append("The account currency is unknown.")
    if not all(_pos(v) for v in (volume_min, volume_max, volume_step)):
        reasons.append("The broker did not report valid volume limits.")
    out = {"trusted": not reasons, "reasons": reasons, "currency": currency, "gain_amount": None, "loss_amount": None, "risk_percent_balance": None, "risk_percent_equity": None,
           "reward_to_risk": None, "loss_per_lot": None, "gain_per_lot": None, "price_distance_target": None, "price_distance_stop": None, "volume": volume,
           "volume_valid": None, "volume_message": "", "allowed_lot": None, "allowed_lot_risk": None, "allowed_lot_message": "",
           "note": "Estimate from the broker's tick size and tick value; spread, slippage and swap are not included. Preview only."}
    if reasons:
        out["message"] = "This calculation cannot be trusted: " + " ".join(reasons)
        return out
    pv = tick_value_profit if _pos(tick_value_profit) else tick_value_loss
    per_unit_loss = unit_size / tick_size * tick_value_loss
    per_unit_gain = unit_size / tick_size * pv
    out["loss_per_lot"], out["gain_per_lot"] = (per_unit_loss * stop_units if _pos(stop_units) else None), (per_unit_gain * target_units if _pos(target_units) else None)
    if _pos(volume):
        steps = (Decimal(str(volume)) - Decimal(str(volume_min))) / Decimal(str(volume_step))
        ok = volume_min <= volume <= volume_max and steps == steps.to_integral_value()
        out["volume_valid"] = ok
        out["volume_message"] = "" if ok else f"{volume:g} lots does not fit the broker's volume rules (min {volume_min:g}, max {volume_max:g}, step {volume_step:g})."
        if _pos(target_units):
            out["gain_amount"] = per_unit_gain * target_units * volume
            out["price_distance_target"] = target_units * unit_size
        if _pos(stop_units):
            out["loss_amount"] = per_unit_loss * stop_units * volume
            out["price_distance_stop"] = stop_units * unit_size
            if _pos(balance):
                out["risk_percent_balance"] = out["loss_amount"] / balance * 100.0
            if _pos(equity):
                out["risk_percent_equity"] = out["loss_amount"] / equity * 100.0
        if out["gain_amount"] is not None and out["loss_amount"]:
            out["reward_to_risk"] = out["gain_amount"] / out["loss_amount"]
    if _pos(max_risk_amount) and _pos(stop_units):
        lpl = per_unit_loss * stop_units
        lot = floor_to_step(min(max_risk_amount / lpl, volume_max), volume_min, volume_step)
        if lot:
            out["allowed_lot"], out["allowed_lot_risk"] = lot, lot * lpl
            out["allowed_lot_message"] = f"{lot:g} lots risks {lot * lpl:,.2f} {currency} at the stop (limit {max_risk_amount:,.2f})."
        else:
            out["allowed_lot_message"] = (f"Even the minimum lot {volume_min:g} would risk {volume_min * lpl:,.2f} {currency} at this stop, more than your limit of "
                                          f"{max_risk_amount:,.2f}. Widen the limit or tighten the stop; no lot is offered.")
    out["message"] = "Estimated from the broker's tick size and tick value."
    return out
