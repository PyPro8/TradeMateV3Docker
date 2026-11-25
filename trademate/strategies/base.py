"""Shared plumbing for the two selectable strategies (pure: candles in, a result dict out; no broker, no risk sizing, no orders).

Both strategies use ONLY H1 (context) and M5 (setup + confirmation). They share data preparation, the existing structure analysis
(`trademate.strategy.mtf.analyze_timeframe`), the stop/target helpers and the result shape. Their qualification rules live in
choch.py and trend.py and share nothing: neither strategy reads the other's signal.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from trademate.strategy.mtf import BEAR, BULL, StrategyConfig, TimeframeAnalysis, analyze_timeframe, pick_targets, structural_stop  # noqa: F401
from trademate.strategy.timeframes import OK, USABLE, PreparedSeries

BUY, SELL, WAIT, WATCH = "BUY", "SELL", "WAIT", "WATCH"
REQUIRED_TIMEFRAMES = ("H1", "M5")          # the DEFAULT pair (context, execution) of the context-reading strategies
CONTEXT_TF, SETUP_TF = "H1", "M5"
EXEC_TIMEFRAMES = ("M1", "M2", "M5", "M15", "M30", "H1", "H4")   # what the user may choose as a strategy's execution timeframe (the bridge serves all six)
# H1 context for M5/M15 (the established pair); the next timeframe up for the others. A timeframe is its own context when nothing is above it.
CONTEXT_FOR = {"M1": "M15", "M2": "M15", "M5": "H1", "M15": "H1", "M30": "H4", "H1": "H4", "H4": "H4"}


def timeframes_for(exec_tf: str, uses_context: bool = True) -> tuple:
    """The distinct timeframes a strategy reads for the chosen execution timeframe, context first."""
    ctx = CONTEXT_FOR.get(exec_tf, "H1")
    return tuple(dict.fromkeys((ctx, exec_tf))) if uses_context else (exec_tf,)

DISCLAIMER = ("A strategy proposes; it is a hypothesis, not proof of profit, and no win rate is implied. Position size and approval belong to the "
              "Risk Engine and orders to the Execution Engine; nothing here sends an order.")


@dataclass(frozen=True)
class StrategyParams:
    """Policy values of the two strategies ([strategy] in settings.toml). None of them loosens the Risk Engine."""

    min_rr: float = 1.5                 # minimum reward-to-risk of TP1
    counter_min_rr: float = 2.0         # CHoCH AGAINST the H1 direction needs at least this reward-to-risk (the only H1 restriction)
    signal_max_age: int = 12            # closed M5 candles a CHoCH / a pullback-resumption break stays actionable
    pullback_min_retrace: float = 0.25  # a pullback must retrace at least this fraction of the M5 impulse leg to count
    sl_buffer: float = 0.0              # absolute price added beyond the structural stop (0 = none)
    exec_tf: str = "M5"                 # the user's execution timeframe (default keeps the established H1 + M5 behaviour)
    context_tf: str = "H1"              # context timeframe read together with exec_tf (derived from exec_tf by the service)
    target_pips: float | None = None    # fixed take-profit distance in pips from the ACTUAL entry (None = the strategy's own structural target)
    risk_reward: float | None = None    # Market Structure only: take-profit = this x the FINAL stop distance (1:N). None = use target_pips
    pip_size: float | None = None       # price units per pip of the selected instrument (from trademate.pips); required when target_pips is set
    stop_pips: float | None = None      # the user's protective-stop distance in pips: caps a structural stop that is FARTHER than this (Market Structure, Bull/Bear, Trendline)
    pip_reason: str = ""                # why pip_size is unavailable (shown to the user) when it is None
    max_extension_fraction: float = 0.5  # CHoCH with a fixed pip target: refused once price has already travelled this fraction of the target beyond the CHoCH level
    ema_fast: int = 20                  # EMA + ATR strategy only (the fields below are ignored by every other strategy)
    ema_slow: int = 50
    atr_period: int = 14
    atr_sl_mult: float = 2.0            # stop-loss distance = this x ATR
    atr_tp_mult: float = 4.0            # take-profit distance = this x ATR
    tick_size: float | None = None      # the instrument's price step: EMA + ATR prices are snapped to it


@dataclass(frozen=True)
class StrategyDefinition:
    id: str
    name: str
    version: str
    rule_id: str
    summary: str
    description: str
    emits_choch_alerts: bool
    choch_alert_timeframes: tuple
    evaluate: object = None             # callable(symbol, series, params, cfg, clock) -> dict (set by the registry)
    uses_context: bool = True           # reads a context timeframe above the execution timeframe
    fixed_target_pips: float | None = None   # informational default of the strategy's pip target (settings.toml is authoritative)
    tooltip: str = ""                    # the concise hover / tap explanation shown beside the strategy name

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "version": self.version, "rule_id": self.rule_id, "summary": self.summary,
                "description": self.description, "timeframes": list(REQUIRED_TIMEFRAMES) if self.uses_context else [SETUP_TF],
                "timeframe_roles": ({CONTEXT_TF: "market context", SETUP_TF: "setup and confirmation"} if self.uses_context else {SETUP_TF: "the forming candle"}),
                "uses_context": self.uses_context, "exec_timeframes": list(EXEC_TIMEFRAMES), "fixed_target_pips": self.fixed_target_pips,
                "tooltip": self.tooltip}

    def stamp(self) -> dict:
        return {"id": self.id, "name": self.name, "version": self.version, "rule_id": self.rule_id, "timeframes": list(REQUIRED_TIMEFRAMES),
                "emits_choch_alerts": self.emits_choch_alerts, "choch_alert_timeframes": list(self.choch_alert_timeframes)}


def finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def gate(gates: list, gid: str, passed: bool, detail: str, evaluated: bool = True) -> bool:
    gates.append({"id": gid, "passed": bool(passed), "detail": detail, "evaluated": evaluated})
    return bool(passed)


def candidate_id(strategy_id: str, direction: str, anchor) -> str:
    return hashlib.sha256("|".join((strategy_id, direction, str(anchor))).encode()).hexdigest()[:20]


def data_problems(series: dict, tfs: tuple | None = None) -> list:
    """What is wrong with the two required series (empty = both are fresh, aligned, sufficient and defect-free). Never read as evidence."""
    out = []
    for tf in (tfs or REQUIRED_TIMEFRAMES):
        s = series.get(tf)
        if s is None:
            out.append(f"{tf} data is missing.")
        elif s.status != OK:
            out.append(f"{tf} data is {s.status}: {s.reason}")
    return out


def confirmed_swings(analysis: TimeframeAnalysis, kind: str) -> list:
    """Confirmed swings only (a swing is confirmed once `swing_right` candles have closed after it), oldest first."""
    src = analysis.swing_highs if kind == "high" else analysis.swing_lows
    return sorted((s for s in src if s.get("confirmed") is True and finite(s.get("price")) and finite(s.get("time"))), key=lambda s: s["time"])


def structural_levels(direction: str, analyses: dict, tfs: tuple | None = None) -> list:
    """Target candidates in the trade direction from H1 and M5 ONLY: confirmed swings and active liquidity pools (as targets, never as a gate)."""
    out = []
    for tf in (tfs or REQUIRED_TIMEFRAMES):
        a = analyses[tf]
        if not a.analyzed:
            continue
        if direction == BULL:
            out += [(s["price"], f"{tf} swing high") for s in confirmed_swings(a, "high")]
            out += [(p, f"{tf} buy-side liquidity") for p in a.liquidity.get("bsl", []) if finite(p)]
        else:
            out += [(s["price"], f"{tf} swing low") for s in confirmed_swings(a, "low")]
            out += [(p, f"{tf} sell-side liquidity") for p in a.liquidity.get("ssl", []) if finite(p)]
    return out


def geometry_problem(direction: str, entry, sl, tp) -> str | None:
    """BUY: stop < entry < target. SELL: target < entry < stop. All finite and positive."""
    if not all(finite(v) and v > 0 for v in (entry, sl, tp)):
        return "a price is missing, not finite or not positive"
    if direction == BULL and not sl < entry < tp:
        return f"BUY needs stop {sl:g} < entry {entry:g} < target {tp:g}"
    if direction == BEAR and not tp < entry < sl:
        return f"SELL needs target {tp:g} < entry {entry:g} < stop {sl:g}"
    return None


def build_result(defn: StrategyDefinition, symbol: str, decision: str, direction: str | None, status: str, *, context: dict, stages: dict,
                 gates: list, reasons: list, missing: list, evidence: list, analyses: dict, series: dict, clock: dict, candidates: list,
                 entry=None, sl=None, tp1=None, tp2=None, rr=None, setup_inv=None, notes=None, params: StrategyParams | None = None,
                 tfs: tuple | None = None, target: dict | None = None) -> dict:
    """The one result shape (a superset of what the Watchdog, the pipeline and the Command Centre read).

    `tfs` = the timeframes this evaluation actually read (defaults to H1 + M5). `target` = {"mode": "pips", "pips": n, "pip_size": x, ...}
    when the take-profit is a fixed distance from the ACTUAL entry (the pipeline then re-derives the price from the executable entry)."""
    stamp = defn.stamp()
    if tfs:
        stamp["timeframes"] = list(tfs)
    exec_tf = params.exec_tf if params is not None else SETUP_TF
    return {
        "symbol": symbol, "decision": decision, "direction": direction, "setup_status": status,
        "strategy": stamp, "rule_id": defn.rule_id, "selected_setup_family": defn.id if decision in (BUY, SELL) else None,
        "primary_execution_timeframe": exec_tf, "execution_timeframe": exec_tf, "target": target, "context": context, "stages": stages, "gates": gates,
        "reasons": reasons, "missing_evidence": missing, "confirmation_evidence": evidence,
        "entry": entry, "stop_loss": sl, "take_profit_1": tp1, "take_profit_2": tp2, "risk_reward": rr, "setup_invalidation": setup_inv,
        "setup_candidates": candidates,
        "timeframes": {tf: analyses[tf].to_dict() for tf in (tfs or REQUIRED_TIMEFRAMES) if tf in analyses},
        "data": {tf: series[tf].to_dict() for tf in (tfs or REQUIRED_TIMEFRAMES) if tf in series},
        "clock": clock or {}, "notes": list(notes or []),
        "parameters": ({"min_rr": params.min_rr, "counter_min_rr": params.counter_min_rr, "signal_max_age": params.signal_max_age,
                        "pullback_min_retrace": params.pullback_min_retrace, "sl_buffer": params.sl_buffer, "exec_tf": params.exec_tf,
                        "context_tf": params.context_tf, "target_pips": params.target_pips, "pip_size": params.pip_size} if params else {}),
        "disclaimer": DISCLAIMER,
    }


def stage(state: str, detail: str) -> dict:
    """state: met / not_met / unknown / not_evaluated"""
    return {"state": state, "detail": detail}


def unusable_result(defn, symbol, problems, series, clock, params, tfs: tuple | None = None) -> dict:
    """Missing / stale / defective data: an honest WAIT with no directional conclusion and no candidate."""
    why = "; ".join(problems)
    gates = []
    gate(gates, "data", False, why)
    stages = {"context": stage("unknown", "Cannot judge the market context without reliable data."),
              "setup_candidate": stage("not_evaluated", "Not evaluated: data is not reliable."),
              "confirmation": stage("not_evaluated", "Not evaluated: data is not reliable."),
              "trade_levels": stage("not_evaluated", "Not evaluated: data is not reliable.")}
    return build_result(defn, symbol, WAIT, None, "inconclusive",
                        context={"state": "data_unavailable", "label": "Data not reliable", "summary": why, "direction": None,
                                 "relation": None, "lines": []},
                        stages=stages, gates=gates, reasons=[why], missing=["Reliable, fresh H1 and M5 data"], evidence=[], analyses={},
                        series=series, clock=clock, candidates=[], params=params, tfs=tfs)


def run_analyses(series: dict, cfg: StrategyConfig, symbol: str, tfs: tuple | None = None) -> dict:
    """Only the named timeframes (default H1 + M5). The structure engine is told to use CLOSED candles and close-based breaks."""
    return {tf: analyze_timeframe(series[tf], cfg, symbol) for tf in (tfs or REQUIRED_TIMEFRAMES)}


def usable(series: dict, tfs: tuple | None = None) -> bool:
    return all(series.get(tf) is not None and series[tf].status in USABLE for tf in (tfs or REQUIRED_TIMEFRAMES))


_TEXT_KEYS = ("reasons", "missing_evidence", "confirmation_evidence", "notes")


def relabel(result: dict, ctx_tf: str, exec_tf: str) -> dict:
    """The CHoCH / trend wording was written for the default H1 + M5 pair. For another execution timeframe the human-readable text is
    relabelled (placeholders first, so H1 -> M5 style swaps cannot collide). Only prose is touched, never keys, numbers or data."""
    if (ctx_tf, exec_tf) == (CONTEXT_TF, SETUP_TF):
        return result

    def fix(text):
        if not isinstance(text, str):
            return text
        return text.replace("H1", "\x00C").replace("M5", "\x00E").replace("\x00C", ctx_tf).replace("\x00E", exec_tf)

    def walk(obj):
        if isinstance(obj, str):
            return fix(obj)
        if isinstance(obj, list):
            return [walk(x) for x in obj]
        if isinstance(obj, dict):
            return {k: (walk(v) if k in ("detail", "summary", "label", "lines", "premise", "supporting_evidence", "missing_evidence",
                                         "invalidation_conditions", "basis", "state_text") or k in _TEXT_KEYS else v)
                    for k, v in obj.items()}
        return obj

    for k in _TEXT_KEYS:
        if k in result:
            result[k] = walk(result[k])
    for k in ("gates", "setup_candidates"):
        result[k] = [walk(x) for x in result.get(k, [])]
    for k in ("stages", "context", "entry", "stop_loss", "take_profit_1", "take_profit_2", "setup_invalidation"):
        if isinstance(result.get(k), dict):
            result[k] = {kk: (walk(vv) if isinstance(vv, dict) else fix(vv) if isinstance(vv, str) else vv) for kk, vv in result[k].items()}
    return result
