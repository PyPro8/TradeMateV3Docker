"""Two-way stop-loss calculator (Phase 7A.1). PREVIEW ONLY: it never sends an order and never authorises one.

    MODE "risk":   risk amount  -> SL price     (how far may the stop be so that a loss at it is at most this amount?)
    MODE "price":  SL price     -> risk amount  (what is lost if the stop is hit?)

ONE CALCULATOR, NOT TWO. Money at a stop is always taken from the Risk Engine's own `loss_per_lot_at_stop` (broker tick size
and tick value, in ACCOUNT currency), the same function the Risk Engine uses to judge and size real trades. This module only
adds the inverse (price from money) and the tick-grid / direction / limit checks around it, so the two modes cannot disagree
with each other or with the engine.

SAFETY RULES
  * The SL price is rounded TOWARD the entry (to the tick grid), so the loss at it can only be <= the requested risk, never
    more. If the request cannot be met on the grid the result says so and shows the actual risk; it never silently adds risk.
  * BUY: the stop must be below the entry. SELL: above. Anything else is invalid, never silently flipped.
  * A stop closer than the broker's minimum stop distance is refused (the requested risk is too small to place a valid stop).
  * Missing / invalid inputs (entry, direction, volume, tick size, tick value, volume limits) give a clear reason, no number.
  * Quantities are named exactly: `risk_amount` is the MONETARY LOSS at the stop in account currency. It is not allocated
    capital and not notional exposure, and the result says so.
  * The result is a preview. /api/order re-reads price and spec and the Risk Engine re-judges everything before any order.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from trademate.risk.engine import loss_per_lot_at_stop
from trademate.risk.position_sizing import SymbolSpec

MODES = ("risk", "price")
QUANTITY_NOTE = ("Risk amount = the money lost if the stop-loss is hit, in the account currency. It is not allocated capital "
                 "and not notional exposure.")


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _d(x) -> Decimal:
    return Decimal(str(x))


@dataclass
class SlResult:
    mode: str
    valid: bool = False
    message: str = ""
    direction: str = ""
    entry: float | None = None
    volume: float | None = None
    stop_loss: float | None = None            # the SL price (calculated in "risk" mode, validated in "price" mode)
    risk_amount: float | None = None          # money lost at stop_loss for `volume` lots (the FINAL figure)
    requested_risk: float | None = None       # what the user asked for ("risk" mode only)
    difference: float | None = None           # requested_risk - risk_amount (>= 0: the tick grid can only lower risk)
    stop_distance: float | None = None
    stop_ticks: int | None = None
    loss_per_lot: float | None = None
    tick_size: float | None = None
    nearest_valid_stop: float | None = None   # only when a typed SL is off the tick grid
    currency: str = ""
    notes: list = field(default_factory=list)
    quantity_note: str = QUANTITY_NOTE
    _per_tick: float | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_per_tick", None)
        return d


def _fail(r: SlResult, message: str, **extra) -> SlResult:
    r.valid, r.message = False, message
    for k, v in extra.items():
        setattr(r, k, v)
    return r


def _check_common(r: SlResult, direction, entry, volume, spec: SymbolSpec | None) -> SlResult | None:
    """Everything both modes need. Returns a failed result, or None when the inputs are usable."""
    d = direction.strip().lower() if isinstance(direction, str) else ""
    r.direction = d
    if d not in ("buy", "sell"):
        return _fail(r, "Direction must be BUY or SELL.")
    if not _num(entry) or entry <= 0:
        return _fail(r, "Entry price is missing or invalid.")
    r.entry = float(entry)
    if spec is None:
        return _fail(r, "No MT5 specification is available for this symbol.")
    if not (_num(spec.tick_size) and spec.tick_size > 0):
        return _fail(r, "MT5 reported no valid tick size for this symbol.")
    r.tick_size = float(spec.tick_size)
    per_tick = loss_per_lot_at_stop(spec, spec.tick_size)
    if per_tick is None or per_tick <= 0:
        return _fail(r, "MT5 reported no valid tick value for this symbol, so money cannot be calculated "
                        "(the conversion to the account currency is unavailable).")
    if spec.trade_mode == 0:
        return _fail(r, "Trading is disabled for this symbol at this broker.")
    if spec.trade_mode == 3:
        return _fail(r, "This symbol is close-only at this broker.")
    if spec.trade_mode == 1 and d == "sell":
        return _fail(r, "This symbol allows BUY (long) only.")
    if spec.trade_mode == 2 and d == "buy":
        return _fail(r, "This symbol allows SELL (short) only.")
    if not _num(volume) or volume <= 0:
        return _fail(r, "Volume must be a finite number above 0.")
    r.volume = float(volume)
    vmin, vmax, step = spec.volume_min, spec.volume_max, spec.volume_step
    if all(_num(v) and v > 0 for v in (vmin, vmax, step)):
        if volume < vmin - 1e-12 or volume > vmax + 1e-12:
            return _fail(r, f"Volume {volume:g} is outside the broker's range {vmin:g} to {vmax:g}.")
        steps = (_d(volume) - _d(vmin)) / _d(step)
        if abs(steps - steps.to_integral_value()) > Decimal("1e-9"):
            return _fail(r, f"Volume {volume:g} must be a multiple of {step:g} starting at {vmin:g}.")
    r._per_tick = per_tick     # money per ONE tick for ONE lot (internal; the public loss_per_lot is set once a stop exists)
    return None


def _on_grid(price: float, tick: float) -> bool:
    q = _d(price) / _d(tick)
    return abs(q - q.to_integral_value()) <= Decimal("1e-7")


def _snap(price: float, tick: float, rounding) -> float:
    return float((_d(price) / _d(tick)).to_integral_value(rounding=rounding) * _d(tick))


def _min_distance(spec_points: int | None, point: float | None) -> float:
    return (spec_points or 0) * point if _num(point) and point else 0.0


def sl_from_risk(direction, entry, volume, risk_amount, spec: SymbolSpec | None, *, currency: str = "", digits: int | None = None,
                 stops_level: int | None = None, point: float | None = None) -> SlResult:
    r = SlResult(mode="risk", currency=currency, requested_risk=float(risk_amount) if _num(risk_amount) else None)
    bad = _check_common(r, direction, entry, volume, spec)
    if bad:
        return bad
    if not _num(risk_amount) or risk_amount <= 0:
        return _fail(r, "Risk amount must be a finite number above 0.")
    tick = spec.tick_size
    per_tick_for_volume = r._per_tick * r.volume               # money lost per tick of adverse move at this volume
    exact_ticks = _d(risk_amount) / _d(per_tick_for_volume)    # (fractional) number of ticks the stop may be from entry
    whole = int(exact_ticks.to_integral_value(rounding=ROUND_FLOOR))
    one_tick = per_tick_for_volume
    if whole < 1:
        return _fail(r, f"The requested risk {risk_amount:g} {currency} is smaller than the loss from a single tick "
                        f"({one_tick:.4f} {currency} at {volume:g} lots), so no valid stop-loss exists. "
                        "Raise the risk or lower the volume.", stop_ticks=0)
    # Round the SL toward the entry on the tick grid: BUY stop goes UP to the grid, SELL stop goes DOWN. Never further out.
    exact_sl = _d(entry) - exact_ticks * _d(tick) if r.direction == "buy" else _d(entry) + exact_ticks * _d(tick)
    sl = _snap(float(exact_sl), tick, ROUND_CEILING if r.direction == "buy" else ROUND_FLOOR)
    if digits is not None:
        sl = round(sl, digits)
    if sl <= 0:
        return _fail(r, "That risk is so large that the stop-loss would be at or below zero. Lower the risk or raise the volume.")
    if (r.direction == "buy" and sl >= entry) or (r.direction == "sell" and sl <= entry):
        return _fail(r, "No stop-loss on the tick grid fits that risk (the stop would not be on the protective side of the entry).")
    distance = abs(entry - sl)
    min_dist = _min_distance(stops_level, point)
    if min_dist and distance < min_dist - (point or 0) / 2:
        need = min_dist / tick * one_tick
        return _fail(r, f"The requested risk is too small for this broker: the stop must be at least {stops_level} points "
                        f"({min_dist:g}) from price, which already risks about {need:.2f} {currency}. Nothing was increased for you.")
    ticks = int(round(distance / tick))
    actual = loss_per_lot_at_stop(spec, distance) * r.volume   # the SAME function the Risk Engine uses
    r.valid, r.stop_loss, r.stop_distance, r.stop_ticks = True, sl, distance, ticks
    r.loss_per_lot = loss_per_lot_at_stop(spec, distance)
    r.risk_amount = actual
    r.difference = max(0.0, float(risk_amount) - actual)
    r.message = "Stop-loss calculated."
    if r.difference > 1e-9:
        r.notes.append(f"Prices move in steps of {tick:g}, so the closest stop that does not exceed your risk loses {actual:.2f} "
                       f"{currency} ({r.difference:.2f} less than the {float(risk_amount):.2f} requested).")
    else:
        r.notes.append("The requested risk fits the tick grid exactly.")
    return r


def risk_from_sl(direction, entry, volume, stop_loss, spec: SymbolSpec | None, *, currency: str = "", digits: int | None = None,
                 stops_level: int | None = None, point: float | None = None) -> SlResult:
    r = SlResult(mode="price", currency=currency)
    bad = _check_common(r, direction, entry, volume, spec)
    if bad:
        return bad
    if not _num(stop_loss) or stop_loss <= 0:
        return _fail(r, "Stop-loss price is missing or invalid.")
    sl = float(stop_loss)
    r.stop_loss = sl
    if (r.direction == "buy" and sl >= entry) or (r.direction == "sell" and sl <= entry):
        return _fail(r, ("Invalid BUY: the stop-loss must be BELOW the entry price." if r.direction == "buy"
                         else "Invalid SELL: the stop-loss must be ABOVE the entry price."))
    tick = spec.tick_size
    if not _on_grid(sl, tick):
        # Never silently change what the user typed: say what is valid. The nearest valid stop that is NOT further out is offered.
        near = _snap(sl, tick, ROUND_CEILING if r.direction == "buy" else ROUND_FLOOR)
        return _fail(r, f"The stop-loss {sl:g} is not a valid price for this symbol: prices move in steps of {tick:g}.",
                     nearest_valid_stop=near if digits is None else round(near, digits))
    if digits is not None and round(sl, digits) != sl:
        return _fail(r, f"The stop-loss {sl:g} has more than {digits} decimal places for this symbol.")
    distance = abs(entry - sl)
    min_dist = _min_distance(stops_level, point)
    if min_dist and distance < min_dist - (point or 0) / 2:
        return _fail(r, f"The stop-loss is closer than this broker allows: at least {stops_level} points ({min_dist:g}) from price.")
    loss_one_lot = loss_per_lot_at_stop(spec, distance)
    r.valid, r.stop_distance, r.stop_ticks = True, distance, int(round(distance / tick))
    r.loss_per_lot = loss_one_lot
    r.risk_amount = loss_one_lot * r.volume
    r.message = "Risk calculated."
    r.notes.append(f"If the stop-loss is hit, about {r.risk_amount:.2f} {currency} is lost at {r.volume:g} lots "
                   "(before slippage, spread, swap and commission).")
    return r


def judge_limit(result: SlResult, equity: float | None, max_risk_percent: float | None) -> dict:
    """Is this risk inside the per-trade limit? Informational: the Risk Engine enforces it at submission."""
    out = {"checked": False, "ok": None, "equity": equity, "risk_percent": None, "max_risk_percent": max_risk_percent,
           "max_risk_amount": None, "message": "Limit not checked: account equity or the configured limit is unavailable."}
    if not result.valid or result.risk_amount is None or not _num(equity) or equity <= 0 or not _num(max_risk_percent):
        return out
    cap = equity * max_risk_percent / 100.0
    pct = result.risk_amount / equity * 100.0
    ok = result.risk_amount <= cap + 1e-9
    out.update(checked=True, ok=ok, risk_percent=pct, max_risk_amount=cap,
               message=(f"Within your per-trade limit ({pct:.2f}% of equity, limit {max_risk_percent:g}% = {cap:.2f} {result.currency})."
                        if ok else f"Above your per-trade limit: {pct:.2f}% of equity against a limit of {max_risk_percent:g}% "
                                   f"({cap:.2f} {result.currency}). The Risk Engine would reject this trade."))
    return out
