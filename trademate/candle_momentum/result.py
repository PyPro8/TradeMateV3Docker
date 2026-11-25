"""Turns the engine's authoritative lifecycle into the result dict the rest of TradeMate already understands (DecisionHub, Watchdog alerts, the pipeline's
`qualify()` contract check, the Command Centre decision panel) PLUS a `candle_momentum` block with the whole lifecycle.

BUY / SELL is published only while the lifecycle is RISK_PENDING or EXECUTING (a validated, executable signal). A merely bullish or bearish candle, a
reached-but-unvalidated trigger, a missed / invalidated / rejected / expired entry, or an open position are never BUY / SELL, so no consumer can trade them.
"""

from __future__ import annotations

from trademate.candle_momentum.models import BEAR, BULL, ENGINE_VERSION, RULE_ID, STRATEGY_ID, State
from trademate.strategies.base import BUY, SELL, WAIT, WATCH, build_result, candidate_id, gate, stage
from trademate.strategy.timeframes import OK, STALE, UNAVAILABLE, PreparedSeries

S = State


def _decision(state: str | None, direction: str | None) -> tuple:
    """-> (decision, setup_status)"""
    if state in (S.RISK_PENDING.value, S.EXECUTING.value):
        return (BUY if direction == BULL else SELL), "confirmed"
    if state in (S.BULLISH.value, S.BEARISH.value, S.TRIGGER_REACHED.value, S.ENTRY_VALIDATION.value):
        return WATCH, "developing"
    if state in (S.POSITION_OPEN.value, S.TARGET_REACHED.value, S.POSITION_CLOSED.value):
        return WAIT, "executed"
    if state in (S.INVALIDATED.value, S.MISSED.value, S.REJECTED.value, S.EXECUTION_FAILED.value, S.EXPIRED.value):
        return WAIT, "invalidated"
    return WAIT, "inconclusive"


def build_result_dict(snap: dict, candle, spec, cfg, now: float, offset=None, feed_ok: bool = True, source: str = "engine") -> dict:
    from trademate.strategies.registry import CANDLE as defn
    m = (snap.get("machine") or {})
    lc = m.get("current")
    symbol, tf = cfg.symbol, cfg.timeframe
    state = lc["state"] if lc else None
    direction_word = lc.get("direction") if lc else None
    decision, status = _decision(state, direction_word)
    unit = getattr(spec, "unit", None)
    now_server = (now + offset) if offset is not None else None
    clock = {"known": now_server is not None, "source": "engine", "detail": "Broker clock offset measured by TradeMate." if offset is not None else "The broker clock is unknown.",
             "now_server": now_server, "offset_seconds": offset}
    newest_closed = getattr(candle, "newest_closed_time", None) if candle is not None else None
    series_status = OK if (feed_ok and candle is not None) else (STALE if candle is not None else UNAVAILABLE)
    series = {tf: PreparedSeries(timeframe=tf, status=series_status, reason="" if series_status == OK else "The live feed is not current.", forming=None, received=1 if candle else 0,
                                 newest_closed_time=newest_closed, clock_known=now_server is not None)}
    gates, reasons, missing = [], [], []
    ctx = {"state": "single_timeframe", "label": f"{tf} candle", "direction": None, "relation": None, "strength": None,
           "summary": f"Candle Momentum reads only the forming {tf} candle, tick by tick. No structure, trend or higher-timeframe context is used.", "lines": []}
    if lc is None:
        why = "No candle is being tracked yet (waiting for the broker's forming candle)."
        gate(gates, "forming_candle", False, why)
        reasons.append(why)
        res = build_result(defn, symbol, WAIT, None, "inconclusive", context=ctx, stages={"context": stage("met", ctx["summary"]), "setup_candidate": stage("not_evaluated", why),
                                                                                    "confirmation": stage("not_evaluated", why), "trade_levels": stage("not_evaluated", why)},
                           gates=gates, reasons=reasons, missing=["A forming candle from MT5"], evidence=[], analyses={}, series=series, clock=clock, candidates=[], params=None,
                           tfs=(tf,), target=None)
        res["candle_momentum"] = _block(snap, None, source)
        res["execution_timeframe"] = tf
        _attach_instrument(res, spec)
        return res
    v = lc.get("verdict") or {}
    why = lc.get("reason") or ""
    reasons.append(why)
    pass_ = decision in (BUY, SELL)
    gate(gates, "data", feed_ok, "The live quote and candle feed are current." if feed_ok else "The live feed is not current.")
    gate(gates, "candle_direction", direction_word is not None, f"The forming candle is {direction_word}." if direction_word else "The candle has no direction yet.")
    gate(gates, "trigger_reached", pass_ or state in (S.POSITION_OPEN.value, S.TARGET_REACHED.value, S.POSITION_CLOSED.value), why if not pass_ else
         f"Price reached the trigger {lc.get('trigger_price'):g}.")
    gate(gates, "entry_valid", pass_, v.get("reason") or why)
    if not feed_ok:
        pass_, decision = False, WAIT
    candidates = []
    cid = lc["candle_id"]
    cand_dir = direction_word or "neutral"
    candidates.append({"candidate_id": cid, "setup_family": defn.id, "direction": cand_dir, "status": "qualified" if pass_ else status, "premise": why,
                       "supporting_evidence": [], "conflicting_evidence": [], "missing_evidence": [] if pass_ else [why],
                       "invalidation_conditions": [f"Price returns inside the trigger {lc.get('trigger_price')} before the order is sent.", f"The {tf} candle closes."],
                       "target_methodology": f"{cfg.target_units:g} units from the actual fill price.", "execution_eligible": bool(pass_)})
    entry = sl = tp1 = rr = inv = target = None
    ref, slp, tpp = lc.get("entry_reference"), lc.get("sl_price"), lc.get("tp_preview")
    if pass_ and None not in (ref, slp, tpp) and unit:
        risk, reward = abs(ref - slp), abs(tpp - ref)
        entry = {"price": ref, "basis": "the executable quote (ask for a BUY, bid for a SELL) at the moment the entry was validated; the real fill is the live ask/bid", "zone": None}
        sl = {"price": slp, "basis": f"{cfg.sl_buffer_units:g} units beyond the candle open {lc['open']:g} (the existing candle-momentum protective stop)"}
        tp1 = {"price": tpp, "basis": f"{cfg.target_units:g} units from the executable entry (reference; the real target is set from the actual fill)", "rr": reward / risk if risk else None}
        rr = {"risk": risk, "reward_tp1": reward, "rr_tp1": reward / risk if risk else None, "reward_tp2": None, "rr_tp2": None, "min_rr": 0.0}
        inv = {"level": lc["trigger_price"], "timeframe": tf, "basis": "price back inside the trigger fails the momentum thesis"}
        slip = None
        if cfg.max_slippage_units is not None:
            try:
                slip = spec.units_to_price(cfg.max_slippage_units)
            except (ValueError, AttributeError):
                slip = None
        target = {"mode": "pips", "pips": cfg.target_units, "pip_size": unit, "max_slippage": slip}
    elif pass_:
        pass_, decision = False, WAIT
        candidates[0]["execution_eligible"] = False
    stages = {"context": stage("met", ctx["summary"]),
              "setup_candidate": stage("met" if direction_word else "not_met", why),
              "confirmation": stage("met" if pass_ else "not_met", v.get("reason") or why),
              "trade_levels": stage("met" if pass_ else "not_evaluated", "Stop and target are set from the validated executable quote." if pass_ else "Waiting for a validated entry.")}
    res = build_result(defn, symbol, decision, direction_word if decision in (BUY, SELL, WATCH) else None, "confirmed" if pass_ else status, context=ctx, stages=stages, gates=gates,
                       reasons=reasons, missing=[] if pass_ else [why], evidence=[v["reason"]] if pass_ and v.get("reason") else [], analyses={}, series=series, clock=clock,
                       candidates=candidates, entry=entry, sl=sl, tp1=tp1, rr=rr, setup_inv=inv, params=None, tfs=(tf,), target=target)
    res["execution_timeframe"] = tf
    res["primary_execution_timeframe"] = tf
    res["candle_momentum"] = _block(snap, lc, source)
    _attach_instrument(res, spec)
    return res


def _attach_instrument(res: dict, spec) -> None:
    """The same `pip` / `instrument_spec` blocks every other strategy's result carries (the Command Centre's pip card reads them)."""
    if spec is not None:
        res["pip"] = spec.to_pip_definition().to_dict()
        res["instrument_spec"] = spec.to_dict()


def _block(snap: dict, lc: dict | None, source: str) -> dict:
    m = snap.get("machine") or {}
    return {"engine_version": ENGINE_VERSION, "rule_id": RULE_ID, "source": source, "monitoring": snap.get("monitoring"), "engine": snap.get("engine"), "config": snap.get("config"),
            "conversion": snap.get("conversion"), "quote": snap.get("quote"), "feed": snap.get("feed"), "feed_problem": m.get("feed_problem"), "lifecycle": lc,
            "tracked_positions": m.get("tracked", []), "version": m.get("version"), "server_time": snap.get("server_time")}
