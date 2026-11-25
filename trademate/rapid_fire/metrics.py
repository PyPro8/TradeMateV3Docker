"""Money maths for Rapid Fire. Every figure comes from the BROKER's own symbol data; nothing is assumed (10 points is NOT 'N naira')."""

from __future__ import annotations

import math


def _ok(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def tick_value_for(info, loss: bool = False) -> float | None:
    """The broker's tick value (account currency, one tick, one lot): the loss-side / profit-side value when given, else the plain one. None if missing or invalid."""
    if loss and _ok(getattr(info, "tick_value_loss", None)):
        return info.tick_value_loss
    if not loss and _ok(getattr(info, "tick_value_profit", None)):
        return info.tick_value_profit
    return info.tick_value if _ok(getattr(info, "tick_value", None)) else None


def money_per_point_per_lot(info, loss: bool = False) -> float | None:
    """Account-currency value of ONE platform point for ONE lot: tick_value / tick_size * point. None when the broker data is incomplete."""
    tv = tick_value_for(info, loss)
    ts, pt = getattr(info, "tick_size", None), getattr(info, "point", None)
    if tv is None or not _ok(ts) or not _ok(pt):
        return None
    return tv / ts * pt


def estimate(info, lot, points, loss: bool = False) -> float | None:
    """ESTIMATED money for `points` at `lot` (gross, before spread, commission and swap). Labelled an estimate everywhere it is shown."""
    m = money_per_point_per_lot(info, loss)
    return None if m is None or not _ok(lot) or not _ok(points) else m * points * lot


def suggest_lot(info, target_money, target_points) -> dict:
    """The largest valid lot whose ESTIMATED gross profit at `target_points` does not exceed `target_money` (account currency).
    Rounded DOWN to the broker's volume step; None with a reason when it cannot be done honestly."""
    m = money_per_point_per_lot(info)
    if m is None:
        return {"lot": None, "reason": "The broker's tick value / tick size / point are incomplete, so money per point cannot be calculated."}
    if not _ok(target_money) or not _ok(target_points):
        return {"lot": None, "reason": "Enter a target profit above 0."}
    raw = target_money / (m * target_points)
    step, vmin, vmax = getattr(info, "volume_step", None), getattr(info, "volume_min", None), getattr(info, "volume_max", None)
    if not _ok(step) or not _ok(vmin):
        return {"lot": None, "reason": "The broker's volume rules are incomplete."}
    lots = math.floor(raw / step + 1e-9) * step
    lots = round(lots, 8)
    if lots < vmin - 1e-12:
        return {"lot": None, "exact_lot": raw, "reason": f"The target needs {raw:.6g} lot, below the broker minimum {vmin:g}. The smallest tradable lot already earns more than that per shot."}
    if _ok(vmax) and lots > vmax:
        lots = vmax
    return {"lot": lots, "exact_lot": raw, "estimated_profit": m * target_points * lots, "reason": ""}
