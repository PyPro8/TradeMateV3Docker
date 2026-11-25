"""Jigsaw 5.1 - risk-based position sizing.

    Account equity x risk %  ->  maximum monetary risk
    entry -> stop-loss distance, priced with the broker's own tick size / tick value  ->  loss per 1 lot
    volume = maximum risk / loss per lot, rounded DOWN to the broker's volume step

Rounding is always down, so the estimated loss never exceeds the allowed risk. A size that would have to be
rounded UP to reach the broker's minimum is rejected, not silently enlarged.

Tick value is what MT5 reports in ACCOUNT currency for 1 lot, so no FX table and no hard-coded instrument
values are needed. It is read from the symbol specification supplied by the caller.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import ROUND_FLOOR, Decimal

VALID = "VALID"
REJECTED = "REJECTED"


@dataclass(frozen=True)
class SymbolSpec:
    """The parts of an MT5 symbol specification the calculation needs."""

    name: str
    tick_size: float | None
    tick_value: float | None = None  # account currency, 1 lot
    tick_value_loss: float | None = None  # preferred for a stop-loss: MT5 reports it separately from the gain value
    volume_min: float | None = None
    volume_max: float | None = None
    volume_step: float | None = None
    trade_mode: int | None = None  # MT5: 0 disabled, 1 long only, 2 short only, 3 close only, 4 full


@dataclass(frozen=True)
class TradeRequest:
    symbol: str
    direction: str  # "buy" / "sell"
    entry_price: float | None
    stop_loss: float | None
    risk_percent: float | None


@dataclass(frozen=True)
class RiskLimits:
    max_risk_percent: float = 2.0
    max_volume: float | None = None  # TradeMate's own per-order guard rail (trading.max_volume); None = no extra cap


@dataclass(frozen=True)
class PositionSizeResult:
    symbol: str
    direction: str
    entry_price: float | None
    stop_loss: float | None
    stop_distance: float | None
    risk_percent: float | None
    account_equity: float | None
    risk_amount: float | None  # maximum monetary risk
    loss_per_lot: float | None  # money lost at the stop for 1 lot
    calculated_volume: float | None  # exact, before the broker's volume rules
    normalized_volume: float | None  # rounded down to the broker's step; None if rejected
    estimated_loss: float | None  # at the stop-loss, for normalized_volume
    validation_status: str  # VALID / REJECTED
    rejection_reason: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _dec(x: float) -> Decimal:
    return Decimal(str(x))


def _round_down_to_step(volume: float, vmin: float, step: float) -> float:
    """Largest valid volume (min + k*step) that is <= volume. Exact decimal maths, no float drift."""
    steps = ((_dec(volume) - _dec(vmin)) / _dec(step)).to_integral_value(rounding=ROUND_FLOOR)
    return float(_dec(vmin) + steps * _dec(step))


def calculate_position_size(equity: float | None, trade: TradeRequest, spec: SymbolSpec | None,
                            limits: RiskLimits | None = None) -> PositionSizeResult:
    """Never raises for bad input: a rejected result carries the reason."""
    limits = limits or RiskLimits()
    direction = (trade.direction or "").strip().lower() if isinstance(trade.direction, str) else ""
    entry, sl, pct = trade.entry_price, trade.stop_loss, trade.risk_percent
    ctx = dict(symbol=trade.symbol, direction=direction, entry_price=entry if _is_number(entry) else None,
               stop_loss=sl if _is_number(sl) else None, risk_percent=pct if _is_number(pct) else None,
               account_equity=equity if _is_number(equity) else None)
    parts = dict(stop_distance=None, risk_amount=None, loss_per_lot=None, calculated_volume=None,
                 normalized_volume=None, estimated_loss=None)

    def reject(reason: str, **extra) -> PositionSizeResult:
        return PositionSizeResult(**ctx, **{**parts, **extra}, validation_status=REJECTED, rejection_reason=reason)

    # ---- risk ----
    if not _is_number(pct):
        return reject("Risk percentage is missing or not a number.")
    if pct <= 0:
        return reject("Risk percentage must be greater than 0.")
    if pct > limits.max_risk_percent:
        return reject(f"Risk {pct:g}% is above the configured maximum of {limits.max_risk_percent:g}%.")
    if not _is_number(equity) or equity <= 0:
        return reject("Account equity is not available (or is not positive).")
    risk_amount = equity * pct / 100.0
    parts["risk_amount"] = risk_amount

    # ---- trade ----
    if direction not in ("buy", "sell"):
        return reject("Direction must be BUY or SELL.")
    if not _is_number(entry) or entry <= 0:
        return reject("Entry price is missing or invalid.")
    if not _is_number(sl) or sl <= 0:
        return reject("Stop-loss is missing or invalid.")
    if entry == sl:
        return reject("Stop-loss cannot equal the entry price.")
    if direction == "buy" and sl >= entry:
        return reject("Invalid BUY: the stop-loss must be BELOW the entry price.")
    if direction == "sell" and sl <= entry:
        return reject("Invalid SELL: the stop-loss must be ABOVE the entry price.")
    distance = abs(entry - sl)
    parts["stop_distance"] = distance

    # ---- symbol specification ----
    if spec is None:
        return reject(f"No MT5 specification available for '{trade.symbol}'.")
    if not (_is_number(spec.tick_size) and spec.tick_size > 0):
        return reject(f"MT5 reported no valid tick size for '{trade.symbol}'.")
    tick_value = spec.tick_value_loss if _is_number(spec.tick_value_loss) and spec.tick_value_loss > 0 else spec.tick_value
    if not (_is_number(tick_value) and tick_value > 0):
        return reject(f"MT5 reported no valid tick value for '{trade.symbol}'.")
    vmin, vmax, step = spec.volume_min, spec.volume_max, spec.volume_step
    if not all(_is_number(v) and v > 0 for v in (vmin, vmax, step)) or vmin > vmax:
        return reject(f"MT5 reported no valid volume limits for '{trade.symbol}'.")
    if spec.trade_mode == 0:
        return reject(f"Trading is disabled for '{trade.symbol}' at this broker.")
    if spec.trade_mode == 3:
        return reject(f"'{trade.symbol}' is close-only at this broker.")
    if spec.trade_mode == 1 and direction == "sell":
        return reject(f"'{trade.symbol}' allows BUY (long) only.")
    if spec.trade_mode == 2 and direction == "buy":
        return reject(f"'{trade.symbol}' allows SELL (short) only.")

    # ---- calculation ----
    loss_per_lot = (distance / spec.tick_size) * tick_value
    parts["loss_per_lot"] = loss_per_lot
    raw = round(risk_amount / loss_per_lot, 10)  # strips float noise so an exact fit (e.g. 0.0099999999) is not misjudged
    parts["calculated_volume"] = raw

    if raw < vmin:
        return reject(f"Allowed size {raw:.4f} lots is below the broker minimum of {vmin:g}. "
                      f"Minimum size would risk {vmin * loss_per_lot:.2f}, more than the {risk_amount:.2f} allowed.")
    cap = vmax if limits.max_volume is None else min(vmax, limits.max_volume)
    if raw > cap:
        what = "broker maximum" if cap == vmax else "TradeMate's per-order maximum (trading.max_volume)"
        return reject(f"Allowed size {raw:.4f} lots is above the {what} of {cap:g}. Widen the stop-loss or lower the risk.")
    volume = _round_down_to_step(raw, vmin, step)
    if volume < vmin or volume > vmax or volume <= 0:  # defensive: cannot be represented on the broker's grid
        return reject("The volume cannot be represented with the broker's volume step.")
    parts["normalized_volume"] = volume
    parts["estimated_loss"] = volume * loss_per_lot
    return PositionSizeResult(**ctx, **parts, validation_status=VALID, rejection_reason=None)
