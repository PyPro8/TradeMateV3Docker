"""Timeframes and data-quality validation for the multi-timeframe Strategy Engine.

Pure functions: candles in, a validated series out. No Flask, no broker, no orders.

TIMES
    Every candle time is MT5 SERVER (broker) time stored as if it were a UTC epoch (see trademate/brokertime.py).
    `now_server` below is therefore "now on the broker's clock" (UTC epoch + the measured server offset), or None when
    the broker clock could not be determined. Nothing here ever uses this computer's clock on its own.

CLOSED-CANDLE POLICY (documented in docs/MULTI_TIMEFRAME_STRATEGY.md)
    A decision depends on CLOSED candles only. A candle that opened at `t` is closed once `now_server >= t + length`.
    The newest candle that is not closed is the FORMING candle: it is removed from the series that the structure
    engines see and is reported separately, so a forming candle can never be counted as a completed structural break.
    When `now_server` is None the newest candle is assumed to be forming (the safe assumption) and the series is
    flagged `clock_unknown`, which prevents a confirmed setup.

H4
    H4 candles are fetched directly from the broker's native H4 feed. They are never synthesized from H1 bars.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trademate.structure import clean_candles

TF_SECONDS = {"M1": 60, "M2": 120, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}   # M1 / M30 were added for the selectable execution timeframe; TIMEFRAMES below (the legacy four-timeframe engine) is unchanged
TIMEFRAMES = ("M5", "M15", "H1", "H4")          # the four timeframes of the strategy, lowest first
NATIVE_TIMEFRAMES = TIMEFRAMES                    # all four are supplied natively by TradeMateBridge

# Data statuses of one timeframe, from best to worst.
OK = "ok"                    # fresh, closed-candle based, aligned, sufficient and free of defects in the recent window
DEGRADED = "degraded"        # usable for analysis but with a defect that forbids a confirmed setup
STALE = "stale"              # the newest closed candle is too old (feed lagging or market closed)
INSUFFICIENT = "insufficient"
MISALIGNED = "misaligned"    # candle times do not sit on this timeframe's grid
UNAVAILABLE = "unavailable"  # no candles / the bridge could not supply them / the timeframe cannot be built
USABLE = (OK, DEGRADED)      # statuses for which structure analysis is run
STATUSES = (OK, DEGRADED, STALE, INSUFFICIENT, MISALIGNED, UNAVAILABLE)


@dataclass(frozen=True)
class DataRules:
    """Thresholds of the data-quality checks (policy values; documented defaults)."""

    min_candles: dict = field(default_factory=lambda: {"M5": 60, "M15": 60, "H1": 60, "H4": 30})
    integrity_window: dict = field(default_factory=lambda: {"M5": 12, "M15": 12, "H1": 12, "H4": 6})
    stale_after_periods: float = 2.0     # newest CLOSED candle must have closed less than this many candle lengths ago
    future_tolerance_seconds: int = 60   # a candle opening further than this ahead of the broker clock is impossible


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str        # "block" = no confirmed setup while it is present | "info" = reported only
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass(frozen=True)
class PreparedSeries:
    """One timeframe's candles after validation. `closed` is what the analysis engines may use."""

    timeframe: str
    status: str
    reason: str
    closed: tuple = ()                   # rows [time, o, h, l, c], ascending, closed candles only
    forming: tuple | None = None         # the newest candle when it is not closed yet (row), else None
    issues: tuple = ()
    received: int = 0
    newest_closed_time: float | None = None
    newest_closed_end: float | None = None      # when the newest closed candle closed
    lag_seconds: float | None = None            # now_server - newest_closed_end (None when the clock is unknown)
    clock_known: bool = False
    gap_count_recent: int = 0
    gap_count_older: int = 0
    build: dict | None = None

    @property
    def usable(self) -> bool:
        return self.status in USABLE

    def to_dict(self) -> dict:
        return {"timeframe": self.timeframe, "status": self.status, "reason": self.reason, "received": self.received,
                "closed_count": len(self.closed), "forming_candle_present": self.forming is not None,
                "newest_closed_time": self.newest_closed_time, "newest_closed_end": self.newest_closed_end,
                "lag_seconds": self.lag_seconds, "clock_known": self.clock_known,
                "issues": [i.to_dict() for i in self.issues], "gaps_recent": self.gap_count_recent,
                "gaps_older": self.gap_count_older, "build": self.build}


def _rows(bars) -> list:
    return [[b.time, b.open, b.high, b.low, b.close] for b in bars]


def _empty(tf: str, status: str, reason: str, received: int = 0, issues=(), clock_known=False) -> PreparedSeries:
    return PreparedSeries(timeframe=tf, status=status, reason=reason, received=received, issues=tuple(issues),
                          clock_known=clock_known)


def prepare_series(rows, tf: str, now_server: float | None, rules: DataRules | None = None) -> PreparedSeries:
    """Validate `rows` ([time, o, h, l, c] each) for timeframe `tf` and split them into closed candles and the forming one.

    Never raises for bad candles: every problem becomes an issue and a status. Nothing is repaired or invented: invalid candles
    are excluded, conflicting duplicates and unordered input are reported (they forbid a confirmed setup).
    """
    rules = rules or DataRules()
    if tf not in TF_SECONDS:
        raise ValueError(f"Unknown timeframe {tf!r}.")
    length = TF_SECONDS[tf]
    clock_known = now_server is not None
    items = list(rows or [])
    if not items:
        return _empty(tf, UNAVAILABLE, f"No {tf} candles were supplied.", 0, clock_known=clock_known)
    bars, diag = clean_candles(items)
    issues = []
    if diag.invalid:
        issues.append(Issue("invalid_candles", "block", f"{len(diag.invalid)} invalid candle(s) were excluded; their neighbours "
                                                        "would look adjacent."))
    if diag.duplicates_removed:
        conflicting = diag.duplicates_conflicting
        issues.append(Issue("duplicate_candles", "block" if conflicting else "info",
                            f"{diag.duplicates_removed} duplicate timestamp(s) removed (last kept)"
                            + (f"; {conflicting} had different prices, so the data source is not consistent." if conflicting else ".")))
    if diag.out_of_order:
        issues.append(Issue("out_of_order", "block", "Candles arrived out of time order and were sorted; the data source is "
                                                     "not behaving as expected."))
    if any(min(b.open, b.high, b.low, b.close) <= 0 for b in bars):
        issues.append(Issue("non_positive_price", "block", "A candle has a price of zero or below."))
    if not bars:
        return _empty(tf, UNAVAILABLE, f"No usable {tf} candles remained after validation.", len(items), issues, clock_known)

    off = [b for b in bars if b.time % length != 0]
    if off:
        return _empty(tf, MISALIGNED, f"{len(off)} {tf} candle time(s) do not sit on the {tf} grid (e.g. {int(off[0].time)}); "
                                      "the candles cannot be trusted as this timeframe.", len(items), issues, clock_known)

    forming = None
    if clock_known:
        future = [b for b in bars if b.time > now_server + rules.future_tolerance_seconds]
        if future:
            issues.append(Issue("future_candle", "block", f"{len(future)} candle(s) open in the future of the broker clock."))
        bars = [b for b in bars if b.time <= now_server + rules.future_tolerance_seconds]
        if bars and bars[-1].time + length > now_server:       # newest candle has not closed yet
            forming = bars[-1]
            bars = bars[:-1]
    else:
        issues.append(Issue("clock_unknown", "block", "The broker clock is unknown, so closed and forming candles cannot be told apart; "
                                                      "the newest candle was treated as forming."))
        forming = bars[-1]
        bars = bars[:-1]
    closed_rows = _rows(bars)
    forming_row = _rows([forming])[0] if forming is not None else None

    need = rules.min_candles.get(tf, 30)
    if len(closed_rows) < need:
        return PreparedSeries(tf, INSUFFICIENT, f"{len(closed_rows)} closed {tf} candle(s); at least {need} are needed.",
                              tuple(map(tuple, closed_rows)), tuple(forming_row) if forming_row else None, tuple(issues),
                              len(items), clock_known=clock_known)

    newest = closed_rows[-1][0]
    end = newest + length
    lag = (now_server - end) if clock_known else None

    # Missing candles. Market closures also leave gaps, so only gaps inside the RECENT window block a confirmed setup.
    window = rules.integrity_window.get(tf, 12)
    times = [r[0] for r in closed_rows]
    recent_gaps = older_gaps = 0
    for i in range(1, len(times)):
        missing = int(round((times[i] - times[i - 1]) / length)) - 1
        if missing > 0:
            if i >= len(times) - window:
                recent_gaps += missing
            else:
                older_gaps += missing
    if recent_gaps:
        issues.append(Issue("recent_gap", "block", f"{recent_gaps} {tf} candle(s) are missing inside the newest {window} candles "
                                                   "(missing data or a market closure)."))
    if older_gaps:
        issues.append(Issue("older_gaps", "info", f"{older_gaps} missing {tf} candle(s) earlier in the series (market closures or "
                                                  "missing data); not used to block."))

    status, reason = OK, f"{len(closed_rows)} closed {tf} candles, newest closed at {int(newest)}."
    if clock_known and lag > rules.stale_after_periods * length:
        status, reason = STALE, (f"The newest closed {tf} candle closed {int(lag)} s ago (more than {rules.stale_after_periods:g} "
                                 f"candle lengths): the feed is lagging or the market is closed.")
    elif any(i.severity == "block" for i in issues):
        status = DEGRADED
        reason = "; ".join(i.message for i in issues if i.severity == "block")
    return PreparedSeries(tf, status, reason, tuple(map(tuple, closed_rows)), tuple(forming_row) if forming_row else None,
                          tuple(issues), len(items), newest, end, lag, clock_known, recent_gaps, older_gaps)
