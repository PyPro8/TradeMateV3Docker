"""Market Structure Engine (Phase 7A).

Pure analysis: OHLC candles + a StructureConfig in, a StructureResult out. No Flask, no broker, no chart code,
no orders, no entries / stops / targets / sizes. The full rules are documented in docs/MARKET_STRUCTURE.md; the
short version is below.

SWING POINTS (pivots)
    A candle i is a pivot HIGH when its high is STRICTLY above the highs of the `swing_left` candles before it and
    at least equal to the highs of the `swing_right` candles after it. A pivot LOW is the mirror image on lows.
    Tie-break for equal highs / lows: the EARLIEST candle of an equal run is the pivot (later equal ones fail the
    strict left-side test). The same candle may be both a pivot high and a pivot low (an outside candle): both are
    kept, they are independent.

CONFIRMATION (no lookahead)
    A pivot at candle i is only KNOWN once candle i + swing_right exists. Every swing therefore carries two
    timestamps: `pivot_time` (where it happened) and `confirmed_time` (when it became knowable). All later logic
    (classification, breaks) only uses a swing from its confirmed candle onwards, so running the engine on a
    prefix of the data gives exactly the same results (for everything confirmed inside that prefix) as running
    it on the full data.

CLASSIFICATION
    A confirmed pivot high is HH / LH / EQH against the previous confirmed pivot HIGH; a pivot low is HL / LL / EQL
    against the previous confirmed pivot LOW. The first pivot of each type has no predecessor: its
    classification is None (insufficient evidence, nothing is invented).

BREAKS: BOS and CHoCH
    With no direction yet, the first break of the latest confirmed swing high/low bootstraps direction as BOS.
    Thereafter bullish BOS requires a confirmed HH/EQH high and bearish BOS requires a confirmed LL/EQL low.
    A countertrend CHoCH instead requires a
    break of the latest confirmed HL (protected low) against bullish structure or LH (protected high) against bearish
    structure. Later LL/LH pivots do not replace a bullish protected low; later HH/HL pivots do not replace a bearish
    protected high. If no qualifying protected swing exists, an opposing break is not promoted to CHoCH.
    A bullish break needs close > level (break_mode="close", default) or high > level ("wick"); a bearish break
    needs close < level or low < level.
    Trend context: "bullish" after a bullish break, "bearish" after a bearish break, None before any break.
    A break in the direction of current structure is a BOS; a qualifying protected-level break against it is a
    CHoCH and changes direction. The first qualifying break from undefined direction is a bootstrap BOS with
    trend_before=None.
    If one candle breaks both levels (possible in wick mode, or when levels cross), both events are emitted with
    `same_candle_conflict=True` and the trend becomes None, because the order inside the candle is unknowable.

DATA HANDLING
    Invalid candles (non-numeric / non-finite values, high < low, open or close outside [low, high]) are excluded
    and listed in diagnostics. Duplicate timestamps keep the LAST occurrence. Out-of-order input is sorted by time.
    Too little data (fewer than swing_left + swing_right + 1 usable candles) returns an empty result flagged
    insufficient_data=True. Nothing is fabricated.

FORMING CANDLE
    If `last_candle_closed` is False the last candle is still forming: anything that depends on it (a pivot
    confirmed by it, a break on it) is reported with confirmed=False and may change on the next call.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

BREAK_MODES = ("close", "wick")
MAX_SWING_SIDE = 50

HH, LH, HL, LL, EQH, EQL = "HH", "LH", "HL", "LL", "EQH", "EQL"


@dataclass(frozen=True)
class StructureConfig:
    swing_left: int = 3             # candles to the left a pivot must be strictly beyond
    swing_right: int = 3            # candles to the right a pivot must hold; also the confirmation delay
    break_mode: str = "close"       # "close": candle must close beyond the level; "wick": a wick beyond is enough
    equal_tolerance: float = 0.0    # swings within this absolute price distance are "equal" (EQH / EQL)
    last_candle_closed: bool = True  # False when the newest candle is still forming (live data)

    def __post_init__(self) -> None:
        for name in ("swing_left", "swing_right"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SWING_SIDE:
                raise ValueError(f"{name} must be a whole number from 1 to {MAX_SWING_SIDE}.")
        if self.break_mode not in BREAK_MODES:
            raise ValueError(f"break_mode must be one of: {', '.join(BREAK_MODES)}.")
        tol = self.equal_tolerance
        if isinstance(tol, bool) or not isinstance(tol, (int, float)) or not math.isfinite(tol) or tol < 0:
            raise ValueError("equal_tolerance must be a number, 0 or more.")
        if not isinstance(self.last_candle_closed, bool):
            raise ValueError("last_candle_closed must be true or false.")

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def min_candles(self) -> int:
        return self.swing_left + self.swing_right + 1


@dataclass(frozen=True)
class SwingPoint:
    id: str
    kind: str                       # "high" | "low"
    price: float
    pivot_index: int                # index in the CLEANED candle list
    pivot_time: float               # candle where the pivot happened
    confirmed_index: int
    confirmed_time: float           # candle at which the pivot became knowable (pivot_index + swing_right)
    classification: str | None      # HH / LH / EQH (highs), HL / LL / EQL (lows), None = no earlier swing to compare
    confirmed: bool                 # False only while the confirming candle is still forming

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StructureEvent:
    id: str
    type: str                       # "BOS" | "CHOCH"
    direction: str                  # "bullish" | "bearish"
    level: float                    # the structural level that was broken (price of the reference pivot)
    reference_pivot_time: float
    reference_pivot_confirmed_time: float
    candle_index: int
    candle_time: float              # the candle that broke the level
    break_price: float              # the close (close mode) or the high / low (wick mode) that crossed the level
    break_mode: str
    trend_before: str | None
    confirmed: bool                 # False only if the breaking candle is still forming
    confirmed_time: float | None    # == candle_time once confirmed, else None
    same_candle_conflict: bool = False
    trend_after: str | None = None
    reference_role: str = "continuation_swing"  # bootstrap | continuation_swing | protected_swing | origin_swing
    reason: str = ""
    reference_swing_id: str = ""                # SwingPoint.id of the broken swing (audit trail)
    reference_classification: str | None = None  # HH / HL / LH / LL / EQH / EQL / None of the broken swing

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Diagnostics:
    input_count: int = 0
    used_count: int = 0
    invalid: tuple = ()             # ({"index": input position, "reason": text}, ...)
    duplicates_removed: int = 0
    duplicates_conflicting: int = 0  # duplicates whose OHLC differed (the last one was kept)
    out_of_order: bool = False
    insufficient_data: bool = False
    warnings: tuple = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["invalid"] = [dict(x) for x in self.invalid]
        d["warnings"] = list(self.warnings)
        return d


@dataclass(frozen=True)
class StructureResult:
    config: StructureConfig
    swings: tuple = ()
    events: tuple = ()
    trend: str | None = None
    active_high: SwingPoint | None = None   # latest confirmed, still unbroken swing high (a live reference level)
    active_low: SwingPoint | None = None
    protected_high: SwingPoint | None = None   # latest confirmed LH, eligible against bearish structure
    protected_low: SwingPoint | None = None    # latest confirmed HL, eligible against bullish structure
    continuation_high: SwingPoint | None = None  # latest confirmed HH/EQH, eligible for bullish BOS
    continuation_low: SwingPoint | None = None   # latest confirmed LL/EQL, eligible for bearish BOS
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    # `trend` includes a break made by a candle that is still FORMING (it may vanish on the next refresh).
    # `trend_confirmed` is the direction as of the last CLOSED candle. `active_*`, `protected_*` and
    # `continuation_*` contain confirmed references only, even when the newest candle is forming.
    trend_confirmed: str | None = None

    def to_dict(self) -> dict:
        return {
            "config": self.config.to_dict(),
            "swings": [s.to_dict() for s in self.swings],
            "events": [e.to_dict() for e in self.events],
            "trend": self.trend,
            "trend_confirmed": self.trend_confirmed,
            "active_high": self.active_high.to_dict() if self.active_high else None,
            "active_low": self.active_low.to_dict() if self.active_low else None,
            "protected_high": self.protected_high.to_dict() if self.protected_high else None,
            "protected_low": self.protected_low.to_dict() if self.protected_low else None,
            "continuation_high": self.continuation_high.to_dict() if self.continuation_high else None,
            "continuation_low": self.continuation_low.to_dict() if self.continuation_low else None,
            "diagnostics": self.diagnostics.to_dict(),
        }


# --------------------------------------------------------------------------------------------------------------
# Input cleaning
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Bar:
    time: float
    open: float
    high: float
    low: float
    close: float


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _read(item: Any) -> tuple | None:
    """(time, open, high, low, close) from a sequence, a mapping or an object with those attributes."""
    if isinstance(item, dict):
        return tuple(item.get(k) for k in ("time", "open", "high", "low", "close"))
    if isinstance(item, (list, tuple)):
        return tuple(item[:5]) if len(item) >= 5 else None
    try:
        return tuple(getattr(item, k) for k in ("time", "open", "high", "low", "close"))
    except AttributeError:
        return None


def _clean(candles: Iterable[Any]) -> tuple[list[_Bar], Diagnostics]:
    items = list(candles or [])
    invalid: list[dict] = []
    valid: list[_Bar] = []
    for pos, item in enumerate(items):
        raw = _read(item)
        if raw is None:
            invalid.append({"index": pos, "reason": "not a candle (needs time, open, high, low, close)"})
            continue
        t, o, h, l, c = (_num(v) for v in raw)
        if None in (t, o, h, l, c):
            invalid.append({"index": pos, "reason": "missing, non-numeric or non-finite value"})
        elif h < l:
            invalid.append({"index": pos, "reason": "high is below low"})
        elif not (l <= o <= h and l <= c <= h):
            invalid.append({"index": pos, "reason": "open or close lies outside the high-low range"})
        else:
            valid.append(_Bar(t, o, h, l, c))

    out_of_order = any(valid[i].time < valid[i - 1].time for i in range(1, len(valid)))
    by_time: dict[float, _Bar] = {}
    removed = conflicting = 0
    for bar in valid:                      # later occurrences replace earlier ones: the newest update of a bar wins
        previous = by_time.get(bar.time)
        if previous is not None:
            removed += 1
            if previous != bar:
                conflicting += 1
        by_time[bar.time] = bar
    bars = [by_time[t] for t in sorted(by_time)]

    warnings = []
    if invalid:
        warnings.append(f"{len(invalid)} invalid candle(s) were excluded; neighbouring candles were treated as adjacent.")
    if removed:
        warnings.append(f"{removed} duplicate timestamp(s) removed (last occurrence kept)"
                        + (f"; {conflicting} had different prices." if conflicting else "."))
    if out_of_order:
        warnings.append("Candles were out of order and have been sorted by time.")
    diag = Diagnostics(input_count=len(items), used_count=len(bars), invalid=tuple(invalid), duplicates_removed=removed,
                       duplicates_conflicting=conflicting, out_of_order=out_of_order, warnings=tuple(warnings))
    return bars, diag


# --------------------------------------------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------------------------------------------
def _detect_pivots(bars: list[_Bar], cfg: StructureConfig) -> list[tuple[int, str, float]]:
    """(pivot_index, kind, price) for every pivot that has all its right-side candles."""
    L, R, n = cfg.swing_left, cfg.swing_right, len(bars)
    found = []
    for i in range(L, n - R):
        hi, lo = bars[i].high, bars[i].low
        left, right = bars[i - L:i], bars[i + 1:i + R + 1]
        if all(hi > b.high for b in left) and all(hi >= b.high for b in right):
            found.append((i, "high", hi))
        if all(lo < b.low for b in left) and all(lo <= b.low for b in right):
            found.append((i, "low", lo))
    return found


def _classify(kind: str, price: float, previous: float | None, tol: float) -> str | None:
    if previous is None:
        return None
    if abs(price - previous) <= tol:
        return EQH if kind == "high" else EQL
    if kind == "high":
        return HH if price > previous else LH
    return HL if price > previous else LL


def analyze_market_structure(candles: Iterable[Any], config: StructureConfig | None = None) -> StructureResult:
    """Analyse candles (sequences [time, o, h, l, c], mappings or objects with those attributes)."""
    cfg = config or StructureConfig()
    bars, diag = _clean(candles)
    n = len(bars)
    if n < cfg.min_candles:
        diag = Diagnostics(**{**diag.__dict__, "insufficient_data": True,
                              "warnings": diag.warnings + (f"Need at least {cfg.min_candles} usable candles, got {n}.",)})
        return StructureResult(config=cfg, diagnostics=diag)

    forming_index = n - 1 if not cfg.last_candle_closed else None
    pivots = _detect_pivots(bars, cfg)
    pivots.sort(key=lambda p: (p[0] + cfg.swing_right, p[0], p[1] != "high"))   # confirmation order, deterministic

    swings: list[SwingPoint] = []
    last_price: dict[str, float | None] = {"high": None, "low": None}
    by_confirmation: dict[int, list[SwingPoint]] = {}
    for i, kind, price in pivots:
        conf = i + cfg.swing_right
        swing = SwingPoint(
            id=f"{kind}:{bars[i].time:g}", kind=kind, price=price, pivot_index=i, pivot_time=bars[i].time,
            confirmed_index=conf, confirmed_time=bars[conf].time,
            classification=_classify(kind, price, last_price[kind], cfg.equal_tolerance),
            confirmed=conf != forming_index)
        last_price[kind] = price
        swings.append(swing)
        by_confirmation.setdefault(conf, []).append(swing)

    events: list[StructureEvent] = []
    trend: str | None = None
    trend_confirmed: str | None = None
    active = {"high": None, "low": None}
    protected = {"high": None, "low": None}
    continuation = {"high": None, "low": None}
    latest_confirmed = {"high": None, "low": None}   # newest confirmed pivot of each kind, whatever its classification
    wick = cfg.break_mode == "wick"
    for k, bar in enumerate(bars):
        if k == forming_index:
            trend_confirmed = trend                      # the state as of the last CLOSED candle

        # Breaks are evaluated before pivots confirmed on this candle are added, avoiding lookahead.
        trend_before = trend
        if trend_before is None:
            candidates = {"bullish": (active["high"], "bootstrap"), "bearish": (active["low"], "bootstrap")}
        elif trend_before == "bullish":
            candidates = {"bullish": (continuation["high"], "continuation_swing"),
                          "bearish": (protected["low"], "protected_swing")}
        else:
            candidates = {"bullish": (protected["high"], "protected_swing"),
                          "bearish": (continuation["low"], "continuation_swing")}

        def break_price(direction: str) -> float:
            if direction == "bullish":
                return bar.high if wick else bar.close
            return bar.low if wick else bar.close

        def crossed(direction: str, swing: SwingPoint | None) -> bool:
            if swing is None:
                return False
            price = break_price(direction)
            return price > swing.price if direction == "bullish" else price < swing.price

        broke: list[tuple[str, SwingPoint, float, str]] = []
        for direction in ("bullish", "bearish"):
            ref, role = candidates[direction]
            if crossed(direction, ref):
                broke.append((direction, ref, break_price(direction), role))
        conflict = len(broke) == 2

        # Any crossed level is consumed even when it is not the protected reference. This prevents a
        # previously broken minor pivot from becoming a delayed event on a later candle.
        crossed_active = {kind for kind, swing in active.items()
                          if swing is not None and crossed("bullish" if kind == "high" else "bearish", swing)}
        crossed_protected = {kind for kind, swing in protected.items()
                             if swing is not None and crossed("bullish" if kind == "high" else "bearish", swing)}
        crossed_continuation = {kind for kind, swing in continuation.items()
                                if swing is not None and crossed("bullish" if kind == "high" else "bearish", swing)}

        for direction, ref, price, role in broke:
            if role == "protected_swing" and ref.classification not in (HL, LH):
                role = "origin_swing"      # no HL / LH exists yet: the swing that started the breaking leg is protected
            kind = "BOS" if trend_before in (None, direction) else "CHOCH"
            certainty = "Confirmed" if k != forming_index else "Provisional forming-candle"
            if kind == "BOS" and role == "bootstrap":
                reason = f"{certainty} first qualifying {direction} break; no earlier structural direction was established."
            elif kind == "BOS":
                reason = f"{certainty} {direction} break of the latest unbroken continuation swing while structure was {trend_before}."
            else:
                what = "protected swing" if role == "protected_swing" else "origin swing (the confirmed swing that started the leg which changed the structure; no later protected HL/LH exists)"
                reason = f"{certainty} {direction} break of the {what} against established {trend_before} structure."
            ok = k != forming_index
            events.append(StructureEvent(
                id=f"{kind}:{direction}:{ref.pivot_time:g}:{bar.time:g}", type=kind, direction=direction,
                level=ref.price, reference_pivot_time=ref.pivot_time, reference_pivot_confirmed_time=ref.confirmed_time,
                candle_index=k, candle_time=bar.time, break_price=price, break_mode=cfg.break_mode,
                trend_before=trend_before, confirmed=ok, confirmed_time=bar.time if ok else None,
                same_candle_conflict=conflict, trend_after=None if conflict else direction,
                reference_role=role, reason=reason, reference_swing_id=ref.id, reference_classification=ref.classification))
        if conflict:
            trend = None
        elif broke:
            trend = broke[0][0]
        # Provisional breaks are visible in `trend`, but do not mutate confirmed references. A forming
        # pivot likewise remains display-only and cannot become a protected level.
        if k != forming_index:
            for kind in crossed_active:
                active[kind] = None
            for kind in crossed_protected:
                protected[kind] = None
            for kind in crossed_continuation:
                continuation[kind] = None

        # After a CONFIRMED CHoCH the broken-against structure needs a protected level on the other side, otherwise price
        # could run through every old extreme with no event until a fresh LH / HL happens to form. The latest confirmed
        # swing of that kind (the origin of the leg that broke structure) becomes the protected level; a later LH / HL
        # replaces it through the normal rule below. Only confirmed swings, known before this candle, are used.
        if k != forming_index and not conflict:
            for direction, ref, price, role in broke:
                if trend_before not in (None, direction):
                    kind_needed = "high" if direction == "bearish" else "low"
                    origin = latest_confirmed[kind_needed]
                    if origin is not None and (origin.price > price if direction == "bearish" else origin.price < price):
                        protected[kind_needed] = origin

        # Confirmed swings become available only after this candle's break check.
        for swing in by_confirmation.get(k, ()):
            if swing.confirmed:
                latest_confirmed[swing.kind] = swing
                active[swing.kind] = swing
                if swing.kind == "low" and swing.classification == HL:
                    protected["low"] = swing
                elif swing.kind == "high" and swing.classification == LH:
                    protected["high"] = swing
                if swing.kind == "high" and swing.classification in (HH, EQH):
                    continuation["high"] = swing
                elif swing.kind == "low" and swing.classification in (LL, EQL):
                    continuation["low"] = swing

    if forming_index is None:
        trend_confirmed = trend
    return StructureResult(config=cfg, swings=tuple(swings), events=tuple(events), trend=trend,
                           active_high=active["high"], active_low=active["low"], diagnostics=diag,
                           trend_confirmed=trend_confirmed, protected_high=protected["high"], protected_low=protected["low"],
                           continuation_high=continuation["high"], continuation_low=continuation["low"])


# --------------------------------------------------------------------------------------------------------------
# Small bounded cache: the chart refreshes often with unchanged candles, the answer is deterministic.
# --------------------------------------------------------------------------------------------------------------
_CACHE: OrderedDict = OrderedDict()
_CACHE_MAX = 16
_CACHE_LOCK = threading.Lock()


def clean_candles(candles: Iterable[Any]) -> tuple[list[_Bar], Diagnostics]:
    """The engine's own candle cleaning (invalid excluded, duplicates collapsed, sorted by time), for engines built on top of it."""
    return _clean(candles)


def analyze_cached(candles: Iterable[Any], config: StructureConfig | None = None) -> StructureResult:
    cfg = config or StructureConfig()
    rows = [_read(c) for c in candles or []]
    try:
        key = (cfg, tuple(rows))
        hash(key)
    except TypeError:                       # unhashable junk in the input: just compute
        return analyze_market_structure(candles, cfg)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
    result = analyze_market_structure(rows, cfg)
    with _CACHE_LOCK:
        _CACHE[key] = result
        while len(_CACHE) > _CACHE_MAX:      # bounded: repeated refreshes cannot grow memory
            _CACHE.popitem(last=False)
    return result
