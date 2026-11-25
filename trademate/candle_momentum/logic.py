"""Pure Candle Momentum rules. No state, no broker, no clock reads (time is always passed in), no threads.

Every distance is converted by the central instrument engine (`trademate.instruments.InstrumentSpec`): this module never contains a pip size, a
number of decimals, or an assumption about what a pip is.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from decimal import Decimal

from trademate.candle_momentum.models import BEAR, BULL, NEUTRAL, CMConfig, Quote


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _d(x) -> Decimal:
    return Decimal(str(x))


# ---------------------------------------------------------------------------------------------------------------------------- identity
def make_candle_id(symbol: str, timeframe: str, open_time, open_price) -> str:
    """The candle's unique identifier: symbol + timeframe + the candle's opening timestamp + its opening price. It deliberately does NOT contain a
    direction: a candle that turns from bullish to bearish is still the same candle and may never be entered twice."""
    price = format(_d(open_price).normalize(), "f")
    raw = "|".join(("candle_momentum", symbol, timeframe, str(int(open_time)), price))
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


# ---------------------------------------------------------------------------------------------------------------------------- direction / trigger
def candle_direction(price: float, open_price: float, tick_size) -> str:
    """bullish: price above the open. bearish: below. neutral: equal within the instrument's price precision (the tick grid)."""
    if not (_num(price) and _num(open_price)):
        return NEUTRAL
    if _num(tick_size) and tick_size > 0:
        t = _d(tick_size)
        a, b = (_d(price) / t).quantize(Decimal(1)), (_d(open_price) / t).quantize(Decimal(1))
        if a == b:
            return NEUTRAL
        return BULL if a > b else BEAR
    diff = price - open_price
    return NEUTRAL if abs(diff) < 1e-12 else (BULL if diff > 0 else BEAR)


def trigger_price(open_price: float, direction: str, trigger_units: float, spec) -> float:
    """BULL: open + distance. BEAR: open - distance. The distance is converted by the instrument engine and the result snapped to the tick grid."""
    dist = _d(spec.units_to_price(trigger_units))
    raw = _d(open_price) + dist if direction == BULL else _d(open_price) - dist
    if raw <= 0:
        raise ValueError(f"{trigger_units:g} units is {dist:g} in price, which puts the trigger at {raw:g}: not a valid price, so the pip conversion for this instrument must be wrong")
    return spec.snap(float(raw), "nearest")


def trigger_crossed(direction: str, price: float, trigger: float) -> bool:
    """The price is AT or BEYOND the trigger in the candle's direction (an exact touch counts as a crossing)."""
    if direction == BULL:
        return price >= trigger - 1e-12
    if direction == BEAR:
        return price <= trigger + 1e-12
    return False


def plan_target_price(trigger: float, direction: str, target_units: float, spec) -> float:
    """The level a full target measured from the TRIGGER would have reached. It is the yardstick for 'is the opportunity still there': it is NOT the
    take-profit (the take-profit is always measured from the actual fill)."""
    d = _d(spec.units_to_price(target_units))
    raw = _d(trigger) + d if direction == BULL else _d(trigger) - d
    if raw <= 0:
        raise ValueError(f"a {target_units:g}-unit target from {trigger:g} would be {raw:g}: not a valid price")
    return spec.snap(float(raw), "nearest")


def protective_stop_price(open_price: float, direction: str, sl_buffer_units, spec) -> float | None:
    """The existing candle-momentum protective stop: `sl_buffer_units` beyond the candle OPEN on the loss side (never closer than computed). None when it
    cannot be established: the caller must then refuse the entry instead of sending an unprotected order."""
    if not _num(sl_buffer_units) or sl_buffer_units <= 0:
        return None
    try:
        d = _d(spec.units_to_price(sl_buffer_units))
    except ValueError:
        return None
    if direction == BULL:
        raw = _d(open_price) - d
        return spec.snap(float(raw), "down") if raw > 0 else None
    return spec.snap(float(_d(open_price) + d), "up")


def executable_price(direction: str, quote: Quote) -> float:
    return quote.ask if direction == BULL else quote.bid


def quote_age(quote: Quote, now: float) -> float | None:
    """How old the quote's SOURCE data is right now: its age when TradeMate read it plus the time since. None = unknown (never treated as fresh)."""
    if quote.age_seconds is None or not _num(quote.age_seconds):
        return None
    return max(0.0, float(quote.age_seconds)) + max(0.0, now - quote.read_at)


def slippage_units(side: str, reference: float, fill: float, spec) -> float:
    """Adverse execution slippage in strategy units: BUY fill above the reference / SELL fill below it is POSITIVE (worse). Negative = price improvement."""
    adverse = (_d(fill) - _d(reference)) if side == "buy" else (_d(reference) - _d(fill))
    return float(_d(spec.price_to_units(float(adverse))))


# ---------------------------------------------------------------------------------------------------------------------------- entry validation
ENTER, WAIT_QUOTE, INVALIDATED, MISSED, REJECTED = "ENTER", "WAIT_QUOTE", "INVALIDATED", "MISSED", "REJECTED"


@dataclass(frozen=True)
class EntryVerdict:
    kind: str                       # ENTER | WAIT_QUOTE | INVALIDATED | MISSED | REJECTED
    code: str                       # machine-readable cause
    reason: str                     # the exact explanation shown to the user and logged
    exec_price: float | None = None
    beyond_trigger_units: float | None = None
    consumed_fraction: float | None = None
    remaining_units: float | None = None          # distance from the executable quote to the plan target (trigger + a full target)
    plan_target_price: float | None = None
    sl_price: float | None = None
    tp_preview: float | None = None               # the take-profit if the order filled at the executable quote right now
    spread_units: float | None = None
    quote_age: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def validate_entry(direction: str, trigger: float, open_price: float, quote: Quote, cfg: CMConfig, spec, now: float) -> EntryVerdict:
    """Judge the entry at the CURRENT executable quote. Called immediately when the trigger is crossed and again on every fresh quote until the
    signal is committed to an order or ends. It never weakens a broker / Risk Engine protection: it only decides whether the strategy's own entry is
    still worth proposing."""
    side = "buy" if direction == BULL else "sell"
    if spec is None or not getattr(spec, "unit", None) or getattr(spec, "unit_problems", ()):
        why = "; ".join(getattr(spec, "unit_problems", ()) or ()) or "the instrument's strategy unit is not defined"
        return EntryVerdict(REJECTED, "conversion_unavailable", f"UNVERIFIED instrument conversion ({why}). Automatic execution is blocked; nothing was guessed.")
    blocking = spec.blocking_reasons(now)
    if blocking:
        return EntryVerdict(REJECTED, "unverified_instrument", "UNVERIFIED instrument data blocks automatic execution: " + " ".join(blocking))
    if not quote.valid:
        return EntryVerdict(WAIT_QUOTE, "invalid_quote", "The broker quote is missing or inconsistent (bid/ask not both positive with ask >= bid).")
    age = quote_age(quote, now)
    if age is None or age > cfg.quote_max_age_seconds:
        return EntryVerdict(WAIT_QUOTE, "stale_quote", f"The quote is {'of unknown age' if age is None else f'{age:.1f}s old'}; the limit is {cfg.quote_max_age_seconds:g}s. "
                                                      "No entry is judged on a stale price.", quote_age=age)
    sl = protective_stop_price(open_price, direction, cfg.sl_buffer_units, spec)
    if sl is None:
        return EntryVerdict(REJECTED, "no_protective_stop", "A mandatory protective stop could not be established (the configured stop distance is missing or "
                                                           "cannot be converted). No unprotected order is ever sent.", quote_age=age)
    if not trigger_crossed(direction, quote.bid, trigger):
        return EntryVerdict(INVALIDATED, "price_inside_trigger", f"Price {quote.bid:g} is back {'below' if direction == BULL else 'above'} the trigger {trigger:g}: "
                                                                  "the momentum thesis no longer holds, so the entry is cancelled.", quote_age=age)
    ex = executable_price(direction, quote)
    if (direction == BULL and not sl < ex) or (direction == BEAR and not ex < sl):
        return EntryVerdict(REJECTED, "geometry", f"The protective stop {sl:g} is on the wrong side of the executable price {ex:g}.", exec_price=ex, sl_price=sl, quote_age=age)
    beyond = (_d(ex) - _d(trigger)) if direction == BULL else (_d(trigger) - _d(ex))
    beyond_units = float(_d(spec.price_to_units(float(beyond))))
    try:
        plan = plan_target_price(trigger, direction, cfg.target_units, spec)
    except ValueError as exc:
        return EntryVerdict(REJECTED, "target_unavailable", f"The target could not be calculated: {exc}.", exec_price=ex, sl_price=sl, quote_age=age)
    remaining = float((_d(plan) - _d(ex)) if direction == BULL else (_d(ex) - _d(plan)))
    remaining_units = float(_d(spec.price_to_units(remaining)))
    consumed = beyond_units / cfg.target_units
    spread_units = float(_d(spec.price_to_units(float(_d(quote.ask) - _d(quote.bid)))))
    common = dict(exec_price=ex, beyond_trigger_units=beyond_units, consumed_fraction=consumed, remaining_units=remaining_units, plan_target_price=plan,
                  sl_price=sl, spread_units=spread_units, quote_age=age)
    # No percentage-based or trigger-relative "too late" rejection exists here (removed on purpose): however far price has moved beyond the trigger, the
    # entry is still proposed at the CURRENT executable quote. The target is measured from the actual fill, so "how much of the trigger-based move is
    # left" is information only. Quote freshness, broker checks, the Risk Engine and the execution-cost policy still apply downstream.
    try:
        tp = spec.target_price(side, ex, cfg.target_units)
    except ValueError as exc:
        return EntryVerdict(REJECTED, "target_unavailable", f"The target could not be calculated: {exc}", **common)
    return EntryVerdict(ENTER, "ok", f"Valid entry: executable {ex:g} is {beyond_units:.2f} units beyond the trigger {trigger:g} ({consumed * 100:.0f}% of the target distance beyond the trigger, "
                                      f"{remaining_units:.2f} units of the trigger-based move remain, shown for information only); stop {sl:g}, target {tp:g} measured from the executable price.",
                        tp_preview=tp, **common)
