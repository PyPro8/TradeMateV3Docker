"""Broker (MT5 server) clock: the single place that answers "what time is it on the broker's clock?".

WHY THIS EXISTS
  MT5 reports every time (candles, deal history) on the BROKER SERVER's wall clock, stored as if it were a UTC epoch.
  The trading day that the daily-loss counters and the candle countdown depend on is therefore a BROKER day, not the day
  of the computer TradeMate runs on. The server's offset from UTC is not in the bridge snapshot, so it is measured from
  a FRESH 1-minute candle (its open time is at most about a minute behind "now").

THE RULE: NEVER GUESS THE BOUNDARY
  Order of trust, highest first:
    live        measured just now from fresh 1-minute candles (two symbols must agree when two are available)
    cached      the last live measurement, reused for at most CACHE_MAX_AGE_SECONDS while a measurement is not possible
    configured  `[risk] broker_timezone` in config/settings.toml (an IANA name; follows the broker's own DST)
    unknown     nothing trustworthy: callers must report the problem and fail safe. This module NEVER falls back to the
                computer's own timezone, because that produced a wrong trading day (see docs/DAILY_LOSS_AND_CLOCKS.md).
"""

from __future__ import annotations

import logging
import math
import threading
import weakref
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trademate.errors import BrokerError

logger = logging.getLogger("trademate.brokertime")

DAY = 86400
# Candle length in seconds for every timeframe the bridge can serve (the chart toolbar offers M1..D1; M30 exists in the adapter).
TF_SECONDS = {"M1": 60, "M2": 120, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}

# A real server offset is a multiple of 15 minutes and never more than 14 hours.
OFFSET_STEP = 900
MAX_ABS_OFFSET = 14 * 3600
# The newest M1 candle opened at most ~60 s before "now" on the broker clock; allow some clock difference between this
# computer and the broker either way. Anything outside this window is a stale candle, not a measurement.
PROBE_MAX_BEHIND = 90
PROBE_MAX_AHEAD = 45
PROBE_MAX_SYMBOLS = 4
PROBE_AGREE = 2                 # stop as soon as this many symbols agree

LIVE_REUSE_SECONDS = 60         # a live measurement is reused this long (saves a bridge round-trip per request)
CACHE_MAX_AGE_SECONDS = 12 * 3600


def valid_timezone(name: str) -> bool:
    """True when `name` is an IANA timezone this computer knows (e.g. America/New_York). Empty is not valid."""
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        return False
    try:
        ZoneInfo(name.strip())
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def zone_offset_seconds(name: str, now_utc: float) -> int:
    """UTC offset of an IANA timezone at the instant `now_utc` (DST-aware). Raises ValueError for an unknown zone."""
    if not valid_timezone(name):
        raise ValueError(f"Unknown timezone {name!r}.")
    moment = datetime.fromtimestamp(now_utc, timezone.utc).astimezone(ZoneInfo(name.strip()))
    return int(moment.utcoffset().total_seconds())


def day_start_from_offset(now_utc: float, offset_seconds: int) -> int:
    """Start (in the broker clock's epoch) of the broker day that contains `now_utc`."""
    server_now = int(now_utc) + int(offset_seconds)
    return server_now - server_now % DAY


def next_reset_utc(now_utc: float, offset_seconds: int) -> int:
    """UTC epoch of the next broker-day boundary (when the daily counters start again)."""
    return day_start_from_offset(now_utc, offset_seconds) + DAY - int(offset_seconds)


def offset_from_candle(candle_time: float, now_utc: float) -> int | None:
    """Server offset implied by ONE fresh 1-minute candle, or None when the candle is too old/odd to be a measurement."""
    try:
        raw = float(candle_time) - float(now_utc)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(raw):
        return None
    rounded = round(raw / OFFSET_STEP) * OFFSET_STEP
    if abs(rounded) > MAX_ABS_OFFSET or not (-PROBE_MAX_BEHIND <= raw - rounded <= PROBE_MAX_AHEAD):
        return None
    return int(rounded)


@dataclass(frozen=True)
class ServerClock:
    """The answer. `offset_seconds` is None when `source` is "unknown"; `detail` is a sentence safe to show the user."""

    offset_seconds: int | None
    source: str                  # live | cached | configured | unknown
    detail: str
    verified_at: float | None = None   # when a live measurement was last made (UTC epoch)

    @property
    def known(self) -> bool:
        return self.offset_seconds is not None

    def to_dict(self) -> dict:
        return asdict(self)


def _fmt_offset(seconds: int) -> str:
    sign = "+" if seconds >= 0 else "-"
    total = abs(int(seconds)) // 60
    return f"UTC{sign}{total // 60:02d}:{total % 60:02d}"


UNKNOWN_HELP = ("Broker server time could not be determined: MT5 returned no fresh 1-minute candle (market closed, MT5 "
                "disconnected, or this computer's clock differs from the broker's by more than a minute). "
                "Set [risk] broker_timezone in config/settings.toml to your broker's timezone, or try again when the market is open.")


class ServerClockResolver:
    """Measures the broker's UTC offset and remembers the last good measurement. Thread-safe; one per application."""

    def __init__(self, configured_timezone: str = "", *, live_reuse: float = LIVE_REUSE_SECONDS,
                 max_age: float = CACHE_MAX_AGE_SECONDS):
        self._lock = threading.Lock()
        self.configured_timezone = (configured_timezone or "").strip()
        self._live_reuse, self._max_age = live_reuse, max_age
        self._offset: int | None = None
        self._verified_at: float | None = None
        self._detail = ""
        self._owner = None            # weakref to the adapter the measurement belongs to

    def configure(self, timezone_name: str) -> None:
        with self._lock:
            self.configured_timezone = (timezone_name or "").strip()

    def reset(self) -> None:
        with self._lock:
            self._offset = self._verified_at = self._owner = None
            self._detail = ""

    # ------------------------------------------------------------------ measuring
    @staticmethod
    def probe(adapter, symbols, now_utc: float) -> tuple[int | None, str]:
        """Measure from fresh 1-minute candles. (None, why) when nothing trustworthy came back; conflicting symbols = None."""
        found: list[tuple[int, str]] = []
        errors = 0
        for name in list(symbols)[:PROBE_MAX_SYMBOLS]:
            try:
                candles = adapter.get_candles(name, "M1", 10)
            except (BrokerError, NotImplementedError):
                errors += 1
                if errors >= 2 and not found:
                    break               # the bridge is not answering: do not wait for every symbol
                continue
            if not candles:
                continue
            offset = offset_from_candle(candles[-1].time, now_utc)
            if offset is None:
                continue
            found.append((offset, name))
            if len(found) >= PROBE_AGREE:
                break
        if not found:
            return None, "no fresh 1-minute candle was available"
        offsets = {o for o, _ in found}
        if len(offsets) > 1:
            return None, "symbols disagree about the broker's clock (" + ", ".join(f"{n}: {_fmt_offset(o)}" for o, n in found) + ")"
        offset = found[0][0]
        return offset, f"{_fmt_offset(offset)} measured from the live {', '.join(n for _, n in found)} candle"

    def resolve(self, adapter, symbols, now_utc: float) -> ServerClock:
        """`symbols` is a list of names to probe, or a callable returning one (only called when a probe is needed)."""
        with self._lock:
            if self._owner is not None and self._owner() is not adapter:
                self._offset = self._verified_at = self._owner = None     # a different broker connection: never reuse
            if (self._offset is not None and self._verified_at is not None
                    and 0 <= now_utc - self._verified_at < self._live_reuse):
                return ServerClock(self._offset, "live", self._detail, self._verified_at)
            names = symbols() if callable(symbols) else symbols
            offset, why = self.probe(adapter, names or [], now_utc)
            if offset is not None:
                self._offset, self._verified_at, self._detail = offset, now_utc, why
                try:
                    self._owner = weakref.ref(adapter)
                except TypeError:
                    self._owner = None
                warn = self._cross_check(offset, now_utc)
                return ServerClock(offset, "live", why + warn, now_utc)
            # Not measurable right now. Reuse the last measurement for a limited time, then the configured zone, else unknown.
            if self._offset is not None and self._verified_at is not None and 0 <= now_utc - self._verified_at <= self._max_age:
                age = int(now_utc - self._verified_at)
                return ServerClock(self._offset, "cached",
                                   f"{_fmt_offset(self._offset)} from the last live measurement {age // 60} min ago "
                                   f"(no fresh candle now: {why})", self._verified_at)
            if self.configured_timezone:
                try:
                    cfg = zone_offset_seconds(self.configured_timezone, now_utc)
                    return ServerClock(cfg, "configured",
                                       f"{_fmt_offset(cfg)} from the configured broker timezone {self.configured_timezone} "
                                       f"(no fresh candle now: {why})", None)
                except ValueError:
                    logger.warning("Configured broker_timezone %r is not a known timezone", self.configured_timezone)
            return ServerClock(None, "unknown", f"{UNKNOWN_HELP} ({why})", None)

    def _cross_check(self, live: int, now_utc: float) -> str:
        if not self.configured_timezone:
            return ""
        try:
            cfg = zone_offset_seconds(self.configured_timezone, now_utc)
        except ValueError:
            return ""
        if cfg != live:
            logger.warning("Measured broker offset %s differs from broker_timezone %s (%s); using the measured one",
                           _fmt_offset(live), self.configured_timezone, _fmt_offset(cfg))
            return f"; NOTE: [risk] broker_timezone {self.configured_timezone} says {_fmt_offset(cfg)}, the live measurement wins"
        return ""


# The application-wide resolver (app.py applies `[risk] broker_timezone` to it at startup).
DEFAULT_RESOLVER = ServerClockResolver()
