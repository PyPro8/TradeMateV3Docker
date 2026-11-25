"""Strategy B: Trend Continuation (id "trend").  RULE "TREND-H1M5-v1".   H1 = the trend, M5 = pullback and resumption.  Nothing else is read.

Enter WITH an established H1 trend after an M5 pullback, once closed M5 candles show the trend resuming. It does not use, wait for, or
depend on a CHoCH signal.

H1 trend (CONFIRMED swings only; provisional swings are ignored):
  bullish   the last two confirmed H1 swing highs are rising (HH) AND the last two confirmed swing lows are rising (HL), and the last H1 close
            is still above the latest confirmed swing low.
  bearish   the mirror image (LH + LL, last close below the latest confirmed swing high).
  unclear   anything else, including fewer than two confirmed swings of either kind. Unclear means WAIT.
M5 pullback (bullish; bearish is mirrored), from CONFIRMED M5 swings:
  impulse   L0 = the last confirmed swing low before H, H = the most recent confirmed swing high; leg = H - L0 > 0.
  pullback  P = the lowest closed-candle low after H. retrace = (H - P) / leg must be at least `pullback_min_retrace` (default 0.25) and
            P must stay above L0 (retrace < 1) and above the latest confirmed H1 swing low. Deeper than that is not a pullback: WAIT.
  P is a confirmed pullback low only once an M5 swing low has confirmed at/after it; before that the pullback is still in progress (WATCH).
Resumption (confirmation): a CLOSED M5 candle after P closes beyond H (bullish) / below the swing low (bearish).
  Fresh: at most `signal_max_age` closed candles ago. Failed: the latest close is back inside the old range or any later close broke P. WAIT.
  Provisional: only the forming candle is beyond H: WATCH, never a trade.
Entry reference = last closed M5 close. Stop = beyond P (+ buffer). Invalidation = a closed M5 candle beyond P.
Target = nearest confirmed H1/M5 swing (or liquidity pool) with reward-to-risk >= `min_rr`, or the measured move (P + leg), an ASSUMPTION not proven.
No CHoCH, order block, FVG, liquidity sweep, M15 or H4 is required.
"""

from __future__ import annotations

from trademate.strategies.base import (BEAR, BUY, BULL, CONTEXT_TF, SELL, SETUP_TF, WAIT, WATCH, build_result, candidate_id, confirmed_swings,
                                      data_problems, finite, gate, geometry_problem, pick_targets, run_analyses, stage, structural_levels,
                                      structural_stop, unusable_result)


def h1_trend(h1) -> dict:
    """The H1 trend from confirmed swings only."""
    highs, lows = confirmed_swings(h1, "high"), confirmed_swings(h1, "low")
    prov = sum(1 for s in h1.swing_highs + h1.swing_lows if s.get("confirmed") is not True)
    note = f" {prov} provisional swing(s) ignored." if prov else ""
    if len(highs) < 2 or len(lows) < 2:
        return {"direction": None, "label": "H1 unclear", "reason": f"Fewer than two confirmed H1 swing highs and lows.{note}", "highs": highs[-2:], "lows": lows[-2:]}
    h_prev, h_last, l_prev, l_last = highs[-2], highs[-1], lows[-2], lows[-1]
    close = h1.last_close
    if h_last["price"] > h_prev["price"] and l_last["price"] > l_prev["price"]:
        if finite(close) and close <= l_last["price"]:
            return {"direction": None, "label": "H1 unclear", "reason": f"Bullish swings, but the last H1 close {close:g} is not above the latest swing low {l_last['price']:g}: structure is broken.{note}",
                    "highs": [h_prev, h_last], "lows": [l_prev, l_last]}
        return {"direction": BULL, "label": "H1 bullish", "reason": f"Confirmed higher highs ({h_prev['price']:g} -> {h_last['price']:g}) and higher lows ({l_prev['price']:g} -> {l_last['price']:g}).{note}",
                "highs": [h_prev, h_last], "lows": [l_prev, l_last], "protect": l_last["price"]}
    if h_last["price"] < h_prev["price"] and l_last["price"] < l_prev["price"]:
        if finite(close) and close >= h_last["price"]:
            return {"direction": None, "label": "H1 unclear", "reason": f"Bearish swings, but the last H1 close {close:g} is not below the latest swing high {h_last['price']:g}: structure is broken.{note}",
                    "highs": [h_prev, h_last], "lows": [l_prev, l_last]}
        return {"direction": BEAR, "label": "H1 bearish", "reason": f"Confirmed lower highs ({h_prev['price']:g} -> {h_last['price']:g}) and lower lows ({l_prev['price']:g} -> {l_last['price']:g}).{note}",
                "highs": [h_prev, h_last], "lows": [l_prev, l_last], "protect": h_last["price"]}
    return {"direction": None, "label": "H1 unclear", "reason": f"Confirmed H1 swings are neither HH+HL nor LH+LL.{note}", "highs": [h_prev, h_last], "lows": [l_prev, l_last]}


def evaluate(defn, symbol: str, series: dict, params, cfg, clock: dict) -> dict:
    problems = data_problems(series)
    if problems:
        return unusable_result(defn, symbol, problems, series, clock, params)
    an = run_analyses(series, cfg, symbol)
    h1, m5 = an[CONTEXT_TF], an[SETUP_TF]
    if not m5.analyzed or not h1.analyzed:
        why = [f"{tf} could not be analysed ({an[tf].data_status}: {an[tf].data_reason or an[tf].bias_basis})" for tf in (CONTEXT_TF, SETUP_TF) if not an[tf].analyzed]
        return unusable_result(defn, symbol, why, series, clock, params)

    gates, reasons, missing, evidence, stages = [], [], [], [], {}
    gate(gates, "data", True, "H1 and M5 data are fresh, closed-candle based and free of recent defects.")
    trend = h1_trend(h1)
    d = trend["direction"]
    ctx = {"state": f"h1_{d}" if d else "h1_unclear", "label": trend["label"], "direction": d, "relation": None, "summary": trend["reason"],
           "lines": [f"H1: {trend['reason']}"], "strength": h1.strength}

    def finish(decision, direction, status, cands, **kw):
        return build_result(defn, symbol, decision, direction, status, context=ctx, stages=stages, gates=gates, reasons=reasons, missing=missing,
                            evidence=evidence, analyses=an, series=series, clock=clock, candidates=cands, params=params, **kw)

    if d is None:
        gate(gates, "context", False, trend["reason"])
        reasons.append("No H1 trend to continue. " + trend["reason"])
        missing.append("A confirmed H1 trend (higher highs and higher lows, or lower highs and lower lows)")
        stages.update(context=stage("not_met", trend["reason"]), setup_candidate=stage("not_evaluated", "Needs an H1 trend first."),
                      confirmation=stage("not_evaluated", "Needs an H1 trend first."), trade_levels=stage("not_evaluated", "Needs an H1 trend first."))
        return finish(WAIT, None, "inconclusive", [])
    gate(gates, "context", True, trend["reason"])
    evidence.append(trend["reason"])
    sign = 1 if d == BULL else -1
    rows = list(m5.rows)
    price = float(m5.last_close)
    hi_key, lo_key = (2, 3)       # row = (time, open, high, low, close)

    def beyond(a, b):             # a is further in the trend direction than b
        return (a - b) * sign > 0

    # ---------------- impulse leg and pullback (confirmed M5 swings)
    # bullish: H = swing high, L0 = swing low before it, pullback low P. bearish: mirror (H = swing low, L0 = swing high before it).
    top_kind, base_kind = ("high", "low") if d == BULL else ("low", "high")
    tops, bases = confirmed_swings(m5, top_kind), confirmed_swings(m5, base_kind)
    if not tops:
        return _no_pullback(finish, stages, gates, reasons, missing, ctx, "No confirmed M5 swing exists to define an impulse.", "A confirmed M5 impulse swing")
    H = tops[-1]
    before = [s for s in bases if s["time"] < H["time"]]
    if not before:
        return _no_pullback(finish, stages, gates, reasons, missing, ctx, f"No confirmed M5 swing {base_kind} before the impulse {top_kind} {H['price']:g}.", "A complete M5 impulse leg")
    L0 = before[-1]
    leg = (H["price"] - L0["price"]) * sign
    if leg <= 0:
        return _no_pullback(finish, stages, gates, reasons, missing, ctx, "The M5 impulse leg has no positive size.", "A valid M5 impulse leg")
    after_H = [r for r in rows if r[0] > H["time"]]
    if not after_H:
        return _no_pullback(finish, stages, gates, reasons, missing, ctx, "No closed M5 candle has formed since the impulse extreme.", "A pullback after the impulse")
    # pullback extreme P = most adverse closed-candle extreme after H
    P_price = min(r[lo_key] for r in after_H) if d == BULL else max(r[hi_key] for r in after_H)
    P_time = next(r[0] for r in after_H if (r[lo_key] == P_price if d == BULL else r[hi_key] == P_price))
    retrace = (H["price"] - P_price) * sign / leg
    protect = trend.get("protect")
    if retrace >= 1.0 or (finite(protect) and not beyond(P_price, protect)):
        msg = (f"The pullback to {P_price:g} went beyond the impulse start {L0['price']:g}" if retrace >= 1.0 else
               f"The pullback to {P_price:g} broke the H1 swing {protect:g}") + ": it is no longer a pullback in the trend."
        gate(gates, "setup_candidate", False, msg)
        reasons.append(msg)
        stages.update(setup_candidate=stage("not_met", msg), confirmation=stage("not_evaluated", "No valid pullback."), trade_levels=stage("not_evaluated", "No valid pullback."), context=stage("met", trend["reason"]))
        return finish(WAIT, None, "invalidated", [])
    if retrace < params.pullback_min_retrace:
        msg = f"Price has only retraced {retrace:.0%} of the M5 impulse leg (minimum {params.pullback_min_retrace:.0%}); there is no pullback yet."
        return _no_pullback(finish, stages, gates, reasons, missing, ctx, msg, f"A pullback of at least {params.pullback_min_retrace:.0%} of the impulse leg", wait=True)
    anchor = f"{int(H['time'])}:{H['price']:g}:{int(P_time)}"
    cand_missing = []
    pullback_confirmed = any(s["time"] >= P_time and (s["price"] == P_price) for s in bases) or any(s["time"] > P_time for s in bases)
    gate(gates, "setup_candidate", True, f"M5 pulled back {retrace:.0%} of its {d} impulse ({L0['price']:g} -> {H['price']:g}) to {P_price:g}.")
    evidence.append(f"M5 pullback of {retrace:.0%} against the {d} H1 trend, to {P_price:g}.")
    invalidation = [f"A closed M5 candle {'below' if d == BULL else 'above'} the pullback extreme {P_price:g}.",
                    f"A closed M5 candle {'below' if d == BULL else 'above'} the H1 swing {protect:g} (H1 trend structure breaks)." if finite(protect) else "The H1 trend structure breaks.",
                    f"More than {params.signal_max_age} M5 candles pass after the resumption break without entry (the signal expires)."]

    # ---------------- resumption: a CLOSED M5 candle beyond the impulse extreme after the pullback extreme
    break_idx = next((i for i, r in enumerate(rows) if r[0] > P_time and beyond(r[4], H["price"])), None)
    forming = series[SETUP_TF].forming
    if break_idx is None:
        prov = forming is not None and beyond(forming[hi_key if d == BULL else lo_key], H["price"])
        msg = f"No closed M5 candle has closed {'above' if d == BULL else 'below'} the impulse extreme {H['price']:g} since the pullback."
        cand_missing.append(f"A closed M5 candle {'above' if d == BULL else 'below'} {H['price']:g}")
        if not pullback_confirmed:
            cand_missing.append("A confirmed M5 pullback swing (the pullback may still be extending)")
        gate(gates, "confirmation", False, msg + (" The forming candle is beyond it, but is not closed (provisional)." if prov else ""))
        reasons.append(("A pullback is in progress. " if not pullback_confirmed else "The pullback is complete. ") + msg + (" The forming candle is already beyond it but may reverse." if prov else ""))
        missing.extend(cand_missing)
        stages.update(context=stage("met", trend["reason"]), setup_candidate=stage("met", "A pullback against the H1 trend exists."),
                      confirmation=stage("not_met", msg), trade_levels=stage("not_evaluated", "Needs the resumption confirmation first."))
        cand = _cand(defn, d, anchor, "developing", f"{d.capitalize()} H1 trend with an M5 pullback; resumption not yet confirmed.", evidence, cand_missing, invalidation)
        return finish(WATCH, d, "developing", [cand])
    age = len(rows) - 1 - break_idx
    broke_P = any(beyond(P_price, r[lo_key] if d == BULL else r[hi_key]) for r in rows[break_idx + 1:])
    if age > params.signal_max_age or broke_P or not beyond(price, H["price"]):
        msg = (f"The resumption break closed {age} M5 candles ago (limit {params.signal_max_age}); it has expired." if age > params.signal_max_age else
               f"Price fell back through the pullback extreme {P_price:g} after the break." if broke_P else
               f"The latest close {price:g} is back {'below' if d == BULL else 'above'} the impulse extreme {H['price']:g}: the breakout failed.")
        gate(gates, "confirmation", False, msg)
        reasons.append(msg)
        stages.update(context=stage("met", trend["reason"]), setup_candidate=stage("met", "A pullback existed."), confirmation=stage("not_met", msg),
                      trade_levels=stage("not_evaluated", "The setup expired or failed."))
        return finish(WAIT, None, "invalidated", [_cand(defn, d, anchor, "invalidated", "The resumption setup expired or failed.", [], [], [])])
    gate(gates, "confirmation", True, f"A closed M5 candle closed {'above' if d == BULL else 'below'} {H['price']:g} after the pullback ({age} candle(s) ago).")
    evidence.append(f"Trend resuming: a closed M5 candle closed {'above' if d == BULL else 'below'} the impulse extreme {H['price']:g}.")

    # ---------------- trade levels
    entry = {"price": price, "basis": "close of the last closed M5 candle (reference price; the real fill depends on the market)", "zone": None}
    sl_price, why = structural_stop(d, price, float(P_price), params.sl_buffer)
    if sl_price is None:
        gate(gates, "trade_levels", False, f"No valid structural stop: {why}.")
        reasons.append(f"No valid structural stop: {why}.")
        missing.append("A valid structural stop beyond the pullback extreme")
        stages.update(context=stage("met", trend["reason"]), setup_candidate=stage("met", "Pullback found."), confirmation=stage("met", "Resumption confirmed."),
                      trade_levels=stage("not_met", why))
        return finish(WATCH, d, "developing", [_cand(defn, d, anchor, "developing", "Resumption confirmed but no valid stop.", evidence, missing, invalidation)])
    levels = structural_levels(d, an) + [(P_price + leg * sign, "measured move (pullback extreme + impulse leg; an assumption)")]
    tp1, tp2, skipped = pick_targets(d, price, sl_price, levels, params.min_rr)
    if tp1 is None:
        near = ", ".join("%.2fR" % s["rr"] for s in skipped[:3])
        msg = f"No target gives at least {params.min_rr:g}R" + (f" (nearest levels give {near})." if skipped else " (no level ahead).")
        gate(gates, "trade_levels", False, msg)
        reasons.append(msg)
        missing.append(f"A target with reward-to-risk of at least {params.min_rr:g}")
        stages.update(context=stage("met", trend["reason"]), setup_candidate=stage("met", "Pullback found."), confirmation=stage("met", "Resumption confirmed."),
                      trade_levels=stage("not_met", msg))
        return finish(WATCH, d, "developing", [_cand(defn, d, anchor, "developing", "Resumption confirmed but the reward-to-risk is insufficient.", evidence, missing, invalidation)])
    geo = geometry_problem(d, price, sl_price, tp1["price"])
    if geo:
        gate(gates, "trade_levels", False, "Trade levels are inconsistent: " + geo + ".")
        reasons.append("Trade levels are inconsistent: " + geo + ".")
        stages.update(context=stage("met", trend["reason"]), setup_candidate=stage("met", "Pullback found."), confirmation=stage("met", "Resumption confirmed."),
                      trade_levels=stage("not_met", geo))
        return finish(WAIT, None, "inconclusive", [])
    sl = {"price": sl_price, "basis": f"beyond the M5 pullback extreme {P_price:g}" + (f" plus buffer {params.sl_buffer:g}" if params.sl_buffer else "")}
    risk = abs(price - sl_price)
    rr = {"risk": risk, "reward_tp1": abs(tp1["price"] - price), "rr_tp1": tp1["rr"], "reward_tp2": abs(tp2["price"] - price) if tp2 else None,
          "rr_tp2": tp2["rr"] if tp2 else None, "min_rr": params.min_rr}
    setup_inv = {"level": sl_price, "timeframe": SETUP_TF, "basis": f"a closed M5 candle {'below' if d == BULL else 'above'} the pullback extreme {P_price:g} invalidates the continuation"}
    gate(gates, "trade_levels", True, f"Stop {sl_price:g}, TP1 {tp1['price']:g} ({tp1['basis']}) gives {tp1['rr']:.2f}R (required {params.min_rr:g}R).")
    evidence.append(f"Structural stop {sl_price:g}; TP1 {tp1['price']:g} ({tp1['rr']:.2f}R).")
    stages.update(context=stage("met", trend["reason"]), setup_candidate=stage("met", "Pullback found."), confirmation=stage("met", "Resumption confirmed."),
                  trade_levels=stage("met", f"{tp1['rr']:.2f}R to TP1 with a structural stop."))
    cand = _cand(defn, d, anchor, "qualified", f"{d.capitalize()} H1 trend, M5 pullback and confirmed resumption.", evidence, [], invalidation, True)
    return finish(BUY if d == BULL else SELL, d, "confirmed", [cand], entry=entry, sl=sl, tp1=tp1, tp2=tp2, rr=rr, setup_inv=setup_inv)


def _no_pullback(finish, stages, gates, reasons, missing, ctx, msg, need, wait=True):
    gate(gates, "setup_candidate", False, msg)
    reasons.append(msg)
    missing.append(need)
    stages.update(context=stage("met", ctx["summary"]), setup_candidate=stage("not_met", msg), confirmation=stage("not_evaluated", "No pullback to confirm."),
                  trade_levels=stage("not_evaluated", "No pullback to build levels from."))
    return finish(WAIT, None, "inconclusive", [])


def _cand(defn, direction, anchor, status, premise, supporting, missing, invalidation, execution_eligible=False) -> dict:
    return {"candidate_id": candidate_id(defn.id, direction, anchor), "setup_family": defn.id, "direction": direction, "status": status,
            "premise": premise, "supporting_evidence": list(supporting), "conflicting_evidence": [], "missing_evidence": list(missing),
            "invalidation_conditions": list(invalidation),
            "target_methodology": "Nearest confirmed H1/M5 swing (or liquidity pool) with the required reward-to-risk, or the measured move (an assumption).",
            "execution_eligible": bool(execution_eligible)}
