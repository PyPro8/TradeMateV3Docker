"""The trading day for daily-loss protection = the COMPUTER's local calendar day (local 00:00 to local 00:00), not the broker server's day.

Broker timestamps (candles, deal history) are on the broker server's clock; they are converted to UTC with the measured server offset (trademate.brokertime),
and compared with the local day's boundaries, which are computed in UTC through the local timezone's own rules (zoneinfo), so a daylight-saving change makes that
day 23 or 25 hours long instead of shifting the boundary by an hour. The timezone is the configured IANA name ([ui] timezone / TRADEMATE_TIMEZONE) or, when empty, the operating
system's. A timezone that cannot be determined is reported (never silently UTC) and makes the daily loss UNKNOWN, which blocks new trades.
"""

from __future__ import annotations

import os
from datetime import datetime, time as dtime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TimezoneProblem(Exception):
    pass


def resolve_tz(name: str | None = None) -> tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise TimezoneProblem(f"The configured local timezone {name!r} is not a valid IANA name ({exc}).") from exc
    env = os.environ.get("TZ")
    if env:
        try:
            return ZoneInfo(env.lstrip(":"))
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    try:                                                                     # Linux / macOS: /etc/localtime points into the zoneinfo database
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return ZoneInfo(target.split("zoneinfo/", 1)[1])
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass
    local = datetime.now().astimezone().tzinfo                               # Windows or a stripped system: the OS's current offset (no DST rules known)
    if local is None:
        raise TimezoneProblem("The operating system's timezone could not be determined. Set timezone in the [ui] section of config/settings.toml.")
    return local


def local_day_start_utc(now_utc: float, tz: tzinfo) -> float:
    """UTC epoch of the most recent local 00:00 at or before `now_utc`."""
    day = datetime.fromtimestamp(now_utc, tz).date()
    return datetime.combine(day, dtime(0, 0), tzinfo=tz).timestamp()


def next_local_midnight_utc(now_utc: float, tz: tzinfo) -> float:
    day = datetime.fromtimestamp(now_utc, tz).date() + timedelta(days=1)
    return datetime.combine(day, dtime(0, 0), tzinfo=tz).timestamp()
