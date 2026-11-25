"""The strategy catalogue: exactly five ACTIVE strategies (Candle Momentum, CHoCH Trading, Market Structure Trading, Bull/Bear Trading, Trendline Trading).
Trend Continuation and EMA 20/50 + ATR were retired from the selector; their definitions and code are kept in RETIRED so shared components, old decisions and their history still resolve. A client may only ever name one of these identifiers; anything else is rejected."""

from __future__ import annotations

from trademate.strategies import bull_bear, candle_momentum, choch, ema_atr, market_structure, trend, trendline
from trademate.strategies.base import StrategyDefinition, StrategyParams

CHOCH = StrategyDefinition(
    id="choch", name="CHoCH Trading", version="1.0.0", rule_id="CHOCH-H1M5-v1",
    summary="Trade a confirmed M5 change of character, with H1 as context.",
    description=("Looks for a confirmed M5 Change of Character (a closed candle breaking a confirmed swing against the previous M5 direction) and "
                 "proposes an entry after it, with a structural stop and target. H1 gives context but does not veto: a CHoCH against H1 simply needs "
                 "a higher reward-to-risk. A CHoCH is a possible change in behaviour, not proof."),
    emits_choch_alerts=True, choch_alert_timeframes=("M5",), fixed_target_pips=100.0,
    tooltip="Identifies a Change of Character in market structure and trades the resulting structural shift.")
TREND = StrategyDefinition(
    id="trend", name="Trend Continuation", version="1.0.0", rule_id="TREND-H1M5-v1",
    summary="Trade with the H1 trend after an M5 pullback resumes.",
    description=("H1 must show a confirmed trend (higher highs and higher lows, or lower highs and lower lows). M5 then pulls back against it, and a "
                 "closed M5 candle beyond the impulse extreme shows the trend resuming. The stop sits beyond the pullback; the target is structural."),
    emits_choch_alerts=False, choch_alert_timeframes=())

CANDLE = StrategyDefinition(
    id="candle_momentum", name="Candle Momentum", version="1.0.0", rule_id="CANDLE-MOMENTUM-v1",
    summary="Live scalping: enter the moment the forming candle crosses its trigger distance from the open; target a further distance from the actual fill.",
    description=("A persistent engine watches the forming candle tick by tick. Price above the candle open = bullish, below = bearish. The moment price crosses "
                 "the configured trigger distance from the open, the entry is judged on the current executable quote and, if the configured target is still "
                 "reasonably available, proposed to the Risk Engine. The take-profit is the configured distance from the ACTUAL fill; the stop sits just beyond "
                 "the candle open. One entry per candle. No structure, CHoCH or trend is used. Its reward-to-risk can be below 1: it only pays if it wins "
                 "more often than it loses, which is not assumed."),
    emits_choch_alerts=False, choch_alert_timeframes=(), uses_context=False, fixed_target_pips=10.0,
    tooltip="Analyses candles in relation to their surrounding candle group to identify directional momentum and trading opportunities.")

MARKET_STRUCTURE = StrategyDefinition(
    id="market_structure", name="Market Structure Trading", version="1.0.0", rule_id="MARKET-STRUCTURE-v1",
    summary="Trade the direction set by higher highs / higher lows or lower highs / lower lows.",
    description=("Reads the confirmed swing structure of the chosen timeframe. Higher highs with higher lows = bullish; lower highs with lower lows = bearish; anything contradictory "
                 "or unproven = wait. A straight line is drawn across the relevant swing highs. When the structure is established it enters on the close of the newest candle with no "
                 "extra confirmation, a stop beyond the protected swing and a fixed, configurable target."),
    emits_choch_alerts=False, choch_alert_timeframes=(), uses_context=False, fixed_target_pips=25.0,
    tooltip="Uses higher highs, higher lows, lower highs and lower lows to establish market direction and identify entries.")
BULL_BEAR = StrategyDefinition(
    id="bull_bear", name="Bull/Bear Trading", version="3.0.0", rule_id="BULL-BEAR-v3",
    summary="Candle Momentum entries, taken only in the direction of a confirmed CHoCH market structure.",
    description=("A modified copy of the Candle Momentum engine. A valid CHoCH establishes the structure direction (bullish or bearish); only candles in that direction can trigger an "
                 "entry, so a bullish candle in bearish structure (or the reverse) is never traded and nothing is traded before a CHoCH. A new opposing CHoCH invalidates the old direction. "
                 "Optional Anti-Twin: on the first adverse move the trade is closed and an equal-volume opposite trade opens at once."),
    emits_choch_alerts=False, choch_alert_timeframes=(), uses_context=False, fixed_target_pips=25.0,
    tooltip="Trades candles that agree with established upward or downward movement.")
TRENDLINE = StrategyDefinition(
    id="trendline", name="Trendline Trading", version="1.0.0", rule_id="TRENDLINE-v1",
    summary="Trade price respecting a line through descending highs or ascending lows; target the indicated level.",
    description=("Draws a line through the two latest confirmed lower highs (or higher lows). While the line is intact and price closes on its trading side it enters, with the stop beyond the "
                 "second anchor and the target at the parallel level indicated by the opposite swing. Independent of Market Structure Trading."),
    emits_choch_alerts=False, choch_alert_timeframes=(), uses_context=False,
    tooltip="Uses lines connecting relevant highs or lows to identify market direction, entries and targets.")

EMA_ATR = StrategyDefinition(
    id="ema_atr", name="EMA 20/50 + ATR", version="1.0.0", rule_id="EMA-ATR-v1",
    summary="Enter on a confirmed EMA 20/50 crossover; stop 2 x ATR, target 4 x ATR.",
    description=("Uses only closed candles of the chosen timeframe. EMA 20 above EMA 50 allows longs only, below allows shorts only. The entry trigger is the "
                 "crossover itself: BUY when EMA 20 crosses above EMA 50, SELL when it crosses below, once per crossover. The stop-loss is 2 x ATR(14) and the "
                 "take-profit 4 x ATR(14) from the entry (nominal 2:1). No other indicator or entry condition is used."),
    emits_choch_alerts=False, choch_alert_timeframes=(), uses_context=False)

_IMPL = {"choch": choch.evaluate, "trend": trend.evaluate, "candle_momentum": candle_momentum.evaluate, "ema_atr": ema_atr.evaluate,
         "market_structure": market_structure.evaluate, "bull_bear": bull_bear.evaluate, "trendline": trendline.evaluate}
CATALOGUE = {d.id: d for d in (CANDLE, CHOCH, MARKET_STRUCTURE, BULL_BEAR, TRENDLINE)}          # the ACTIVE selector: display order = dropdown order
RETIRED = {d.id: d for d in (TREND, EMA_ATR)}                                                   # out of the selector; code and history kept
STRATEGY_IDS = tuple(CATALOGUE)


def get_strategy(strategy_id) -> StrategyDefinition | None:
    """The registered strategy, or None. Only exact registered identifiers match; nothing is guessed, trimmed or lower-cased."""
    return CATALOGUE.get(strategy_id) if isinstance(strategy_id, str) else None


def get_any_strategy(strategy_id) -> StrategyDefinition | None:
    """An active OR retired strategy (for old decisions, journal rows and history). The selector and every new selection use get_strategy."""
    return CATALOGUE.get(strategy_id) or RETIRED.get(strategy_id) if isinstance(strategy_id, str) else None


def public_catalogue() -> list:
    return [d.public() for d in CATALOGUE.values()]


def evaluate_strategy_result(strategy_id: str, symbol: str, series: dict, params: StrategyParams, cfg, clock: dict) -> dict:
    """Run exactly the named strategy on already-prepared H1/M5 series. Raises KeyError for an unregistered identifier."""
    defn = CATALOGUE.get(strategy_id) or RETIRED[strategy_id]
    return _IMPL[strategy_id](defn, symbol, series, params, cfg, clock)
