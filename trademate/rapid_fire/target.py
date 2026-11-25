"""Rapid Fire: turn a MONEY target into a take-profit distance, from the broker's own instrument data. Pure functions; nothing is sent anywhere.

    money per tick per lot    = tick_value                      (the broker's number, in the account currency; the profit-side value when the broker gives both)
    money per price unit/lot  = tick_value / tick_size
    money for a move of D     = D * (tick_value / tick_size) * lot

THE SPREAD. A BUY fills at the ask and its exit is judged on the bid; a SELL fills at the bid and its exit is judged on the ask. The fill is therefore
already one spread worse than the exit quote, and the take-profit distance is measured from the FILL to the exit price. A distance of D from the fill
is exactly D * value of profit as MT5 will report it: the spread is already inside it, so the target is NET of the spread. The spread is reported
separately ("the market must move D + spread from the quote you see now") but is never added twice.

COMMISSION is not part of the symbol data the bridge reads. If the broker charges one, enter it (account currency per lot, round turn): it is added to the
money the move has to earn, so the target is net of it. When it is 0 the plan says so explicitly; it is never silently assumed.

The distance is rounded UP to a whole number of ticks, so the net result is at least the requested money, never less. Nothing is guessed: any missing or
invalid instrument number gives a plain-language problem and NO distance.

WHAT THE TARGET IS: an ESTIMATED NET profit per fire: the price move from the FILL to the exit, valued with the broker's tick value (account currency), minus the commission
the user entered. The spread is inside it (the fill is already one spread worse than the exit quote). Swap, slippage on the closing order and any broker commission that was not
entered are NOT included: MT5's deal history holds the real result.

VALIDATION (the bridge exposes no order_calc_profit command, so an equivalent is used):
  * the broker's tick value is the primary number (MT5's own, in the account currency);
  * if it is missing or invalid, and the instrument's PROFIT currency is the account currency, contract_size x tick_size is used instead (that IS MT5's definition of the tick
    value in that case) and the plan says so;
  * if both exist and disagree by more than 2% (same currency), the plan keeps MT5's tick value (futures-style instruments are valued by tick value, not by contract size) but
    carries a visible warning;
  * if neither can be trusted, there is NO distance and NO order.
"""

from __future__ import annotations

import math

from trademate.rapid_fire import metrics


def _ok(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _fail(problem: str, **extra) -> dict:
    return {"ok": False, "problem": problem, "required_points": None, **extra}


def plan(info, lot, profit, commission_per_lot=0.0, bid=None, ask=None, currency: str = "") -> dict:
    """The take-profit plan for `profit` (account currency, net) at `lot` on this instrument."""
    if info is None:
        return _fail("The instrument's MT5 specification is not available, so the take-profit distance cannot be calculated.")
    if not _ok(profit):
        return _fail("Enter a profit per fire above 0.")
    if not _ok(lot):
        return _fail("Set a lot size first: the take-profit distance depends on it.")
    missing = [n for n in ("point", "tick_size") if not _ok(getattr(info, n, None))]
    tick_value = metrics.tick_value_for(info, False)
    value_source, warnings = "mt5_tick_value", []
    cs, tsz = getattr(info, "contract_size", None), getattr(info, "tick_size", None)
    same_ccy = bool(currency) and str(getattr(info, "currency_profit", "") or "").upper() == str(currency).upper() and _ok(cs) and _ok(tsz)
    expected = float(cs) * float(tsz) if same_ccy else None          # money per tick per lot when the profit currency IS the account currency
    if tick_value is None and expected is not None and "tick_size" not in missing:
        tick_value, value_source = expected, "contract_size x tick_size (the instrument's profit currency is the account currency)"
        warnings.append("MT5 gave no usable tick value, so contract size x tick size was used (verified: the profit currency is the account currency).")
    elif tick_value is not None and expected is not None and abs(tick_value - expected) / expected > 0.02:
        warnings.append(f"The broker's tick value ({tick_value:g}) differs from contract size x tick size ({expected:g}) by more than 2%. MT5's own tick value is used "
                        "(futures-style instruments are valued that way); check the first fire's result in MT5 history.")
    if tick_value is None:
        missing.append("tick_value")
    if missing:
        return _fail(f"The broker's data for {getattr(info, 'name', 'this instrument')} is incomplete or invalid ({', '.join(missing)}), so the take-profit distance cannot be "
                     "calculated. Nothing was guessed and no order will be sent.")
    if commission_per_lot is None or not isinstance(commission_per_lot, (int, float)) or isinstance(commission_per_lot, bool) or commission_per_lot < 0 or not math.isfinite(commission_per_lot):
        return _fail("The commission must be 0 or a positive number (account currency per lot).")
    point, tick = float(info.point), float(info.tick_size)
    commission = float(commission_per_lot) * float(lot)
    needed = float(profit) + commission                                    # what the price move itself has to earn
    per_tick = float(tick_value) * float(lot)                              # money for one tick at this lot
    ticks = math.ceil(needed / per_tick - 1e-9)                            # round UP: net is never below the request
    if ticks < 1:
        ticks = 1
    distance = ticks * tick
    points = distance / point
    gross = ticks * per_tick
    spread_points = None
    if _ok(bid) and _ok(ask) and ask >= bid:
        spread_points = (ask - bid) / point
    digits = getattr(info, "digits", None)

    def px(v):
        return round(v, digits) if isinstance(digits, int) and 0 <= digits <= 12 else v

    money_per_point_lot = float(tick_value) / tick * point
    spread_money = None if spread_points is None else spread_points * money_per_point_lot * float(lot)
    if spread_money is not None and spread_money >= 0.5 * float(profit):
        warnings.append(f"The spread alone costs about {spread_money:.2f} {currency or ''} at this lot, {spread_money / float(profit) * 100:.0f}% of the {float(profit):g} target; "
                        "the price must move far past it before the target is reached.".replace("  ", " "))
    out = {"ok": True, "problem": "", "currency": currency or None,
           "target_kind": "estimated_net",
           "target_label": ("Estimated NET profit per fire: price move from the fill, spread included, minus the commission entered. "
                            "Swap, closing slippage and any commission not entered are not included; MT5's history has the real result."),
           "value_source": value_source, "validated": value_source == "mt5_tick_value" and expected is not None and not warnings,
           "warnings": warnings, "spread_money": spread_money, "profit_per_fire": float(profit), "lot": float(lot), "tick_size": tick, "tick_value": float(tick_value), "point": point,
           "money_per_point_per_lot": float(tick_value) / tick * point, "money_per_point": float(tick_value) / tick * point * float(lot),
           "commission": commission, "commission_known": float(commission_per_lot) > 0,
           "commission_note": ("Commission " + f"{commission:g} per round turn is included." if commission > 0 else
                               "Commission is 0: the bridge does not read it. If your broker charges one, enter it so the target is net of it."),
           "required_price_distance": distance, "required_points": points, "required_ticks": ticks, "gross_at_target": gross, "net_at_target": gross - commission,
           "spread_points": spread_points, "spread_note": "The spread is already inside the distance: the fill is at the ask (buy) / bid (sell) and the exit is judged on the bid (buy) / ask (sell)."}
    if _ok(bid) and _ok(ask) and ask >= bid:
        out["buy_tp_price"] = px(ask + distance)                           # a BUY filling at today's ask
        out["sell_tp_price"] = px(bid - distance)                          # a SELL filling at today's bid
        out["move_from_quote_points"] = points + (spread_points or 0.0)    # for display: how far the quote you see now must travel
    return out
