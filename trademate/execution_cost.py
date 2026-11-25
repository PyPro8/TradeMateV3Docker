"""Execution-cost assessment: is the live spread compatible with THIS trade's own stop and target?

This replaces the old universal rule "the spread may be at most 0.25 x the live stop distance" (an unexplained multiplier that was applied to
every strategy and every instrument alike). The assessment is built from the trade itself, in PRICE units (bid, ask, stop and target are all
prices, so no broker-point / strategy-pip mix-up is possible here), and uses no tunable multiplier by default:

  * STOP  A market order fills at the ask (BUY) / bid (SELL), and the stop is hit by the opposite side of the quote. If the spread is at least
          the whole distance from the fill to the stop, the stop is already touched the moment the order is filled: the trade can only
          lose. This is arithmetic, not policy, and always blocks.
  * TARGET The take-profit is hit by the opposite side of the quote as well, so the market must travel the target distance PLUS the spread.
          If the spread is at least the whole target distance, a winning trade nets nothing: blocked as uneconomic.
  * COST  Between those limits the spread is real but the trade is workable. Its share of the stop and of the target is reported (and
          warned about) so the decision is transparent, but it does not block by itself.

An OPTIONAL stricter ratio ([pipeline] max_spread_stop_fraction, 0 = off, the default) remains available for users who want one; when it is set
the message says it is the user's own setting.
"""

from __future__ import annotations

import math

SPREAD_TOO_WIDE = "spread_too_wide"
SPREAD_INVALID = "spread_invalid"


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def assess_spread(side: str, live_entry: float, stop_loss: float, take_profit: float, bid: float, ask: float,
                  pip_size: float | None = None, user_max_fraction: float | None = None) -> dict:
    """-> {"problems": [(code, message)], "warnings": [str], "facts": {...}}. Pure: no broker, no clock."""
    spread = ask - bid
    stop_dist = abs(live_entry - stop_loss)
    target_dist = abs(take_profit - live_entry)
    facts = {"spread_price": spread, "stop_distance_price": stop_dist, "target_distance_price": target_dist, "unit_price": pip_size,
             "spread_units": (spread / pip_size) if _num(pip_size) and pip_size > 0 else None,
             "stop_distance_units": (stop_dist / pip_size) if _num(pip_size) and pip_size > 0 else None,
             "target_distance_units": (target_dist / pip_size) if _num(pip_size) and pip_size > 0 else None,
             "spread_share_of_stop": (spread / stop_dist) if stop_dist > 0 else None,
             "spread_share_of_target": (spread / target_dist) if target_dist > 0 else None, "policy": "auto"}
    problems, warnings = [], []
    if not (_num(spread) and spread >= 0):
        problems.append((SPREAD_INVALID, f"The quote is inconsistent (bid {bid:g}, ask {ask:g}); the spread cannot be measured."))
        return {"problems": problems, "warnings": warnings, "facts": facts}
    u = f" ({facts['spread_units']:.2f} units)" if facts["spread_units"] is not None else ""
    if stop_dist > 0 and spread >= stop_dist - 1e-12:
        problems.append((SPREAD_TOO_WIDE, f"Execution cost: the spread {spread:g}{u} is as large as the whole stop distance {stop_dist:g} "
                                          f"(live entry {live_entry:g}, stop {stop_loss:g}), so the stop would be touched the moment the order fills. "
                                          "This is a genuinely unfavourable spread, not a unit mismatch (both are prices)."))
    elif target_dist > 0 and spread >= target_dist - 1e-12:
        problems.append((SPREAD_TOO_WIDE, f"Execution cost: the spread {spread:g}{u} is as large as the whole target distance {target_dist:g}, "
                                          "so even a winning trade would net nothing after the spread."))
    if user_max_fraction and stop_dist > 0 and spread > user_max_fraction * stop_dist + 1e-12 and not problems:
        facts["policy"] = "user_fraction"
        problems.append((SPREAD_TOO_WIDE, f"Your own spread limit ([pipeline] max_spread_stop_fraction = {user_max_fraction:g}) is exceeded: the spread {spread:g}{u} is more than "
                                          f"{user_max_fraction:g} x the live stop distance ({user_max_fraction * stop_dist:g})."))
    if not problems and spread > 0:
        if stop_dist > 0 and spread / stop_dist >= 0.5:
            warnings.append(f"The spread is {spread / stop_dist * 100:.0f}% of the stop distance.")
        if target_dist > 0 and spread / target_dist >= 0.5:
            warnings.append(f"The spread is {spread / target_dist * 100:.0f}% of the target distance.")
    return {"problems": problems, "warnings": warnings, "facts": facts}
