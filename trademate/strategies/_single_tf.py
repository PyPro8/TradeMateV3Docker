"""Shared plumbing for the single-timeframe, closed-candle strategies (Market Structure, Bull/Bear, Trendline): data check, gates, WAIT/refusal and the one result shape.

Pure and strategy-agnostic. It contains NO signal logic: each strategy module decides direction, entry, stop and target itself and only hands the finished numbers here.
The strategies do not read each other's output (Market Structure and Trendline in particular stay independent).
"""

from __future__ import annotations

from trademate.strategies.base import BEAR, BUY, BULL, SELL, WAIT, build_result, candidate_id, data_problems, finite, gate, geometry_problem, stage, unusable_result


class Run:
    def __init__(self, defn, symbol, series, params, clock):
        self.defn, self.symbol, self.series, self.params, self.clock = defn, symbol, series, params, clock
        self.tf = params.exec_tf
        self.tfs = (self.tf,)
        self.gates, self.reasons, self.missing, self.evidence, self.stages = [], [], [], [], {}
        self.ctx = {"state": defn.id, "label": "Not calculated", "direction": None, "relation": None, "strength": None, "summary": "", "lines": []}
        self.extra = {}                    # strategy-specific data attached to the result (e.g. the drawn lines)
        self.problems = data_problems(series, self.tfs)

    def unusable(self):
        return unusable_result(self.defn, self.symbol, self.problems, self.series, self.clock, self.params, self.tfs)

    def ok_gate(self, gid, detail):
        gate(self.gates, gid, True, detail)

    def _done(self, decision, direction, status, cands, analyses=None, **kw):
        res = build_result(self.defn, self.symbol, decision, direction, status, context=self.ctx, stages=self.stages, gates=self.gates, reasons=self.reasons,
                           missing=self.missing, evidence=self.evidence, analyses=analyses or {}, series=self.series, clock=self.clock, candidates=cands,
                           params=self.params, tfs=self.tfs, **kw)
        res.update(self.extra)
        return res

    def wait(self, gid, why, miss=None, context_met=False, analyses=None):
        gate(self.gates, gid, False, why)
        self.reasons.append(why)
        if miss:
            self.missing.append(miss)
        self.ctx["summary"] = self.ctx["summary"] or why
        self.stages.update(context=stage("met" if context_met else "not_met", self.ctx["summary"] or why), setup_candidate=stage("not_evaluated", "Not evaluated."),
                           confirmation=stage("not_evaluated", "Not evaluated."), trade_levels=stage("not_evaluated", "Not evaluated."))
        return self._done(WAIT, None, "inconclusive", [], analyses)

    def fixed_target(self, direction, entry):
        """-> (tp price, target_info) for a fixed pip target from the entry reference, or (None, reason). Never guesses a pip size."""
        p = self.params
        if p.target_pips is None:
            return None, "no fixed target distance is configured"
        if not p.pip_size:
            return None, p.pip_reason or "the instrument's pip size is unknown, so a pip target cannot be converted to a price"
        dist = p.target_pips * p.pip_size
        tp = entry + dist if direction == BULL else entry - dist
        return tp, {"mode": "pips", "pips": p.target_pips, "pip_size": p.pip_size}

    def signal(self, direction, entry, sl, tp, explanation, anchor, premise_target, tp_basis, sl_basis, target_info=None, analyses=None, min_rr=None, rr=None):
        cap = self.params.stop_pips * self.params.pip_size if (self.params.stop_pips and self.params.pip_size) else None
        if cap and abs(entry - sl) > cap:                                  # the user's protective-stop distance caps a structural stop that is farther away
            sl = entry - cap if direction == BULL else entry + cap
            sl_basis = f"{self.params.stop_pips:g} pips from the entry (your protective-stop distance; the structural stop was farther)"
        if rr:                                                             # chosen risk-to-reward: the target follows the FINAL stop distance (after the protective-stop cap)
            if not self.params.pip_size:
                return self.wait("target", "No target can be set: " + (self.params.pip_reason or "the instrument's pip size is unknown, so the ratio cannot be converted to a pip target") + ".", analyses=analyses)
            stop_dist = abs(entry - sl)
            tp = entry + rr * stop_dist if direction == BULL else entry - rr * stop_dist
            target_info = {"mode": "pips", "pips": rr * stop_dist / self.params.pip_size, "pip_size": self.params.pip_size, "risk_reward": rr}
            premise_target = f"Risk-to-reward 1:{rr:g}: {rr:g} x the stop distance."
            tp_basis = f"1:{rr:g} risk-to-reward ({rr:g} x the {stop_dist / self.params.pip_size:.4g}-pip stop distance), measured from the executable entry"
        geo = geometry_problem(direction, entry, sl, tp)
        if geo:
            return self.wait("trade_levels", "The calculated levels are inconsistent: " + geo + ".", analyses=analyses)
        risk, reward = abs(entry - sl), abs(tp - entry)
        word = "BUY" if direction == BULL else "SELL"
        self.ok_gate("trade_levels", f"Stop-loss {sl:g}, take-profit {tp:g}, reward-to-risk {reward / risk:.2f}.")
        self.evidence.append(explanation)
        self.stages.update(context=stage("met", self.ctx["summary"]), setup_candidate=stage("met", explanation), confirmation=stage("met", "The signal is on a confirmed (closed) candle."),
                           trade_levels=stage("met", f"Stop {sl:g}, target {tp:g}, reward-to-risk {reward / risk:.2f}."))
        inval = [f"Price reaches the stop-loss {sl:g}.", f"A newer {self.tf} candle closes (the signal is then no longer fresh and expires)."]
        cand = {"candidate_id": candidate_id(self.defn.id, direction, anchor), "setup_family": self.defn.id, "direction": direction, "status": "qualified", "premise": explanation,
                "supporting_evidence": [explanation], "conflicting_evidence": [], "missing_evidence": [], "invalidation_conditions": inval,
                "target_methodology": premise_target, "execution_eligible": True}
        entry_d = {"price": entry, "basis": f"close of the signal {self.tf} candle (reference; the real fill is the live ask/bid)", "zone": None}
        sl_d = {"price": sl, "basis": sl_basis}
        tp_d = {"price": tp, "basis": tp_basis, "rr": reward / risk}
        rr = {"risk": risk, "reward_tp1": reward, "rr_tp1": reward / risk, "reward_tp2": None, "rr_tp2": None, "min_rr": self.params.min_rr if min_rr is None else min_rr}
        inv = {"level": sl, "timeframe": self.tf, "basis": f"price reaching the stop-loss {sl:g} ends the {word} setup"}
        return self._done(BUY if direction == BULL else SELL, direction, "confirmed", [cand], analyses, entry=entry_d, sl=sl_d, tp1=tp_d, rr=rr, setup_inv=inv, target=target_info)


def closed_rows_problem(rows, need, tf, newest_closed_end=None, now=None):
    """A reason string if the closed candles cannot be used, else None."""
    if len(rows) < need:
        return f"Not enough history: {len(rows)} closed {tf} candles are available, {need} are needed."
    bad = next((i for i, r in enumerate(rows) if len(r) < 5 or not all(finite(v) for v in r[:5]) or r[2] < r[3] or r[2] < max(r[1], r[4]) or r[3] > min(r[1], r[4])), None)
    if bad is not None:
        return f"Closed candle #{bad + 1} of {len(rows)} is incomplete or inconsistent; no signal is calculated from it."
    if finite(now) and finite(newest_closed_end) and newest_closed_end > now + 1e-9:
        return f"The newest {tf} candle has not closed yet; it is not used."
    return None
