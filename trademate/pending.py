"""Pending orders (Phase 7B): server-side validation of the order type, entry price, stops, volume and expiry.

Pure functions. Nothing here talks to MT5. A pending order waits for price to reach `price` and then becomes a position.

SUPPORTED: buy_limit, sell_limit, buy_stop, sell_stop. NOT supported: buy_stop_limit / sell_stop_limit. The bridge can place
them in principle, but TradeMate cannot verify their two-price behaviour end to end, so they are not offered as operational.

PRICE RULES (the broker's minimum distance = SYMBOL_TRADE_STOPS_LEVEL x point, from the symbol's own data):
    Buy Limit  : price BELOW the current Ask by at least the minimum distance
    Sell Limit : price ABOVE the current Bid by at least the minimum distance
    Buy Stop   : price ABOVE the current Ask by at least the minimum distance
    Sell Stop  : price BELOW the current Bid by at least the minimum distance
The user's price is NEVER adjusted: a price that breaks a rule is refused with the reason and the allowed side, so the user corrects
it. The entry must lie on the symbol's tick grid. SL/TP are checked against the PENDING price (not the market) with the same
minimum-distance rule as market orders. Expiry (optional) must be a whole epoch second at least one minute ahead and within a year.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from trademate.broker.validation import check_stops, check_volume
from trademate.errors import OrderRejectedError

PENDING_TYPES = {"buy_limit": "buy", "sell_limit": "sell", "buy_stop": "buy", "sell_stop": "sell"}
UNSUPPORTED_TYPES = ("buy_stop_limit", "sell_stop_limit")
LABELS = {"buy_limit": "Buy Limit", "sell_limit": "Sell Limit", "buy_stop": "Buy Stop", "sell_stop": "Sell Stop"}
MIN_EXPIRY_SECONDS, MAX_EXPIRY_SECONDS = 60, 366 * 86400


@dataclass(frozen=True)
class CleanPending:
    symbol: str
    order_type: str
    side: str
    volume: float
    price: float
    sl: float | None
    tp: float | None
    expiration: int | None


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def check_type(order_type) -> str:
    """The order's side, or OrderRejectedError."""
    if order_type in PENDING_TYPES:
        return PENDING_TYPES[order_type]
    if order_type in UNSUPPORTED_TYPES:
        raise OrderRejectedError(f"{str(order_type).replace('_', ' ').title()} orders are not supported by this TradeMate bridge. "
                                 "Use Buy/Sell Limit or Buy/Sell Stop.")
    raise OrderRejectedError("Order type must be one of: " + ", ".join(PENDING_TYPES) + ".")


def check_price(info, order_type: str, price) -> float:
    """The entry price against the CURRENT quote and the broker's minimum distance. Never changes the price."""
    side = check_type(order_type)
    if not _num(price) or price <= 0:
        raise OrderRejectedError("Entry price must be a finite number above 0.")
    bid, ask, point = info.bid, info.ask, info.point
    if not (_num(bid) and _num(ask) and bid > 0 and ask > 0 and ask >= bid):
        raise OrderRejectedError(f"No valid live quote for {info.name}, so the entry price cannot be checked. Nothing was sent.")
    if not (_num(point) and point > 0):
        raise OrderRejectedError(f"MT5 reported no point size for {info.name}, so the entry price cannot be checked. Nothing was sent.")
    tick = info.tick_size if _num(info.tick_size) and info.tick_size > 0 else point
    q = Decimal(str(price)) / Decimal(str(tick))
    if abs(q - q.to_integral_value()) > Decimal("1e-7"):
        near = float(q.to_integral_value() * Decimal(str(tick)))
        raise OrderRejectedError(f"The entry price {price:g} is not a valid price for {info.name}: prices move in steps of {tick:g} "
                                 f"(nearest valid: {near:g}). Your price was not changed.")
    min_dist = (info.stops_level or 0) * point
    eps = point / 2
    d = info.digits if isinstance(info.digits, int) else 5
    need = f" and at least {info.stops_level} points ({min_dist:.{d}f}) away" if min_dist else ""
    label = LABELS[order_type]
    if order_type == "buy_limit" and not price < ask - min_dist + eps:
        raise OrderRejectedError(f"A {label} must be placed BELOW the current Ask ({ask:.{d}f}){need}. Choose a lower price.")
    if order_type == "sell_limit" and not price > bid + min_dist - eps:
        raise OrderRejectedError(f"A {label} must be placed ABOVE the current Bid ({bid:.{d}f}){need}. Choose a higher price.")
    if order_type == "buy_stop" and not price > ask + min_dist - eps:
        raise OrderRejectedError(f"A {label} must be placed ABOVE the current Ask ({ask:.{d}f}){need}. Choose a higher price.")
    if order_type == "sell_stop" and not price < bid - min_dist + eps:
        raise OrderRejectedError(f"A {label} must be placed BELOW the current Bid ({bid:.{d}f}){need}. Choose a lower price.")
    return float(price)


def check_expiration(expiration, now: float) -> int | None:
    if expiration is None:
        return None
    if not _num(expiration) or float(expiration) != int(expiration):
        raise OrderRejectedError("Expiration must be a whole epoch second, or empty for good-till-cancelled.")
    if not now + MIN_EXPIRY_SECONDS <= expiration <= now + MAX_EXPIRY_SECONDS:
        raise OrderRejectedError("Expiration must be at least one minute from now and within one year.")
    return int(expiration)


def validate_pending(info, order_type, volume, price, sl, tp, expiration, max_volume=None, now: float = 0.0) -> CleanPending:
    """Everything the server checks BEFORE the Risk Engine and before anything is sent. Raises OrderRejectedError."""
    side = check_type(order_type)
    if info.trade_mode is not None and info.trade_mode not in (4, 1 if side == "buy" else 2):
        raise OrderRejectedError(f"{info.name} does not allow {side} orders right now (market closed or restricted).")
    entry = check_price(info, order_type, price)
    cap = max_volume if _num(max_volume) and max_volume > 0 else (info.volume_max if _num(info.volume_max) else 1e9)
    vol = check_volume(info, volume, cap)
    sl, tp = check_stops(info, side, entry, sl, tp)          # against the PENDING price, with the broker's minimum distance
    return CleanPending(info.name, order_type, side, vol, entry, sl, tp, check_expiration(expiration, now))
