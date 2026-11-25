"""Bull/Bear market-structure state. Pure functions, no broker, no clock.

The direction of the market is decided ONLY by the structure engine's confirmed breaks on CLOSED candles of the user's timeframe:

  * the latest VALID CHoCH (a confirmed break of a confirmed swing, no same-candle conflict, reference swing confirmed before the break candle) establishes
    the direction: a bullish CHoCH -> bullish structure, a bearish CHoCH -> bearish structure;
  * BOS events after it are continuation: the direction stays;
  * a NEWER valid CHoCH in the opposite direction invalidates the old direction and establishes the opposite one;
  * before any valid CHoCH exists there is NO direction: nothing is traded ("no entry before a CHoCH").

A candle may be entered only when its direction equals the structure direction (`gate`).
"""

from __future__ import annotations

import math

BULL, BEAR = "bullish", "bearish"
NO_CHOCH, ESTABLISHED, NOT_ALIGNED = "NO_CHOCH", "ESTABLISHED", "NOT_ALIGNED"


def _fin(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def valid_choch(ev) -> bool:
    return (isinstance(ev, dict) and ev.get("type") == "CHOCH" and ev.get("confirmed") is True and ev.get("same_candle_conflict") is not True
            and ev.get("direction") in (BULL, BEAR) and _fin(ev.get("level")) and _fin(ev.get("candle_time"))
            and _fin(ev.get("reference_pivot_confirmed_time")) and ev["reference_pivot_confirmed_time"] < ev["candle_time"])


def structure_from_events(events) -> dict:
    """-> {"state": NO_CHOCH | ESTABLISHED, "direction", "choch_time", "choch_level", "previous_direction", "reversals", "summary"}."""
    direction, ev_ok, prev, flips = None, None, None, 0
    for ev in events or []:
        if not valid_choch(ev):
            continue
        if direction is not None and ev["direction"] != direction:
            prev, flips = direction, flips + 1
        direction, ev_ok = ev["direction"], ev
    if direction is None:
        return {"state": NO_CHOCH, "direction": None, "choch_time": None, "choch_level": None, "previous_direction": None, "reversals": 0,
                "summary": "No valid CHoCH has established a direction yet: Bull/Bear does not trade."}
    return {"state": ESTABLISHED, "direction": direction, "choch_time": int(ev_ok["candle_time"]), "choch_level": ev_ok["level"], "previous_direction": prev,
            "reversals": flips,
            "summary": f"A {direction} CHoCH established {direction} structure" + (f" (it invalidated the earlier {prev} structure)." if prev else ".")}


def unavailable(reason: str) -> dict:
    return {"state": NO_CHOCH, "direction": None, "choch_time": None, "choch_level": None, "previous_direction": None, "reversals": 0, "unavailable": True,
            "summary": f"Market structure could not be established: {reason} Nothing is traded."}


def gate(candle_direction: str | None, struct: dict | None) -> dict:
    """-> {"ok", "code", "reason"}. Entry needs an established structure AND candle direction == structure direction."""
    if not struct or struct.get("state") != ESTABLISHED or struct.get("direction") not in (BULL, BEAR):
        return {"ok": False, "code": NO_CHOCH, "reason": (struct or {}).get("summary") or "No CHoCH yet: no entry."}
    sd = struct["direction"]
    if candle_direction not in (BULL, BEAR):
        return {"ok": False, "code": NOT_ALIGNED, "reason": "The candle has no direction yet."}
    if candle_direction != sd:
        return {"ok": False, "code": NOT_ALIGNED, "reason": f"The candle is {candle_direction} but the structure is {sd}: a counter-structure candle is never entered."}
    return {"ok": True, "code": ESTABLISHED, "reason": f"The {candle_direction} candle agrees with the {sd} structure."}


def analyse(rows, tf: str, now_server, cfg, symbol: str) -> dict:
    """Closed-candle structure of `rows` ([time, o, h, l, c]) on timeframe `tf`."""
    from trademate.strategies.base import run_analyses
    from trademate.strategy.timeframes import DataRules, prepare_series
    ps = prepare_series(rows, tf, now_server, DataRules())
    try:
        an = run_analyses({tf: ps}, cfg, symbol, (tf,))[tf]
    except Exception as exc:                                    # noqa: BLE001 - a structure failure means "no direction", never a guess
        return unavailable(f"the structure engine failed ({exc}).")
    if not an.analyzed:
        return unavailable(f"{tf} data cannot be analysed ({an.data_status}: {an.data_reason or an.bias_basis}).")
    return structure_from_events(an.events)
