"""Strategy: EMA 20/50 + ATR (id "ema_atr").  RULE "EMA-ATR-v1".   One timeframe: the user's execution timeframe.

Uses ONLY confirmed (closed) candles. The forming candle is never read.

  Direction filter  EMA 20 > EMA 50: longs only.  EMA 20 < EMA 50: shorts only.  Equal: wait.
  Entry trigger     A CROSSOVER on the newest closed candle: EMA 20 moves from below EMA 50 to above it (BUY) or from above to below (SELL).
                    There is no other entry condition and no other indicator. Without a fresh crossover the decision is WAIT, whatever the direction filter says.
  Once only         The signal exists only while the crossover candle is the newest closed candle. Its identity (candidate id) is built from the timeframe and
                    that candle's open time, so repeated evaluations of the same candle are the SAME setup (the Watchdog does not re-announce it and the
                    setup ledger refuses a second order). The next closed candle is no longer a crossover.
  ATR               Wilder's Average True Range (default period 14) at the crossover candle: TR = max(high-low, |high-prev close|, |low-prev close|);
                    ATR = mean of the first `period` TRs, then (previous ATR * (period-1) + TR) / period. In price units: no pip or forex assumption.
  Exits             stop-loss = entry -/+ 2.0 x ATR, take-profit = entry +/- 4.0 x ATR (nominal reward-to-risk 2:1). Entry reference = the close of the crossover candle;
                    the pipeline re-checks the live price against it. Prices are snapped to the instrument's tick (stop away from entry, target never closer).
  EMA               alpha = 2 / (period + 1), seeded with the simple average of the first `period` closes.
"""

from __future__ import annotations

import math

from trademate.pips import snap_to_tick
from trademate.strategies.base import (BEAR, BUY, BULL, SELL, WAIT, build_result, candidate_id, data_problems, finite, gate, geometry_problem, stage,
                                      unusable_result)


def ema(values: list, period: int) -> list:
    """EMA aligned with `values` (None for the first period-1 entries). Needs len(values) >= period for any output."""
    out = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def atr(rows: list, period: int) -> list:
    """Wilder ATR aligned with `rows` ([t,o,h,l,c]); None until `period` true ranges exist (first value at index `period`)."""
    out = [None] * len(rows)
    if period < 1 or len(rows) <= period:
        return out
    tr = [None]
    for i in range(1, len(rows)):
        h, l, pc = rows[i][2], rows[i][3], rows[i - 1][4]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    prev = sum(tr[1:period + 1]) / period
    out[period] = prev
    for i in range(period + 1, len(rows)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def _sign(diff: float, scale: float) -> int:
    eps = 1e-12 * max(1.0, abs(scale))
    return 1 if diff > eps else -1 if diff < -eps else 0


def min_candles(params) -> int:
    """History needed before anything is calculated: three slow-EMA lengths so the EMA has settled, and enough for a settled ATR."""
    return max(params.ema_slow * 3, params.atr_period * 3 + 1)


def evaluate(defn, symbol: str, series: dict, params, cfg, clock: dict) -> dict:
    tf = params.exec_tf
    tfs = (tf,)
    problems = data_problems(series, tfs)
    if problems:
        return unusable_result(defn, symbol, problems, series, clock, params, tfs)
    s = series[tf]
    gates, reasons, missing, evidence, stages = [], [], [], [], {}
    gate(gates, "data", True, f"{tf} data is fresh, aligned and free of recent defects.")
    indicators = {"ema_fast_period": params.ema_fast, "ema_slow_period": params.ema_slow, "atr_period": params.atr_period, "ema_fast": None, "ema_slow": None, "atr": None,
                  "crossover": None, "signal_candle_time": None}

    def done(decision, direction, status, cands, ctx, **kw):
        res = build_result(defn, symbol, decision, direction, status, context=ctx, stages=stages, gates=gates, reasons=reasons, missing=missing, evidence=evidence,
                           analyses={}, series=series, clock=clock, candidates=cands, params=params, tfs=tfs, **kw)
        res["indicators"] = indicators
        return res

    ctx = {"state": "ema_filter", "label": "EMA filter not calculated", "direction": None, "relation": None, "strength": None, "summary": "", "lines": []}

    def refuse(gid, why, miss=None):
        gate(gates, gid, False, why)
        reasons.append(why)
        if miss:
            missing.append(miss)
        ctx["summary"] = ctx["summary"] or why
        stages.update(context=stage("not_met", why), setup_candidate=stage("not_evaluated", "Not evaluated."), confirmation=stage("not_evaluated", "Not evaluated."),
                      trade_levels=stage("not_evaluated", "Not evaluated."))
        return done(WAIT, None, "inconclusive", [], ctx)

    # ---- configuration sanity (a bad setting is refused, never silently replaced)
    if not (isinstance(params.ema_fast, int) and isinstance(params.ema_slow, int) and isinstance(params.atr_period, int)
            and 1 <= params.ema_fast < params.ema_slow and params.atr_period >= 1 and finite(params.atr_sl_mult) and finite(params.atr_tp_mult)
            and params.atr_sl_mult > 0 and params.atr_tp_mult > 0):
        return refuse("configuration", "The EMA / ATR settings are invalid (the fast EMA period must be below the slow one; the ATR period and both multipliers must be positive).")
    rows = list(s.closed)
    need = min_candles(params)
    if len(rows) < need:
        return refuse("history", f"Not enough history: {len(rows)} closed {tf} candles are available, {need} are needed (EMA {params.ema_slow} settled + ATR {params.atr_period}).",
                      f"{need - len(rows)} more closed {tf} candles")
    bad = next((i for i, r in enumerate(rows) if len(r) < 5 or not all(finite(v) for v in r[:5]) or r[2] < r[3] or r[2] < max(r[1], r[4]) or r[3] > min(r[1], r[4])), None)
    if bad is not None:
        return refuse("candles_valid", f"Closed candle #{bad + 1} of {len(rows)} is incomplete or inconsistent (missing or impossible open/high/low/close); no signal is calculated from it.")
    now = clock.get("now_server") if isinstance(clock, dict) else None
    if finite(now) and finite(s.newest_closed_end) and s.newest_closed_end > now + 1e-9:
        return refuse("candles_closed", f"The newest {tf} candle has not closed yet (it closes at {s.newest_closed_end:g}, server time is {now:g}); it is not used.")
    gate(gates, "history", True, f"{len(rows)} closed {tf} candles (need {need}).")
    gate(gates, "candles_valid", True, "Every candle used is complete: finite prices with high >= low.")

    closes = [r[4] for r in rows]
    fast, slow = ema(closes, params.ema_fast), ema(closes, params.ema_slow)
    i = len(rows) - 1
    f_now, s_now = fast[i], slow[i]
    indicators.update(ema_fast=f_now, ema_slow=s_now, signal_candle_time=rows[i][0])
    st_now = _sign(f_now - s_now, s_now)
    prev_st = 0
    for j in range(i - 1, params.ema_slow - 2, -1):          # the last candle BEFORE this one with a definite relationship
        prev_st = _sign(fast[j] - slow[j], slow[j])
        if prev_st != 0:
            break
    names = {1: ("Longs only", f"EMA {params.ema_fast} is above EMA {params.ema_slow}: longs only.", BULL),
             -1: ("Shorts only", f"EMA {params.ema_fast} is below EMA {params.ema_slow}: shorts only.", BEAR),
             0: ("Wait", f"EMA {params.ema_fast} equals EMA {params.ema_slow}: wait.", None)}
    label, text, ctx_dir = names[st_now]
    ctx.update(state="ema_filter", label=label, direction=ctx_dir, summary=f"{text} (EMA {params.ema_fast} {f_now:g}, EMA {params.ema_slow} {s_now:g}.)",
               lines=[f"EMA {params.ema_fast}: {f_now:g}", f"EMA {params.ema_slow}: {s_now:g}"])
    gate(gates, "ema_values", True, ctx["summary"])
    cross = "bullish" if (st_now == 1 and prev_st == -1) else "bearish" if (st_now == -1 and prev_st == 1) else None
    indicators["crossover"] = cross
    a_series = atr(rows, params.atr_period)
    a = a_series[i]
    indicators["atr"] = a
    if cross is None:
        why = text + " No crossover on the newest closed candle, so there is no entry trigger."
        gate(gates, "crossover", False, why)
        reasons.append(why)
        stages.update(context=stage("met", ctx["summary"]), setup_candidate=stage("not_met", "No EMA crossover on the newest closed candle."),
                      confirmation=stage("not_evaluated", "No crossover."), trade_levels=stage("not_evaluated", "No crossover."))
        return done(WAIT, None, "inconclusive", [], ctx)
    d = BULL if cross == "bullish" else BEAR
    word = "BUY" if d == BULL else "SELL"
    gate(gates, "crossover", True, f"EMA {params.ema_fast} crossed {'above' if d == BULL else 'below'} EMA {params.ema_slow} on the newest closed {tf} candle.")
    if not finite(a) or a <= 0:
        return refuse("atr_valid", f"A {cross} crossover occurred but the ATR ({a!r}) is zero or invalid, so no stop-loss or take-profit can be calculated. No trade.", "A valid, positive ATR")
    gate(gates, "atr_valid", True, f"ATR({params.atr_period}) = {a:g}.")
    entry = rows[i][4]
    sl_raw = entry - params.atr_sl_mult * a if d == BULL else entry + params.atr_sl_mult * a
    tp_raw = entry + params.atr_tp_mult * a if d == BULL else entry - params.atr_tp_mult * a
    tick = params.tick_size
    sl = snap_to_tick(sl_raw, tick, direction="down" if d == BULL else "up")        # the stop is never closer than calculated
    tp = snap_to_tick(tp_raw, tick, direction="up" if d == BULL else "down")        # the target is never closer than calculated
    geo = geometry_problem(d, entry, sl, tp)
    if geo:
        return refuse("trade_levels", "The calculated levels are inconsistent: " + geo + ".")
    risk, reward = abs(entry - sl), abs(tp - entry)
    gate(gates, "trade_levels", True, f"Stop-loss {sl:g} = {params.atr_sl_mult:g} x ATR from the entry; take-profit {tp:g} = {params.atr_tp_mult:g} x ATR.")
    explanation = (f"EMA {params.ema_fast} crossed {'above' if d == BULL else 'below'} EMA {params.ema_slow} on the newest closed {tf} candle "
                   f"(EMA {params.ema_fast} {f_now:g}, EMA {params.ema_slow} {s_now:g}). ATR({params.atr_period}) = {a:g}: stop {params.atr_sl_mult:g} x ATR, target {params.atr_tp_mult:g} x ATR.")
    evidence.append(explanation)
    stages.update(context=stage("met", ctx["summary"]), setup_candidate=stage("met", f"A {cross} EMA crossover on the newest closed candle."),
                  confirmation=stage("met", "The crossover is on a confirmed (closed) candle."),
                  trade_levels=stage("met", f"Stop {sl:g}, target {tp:g}, reward-to-risk {reward / risk:.2f}."))
    anchor = f"{tf}:{int(rows[i][0])}"
    inval = [f"Price reaches the stop-loss {sl:g}.", f"The opposite EMA {params.ema_fast}/{params.ema_slow} crossover closes on a later candle.",
             f"A newer {tf} candle closes (the crossover is then no longer fresh and the signal expires)."]
    cand = {"candidate_id": candidate_id(defn.id, d, anchor), "setup_family": defn.id, "direction": d, "status": "qualified", "premise": explanation,
            "supporting_evidence": [explanation], "conflicting_evidence": [], "missing_evidence": [], "invalidation_conditions": inval,
            "target_methodology": f"{params.atr_tp_mult:g} x ATR({params.atr_period}) from the entry reference.", "execution_eligible": True}
    entry_d = {"price": entry, "basis": f"close of the crossover {tf} candle (reference; the real fill is the live ask/bid)", "zone": None}
    sl_d = {"price": sl, "basis": f"{params.atr_sl_mult:g} x ATR({params.atr_period}) = {params.atr_sl_mult * a:g} from the entry reference"}
    tp_d = {"price": tp, "basis": f"{params.atr_tp_mult:g} x ATR({params.atr_period}) = {params.atr_tp_mult * a:g} from the entry reference", "rr": reward / risk}
    rr = {"risk": risk, "reward_tp1": reward, "rr_tp1": reward / risk, "reward_tp2": None, "rr_tp2": None, "min_rr": params.min_rr}
    inv = {"level": sl, "timeframe": tf, "basis": f"price reaching the {params.atr_sl_mult:g} x ATR stop-loss {sl:g} ends the {word} setup"}
    return done(BUY if d == BULL else SELL, d, "confirmed", [cand], ctx, entry=entry_d, sl=sl_d, tp1=tp_d, rr=rr, setup_inv=inv)
