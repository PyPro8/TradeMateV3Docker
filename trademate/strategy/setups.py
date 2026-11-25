"""Evidence-backed setup-family discovery; candidates are not execution approvals."""

from __future__ import annotations

import hashlib

from trademate.strategy.regime import RANGE, TREND_BEAR, TREND_BULL


def _analysis(analyses: dict, timeframe: str):
    return analyses.get(timeframe)


def _id(family: str, direction: str, anchor: str) -> str:
    raw = "|".join((family, direction, anchor))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _candidate(family: str, direction: str, anchor: str, premise: str, supporting=(), conflicting=(),
               missing=(), invalidation=(), target_method: str = "No target is proposed until the existing qualification gates establish a structural level.") -> dict:
    return {
        "candidate_id": _id(family, direction, anchor),
        "setup_family": family,
        "direction": direction,
        "status": "developing",
        "premise": premise,
        "supporting_evidence": list(supporting),
        "conflicting_evidence": list(conflicting),
        "missing_evidence": list(missing),
        "invalidation_conditions": list(invalidation),
        "target_methodology": target_method,
        "execution_eligible": False,
    }


def _trend_direction(regimes: list[dict]) -> str | None:
    active = {item["classification"] for item in regimes if item.get("active")}
    return "bullish" if TREND_BULL in active else "bearish" if TREND_BEAR in active else None


def discover_candidates(analyses: dict, regimes: list[dict]) -> list[dict]:
    """Discover candidates from supported structure, zones, sweeps, and validated closed candles."""
    output = []
    trend = _trend_direction(regimes)
    h1, h4, m15, m5 = (_analysis(analyses, tf) for tf in ("H1", "H4", "M15", "M5"))
    htf = [a for a in (h4, h1) if a is not None and getattr(a, "analyzed", False)]
    if trend is None and h4 and h1 and h4.bias in ("bullish", "bearish") and h1.bias in ("bullish", "bearish"):
        h1_reversal = getattr(h1, "reversal", {})
        if h4.bias != h1.bias and h1_reversal.get("stage") == "confirmed" and h1_reversal.get("toward") == h1.bias:
            trend = h4.bias

    if trend:
        aligned = [a for a in (h1, m15) if a is not None and getattr(a, "analyzed", False) and a.bias == trend]
        opposite = [a for a in (m15, m5) if a is not None and getattr(a, "analyzed", False) and a.bias != "neutral" and a.bias != trend]
        bos = next((event for analysis in (m15, m5) if analysis is not None
                    for event in reversed(getattr(analysis, "events", ()))
                    if event.get("type") == "BOS" and event.get("direction") == trend and event.get("confirmed")), None)
        if aligned or bos:
            evidence = [f"{a.timeframe} structure is {trend}." for a in aligned]
            if bos:
                evidence.append(f"A closed {bos['direction']} BOS was observed on {next(a.timeframe for a in (m15, m5) if a and bos in a.events)}.")
            output.append(_candidate(
                "trend_continuation", trend, str((bos or {}).get("candle_time", getattr(h1, "last_close_time", "trend"))),
                f"Continuation in the measured {trend} higher-timeframe context.",
                evidence, [f"{a.timeframe} structure is {a.bias}." for a in opposite],
                [] if bos else ["A recent closed structural continuation event."],
                [f"A closed break beyond the higher-timeframe {trend} invalidation level."],
                "Use the next valid structural swing/liquidity level in the trade direction; the Risk Engine determines position size.",
            ))

        zones = [z for z in getattr(m15, "zones", ()) if z.get("direction") == trend] if m15 else []
        if opposite or zones:
            zone = zones[0] if zones else None
            output.append(_candidate(
                "pullback_continuation", trend, str(zone.get("id") if zone else getattr(m15, "last_close_time", "pullback")),
                f"Counter-move or pullback area within the {trend} higher-timeframe structure.",
                [f"{a.timeframe} is {a.bias}, opposite the higher-timeframe trend." for a in opposite] +
                ([f"An actual M15 {zone.get('kind', 'zone')} exists at {zone.get('lower'):g}-{zone.get('upper'):g}." ] if zone else []),
                [f"{a.timeframe} is not aligned with the {trend} continuation premise." for a in htf if a.bias not in ("neutral", trend)],
                ["A closed M5 reaction and directional break after a live M15 area is reached."],
                [f"A closed M15 candle beyond the selected zone's far edge." if zone else "A closed break of the higher-timeframe invalidation level."],
                "Use a structural target beyond entry with the configured minimum reward-to-risk; current qualification derives targets from swings/liquidity.",
            ))

    for analysis in (m15, m5):
        if analysis is None or not getattr(analysis, "analyzed", False):
            continue
        for sweep in getattr(analysis, "liquidity", {}).get("sweeps", ()):
            if sweep.get("confirmed") is not True:
                continue
            direction = "bearish" if sweep.get("side") == "BSL" else "bullish" if sweep.get("side") == "SSL" else None
            if direction is None:
                continue
            follow = next((event for event in analysis.events
                           if event.get("confirmed") and event.get("direction") == direction and event.get("candle_time", 0) > sweep.get("time", 0)), None)
            output.append(_candidate(
                "liquidity_sweep_reaction", direction, f"{analysis.timeframe}:{sweep.get('side')}:{sweep.get('time')}:{sweep.get('level')}",
                f"A confirmed {sweep['side']} liquidity sweep is a possible opposing reaction, not proof of reversal.",
                [f"{analysis.timeframe} confirmed a {sweep['side']} sweep at {sweep.get('level')}." ] +
                ([f"A later closed {direction} {follow['type']} followed the sweep."] if follow else []),
                ["A sweep candle that closed beyond the pool is not itself reversal confirmation."] if sweep.get("close_beyond") else [],
                [] if follow else [f"A later closed {direction} structural reaction after the sweep."],
                [f"A closed break through the sweep extreme in the opposite direction."],
                "Use the next opposing structural swing/liquidity only after a separate entry, stop, and reward-to-risk evaluation.",
            ))

    for tf in ("H4", "H1", "M15", "M5"):
        analysis = _analysis(analyses, tf)
        reversal = getattr(analysis, "reversal", {}) if analysis else {}
        direction = reversal.get("toward") if isinstance(reversal, dict) else None
        stage = reversal.get("stage") if isinstance(reversal, dict) else None
        if direction in ("bullish", "bearish") and stage in ("possible", "developing", "confirmed"):
            opposing = [a for a in htf if a.timeframe != tf and a.bias in ("bullish", "bearish") and a.bias != direction]
            anchor = reversal.get("anchor_time") or getattr(analysis, "invalidation", {}).get("level") or getattr(analysis, "last_close_time", "reversal")
            output.append(_candidate(
                "structural_reversal", direction, f"{tf}:{anchor}",
                f"{tf} structure reports a {stage} reversal toward {direction}.",
                list(reversal.get("evidence", ())) or [f"{tf} reversal stage is {stage}."],
                [f"{a.timeframe} remains {a.bias}." for a in opposing],
                list(reversal.get("conditions", ())) if stage != "confirmed" else ["A developing M15 structure and a separate M5 entry trigger."],
                [next(iter(reversal.get("conditions", ())), "A closed break of the actual structural invalidation level reverses the thesis.")],
                "Use structural levels beyond the entry; do not infer a target from reversal status alone.",
            ))

    if m15 and getattr(m15, "analyzed", False):
        events = getattr(m15, "events", ())
        rows = getattr(m15, "rows", ())
        for event in reversed(events):
            direction = event.get("direction")
            if event.get("type") != "BOS" or not event.get("confirmed") or direction not in ("bullish", "bearish"):
                continue
            later = [row for row in rows if row[0] > event.get("candle_time", 0)]
            retest = next((row for row in later if row[3] <= event["level"] <= row[2]
                           and (row[4] > event["level"] if direction == "bullish" else row[4] < event["level"])), None)
            if retest:
                output.append(_candidate(
                    "breakout_retest", direction, f"M15:{event['candle_time']}:{event['level']}",
                    f"A closed M15 {direction} BOS was followed by a closed candle retest that closed back beyond the broken level.",
                    [f"M15 retested {event['level']} at {retest[0]} and closed in the breakout direction."],
                    [f"{a.timeframe} structure is {a.bias} against the breakout." for a in htf if a.bias not in ("neutral", direction)],
                    ["A separate closed M5 entry trigger, structural stop, and eligible target."],
                    [f"A closed M15 return through {event['level']} against the breakout."],
                    "Use the next structural level in the breakout direction subject to the minimum reward-to-risk.",
                ))
                break

    if any(item.get("active") and item.get("classification") == RANGE for item in regimes):
        output.append(_candidate(
            "range_bound_opportunity", "neutral", "M15:range",
            "Measured M15 compression may support a range hypothesis; no direction is assumed until price location and reaction are established.",
            [item["certainty_basis"] for item in regimes if item.get("classification") == RANGE and item.get("active")],
            [f"{tf} structure is directional." for tf, a in (("H4", h4), ("H1", h1)) if a and a.bias in ("bullish", "bearish")],
            ["A closed reaction at a measured range boundary; no range entry is proposed by this engine."],
            ["A closed candle outside the observed range invalidates the range hypothesis."],
            "The opposing observed range boundary is a research reference only, not a qualified take-profit.",
        ))

    if trend:
        counter_direction = "bearish" if trend == "bullish" else "bullish"
        h1_reversal = getattr(h1, "reversal", {}) if h1 else {}
        counter_m15 = bool(m15 and getattr(m15, "analyzed", False) and m15.bias == counter_direction and m15.strength == "established")
        if h1_reversal.get("toward") == counter_direction and h1_reversal.get("stage") in ("possible", "developing", "confirmed"):
            supported = h1_reversal.get("stage") == "confirmed" and counter_m15
            output.append(_candidate(
                "countertrend_opportunity",
                counter_direction,
                f"H1:{h1_reversal.get('anchor_time') or getattr(h1, 'invalidation', {}).get('level') or 'countertrend'}",
                f"Countertrend {counter_direction} candidate against the {trend} higher-timeframe structure.",
                list(h1_reversal.get("evidence", ())) + ([f"M15 structure is established {counter_direction}."] if counter_m15 else []),
                [f"HTF context remains {trend}."] + [f"{a.timeframe} is {a.bias}." for a in htf if a.bias == trend],
                [] if supported else ["A confirmed H1 reversal and established M15 structure in the countertrend direction."],
                list(h1_reversal.get("conditions", ())) or ["A closed break of the H1 invalidation level reverses the thesis."],
                "Require the existing countertrend confirmation rule plus the same structural stop/target and Risk Engine checks.",
            ))

    return output
