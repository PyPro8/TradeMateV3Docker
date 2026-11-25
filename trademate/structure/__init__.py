"""Phase 7A: objective market-structure analysis (analysis only: it never trades)."""

from trademate.structure.engine import (
    BREAK_MODES,
    Diagnostics,
    StructureConfig,
    StructureEvent,
    StructureResult,
    SwingPoint,
    analyze_cached,
    analyze_market_structure,
    clean_candles,
)
from trademate.structure.facts import structural_facts

__all__ = ["structural_facts", "BREAK_MODES", "Diagnostics", "StructureConfig", "StructureEvent", "StructureResult", "SwingPoint",
           "analyze_cached", "analyze_market_structure", "clean_candles"]
