"""Strategy A: CHoCH Trading (id "choch").  RULE "CHOCH-H1M5-v1".   H1 = context, M5 = signal.  Nothing else is read.

A Change of Character (CHoCH) is a POTENTIAL change in market behaviour. It is not proof that a reversal will succeed.

Precise definition used here (all on CLOSED M5 candles; the break is close-based, never a wick):
  CHoCH       the structure engine reports a confirmed break (type CHOCH) of a CONFIRMED swing in the direction opposite to the previous
              M5 trend, and the break is the LATEST closed M5 break. Same-candle conflicts and breaks whose reference swing was not yet
              confirmed when the candle closed are rejected.
  Fresh       at most `signal_max_age` closed M5 candles have closed since the CHoCH candle. Older: expired (WAIT).
  Failed      any closed M5 candle after the CHoCH closed back through the CHoCH level (or the latest close is not beyond it): WAIT.
  Superseded  a later closed break (a BOS) already followed the CHoCH: the entry opportunity after the CHoCH has passed (WAIT).
  Provisional the forming M5 candle currently shows an opposite break: WATCH only, never BUY/SELL, never used for the decision.
Context (H1): informs, does not veto. H1 bias aligned / counter / unclear is reported. The ONLY restriction: a CHoCH AGAINST an H1 direction
  needs reward-to-risk >= `counter_min_rr` (default 2.0) instead of `min_rr`. Unusable H1 data means no decision (WAIT), never "neutral".
Entry     reference = last closed M5 close (a reference price, not the fill price).
Stop      the structure engine's protected swing for the new direction (confirmed, unbroken), plus the configured buffer.
Target    nearest confirmed H1/M5 swing (or active liquidity pool) beyond entry whose reward-to-risk meets the requirement.
No order block, FVG, liquidity sweep, M15 or H4 is required.
"""

from __future__ import annotations

from trademate.strategies.base import (BEAR, BUY, BULL, CONTEXT_TF, SELL, SETUP_TF, WAIT, WATCH, build_result, candidate_id, data_problems,
                                      finite, gate, geometry_problem, pick_targets, relabel, run_analyses, stage, structural_levels,
                                      structural_stop, timeframes_for, unusable_result)


def _context(h1, direction: str | None) -> dict:
    if not h1.analyzed or h1.bias not in (BULL, BEAR):
        return {"state": "h1_unclear", "label": "H1 unclear", "direction": None, "relation": "unclear", "strength": None,
                "summary": "H1 shows no clear direction. A CHoCH is judged on its own merits with the normal reward-to-risk requirement.",
                "lines": [f"H1: {h1.bias_basis}"]}
    relation = "aligned" if direction == h1.bias else "counter" if direction in (BULL, BEAR) else "unknown"
    base = f"H1 is {h1.bias} ({h1.strength})."
    extra = {"aligned": " The M5 CHoCH points the same way.",
             "counter": " The M5 CHoCH points against it: allowed, but it needs a higher reward-to-risk.",
             "unknown": " No M5 CHoCH is under review."}[relation]
    return {"state": f"h1_{h1.bias}", "label": f"H1 {h1.bias}", "direction": h1.bias, "relation": relation, "strength": h1.strength,
            "summary": base + extra, "lines": [f"H1: {h1.bias_basis}"]}


def evaluate(defn, symbol: str, series: dict, params, cfg, clock: dict) -> dict:
    """The user's execution timeframe is params.exec_tf (default M5, the established behaviour); its context timeframe is derived from it.
    With params.target_pips set the take-profit is that many pips from the actual entry (Strategy B: 100) instead of a structural level;
    the structural stop is unchanged and calculated independently."""
    set_tf = params.exec_tf
    tfs = timeframes_for(set_tf)
    ctx_tf = tfs[0]
    problems = data_problems(series, tfs)
    if problems:
        return relabel(unusable_result(defn, symbol, problems, series, clock, params, tfs), ctx_tf, set_tf)
    an = run_analyses(series, cfg, symbol, tfs)
    h1, m5 = an[ctx_tf], an[set_tf]
    if not m5.analyzed or not h1.analyzed:
        why = [f"{tf} could not be analysed ({an[tf].data_status}: {an[tf].data_reason or an[tf].bias_basis})" for tf in tfs if not an[tf].analyzed]
        return unusable_result(defn, symbol, why, series, clock, params, tfs)
    fixed = params.target_pips is not None
    target_info = {"mode": "pips", "pips": params.target_pips, "pip_size": params.pip_size} if fixed else None

    gates, reasons, missing, evidence = [], [], [], []
    gate(gates, "data", True, "H1 and M5 data are fresh, closed-candle based and free of recent defects.")
    rows = list(m5.rows)
    last_close = m5.last_close
    ev = m5.events[-1] if m5.events else None
    stages = {}

    def finish(decision, direction, status, ctx, cands, **kw):
        return relabel(build_result(defn, symbol, decision, direction, status, context=ctx, stages=stages, gates=gates, reasons=reasons, missing=missing,
                                    evidence=evidence, analyses=an, series=series, clock=clock, candidates=cands, params=params, tfs=tfs,
                                    target=target_info, **kw), ctx_tf, set_tf)

    # ---------------- setup candidate: the latest closed M5 break must be a valid, fresh, unfailed CHoCH
    valid = (ev is not None and ev.get("type") == "CHOCH" and ev.get("confirmed") is True and ev.get("same_candle_conflict") is not True
             and ev.get("direction") in (BULL, BEAR) and finite(ev.get("level")) and finite(ev.get("candle_time"))
             and finite(ev.get("reference_pivot_confirmed_time")) and ev["reference_pivot_confirmed_time"] < ev["candle_time"])
    if not valid:
        ctx = _context(h1, None)
        if ev is not None and ev.get("type") == "BOS":
            note = (f"The latest closed M5 break is a {ev['direction']} BOS (continuation), not a CHoCH. If a CHoCH came before it, the entry "
                    "opportunity after that CHoCH has passed.")
        elif ev is not None and ev.get("type") == "CHOCH":
            note = "The latest M5 CHoCH is not usable: it was a same-candle conflict or its reference swing was not confirmed when it closed."
        else:
            note = "No closed M5 structure break exists yet."
        # provisional (forming candle) evidence: WATCH, never a trade
        intr = m5.intrabar
        if intr.get("present") and intr.get("direction") in (BULL, BEAR) and intr["direction"] != m5.bias:
            d = intr["direction"]
            gate(gates, "setup_candidate", False, "Only a provisional, unclosed M5 break exists: " + intr["note"])
            reasons.append(f"A possible {d} CHoCH is forming on the unclosed M5 candle. It is provisional and may vanish before the candle closes.")
            missing.append("A closed M5 candle that confirms the break")
            stages.update(context=stage("met", ctx["summary"]), setup_candidate=stage("not_met", "Only a provisional (unclosed) break exists."),
                          confirmation=stage("not_met", "The break must be confirmed by a CLOSED M5 candle."),
                          trade_levels=stage("not_evaluated", "Not evaluated until the break is confirmed."))
            cand = _candidate(defn, d, f"provisional:{intr.get('direction')}:{int(rows[-1][0])}", "developing",
                              f"A possible {d} CHoCH is forming on the unclosed M5 candle.", [], missing, [])
            return finish(WATCH, d, "developing", ctx, [cand])
        gate(gates, "setup_candidate", False, note)
        reasons.append("No confirmed M5 CHoCH is available. " + note)
        stages.update(context=stage("met", ctx["summary"]), setup_candidate=stage("not_met", note), confirmation=stage("not_evaluated", "No CHoCH to confirm."),
                      trade_levels=stage("not_evaluated", "No CHoCH to build levels from."))
        return finish(WAIT, None, "inconclusive", ctx, [])

    d, level, ctime = ev["direction"], float(ev["level"]), float(ev["candle_time"])
    ctx = _context(h1, d)
    after = [r for r in rows if r[0] > ctime]
    age = len(after)
    word = "above" if d == BULL else "below"
    anchor = f"{int(ctime)}:{level:g}"
    cid_missing = []
    if age > params.signal_max_age:
        msg = f"The {d} CHoCH through {level:g} closed {age} M5 candles ago; it is older than {params.signal_max_age} and has expired."
        gate(gates, "setup_candidate", False, msg)
        reasons.append(msg)
        stages.update(context=stage("met", ctx["summary"]), setup_candidate=stage("not_met", msg), confirmation=stage("not_evaluated", "Signal expired."),
                      trade_levels=stage("not_evaluated", "Signal expired."))
        return finish(WAIT, None, "invalidated", ctx, [_candidate(defn, d, anchor, "invalidated", f"The {d} CHoCH through {level:g} expired.", [], [], [])])
    failed = any((r[4] <= level) if d == BULL else (r[4] >= level) for r in after) or not ((last_close > level) if d == BULL else (last_close < level))
    if failed:
        msg = f"A closed M5 candle returned {'below' if d == BULL else 'above'} the CHoCH level {level:g}: the {d} CHoCH failed."
        gate(gates, "setup_candidate", False, msg)
        reasons.append(msg)
        stages.update(context=stage("met", ctx["summary"]), setup_candidate=stage("not_met", msg), confirmation=stage("not_met", "The CHoCH did not hold."),
                      trade_levels=stage("not_evaluated", "The setup failed."))
        return finish(WAIT, None, "invalidated", ctx, [_candidate(defn, d, anchor, "invalidated", f"The {d} CHoCH through {level:g} failed.", [], [], [])])
    if fixed and params.pip_size:
        moved = abs(last_close - level) / params.pip_size
        limit = params.max_extension_fraction * params.target_pips
        if moved > limit:
            msg = (f"The {d} CHoCH through {level:g} has already played out: price is {moved:.1f} pips beyond it, more than "
                   f"{params.max_extension_fraction:.0%} of the {params.target_pips:g}-pip target ({limit:.1f}). The target distance is no longer reasonably available.")
            gate(gates, "setup_candidate", False, msg)
            reasons.append(msg)
            stages.update(context=stage("met", ctx["summary"]), setup_candidate=stage("not_met", msg), confirmation=stage("not_evaluated", "Signal already played out."),
                          trade_levels=stage("not_evaluated", "Signal already played out."))
            return finish(WAIT, None, "invalidated", ctx, [_candidate(defn, d, anchor, "invalidated", f"The {d} CHoCH through {level:g} already played out.", [], [], [])])
    gate(gates, "setup_candidate", True, f"Closed M5 {d} CHoCH through {level:g}, {age} candle(s) ago (limit {params.signal_max_age}).")
    gate(gates, "confirmation", True, f"The break was confirmed by a closed candle and every close since is {word} {level:g}.")
    evidence.append(f"M5 closed a {d} CHoCH through {level:g} (reference swing already confirmed); price still holds {word} it.")
    evidence.append(ctx["summary"])

    # ---------------- context restriction (explicit): counter-H1 needs a higher reward-to-risk
    required_rr = max(params.min_rr, params.counter_min_rr) if ctx["relation"] == "counter" else params.min_rr
    if ctx["relation"] == "counter":
        evidence.append(f"Counter-H1 CHoCH: the reward-to-risk requirement is {required_rr:g} instead of {params.min_rr:g}.")

    # ---------------- trade levels
    inv = m5.invalidation
    raw = inv.get("level") if isinstance(inv, dict) else None
    price = float(last_close)
    entry = {"price": price, "basis": "close of the last closed M5 candle (reference price; the real fill depends on the market)", "zone": None}
    sl = tp1 = tp2 = rr = setup_inv = None
    levels_ok = False
    why_missing = None
    if not finite(raw):
        why_missing = f"No confirmed, unbroken M5 swing {'low' if d == BULL else 'high'} exists yet to place a structural stop."
        missing.append("A confirmed M5 swing to protect (structural stop)")
    else:
        sl_price, why = structural_stop(d, price, float(raw), params.sl_buffer)
        if sl_price is None:
            why_missing = f"No valid structural stop: {why}."
            missing.append("A valid structural stop on the loss side of the entry")
        else:
            if fixed:
                if not params.pip_size:
                    tp1, skipped = None, []
                    why_missing = params.pip_reason or "The pip size of this instrument is unknown, so the pip target cannot be placed."
                    missing.append("A defined pip size for this instrument")
                    levels = None
                else:
                    dist = params.target_pips * params.pip_size
                    tp_price = price + dist if d == BULL else price - dist
                    tp1 = {"price": tp_price, "basis": f"fixed {params.target_pips:g} pips from the entry (reference; the real target is set from the executable entry)",
                           "rr": dist / abs(price - sl_price) if abs(price - sl_price) > 0 else 0.0}
                    tp2, skipped, required_rr = None, [], 0.0
                    levels = []
            else:
                levels = structural_levels(d, an, tfs)
                tp1, tp2, skipped = pick_targets(d, price, sl_price, levels, required_rr)
            if why_missing:
                pass
            elif tp1 is None:
                near = ", ".join("%.2fR" % s["rr"] for s in skipped[:3])
                why_missing = f"No structural H1/M5 target gives at least {required_rr:g}R" + (f" (nearest levels give {near})." if skipped else " (no level ahead).")
                missing.append(f"A structural target with reward-to-risk of at least {required_rr:g}")
            else:
                geo = geometry_problem(d, price, sl_price, tp1["price"])
                if geo:
                    why_missing = "Trade levels are inconsistent: " + geo + "."
                else:
                    sl = {"price": sl_price, "basis": f"beyond the protected M5 swing {float(raw):g}" + (f" plus buffer {params.sl_buffer:g}" if params.sl_buffer else "")}
                    risk = abs(price - sl_price)
                    rr = {"risk": risk, "reward_tp1": abs(tp1["price"] - price), "rr_tp1": tp1["rr"], "reward_tp2": abs(tp2["price"] - price) if tp2 else None,
                          "rr_tp2": tp2["rr"] if tp2 else None, "min_rr": required_rr}
                    setup_inv = {"level": sl_price, "timeframe": set_tf,
                                 "basis": f"a closed M5 candle {'below' if d == BULL else 'above'} {float(raw):g} (the protected swing) invalidates the {d} CHoCH setup"}
                    levels_ok = True
    invalidation = [f"A closed M5 candle back {'below' if d == BULL else 'above'} the CHoCH level {level:g}.",
                    f"A closed M5 candle {'below' if d == BULL else 'above'} the protected swing" + (f" {float(raw):g}." if finite(raw) else "."),
                    f"More than {params.signal_max_age} M5 candles pass without entry (the signal expires)."]
    if not levels_ok:
        gate(gates, "trade_levels", False, why_missing)
        reasons.append(why_missing)
        stages.update(context=stage("met", ctx["summary"]), setup_candidate=stage("met", "A fresh closed M5 CHoCH exists."),
                      confirmation=stage("met", "Closed-candle confirmation holds."), trade_levels=stage("not_met", why_missing))
        cand = _candidate(defn, d, anchor, "developing", f"A confirmed {d} CHoCH exists but its trade levels are incomplete.", evidence, missing, invalidation)
        return finish(WATCH, d, "developing", ctx, [cand])
    gate(gates, "trade_levels", True, f"Stop {sl['price']:g}, TP1 {tp1['price']:g} ({tp1['basis']}) gives {tp1['rr']:.2f}R (required {required_rr:g}R).")
    evidence.append(f"Structural stop {sl['price']:g}; TP1 {tp1['price']:g} ({tp1['rr']:.2f}R).")
    stages.update(context=stage("met", ctx["summary"]), setup_candidate=stage("met", "A fresh closed M5 CHoCH exists."),
                  confirmation=stage("met", "Closed-candle confirmation holds."), trade_levels=stage("met", f"{tp1['rr']:.2f}R to TP1 with a structural stop."))
    cand = _candidate(defn, d, anchor, "qualified", f"Confirmed {d} CHoCH on M5 with valid structural levels.", evidence, [], invalidation, execution_eligible=True)
    decision = BUY if d == BULL else SELL
    return finish(decision, d, "confirmed", ctx, [cand], entry=entry, sl=sl, tp1=tp1, tp2=tp2, rr=rr, setup_inv=setup_inv)


def _candidate(defn, direction, anchor, status, premise, supporting, missing, invalidation, execution_eligible=False) -> dict:
    return {"candidate_id": candidate_id(defn.id, direction, anchor), "setup_family": defn.id, "direction": direction, "status": status,
            "premise": premise, "supporting_evidence": list(supporting), "conflicting_evidence": [], "missing_evidence": list(missing),
            "invalidation_conditions": list(invalidation),
            "target_methodology": "Nearest confirmed H1/M5 swing (or active liquidity pool) beyond entry that meets the required reward-to-risk.",
            "execution_eligible": bool(execution_eligible)}
