"""The Anti-Twin TRIGGER RULE, shared by Candle Momentum, Bull/Bear and Rapid Fire.   (docs/ANTI_TWIN.md)

Anti-Twin is a direction switch, not a stop-loss and not a profit-triggered reversal. It has ONE active rule and two settings (ON / OFF):

    ADVERSE MOVE (the active mode)
        1. A position opens. Its ACTUAL MT5 fill price is its entry reference E (never reused by the next position).
        2. The very first CONFIRMED adverse movement of its executable exit price closes it, the closure is confirmed, and an equal-volume opposite
           position is opened. The new position is watched the same way, immediately, so BUY -> SELL -> BUY -> SELL can continue.

QUOTE CONVENTIONS (so an ordinary spread is never mistaken for movement)
    A BUY fills at the ASK and exits at the BID. A SELL fills at the BID and exits at the ASK. Everything below is therefore measured on the
    position's EXECUTABLE EXIT price x (bid for a BUY, ask for a SELL):

        d = sign * (x - E)        sign = +1 for a BUY, -1 for a SELL.   d > 0 is profit, d = 0 is break-even (spread included), d < 0 is a loss.

    The moment a position opens, d is about -spread: the spread has already been paid. That is NOT an adverse move. So:

        ADVERSE MOVE   d <= -spread0 - one tick     (the exit price is at least one tick WORSE than where it started; `spread0` = the spread seen when
                                                     the position was first observed). One tick is the smallest price change that exists on the
                                                     instrument: it is the quantum of "moved", not a profit or loss threshold.

    No profit threshold, no minimum favourable move, no loss threshold, no candle close, no waiting for the original take-profit and no waiting for
    price to come back to the entry. It is evaluated on every quote it is given. A gap that jumps far beyond the threshold triggers exactly like one tick.

TRIGGER MODES ARE EXPLICIT
    ADVERSE_MOVE       the ACTIVE mode, used by every strategy (see ACTIVE_MODE).
    BREAK_EVEN_RETURN  the PREVIOUS rule (v5.0/5.1): leave the entry, then return to / cross it. It is kept only as an explicit, separate, non-default mode
                       so its behaviour is documented and tested; no setting, route or engine selects it, and the two are never combined: observe() runs
                       the ONE mode written into the state.

The functions are pure and the state is a plain dict, so every strategy can persist it. Nothing here sends an order.
"""

from __future__ import annotations

import math

EPS = 1e-12
MAX_CHAIN = 20          # fixed safety ceiling: reversals in one chain (original -> twin -> twin ...). Not a setting. The Risk Engine's own limits apply to every twin.

ADVERSE_MOVE = "adverse_move"
BREAK_EVEN_RETURN = "break_even_return"
MODES = (ADVERSE_MOVE, BREAK_EVEN_RETURN)
ACTIVE_MODE = ADVERSE_MOVE


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def new_state(side: str, entry: float, mode: str = ACTIVE_MODE) -> dict:
    """Start watching one position. `entry` must be the broker-confirmed fill price. The mode is fixed for the life of the state."""
    if mode not in MODES:
        raise ValueError(f"Unknown Anti-Twin trigger mode {mode!r}.")
    return {"mode": mode, "side": side, "entry": float(entry), "sign": 1 if side == "buy" else -1, "spread0": None, "up": False, "down": False,
            "lo": None, "hi": None, "triggered": False}


def exit_price(state: dict, bid, ask):
    """The price this position would be closed at right now: the bid for a BUY, the ask for a SELL (None if the quote is unusable)."""
    x = bid if state["side"] == "buy" else ask
    return x if _num(x) and x > 0 else None


def displacement(state: dict, bid, ask):
    """d = sign * (exit price - entry): positive in profit, 0 at break-even, negative in loss. None if the quote is unusable."""
    x = exit_price(state, bid, ask)
    return None if x is None else state["sign"] * (x - state["entry"])


def observe(state: dict, bid, ask, tick_size) -> bool:
    """Feed one quote. Returns True exactly when the active mode's trigger condition is met. Updates `state` in place.
    A state that has already triggered never triggers again (one reversal per position).

    A state written by an older version carries no mode: it is normalised to the ACTIVE mode, so a position that was already being watched follows the
    current specification instead of the retired one."""
    if state.get("triggered"):
        return False
    if state.get("mode") not in MODES:
        state["mode"] = ACTIVE_MODE
    d = displacement(state, bid, ask)
    if d is None or not (_num(bid) and _num(ask) and ask >= bid):
        return False                                     # a crossed or missing quote proves nothing
    if state["spread0"] is None:
        state["spread0"] = float(ask - bid)              # the spread paid at the open: where "no movement yet" sits
    state["lo"] = d if state["lo"] is None else min(state["lo"], d)
    state["hi"] = d if state["hi"] is None else max(state["hi"], d)
    if d > EPS:
        state["up"] = True
    adverse = _num(tick_size) and tick_size > 0 and d <= -state["spread0"] - tick_size + EPS   # without a valid tick size an adverse move cannot be told from spread noise
    if adverse:
        state["down"] = True
    if state["mode"] == ADVERSE_MOVE:
        fire = adverse
    else:                                                # BREAK_EVEN_RETURN: left the entry, now back at / across it
        fire = (state["up"] and d <= EPS) or (state["down"] and d >= -EPS)
    if fire:
        state["triggered"] = True
        state["trigger"] = {"d": d, "bid": bid, "ask": ask, "exit": exit_price(state, bid, ask)}
        return True
    return False


def describe(state: dict) -> str:
    if state.get("mode", ACTIVE_MODE) == BREAK_EVEN_RETURN:
        if state.get("triggered"):
            return "Returned to its entry price."
        if state["up"]:
            return "Left its entry price in profit; waiting for the return to the entry price."
        if state["down"]:
            return "Left its entry price against the position; waiting for the return to the entry price."
        return "Watching: the position has not left its entry price yet."
    if state.get("triggered"):
        return "Adverse move detected: switching direction."
    return "Watching every quote: the first adverse move (one tick worse than where the position started) switches direction."


# ------------------------------------------------------------------------------------------------------------------ measured latency
# Every reversal is timed with a monotonic clock at its stages. These helpers only do arithmetic on the stamps; they never claim a number that was not measured.
# Stages: detect (the adverse move was seen) -> close_sent -> close_done (the executor confirmed the close) -> open_sent -> open_done (the twin is filled and confirmed).
# `quote_age` is how old the bridge snapshot behind the detecting quote was when it was read: the part of the delay that happens BEFORE TradeMate can see the move.

def latency_record(lat: dict, *, side: str, entry, outcome: str, leg=None, ticket=None, symbol=None) -> dict:
    def ms(a, b):
        return None if lat.get(a) is None or lat.get(b) is None else round((lat[b] - lat[a]) * 1000.0, 1)
    trig = lat.get("trigger") or {}
    sign = 1 if side == "buy" else -1
    close_px = lat.get("close_price")
    return {"leg": leg, "ticket": ticket, "symbol": symbol, "side": side, "outcome": outcome,
            "quote_age_ms": None if lat.get("quote_age") is None else round(lat["quote_age"] * 1000.0, 1),
            "detect_to_close_ms": ms("detect", "close_done"),            # adverse move seen -> original closed and confirmed by the executor
            "close_roundtrip_ms": ms("close_sent", "close_done"),
            "close_to_open_sent_ms": ms("close_done", "open_sent"),      # the gap between the two orders (account re-read, fresh quote, sizing)
            "open_roundtrip_ms": ms("open_sent", "open_done"),           # Risk gate + order + fill confirmation
            "detect_to_open_ms": ms("detect", "open_done"),              # the whole switch
            "t_detect": lat.get("detect"), "t_open_done": lat.get("open_done"),   # absolute monotonic stamps (time.perf_counter): lets a harness measure from the instant the price moved
            "exit_at_trigger": trig.get("exit"), "close_fill_price": close_px,
            # the REAL result of the close in price units (negative = a loss, spread and slippage included). Never reported as break-even.
            "closed_vs_entry": None if not (_num(close_px) and _num(entry)) else round(sign * (close_px - entry), 10)}


def latency_summary(records) -> dict:
    done = [r for r in records if r.get("detect_to_open_ms") is not None]

    def stat(key):
        v = sorted(r[key] for r in done if r.get(key) is not None)
        return None if not v else {"median": round(v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2, 1), "max": round(v[-1], 1), "min": round(v[0], 1)}
    ages = sorted(r["quote_age_ms"] for r in done if r.get("quote_age_ms") is not None)
    note = None
    if ages and ages[len(ages) // 2] > 400:
        note = ("Most of the delay is the age of the quote when the move was seen: the MT5 bridge writes its snapshot rarely. "
                "In the TradeMateBridge EA inputs, set UpdateIntervalMs to 250 or lower and CommandPollMs to 100.")
    return {"count": len(done), "detect_to_open_ms": stat("detect_to_open_ms"), "detect_to_close_ms": stat("detect_to_close_ms"),
            "open_roundtrip_ms": stat("open_roundtrip_ms"), "quote_age_ms": stat("quote_age_ms"), "last": dict(records[-1]) if records else None, "note": note}
