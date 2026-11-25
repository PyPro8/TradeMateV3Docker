"""Plain-language summary of the Analysis page (Task 2, Objective A).

This module only RE-PRESENTS results the existing engines already produced (market structure, liquidity, fair value gaps,
order blocks). It never runs an engine, never reads a broker and never invents a zone, level or event: every number and every
sentence below is built from fields that are already in the analysis payload (plus the last candle close, used only to say
whether price is inside / above / below a zone).

Rules used (also documented in docs/ANALYSIS.md):

DIRECTION  - 'unclear'  : no confirmed break has set a direction, or the structure analysis did not run.
           - 'mixed'    : the evidence disagrees with itself: a forming candle breaks the other way, the swing pattern opposes the
                          last break, or the last break was a CHoCH that the swing pattern does not yet follow.
           - 'bullish' / 'bearish' : the last confirmed break points that way and nothing in the structure opposes it.
           `confirmed_direction` always reports the engine's last closed-candle direction separately from evidence quality.
           A CHoCH changes the engine's direction; it is NEVER presented as a confirmed trading reversal.
SWEEP EVIDENCE - only a structure break AFTER the sweep counts: a CHoCH in the direction opposite to the sweep = 'reversal'
           evidence; any other break after it = 'continuation' evidence; none = 'none yet'. Closing back inside the level is
           mentioned but is not evidence on its own.
OVERALL    - structure direction + the sweep follow-through + whether price currently sits inside a confirmed zone. Zones that merely
           exist above/below price are listed as areas to watch, not as direction. Opposing evidence is always reported.
"""
from __future__ import annotations

import logging

EVIDENCE_NOTE = "This is a plain-language reading of what the engines found. It is not advice, a prediction or a trade signal."
NEAREST_PER_SIDE = 2                  # zones shown per side in the plain-language sections
SWEEPS_SHOWN = 3
EVENTS_SHOWN = 3


def _opposite(direction: str) -> str:
    return "bearish" if direction == "bullish" else "bullish"


def location_of(lower: float, upper: float, price: float | None) -> str | None:
    """Where the last close is relative to a zone: 'inside' | 'above' | 'below' (the price is above/below the zone). None = unknown."""
    if price is None:
        return None
    if lower <= price <= upper:
        return "inside"
    return "above" if price > upper else "below"


def location_text(loc: str | None) -> str:
    return {"inside": "Price is currently inside this zone.", "above": "Price is currently above this zone.",
            "below": "Price is currently below this zone.", None: "Where price is relative to this zone is unknown."}[loc]


def nearest(zones: list, price: float | None, n: int) -> list:
    """The `n` zones closest to `price` (0 away while price is inside). Without a price the engine's own order (newest first) is kept."""
    if price is None:
        return list(zones[:n])

    def gap(z):
        return 0.0 if z["lower"] <= price <= z["upper"] else min(abs(z["lower"] - price), abs(z["upper"] - price))
    return sorted(zones, key=gap)[:n]


# ---------------------------------------------------------------------------------------------------- direction
_EVENT_MEANING = {
    "BOS": "Price broke a swing {side} in the same direction as the existing trend (a Break of Structure). "
           "That points to the trend continuing, but it is an observation, not a signal.",
    "BOS_FIRST": "The first confirmed break in these candles. It sets the starting direction; there was no earlier trend to continue.",
    "CHOCH": "Price broke a swing {side} against the previous {before} direction (a Change of Character). It is an early sign "
             "the direction may be changing, not a confirmed reversal: price can still resume the earlier direction.",
}


def _event_line(e: dict) -> dict:
    side = "high" if e["direction"] == "bullish" else "low"
    before = e.get("trend_before")
    if e["type"] == "CHOCH":
        meaning = _EVENT_MEANING["CHOCH"].format(side=side, before=before or "earlier")
    elif before:
        meaning = _EVENT_MEANING["BOS"].format(side=side)
    else:
        meaning = _EVENT_MEANING["BOS_FIRST"]
    if not e["confirmed"]:
        meaning = "Still forming: the candle has not closed, so this may not hold. " + meaning
    name = "Change of Character (CHoCH)" if e["type"] == "CHOCH" else "Break of Structure (BOS)"
    return {"title": f"{e['direction'].capitalize()} {name}", "type": e["type"], "direction": e["direction"],
            "level_text": e["level_text"], "when": e["candle"]["label"] if e.get("candle") else None,
            "confirmed": e["confirmed"], "meaning": meaning}


def _last_class(swings: list, kind: str) -> str | None:
    for s in reversed(swings):
        if s["kind"] == kind and s.get("classification"):
            return s["classification"]
    return None


def direction_section(payload: dict) -> dict:
    """Direction and quality come from `structure.facts` (trademate/structure/facts.py), the same facts the Strategy page uses.
    The headline `label` is the CONFIRMED direction; evidence quality is reported separately in `quality`. A forming-candle break is
    reported as provisional and never changes either."""
    st = payload.get("structure")
    if not st or "trend" not in st or "facts" not in st:   # not analysed, or too few swings for a trend (the status says why)
        reason = (payload.get("status") or {}).get("reason") or "Market structure was not analysed."
        return {"available": False, "label": "unclear", "headline": "Unclear", "confirmed_direction": None,
                "quality": "unavailable", "reason": reason, "context": [], "events": [], "caveat": None, "provisional": None}
    facts = st["facts"]
    trend, quality = facts["confirmed_direction"], facts["quality"]
    hi, lo = facts["swing_pattern"]["high"], facts["swing_pattern"]["low"]
    latest = st.get("latest_confirmed_event")
    dev = st.get("developing_event")
    context = []
    if latest:
        context.append(f"Last confirmed break: {latest['direction']} {latest['type'] if latest['type'] == 'BOS' else 'CHoCH'} "
                       f"through {latest['level_text']} on {latest['candle']['label']}.")
    else:
        context.append("No swing has been broken yet in these candles.")
    if hi or lo:
        names = {"HH": "higher high", "LH": "lower high", "EQH": "equal high", "HL": "higher low", "LL": "lower low", "EQL": "equal low"}
        context.append("Latest swing pattern: " + ", ".join(x for x in (
            f"high = {names.get(hi, hi)}" if hi else None, f"low = {names.get(lo, lo)}" if lo else None) if x) + ".")
    caveat, reason = None, ""
    if trend is None:
        label, quality = "unclear", "unavailable"
        reason = "No confirmed break has set a direction yet, so TradeMate cannot call one."
    else:
        label = trend
        if quality == "early":
            reason = f"The last confirmed break was a {trend} CHoCH with no follow-through BOS yet."
            caveat = ("The structure changed direction recently (a CHoCH). The swing pattern may not have followed yet, and a CHoCH alone is "
                      "not proof of a reversal.")
        elif quality == "mixed":
            reason = f"The last confirmed break was a {trend} BOS, but the latest swings point the other way."
        elif quality == "aligned":
            reason = f"The last confirmed break was {trend} and the swing pattern agrees."
        else:
            reason = f"The last confirmed break was {trend}."
    if dev:
        against = trend is not None and dev["direction"] != trend
        context.append(f"PROVISIONAL: a {dev['direction']} {dev['type']} is forming on the newest candle"
                       + (f", against the confirmed {trend} direction" if against else "")
                       + ". It is unconfirmed, may vanish when the candle closes, and does not change the confirmed direction or quality above.")
    events = [_event_line(e) for e in (st.get("recent_events") or [])[:EVENTS_SHOWN]]
    return {"available": True, "label": label, "headline": label.capitalize(), "confirmed_direction": trend, "quality": quality,
            "reason": reason, "context": context, "events": events, "caveat": caveat,
            "provisional": ({"type": dev["type"], "direction": dev["direction"]} if dev else None)}


# ---------------------------------------------------------------------------------------------------- liquidity
def sweep_evidence(sweep: dict) -> dict:
    """{'verdict': 'reversal'|'continuation'|'none'|'unclear', 'direction': 'bullish'|'bearish'|None, 'text': str}"""
    struct = sweep.get("structure") or {}
    after, same = struct.get("after"), struct.get("same_candle") or []
    bsl = sweep["side"] == "BSL"
    push = "bullish" if bsl else "bearish"                       # the way price pushed through the level
    if after:
        if after["type"] == "CHOCH" and after["direction"] != push:
            return {"verdict": "reversal", "direction": after["direction"],
                    "text": f"A {after['direction']} CHoCH followed this sweep. That is evidence for a possible reversal, not proof of one."}
        same_way = "went the same way as the sweep" if after["direction"] == push else "continued the existing trend"
        return {"verdict": "continuation", "direction": after["direction"],
                "text": f"A {after['direction']} {after['type']} followed this sweep and {same_way}. That is evidence for continuation, not reversal."}
    if same:
        return {"verdict": "unclear", "direction": None,
                "text": "A structure break happened on the same candle, so the order of events cannot be known."}
    text = "No structure break has followed yet, so the sweep alone does not say which way price will go."
    if not sweep.get("close_beyond"):
        text += " Price closed back inside the level on the sweep candle, which is consistent with a rejection but is not confirmed."
    return {"verdict": "none", "direction": None, "text": text}


def liquidity_section(payload: dict, price: float | None, bar_times: list) -> dict:
    liq = payload.get("liquidity") or {}
    if not liq.get("available"):
        return {"available": False, "reason": liq.get("reason") or "Liquidity was not analysed.", "levels": [], "sweeps": [],
                "evidence": None, "headline": None}
    levels = []
    for key, label in (("bsl", "Buy-side"), ("ssl", "Sell-side")):
        pools = liq.get(key) or []
        for p in (nearest([{**x, "lower": x["level"], "upper": x["level"]} for x in pools], price, NEAREST_PER_SIDE) if pools else []):
            where = None if price is None else ("above" if p["level"] > price else "below" if p["level"] < price else "at")
            levels.append({"side": label, "price_text": p["level_text"], "what": p["kind_label"], "where": where,
                           "provisional": bool(p.get("provisional"))})
    sweeps = []
    for sw in (liq.get("confirmed_sweeps") or [])[:SWEEPS_SHOWN]:
        ev = sweep_evidence(sw)
        ago = sum(1 for t in bar_times if t > sw["candle_time"])
        sweeps.append({"title": f"{'Buy-side' if sw['side'] == 'BSL' else 'Sell-side'} liquidity swept at {sw['level_text']}",
                       "when": sw["candle"]["label"] if sw.get("candle") else None, "candles_ago": ago,
                       "closed_back_inside": not sw.get("close_beyond"), **ev})
    provisional = [{"title": f"{'Buy-side' if s['side'] == 'BSL' else 'Sell-side'} sweep forming at {s['level_text']}"}
                   for s in (liq.get("provisional_sweeps") or [])[:2]]
    if sweeps:
        newest = sweeps[0]
        headline = {"reversal": "Evidence from the newest sweep supports a possible reversal.",
                    "continuation": "Evidence from the newest sweep supports continuation.",
                    "none": "The newest sweep has no structural follow-through yet.",
                    "unclear": "The newest sweep cannot be read: its order against a structure break is unknowable."}[newest["verdict"]]
    else:
        headline = "No liquidity sweep has been detected in these candles."
    return {"available": True, "reason": "", "levels": levels, "sweeps": sweeps, "provisional": provisional,
            "headline": headline, "evidence": sweeps[0] if sweeps else None}


# ---------------------------------------------------------------------------------------------------- zones
def _range_text(z: dict) -> str:
    return f"{z['lower_text']} – {z['upper_text']}"


def fvg_section(payload: dict, price: float | None) -> dict:
    fvg = payload.get("fvg") or {}
    if not fvg.get("available"):
        return {"available": False, "reason": fvg.get("reason") or "Fair value gaps were not analysed.", "bullish": [], "bearish": []}
    rel = fvg.get("relevant") or {}

    def item(g):
        loc = location_of(g["lower"], g["upper"], price)
        return {"id": g["id"], "direction": g["direction"], "range_text": _range_text(g), "lower": g["lower"], "upper": g["upper"],
                "status": g["fill"]["label"], "status_code": g["fill"]["status"], "fill_percent": g["fill"]["percent"],
                "forming": not g["confirmed"], "provisional": g["fill"]["provisional"],
                "formed": g["third"]["label"] if g.get("third") else None, "location": loc, "location_text": location_text(loc)}
    counts = fvg.get("fill_counts") or {}
    return {"available": True, "reason": "", "bullish": [item(g) for g in rel.get("bullish", [])],
            "bearish": [item(g) for g in rel.get("bearish", [])], "total": fvg.get("total", 0), "counts": counts,
            "status_note": ("Status is derived from the engine's zone and the candles after it formed (wicks count): the FVG engine "
                            "detects gaps but does not track fills itself.")}


def ob_section(payload: dict, price: float | None) -> dict:
    ob = payload.get("order_blocks") or {}
    if not ob.get("available"):
        return {"available": False, "reason": ob.get("reason") or "Order blocks were not analysed.", "bullish": [], "bearish": []}
    rel = ob.get("relevant") or {}

    def item(b):
        loc = location_of(b["lower"], b["upper"], price)
        status = b["status"] + (" (provisional)" if b.get("provisional") else "")
        return {"id": b["id"], "direction": b["direction"], "range_text": _range_text(b), "lower": b["lower"], "upper": b["upper"],
                "status": status, "status_code": b["status"], "provisional": b.get("provisional", False),
                "source": b["source"]["label"] if b.get("source") else None, "location": loc, "location_text": location_text(loc)}
    return {"available": True, "reason": "", "bullish": [item(b) for b in rel.get("bullish", [])],
            "bearish": [item(b) for b in rel.get("bearish", [])], "counts": ob.get("counts") or {}, "total": ob.get("total", 0),
            "status_note": "Status comes straight from the order block engine. A candidate is not confirmed."}


# ---------------------------------------------------------------------------------------------------- overall
def overall_section(direction: dict, liquidity: dict, fvg: dict, ob: dict) -> dict:
    label = direction["label"]
    items = []                                           # {'direction', 'text'} evidence besides the structure label itself
    ev = liquidity.get("evidence") if liquidity.get("available") else None
    if ev and ev["verdict"] in ("reversal", "continuation") and ev["direction"]:
        items.append({"direction": ev["direction"], "text": f"{ev['title']}: " + ev["text"]})
    for kind, sec in (("order block", ob), ("fair value gap", fvg)):
        if not sec.get("available"):
            continue
        for side in ("bullish", "bearish"):
            for z in sec[side]:
                usable = z["status_code"] == "confirmed" if kind == "order block" else (z["status_code"] != "filled" and not z["forming"])
                if z["location"] == "inside" and usable:
                    items.append({"direction": side, "text": f"Price is inside a {side} {kind} ({z['range_text']})."})
    supporting, opposing = [], []
    if label in ("bullish", "bearish"):
        for it in items:
            (supporting if it["direction"] == label else opposing).append(it["text"])
    else:
        opposing = [it["text"] for it in items]              # no leaning direction: everything is simply 'pointing somewhere'
    conflicting = list(opposing)
    prov = direction.get("provisional")
    structure_disputed = label in ("bullish", "bearish") and (
        direction.get("quality") == "mixed" or bool(prov and prov["direction"] != label))
    if structure_disputed and direction.get("reason"):
        conflicting.insert(0, direction["reason"])
    if prov and prov["direction"] != label and label in ("bullish", "bearish"):
        conflicting.insert(0, f"A {prov['direction']} {prov['type']} is forming on the newest candle (unconfirmed).")
    if direction.get("caveat"):
        conflicting.insert(0, direction["caveat"])

    uncertain = ["The newest candle is still forming, so anything that depends on it can change when it closes.",
                 "Only this one timeframe is analysed. Higher or lower timeframes may tell a different story.",
                 "The engines use candles only: no volume, order flow, news or spreads. Zones and levels are areas of interest, not predictions.",
                 "There is no setup engine yet, so this page cannot say whether a trade exists."]
    for name, sec in (("Liquidity", liquidity), ("Fair value gaps", fvg), ("Order blocks", ob)):
        if not sec.get("available"):
            uncertain.append(f"{name} are unavailable, so they are not part of this reading.")
    if liquidity.get("available") and liquidity["headline"] and ev and ev["verdict"] == "none":
        uncertain.append("The newest liquidity sweep has not been followed by a structure break, so it says nothing yet about direction.")
    if ob.get("available") and any(z["status_code"] == "candidate" for z in ob["bullish"] + ob["bearish"]):
        uncertain.append("Some nearby order blocks are only candidates: their leg has not yet broken structure.")

    insufficient = label == "unclear"
    if insufficient:
        verdict, text = "insufficient", ("There is not enough evidence to support a directional conclusion. " + (direction.get("reason") or ""))
    elif label == "mixed" or structure_disputed:
        verdict, text = "mixed", "The evidence is mixed: it points in more than one direction, so no single direction is supported."
    else:
        if opposing and len(opposing) >= len(supporting):
            verdict, text = "mixed", f"Structure reads {label}, but other evidence points the other way, so the overall picture is mixed."
        else:
            verdict = label
            text = (f"Structure reads {label}" + (" and the other evidence agrees." if supporting else ". Nothing else confirms or contradicts it yet.")
                    + (" Some conflicting evidence is listed below." if opposing else "") + " This describes what has happened, not what price will do.")
    return {"verdict": verdict, "insufficient": insufficient, "text": text, "supporting": supporting, "conflicting": conflicting,
            "uncertain": uncertain, "note": EVIDENCE_NOTE}


def build_summary(payload: dict, last_close: float | None, bar_times: list) -> dict | None:
    """The five plain-language sections. An unavailable engine makes ITS section say so. A bug here returns None (the page then shows the
    detailed results unchanged) and can never break the analysis."""
    try:
        return _build_summary(payload, last_close, bar_times)
    except Exception:                                    # noqa: BLE001
        logging.getLogger("trademate.analysis").exception("Plain-language summary crashed")
        return None


def _build_summary(payload: dict, last_close: float | None, bar_times: list) -> dict:
    direction = direction_section(payload)
    liquidity = liquidity_section(payload, last_close, bar_times)
    fvg = fvg_section(payload, last_close)
    ob = ob_section(payload, last_close)
    return {"reference_price": last_close,
            "reference_note": "Price location uses the close of the newest candle, not a live quote.",
            "direction": direction, "liquidity": liquidity, "fvg": fvg, "order_blocks": ob,
            "overall": overall_section(direction, liquidity, fvg, ob)}
