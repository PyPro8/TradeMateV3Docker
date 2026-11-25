"""Fair Value Gap (FVG) detection engine (Phase 7C, task 1). Objective, deterministic, analysis only.

It never trades, sizes a position, sets a stop or target, or influences the Risk Engine. It is not connected to the chart, the
Analysis page or any strategy yet.

WHAT AN FVG IS (and is not)
  An FVG is a price range left between candle 1 and candle 3 of three consecutive candles, where candle 2 (the middle,
  "displacement" candle) moved so far that the outer two candles do not overlap. It is a purely geometric observation about three
  candles. It does NOT say price will return to the gap, will react there, or will reverse, and it carries no quality score: a
  detected gap is not a trade setup.

DEFINITION (strict: touching boundaries do not qualify)
  Three consecutive candles c1, c2, c3 (oldest first):
    bullish FVG : c3.low  >  c1.high   -> lower = c1.high, upper = c3.low
    bearish FVG : c3.high <  c1.low    -> upper = c1.low,  lower = c3.high
  Nothing about c2 is tested: its range is irrelevant to whether the gap exists. If c3.low == c1.high (or c3.high == c1.low) the
  candles merely touch and there is no gap. One window can never be both bullish and bearish.
  Every window of three consecutive cleaned candles is examined, so overlapping windows (c1..c3, c2..c4, ...) may each yield a gap.

TIMESTAMPS (the same no-lookahead idea as the swing and liquidity engines)
  first_time / middle_time / third_time: the three candles' own times. A gap only becomes KNOWABLE when candle 3 exists, so
  `formed_time` == `third_time`. Nothing from a later candle is ever used; analysing a prefix of the data gives the same gaps
  (for everything inside that prefix) as analysing all of it.

IDENTITY / NO DUPLICATES
  id = `FVG-<BULL|BEAR>-<first candle time in whole epoch seconds>` (plain digits, as the liquidity ids). The result is a pure
  function of the candles: analysing the same data twice, analysing it with repeated rows, or analysing a growing series on
  every refresh yields the same ids and each gap exactly once. A caller that keeps gaps between refreshes should key on `id`.

FORMING CANDLE
  `FVGConfig.last_candle_closed` defaults to False: the newest candle is assumed still forming (the safe assumption, exactly as
  the liquidity engine and the Analysis page treat it, because the bridge exports no candle-completion flag and no broker clock).
  A gap whose third candle is the forming one is reported with confirmed=False and confirmed_time=None: its boundary (c3's low
  or high) can still change, and the gap can disappear when that candle closes. It is never silently treated as confirmed.
  Pass last_candle_closed=True only when the newest candle is known to be closed.

DATA TREATMENT (fail closed, because an FVG depends on which candles are NEIGHBOURS)
  The structure engine's cleaning is reused (`clean_candles`): out-of-order input is sorted by time (flagged in diagnostics),
  duplicate timestamps keep the last occurrence. Beyond that:
    * a candle that is invalid (missing / non-numeric / non-finite value, high < low, open or close outside [low, high]) or has a
      price <= 0 (never a real quote; a zero is how a feed reports "no data"), or two rows with the same time but different
      prices, makes the result `data_status="invalid"` with NO gaps. Dropping such a candle would make the candles on either side
      of it look adjacent and could manufacture a gap that never existed, so nothing is guessed (the Analysis page refuses the
      same data for the same reason).
    * fewer than 3 usable candles -> `data_status="insufficient"`, no gaps.
  Missing prices or timestamps are never filled in. Time gaps between candles (weekends, market closures, a missing bar) are NOT
  detected: the engine knows no timeframe length, so "consecutive" means consecutive in the supplied data. The caller supplies a
  contiguous series from the broker.

SYMBOL / TIMEFRAME
  Candles carry neither. Optional `symbol` and `timeframe` labels given to `analyze_fvg` are copied onto the result and each gap
  unchanged (None when not given); they never affect detection.

NOT IMPLEMENTED (deliberately; no justified specification yet): minimum gap size, quality or strength scores, partial / full fill
(mitigation) tracking, expiry, higher-timeframe logic, merging of adjacent gaps, chart or Analysis integration.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from trademate.structure import Diagnostics, clean_candles

BULLISH, BEARISH = "bullish", "bearish"
DATA_OK, DATA_INSUFFICIENT, DATA_INVALID = "ok", "insufficient", "invalid"
MIN_CANDLES = 3


@dataclass(frozen=True)
class FVGConfig:
    last_candle_closed: bool = False    # False (default) = the newest candle is still forming (safe assumption)

    def validate(self) -> "FVGConfig":
        if not isinstance(self.last_candle_closed, bool):
            raise ValueError("last_candle_closed must be true or false.")
        return self

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FairValueGap:
    id: str
    direction: str                  # "bullish" | "bearish"
    upper: float                    # upper boundary of the gap
    lower: float                    # lower boundary of the gap (always < upper)
    size: float                     # upper - lower (> 0)
    first_time: float               # candle 1
    middle_time: float              # candle 2 (the displacement candle)
    third_time: float               # candle 3
    formed_time: float              # when the gap became knowable == third_time
    first_index: int                # positions in the CLEANED, time-sorted candle list (not in the caller's input)
    middle_index: int
    third_index: int
    confirmed: bool                 # False only while candle 3 is the still-forming candle (the gap may change or vanish)
    confirmed_time: float | None    # == third_time once confirmed, else None
    symbol: str | None = None
    timeframe: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FVGResult:
    config: FVGConfig
    gaps: tuple = ()                # chronological (by first candle), every gap exactly once
    data_status: str = DATA_OK      # "ok" | "insufficient" | "invalid"
    reason: str = ""                # why the status is not "ok" (empty when ok)
    candles_used: int = 0
    revision: str = ""              # hash of the cleaned candles; changes when MT5 corrects or adds data
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    non_positive_price_times: tuple = ()   # times of candles rejected for a price <= 0
    symbol: str | None = None
    timeframe: str | None = None
    note: str = ("A fair value gap is a price range between two non-overlapping candles. It is an observation, not a prediction, "
                 "a quality rating or a trade signal.")

    def to_dict(self) -> dict:
        d = self.diagnostics.to_dict()
        return {"config": self.config.to_dict(), "gaps": [g.to_dict() for g in self.gaps], "data_status": self.data_status,
                "reason": self.reason, "candles_used": self.candles_used, "revision": self.revision, "diagnostics": d,
                "non_positive_price_times": list(self.non_positive_price_times), "symbol": self.symbol,
                "timeframe": self.timeframe, "note": self.note}


def _tid(value: float) -> str:
    """A time as plain digits for ids (epoch seconds: 1760000000, never 1.76e+09, which would make different times collide)."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _revision(bars) -> str:
    h = hashlib.sha1()
    for b in bars:
        h.update(f"{b.time!r}|{b.open!r}|{b.high!r}|{b.low!r}|{b.close!r};".encode())
    return h.hexdigest()[:12]


def _label(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text or None.")
    return value


def analyze_fvg(candles: Iterable[Any], config: FVGConfig | None = None, *, symbol: str | None = None,
                timeframe: str | None = None) -> FVGResult:
    """Fair value gaps in `candles` (sequences [time, o, h, l, c], mappings or objects with those attributes, as for the structure
    engine). Raises ValueError for a bad config or a non-text symbol / timeframe; bad CANDLES never raise: they are reported in
    `data_status` / `diagnostics` and no gap is returned."""
    cfg = (config or FVGConfig()).validate()
    symbol, timeframe = _label(symbol, "symbol"), _label(timeframe, "timeframe")
    bars, diag = clean_candles(candles)
    n = len(bars)
    base = dict(config=cfg, candles_used=n, diagnostics=diag, symbol=symbol, timeframe=timeframe)

    bad_prices = tuple(b.time for b in bars if min(b.open, b.high, b.low, b.close) <= 0)
    if diag.invalid or diag.duplicates_conflicting or bad_prices:
        parts = []
        if diag.invalid:
            parts.append(f"{len(diag.invalid)} candle(s) had impossible or missing values")
        if bad_prices:
            parts.append(f"{len(bad_prices)} candle(s) had a price of zero or below")
        if diag.duplicates_conflicting:
            parts.append(f"{diag.duplicates_conflicting} duplicate timestamp(s) had conflicting prices")
        return FVGResult(**base, data_status=DATA_INVALID, non_positive_price_times=bad_prices,
                         reason="; ".join(parts) + ". No gaps were reported: excluding such candles would make their neighbours "
                                                   "look adjacent and could create a gap that never existed.")
    if n < MIN_CANDLES:
        return FVGResult(**base, data_status=DATA_INSUFFICIENT, revision=_revision(bars),
                         reason=f"Need at least {MIN_CANDLES} usable candles, got {n}.")

    forming = None if cfg.last_candle_closed else n - 1
    gaps: list[FairValueGap] = []
    for k in range(2, n):
        c1, c2, c3 = bars[k - 2], bars[k - 1], bars[k]
        if c3.low > c1.high:                                    # strict: equal means touching, not a gap
            direction, lower, upper, tag = BULLISH, c1.high, c3.low, "BULL"
        elif c3.high < c1.low:
            direction, lower, upper, tag = BEARISH, c3.high, c1.low, "BEAR"
        else:
            continue
        ok = k != forming
        gaps.append(FairValueGap(
            id=f"FVG-{tag}-{_tid(c1.time)}", direction=direction, upper=upper, lower=lower, size=upper - lower,
            first_time=c1.time, middle_time=c2.time, third_time=c3.time, formed_time=c3.time,
            first_index=k - 2, middle_index=k - 1, third_index=k, confirmed=ok, confirmed_time=c3.time if ok else None,
            symbol=symbol, timeframe=timeframe))
    return FVGResult(**base, gaps=tuple(gaps), revision=_revision(bars))
