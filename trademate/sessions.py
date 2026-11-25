"""Trading sessions and instrument availability.

TWO DIFFERENT KINDS OF FACT, labelled differently on screen:
  * Session windows (Sydney, Tokyo, London, New York) are the CONVENTIONAL local business hours of those financial centres, converted to UTC with each city's own
    daylight-saving rules (zoneinfo). They describe when liquidity is typically concentrated. They are NOT broker data and are not a promise that a market is open.
  * Instrument availability comes from the instrument's asset class: synthetic indices (Vol_*, Crash/Boom, ...) trade continuously (24/7) on the broker's platform; crypto is
    normally 24/7 but depends on the broker; the forex/metals week runs from Sunday 22:00 UTC to Friday 22:00 UTC (approximately, the usual close of the New York rollover);
    equity indices follow their exchange and the broker, so their hours are reported as UNVERIFIED rather than guessed. The bridge does not send MT5's per-symbol trading
    sessions, so no instrument's exact hours are claimed.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

SESSIONS = (
    {"id": "sydney", "name": "Sydney", "tz": "Australia/Sydney", "open": (7, 0), "close": (16, 0), "pairs": ("AUDUSD", "AUDJPY", "NZDUSD", "AUDNZD", "NZDJPY", "AUDCAD")},
    {"id": "tokyo", "name": "Tokyo", "tz": "Asia/Tokyo", "open": (9, 0), "close": (18, 0), "pairs": ("USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CHFJPY", "CADJPY")},
    {"id": "london", "name": "London", "tz": "Europe/London", "open": (8, 0), "close": (17, 0), "pairs": ("EURUSD", "GBPUSD", "EURGBP", "EURCHF", "USDCHF", "GBPJPY", "EURJPY", "XAUUSD")},
    {"id": "newyork", "name": "New York", "tz": "America/New_York", "open": (8, 0), "close": (17, 0), "pairs": ("EURUSD", "GBPUSD", "USDCAD", "USDJPY", "XAUUSD", "USDMXN")},
)
FX_WEEK_NOTE = "Forex and metals trade from about Sunday 22:00 UTC to Friday 22:00 UTC."


def _window(sess, now_utc: datetime):
    """The session window containing `now_utc` if open, else the next one: (open_utc, close_utc)."""
    tz = ZoneInfo(sess["tz"])
    local_now = now_utc.astimezone(tz)
    for day_offset in (-1, 0, 1, 2, 3):                                      # yesterday's window can still be open (Sydney); then the next weekday
        d = local_now.date() + timedelta(days=day_offset)
        if d.weekday() >= 5:
            continue
        o = datetime.combine(d, dtime(*sess["open"]), tzinfo=tz).astimezone(timezone.utc)
        c = datetime.combine(d, dtime(*sess["close"]), tzinfo=tz).astimezone(timezone.utc)
        if c > now_utc:
            return o, c
    raise RuntimeError("no session window found")


def fx_week_open(now_utc: datetime) -> bool:
    wd, h = now_utc.weekday(), now_utc.hour + now_utc.minute / 60
    if wd == 5:
        return False
    if wd == 6:
        return h >= 22
    if wd == 4:
        return h < 22
    return True


def availability(asset_class: str, now_utc: datetime) -> dict:
    if asset_class == "synthetic":
        return {"mode": "continuous", "open_now": True, "label": "Continuous (24/7)", "verified": False, "detail": "Synthetic indices trade around the clock on the broker's platform."}
    if asset_class == "crypto":
        return {"mode": "continuous", "open_now": True, "label": "Normally 24/7", "verified": False, "detail": "Crypto CFDs are normally continuous, but this depends on the broker."}
    if asset_class in ("forex_major", "forex_jpy", "forex_other", "metal"):
        return {"mode": "fx_week", "open_now": fx_week_open(now_utc), "label": "Forex week (Sun 22:00 - Fri 22:00 UTC)", "verified": False, "detail": FX_WEEK_NOTE}
    if asset_class == "index":
        return {"mode": "exchange", "open_now": None, "label": "Exchange hours (unverified)", "verified": False, "detail": "Index CFD hours follow the exchange and the broker; TradeMate does not know them."}
    return {"mode": "unknown", "open_now": None, "label": "Hours unknown", "verified": False, "detail": "The asset class could not be determined."}


def snapshot(now_utc: datetime, symbols=(), selected: str | None = None, classify=lambda s: "unknown") -> dict:
    sessions, current = [], []
    for s in SESSIONS:
        o, c = _window(s, now_utc)
        is_open = o <= now_utc < c
        if is_open:
            current.append(s["name"])
        have = [p for p in s["pairs"] if not symbols or any(sym.upper().startswith(p) for sym in symbols)]
        sessions.append({"id": s["id"], "name": s["name"], "timezone": s["tz"], "open_utc": o.isoformat(), "close_utc": c.isoformat(), "is_open": is_open,
                         "local_hours": f"{s['open'][0]:02d}:{s['open'][1]:02d}-{s['close'][0]:02d}:{s['close'][1]:02d} {s['tz']}",
                         "seconds_to_change": int(((c if is_open else o) - now_utc).total_seconds()), "pairs": have})
    groups: dict = {}
    for sym in symbols:
        groups.setdefault(classify(sym), []).append(sym)
    sel = None
    if selected:
        cls = classify(selected)
        sel = {"symbol": selected, "asset_class": cls, "availability": availability(cls, now_utc)}
    return {"now_utc": now_utc.isoformat(), "current": current, "overlap": len(current) > 1, "sessions": sessions, "instrument_groups": {k: sorted(v) for k, v in groups.items()}, "selected": sel,
            "note": "Session windows are conventional financial-centre hours (not broker data). Instrument availability is by asset class; exact per-instrument hours are not reported by the bridge."}
