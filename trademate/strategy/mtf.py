"""Multi-timeframe Strategy Engine (Phase 8, task: multi-timeframe strategy intelligence).

Pure analysis: validated candles in, a StrategyResult out. It reuses the existing Market Structure, Liquidity, FVG and Order Block
engines unchanged. It never sizes a position (Risk Engine), never sends an order (Execution Engine), and never reads a broker.

RULE SET  "MTF-ZONE-REACTION-v1"  (full text: docs/MULTI_TIMEFRAME_STRATEGY.md)

  Roles          H4 = higher-timeframe context     H1 = intermediate context
                 M15 = setup (area of interest)    M5 = entry trigger and execution timeframe
  Per timeframe  bias (closed-candle structure), its strength, the level that invalidates it, the stage of any reversal
                 (none / possible / developing / confirmed), live zones, liquidity and swing levels.
  Context        an explicit state machine over the four biases (see resolve_context). It is NOT a vote: strength, reversal stage
                 and which timeframe disagrees all matter, and four agreeing timeframes are only context.
  Qualification  BUY / SELL only when ALL gates pass: data, context permission, an M15 zone that price has reacted at, an M5 closed
                 structure break in the trade direction after that reaction, a valid structural stop, and a structural target with
                 the minimum reward-to-risk. Otherwise WATCH (something is developing) or WAIT (nothing to act on / cannot judge).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from trademate.analysis import fvg_fill_status
from trademate.fvg import FVGConfig, analyze_fvg
from trademate.liquidity import LiquidityConfig, analyze_liquidity
from trademate.orderblocks import OrderBlockConfig, analyze_order_blocks
from trademate.strategy.regime import estimate_regimes
from trademate.strategy.setups import discover_candidates
from trademate.strategy.timeframes import OK, TIMEFRAMES, USABLE, PreparedSeries
from trademate.structure import StructureConfig, analyze_cached, clean_candles, structural_facts

RULE_ID = "MTF-ZONE-REACTION-v1"
BUY, SELL, WAIT, WATCH = "BUY", "SELL", "WAIT", "WATCH"
BULL, BEAR, NEUTRAL = "bullish", "bearish", "neutral"
ESTABLISHED, EARLY, MIXED, NONE = "established", "early", "mixed", "none"
R_NONE, R_POSSIBLE, R_DEVELOPING, R_CONFIRMED = "none", "possible", "developing", "confirmed"
EXECUTION_TF, SETUP_TF = "M5", "M15"

# Context states
S_DATA = "data_unavailable"
S_ALIGNED = "aligned_{x}"                       # all four agree, H4/H1/M15 established, M5 established or early
S_AGREEING = "agreeing_{x}"                     # H1 (or H4) and M15 agree; some timeframe is weaker or neutral
S_LTF_UNCLEAR = "htf_{x}_ltf_unclear"           # higher timeframes {x}; M15 gives no direction and nothing opposes
S_PULLBACK = "htf_{x}_ltf_{o}_pullback"         # higher timeframes {x}; M15 and/or M5 point the other way
S_POSSIBLE_REV = "possible_reversal_unconfirmed"
S_CONFIRMED_REV = "confirmed_reversal_vs_htf"   # H1 reversal confirmed against H4
S_CONFLICT = "conflicting"
S_INCONCLUSIVE = "inconclusive"

DISCLAIMER = ("Multi-timeframe agreement is context, not proof of profit: no win rate is implied. This is analysis and a proposal only. "
              "Position size and approval belong to the Risk Engine and orders to the Execution Engine; nothing here sends an order.")


def opposite(direction: str | None) -> str | None:
    return {BULL: BEAR, BEAR: BULL}.get(direction)


@dataclass(frozen=True)
class StrategyConfig:
    swing_left: int = 3
    swing_right: int = 3
    break_mode: str = "close"
    equal_tolerance: float = 0.0
    reversal_max_bos: int = 2           # a confirmed reversal stays "confirmed" for this many follow-through BOS, then it is just a trend
    trigger_lookback: int = 12          # M5 closed candles in which the zone reaction and the trigger must have happened
    min_rr: float = 1.5                 # minimum reward-to-risk of TP1 (policy value; change in [strategy] min_rr)
    sl_buffer: float = 0.0              # absolute price added beyond the structural stop (0 = none)
    zones_listed: int = 4               # zones per direction included in the result
    recent_invalid_window: int = 20     # candles in which an invalidated zone still explains an "invalidated" setup

    def structure(self) -> StructureConfig:
        return StructureConfig(swing_left=self.swing_left, swing_right=self.swing_right, break_mode=self.break_mode,
                               equal_tolerance=self.equal_tolerance, last_candle_closed=True)

    def validate(self) -> "StrategyConfig":
        if self.min_rr <= 0 or not math.isfinite(self.min_rr):
            raise ValueError("min_rr must be a number above 0.")
        if self.sl_buffer < 0 or not math.isfinite(self.sl_buffer):
            raise ValueError("sl_buffer must be 0 or more.")
        if self.trigger_lookback < 1 or self.reversal_max_bos < 1:
            raise ValueError("trigger_lookback and reversal_max_bos must be at least 1.")
        self.structure()
        return self


@dataclass
class TimeframeAnalysis:
    timeframe: str
    data_status: str
    data_reason: str
    analyzed: bool = False
    structure_config: dict = field(default_factory=dict)
    confirmed_direction: str | None = None
    bias: str = NEUTRAL
    strength: str = NONE
    bias_basis: str = ""
    last_close: float | None = None
    last_close_time: float | None = None
    last_event: dict | None = None
    events: list = field(default_factory=list)
    invalidation: dict = field(default_factory=lambda: {"level": None, "basis": "not analysed"})
    reversal: dict = field(default_factory=lambda: {"stage": R_NONE, "toward": None, "evidence": [], "provisional": False,
                                                    "confirm_level": None, "conditions": []})
    intrabar: dict = field(default_factory=lambda: {"present": False, "direction": None, "note": "No forming candle was analysed."})
    zones: list = field(default_factory=list)
    zones_invalidated_recent: int = 0
    liquidity: dict = field(default_factory=lambda: {"available": False, "reason": "not analysed", "bsl": [], "ssl": [], "sweeps": []})
    swing_highs: list = field(default_factory=list)
    swing_lows: list = field(default_factory=list)
    structural_facts: dict = field(default_factory=dict)
    rows: tuple = field(default=(), repr=False, compare=False)       # closed candles used (not serialised)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "rows"}
        d["usable_for_confirmation"] = self.data_status == OK
        return d


# ------------------------------------------------------------------------------------------------------------ per timeframe
def _last_class(swings, kind: str):
    for s in reversed(swings):
        if s.kind == kind:
            return s.classification
    return None


def _event_dict(e, symbol: str | None, timeframe: str) -> dict:
    return {"id": e.id, "symbol": symbol, "timeframe": timeframe, "type": e.type, "direction": e.direction, "level": e.level,
            "reference_pivot_time": e.reference_pivot_time, "reference_pivot_confirmed_time": e.reference_pivot_confirmed_time,
            "candle_time": e.candle_time, "break_price": e.break_price, "break_mode": e.break_mode,
            "trend_before": e.trend_before, "trend_after": e.trend_after, "reference_role": e.reference_role,
            "reason": e.reason, "confirmed": e.confirmed, "confirmed_time": e.confirmed_time,
            "same_candle_conflict": e.same_candle_conflict, "reference_swing_id": e.reference_swing_id,
            "reference_classification": e.reference_classification}


def _reversal_state(res, bias, strength, intrabar, cfg: StrategyConfig, oppose: bool) -> dict:
    events = sorted(res.events, key=lambda e: e.candle_index)
    evidence: list[str] = []
    stage, toward = R_NONE, None
    anchor_time = None
    last = events[-1] if events else None
    if bias in (BULL, BEAR) and last is not None:
        if last.type == "CHOCH":
            stage, toward = R_DEVELOPING, bias
            anchor_time = last.candle_time
            evidence.append(f"The latest closed break is a {bias} CHoCH through {last.level:g}; no follow-through break has happened yet.")
        else:
            k = max((i for i, e in enumerate(events) if e.type == "CHOCH" and e.direction == bias), default=None)
            if k is not None:
                after = events[k + 1:]
                if after and all(e.type == "BOS" and e.direction == bias for e in after) and len(after) <= cfg.reversal_max_bos:
                    stage, toward = R_CONFIRMED, bias
                    anchor_time = events[k].candle_time
                    evidence.append(f"A {bias} CHoCH through {events[k].level:g} was followed by {len(after)} closed {bias} BOS: "
                                    "the reversal has follow-through.")
    if stage == R_NONE and bias in (BULL, BEAR):
        reasons = []
        if oppose:
            reasons.append("the latest confirmed swings point against the trend")
        if intrabar.get("direction") == opposite(bias):
            reasons.append(f"the forming candle shows an unconfirmed {opposite(bias)} break (provisional)")
        if reasons:
            stage, toward = R_POSSIBLE, opposite(bias)
            evidence.append("A reversal is merely possible: " + " and ".join(reasons) + ". No closed candle has broken the invalidation level.")
    return {"stage": stage, "toward": toward, "evidence": evidence, "anchor_time": anchor_time,
            "provisional": bool(stage == R_POSSIBLE and intrabar.get("direction")),
            "confirm_level": None, "conditions": []}


def _conditions(bias, res, reversal: dict, invalidation: dict) -> None:
    """Fill the reversal conditions from real structure only; a level that does not exist stays None and the text says so."""
    level = invalidation["level"]
    stage, toward = reversal["stage"], reversal["toward"]
    out: list[str] = []
    if bias not in (BULL, BEAR):
        reversal["conditions"] = ["No directional bias, so there is no reversal to confirm."]
        return
    opp = opposite(bias)
    if stage in (R_NONE, R_POSSIBLE):
        if level is None:
            out.append(f"Invalidation level not available: no confirmed unbroken swing {'low' if bias == BULL else 'high'} exists yet.")
        else:
            out.append(f"First step: a candle CLOSE {'below' if bias == BULL else 'above'} {level:g} would break the {bias} structure "
                       f"(a {opp} CHoCH). That alone is early evidence, not a confirmed reversal.")
        out.append(f"Confirmation needs follow-through after that: a {'lower high' if opp == BEAR else 'higher low'} and a further closed "
                   f"{opp} break of the next confirmed swing (a {opp} BOS). The level of that swing does not exist until it forms.")
    elif stage == R_DEVELOPING:
        nxt = res.active_low if toward == BEAR else res.active_high
        reversal["confirm_level"] = nxt.price if nxt is not None else None
        if nxt is not None:
            out.append(f"To confirm the {toward} reversal: a candle CLOSE {'below' if toward == BEAR else 'above'} {nxt.price:g} "
                       f"(a {toward} BOS).")
        else:
            out.append(f"To confirm the {toward} reversal a further closed {toward} break is needed, but no confirmed unbroken swing "
                       "exists yet to name its level.")
        out.append(f"The {toward} reversal is invalidated by a CLOSE {'above' if toward == BEAR else 'below'} "
                   f"{level:g}." if level is not None else "Its invalidation level is not available yet.")
    else:                                                  # confirmed
        out.append(f"The {toward} reversal is confirmed. It is invalidated by a CLOSE {'above' if toward == BEAR else 'below'} "
                   f"{level:g}." if level is not None else "Its invalidation level is not available.")
    reversal["conditions"] = out


def _zones(rows, res, tf: str, cfg: StrategyConfig) -> tuple[list, int]:
    """Live zones (confirmed order blocks, not-fully-filled FVGs) from the existing engines, plus how many died recently."""
    zones: list[dict] = []
    dead = 0
    bars, _ = clean_candles(rows)
    n = len(bars)
    try:
        ob = analyze_order_blocks(rows, res, OrderBlockConfig(), timeframe=tf)
        if ob.data_status == "ok":
            for b in ob.blocks:
                if b.status == "confirmed":
                    zones.append({"kind": "order_block", "id": b.id, "direction": b.direction, "lower": b.lower, "upper": b.upper,
                                  "status": "confirmed", "time": b.source_time, "timeframe": tf})
                elif b.status == "invalidated" and b.invalidated_time is not None and n and \
                        b.invalidated_time >= bars[max(0, n - cfg.recent_invalid_window)].time:
                    dead += 1
    except ValueError:
        pass
    try:
        fv = analyze_fvg(rows, FVGConfig(last_candle_closed=True), timeframe=tf)
        if fv.data_status == "ok":
            for g in fv.gaps:
                gd = g.to_dict()
                fill = fvg_fill_status(gd, bars, last_candle_forming=False)
                if fill["status"] in ("untouched", "partial"):
                    zones.append({"kind": "fvg", "id": g.id, "direction": g.direction, "lower": g.lower, "upper": g.upper,
                                  "status": fill["status"], "time": g.formed_time, "timeframe": tf})
                elif g.third_index >= n - cfg.recent_invalid_window:
                    dead += 1
    except ValueError:
        pass
    return zones, dead


def analyze_timeframe(series: PreparedSeries, cfg: StrategyConfig, symbol: str | None = None) -> TimeframeAnalysis:
    """Structure, bias, invalidation, reversal stage, zones and liquidity of ONE timeframe from its CLOSED candles."""
    a = TimeframeAnalysis(series.timeframe, series.status, series.reason)
    if series.status not in USABLE:
        a.bias_basis = f"Not analysed: {series.status}."
        return a
    rows = [list(r) for r in series.closed]
    a.rows = tuple(tuple(r) for r in rows)
    res = analyze_cached(rows, cfg.structure())
    if res.diagnostics.insufficient_data:
        a.data_status, a.data_reason, a.bias_basis = "insufficient", "Too few candles for the structure analysis.", "Not analysed: insufficient."
        return a
    a.analyzed = True
    a.structure_config = res.config.to_dict()
    a.confirmed_direction = res.trend_confirmed
    a.last_close, a.last_close_time = rows[-1][4], rows[-1][0]
    events = sorted(res.events, key=lambda e: e.candle_index)
    a.events = [_event_dict(e, symbol, series.timeframe) for e in events[-8:]]
    a.last_event = a.events[-1] if a.events else None
    a.swing_highs = [{"id": s.id, "price": s.price, "time": s.pivot_time, "confirmed_time": s.confirmed_time,
                      "classification": s.classification, "confirmed": s.confirmed} for s in res.swings if s.kind == "high"][-12:]
    a.swing_lows = [{"id": s.id, "price": s.price, "time": s.pivot_time, "confirmed_time": s.confirmed_time,
                     "classification": s.classification, "confirmed": s.confirmed} for s in res.swings if s.kind == "low"][-12:]

    # Intrabar (forming candle) evidence, labelled provisional and never used for the closed-candle bias.
    if series.forming is not None:
        full = analyze_cached(rows + [list(series.forming)], StructureConfig(
            swing_left=cfg.swing_left, swing_right=cfg.swing_right, break_mode=cfg.break_mode, equal_tolerance=cfg.equal_tolerance,
            last_candle_closed=False))
        pending = [e for e in full.events if not e.confirmed]
        if pending:
            e = pending[-1]
            a.intrabar = {"present": True, "direction": e.direction, "note": f"PROVISIONAL: the forming {series.timeframe} candle currently "
                          f"shows a {e.direction} {e.type} through {e.level:g}. It may vanish before the candle closes and is not used for the decision."}
        else:
            a.intrabar = {"present": False, "direction": None, "note": "The forming candle has not broken any structure level."}
    else:
        a.intrabar = {"present": False, "direction": None, "note": "No forming candle was available (or the broker clock is unknown)."}

    trend = res.trend
    facts = structural_facts(res)                     # the SAME facts the Analysis page uses
    a.structural_facts = facts
    hi, lo = facts["swing_pattern"]["high"], facts["swing_pattern"]["low"]
    oppose = facts["swing_pattern"]["opposes"]
    if trend in (BULL, BEAR):
        a.bias = trend
        last = events[-1]
        if facts["quality"] == "early":
            a.strength = EARLY
            a.bias_basis = f"{trend} after a closed CHoCH through {last.level:g}; no follow-through BOS yet."
        elif facts["quality"] == "mixed":
            a.strength = MIXED
            a.bias_basis = f"Last closed break was a {trend} BOS but the latest swings point the other way (high {hi}, low {lo})."
        else:
            a.strength = ESTABLISHED
            a.bias_basis = f"Last closed break was a {trend} {last.type} through {last.level:g}; swings agree (high {hi}, low {lo})."
    else:
        a.bias, a.strength = NEUTRAL, NONE
        a.bias_basis = "No closed break has set a direction yet." if not events else "Conflicting closed breaks on one candle: direction unknown."

    inv = res.protected_low if trend == BULL else res.protected_high if trend == BEAR else None
    if trend in (BULL, BEAR):
        a.invalidation = ({"level": inv.price, "basis": f"latest confirmed unbroken swing {'low' if trend == BULL else 'high'} "
                           f"({inv.classification or 'unclassified'}, pivot {int(inv.pivot_time)}): a closed candle beyond it flips the {trend} bias"}
                          if inv is not None else
                          {"level": None, "basis": f"no confirmed unbroken swing {'low' if trend == BULL else 'high'} exists yet"})
    else:
        a.invalidation = {"level": None, "basis": "no directional bias, so nothing to invalidate"}
    a.reversal = _reversal_state(res, a.bias, a.strength, a.intrabar, cfg, oppose)
    _conditions(a.bias, res, a.reversal, a.invalidation)

    zones, dead = _zones(rows, res, series.timeframe, cfg)
    a.zones_invalidated_recent = dead
    price = a.last_close
    a.zones = sorted(zones, key=lambda z: (z["direction"], _distance(z, price)))
    try:
        liq = analyze_liquidity(rows, res, LiquidityConfig(last_candle_closed=True))
        a.liquidity = {"available": True, "reason": "",
                       "bsl": [p.level for p in liq.pools if p.side == "BSL" and p.state == "ACTIVE"],
                       "ssl": [p.level for p in liq.pools if p.side == "SSL" and p.state == "ACTIVE"],
                       "sweeps": [{"side": s.side, "level": s.level, "time": s.candle_time, "close_beyond": s.close_beyond}
                                  | {"confirmed": s.confirmed}
                                  for s in liq.sweeps[-3:]]}
    except ValueError as exc:
        a.liquidity = {"available": False, "reason": str(exc), "bsl": [], "ssl": [], "sweeps": []}
    return a


def _distance(zone: dict, price: float | None) -> float:
    if price is None:
        return math.inf
    if zone["lower"] <= price <= zone["upper"]:
        return 0.0
    return min(abs(price - zone["lower"]), abs(price - zone["upper"]))


# ---------------------------------------------------------------------------------------------------------- timeframe conflicts
@dataclass(frozen=True)
class Context:
    state: str
    label: str
    summary: str
    direction: str | None          # the direction a trade would take if everything else qualified (None = no candidate)
    permits: str | None            # "with_trend" | "counter_htf" | None
    htf_direction: str | None
    lines: tuple = ()
    conflicts: tuple = ()

    def to_dict(self) -> dict:
        return {"state": self.state, "label": self.label, "summary": self.summary, "direction": self.direction, "permits": self.permits,
                "htf_direction": self.htf_direction, "lines": list(self.lines), "conflicts": list(self.conflicts)}


def _dir(a: TimeframeAnalysis) -> str | None:
    return a.bias if a.analyzed and a.bias in (BULL, BEAR) else None


def resolve_context(an: dict) -> Context:
    """Resolve context without requiring every timeframe to exist or agree.

    H4/H1 establish the available broader context; M15/M5 remain distinct lower-timeframe
    observations. Missing higher-timeframe data lowers certainty and blocks execution later,
    but does not erase an otherwise meaningful candidate.
    """
    h4, h1, m15, m5 = an["H4"], an["H1"], an["M15"], an["M5"]
    lines = tuple(f"{tf}: {an[tf].bias} ({an[tf].strength})" if an[tf].analyzed else f"{tf}: no analysis ({an[tf].data_status})"
                  for tf in ("H4", "H1", "M15", "M5"))
    d4, d1, d15, d5 = _dir(h4), _dir(h1), _dir(m15), _dir(m5)
    analyzed = [a for a in an.values() if a.analyzed]
    if not analyzed:
        bad = [tf for tf in TIMEFRAMES if not an[tf].analyzed]
        why = "; ".join(f"{tf} is {an[tf].data_status}" for tf in bad)
        return Context(S_DATA, "Missing or stale timeframe data", f"Cannot analyse any timeframe: {why}.", None, None, None, lines,
                       tuple(f"{tf}: {an[tf].data_reason}" for tf in bad))

    if d4 and d1 and d4 == d1 == d15 == d5:
        strong = all(a.strength == ESTABLISHED for a in (h4, h1, m15)) and m5.strength in (ESTABLISHED, EARLY)
        if strong:
            return Context(S_ALIGNED.format(x=d5), f"Strongly aligned {d5}",
                           f"H4, H1, M15 and M5 are all {d5} with established structure. This is context only; entry needs its own evidence.",
                           d5, "with_trend", d4, lines)
        weak = [f"{a.timeframe} is {a.strength}" for a in (h4, h1, m15, m5) if a.strength in (EARLY, MIXED)]
        return Context(S_AGREEING.format(x=d5), f"{d5.capitalize()} on all four, but not strongly",
                       f"All four timeframes lean {d5}, but " + ", ".join(weak or ["some structure is not established"]) + ".",
                       d5, "with_trend", d4, lines)

    if d4 and d1 and d4 != d1:
        if h1.reversal["stage"] == R_CONFIRMED and h1.reversal["toward"] == d1:
            return Context(S_CONFIRMED_REV, f"H1 {d1} reversal confirmed against H4 {d4}",
                           f"H1 has a confirmed {d1} reversal (CHoCH with follow-through) while H4 is still {d4}: a counter-H4 setup is "
                           "possible only with stricter evidence.", d1, "counter_htf", d1, lines,
                           (f"H4 is {d4} but H1 turned {d1}.",))
        if h1.reversal["stage"] in (R_POSSIBLE, R_DEVELOPING) and h1.reversal["toward"] == d1:
            return Context(S_POSSIBLE_REV, f"Developing H1 {d1} reversal against H4 {d4}",
                           f"H1 is {d1} with a {h1.reversal['stage']} reversal while H4 remains {d4}. This is a candidate to monitor, "
                           "not a qualified reversal or trade.", d1, "developing_reversal", d4, lines,
                           (f"H4 remains {d4}; H1 reversal is {h1.reversal['stage']}.",))
        return Context(S_CONFLICT, "Conflicting higher-timeframe structure",
                       f"H4 is {d4} but H1 is {d1} and the H1 reversal is only '{h1.reversal['stage']}'. The higher timeframes disagree "
                       "and nothing confirms which one will hold.", None, None, None, lines, (f"H4 {d4} vs H1 {d1} (H1 reversal: {h1.reversal['stage']}).",))

    x = d1 or d4
    if x is None:
        return Context(S_INCONCLUSIVE, "Inconclusive", "Neither available H1 nor H4 structure establishes a direction. M15/M5 observations remain "
                       "visible, but there is no supported higher-timeframe context for a trade.", None, None, None, lines)
    o = opposite(x)
    counter = [tf for tf, d in (("M15", d15), ("M5", d5)) if d == o]
    htf_opp = [a.timeframe for a in (h1, h4) if a.reversal["stage"] == R_POSSIBLE and a.reversal["toward"] == o]
    missing_high = [tf for tf, a in (("H4", h4), ("H1", h1)) if not a.analyzed]
    coverage_note = (" " + "; ".join(f"{tf} is unavailable, so no substitute is used" for tf in missing_high) + ".") if missing_high else ""
    missing_low = [tf for tf, a in (("M15", m15), ("M5", m5)) if not a.analyzed]
    low_note = (" " + "; ".join(f"{tf} is unavailable and remains unevaluated" for tf in missing_low) + ".") if missing_low else ""
    if counter:
        if htf_opp:
            return Context(S_POSSIBLE_REV, f"Possible {o} reversal, not confirmed",
                           f"{', '.join(counter)} point {o} against a {x} higher-timeframe structure, and {', '.join(htf_opp)} already show "
                           f"signs of opposition. The reversal is possible but lacks confirmation.", o, "developing_reversal", x, lines,
                           (f"{', '.join(counter)} {o} vs higher timeframes {x}.",))
        return Context(S_PULLBACK.format(x=x, o=o), f"Higher timeframes {x}, lower timeframe {o} pullback",
                       f"H1/H4 are {x} and {', '.join(counter)} point {o}, but the higher-timeframe structure has not been broken. "
                       f"Treated as a pullback; it is not a reversal unless a higher timeframe confirms one.{coverage_note}", x, "with_trend", x, lines,
                       (f"{', '.join(counter)} {o} vs higher timeframes {x}.",))
    if d15 == x:
        return Context(S_AGREEING.format(x=x), f"{x.capitalize()} context with M15 agreeing",
                       f"Higher-timeframe context is {x} and M15 agrees; at least one timeframe is neutral, unavailable or weaker.{coverage_note}{low_note}",
                       x, "with_trend", x, lines)
    return Context(S_LTF_UNCLEAR.format(x=x), f"Higher timeframes {x}, M15 unclear",
                   f"Higher-timeframe context is {x} but M15 has no direction and nothing opposes it.{coverage_note}{low_note}",
                   x, "with_trend", x, lines)


# --------------------------------------------------------------------------------------------------------------- setup evaluation
def structural_stop(direction: str, entry: float, raw: float, buffer: float = 0.0) -> tuple[float | None, str]:
    """Stop beyond the structural extreme `raw` (+ buffer). (None, why) when it would not sit on the loss side of the entry."""
    for v in (entry, raw, buffer):
        if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
            return None, "an input price is not a finite number"
    sl = raw - buffer if direction == BULL else raw + buffer
    if direction == BULL and not sl < entry:
        return None, f"the structural low {sl:g} is not below the entry {entry:g}"
    if direction == BEAR and not sl > entry:
        return None, f"the structural high {sl:g} is not above the entry {entry:g}"
    return sl, ""


def pick_targets(direction: str, entry: float, sl: float, levels: list, min_rr: float) -> tuple[dict | None, dict | None, list]:
    """TP1 = nearest structural level in the trade direction with RR >= min_rr; TP2 = the next level beyond TP1. Levels are
    (price, basis). Returns (tp1, tp2, skipped) where skipped lists nearer levels rejected for RR."""
    risk = abs(entry - sl)
    ahead = sorted({round(p, 10): b for p, b in levels if (p > entry if direction == BULL else p < entry)}.items(),
                   key=lambda kv: abs(kv[0] - entry))
    tp1 = tp2 = None
    skipped = []
    for price, basis in ahead:
        rr = abs(price - entry) / risk if risk > 0 else 0.0
        if tp1 is None:
            if rr >= min_rr:
                tp1 = {"price": price, "basis": basis, "rr": rr}
            else:
                skipped.append({"price": price, "basis": basis, "rr": rr})
        elif tp2 is None:
            tp2 = {"price": price, "basis": basis, "rr": rr}
            break
    return tp1, tp2, skipped


def _gate(gates: list, gid: str, passed: bool, detail: str, evaluated: bool = True) -> bool:
    gates.append({"id": gid, "passed": bool(passed), "detail": detail, "evaluated": evaluated})
    return passed


@dataclass
class StrategyResult:
    symbol: str
    primary_execution_timeframe: str
    decision: str
    direction: str | None
    setup_status: str
    context: dict
    market_regimes: list
    setup_candidates: list
    selected_setup_family: str | None
    timeframes: dict
    data: dict
    entry: dict | None
    stop_loss: dict | None
    take_profit_1: dict | None
    take_profit_2: dict | None
    risk_reward: dict | None
    structural_invalidation: dict
    setup_invalidation: dict | None
    reversal: dict
    gates: list
    reasons: list
    missing_evidence: list
    confirmation_evidence: list
    notes: list
    clock: dict
    rule_id: str = RULE_ID
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict:
        result = dict(self.__dict__)
        selected = next((candidate for candidate in self.setup_candidates
                         if candidate.get("setup_family") == self.selected_setup_family
                         and candidate.get("direction") == self.direction), None)
        result["validation_snapshot"] = {
            "schema": "trademate.strategy.validation.v1",
            "symbol": self.symbol,
            "rule_id": self.rule_id,
            "evaluated_at_server": self.clock.get("now_server"),
            "setup_family": self.selected_setup_family,
            "decision": self.decision,
            "direction": self.direction,
            "observed_candles": {
                tf: {"status": item.get("status"), "newest_closed_time": item.get("newest_closed_time")}
                for tf, item in self.data.items()
            },
            "regimes": self.market_regimes,
            "supporting_evidence": (selected or {}).get("supporting_evidence", self.confirmation_evidence),
            "conflicting_evidence": (selected or {}).get("conflicting_evidence", self.context.get("conflicts", [])),
            "missing_evidence": list(dict.fromkeys(
                self.missing_evidence + (selected or {}).get("missing_evidence", [])
            )),
            "eligibility_gates": self.gates,
            "proposed_levels": {
                "entry_reference": self.entry,
                "stop_loss": self.stop_loss,
                "take_profit_1": self.take_profit_1,
                "take_profit_2": self.take_profit_2,
                "risk_reward": self.risk_reward,
            } if self.decision in (BUY, SELL) else None,
            "execution_linked": False,
            "execution_outcome": None,
            "unavailable_outcome_metrics": [
                "broker fill and slippage", "realised outcome", "maximum adverse excursion",
                "maximum favourable excursion", "spread and execution costs", "thesis validity at exit",
            ],
        }
        return result

    def to_trade_proposal(self):
        """The Risk Engine's own TradeProposal for a BUY/SELL decision, else None. No size, no risk approval, no order: the proposal is
        handed to the existing Risk Engine, which alone calculates volume and accepts or rejects."""
        if self.decision not in (BUY, SELL) or not self.entry or not self.stop_loss:
            return None
        from trademate.risk.engine import TradeProposal
        return TradeProposal(symbol=self.symbol, direction="buy" if self.decision == BUY else "sell", entry=self.entry["price"],
                             stop_loss=self.stop_loss["price"], take_profit=(self.take_profit_1 or {}).get("price"))


def _zone_candidates(m15: TimeframeAnalysis, direction: str) -> list:
    return [z for z in m15.zones if z["direction"] == direction]


def _reference_tf(an: dict) -> TimeframeAnalysis | None:
    for tf in ("H1", "H4", "M15"):
        if _dir(an[tf]):
            return an[tf]
    return None


def evaluate(symbol: str, series: dict, cfg: StrategyConfig | None = None, *, clock: dict | None = None,
             analyses: dict | None = None) -> StrategyResult:
    """Full multi-timeframe evaluation. `series` maps "M5" / "M15" / "H1" / "H4" to PreparedSeries.
    `analyses` lets a caller (tests) supply TimeframeAnalysis objects directly instead of analysing candles."""
    cfg = (cfg or StrategyConfig()).validate()
    an = analyses if analyses is not None else {tf: analyze_timeframe(series[tf], cfg, symbol) for tf in TIMEFRAMES}
    ctx = resolve_context(an)
    regimes = estimate_regimes(series, an)
    candidates = discover_candidates(an, regimes)
    gates: list = []
    reasons: list = []
    missing: list = []
    evidence: list = []
    notes: list = []
    m5, m15 = an["M5"], an["M15"]

    # Reference timeframe for the bias/invalidation/reversal block (kept apart from the entry trigger and the stop).
    ref = _reference_tf(an)
    structural_invalidation = ({"level": ref.invalidation["level"], "timeframe": ref.timeframe, "bias": ref.bias, "basis": ref.invalidation["basis"]}
                               if ref else {"level": None, "timeframe": None, "bias": None, "basis": "no timeframe has a directional bias"})
    reversal = {"reference_timeframe": ref.timeframe if ref else None, "bias": ref.bias if ref else None,
                "stage": ref.reversal["stage"] if ref else R_NONE, "toward": ref.reversal["toward"] if ref else None,
                "evidence": list(ref.reversal["evidence"]) if ref else [], "conditions": list(ref.reversal["conditions"]) if ref else [],
                "by_timeframe": {tf: {"stage": an[tf].reversal["stage"], "toward": an[tf].reversal["toward"],
                                      "provisional": an[tf].reversal["provisional"]} for tf in TIMEFRAMES if an[tf].analyzed}}
    for tf in TIMEFRAMES:
        if an[tf].analyzed and an[tf].intrabar["present"]:
            notes.append(an[tf].intrabar["note"])

    base = dict(symbol=symbol, primary_execution_timeframe=EXECUTION_TF, context=ctx.to_dict(), market_regimes=regimes,
                setup_candidates=candidates, selected_setup_family=None,
                timeframes={tf: an[tf].to_dict() for tf in TIMEFRAMES}, data={tf: series[tf].to_dict() for tf in TIMEFRAMES} if series else {},
                structural_invalidation=structural_invalidation, reversal=reversal, notes=notes, clock=clock or {})

    not_ok = [tf for tf in TIMEFRAMES if an[tf].data_status != OK]
    data_ok = not not_ok
    _gate(gates, "data", data_ok, "All four timeframes are fresh, closed-candle based and free of recent defects." if data_ok else
          "; ".join(f"{tf} is {an[tf].data_status}: {an[tf].data_reason}" for tf in not_ok))
    if not data_ok:
        missing.append("Reliable data for all four timeframes before an executable proposal")

    def done(decision, direction, status, *, entry=None, sl=None, tp1=None, tp2=None, rr=None, setup_inv=None):
        selected_family = None
        if decision in (BUY, SELL):
            if ctx.permits == "counter_htf":
                selected_family = "countertrend_opportunity"
            elif any(c["setup_family"] == "pullback_continuation" and c["direction"] == direction for c in candidates):
                selected_family = "pullback_continuation"
            else:
                selected_family = "trend_continuation"
            selected = next((c for c in candidates if c["setup_family"] == selected_family and c["direction"] == direction), None)
            if selected is None:
                selected = {
                    "candidate_id": f"qualified:{symbol}:{selected_family}:{direction}",
                    "setup_family": selected_family,
                    "direction": direction,
                    "status": "developing",
                    "premise": "The existing qualification gates evaluated this candidate.",
                    "supporting_evidence": [],
                    "conflicting_evidence": list(ctx.conflicts),
                    "missing_evidence": [],
                    "invalidation_conditions": [],
                    "target_methodology": "The qualified result uses existing structural target selection and the configured minimum reward-to-risk.",
                    "execution_eligible": False,
                }
                candidates.append(selected)
            selected["status"] = "qualified"
            selected["execution_eligible"] = True
            selected["supporting_evidence"] = list(dict.fromkeys(selected["supporting_evidence"] + evidence))
            selected["conflicting_evidence"] = list(dict.fromkeys(selected["conflicting_evidence"] + list(ctx.conflicts)))
            base["selected_setup_family"] = selected_family
        return StrategyResult(decision=decision, direction=direction, setup_status=status, entry=entry, stop_loss=sl, take_profit_1=tp1,
                              take_profit_2=tp2, risk_reward=rr, setup_invalidation=setup_inv, gates=gates, reasons=reasons,
                              missing_evidence=missing, confirmation_evidence=evidence, **base)

    # ---- no candidate direction: nothing to qualify
    if ctx.state == S_DATA:
        reasons.append(ctx.summary)
        missing += [f"Reliable {tf} data ({an[tf].data_status}: {an[tf].data_reason})" for tf in TIMEFRAMES if not an[tf].analyzed]
        return done(WAIT, None, "inconclusive")
    if ctx.state in (S_CONFLICT, S_INCONCLUSIVE):
        reasons.append(ctx.summary)
        missing.append("A clear higher-timeframe structure (H1/H4 agreement, or a confirmed H1 reversal)")
        return done(WAIT, None, "inconclusive")
    if ctx.state == S_POSSIBLE_REV:
        reasons.append(ctx.summary)
        developing = next((c for c in candidates if c["setup_family"] == "structural_reversal" and c["direction"] == ctx.direction), None)
        missing.append("Confirmation of the reversal: " + (
            "; ".join(developing["missing_evidence"]) if developing and developing["missing_evidence"]
            else "; ".join(reversal["conditions"]) or "a closed break of the invalidation level with follow-through"))
        return done(WATCH, ctx.direction, "developing")

    direction = ctx.direction
    assert direction in (BULL, BEAR) and ctx.permits
    evidence.append(f"Context: {ctx.label}.")

    # ---- Context permission
    permitted = True
    if ctx.permits == "counter_htf":
        permitted = m15.bias == direction and m15.strength == ESTABLISHED
        _gate(gates, "context", permitted, ("Counter-H4 setup: H1 reversal confirmed and M15 is established " + direction + "." if permitted else
                                            f"Counter-H4 setup needs M15 established {direction}; M15 is {m15.bias} ({m15.strength})."))
        if not permitted:
            missing.append(f"M15 established {direction} structure")
    else:
        _gate(gates, "context", True, f"Context {ctx.state} permits a {direction} setup with the higher-timeframe structure.")

    # ---- Gate 3: M15 zone + price reaction
    price = m5.last_close
    zones = [z for z in _zone_candidates(m15, direction)]
    zone = None
    touch_time = None
    if zones:
        zones.sort(key=lambda z: (_distance(z, price), 0 if z["kind"] == "order_block" else 1, -z["time"]))
        recent = m5.rows[-cfg.trigger_lookback:]
        for z in zones:
            hits = [r for r in recent if r[3] <= z["upper"] and r[2] >= z["lower"]]
            if hits:
                zone, touch_time = z, hits[0][0]
                break
    if not zones:
        _gate(gates, "zone", False, f"No live {direction} M15 order block or unfilled fair value gap exists.")
        missing.append(f"A live {direction} M15 zone (confirmed order block or unfilled fair value gap)")
        if m15.zones_invalidated_recent:
            reasons.append(f"{m15.zones_invalidated_recent} {direction}-side M15 zone(s) were invalidated or filled recently; no live zone remains.")
    elif zone is None:
        nearest = zones[0]
        _gate(gates, "zone", False, f"A {direction} M15 {nearest['kind'].replace('_', ' ')} exists at {nearest['lower']:g}-{nearest['upper']:g} but no closed "
              f"M5 candle in the last {cfg.trigger_lookback} reached it.")
        missing.append(f"Price reacting at the M15 zone {nearest['lower']:g}-{nearest['upper']:g}")
    else:
        _gate(gates, "zone", True, f"M5 traded into the M15 {zone['kind'].replace('_', ' ')} {zone['lower']:g}-{zone['upper']:g} at {int(touch_time)}.")
        evidence.append(f"M15 {zone['kind'].replace('_', ' ')} {zone['lower']:g}-{zone['upper']:g} ({zone['status']}) was reached by price.")

    # ---- Gate 4: M5 trigger (closed structure break in the trade direction AFTER the reaction)
    trigger = None
    if zone is not None:
        later = [e for e in m5.events if e["direction"] == direction and e["confirmed"] and e["candle_time"] > touch_time]
        trigger = later[-1] if later and m5.bias == direction else None
        if trigger:
            _gate(gates, "trigger", True, f"M5 closed a {direction} {trigger['type']} through {trigger['level']:g} after the zone reaction.")
            evidence.append(f"M5 {direction} {trigger['type']} through {trigger['level']:g} (closed candle) after the zone reaction.")
        else:
            _gate(gates, "trigger", False, f"No closed M5 {direction} structure break after the zone reaction (M5 is {m5.bias}, {m5.strength}).")
            missing.append(f"A closed M5 {direction} break of structure after price reacted at the zone")
    else:
        _gate(gates, "trigger", False, "Not evaluated: no zone reaction yet.", evaluated=False)

    # ---- Gates 5 and 6: structural stop, structural target, reward-to-risk
    entry = sl = tp1 = tp2 = rr = setup_inv = None
    if zone is not None and trigger is not None and price is not None:
        span = [r for r in m5.rows if r[0] >= touch_time]
        raw = min([zone["lower"]] + [r[3] for r in span]) if direction == BULL else max([zone["upper"]] + [r[2] for r in span])
        sl_price, why = structural_stop(direction, price, raw, cfg.sl_buffer)
        if sl_price is None:
            _gate(gates, "stop_loss", False, f"No valid structural stop: {why}.")
            missing.append("A valid structural stop loss on the loss side of the entry")
        else:
            _gate(gates, "stop_loss", True, f"Stop {sl_price:g} lies beyond the zone and the reaction extreme {raw:g}.")
            levels = []
            for tf in ("M15", "H1", "H4"):
                a = an[tf]
                if direction == BULL:
                    levels += [(s["price"], f"{tf} swing high") for s in a.swing_highs] + [(p, f"{tf} buy-side liquidity") for p in a.liquidity["bsl"]]
                else:
                    levels += [(s["price"], f"{tf} swing low") for s in a.swing_lows] + [(p, f"{tf} sell-side liquidity") for p in a.liquidity["ssl"]]
            tp1, tp2, skipped = pick_targets(direction, price, sl_price, levels, cfg.min_rr)
            if tp1 is None:
                near = ", ".join("%.2fR" % item["rr"] for item in skipped[:3])
                _gate(gates, "target", False, f"No structural target gives at least {cfg.min_rr:g}R"
                      + (f" (nearest levels give {near})." if skipped else " (no level ahead)."))
                missing.append(f"A structural target with reward-to-risk of at least {cfg.min_rr:g}")
            else:
                _gate(gates, "target", True, f"TP1 {tp1['price']:g} ({tp1['basis']}) gives {tp1['rr']:.2f}R.")
                entry = {"price": price, "basis": "close of the last closed M5 candle (reference price; the real fill depends on the market)",
                         "zone": {"lower": zone["lower"], "upper": zone["upper"], "source": f"M15 {zone['kind'].replace('_', ' ')}"}}
                sl = {"price": sl_price, "basis": f"beyond the M15 zone and the M5 reaction extreme ({raw:g})" + (f" plus buffer {cfg.sl_buffer:g}" if cfg.sl_buffer else "")}
                setup_inv = {"level": zone["lower"] if direction == BULL else zone["upper"], "timeframe": "M15",
                             "basis": f"a closed M15 candle beyond the far edge of the {zone['kind'].replace('_', ' ')} invalidates this setup"}
                rr = {"risk": abs(price - sl_price), "reward_tp1": abs(tp1["price"] - price), "rr_tp1": tp1["rr"],
                      "reward_tp2": abs(tp2["price"] - price) if tp2 else None, "rr_tp2": tp2["rr"] if tp2 else None, "min_rr": cfg.min_rr}
                if tp2 is None:
                    notes.append("Only one structural target lies ahead; there is no TP2.")
    else:
        _gate(gates, "stop_loss", False, "Not evaluated: the zone reaction and M5 trigger must come first.", evaluated=False)
        _gate(gates, "target", False, "Not evaluated: the zone reaction and M5 trigger must come first.", evaluated=False)

    all_pass = all(g["passed"] for g in gates) and sl is not None and tp1 is not None
    if all_pass:
        evidence.append(f"Structural stop {sl['price']:g}; TP1 {tp1['price']:g} ({tp1['rr']:.2f}R).")
        return done(BUY if direction == BULL else SELL, direction, "confirmed", entry=entry, sl=sl, tp1=tp1, tp2=tp2, rr=rr, setup_inv=setup_inv)

    # ---- not qualified: WATCH when there is an area of interest or a developing trigger, else WAIT
    for g in gates:
        if not g["passed"] and g["evaluated"] and g["detail"] not in reasons:
            reasons.append(g["detail"])
    if not reasons:
        reasons.append("Entry evidence is incomplete.")
    zone_known = bool(zones)
    if not zone_known:
        reasons.insert(0, f"The context permits a {direction} idea ({ctx.label}) but no area of interest exists yet.")
        return done(WAIT, direction, "invalidated" if m15.zones_invalidated_recent else "inconclusive")
    return done(WATCH, direction, "developing")
