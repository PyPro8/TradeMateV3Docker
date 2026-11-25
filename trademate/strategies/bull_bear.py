"""Strategy: Bull/Bear Trading (id "bull_bear").  RULE "BULL-BEAR-v3".

Bull/Bear is NOT evaluated here. It is a modified copy of the Candle Momentum engine (`trademate.bull_bear`): the same persistent, tick-driven candle
lifecycle, with ONE extra requirement: a confirmed market structure established by a valid CHoCH. A candle is entered only when its direction equals the
structure direction; before a CHoCH nothing is traded; a new opposing CHoCH invalidates the old direction. See docs/BULL_BEAR.md.

The previous candle-group rules ("BULL-BEAR-v1") were deliberately REPLACED. What remains here is the registry's snapshot hook: `evaluate_selected` routes
Bull/Bear to the engine BEFORE this function can be reached; a direct call (no broker, no live quote, no lifecycle) answers with an honest WAIT.
"""

from __future__ import annotations

from trademate.strategies.base import unusable_result


def evaluate(defn, symbol: str, series: dict, params, cfg, clock: dict) -> dict:
    tf = params.exec_tf
    return unusable_result(defn, symbol, [f"Bull/Bear is decided tick by tick by the live engine (trademate.bull_bear) from the confirmed market structure, not from {tf} "
                                          "candle snapshots. No live engine result was supplied, so there is no decision."], series, clock, params, (tf,))
