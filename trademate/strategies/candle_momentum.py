"""Strategy: Candle Momentum (id "candle_momentum").  RULE "CANDLE-MOMENTUM-v2".

Candle Momentum is NOT evaluated here any more. The v1 implementation was a snapshot function that a Watchdog called once a minute on freshly fetched
candles; a 10-pip trigger was therefore usually seen only after price had already run past the hard-coded 2-pip "late" window (see docs/CANDLE_MOMENTUM.md,
"Why the old strategy was late"). It has been replaced by a persistent, tick-driven engine: `trademate.candle_momentum`.

What remains here is the registry's snapshot hook. `evaluate_selected` (trademate.strategies.service) routes Candle Momentum to the engine BEFORE this function
could be reached; if anything calls it directly (it has no broker, no live quote and no persistent lifecycle) it answers honestly with a WAIT instead of
guessing a decision from candles.
"""

from __future__ import annotations

from trademate.strategies.base import unusable_result


def evaluate(defn, symbol: str, series: dict, params, cfg, clock: dict) -> dict:
    tf = params.exec_tf
    return unusable_result(defn, symbol, [f"Candle Momentum is decided tick by tick by the live engine (trademate.candle_momentum), not from {tf} candle snapshots. "
                                          "No live engine result was supplied, so there is no decision."], series, clock, params, (tf,))
