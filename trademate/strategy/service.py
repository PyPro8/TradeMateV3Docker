"""Glue between the broker adapter and the pure multi-timeframe engine: fetch native candles, measure the broker clock, evaluate.

Read-only. It asks the adapter for candles and symbol specs only; it has no access to any order method and never calls the Risk or
Execution Engines.
"""

from __future__ import annotations

import logging
import time

from trademate.broker.base import ConnectionState
from trademate.errors import BrokerError
from trademate.risk.account_state import resolve_server_clock
from trademate.strategy.mtf import StrategyConfig, evaluate
from trademate.strategy.timeframes import NATIVE_TIMEFRAMES, UNAVAILABLE, DataRules, PreparedSeries, prepare_series

logger = logging.getLogger("trademate.strategy")

FETCH_COUNT = {"M1": 300, "M2": 300, "M5": 300, "M15": 300, "M30": 300, "H1": 500, "H4": 500}  # the bridge serves at most 500 candles per request


def strategy_config(settings, tick_size: float | None = None) -> StrategyConfig:
    st, sc = settings.strategy, settings.structure
    buffer = st.sl_buffer_ticks * tick_size if (st.sl_buffer_ticks and tick_size) else 0.0
    return StrategyConfig(swing_left=sc.swing_left, swing_right=sc.swing_right, break_mode=sc.break_mode, equal_tolerance=sc.equal_tolerance,
                          min_rr=st.min_rr, trigger_lookback=st.trigger_lookback, sl_buffer=buffer).validate()


def _unavailable(tf: str, reason: str, clock_known: bool = False) -> PreparedSeries:
    return PreparedSeries(timeframe=tf, status=UNAVAILABLE, reason=reason, clock_known=clock_known)


def gather_series(adapter, symbol: str, now_server: float | None, rules: DataRules | None = None) -> dict:
    """{"M5","M15","H1","H4"} -> PreparedSeries. A timeframe that cannot be fetched is UNAVAILABLE with the broker's message."""
    rules = rules or DataRules()
    out: dict[str, PreparedSeries] = {}
    for tf in NATIVE_TIMEFRAMES:
        try:
            rows = [c.to_list() for c in adapter.get_candles(symbol, tf, FETCH_COUNT[tf])]
        except (BrokerError, NotImplementedError) as exc:
            out[tf] = _unavailable(tf, f"{tf} candles could not be fetched: {exc}", now_server is not None)
            continue
        out[tf] = prepare_series(rows, tf, now_server, rules)
    return out


def evaluate_symbol(adapter, symbol: str, settings, now_utc: float | None = None, resolver=None) -> dict:
    """The whole multi-timeframe evaluation for one symbol as a JSON-ready dict. Never raises for market-data problems."""
    now_utc = time.time() if now_utc is None else now_utc
    rules = DataRules()
    status = adapter.get_connection_status()
    if status.state is not ConnectionState.CONNECTED:
        why = f"MT5 is not connected with fresh data ({status.message})."
        series = {tf: _unavailable(tf, why) for tf in ("M5", "M15", "H1", "H4")}
        clock = {"known": False, "source": "unknown", "detail": why, "now_server": None, "offset_seconds": None}
        result = evaluate(symbol, series, strategy_config(settings), clock=clock)
        return {"ok": True, "result": result.to_dict(), "connection": status.state.value}
    try:
        server = resolve_server_clock(adapter, now_utc, resolver, first=(symbol,))
    except Exception:                                                     # noqa: BLE001 - a clock lookup must never break analysis
        logger.exception("Could not resolve the broker clock")
        server = None
    now_server = (now_utc + server.offset_seconds) if server is not None and server.known else None
    clock = {"known": now_server is not None, "source": server.source if server else "unknown",
             "detail": server.detail if server else "The broker clock could not be read.", "now_server": now_server,
             "offset_seconds": server.offset_seconds if server else None}
    tick = None
    try:
        tick = adapter.get_symbol_spec(symbol).tick_size
    except (BrokerError, NotImplementedError, AttributeError):
        tick = None
    series = gather_series(adapter, symbol, now_server, rules)
    result = evaluate(symbol, series, strategy_config(settings, tick), clock=clock)
    return {"ok": True, "result": result.to_dict(), "connection": status.state.value}
