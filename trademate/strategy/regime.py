"""Deterministic market-regime estimates from validated candles and structure.

Certainty is qualitative, not a probability: high means independent higher-timeframe
structure agrees, moderate means one reliable directional source, and low means the
classification is incomplete or contradictory.
"""

from __future__ import annotations

from statistics import median

TREND_BULL = "bullish_trend"
TREND_BEAR = "bearish_trend"
RANGE = "range_bound"
TRANSITION = "transition_or_potential_reversal"
VOLATILITY = "elevated_volatility"
UNCERTAIN = "uncertain_or_insufficient"

_VOLATILITY_RATIO = 2.0
_RANGE_EFFICIENCY_MAX = 0.20
_RANGE_WIDTH_ATR_MAX = 10.0
_LOOKBACK = 24


def _direction(analyses: dict, timeframe: str) -> str | None:
    analysis = analyses.get(timeframe)
    if analysis is None or not getattr(analysis, "analyzed", False):
        return None
    direction = getattr(analysis, "bias", None)
    return direction if direction in ("bullish", "bearish") else None


def _series_rows(series: dict | None, timeframe: str) -> tuple:
    if not isinstance(series, dict):
        return ()
    prepared = series.get(timeframe)
    if prepared is None or getattr(prepared, "status", None) not in ("ok", "degraded"):
        return ()
    return getattr(prepared, "closed", ())


def _regime(name: str, active: bool, timeframe: str, supporting=(), conflicting=(), limitations=(),
            certainty: str = "low", certainty_basis: str = "") -> dict:
    return {
        "classification": name,
        "active": active,
        "timeframe": timeframe,
        "supporting_evidence": list(supporting),
        "conflicting_evidence": list(conflicting),
        "limitations": list(limitations),
        "certainty": certainty,
        "certainty_basis": certainty_basis,
    }


def _range_measure(rows: tuple) -> tuple[float, float] | None:
    if len(rows) < _LOOKBACK:
        return None
    closes = [float(row[4]) for row in rows[-_LOOKBACK:]]
    path = sum(abs(right - left) for left, right in zip(closes, closes[1:]))
    if path <= 0:
        return 0.0, 0.0
    efficiency = abs(closes[-1] - closes[0]) / path
    true_ranges = []
    for previous, current in zip(rows[-_LOOKBACK - 1:-1], rows[-_LOOKBACK:]):
        true_ranges.append(max(float(current[2]) - float(current[3]),
                               abs(float(current[2]) - float(previous[4])),
                               abs(float(current[3]) - float(previous[4]))))
    typical_range = median(true_ranges) if true_ranges else 0.0
    width = max(closes) - min(closes)
    width_in_atr = width / typical_range if typical_range > 0 else float("inf")
    return efficiency, width_in_atr


def _volatility_ratio(rows: tuple) -> float | None:
    if len(rows) < 40:
        return None
    true_ranges = []
    for previous, current in zip(rows[:-1], rows[1:]):
        true_ranges.append(max(float(current[2]) - float(current[3]),
                               abs(float(current[2]) - float(previous[4])),
                               abs(float(current[3]) - float(previous[4]))))
    baseline = median(true_ranges[-31:-10])
    recent = median(true_ranges[-10:])
    if baseline <= 0:
        return None
    return recent / baseline


def estimate_regimes(series: dict | None, analyses: dict) -> list[dict]:
    """Return interpretable overlapping regime classifications from actual closed data."""
    h4, h1, m15, m5 = (_direction(analyses, tf) for tf in ("H4", "H1", "M15", "M5"))
    h4a, h1a = analyses.get("H4"), analyses.get("H1")
    htf_conflict = bool(h4 and h1 and h4 != h1)
    available_high = [tf for tf, direction in (("H4", h4), ("H1", h1)) if direction]
    trend_direction = h4 if h4 and h1 and h4 == h1 else (h1 or h4)
    main_evidence = []
    main_conflicts = []
    limitations = []
    for tf, direction, analysis in (("H4", h4, h4a), ("H1", h1, h1a)):
        if direction:
            main_evidence.append(f"{tf} closed-candle structure is {direction} ({getattr(analysis, 'strength', 'unknown')} strength).")
        elif analysis is None or not getattr(analysis, "analyzed", False):
            status = getattr(analysis, "data_status", "unavailable")
            limitations.append(f"{tf} structure is unavailable ({status}); no other timeframe is substituted for it.")
    if htf_conflict:
        main_conflicts.append(f"H4 is {h4} while H1 is {h1}; both observations are retained.")
    if m15 and trend_direction and m15 != trend_direction:
        main_conflicts.append(f"M15 is {m15}, opposite the {trend_direction} higher-timeframe context.")
    if m5 and trend_direction and m5 != trend_direction:
        main_conflicts.append(f"M5 is {m5}, opposite the {trend_direction} higher-timeframe context.")

    high_strength = bool(
        trend_direction
        and not htf_conflict
        and ((h4 and h1 and h4 == h1 and all(getattr(a, "strength", None) == "established" for a in (h4a, h1a)))
             or (len(available_high) == 1 and getattr(h1a if h1 else h4a, "strength", None) == "established"))
    )
    regimes = [
        _regime(TREND_BULL, trend_direction == "bullish" and not htf_conflict,
                "H4/H1" if h4 and h1 else ("H1" if h1 else "H4"),
                main_evidence if trend_direction == "bullish" else (),
                main_conflicts, limitations,
                "high" if high_strength and h4 and h1 else ("moderate" if trend_direction == "bullish" and not htf_conflict else "low"),
                "High means H4 and H1 both have established bullish structure; moderate means only one higher timeframe is available or structure is weaker; these categories describe evidence coverage, not a probability score."),
        _regime(TREND_BEAR, trend_direction == "bearish" and not htf_conflict,
                "H4/H1" if h4 and h1 else ("H1" if h1 else "H4"),
                main_evidence if trend_direction == "bearish" else (),
                main_conflicts, limitations,
                "high" if high_strength and h4 and h1 else ("moderate" if trend_direction == "bearish" and not htf_conflict else "low"),
                "High means H4 and H1 both have established bearish structure; moderate means only one higher timeframe is available or structure is weaker; these categories describe evidence coverage, not a probability score."),
    ]

    reversal_frames = []
    reversal_evidence = []
    for tf in ("H4", "H1", "M15", "M5"):
        analysis = analyses.get(tf)
        reversal = getattr(analysis, "reversal", {}) if analysis else {}
        stage = reversal.get("stage") if isinstance(reversal, dict) else None
        if stage in ("possible", "developing", "confirmed"):
            reversal_frames.append(tf)
            toward = reversal.get("toward")
            reversal_evidence.extend(reversal.get("evidence", []))
            if toward:
                reversal_evidence.append(f"{tf} reports a {stage} reversal toward {toward}.")
    transition_active = bool(htf_conflict or reversal_frames)
    regimes.append(_regime(
        TRANSITION, transition_active, "/".join(reversal_frames) or "H4/H1",
        reversal_evidence or (["H4 and H1 have conflicting directional structure."] if htf_conflict else []),
        main_conflicts, limitations,
        "moderate" if transition_active and not limitations else "low",
        "Moderate means a closed structural disagreement or reversal-stage observation exists; it does not establish a completed reversal.",
    ))

    range_rows = _series_rows(series, "M15")
    range_metrics = _range_measure(range_rows)
    range_active = bool(
        range_metrics
        and range_metrics[0] <= _RANGE_EFFICIENCY_MAX
        and range_metrics[1] <= _RANGE_WIDTH_ATR_MAX
        and not (h4 and h1 and h4 == h1)
    )
    range_support = []
    if range_metrics:
        range_support.append(f"M15 close-path efficiency is {range_metrics[0]:.2f} and the { _LOOKBACK }-bar close range is {range_metrics[1]:.2f} median true ranges.")
    range_limits = [] if range_rows else ["At least 24 validated closed M15 candles are required to estimate a range."]
    regimes.append(_regime(
        RANGE, range_active, "M15", range_support,
        main_conflicts if htf_conflict else (),
        range_limits,
        "moderate" if range_active else "low",
        f"Range means M15 close-path efficiency <= {_RANGE_EFFICIENCY_MAX:.2f} and width <= {_RANGE_WIDTH_ATR_MAX:g} median true ranges over {_LOOKBACK} closes, with no established aligned H4/H1 trend. This is a heuristic, not a forecast.",
    ))

    volatility_evidence = []
    volatility_conflicts = []
    volatility_frames = []
    for tf in ("M5", "M15", "H1", "H4"):
        ratio = _volatility_ratio(_series_rows(series, tf))
        if ratio is not None and ratio >= _VOLATILITY_RATIO:
            volatility_frames.append(tf)
            volatility_evidence.append(f"{tf} median true range over the latest 10 bars is {ratio:.2f}x its prior 21-bar median.")
    if volatility_frames and trend_direction:
        volatility_conflicts.append(f"Elevated volatility does not negate the {trend_direction} structural context.")
    regimes.append(_regime(
        VOLATILITY, bool(volatility_frames), "/".join(volatility_frames) or "M5/M15/H1/H4",
        volatility_evidence, volatility_conflicts,
        ["At least 40 validated closed candles are required per timeframe; no ATR is fabricated."],
        "moderate" if volatility_frames else "low",
        f"Elevated means recent median true range is at least {_VOLATILITY_RATIO:g}x the preceding 21-bar median on an available timeframe; this is relative expansion, not a volatility forecast.",
    ))

    core_missing = [tf for tf in ("H4", "H1", "M15", "M5")
                    if not getattr(analyses.get(tf), "analyzed", False)]
    uncertain_active = bool(core_missing or htf_conflict or not (
        any(item["active"] for item in regimes if item["classification"] in (TREND_BULL, TREND_BEAR, RANGE, TRANSITION))
    ))
    uncertain_support = []
    if htf_conflict:
        uncertain_support.append("Higher-timeframe directional evidence conflicts.")
    if not trend_direction and not range_active and not transition_active:
        uncertain_support.append("No directional higher-timeframe structure, measurable range, or reversal-stage evidence is established.")
    regimes.append(_regime(
        UNCERTAIN, uncertain_active, "H4/H1/M15/M5", uncertain_support, main_conflicts,
        [f"{tf} structure is unavailable; no timeframe is substituted." for tf in core_missing] + limitations,
        "low" if uncertain_active else "moderate",
        "Low means at least one required analytical view is missing, higher-timeframe evidence conflicts, or no regime rule is supported; it is not a probability.",
    ))
    return regimes
