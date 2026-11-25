"""Analysis view model (Phase 7A.1): what the Market Structure Engine has determined, explained from evidence.

Pure functions: candles + a StructureConfig + the health of the data feed in, a JSON-ready dict out. No Flask, no broker
calls, no orders, no entries / stops / targets. Everything shown is derived from the engine's result for the candles that
were supplied. A field the system cannot substantiate is reported as NOT AVAILABLE; nothing is invented.

THE FOUR KINDS OF STATEMENT are kept apart in `interpretation`:
    observed_facts   what the candles and swings literally are (prices, times, labels)
    rule_based       what the written rules (docs/MARKET_STRUCTURE.md) conclude from those facts
    watch            conditions that WOULD change the classification. Conditional, never a prediction or a signal
    setups           this page does not evaluate setups (the multi-timeframe Strategy Engine is a separate feature on the Strategy page)

ANALYSIS STATUS (`status.code`). Only states the system can substantiate are emitted:
    READY              analysis finished on healthy data and nothing is developing. NOT a trade signal.
    WATCH              as READY, but a break made by the still-FORMING candle exists and may be undone before it closes.
    STALE DATA         MT5's snapshot stopped updating (age above [mt5] stale_after_seconds). Nothing is analysed.
    UNAVAILABLE        no connection to MT5 / no candles were returned. Nothing is analysed.
    INVALID DATA       candles with impossible values or conflicting duplicates were received. Not analysed: excluding
                       them would silently change which candles are neighbours.
    INSUFFICIENT DATA  too few candles, or no confirmed swing high or no confirmed swing low yet.
    ANALYSING          shown by the browser while a request is in flight; the server never sends it.
    NO SETUP           RESERVED. Saying "no setup" requires a setup engine, which does not exist yet. Never emitted.
There is no READY-to-trade state, because nothing here decides trades.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from trademate.analysis_summary import build_summary, nearest
from trademate.fvg import FVGConfig, analyze_fvg
from trademate.liquidity import LiquidityConfig, analyze_liquidity, ticks_to_price
from trademate.orderblocks import OrderBlockConfig, analyze_order_blocks
from trademate.structure import StructureConfig, StructureResult, analyze_cached, structural_facts
from trademate.structure.engine import clean_candles

SUPPORTED_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")
SWINGS_SHOWN = 8
EVENTS_SHOWN = 5
OB_RELEVANT_PER_SIDE = 2    # nearest live order blocks shown per side in the plain-language summary
FVG_RELEVANT_PER_SIDE = 2   # nearest not-fully-filled gaps shown per side in the plain-language summary
FVGS_SHOWN = 10            # newest gaps listed; the rest are counted
OB_ACTIVE_SHOWN = 12       # live (candidate / confirmed) order blocks listed
OB_HISTORY_SHOWN = 8       # invalidated / expired order blocks listed

TIME_NOTE = "MT5 server time as reported by the broker (epoch seconds); not converted to your local time zone."

STATUS_MEANING = {
    "READY": "Structure analysis finished on healthy data and nothing is developing. This is not a trade signal.",
    "WATCH": "Structure analysis finished, and the still-forming candle has made a break that may be undone before it closes.",
    "STALE DATA": "MT5 stopped updating TradeMate, so nothing is analysed.",
    "UNAVAILABLE": "TradeMate has no market data for this instrument right now, so nothing is analysed.",
    "INVALID DATA": "The candles contain impossible values or conflicting duplicates, so nothing is analysed.",
    "INSUFFICIENT DATA": "There is not yet enough confirmed structure to say anything.",
    "ANALYSING": "Waiting for the analysis to finish.",
    "NO SETUP": "Reserved for a future setup engine. TradeMate does not emit it.",
}

# The modules this page knows about. Status is decided PER RESPONSE from what actually ran (see `module_list`):
#   available      the engine ran on this request's candles and produced a result (possibly "nothing found")
#   unavailable    the engine exists but could not run for this request; `reason` says why
#   not_available  this single-timeframe page does not provide it (multi-timeframe and setups live on the Strategy page, /api/strategy/mtf)
# The defaults below are only what a response says before any engine has run.
MODULES = (
    {"id": "structure", "name": "Market structure", "status": "available"},
    {"id": "liquidity", "name": "Liquidity", "status": "unavailable"},
    {"id": "fvg", "name": "Fair value gaps", "status": "unavailable"},
    {"id": "order_blocks", "name": "Order blocks", "status": "unavailable"},
    {"id": "multi_timeframe", "name": "Multi-timeframe", "status": "not_available"},
    {"id": "setups", "name": "Trade setups", "status": "not_available"},
)
ENGINE_MODULES = ("liquidity", "fvg", "order_blocks")


def module_list(payload: dict) -> list:
    """The module list for one response: an engine is `available` only if its view in THIS payload says it produced a result.
    A view that is missing or reports available=False makes the module `unavailable` with that view's reason. Nothing is
    claimed because an engine merely exists."""
    out = []
    for m in MODULES:
        m = dict(m)
        if m["id"] in ENGINE_MODULES:
            view = payload.get(m["id"])
            ok = isinstance(view, dict) and view.get("available") is True
            m["status"] = "available" if ok else "unavailable"
            m["reason"] = "" if ok else ((view or {}).get("reason") if isinstance(view, dict) else None) or "Not analysed in this response."
        out.append(m)
    return out

CAVEATS = (
    "BOS and CHoCH are structural classifications produced by explicit rules. They are not forecasts, signals or guarantees.",
    "A CHoCH is a break against the previously established direction. It does not by itself confirm a trend reversal.",
    "A BOS does not mean a trade is high-probability.",
)


def when(epoch: float | int | None) -> dict | None:
    """{'time': epoch, 'label': 'YYYY-MM-DD HH:MM'}. The label is the broker's clock, shown as read."""
    if epoch is None:
        return None
    try:
        label = datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        label = None
    return {"time": epoch, "label": label}


def _price(value: float, digits: int | None) -> str:
    return f"{value:.{digits}f}" if isinstance(digits, int) and digits >= 0 else f"{value:g}"


def _swing_view(s: dict, digits) -> dict:
    return {"id": s["id"], "kind": s["kind"], "price": s["price"], "price_text": _price(s["price"], digits),
            "classification": s["classification"], "confirmed": s["confirmed"],
            "pivot": when(s["pivot_time"]), "confirmed_at": when(s["confirmed_time"])}


def explain_event(e: dict, digits: int | None) -> dict:
    """Why this event exists, in terms of the previous level, the breaking candle, the method and the classification."""
    bull = e["direction"] == "bullish"
    swing = "swing high" if bull else "swing low"
    above = "above" if bull else "below"
    level = _price(e["level"], digits)
    price = _price(e["break_price"], digits)
    ref, cand = when(e["reference_pivot_time"]), when(e["candle_time"])
    method = ("the candle CLOSED " + above + " the level" if e["break_mode"] == "close"
              else "the candle's " + ("high" if bull else "low") + " (wick) went " + above + " the level")
    lines = [
        f"Level broken: the {swing} at {level}, formed at {ref['label']} and confirmed at "
        f"{when(e['reference_pivot_confirmed_time'])['label']} (a swing is only known after its right-side candles exist).",
        f"Breaking candle: {cand['label']}. {method[0].upper() + method[1:]} ({price}); break method: "
        f"{'candle close' if e['break_mode'] == 'close' else 'wick'}.",
    ]
    before = e["trend_before"]
    if e["type"] == "BOS":
        if before is None:
            lines.append("Classification BOS: no earlier break had established a direction, so this first break is labelled "
                         "BOS by the engine's rule (there is nothing for it to be 'against').")
        else:
            lines.append(f"Classification BOS: the direction established by the previous break was {before}, and this "
                         f"{e['direction']} break goes the same way, so it is a continuation of structure.")
    else:
        lines.append(f"Classification CHoCH: the direction established by the previous break was {before}; this "
                     f"{e['direction']} break goes against it. That is a change of character in the structure's labelling, "
                     "not proof that the market has reversed.")
    if e.get("reference_swing_id"):
        lines.append(f"Reference swing: {e['reference_swing_id']}"
                     + (f" (classified {e['reference_classification']})" if e.get("reference_classification") else " (no earlier swing to classify it against)")
                     + f"; role in the rules: {str(e.get('reference_role', '')).replace('_', ' ')}.")
    if e.get("reason"):
        lines.append(e["reason"])
    after = e.get("trend_after")
    if e.get("same_candle_conflict"):
        lines.append("Resulting structural direction: undetermined because opposing breaks occurred on the same candle.")
    else:
        lines.append(f"Resulting structural direction after this event: {after or 'undetermined'}.")
    if e["same_candle_conflict"]:
        lines.append("Conflict: the same candle broke levels in both directions. The order inside a candle is unknowable, "
                     "so the trend is left undetermined afterwards.")
    if not e["confirmed"]:
        lines.append("DEVELOPING: the breaking candle is still forming. It can still close back inside the level, in which "
                     "case this event disappears on the next refresh.")
    return {"lines": lines}


def _event_view(e: dict, digits, symbol: str, timeframe: str) -> dict:
    return {**e, "level_text": _price(e["level"], digits), "break_price_text": _price(e["break_price"], digits),
            "candle": when(e["candle_time"]), "reference_pivot": when(e["reference_pivot_time"]),
            "symbol": symbol, "timeframe": timeframe,
            "method": "candle close" if e["break_mode"] == "close" else "wick",
            "state": "confirmed" if e["confirmed"] else "developing",
            "explanation": explain_event(e, digits)}



LIQUIDITY_NOTE = ("Liquidity pools are potential liquidity areas inferred from observable swing highs and lows. TradeMate cannot see "
                  "anyone's orders, so a pool does not reveal hidden orders, and a sweep does not mean a reversal.")


def liquidity_unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason, "note": LIQUIDITY_NOTE}


def _pool_view(p: dict, digits) -> dict:
    side = "buy-side" if p["side"] == "BSL" else "sell-side"
    what = ("equal highs" if p["side"] == "BSL" else "equal lows") if p["kind"] == "equal" else ("swing high" if p["side"] == "BSL" else "swing low")
    lines = [f"Potential {side} liquidity at {_price(p['level'], digits)}, around {'the ' + str(p['member_count']) + ' ' if p['kind'] == 'equal' else 'a confirmed '}{what}.",
             f"First extreme formed at {when(p['created_time'])['label']}; it became known at {when(p['confirmed_time'])['label']} "
             "(a swing is only known after its right-side candles exist)."]
    if p["kind"] == "equal":
        lines.append(f"It was recognised as EQUAL once the second swing was confirmed at {when(p['equal_confirmed_time'])['label']}; "
                     f"the level used is the {'highest' if p['side'] == 'BSL' else 'lowest'} member price.")
    if p["state"] == "SWEPT":
        lines.append(f"Swept at {when(p['swept_time'])['label']}" + (" (provisional: the candle is still forming)." if p["provisional"] else "."))
    elif p["provisional"]:
        lines.append("Provisional: it depends on a swing confirmed by the still-forming candle.")
    return {**p, "level_text": _price(p["level"], digits), "created": when(p["created_time"]), "confirmed": when(p["confirmed_time"]),
            "kind_label": what, "explanation": {"lines": lines}}


def _sweep_view(sw: dict, pools_by_id: dict, digits) -> dict:
    bsl = sw["side"] == "BSL"
    part = "high" if bsl else "low"
    got = sw["high"] if bsl else sw["low"]
    pool = pools_by_id.get(sw["pool_id"])
    lines = [f"Pool swept: the {'buy' if bsl else 'sell'}-side level at {_price(sw['level'], digits)} ({sw['pool_kind']}, {sw['member_count']} swing(s)).",
             f"Crossing candle: {when(sw['candle_time'])['label']}. Detection mode: {sw['mode']}"
             + (f" (the candle's {part} {_price(got, digits)} went {_price(sw['penetration'], digits)} beyond the level)." if sw["mode"] == "wick"
                else f" (the candle closed at {_price(sw['close'], digits)}, beyond the level)."),
             "The same candle also closed beyond the level." if sw["close_beyond"] else "The same candle closed back inside the level (price pushed through and closed back).",
             "A sweep is an observation about price crossing a level. It is not a reversal signal."]
    if len(sw["group"]) > 1:
        lines.append(f"This candle crossed {len(sw['group'])} overlapping pools on this side; each is reported on its own.")
    if not sw["confirmed"]:
        lines.append("DEVELOPING: the crossing candle is still forming and may close back inside the level.")
    lines.extend(sw["structure"]["statements"])
    return {**sw, "level_text": _price(sw["level"], digits), "candle": when(sw["candle_time"]), "state": "confirmed" if sw["confirmed"] else "developing",
            "explanation": {"lines": lines}}


def build_liquidity_view(candles, structure: StructureResult, ctx: dict, digits) -> dict:
    """Liquidity pools and sweeps for the Analysis page. `ctx` = {settings: ChartSettings, tick_size: float|None}.
    Never raises: a failure is reported as unavailable (and can never approve or block anything)."""
    try:
        st = ctx["settings"]
        tick = ctx.get("tick_size")
        if not (isinstance(tick, (int, float)) and not isinstance(tick, bool) and tick > 0):
            return liquidity_unavailable("The symbol's tick size is not available from MT5, so the equal-high/low tolerance cannot be set. Liquidity was not analysed.")
        cfg = LiquidityConfig(tolerance=ticks_to_price(st.liquidity_tolerance_ticks, tick), sweep_mode=st.liquidity_sweep_mode,
                              max_pools_per_side=st.liquidity_max_pools, last_candle_closed=structure.config.last_candle_closed)
        res = analyze_liquidity(candles, structure, cfg)
    except ValueError as exc:                                  # bad configuration: a message the user can act on
        return liquidity_unavailable(f"Liquidity could not be analysed: {exc}")
    except Exception:                                          # noqa: BLE001 - a bug here must never break the structure analysis
        logging.getLogger("trademate.analysis").exception("Liquidity analysis crashed")
        return liquidity_unavailable("Liquidity could not be analysed because of an internal error (see the log). Structure analysis is unaffected.")
    d = res.to_dict()
    pools = [_pool_view(p, digits) for p in d["pools"]]
    by_id = {p["id"]: p for p in pools}
    sweeps = [_sweep_view(s, by_id, digits) for s in d["sweeps"]]
    active = [p for p in pools if p["state"] == "ACTIVE"]
    return {"available": True, "reason": "", "note": LIQUIDITY_NOTE, "config": {**d["config"], "tolerance_ticks": st.liquidity_tolerance_ticks, "tick_size": tick},
            "revision": d["revision"], "hidden_pools": d["hidden_pools"],
            "bsl": [p for p in active if p["side"] == "BSL"], "ssl": [p for p in active if p["side"] == "SSL"],
            "equal": [p for p in active if p["kind"] == "equal"], "swept": [p for p in pools if p["state"] == "SWEPT"],
            "sweeps": sweeps[::-1],
            "confirmed_sweeps": [s for s in sweeps[::-1] if s["confirmed"]], "provisional_sweeps": [s for s in sweeps[::-1] if not s["confirmed"]]}


def _liquidity_interpretation(liq: dict, mode: str) -> tuple[list, list, list]:
    facts, rules, watch = [], [], []
    nb, ns, ne = len(liq["bsl"]), len(liq["ssl"]), len(liq["equal"])
    facts.append(f"{nb} active buy-side and {ns} active sell-side liquidity pool(s) were inferred from confirmed swings ({ne} of them equal highs/lows).")
    confirmed, prov = liq["confirmed_sweeps"], liq["provisional_sweeps"]
    if confirmed:
        s = confirmed[0]
        facts.append(f"Latest confirmed sweep: the {'buy' if s['side'] == 'BSL' else 'sell'}-side level at {s['level_text']} was crossed on {s['candle']['label']} ({s['mode']} mode; "
                     + ("the candle closed beyond the level)." if s["close_beyond"] else "the candle closed back inside the level)."))
    else:
        facts.append("No confirmed liquidity sweep occurred in these candles.")
    for s in prov:
        facts.append(f"The forming candle currently crosses the {'buy' if s['side'] == 'BSL' else 'sell'}-side level at {s['level_text']} (unconfirmed).")
    rules.append("A sweep records that price traded beyond a level where stops may rest. By the engine's rules it does not mean a reversal, "
                 "and it does not have to be followed by a BOS or CHoCH.")
    if confirmed:
        rules.extend(confirmed[0]["structure"]["statements"])
    trade_word = "trades through" if mode == "wick" else "closes beyond"
    if liq["bsl"]:
        p = liq["bsl"][0]
        watch.append(f"If a candle {trade_word} the unswept buy-side level at {p['level_text']} ({p['kind_label']}), the rules would record a buy-side sweep.")
    if liq["ssl"]:
        p = liq["ssl"][0]
        watch.append(f"If a candle {trade_word} the unswept sell-side level at {p['level_text']} ({p['kind_label']}), the rules would record a sell-side sweep.")
    return facts, rules, watch


FVG_NOTE = ("A fair value gap is a price range left between candle 1 and candle 3 of three consecutive candles. It is an observation "
            "about those candles, not a prediction, a quality rating or a trade signal.")
ORDER_BLOCKS_NOTE = ("An order block is a candle zone selected by price structure alone (TradeMate has no volume or order-flow data). "
                     "It is an observation, not evidence of institutional activity, a prediction, a quality rating or a trade signal.")


def fvg_unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason, "note": FVG_NOTE}


def order_blocks_unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason, "note": ORDER_BLOCKS_NOTE}


FILL_LABEL = {"untouched": "Open (price has not returned)", "partial": "Partly filled", "filled": "Filled"}


def fvg_fill_status(gap: dict, bars, last_candle_forming: bool = True) -> dict:
    """How far LATER price has traded back into a gap, from the engine's own zone and the candles after candle 3 (wicks count).
    The FVG engine detects gaps; it does not track fills (docs/FAIR_VALUE_GAPS.md), so this is derived here, never stored or guessed.
      untouched  no later candle reached the gap          partial  price entered the gap but did not cross it completely
      filled     a later candle traded through the whole gap
    `provisional` = the only candle that reached this depth is the newest one, which is assumed still forming."""
    bull = gap["direction"] == "bullish"
    size = gap["upper"] - gap["lower"]
    deepest, deepest_idx, first_touch = None, None, None
    for i in range(gap["third_index"] + 1, len(bars)):
        b = bars[i]
        reach = b.low if bull else b.high
        if (bull and reach <= gap["upper"]) or (not bull and reach >= gap["lower"]):
            if first_touch is None:
                first_touch = b.time
            if deepest is None or (bull and reach < deepest) or (not bull and reach > deepest):
                deepest, deepest_idx = reach, i
    if deepest is None or size <= 0:
        return {"status": "untouched", "label": FILL_LABEL["untouched"], "percent": 0, "first_touch": None, "provisional": False}
    pct = (gap["upper"] - deepest) / size if bull else (deepest - gap["lower"]) / size
    pct = max(0.0, min(1.0, pct))
    status = "filled" if pct >= 1 else "partial"
    provisional = bool(last_candle_forming and deepest_idx == len(bars) - 1)
    return {"status": status, "label": FILL_LABEL[status], "percent": round(pct * 100), "first_touch": when(first_touch), "provisional": provisional}


def _gap_view(g: dict, digits, fill: dict | None = None) -> dict:
    up, lo, bull = _price(g["upper"], digits), _price(g["lower"], digits), g["direction"] == "bullish"
    lines = [(f"Candle 3's low ({up}) is above candle 1's high ({lo}), so price left a gap between {lo} and {up}." if bull else
              f"Candle 3's high ({lo}) is below candle 1's low ({up}), so price left a gap between {lo} and {up}."),
             f"Candle 1 {when(g['first_time'])['label']}, candle 2 (the displacement candle) {when(g['middle_time'])['label']}, "
             f"candle 3 {when(g['third_time'])['label']}. The gap became knowable when candle 3 existed.",
             "Touching boundaries do not count as a gap. The middle candle is not graded and nothing is implied about a return to the gap."]
    if not g["confirmed"]:
        lines.append("Candle 3 is the still-forming candle, so this gap may change or disappear when it closes.")
    return {"id": g["id"], "direction": g["direction"], "state": "confirmed" if g["confirmed"] else "developing", "confirmed": g["confirmed"],
            "upper": g["upper"], "lower": g["lower"], "upper_text": up, "lower_text": lo, "size_text": _price(g["size"], digits),
            "first": when(g["first_time"]), "middle": when(g["middle_time"]), "third": when(g["third_time"]),
            "fill": fill, "explanation": {"lines": lines}}


def build_fvg_view(candles, symbol: str, timeframe: str, digits) -> dict:
    """Fair value gaps for the Analysis page, straight from the FVG engine. Never raises: a failure is reported as unavailable.
    The newest candle is assumed still forming (the page's standing assumption), so a gap it completes is `developing`."""
    try:
        res = analyze_fvg(candles, FVGConfig(last_candle_closed=False), symbol=symbol or None, timeframe=timeframe)
    except ValueError as exc:
        return fvg_unavailable(f"Fair value gaps could not be analysed: {exc}")
    except Exception:                                          # noqa: BLE001 - a bug here must never break the structure analysis
        logging.getLogger("trademate.analysis").exception("FVG analysis crashed")
        return fvg_unavailable("Fair value gaps could not be analysed because of an internal error (see the log). Structure analysis is unaffected.")
    if res.data_status != "ok":
        return fvg_unavailable("Not analysed: " + (res.reason or f"the candle data was {res.data_status}."))
    d = res.to_dict()
    bars, _diag = clean_candles(candles)            # the engine's own cleaning, so third_index lines up with these bars
    fills = {g["id"]: fvg_fill_status(g, bars) for g in d["gaps"]}
    newest_first = [_gap_view(g, digits, fills[g["id"]]) for g in d["gaps"][::-1]]
    fill_counts = {k: sum(1 for f in fills.values() if f["status"] == k) for k in ("untouched", "partial", "filled")}
    last_close = bars[-1].close if bars else None
    relevant = {side: nearest([g for g in newest_first if g["direction"] == side and g["fill"]["status"] != "filled"], last_close, FVG_RELEVANT_PER_SIDE)
                for side in ("bullish", "bearish")}                  # not fully filled, nearest to the last close first
    return {"available": True, "reason": "", "note": FVG_NOTE, "revision": d["revision"], "candles_used": d["candles_used"],
            "fill_counts": fill_counts, "relevant": relevant, "last_close": last_close,
            "total": len(newest_first), "bullish": sum(1 for g in newest_first if g["direction"] == "bullish"),
            "bearish": sum(1 for g in newest_first if g["direction"] == "bearish"),
            "gaps": newest_first[:FVGS_SHOWN], "hidden": max(0, len(newest_first) - FVGS_SHOWN)}


def _block_story(b: dict, digits, mode: str) -> str:
    """One readable sentence for an order block, built from the engine's own fields with the page's human-readable times (the engine's
    own `explanation` quotes raw epoch seconds, which is right for a log but not for this page)."""
    bull = b["direction"] == "bullish"
    colour, side, ref_kind = ("bearish", "swing low", "high") if bull else ("bullish", "swing high", "low")
    level = _price(b["reference_level"], digits)
    base = (f"the last {colour} candle ({when(b['source_time'])['label']}) at or before the {side} at {when(b['origin_time'])['label']}, "
            f"after the {ref_kind} at {level}")
    status = b["status"]
    if status == "candidate":
        return (f"Candidate: {base}. Not confirmed: that level has not been broken {'above' if bull else 'below'} by a {b['direction']} "
                f"structure break from this leg.")
    if status == "confirmed":
        return (f"Confirmed: {base}, the origin of the leg whose {b['direction']} {b['confirming_event_type']} broke the {ref_kind} {level} "
                f"({when(b['confirming_event_time'])['label']}).")
    if status == "invalidated":
        how, beyond = ("traded" if mode == "wick" else "closed"), ("below" if bull else "above")
        edge = _price(b["lower"] if bull else b["upper"], digits)
        tail = (f", after it was confirmed ({when(b['confirmed_time'])['label']})." if b["confirmed_time"] is not None
                else "; it was never confirmed.")
        return f"Invalidated: price {how} {beyond} the zone's {'lower' if bull else 'upper'} boundary {edge} ({when(b['invalidated_time'])['label']}){tail}"
    reason = ("the reference level was replaced by a newer swing without being broken, or was broken ambiguously"
              if b["expired_reason"] == "REFERENCE_LOST" else
              "the reference level was broken, but not by this block's leg (a different origin, or beyond the leg limit)")
    return f"Expired: {reason}, so this candidate can no longer be confirmed ({when(b['expired_time'])['label']}). It was {base}."


def _block_view(b: dict, digits, mode: str = "close") -> dict:
    up, lo = _price(b["upper"], digits), _price(b["lower"], digits)
    lines = [_block_story(b, digits, mode),
             f"Zone {lo} to {up} (the source candle's full range); known from {when(b['identified_time'])['label']}. "
             f"Rule: {b['rule']} (see docs/ORDER_BLOCKS.md)."]
    if b["provisional"]:
        lines.append("Its current status rests on the still-forming candle, so it may change or disappear when that candle closes.")
    live = b["status"] in ("candidate", "confirmed")
    return {"id": b["id"], "direction": b["direction"], "status": b["status"], "live": live, "provisional": b["provisional"],
            "state": "confirmed" if b["status"] == "confirmed" and not b["provisional"] else "developing" if live else b["status"],
            "upper": b["upper"], "lower": b["lower"], "upper_text": up, "lower_text": lo,
            "source": when(b["source_time"]), "identified": when(b["identified_time"]), "confirmed_at": when(b["confirmed_time"]),
            "invalidated": when(b["invalidated_time"]), "expired": when(b["expired_time"]), "expired_reason": b["expired_reason"],
            "confirming_event": ({"type": b["confirming_event_type"], "candle": when(b["confirming_event_time"])}
                                 if b["confirming_event_time"] is not None else None),
            "explanation": {"lines": lines}}


def build_order_blocks_view(candles, structure: StructureResult, symbol: str, timeframe: str, digits) -> dict:
    """Order blocks for the Analysis page, from the order block engine and the structure result of the SAME candles.
    Never raises: a failure is reported as unavailable."""
    try:
        res = analyze_order_blocks(candles, structure, OrderBlockConfig(), symbol=symbol or None, timeframe=timeframe)
    except ValueError as exc:
        return order_blocks_unavailable(f"Order blocks could not be analysed: {exc}")
    except Exception:                                          # noqa: BLE001
        logging.getLogger("trademate.analysis").exception("Order block analysis crashed")
        return order_blocks_unavailable("Order blocks could not be analysed because of an internal error (see the log). Structure analysis is unaffected.")
    if res.data_status != "ok":
        return order_blocks_unavailable("Not analysed: " + (res.reason or f"the candle data was {res.data_status}."))
    d = res.to_dict()
    views = [_block_view(b, digits, d["config"]["invalidation_mode"]) for b in d["blocks"][::-1]]                   # newest source candle first
    live = [b for b in views if b["live"]]
    history = [b for b in views if not b["live"]]
    count = lambda st: sum(1 for b in views if b["status"] == st)               # noqa: E731
    bars, _diag = clean_candles(candles)
    last_close = bars[-1].close if bars else None
    relevant = {side: nearest([b for b in live if b["direction"] == side], last_close, OB_RELEVANT_PER_SIDE) for side in ("bullish", "bearish")}
    return {"available": True, "reason": "", "note": ORDER_BLOCKS_NOTE, "revision": d["revision"], "candles_used": d["candles_used"],
            "config": d["config"], "total": len(views), "relevant": relevant, "last_close": last_close,
            "counts": {"candidate": count("candidate"), "confirmed": count("confirmed"), "invalidated": count("invalidated"), "expired": count("expired")},
            "live": live[:OB_ACTIVE_SHOWN], "live_hidden": max(0, len(live) - OB_ACTIVE_SHOWN),
            "history": history[:OB_HISTORY_SHOWN], "history_hidden": max(0, len(history) - OB_HISTORY_SHOWN)}


def _status(code: str, reason: str) -> dict:
    return {"code": code, "reason": reason, "meaning": STATUS_MEANING[code], "is_trade_signal": False}


def _empty(symbol, timeframe, cfg, code, reason, *, freshness="unavailable", extra_warnings=()) -> dict:
    out = {"ok": True, "symbol": symbol, "timeframe": timeframe, "supported_timeframes": list(SUPPORTED_TIMEFRAMES),
            "status": _status(code, reason),
            "context": {"candles_used": 0, "latest_candle": None, "time_note": TIME_NOTE,
                        "data": {"freshness": freshness, "freshness_note": reason, "candle_clock": "not_verifiable",
                                 "candle_clock_note": CANDLE_CLOCK_NOTE, "completeness": "unavailable",
                                 "warnings": list(extra_warnings)}},
            "structure": None, "interpretation": None, "config": cfg.to_dict() if cfg else None,
            "caveats": list(CAVEATS), "liquidity": liquidity_unavailable("Not analysed: " + reason),
            "fvg": fvg_unavailable("Not analysed: " + reason), "order_blocks": order_blocks_unavailable("Not analysed: " + reason)}
    out["modules"] = module_list(out)
    return out


CANDLE_CLOCK_NOTE = ("The MT5 bridge does not export a broker clock directly. When a fresh M1 measurement is unavailable, "
                     "the newest candle is conservatively treated as forming.")


def unavailable(symbol: str, timeframe: str, cfg: StructureConfig | None, message: str, stale: bool = False) -> dict:
    """Feed problem: MT5 not connected (UNAVAILABLE) or its snapshot stopped updating (STALE DATA)."""
    return _empty(symbol, timeframe, cfg, "STALE DATA" if stale else "UNAVAILABLE", message,
                  freshness="stale_feed" if stale else "unavailable")


def build_analysis(symbol: str, timeframe: str, candles: Iterable[Any], cfg: StructureConfig, digits: int | None = None,
                   feed_warnings: Iterable[str] = (), liquidity_ctx: dict | None = None,
                   candle_clock: dict | None = None) -> dict:
    rows = [list(c) for c in candles or []]
    if not rows:
        return _empty(symbol, timeframe, cfg, "UNAVAILABLE", "MT5 returned no candles for this symbol and timeframe.")
    res: StructureResult = analyze_cached(rows, cfg)
    d = res.to_dict()
    diag = d["diagnostics"]
    warnings = list(feed_warnings) + list(diag["warnings"])
    latest = when(max((r[0] for r in rows if isinstance(r[0], (int, float))), default=None))
    clock_note = (candle_clock or {}).get("note") or CANDLE_CLOCK_NOTE
    clock_state = "verified" if (candle_clock or {}).get("known") else "not_verifiable"
    data = {"freshness": "live_feed", "freshness_note": "TradeMate is receiving live data from MT5.",
            "candle_clock": clock_state, "candle_clock_note": clock_note, "completeness": "complete",
            "warnings": warnings}
    context = {"candles_used": diag["used_count"], "candles_received": diag["input_count"],
               "latest_candle": {**(latest or {}), "state": "closed" if cfg.last_candle_closed else "forming",
                                 "state_note": clock_note},
               "structure_input": {"symbol": symbol, "timeframe": timeframe, "last_candle_closed": cfg.last_candle_closed,
                                   "swing_left": cfg.swing_left, "swing_right": cfg.swing_right,
                                   "break_mode": cfg.break_mode, "equal_tolerance": cfg.equal_tolerance},
               "time_note": TIME_NOTE, "data": data}
    base = {"ok": True, "symbol": symbol, "timeframe": timeframe, "supported_timeframes": list(SUPPORTED_TIMEFRAMES),
            "context": context, "config": cfg.to_dict(), "caveats": list(CAVEATS),
            "liquidity": liquidity_unavailable("Not analysed: the market-structure analysis it depends on did not complete."),
            "order_blocks": order_blocks_unavailable("Not analysed: the market-structure analysis it depends on did not complete."),
            # fair value gaps need only candles, so they are analysed (or honestly refused) whatever structure can say
            "fvg": build_fvg_view(rows, symbol, timeframe, digits)}

    bars_all, _d = clean_candles(rows)
    last_close = bars_all[-1].close if bars_all else None
    bar_times = [b.time for b in bars_all]

    def finish(payload: dict) -> dict:                          # module status is decided from what THIS response actually contains
        payload["modules"] = module_list(payload)
        payload["summary"] = build_summary(payload, last_close, bar_times)      # plain-language view of the same results
        return payload

    # ---- data-quality gates: nothing below is shown unless the data can support it ----
    if diag["invalid"] or diag["duplicates_conflicting"]:
        data["completeness"] = "invalid"
        n = len(diag["invalid"])
        why = (f"{n} candle(s) had impossible values" if n else "") + ("; " if n and diag["duplicates_conflicting"] else "") + \
              (f"{diag['duplicates_conflicting']} duplicate timestamp(s) had conflicting prices" if diag["duplicates_conflicting"] else "")
        return finish({**base, "status": _status("INVALID DATA", why + ". Nothing was analysed."), "structure": None, "interpretation": None})
    if diag["insufficient_data"]:
        data["completeness"] = "insufficient"
        return finish({**base, "status": _status("INSUFFICIENT DATA", f"Need at least {cfg.min_candles} candles, got {diag['used_count']}."),
                       "structure": None, "interpretation": None})
    base["order_blocks"] = build_order_blocks_view(rows, res, symbol, timeframe, digits)     # structure ran on healthy data
    swings = [s for s in d["swings"]]
    highs, lows = [s for s in swings if s["kind"] == "high"], [s for s in swings if s["kind"] == "low"]
    if not highs or not lows:
        data["completeness"] = "insufficient"
        missing = "swing high" if not highs else "swing low"
        return finish({**base, "status": _status("INSUFFICIENT DATA", f"No confirmed {missing} exists yet in these candles."),
                       "structure": {"swings": [_swing_view(s, digits) for s in swings[-SWINGS_SHOWN:]], "events": [], "latest_event": None},
                       "interpretation": None})

    events = [_event_view(e, digits, symbol, timeframe) for e in d["events"]]
    developing = [e for e in events if not e["confirmed"]]
    settled = [e for e in events if e["confirmed"]]
    trend_c = d["trend_confirmed"]
    structure = {
        "trend": {"confirmed": trend_c or "undetermined", "including_developing": d["trend"] or "undetermined",
                  "note": ("'undetermined' means no break has established a direction yet (or a same-candle double break left "
                           "it open). It is not 'sideways'.")},
        "swings": [_swing_view(s, digits) for s in swings[-SWINGS_SHOWN:]],
        "active_levels": {"high": _swing_view(d["active_high"], digits) if d["active_high"] else None,
                          "low": _swing_view(d["active_low"], digits) if d["active_low"] else None},
        "protected_levels": {"high": _swing_view(d["protected_high"], digits) if d["protected_high"] else None,
                             "low": _swing_view(d["protected_low"], digits) if d["protected_low"] else None},
        "continuation_levels": {"high": _swing_view(d["continuation_high"], digits) if d["continuation_high"] else None,
                                "low": _swing_view(d["continuation_low"], digits) if d["continuation_low"] else None},
        "structural_state": {"confirmed_direction": d["trend_confirmed"], "including_developing": d["trend"],
                             "last_confirmed_event_id": settled[-1]["id"] if settled else None},
        "latest_confirmed_event": settled[-1] if settled else None,
        "developing_event": developing[-1] if developing else None,
        "latest_event": events[-1] if events else None,
        "recent_events": events[-EVENTS_SHOWN:][::-1],
        "break_mode": cfg.break_mode,
        "facts": structural_facts(res),          # the same facts the Strategy page uses (trademate/structure/facts.py)
    }

    # ---- interpretation: facts / rules / watch / setups, kept apart ----
    facts, rules, watch = [], [], []
    facts.append(f"{len(swings)} swing point(s) confirmed in {diag['used_count']} candles "
                 f"(pivot width {cfg.swing_left} left / {cfg.swing_right} right; break method: "
                 f"{'candle close' if cfg.break_mode == 'close' else 'wick'}).")
    last_high = [s["classification"] for s in highs[-2:] if s["classification"]]
    last_low = [s["classification"] for s in lows[-2:] if s["classification"]]
    if last_high or last_low:
        facts.append("Latest swing labels: highs " + (", ".join(last_high) or "none yet") + "; lows " + (", ".join(last_low) or "none yet") + ".")
    if settled:
        e = settled[-1]
        facts.append(f"Latest confirmed event: {e['type']} ({e['direction']}) at {e['level_text']} on {e['candle']['label']}.")
    else:
        facts.append("No break of a confirmed swing has occurred in these candles.")
    if developing:
        e = developing[-1]
        facts.append(f"The forming candle currently shows a {e['direction']} {e['type']} through {e['level_text']} (unconfirmed).")

    if trend_c == "bullish":
        rules.append("By the engine's rules the last confirmed break was bullish, so structure currently reads bullish.")
    elif trend_c == "bearish":
        rules.append("By the engine's rules the last confirmed break was bearish, so structure currently reads bearish.")
    else:
        rules.append("No confirmed break has established a direction, so structure is undetermined.")
    if settled and settled[-1]["type"] == "CHOCH":
        rules.append("The latest confirmed break was a CHoCH: it went against the previously established direction. "
                     "That changes the structural label; it does not confirm a reversal.")
    elif settled and settled[-1]["type"] == "BOS" and settled[-1]["trend_before"]:
        rules.append("The latest confirmed break was a BOS: it continued the previously established direction.")
    if last_high and last_low:
        rules.append("Swing labels describe the pattern of highs and lows (e.g. HH + HL = higher highs and higher lows). "
                     "They describe what happened; they do not predict what comes next.")

    ah, al = structure["active_levels"]["high"], structure["active_levels"]["low"]
    ch, cl = structure["continuation_levels"]["high"], structure["continuation_levels"]["low"]
    ph, pl = structure["protected_levels"]["high"], structure["protected_levels"]["low"]
    verb = "closes" if cfg.break_mode == "close" else "wicks"
    if trend_c == "bullish":
        if ch:
            watch.append(f"If a candle {verb} above confirmed continuation swing high {ch['price_text']}, it would be a bullish BOS.")
        if pl:
            watch.append(f"If a candle {verb} below protected higher low {pl['price_text']}, it would be a bearish CHoCH.")
        else:
            watch.append("If no confirmed protected higher low exists, a bearish CHoCH cannot currently be classified.")
    elif trend_c == "bearish":
        if cl:
            watch.append(f"If a candle {verb} below confirmed continuation swing low {cl['price_text']}, it would be a bearish BOS.")
        if ph:
            watch.append(f"If a candle {verb} above protected lower high {ph['price_text']}, it would be a bullish CHoCH.")
        else:
            watch.append("If no confirmed protected lower high exists, a bullish CHoCH cannot currently be classified.")
    else:
        if ah:
            watch.append(f"If a candle {verb} above swing high {ah['price_text']} first, direction would bootstrap bullish as a BOS.")
        if al:
            watch.append(f"If a candle {verb} below swing low {al['price_text']} first, direction would bootstrap bearish as a BOS.")
    if developing:
        watch.append("The forming candle must close beyond the broken level for the developing event to become confirmed.")
    watch.append("These are conditions that would change the classification. They are not trade signals and do not say "
                 "which way price will go.")
    interpretation = {
        "observed_facts": facts, "rule_based": rules, "watch": watch,
        "setups": {"supported": False, "status": "NOT AVAILABLE",
                   "note": "This page analyses one timeframe and does not evaluate trade setups. Multi-timeframe setup "
                           "evaluation is on the Strategy page. Nothing on this page is a trade signal."},
    }
    if developing:
        status = _status("WATCH", f"The forming candle shows an unconfirmed {developing[-1]['type']} ({developing[-1]['direction']}).")
    else:
        status = _status("READY", "Analysis complete on healthy data. Nothing is developing.")
    if liquidity_ctx is not None:
        liq = build_liquidity_view(rows, res, liquidity_ctx, digits)
        base["liquidity"] = liq
        if liq["available"]:
            f2, r2, w2 = _liquidity_interpretation(liq, liquidity_ctx["settings"].liquidity_sweep_mode)
            interpretation["observed_facts"] += f2
            interpretation["rule_based"] += r2
            interpretation["watch"] = interpretation["watch"][:-1] + w2 + interpretation["watch"][-1:]     # the closing disclaimer stays last
    return finish({**base, "status": status, "structure": structure, "interpretation": interpretation})
