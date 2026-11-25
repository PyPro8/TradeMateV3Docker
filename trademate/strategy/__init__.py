"""Phase 8: multi-timeframe Strategy Engine (analysis and proposals only: it never sizes, approves or sends an order)."""

from trademate.strategy.mtf import (BUY, RULE_ID, SELL, WAIT, WATCH, Context, StrategyConfig, StrategyResult, TimeframeAnalysis,
                                    analyze_timeframe, evaluate, pick_targets, resolve_context, structural_stop)
from trademate.strategy.regime import estimate_regimes
from trademate.strategy.setups import discover_candidates
from trademate.strategy.timeframes import (NATIVE_TIMEFRAMES, TF_SECONDS, TIMEFRAMES, DataRules, PreparedSeries, prepare_series)

__all__ = ["BUY", "RULE_ID", "SELL", "WAIT", "WATCH", "Context", "StrategyConfig", "StrategyResult", "TimeframeAnalysis",
           "analyze_timeframe", "evaluate", "pick_targets", "resolve_context", "structural_stop", "estimate_regimes",
           "discover_candidates", "NATIVE_TIMEFRAMES", "TF_SECONDS", "TIMEFRAMES", "DataRules", "PreparedSeries", "prepare_series"]
