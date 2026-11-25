"""Structural facts: ONE interpretation of a StructureResult, shared by the Analysis page and the Multi-timeframe Strategy page.

Both pages used to derive "direction" and "quality" separately and could disagree for identical candles (for example Analysis
said "mixed" right after a CHoCH while the Strategy page said "bullish, early"). They now both call `structural_facts`.

Kept apart on purpose (see docs/MARKET_STRUCTURE.md):
    confirmed_direction   bullish / bearish / None - the engine's direction as of the last CLOSED candle only.
    quality               how well the swing pattern supports that direction:
                            "early"      the last confirmed break was a CHoCH and no follow-through BOS has happened yet
                            "mixed"      the last confirmed break was a BOS but the latest confirmed swings point the other way
                            "aligned"    the last break was a BOS and the latest swings agree (HH+HL bullish, LH+LL bearish)
                            "developing" the last break was a BOS, nothing opposes it, but the swing pattern is not fully formed
                            None         no confirmed direction
    last_confirmed_event  the newest confirmed BOS / CHoCH (id, type, direction, level), or None
    provisional           what a still-forming candle currently shows (never part of direction or quality), or None
Neither value is a trade signal.
"""
from __future__ import annotations

from trademate.structure.engine import StructureResult

QUALITIES = ("early", "mixed", "aligned", "developing")


def _last_class(res: StructureResult, kind: str) -> str | None:
    for s in reversed(res.swings):
        if s.kind == kind and s.confirmed and s.classification:
            return s.classification
    return None


def structural_facts(res: StructureResult) -> dict:
    direction = res.trend_confirmed if res.trend_confirmed in ("bullish", "bearish") else None
    confirmed = sorted((e for e in res.events if e.confirmed), key=lambda e: e.candle_index)
    forming = [e for e in res.events if not e.confirmed]
    last = confirmed[-1] if confirmed else None
    hi, lo = _last_class(res, "high"), _last_class(res, "low")
    agree = oppose = False
    quality = None
    if direction is not None and last is not None:
        bull = direction == "bullish"
        agree = (hi, lo) == (("HH", "HL") if bull else ("LH", "LL"))
        oppose = hi == ("LH" if bull else "HH") or lo == ("LL" if bull else "HL")
        if last.type == "CHOCH":
            quality = "early"
        elif oppose:
            quality = "mixed"
        elif agree:
            quality = "aligned"
        else:
            quality = "developing"
    return {
        "confirmed_direction": direction,
        "quality": quality,
        "swing_pattern": {"high": hi, "low": lo, "agrees": agree, "opposes": oppose},
        "last_confirmed_event": ({"id": last.id, "type": last.type, "direction": last.direction, "level": last.level,
                                  "candle_time": last.candle_time, "reference_role": last.reference_role} if last else None),
        "provisional": ({"type": forming[-1].type, "direction": forming[-1].direction, "level": forming[-1].level,
                         "against_confirmed": direction is not None and forming[-1].direction != direction} if forming else None),
    }
