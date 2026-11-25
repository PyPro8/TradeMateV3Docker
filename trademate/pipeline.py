"""Strategy -> Risk -> Execution pipeline (controlled, manual-confirmation mode).

This module connects three EXISTING engines that were previously disconnected. It adds no new order path:

    Strategy Engine   evaluate_symbol()           qualifies a BUY / SELL (WATCH and WAIT never go further)
    Risk Engine       evaluate_trade()            sizes from the live executable price and approves or rejects
    Execution gate    gate_order()                independently re-judges the exact order again just before sending
    MT5 pathway       adapter.place_order()       called by app.py ONLY (this module never sends anything)

Six different states are kept apart and reported separately (a later one never implies an earlier one is "good enough"):

    strategy_qualified -> risk_approved -> execution_eligible -> order_submitted -> broker_accepted -> position_confirmed

Rules kept here:
  * Fail closed. Missing, stale, inconsistent or unusable data means "not qualified" / "not approved", with a specific code.
  * Nothing is invented: the entry used for sizing is the broker's live ask (BUY) / bid (SELL); SL and TP are the strategy's own.
  * A risk approval is single-use, short-lived and bound to the symbol, direction, volume, SL, TP, the strategy setup, the account
    identity, the risk rules and the set of open positions / pending orders. Any material change invalidates it.
  * Execution re-validates everything from scratch (fresh strategy evaluation, fresh account state, the existing Risk gate).
  * Rounding never raises risk: volumes are rounded DOWN to the broker's step by the Risk Engine's own sizing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from trademate.broker.base import ConnectionState
from trademate.errors import BrokerError
from trademate.execution_cost import assess_spread
from trademate.lotsize import validate_for_info
from trademate.pips import PipDefinition, min_stop_problem, pip_for_info, target_price
from trademate.execution_gate import _reports_pending, _state_with_pending, gate_order
from trademate.risk.account_state import gather_state, to_spec
from trademate import entry_limits
from trademate.risk.engine import ACCEPTED, TradeProposal, evaluate_trade
from trademate.risk.position_sizing import _round_down_to_step
from trademate.strategy.timeframes import OK, TIMEFRAMES

logger = logging.getLogger("trademate.pipeline")

# ---- the six states -------------------------------------------------------------------------------------------------
STAGES = ("strategy_qualified", "risk_approved", "execution_eligible", "order_submitted", "broker_accepted", "position_confirmed")

# ---- machine-readable codes (the UI and the tests branch on these, never on message text) ------------------------------
# Strategy qualification
NOT_QUALIFIED = "not_qualified"                  # WATCH / WAIT / no decision: this is not an executable trade
DATA_UNUSABLE = "data_unusable"                  # at least one timeframe is stale / unavailable / defective
PROPOSAL_INCOMPLETE = "proposal_incomplete"      # a mandatory field is missing or unusable
PROPOSAL_INCONSISTENT = "proposal_inconsistent"  # entry / stop / target contradict the direction or the policy RR
# Risk
RISK_REJECTED = "risk_rejected"
MARKET_UNAVAILABLE = "market_unavailable"        # MT5 not connected / no live price / no symbol specification
# Execution eligibility
ENTRY_DRIFTED = "entry_drifted"
SPREAD_TOO_WIDE = "spread_too_wide"
RR_DEGRADED = "rr_degraded"
TRADING_DISABLED = "trading_disabled"
# Execution-time refusals
CONFIRMATION_REQUIRED = "confirmation_required"
APPROVAL_UNKNOWN = "approval_unknown"
APPROVAL_USED = "approval_already_used"
APPROVAL_EXPIRED = "approval_expired"
APPROVAL_INVALIDATED = "approval_invalidated"
ACCOUNT_CHANGED = "account_changed"
RULES_CHANGED = "risk_rules_changed"
STATE_CHANGED = "account_state_changed"
STRATEGY_MISMATCH = "strategy_mismatch"          # the result came from a different strategy than the selected one
STRATEGY_CHANGED = "strategy_changed"            # the user selected another strategy after this approval was issued
PROPOSAL_CHANGED = "proposal_changed"
NO_LONGER_QUALIFIED = "no_longer_qualified"
RISK_REVALIDATION_FAILED = "risk_revalidation_failed"
VOLUME_INVALID = "volume_invalid"                # the lot size is not valid for this instrument (never rounded into something else)
DUPLICATE_SETUP = "duplicate_setup"              # this exact setup (strategy, direction, candle/CHoCH identity) already produced an order
LEVELS_INVALID = "levels_invalid"                # the stop / target cannot be placed (pip undefined, broker minimum distance, geometry)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pos(x) -> bool:
    return _num(x) and x > 0


# =====================================================================================================================
# 1. Strategy qualification: what a BUY / SELL proposal MUST contain before anything else may look at it
# =====================================================================================================================
@dataclass(frozen=True)
class QualifiedProposal:
    """A strategy-qualified trade. It carries NO size and NO approval: those belong to the Risk Engine."""

    symbol: str
    direction: str                    # "buy" / "sell"
    setup_family: str
    rule_id: str
    strategy_id: str | None
    strategy_version: str | None
    entry_reference: float            # the strategy's reference price (last closed M5 close). NOT the executable price.
    entry_zone: dict | None
    stop_loss: float
    take_profit: float                # TP1
    rr_tp1: float                     # reward-to-risk of TP1 at the reference entry
    min_rr: float                     # the strategy's own policy minimum
    invalidation_level: float | None  # setup invalidation (a closed M15 candle beyond it invalidates the setup)
    invalidation_basis: str
    evaluated_at_server: float        # broker-clock time of the evaluation
    candle_times: dict                # timeframe -> newest closed candle time used
    supporting_evidence: list = field(default_factory=list)
    target_pips: float | None = None  # fixed take-profit distance from the ACTUAL entry (Candle Momentum 10, CHoCH 100); None = take_profit is the strategy's own level
    pip_size: float | None = None
    candidate_id: str | None = None   # the setup's identity (candle / CHoCH): one order per id, ever
    exec_tf: str | None = None
    max_slippage: float | None = None  # Candle Momentum: the user's execution-slippage tolerance in PRICE units (None = the generic drift protection below applies)

    def material_core(self) -> dict:
        """What must stay the same for an approval to remain valid. The reference entry moves with every M5 close and is NOT part of it
        (the executable price is re-checked separately); the thesis (direction, family, stop, target, invalidation) is."""
        # A pip target is re-derived from the executable entry at every step, so the PIPS (not a price that moves with every tick) are the thesis.
        return {"symbol": self.symbol, "direction": self.direction, "setup_family": self.setup_family, "rule_id": self.rule_id,
                "strategy_id": self.strategy_id, "strategy_version": self.strategy_version, "stop_loss": self.stop_loss,
                "take_profit": None if self.target_pips else self.take_profit, "target_pips": self.target_pips,
                "invalidation_level": self.invalidation_level, "candidate_id": self.candidate_id, "exec_tf": self.exec_tf}

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Qualification:
    qualified: bool
    code: str | None = None
    reasons: list = field(default_factory=list)
    proposal: QualifiedProposal | None = None
    decision: str | None = None       # BUY / SELL / WATCH / WAIT as the Strategy Engine reported it

    def to_dict(self) -> dict:
        return {"qualified": self.qualified, "code": self.code, "reasons": self.reasons, "decision": self.decision,
                "proposal": self.proposal.to_dict() if self.proposal else None}


def _fail(code, reasons, decision=None) -> Qualification:
    return Qualification(False, code, [reasons] if isinstance(reasons, str) else list(reasons), None, decision)


def qualify(payload: dict | None, expected_strategy_id: str | None = None) -> Qualification:
    """Turn the Strategy Engine's JSON (what `evaluate_symbol` returns) into a QualifiedProposal, or say precisely why not.

    This is a CONTRACT check, not a second strategy: it never re-decides whether the setup is good. It only refuses to pass on
    something that is not an executable, complete and internally consistent BUY / SELL."""
    if not isinstance(payload, dict) or not payload.get("ok") or not isinstance(payload.get("result"), dict):
        return _fail(NOT_QUALIFIED, "The Strategy Engine returned no usable result.")
    r = payload["result"]
    decision = r.get("decision")
    stamp = r.get("strategy") if isinstance(r.get("strategy"), dict) else None
    if expected_strategy_id is not None and (stamp is None or stamp.get("id") != expected_strategy_id):
        return _fail(STRATEGY_MISMATCH, f"The result does not come from the selected strategy '{expected_strategy_id}' "
                                        f"(it reports {stamp.get('id') if stamp else 'no strategy'}). It was refused.", decision)
    required = tuple(stamp.get("timeframes") or ()) if stamp else TIMEFRAMES      # the strategy's own timeframes (H1 and M5); legacy results: all four
    if stamp is not None and not required:
        return _fail(PROPOSAL_INCOMPLETE, "The strategy result does not name the timeframes it used.", decision)
    if decision not in ("BUY", "SELL"):
        why = (r.get("reasons") or ["The Strategy Engine has not qualified a trade."])
        return _fail(NOT_QUALIFIED, [f"{decision or 'No decision'} is not an executable trade."] + list(why), decision)
    # ---- data first: stale / unavailable data can never support a trade ----
    data = r.get("data") or {}
    bad = [f"{tf} is {(data.get(tf) or {}).get('status', 'missing')}: {(data.get(tf) or {}).get('reason', 'no data')}"
           for tf in required if (data.get(tf) or {}).get("status") != OK]
    if bad:
        return _fail(DATA_UNUSABLE, bad, decision)
    clock = r.get("clock") or {}
    if not clock.get("known") or not _num(clock.get("now_server")):
        return _fail(DATA_UNUSABLE, "The broker's clock is unknown, so candle freshness cannot be verified.", decision)
    times = {tf: (data.get(tf) or {}).get("newest_closed_time") for tf in required}
    if not all(_pos(t) for t in times.values()):
        return _fail(PROPOSAL_INCOMPLETE, "A timeframe has no newest-closed-candle timestamp.", decision)

    # ---- mandatory fields ----
    missing = []
    direction_word = r.get("direction")
    expect = {"BUY": "bullish", "SELL": "bearish"}[decision]
    entry, sl, tp1 = r.get("entry") or {}, r.get("stop_loss") or {}, r.get("take_profit_1") or {}
    rr = r.get("risk_reward") or {}
    family = r.get("selected_setup_family")
    if not r.get("symbol"):
        missing.append("symbol")
    if not family:
        missing.append("setup family")
    if not _pos(entry.get("price")):
        missing.append("entry reference")
    if not _pos(sl.get("price")):
        missing.append("stop-loss")
    if not _pos(tp1.get("price")):
        missing.append("take-profit")
    if not _pos(rr.get("rr_tp1")) or not (_num(rr.get("min_rr")) and rr["min_rr"] >= 0):      # min_rr 0 = the strategy's target is a fixed pip distance, not an RR rule
        missing.append("risk-to-reward information")
    inv = r.get("setup_invalidation") or {}
    if not _pos(inv.get("level")):
        missing.append("invalidation condition")
    if missing:
        return _fail(PROPOSAL_INCOMPLETE, "The proposal is missing: " + ", ".join(missing) + ".", decision)
    if direction_word != expect:
        return _fail(PROPOSAL_INCONSISTENT, f"The {decision} decision carries direction '{direction_word}', expected '{expect}'.", decision)
    if r.get("setup_status") != "confirmed":
        return _fail(NOT_QUALIFIED, f"Setup status is '{r.get('setup_status')}', not 'confirmed'.", decision)
    gates = r.get("gates") or []
    failed = [g.get("detail") for g in gates if not g.get("passed")]
    if not gates or failed:
        return _fail(NOT_QUALIFIED, failed or "The Strategy Engine reported no qualification gates.", decision)
    selected = next((c for c in (r.get("setup_candidates") or []) if c.get("setup_family") == family and c.get("direction") == expect), None)
    if not selected or selected.get("execution_eligible") is not True:
        return _fail(NOT_QUALIFIED, "The selected setup candidate is not marked execution-eligible by the Strategy Engine.", decision)

    # ---- internal consistency (direction vs levels). Only contradictions are refused; nothing is repaired. ----
    side = "buy" if decision == "BUY" else "sell"
    e, s, t = float(entry["price"]), float(sl["price"]), float(tp1["price"])
    bad = []
    if side == "buy" and not (s < e < t):
        bad.append(f"BUY needs stop {s:g} < entry {e:g} < target {t:g}.")
    if side == "sell" and not (t < e < s):
        bad.append(f"SELL needs target {t:g} < entry {e:g} < stop {s:g}.")
    if not bad:
        actual_rr = abs(t - e) / abs(e - s)
        if actual_rr + 1e-9 < float(rr["min_rr"]):
            bad.append(f"Reward-to-risk {actual_rr:.2f} is below the strategy's own minimum of {float(rr['min_rr']):g}.")
    if bad:
        return _fail(PROPOSAL_INCONSISTENT, bad, decision)

    target = r.get("target") if isinstance(r.get("target"), dict) else None
    tpips = float(target["pips"]) if target and _pos(target.get("pips")) else None
    psize = float(target["pip_size"]) if target and _pos(target.get("pip_size")) else None
    if target and tpips is not None and psize is None:
        return _fail(PROPOSAL_INCOMPLETE, "The strategy asked for a pip target but the instrument's pip size is unknown. Nothing was approved.", decision)
    cid = selected.get("candidate_id") if isinstance(selected, dict) else None
    ms = target.get("max_slippage") if target else None
    proposal = QualifiedProposal(
        max_slippage=float(ms) if _pos(ms) else None, target_pips=tpips, pip_size=psize, candidate_id=cid if isinstance(cid, str) else None, exec_tf=r.get("execution_timeframe"),
        symbol=str(r["symbol"]), direction=side, setup_family=str(family), rule_id=str(r.get("rule_id") or ""),
        strategy_id=stamp.get("id") if stamp else None, strategy_version=stamp.get("version") if stamp else None,
        entry_reference=e, entry_zone=entry.get("zone"), stop_loss=s, take_profit=t, rr_tp1=float(rr["rr_tp1"]), min_rr=float(rr["min_rr"]),
        invalidation_level=float(inv["level"]), invalidation_basis=str(inv.get("basis") or ""),
        evaluated_at_server=float(clock["now_server"]), candle_times={tf: float(v) for tf, v in times.items()},
        supporting_evidence=list(r.get("confirmation_evidence") or []))
    return Qualification(True, None, [], proposal, decision)


# =====================================================================================================================
# 2. Approvals: single-use, short-lived, bound to the proposal, the account and the risk rules
# =====================================================================================================================
@dataclass
class Approval:
    id: str
    created_at: float
    expires_at: float
    symbol: str
    direction: str
    volume: float
    stop_loss: float
    take_profit: float
    entry_live: float
    core: dict                 # QualifiedProposal.material_core() at approval time
    account: dict              # {"login", "server", "currency"}
    rules: dict                # RiskRules.to_dict() at approval time
    state_signature: str       # open positions + pending orders at approval time
    summary: dict              # what the user was shown (risk amount / percent, RR, ...)
    status: str = "issued"     # issued | consumed | invalidated | expired
    status_reason: str = ""
    outcome: dict | None = None

    def public(self) -> dict:
        return {"id": self.id, "status": self.status, "created_at": self.created_at, "expires_at": self.expires_at, "symbol": self.symbol,
                "direction": self.direction, "volume": self.volume, "stop_loss": self.stop_loss, "take_profit": self.take_profit,
                "entry_live": self.entry_live, "summary": self.summary, "status_reason": self.status_reason, "outcome": self.outcome}


class ApprovalStore:
    """In-memory only, on purpose: a restart discards every approval (fail closed). Thread-safe; consuming is atomic."""

    MAX = 200

    def __init__(self, log_path: Path | None = None):
        self._lock = threading.Lock()
        self._items: dict[str, Approval] = {}
        self._log_path = Path(log_path) if log_path else None

    def issue(self, **fields) -> Approval:
        with self._lock:
            a = Approval(id=secrets.token_hex(12), **fields)
            self._items[a.id] = a
            while len(self._items) > self.MAX:
                self._items.pop(next(iter(self._items)))
        self.log("approval_issued", a)
        return a

    def get(self, approval_id) -> Approval | None:
        with self._lock:
            return self._items.get(approval_id) if isinstance(approval_id, str) else None

    def consume(self, approval_id: str) -> bool:
        """Atomically turn an 'issued' approval into 'consumed'. False when it was not (still) issued: a duplicate attempt."""
        with self._lock:
            a = self._items.get(approval_id)
            if a is None or a.status != "issued":
                return False
            a.status, a.status_reason = "consumed", "An order was submitted from this approval."
        return True

    def invalidate(self, approval_id: str, status: str, reason: str) -> None:
        with self._lock:
            a = self._items.get(approval_id)
            if a is not None and a.status == "issued":
                a.status, a.status_reason = status, reason
        a = self.get(approval_id)
        if a is not None:
            self.log("approval_" + status, a)

    def invalidate_all(self, status: str, reason: str) -> int:
        """Cancel every still-issued approval (e.g. the selected strategy changed). Returns how many were cancelled."""
        with self._lock:
            ids = [a.id for a in self._items.values() if a.status == "issued"]
            for i in ids:
                self._items[i].status, self._items[i].status_reason = status, reason
        for i in ids:
            a = self.get(i)
            if a is not None:
                self.log("approval_" + status, a)
        return len(ids)

    def record_outcome(self, approval_id: str, outcome: dict) -> None:
        with self._lock:
            a = self._items.get(approval_id)
            if a is not None:
                a.outcome = outcome
        a = self.get(approval_id)
        if a is not None:
            self.log("execution_outcome", a)

    def log(self, event: str, a: Approval) -> None:
        """Append one JSON line to data/pipeline_log.jsonl (best effort: a logging problem never blocks or fails a trade)."""
        if self._log_path is None:
            return
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": time.time(), "event": event, "approval": a.public(), "core": a.core, "account": a.account}, default=str) + "\n")
        except OSError:
            logger.exception("Could not write the pipeline log")


def state_signature(adapter) -> str:
    """A fingerprint of what the account holds right now: every open position (with its stops) and every pending order.
    Prices, profit and equity are deliberately excluded: they move constantly; the Risk gate re-judges the numbers anyway."""
    positions = sorted((p.ticket, p.symbol, p.direction, p.volume, p.stop_loss, p.take_profit) for p in adapter.get_positions())
    orders = []
    if _reports_pending(adapter):
        orders = sorted((o.ticket, o.symbol, o.order_type, o.volume, o.price, o.stop_loss, o.take_profit) for o in adapter.get_orders())
    return hashlib.sha256(json.dumps([positions, orders], default=str).encode()).hexdigest()


def _account_identity(adapter) -> dict:
    a = adapter.get_account()
    return {"login": a.login, "server": a.server, "currency": a.currency}


# =====================================================================================================================
# 3. Execution eligibility (NOT strategy qualification, NOT risk): is the live market still where the proposal assumed?
# =====================================================================================================================
def eligibility_problems(p_core: dict, entry_reference: float, min_rr: float, live_entry: float, bid: float, ask: float, cfg, max_slippage: float | None = None,
                         pip_size: float | None = None, cost_out: dict | None = None, strategy_id: str | None = None) -> list:
    """[(code, message)] - empty = eligible. Uses the proposal's own stop / target and the live executable price.
    The drift and spread limits are execution-eligibility policy ([pipeline] in settings.toml)."""
    side, sl, tp = p_core["direction"], p_core["stop_loss"], p_core["take_profit"]
    out = []
    stop_distance = abs(entry_reference - sl)
    if side == "buy" and not (sl < live_entry < tp):
        out.append((ENTRY_DRIFTED, f"The live price {live_entry:g} is no longer between the stop {sl:g} and the target {tp:g}."))
    if side == "sell" and not (tp < live_entry < sl):
        out.append((ENTRY_DRIFTED, f"The live price {live_entry:g} is no longer between the target {tp:g} and the stop {sl:g}."))
    if out:
        return out
    drift = abs(live_entry - entry_reference)
    if strategy_id in ("candle_momentum", "bull_bear"):
        # Candle Momentum is a live trigger strategy. Its original trigger/reference is not a
        # fair execution-price anchor after the trigger has fired. The caller has already
        # re-evaluated the live candle and resolve_levels() re-anchors its pip target to the
        # current executable quote. Keep the hard geometry, spread, RR, Risk Engine and broker
        # checks below; do not apply the unrelated generic fraction-of-stop drift cap.
        pass
    elif max_slippage is not None:
        # Explicit adverse-slippage tolerance for strategies that define one.
        adverse = (live_entry - entry_reference) if side == "buy" else (entry_reference - live_entry)
        if adverse > max_slippage + 1e-12:
            out.append((ENTRY_DRIFTED, f"Execution slippage guard: the live executable price {live_entry:g} is {adverse:g} worse than the validated quote {entry_reference:g}; "
                                       f"the configured slippage tolerance is {max_slippage:g}."))
    elif drift > cfg.max_entry_drift_stop_fraction * stop_distance + 1e-12:
        out.append((ENTRY_DRIFTED, f"The live price {live_entry:g} is {drift:g} away from the strategy's reference {entry_reference:g}; "
                                   f"the limit is {cfg.max_entry_drift_stop_fraction:g} x the stop distance ({cfg.max_entry_drift_stop_fraction * stop_distance:g})."))
    live_stop = abs(live_entry - sl)
    # Execution cost: judged from this trade's own stop and target in PRICE units (see trademate/execution_cost.py). The old universal
    # "spread <= 0.25 x stop" rule is gone; [pipeline] max_spread_stop_fraction is an OPTIONAL user limit (0 = off, the default).
    cost = assess_spread(side, live_entry, sl, tp, bid, ask, pip_size=pip_size, user_max_fraction=getattr(cfg, "max_spread_stop_fraction", 0.0) or None)
    out.extend(cost["problems"])
    if cost_out is not None:
        cost_out.update(cost)
    live_rr = abs(tp - live_entry) / live_stop if live_stop > 0 else 0.0
    if min_rr and live_rr + 1e-9 < min_rr:
        out.append((RR_DEGRADED, f"At the live price the reward-to-risk is {live_rr:.2f}, below the strategy's own minimum of {min_rr:g}."))
    return out


# =====================================================================================================================
# 4. propose(): strategy -> risk -> eligibility -> approval.   NOTHING is sent.
# =====================================================================================================================
@dataclass
class PipelineReport:
    ok: bool                       # True only when an approval was issued
    code: str | None
    message: str
    reasons: list = field(default_factory=list)
    stages: dict = field(default_factory=lambda: {s: None for s in STAGES})   # True / False / None (= not reached)
    qualification: dict | None = None
    risk: dict | None = None
    eligibility: list = field(default_factory=list)
    approval: dict | None = None
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {**asdict(self), "order_sent": False}


def _cap_volume(decision, spec, max_volume):
    """The Risk Engine rejects (rather than caps) a size above the broker maximum / TradeMate's per-order maximum. A SMALLER position
    cannot exceed any risk limit the full one met, so it is a legitimate answer: return the capped volume, else None."""
    sizing = decision.sizing or {}
    calc = sizing.get("calculated_volume")
    if not _pos(calc) or spec is None or not all(_pos(v) for v in (spec.volume_min, spec.volume_max, spec.volume_step)):
        return None
    cap = spec.volume_max if max_volume is None else min(spec.volume_max, max_volume)
    if calc <= cap:
        return None
    capped = _round_down_to_step(cap, spec.volume_min, spec.volume_step)
    return capped if capped >= spec.volume_min else None


def resolve_levels(prop: QualifiedProposal, info, live: float):
    """-> (stop, target, problem). A pip target is measured from the EXECUTABLE entry (`live`), snapped so it is never closer than asked; the stop is
    snapped so it is never closer than computed. The broker's minimum stop distance and the price grid are checked here. No problem = levels usable."""
    from trademate.pips import protective_stop
    tick = getattr(info, "tick_size", None)
    sl = protective_stop(prop.direction, prop.stop_loss, tick)
    tp = prop.take_profit
    if prop.target_pips:
        if not _pos(prop.pip_size):
            return sl, None, "The pip size of this instrument is unknown, so the pip target cannot be placed. Nothing was approved."
        tp = target_price(prop.direction, live, prop.target_pips, PipDefinition(True, prop.pip_size, "strategy", "", prop.symbol), tick)
    if prop.direction == "buy" and not sl < live < tp or prop.direction == "sell" and not tp < live < sl:
        return sl, tp, f"The stop {sl:g}, live price {live:g} and target {tp:g} are not in a valid order for a {prop.direction.upper()}."
    problem = min_stop_problem(prop.direction, live, sl, tp, getattr(info, "point", None), getattr(info, "stops_level", None))
    return sl, tp, problem


def propose(adapter, settings, rules, max_volume, symbol, store: ApprovalStore, *, evaluate, risk_percent=None, now=None,
            strategy_id: str | None = None, fixed_volume=None, ledger=None, max_entries: int = 1) -> PipelineReport:
    """Run the pipeline up to (not including) execution. `evaluate(symbol)` must return the Strategy Engine's payload."""
    now = time.time() if now is None else now
    rep = PipelineReport(False, None, "")
    # ---- 1. strategy ----
    qual = qualify(evaluate(symbol), strategy_id)
    rep.qualification = qual.to_dict()
    rep.stages["strategy_qualified"] = qual.qualified
    if not qual.qualified:
        rep.code, rep.reasons = qual.code, qual.reasons
        rep.message = f"Not qualified ({qual.decision or 'no decision'}): {qual.reasons[0] if qual.reasons else qual.code}"
        return rep
    prop = qual.proposal
    if ledger is not None and prop.candidate_id:
        slot, why = entry_limits.next_slot(ledger, prop.candidate_id, max_entries)
        if slot is None:
            rep.stages["strategy_qualified"] = False
            rep.code, rep.reasons = DUPLICATE_SETUP, [why]
            rep.message = "Refused: " + rep.reasons[0]
            return rep

    # ---- 2. live market + account (read-only) ----
    try:
        if adapter.get_connection_status().state is not ConnectionState.CONNECTED:
            raise BrokerError("MT5 is not connected with fresh data.")
        info = adapter.get_symbol_spec(prop.symbol)
        state = _state_with_pending(adapter) if _reports_pending(adapter) else gather_state(adapter, now)
        identity = _account_identity(adapter)
        signature = state_signature(adapter)
    except (BrokerError, NotImplementedError) as exc:
        rep.code, rep.message = MARKET_UNAVAILABLE, (str(exc) or "MT5 data is not available.") + " Nothing was approved and nothing was sent."
        rep.reasons = [rep.message]
        return rep
    live = info.ask if prop.direction == "buy" else info.bid
    if not (_pos(live) and _pos(info.bid) and _pos(info.ask)):
        rep.code, rep.message = MARKET_UNAVAILABLE, f"No live price for {prop.symbol}. Nothing was approved and nothing was sent."
        rep.reasons = [rep.message]
        return rep

    # ---- 3a. levels measured from the executable entry, checked against the broker's grid and minimum distance ----
    sl_used, tp_used, bad_levels = resolve_levels(prop, info, live)
    if bad_levels:
        rep.code, rep.message, rep.reasons = LEVELS_INVALID, "Levels rejected: " + bad_levels + " Nothing was sent.", [bad_levels]
        return rep
    # ---- 3b. volume: the user's validated lot size (exact, never rounded) or, with none set, the risk-based size ----
    fixed = None
    if fixed_volume is not None:
        vc = validate_for_info(fixed_volume, info, max_volume)
        if not vc.ok:
            rep.code, rep.message, rep.reasons = VOLUME_INVALID, "Lot size rejected: " + vc.message + " Nothing was sent.", [vc.message]
            return rep
        fixed = vc.volume

    # ---- 3c. Risk Engine: judged from the LIVE executable price, the strategy's own stop and its own target ----
    pct = risk_percent if _pos(risk_percent) else min(settings.risk.default_risk_percent, rules.max_risk_per_trade_percent)
    spec = to_spec(info)
    tp_ = TradeProposal(symbol=info.name, direction=prop.direction, entry=live, stop_loss=sl_used, take_profit=tp_used,
                        strategy_risk_percent=None if fixed is not None else pct, volume=fixed)
    decision = evaluate_trade(state, tp_, spec, rules, max_volume)
    capped = False
    if decision.decision != ACCEPTED and fixed is None:
        smaller = _cap_volume(decision, spec, max_volume)
        failed = {c["name"] for c in decision.checks if not c["passed"]}
        if smaller is not None and failed == {"Broker constraints"}:
            retry = evaluate_trade(state, TradeProposal(symbol=info.name, direction=prop.direction, entry=live, stop_loss=sl_used,
                                                        take_profit=tp_used, volume=smaller), spec, rules, max_volume)
            if retry.decision == ACCEPTED:
                decision, capped = retry, True
                rep.warnings.append(f"The risk-based size exceeds the maximum order size, so the position was capped at {smaller:g} lots. "
                                    f"This risks LESS than the {pct:g}% requested ({retry.trade_risk_percent:.3f}%).")
    rep.risk = decision.to_dict()
    rep.risk["volume_capped"] = capped
    rep.risk["risk_percent_requested"] = pct
    approved = decision.decision == ACCEPTED and decision.ready_for_execution and _pos(decision.approved_volume)
    rep.stages["risk_approved"] = bool(approved)
    if not approved:
        rep.code, rep.reasons = RISK_REJECTED, list(decision.rejection_reasons)
        rep.message = f"Rejected by the Risk Engine: {decision.rejection_reason or 'no reason given'}"
        return rep

    # ---- 4. execution eligibility ----
    cost_info: dict = {}
    problems = eligibility_problems(dict(prop.material_core(), stop_loss=sl_used, take_profit=tp_used), prop.entry_reference, prop.min_rr, live,
                                    info.bid, info.ask, settings.pipeline, prop.max_slippage, pip_size=prop.pip_size, cost_out=cost_info, strategy_id=prop.strategy_id)
    if not settings.trading.enabled:
        problems.append((TRADING_DISABLED, "Trading is switched off in settings.toml ([trading] enabled = false)."))
    rep.eligibility = [{"code": c, "message": m} for c, m in problems]
    rep.stages["execution_eligible"] = not problems
    if problems:
        rep.code, rep.reasons = problems[0][0], [m for _c, m in problems]
        policy = "the execution-cost policy" if problems[0][0] in (SPREAD_TOO_WIDE, "spread_invalid") else "an execution-eligibility check"
        rep.message = f"The Risk Engine approved this setup, but {policy} blocked execution: {problems[0][1]}"
        return rep
    rep.warnings.extend(cost_info.get("warnings", []))

    # ---- 5. approval, bound to everything that matters ----
    live_rr = abs(tp_used - live) / abs(live - sl_used)
    summary = {"symbol": info.name, "direction": prop.direction, "volume": decision.approved_volume, "entry_live": live,
               "entry_reference": prop.entry_reference, "stop_loss": sl_used, "take_profit": tp_used, "target_pips": prop.target_pips,
               "candidate_id": prop.candidate_id, "volume_source": "user_lot" if fixed is not None else "risk_percent",
               "risk_amount": decision.trade_risk_amount, "risk_percent": decision.trade_risk_percent, "currency": state.currency,
               "reward_to_risk_live": live_rr, "setup_family": prop.setup_family, "volume_capped": capped,
               "invalidation_level": prop.invalidation_level, "invalidation_basis": prop.invalidation_basis,
               "supporting_evidence": prop.supporting_evidence, "strategy_id": prop.strategy_id, "strategy_version": prop.strategy_version}
    approval = store.issue(created_at=now, expires_at=now + settings.pipeline.approval_ttl_seconds, symbol=info.name, direction=prop.direction,
                           volume=decision.approved_volume, stop_loss=sl_used, take_profit=tp_used, entry_live=live,
                           core=prop.material_core(), account=identity, rules=rules.to_dict(), state_signature=signature, summary=summary)
    rep.approval = approval.public()
    rep.ok, rep.message = True, (f"Approved: {prop.direction.upper()} {decision.approved_volume:g} {info.name}. "
                                 f"Nothing has been sent; confirm within {settings.pipeline.approval_ttl_seconds:g}s to execute.")
    return rep


# =====================================================================================================================
# 5. prepare_execution(): everything re-validated from scratch.  Returns the order to send, or a refusal.  Sends NOTHING.
# =====================================================================================================================
@dataclass
class ExecutionPlan:
    approval_id: str
    symbol: str
    side: str
    volume: float
    stop_loss: float
    take_profit: float
    entry_live: float
    tick_size: float | None
    target_pips: float | None = None
    pip_size: float | None = None
    candidate_id: str | None = None


@dataclass
class Refusal:
    code: str
    message: str
    invalidated: bool = False

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "approval_invalidated": self.invalidated}


def prepare_execution(adapter, settings, rules, max_volume, store: ApprovalStore, approval_id, confirm, *, evaluate, now=None,
                      strategy_id: str | None = None, ledger=None, max_entries: int = 1):
    """-> (ExecutionPlan, None) or (None, Refusal). The approval is CONSUMED (atomically) only after every check passed."""
    now = time.time() if now is None else now
    if confirm is not True:
        return None, Refusal(CONFIRMATION_REQUIRED, "Explicit confirmation (confirm: true) is required to execute.")
    a = store.get(approval_id)
    if a is None:
        return None, Refusal(APPROVAL_UNKNOWN, "No such approval. It may have been discarded by a restart. Propose the trade again.")
    if a.status == "consumed":
        return None, Refusal(APPROVAL_USED, "This approval was already used to submit an order. If its outcome is unclear, reconcile with MT5; "
                                            "do not retry blindly.")
    if a.status != "issued":
        return None, Refusal(APPROVAL_INVALIDATED if a.status == "invalidated" else APPROVAL_EXPIRED, f"This approval is {a.status}: {a.status_reason}")

    def refuse(code, message):
        store.invalidate(a.id, "invalidated", f"{code}: {message}")
        return None, Refusal(code, message, invalidated=True)

    if strategy_id is not None and a.core.get("strategy_id") != strategy_id:
        return refuse(STRATEGY_CHANGED, f"The selected strategy is now '{strategy_id}' but this approval was made under "
                                        f"'{a.core.get('strategy_id')}'. It was cancelled; analyse again under the selected strategy.")
    if now > a.expires_at:
        store.invalidate(a.id, "expired", "The approval's time limit passed.")
        return None, Refusal(APPROVAL_EXPIRED, "The approval expired. Market data and account state must be checked again: propose the trade again.", True)
    if not settings.trading.enabled:
        return None, Refusal(TRADING_DISABLED, "Trading is switched off in settings.toml ([trading] enabled = false).")
    try:
        if adapter.get_connection_status().state is not ConnectionState.CONNECTED:
            return None, Refusal(MARKET_UNAVAILABLE, "MT5 is not connected with fresh data. Nothing was sent. The approval is kept until it expires.")
        identity = _account_identity(adapter)
        signature = state_signature(adapter)
    except (BrokerError, NotImplementedError) as exc:
        return None, Refusal(MARKET_UNAVAILABLE, (str(exc) or "MT5 data is not available.") + " Nothing was sent.")
    if identity != a.account:
        return refuse(ACCOUNT_CHANGED, f"The account changed since approval ({a.account} -> {identity}).")
    if rules.to_dict() != a.rules:
        return refuse(RULES_CHANGED, "The risk rules changed since approval.")
    if signature != a.state_signature:
        return refuse(STATE_CHANGED, "The account's open positions or pending orders changed since approval.")

    # fresh strategy evaluation: the thesis must be unchanged
    qual = qualify(evaluate(a.symbol), a.core.get("strategy_id"))
    if not qual.qualified:
        return refuse(NO_LONGER_QUALIFIED, "The setup no longer qualifies: " + (qual.reasons[0] if qual.reasons else str(qual.code)))
    if qual.proposal.material_core() != a.core:
        return refuse(PROPOSAL_CHANGED, "The proposal changed materially since approval (direction, stop, target, setup or invalidation).")

    # execution eligibility first (cheap, and gives the specific reason when the market simply moved)
    try:
        info = adapter.get_symbol_spec(a.symbol)
    except (BrokerError, NotImplementedError) as exc:
        return None, Refusal(MARKET_UNAVAILABLE, (str(exc) or "No symbol specification.") + " Nothing was sent. The approval is kept until it expires.")
    live = info.ask if a.direction == "buy" else info.bid
    if ledger is not None and qual.proposal.candidate_id:
        slot, why = entry_limits.next_slot(ledger, qual.proposal.candidate_id, max_entries, a.created_at)
        if slot is None:
            return refuse(DUPLICATE_SETUP, why)
    if not _pos(live):
        return refuse(MARKET_UNAVAILABLE, "No live price.")
    vc = validate_for_info(a.volume, info, max_volume)                     # the volume is judged again against the CURRENT instrument rules
    if not vc.ok:
        return refuse(VOLUME_INVALID, vc.message)
    sl_exec, tp_exec, bad_levels = resolve_levels(qual.proposal, info, live)   # a pip target is re-measured from the price the order will really be sent at
    if bad_levels:
        return refuse(LEVELS_INVALID, bad_levels)
    problems = eligibility_problems(dict(qual.proposal.material_core(), stop_loss=sl_exec, take_profit=tp_exec), qual.proposal.entry_reference,
                                    qual.proposal.min_rr, live, info.bid, info.ask, settings.pipeline, qual.proposal.max_slippage,
                                    pip_size=qual.proposal.pip_size, strategy_id=qual.proposal.strategy_id)
    if problems:
        return refuse(problems[0][0], problems[0][1])

    # independent risk verification of the EXACT order, by the existing execution gate (live price, fresh account state)
    review = gate_order(adapter, rules, max_volume, a.symbol, a.direction, a.volume, sl_exec, tp_exec)
    if not review.approved:
        if review.stage == "unavailable":            # MT5 data could not be read (or the review crashed): nothing proved the approval wrong
            return None, Refusal(MARKET_UNAVAILABLE, f"The Risk Engine could not re-check this order: {review.message} The approval is kept until it expires.")
        return refuse(RISK_REVALIDATION_FAILED, f"The Risk Engine no longer approves this exact order: {review.message}")

    if not store.consume(a.id):                      # a concurrent duplicate attempt loses here
        return None, Refusal(APPROVAL_USED, "This approval was already used to submit an order.")
    cid = qual.proposal.candidate_id
    if ledger is not None and cid:
        slot, why = entry_limits.next_slot(ledger, cid, max_entries, a.created_at)         # decided again at the moment of claiming: a concurrent duplicate loses here
        if slot is None or not ledger.claim(slot, approval=a.id, symbol=a.symbol, direction=a.direction, strategy=qual.proposal.strategy_id, entry_no=entry_limits.entry_no(cid, slot)):
            return None, Refusal(DUPLICATE_SETUP, why if slot is None else "An order was already submitted for this exact setup. One setup, one order.", True)
        cid = slot
    return ExecutionPlan(a.id, a.symbol, a.direction, a.volume, sl_exec, tp_exec, live, getattr(info, "tick_size", None),
                         target_pips=qual.proposal.target_pips, pip_size=qual.proposal.pip_size, candidate_id=cid), None


# =====================================================================================================================
# 6. After sending: states and a check that the position the broker shows is the one that was approved
# =====================================================================================================================
def stage_states(outcome: str) -> dict:
    """Map the app's machine-readable write outcome to the last three stages. True / False / None (= unknown)."""
    T, F, U = True, False, None
    table = {
        "executed": (T, T, T),
        "accepted_unconfirmed": (T, T, U),     # the broker accepted it; the account does not show the position (yet)
        "broker_rejected": (T, F, F),
        "not_executed": (T, F, F),             # reconciliation proved it did not happen
        "unknown": (U, U, U),                  # sent, answer lost: UNRESOLVED until reconciled
        "execution_refused": (F, F, F), "connection_failed": (F, F, F), "invalid_request": (F, F, F), "risk_rejected": (F, F, F),
    }
    sub, acc, pos = table.get(outcome, (U, U, U))
    return {"order_submitted": sub, "broker_accepted": acc, "position_confirmed": pos}


def verify_placement(plan: ExecutionPlan, position) -> dict:
    """Compare the broker-confirmed position with the approved order. Differences are REPORTED, never silently accepted."""
    if position is None:
        return {"checked": False, "matches": None, "discrepancies": ["No broker-confirmed position to compare."]}
    tol = (plan.tick_size or 0.0) / 2 + 1e-12
    bad = []
    if position.symbol != plan.symbol:
        bad.append(f"symbol {position.symbol} != {plan.symbol}")
    if position.direction != plan.side:
        bad.append(f"direction {position.direction} != {plan.side}")
    if position.volume is None or abs(position.volume - plan.volume) > 1e-9:
        bad.append(f"volume {position.volume} != {plan.volume}")
    if position.stop_loss is None or abs(position.stop_loss - plan.stop_loss) > tol:
        bad.append(f"stop-loss {position.stop_loss} != {plan.stop_loss}")
    if position.take_profit is None or abs(position.take_profit - plan.take_profit) > tol:
        bad.append(f"take-profit {position.take_profit} != {plan.take_profit}")
    return {"checked": True, "matches": not bad, "discrepancies": bad}


def realigned_target(plan, price_open, tick_size=None):
    """After a fill: the take-profit that is exactly `target_pips` from the ACTUAL entry price, or None when the placed target is already right
    (or the plan has no pip target). The pipeline measured it from the live quote; the fill can differ by slippage."""
    if not getattr(plan, "target_pips", None) or not _pos(getattr(plan, "pip_size", None)) or not _pos(price_open):
        return None
    new = target_price(plan.side, float(price_open), plan.target_pips, PipDefinition(True, plan.pip_size, "strategy", "", plan.symbol), tick_size)
    return None if abs(new - plan.take_profit) < (tick_size or 1e-12) / 2 else new
