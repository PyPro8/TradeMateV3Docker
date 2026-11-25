"""Pre-flight checks for orders and SL/TP changes.

Pure functions (no I/O) so they are easy to test. They catch the mistakes the
broker would reject anyway, with a clear message, BEFORE anything is sent.
The broker still has the final say: its answer is what TradeMate reports.
"""

from __future__ import annotations

import math

from trademate.broker.base import SymbolInfo
from trademate.errors import OrderRejectedError


def check_volume(info: SymbolInfo, volume: float, max_volume: float) -> float:
    if not isinstance(volume, (int, float)) or isinstance(volume, bool) or not math.isfinite(volume) or volume <= 0:
        raise OrderRejectedError("Lot size must be a number greater than 0.")
    vmin, vmax, step = info.volume_min, info.volume_max, info.volume_step
    if vmin is not None and volume < vmin - 1e-9:
        raise OrderRejectedError(f"Lot size {volume} is below the broker minimum of {vmin} for {info.name}.")
    if vmax is not None and volume > vmax + 1e-9:
        raise OrderRejectedError(f"Lot size {volume} is above the broker maximum of {vmax} for {info.name}.")
    if volume > max_volume + 1e-9:
        raise OrderRejectedError(f"Lot size {volume} is above TradeMate's safety limit of {max_volume} "
                                 "([trading] max_volume in config/settings.toml).")
    if step and vmin is not None:
        steps = (volume - vmin) / step
        if abs(steps - round(steps)) > 1e-6:
            raise OrderRejectedError(f"Lot size {volume} must be a multiple of {step} (starting at {vmin}).")
    return round(volume, 8)


def _round_price(info: SymbolInfo, price: float) -> float:
    return round(price, info.digits) if info.digits is not None else price


def _check_number(label: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise OrderRejectedError(f"{label} must be a price greater than 0.")
    return float(value)


def check_stops(info: SymbolInfo, side: str, reference: float | None,
                stop_loss: float | None, take_profit: float | None) -> tuple[float | None, float | None]:
    """Validate SL/TP against the side and the broker's minimum stop distance.

    reference = the price the position would close at (bid for a buy, ask for a sell).
    Returns the prices rounded to the symbol's digits.
    """
    if side not in ("buy", "sell"):
        raise OrderRejectedError("Side must be buy or sell.")
    sl = _round_price(info, _check_number("Stop loss", stop_loss)) if stop_loss is not None else None
    tp = _round_price(info, _check_number("Take profit", take_profit)) if take_profit is not None else None
    if reference is None or info.point is None:
        return sl, tp  # cannot judge distances without a price; the broker will decide

    min_dist = (info.stops_level or 0) * info.point
    eps = (info.point or 0) / 2
    if side == "buy":
        if sl is not None and sl > reference - min_dist + eps:
            raise OrderRejectedError(_too_close("Stop loss", "below", reference, min_dist, info))
        if tp is not None and tp < reference + min_dist - eps:
            raise OrderRejectedError(_too_close("Take profit", "above", reference, min_dist, info))
    else:
        if sl is not None and sl < reference + min_dist - eps:
            raise OrderRejectedError(_too_close("Stop loss", "above", reference, min_dist, info))
        if tp is not None and tp > reference - min_dist + eps:
            raise OrderRejectedError(_too_close("Take profit", "below", reference, min_dist, info))
    return sl, tp


def _too_close(label: str, where: str, reference: float, min_dist: float, info: SymbolInfo) -> str:
    digits = info.digits if info.digits is not None else 5
    need = f" and at least {info.stops_level} points away" if info.stops_level else ""
    return (f"{label} must be {where} the current price ({reference:.{digits}f}){need} "
            f"for a {info.name} position on this side.")
