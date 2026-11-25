"""Glue between the broker adapter and the selected strategy: fetch ONLY H1 and M5, measure the broker clock, evaluate. Read-only."""

from __future__ import annotations

import logging
import time

from trademate.broker.base import ConnectionState
from trademate.errors import BrokerError
from trademate.risk.account_state import resolve_server_clock
from trademate.pips import resolve_pip
from trademate.instruments import build_spec
from trademate.strategies.base import EXEC_TIMEFRAMES, REQUIRED_TIMEFRAMES, StrategyParams, timeframes_for
from trademate.strategies.registry import evaluate_strategy_result, get_any_strategy, get_strategy
from trademate.strategy.service import FETCH_COUNT, _unavailable, strategy_config
from trademate.strategy.timeframes import DataRules, prepare_series

logger = logging.getLogger("trademate.strategies")


_TS = {"provider": None}


def set_trade_settings_provider(fn) -> None:
    """fn(symbol, strategy_id) -> {key: value} of the SAVED Command Centre values only (built-in defaults never override settings.toml). None = no overrides."""
    _TS["provider"] = fn


def strategy_params(settings, tick_size=None, defn=None, exec_tf=None, pip=None, saved=None) -> StrategyParams:
    """Policy values for one evaluation. With a strategy definition the pip targets of THAT strategy are applied (CHoCH: 100, Candle Momentum: 10);
    `pip` is a trademate.pips.PipDefinition (its size, or the reason there is none, travels with the params)."""
    st = settings.strategy
    buffer = st.sl_buffer_ticks * tick_size if (st.sl_buffer_ticks and tick_size) else 0.0
    tf = exec_tf or st.default_exec_timeframe
    kw = dict(min_rr=st.min_rr, counter_min_rr=st.counter_min_rr, signal_max_age=st.signal_max_age, pullback_min_retrace=st.pullback_min_retrace,
              sl_buffer=buffer, exec_tf=tf, context_tf=timeframes_for(tf)[0])
    if pip is not None:
        kw.update(pip_size=pip.size if pip.ok else None, pip_reason="" if pip.ok else pip.reason)
    if defn is not None and defn.id == "choch":
        kw.update(target_pips=st.choch_target_pips, max_extension_fraction=st.choch_max_extension_fraction)
    elif defn is not None and defn.id == "candle_momentum":
        kw.update(target_pips=st.candle_target_pips)
    elif defn is not None and defn.id == "market_structure":
        kw.update(target_pips=st.market_structure_target_pips)
    elif defn is not None and defn.id == "bull_bear":
        kw.update(target_pips=st.bull_bear_target_pips)
    elif defn is not None and defn.id == "ema_atr":
        kw.update(ema_fast=st.ema_fast_period, ema_slow=st.ema_slow_period, atr_period=st.atr_period, atr_sl_mult=st.atr_sl_multiplier,
                  atr_tp_mult=st.atr_tp_multiplier, tick_size=tick_size)
    saved = saved or {}
    if defn is not None and defn.id in ("choch", "market_structure", "bull_bear") and saved.get("target_distance"):
        kw["target_pips"] = float(saved["target_distance"])                    # the user's saved target wins over settings.toml; Candle Momentum keeps its own config
    if defn is not None and defn.id == "market_structure" and saved.get("risk_reward"):
        kw["risk_reward"] = float(saved["risk_reward"])                         # the user's chosen ratio replaces the fixed pip target (validated when saved)
    if defn is not None and defn.id in ("market_structure", "bull_bear", "trendline"):
        kw["stop_pips"] = float(saved["stop_distance"]) if saved.get("stop_distance") else 100.0       # protective stop cap (default 100)
    return StrategyParams(**kw)


def _saved(symbol, strategy_id) -> dict:
    fn = _TS["provider"]
    try:
        return (fn(symbol, strategy_id) if fn else None) or {}
    except Exception:                                                      # noqa: BLE001 - settings trouble must never break analysis
        logger.exception("Could not read the saved trade settings")
        return {}


def _cfg(settings, tick):
    import dataclasses
    return dataclasses.replace(strategy_config(settings, tick), break_mode="close")          # both strategies confirm on closed candles


def evaluate_selected(adapter, strategy_id: str, symbol: str, settings, now_utc: float | None = None, resolver=None, exec_tf: str | None = None, pip_overrides: dict | None = None) -> dict:
    """{"ok", "result", "connection", "strategy", "exec_tf"}; never raises for market-data problems. Unknown strategy -> ok False (nothing is evaluated).
    `exec_tf` is the user's execution timeframe for this strategy (default: [strategy] default_exec_timeframe)."""
    defn = get_any_strategy(strategy_id)
    if defn is None:
        return {"ok": False, "result": None, "connection": None, "strategy": None,
                "message": "No valid strategy is selected. Choose Candle Momentum, CHoCH Trading, Market Structure Trading, Bull/Bear Trading or Trendline Trading."}
    exec_tf = exec_tf or settings.strategy.default_exec_timeframe
    if exec_tf not in EXEC_TIMEFRAMES:
        return {"ok": False, "result": None, "connection": None, "strategy": None, "message": f"Unknown timeframe {exec_tf!r}. Choose one of: {', '.join(EXEC_TIMEFRAMES)}."}
    if strategy_id == "trend":
        exec_tf = "M5"                         # Trend Continuation keeps its established H1 + M5 definition; it is not timeframe-selectable
    tfs = timeframes_for(exec_tf, defn.uses_context)
    now_utc = time.time() if now_utc is None else now_utc
    status = adapter.get_connection_status()
    if strategy_id == "bull_bear":
        return _evaluate_engine("bull_bear", adapter, defn, symbol, settings, now_utc, exec_tf, pip_overrides, status)
    if strategy_id == "candle_momentum":                                   # decided by the live tick engine, never from candle snapshots
        return _evaluate_candle_momentum(adapter, defn, symbol, settings, now_utc, exec_tf, pip_overrides, status)
    if status.state is not ConnectionState.CONNECTED:
        why = f"MT5 is not connected with fresh data ({status.message})."
        series = {tf: _unavailable(tf, why) for tf in tfs}
        clock = {"known": False, "source": "unknown", "detail": why, "now_server": None, "offset_seconds": None}
        result = evaluate_strategy_result(defn.id, symbol, series, strategy_params(settings, None, defn, exec_tf, saved=_saved(symbol, defn.id)), _cfg(settings, None), clock)
        return {"ok": True, "result": result, "connection": status.state.value, "strategy": defn.stamp(), "exec_tf": exec_tf}
    try:
        server = resolve_server_clock(adapter, now_utc, resolver, first=(symbol,))
    except Exception:                                                     # noqa: BLE001 - a clock lookup must never break analysis
        logger.exception("Could not resolve the broker clock")
        server = None
    now_server = (now_utc + server.offset_seconds) if server is not None and server.known else None
    clock = {"known": now_server is not None, "source": server.source if server else "unknown",
             "detail": server.detail if server else "The broker clock could not be read.", "now_server": now_server,
             "offset_seconds": server.offset_seconds if server else None}
    tick, pip = None, None
    try:
        info = adapter.get_symbol_spec(symbol)
        tick = info.tick_size
        # ONE conversion layer: the instrument engine decides the strategy unit for every asset class (forex, metals, crypto, indices, synthetics).
        spec = build_spec(info, pip_overrides if pip_overrides is not None else settings.pips.overrides, now_utc, symbol)
        pip = spec.to_pip_definition()
    except (BrokerError, NotImplementedError, AttributeError):
        tick = None
    rules = DataRules()
    series = {}
    for tf in tfs:                                                        # ONLY the strategy's own timeframes are ever requested
        try:
            rows = [c.to_list() for c in adapter.get_candles(symbol, tf, FETCH_COUNT[tf])]
        except (BrokerError, NotImplementedError) as exc:
            series[tf] = _unavailable(tf, f"{tf} candles could not be fetched: {exc}", now_server is not None)
            continue
        series[tf] = prepare_series(rows, tf, now_server, rules)
    result = evaluate_strategy_result(defn.id, symbol, series, strategy_params(settings, tick, defn, exec_tf, pip, saved=_saved(symbol, defn.id)), _cfg(settings, tick), clock)
    if pip is not None:
        result["pip"] = pip.to_dict()
        result["instrument_spec"] = spec.to_dict()
    result["fetched_at"] = now_utc
    return {"ok": True, "result": result, "connection": status.state.value, "strategy": defn.stamp(), "exec_tf": exec_tf}


def _evaluate_candle_momentum(adapter, defn, symbol, settings, now_utc, exec_tf, pip_overrides, status) -> dict:
    """The authoritative engine record when the engine is monitoring this selection; otherwise a read-only one-shot with the SAME rules (manual Analyse)."""
    from trademate.candle_momentum import get_engine
    from trademate.candle_momentum.diagnostics import EventLog
    from trademate.candle_momentum.engine import CandleMomentumEngine
    from trademate.candle_momentum.feed import AdapterFeed
    from trademate.candle_momentum.store import ConfigStore
    eng = get_engine()
    if eng is None or getattr(eng.feed, "adapter", None) is not adapter:
        eng = CandleMomentumEngine(AdapterFeed(adapter), settings, ConfigStore(None, EXEC_TIMEFRAMES), None, EventLog(None, 100),
                                   pip_overrides=lambda: pip_overrides if pip_overrides is not None else settings.pips.overrides)
    res = eng.result(symbol, exec_tf, now_utc) if eng.is_monitoring(symbol, exec_tf) else eng.evaluate_once(symbol, exec_tf, now_utc)
    res["fetched_at"] = now_utc
    return {"ok": True, "result": res, "connection": status.state.value, "strategy": defn.stamp(), "exec_tf": exec_tf}


def _evaluate_engine(pkg_name, adapter, defn, symbol, settings, now_utc, exec_tf, pip_overrides, status) -> dict:
    """Bull/Bear: the same hand-off as Candle Momentum, to the Bull/Bear engine (the authoritative record while monitoring, else a read-only one-shot)."""
    import importlib
    pkg = importlib.import_module(f"trademate.{pkg_name}")
    eng = pkg.get_engine()
    if eng is None or getattr(eng.feed, "adapter", None) is not adapter:
        diag = importlib.import_module(f"trademate.{pkg_name}.diagnostics")
        eng_mod = importlib.import_module(f"trademate.{pkg_name}.engine")
        feed = importlib.import_module(f"trademate.{pkg_name}.feed")
        store = importlib.import_module(f"trademate.{pkg_name}.store")
        eng = eng_mod.BullBearEngine(feed.AdapterFeed(adapter), settings, store.ConfigStore(None, EXEC_TIMEFRAMES), None, diag.EventLog(None, 100),
                                     pip_overrides=lambda: pip_overrides if pip_overrides is not None else settings.pips.overrides)
    res = eng.result(symbol, exec_tf, now_utc) if eng.is_monitoring(symbol, exec_tf) else eng.evaluate_once(symbol, exec_tf, now_utc)
    res["fetched_at"] = now_utc
    return {"ok": True, "result": res, "connection": status.state.value, "strategy": defn.stamp(), "exec_tf": exec_tf}
