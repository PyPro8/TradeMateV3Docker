"""The persistent, thread-safe lifecycle state machine of Candle Momentum: ONE candle at a time for ONE (symbol, timeframe) configuration.

It is fed three kinds of input and decides nothing about orders itself:
    begin_candle(snap)           the broker's forming candle (a new open time = a new candle = a new lifecycle)
    on_quote(quote, spec)        a live quote (price tick). Direction, trigger crossing and entry validation happen HERE, immediately.
    apply_execution(event)       what the executor (Autopilot -> Risk Engine -> pipeline) reported back
    on_time(now)                 signal expiry, executor time-out, bounded retry

Every state change goes through `_go()`, which (1) refuses an illegal transition, (2) records a Transition with every field the specification lists,
(3) writes the diagnostics event and (4) persists the lifecycle. The one deliberate exception is `force=True`, used ONLY for facts reported by the
broker (an order really filled): a real position must always be recorded and tracked even if the table would not have allowed that edge.

A candle is never entered twice: its identity is symbol + timeframe + open time + open price and, additionally, the (configuration, open time) pair, so
even a revised open price from the broker cannot create a second lifecycle for the same candle period.
"""

from __future__ import annotations

import json
import math
import threading

from trademate.bull_bear import logic, structure as bbs
from trademate.bull_bear.diagnostics import EventLog
from trademate.bull_bear.models import (BEAR, BULL, COMMITTED, IN_POSITION, LEGAL, MATERIAL, NEUTRAL, PRE_TRIGGER, SIGNAL_ACTIVE, TERMINAL, VALIDATING,
                                              CandleSnap, BBConfig, Quote, State, Transition)

S = State
MAX_TRANSITIONS = 80
EXECUTOR_TIMEOUT_SECONDS = 60.0

# executor refusal code (trademate.pipeline) -> (state, retryable). Unknown codes fail CLOSED as a non-retryable REJECTED.
MARKET_MOVED = {"entry_drifted", "rr_degraded", "no_longer_qualified", "proposal_changed", "slippage_exceeded", "approval_expired"}
TRANSIENT = {"market_unavailable", "connection_failed"}


def classify_refusal(code: str | None) -> tuple:
    if code in MARKET_MOVED:
        return S.MISSED, False
    if code in TRANSIENT:
        return S.EXECUTION_FAILED, True
    return S.REJECTED, False


class IllegalTransition(Exception):
    pass


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


class CandleMachine:
    def __init__(self, cfg: BBConfig, events: EventLog, store=None, clock=None):
        import time as _t
        self.cfg = cfg
        self.events = events
        self.store = store
        self._clock = clock or _t.time
        self._lock = threading.RLock()
        self.lc: dict | None = None            # the CURRENT candle's lifecycle
        self.tracked: dict = {}                # candle_id -> lifecycle of EARLIER candles whose position is still open
        self.version = 0
        self._announce: str | None = None      # candle id of a validated signal not yet announced to the executor
        self.last_feed_problem: dict | None = None
        self.structure: dict | None = None     # the confirmed market structure (set by the engine on every candle boundary); None = unknown = no entry
        self.max_entries = lambda: 1           # entries allowed per candle (the user's 'Allow multiple trades per candle' setting; 1 = baseline behaviour)

    # ------------------------------------------------------------------------------------------------------------------ helpers
    def set_config(self, cfg: BBConfig) -> None:
        with self._lock:
            self.cfg = cfg

    def _ev(self, event: str, level: str = "info", **f):
        lc = self.lc or {}
        return self.events.emit(event, f.pop("candle_id", lc.get("candle_id")), symbol=self.cfg.symbol, timeframe=self.cfg.timeframe, level=level, **f)

    def _persist(self, lc: dict, immediate: bool) -> None:
        if self.store is not None:
            self.store.put(lc, immediate)

    def _blank(self, snap: CandleSnap, cid: str, now: float) -> dict:
        return {"candle_id": cid, "config_key": self.cfg.key, "symbol": snap.symbol, "timeframe": snap.timeframe, "open_time": snap.open_time, "open": snap.open,
                "state": None, "direction": None, "trigger_price": None, "plan_target_price": None, "price": None, "bid": None, "ask": None, "quote_age": None,
                "last_quote": None, "prev_bid": None, "high_seen": snap.open, "low_seen": snap.open, "direction_changes": 0, "block_reason": None,
                "entry_reference": None, "sl_price": None, "tp_preview": None, "entry_price": None, "target_price": None, "ticket": None,
                "slippage_units": None, "slippage_exceeded": False, "verdict": None, "risk_decision": {"status": "none", "message": "", "code": None},
                "attempts": 0, "retry_at": None, "engaged": False, "engaged_at": None, "trigger_reached_at": None, "crossing_context": None,
                "timing": {}, "reason": "", "closed": None, "needs_reconcile": False, "restored": False, "transitions": [], "created_at": now, "updated_at": now,
                "config": self.cfg.to_dict(), "target_units": self.cfg.target_units, "trigger_units": self.cfg.trigger_units}

    def _go(self, state: State, why: str, now: float, force: bool = False, **fields) -> None:
        lc = self.lc
        return self._go_lc(lc, state, why, now, force, **fields)

    def _go_lc(self, lc: dict, state: State, why: str, now: float, force: bool = False, **fields) -> None:
        old = State(lc["state"]) if lc["state"] else None
        if old is not None and state != old and state not in LEGAL.get(old, set()) and not force:
            self._ev("transition", "error", candle_id=lc["candle_id"], illegal=True, from_state=old.value, to_state=state.value, explanation=why)
            raise IllegalTransition(f"{old.value} -> {state.value} is not a legal transition ({why})")
        lc.update(fields)
        lc["state"] = state.value
        lc["reason"] = why
        lc["updated_at"] = now
        tr = Transition(now, lc["symbol"], lc["timeframe"], lc["candle_id"], old.value if old else None, state.value, lc.get("price"), lc.get("open"), lc.get("direction"),
                        lc.get("trigger_price"), lc.get("entry_price"), lc.get("target_price"), why)
        lc["transitions"] = (lc["transitions"] + [tr.to_dict()])[-MAX_TRANSITIONS:]
        self.version += 1
        self._ev("transition", candle_id=lc["candle_id"], from_state=tr.from_state, to_state=state.value, price=lc.get("price"), candle_open=lc.get("open"),
                 direction=lc.get("direction"), trigger_price=lc.get("trigger_price"), entry_price=lc.get("entry_price"), target_price=lc.get("target_price"),
                 explanation=why, forced=bool(force and old is not None and state not in LEGAL.get(old, set())))
        self._persist(lc, immediate=state in MATERIAL)

    # ------------------------------------------------------------------------------------------------------------------ candle boundary
    def begin_candle(self, snap: CandleSnap, now: float) -> bool:
        """Register the broker's forming candle. True when a NEW lifecycle was started (a new candle)."""
        with self._lock:
            cid = logic.make_candle_id(snap.symbol, snap.timeframe, snap.open_time, snap.open)
            if self.lc is not None and (self.lc["candle_id"] == cid or self.lc.get("base_candle_id") == cid):
                return False
            if self.lc is not None and self.lc["open_time"] == snap.open_time:          # same candle period, different open price: a data revision, NOT a new candle
                self._ev("open_price_revised", "warning", candle_id=self.lc["candle_id"], old_open=self.lc["open"], new_open=snap.open)
                return False
            if self.lc is not None and snap.open_time < self.lc["open_time"]:           # an older candle than the one tracked: never go backwards
                self._ev("stale_candle_ignored", "warning", candle_id=self.lc["candle_id"], offered_open_time=snap.open_time)
                return False
            self._close_previous(now)
            existing = self._find_stored(snap, cid)
            self.lc = self._blank(snap, cid, now)
            self.lc["config"] = self.cfg.to_dict()
            self._ev("new_candle_detected", candle_id=cid, open_time=snap.open_time, candle_open=snap.open, restored=bool(existing))
            if existing is not None:
                self._restore(existing, now)
            else:
                self._go(S.NEW_CANDLE, f"New {snap.timeframe} candle opened at {snap.open:g} (candle {cid}).", now)
            self.version += 1
            return True

    def _find_stored(self, snap: CandleSnap, cid: str) -> dict | None:
        if self.store is None:
            return None
        hit = self.store.get(cid)
        if hit is not None:
            return hit
        for other in self.store.for_key(self.cfg.key):                                  # same period, other open price (revised data): adopt the record
            if other.get("open_time") == snap.open_time:
                return other
        return None

    def _restore(self, old: dict, now: float) -> None:
        """A candle seen before (a restart, a reconnect, a Watchdog restart). Nothing that was already decided is ever decided again."""
        lc = self.lc
        for k, v in old.items():
            if k in ("candle_id", "config_key", "symbol", "timeframe", "open_time"):
                continue
            lc[k] = v
        lc["restored"] = True
        st = State(old["state"])
        self._ev("state_restored", "warning" if st in (S.EXECUTING,) else "info", candle_id=lc["candle_id"], restored_state=st.value)
        if st in PRE_TRIGGER:
            lc["state"] = None
            lc["transitions"] = []
            self._go(S.NEW_CANDLE, "Monitoring resumed on this candle after a restart.", now)
        elif st in VALIDATING:
            lc["state"] = st.value
            self._go(S.EXPIRED, "TradeMate restarted while a validated signal existed for this candle. No order was sent, and a candle is never entered after a "
                                "restart, so this candle is closed to new entries.", now, force=True)
        elif st == S.EXECUTING:
            lc["state"] = st.value
            self._go(S.EXECUTION_FAILED, "TradeMate restarted while an order for this candle may have been in flight. The outcome is UNKNOWN: reconcile with MT5. "
                                         "This candle will not be entered again.", now, force=True, needs_reconcile=True)
        # POSITION_OPEN / TARGET_REACHED / terminal states stay exactly as they were; the engine re-checks a position against the broker
        if lc["state"] in {s.value for s in IN_POSITION}:
            self.tracked.pop(lc["candle_id"], None)

    def _close_previous(self, now: float) -> None:
        old = self.lc
        if old is None:
            return
        st = State(old["state"])
        if st in IN_POSITION:
            self.tracked[old["candle_id"]] = old                                         # keep following the position of the closed candle
        elif st in PRE_TRIGGER or st in VALIDATING:
            why = ("The candle closed before the trigger was reached; no entry was made." if st in PRE_TRIGGER else
                   "The candle closed before an order could be submitted; the signal ended with the candle.")
            if st in VALIDATING and old.get("engaged"):
                return                                                                  # the executor is mid-flight: its outcome arrives through apply_execution
            self._go(S.EXPIRED, why, now)

    # ------------------------------------------------------------------------------------------------------------------ quotes
    def on_quote(self, quote: Quote, spec, now: float) -> None:
        with self._lock:
            lc = self.lc
            if lc is None or not quote.valid:
                return
            prev = lc.get("bid")
            lc.update(prev_bid=prev, bid=quote.bid, ask=quote.ask, price=quote.bid, quote_age=logic.quote_age(quote, now), last_quote=quote.to_dict())
            lc["high_seen"], lc["low_seen"] = max(lc["high_seen"], quote.bid), min(lc["low_seen"], quote.bid)
            self.version += 1
            st = State(lc["state"])
            if st in PRE_TRIGGER:
                self._pre_trigger(quote, spec, now)
            elif st in (S.TRIGGER_REACHED, S.ENTRY_VALIDATION, S.RISK_PENDING):
                self._validate(quote, spec, now)
            elif st in IN_POSITION:
                self._follow_target(lc, quote, now)
            for tracked in list(self.tracked.values()):
                self._follow_target(tracked, quote, now)

    def _pre_trigger(self, quote: Quote, spec, now: float) -> None:
        lc = self.lc
        direction = logic.candle_direction(quote.bid, lc["open"], getattr(spec, "tick_size", None))
        want = {BULL: S.BULLISH, BEAR: S.BEARISH, NEUTRAL: S.MONITORING}[direction]
        gate = bbs.gate(None if direction == NEUTRAL else direction, self.structure)
        gsig = (direction, gate["code"], (self.structure or {}).get("direction"), (self.structure or {}).get("choch_time"))
        if lc["state"] != want.value or lc.get("gate_sig") != gsig:
            lc["gate_sig"], lc["structure"], lc["gate"] = gsig, self.structure, gate
            changed = lc["direction"] not in (None, direction) and direction != NEUTRAL
            trigger, block, plan = None, None, None
            if direction != NEUTRAL and not gate["ok"]:
                block = f"No entry: {gate['reason']}"
            elif direction != NEUTRAL:
                problems = list(getattr(spec, "unit_problems", ()) or ())
                if problems:
                    block = "UNVERIFIED instrument conversion: " + "; ".join(problems) + ". The trigger cannot be calculated, so nothing is traded."
                else:
                    try:
                        trigger = logic.trigger_price(lc["open"], direction, self.cfg.trigger_units, spec)
                        plan = logic.plan_target_price(trigger, direction, self.cfg.target_units, spec)
                    except (ValueError, AttributeError) as exc:
                        trigger, plan = None, None
                        block = f"The {direction} trigger cannot be calculated ({exc}). Nothing is traded until the instrument conversion is corrected."
            word = {"bullish": "above", "bearish": "below"}.get(direction)
            if direction == NEUTRAL:
                why = f"Price {quote.bid:g} equals the candle open {lc['open']:g}: no direction yet."
            else:
                why = (f"Price {quote.bid:g} is {word} the candle open {lc['open']:g}: the candle is {direction}. "
                       + (f"Trigger {trigger:g} ({self.cfg.trigger_units:g} units from the open)." if trigger is not None else (block or "")))
            if changed:
                lc["direction_changes"] += 1
            self._ev("direction_changed", candle_id=lc["candle_id"], direction=direction, previous=lc["direction"], price=quote.bid, candle_open=lc["open"])
            lc.update(direction=(None if direction == NEUTRAL else direction), trigger_price=trigger, plan_target_price=plan, block_reason=block)
            if trigger is not None:
                self._ev("trigger_calculated", candle_id=lc["candle_id"], trigger_price=trigger, trigger_units=self.cfg.trigger_units, direction=direction)
            self._go(want, why, now, price=quote.bid)
        d = lc["direction"]
        if d and lc["trigger_price"] is not None and logic.trigger_crossed(d, quote.bid, lc["trigger_price"]):
            # the previous tick we saw was short of the trigger -> the crossing happened between two observed ticks (a jump of any size is judged on the CURRENT quote);
            # no previous tick -> monitoring began (or resumed) with price already beyond the trigger
            ctx = "crossed_between_ticks" if lc.get("prev_bid") is not None else "first_observation_beyond_trigger"
            lc["crossing_context"] = ctx
            lc["trigger_reached_at"] = now
            lc["timing"].update(trigger_detected_at=now, trigger_quote_read_at=quote.read_at, quote_age_at_trigger=lc["quote_age"])
            self._ev("trigger_crossed", candle_id=lc["candle_id"], price=quote.bid, trigger_price=lc["trigger_price"], context=ctx, quote_age=lc["quote_age"])
            self._go(S.TRIGGER_REACHED, f"Price {quote.bid:g} reached the {d} trigger {lc['trigger_price']:g} ({ctx.replace('_', ' ')}). Evaluating the entry now.", now,
                     price=quote.bid)
            self._validate(quote, spec, now)

    # ------------------------------------------------------------------------------------------------------------------ entry validation
    def _validate(self, quote: Quote, spec, now: float) -> None:
        lc = self.lc
        d = lc["direction"]
        if d is None or lc["trigger_price"] is None:
            return
        g = bbs.gate(d, self.structure)
        if not g["ok"] and lc["state"] in (S.TRIGGER_REACHED.value, S.ENTRY_VALIDATION.value):
            if lc["state"] == S.TRIGGER_REACHED.value:
                self._go(S.ENTRY_VALIDATION, "Structure re-checked before the entry.", now)
            self._go(S.INVALIDATED, f"The market structure no longer supports this entry: {g['reason']}", now)
            return
        first = lc["state"] == S.TRIGGER_REACHED.value
        retry = lc["state"] == S.ENTRY_VALIDATION.value and lc.get("_retrying")
        if lc["state"] == S.RISK_PENDING.value and lc["engaged"]:
            return                                                                        # the executor decides now; the pipeline re-checks the live price itself
        if first:
            self._ev("entry_evaluation_started", candle_id=lc["candle_id"], price=quote.bid, ask=quote.ask, trigger_price=lc["trigger_price"])
            self._go(S.ENTRY_VALIDATION, "Entry evaluation started on the current executable quote.", now)
        v = logic.validate_entry(d, lc["trigger_price"], lc["open"], quote, self.cfg, spec, now)
        lc["verdict"] = v.to_dict()
        lc["sl_price"] = v.sl_price if v.sl_price is not None else lc["sl_price"]
        self._ev("entry_evaluation_completed", candle_id=lc["candle_id"], verdict=v.kind, code=v.code, reason=v.reason, exec_price=v.exec_price,
                 beyond_trigger_units=v.beyond_trigger_units, consumed_fraction=v.consumed_fraction, remaining_units=v.remaining_units, spread_units=v.spread_units)
        if v.quote_age is not None:
            self._ev("quote_freshness", "warning" if v.code == "stale_quote" else "info", candle_id=lc["candle_id"], quote_age=v.quote_age, limit=self.cfg.quote_max_age_seconds)
        if v.remaining_units is not None:
            self._ev("remaining_target_distance", candle_id=lc["candle_id"], remaining_units=v.remaining_units, plan_target_price=v.plan_target_price, exec_price=v.exec_price)
        st = State(lc["state"])
        if v.kind == logic.ENTER:
            if st == S.ENTRY_VALIDATION:
                lc.pop("_retrying", None)
                lc["timing"]["validated_at"] = now
                self._go(S.RISK_PENDING, v.reason + " Waiting for the Risk Engine and the executor.", now, entry_reference=v.exec_price, tp_preview=v.tp_preview,
                         target_price=v.tp_preview, risk_decision={"status": "pending", "message": "Awaiting the Risk Engine.", "code": None})
                lc["timing"]["signal_ready_at"] = now
                self._announce = lc["candle_id"]
            return
        if v.kind == logic.WAIT_QUOTE:
            if st == S.ENTRY_VALIDATION:
                lc["reason"] = v.reason
            return
        target = {logic.INVALIDATED: S.INVALIDATED, logic.MISSED: S.MISSED, logic.REJECTED: S.REJECTED}[v.kind]
        if target == S.MISSED:
            self._ev("entry_missed", "warning", candle_id=lc["candle_id"], code=v.code, reason=v.reason)
        elif target == S.INVALIDATED:
            self._ev("entry_invalidated", candle_id=lc["candle_id"], code=v.code, reason=v.reason)
        self._go(target, v.reason, now, risk_decision={"status": "not_submitted", "message": v.reason, "code": v.code})

    # ------------------------------------------------------------------------------------------------------------------ time
    def on_time(self, now: float, spec=None) -> None:
        with self._lock:
            lc = self.lc
            if lc is None:
                return
            st = State(lc["state"])
            if st in (S.TRIGGER_REACHED, S.ENTRY_VALIDATION, S.RISK_PENDING) and not lc["engaged"] and lc.get("trigger_reached_at") is not None:
                if now - lc["trigger_reached_at"] > self.cfg.signal_expiry_seconds:
                    self._go(S.EXPIRED, f"The signal expired: {now - lc['trigger_reached_at']:.1f}s passed since the trigger without an order (limit "
                                        f"{self.cfg.signal_expiry_seconds:g}s). It is not entered late.", now)
                    return
            if lc["engaged"] and st == S.RISK_PENDING and lc.get("engaged_at") is not None and now - lc["engaged_at"] > EXECUTOR_TIMEOUT_SECONDS:
                self._go(S.EXECUTION_FAILED, f"The executor reported no outcome within {EXECUTOR_TIMEOUT_SECONDS:g}s. The outcome is UNKNOWN: reconcile with MT5.", now,
                         needs_reconcile=True)
                return
            if st == S.EXECUTING and lc.get("engaged_at") is not None and now - lc["engaged_at"] > EXECUTOR_TIMEOUT_SECONDS * 2:
                self._go(S.EXECUTION_FAILED, "No broker confirmation arrived in time. The outcome is UNKNOWN: reconcile with MT5; nothing is retried.", now, needs_reconcile=True)
                return
            if st == S.EXECUTION_FAILED and lc.get("retry_at") is not None and now >= lc["retry_at"]:
                lc["retry_at"] = None
                lc["engaged"] = False
                lc["_retrying"] = True
                self._ev("retry_scheduled", candle_id=lc["candle_id"], attempt=lc["attempts"] + 1, of=self.cfg.max_entry_attempts)
                self._go(S.ENTRY_VALIDATION, f"Bounded retry {lc['attempts'] + 1} of {self.cfg.max_entry_attempts}: the previous order was provably not sent.", now)

    # ------------------------------------------------------------------------------------------------------------------ executor reports
    def apply_execution(self, ev: dict, spec, now: float) -> bool:
        """What the Autopilot / pipeline did with the signal. Returns False when the event does not belong to the current candle's signal."""
        with self._lock:
            cid = ev.get("candidate_id")
            lc = self.lc if (self.lc and self.lc["candle_id"] == cid) else self.tracked.get(cid)
            if lc is None:
                stored = self.store.get(cid) if (self.store is not None and cid) else None
                if stored is None:
                    return False
                lc = stored
                self.lc = lc if (self.lc is None) else self.lc
                if lc is not self.lc:
                    self.tracked[cid] = lc
            stage = ev.get("stage")
            t = lc["timing"]
            self.version += 1
            if stage == "proposing":
                if State(lc["state"]) == S.RISK_PENDING:
                    lc.update(engaged=True, engaged_at=now, attempts=lc["attempts"] + 1)
                    lc["risk_decision"] = {"status": "evaluating", "message": "The Risk Engine is evaluating this proposal.", "code": None}
                    t["executor_pickup_at"] = now
                    self._persist(lc, True)
                return True
            if stage == "approved":
                lc["risk_decision"] = {"status": "approved", "message": ev.get("message") or "Approved by the Risk Engine.", "code": None, "volume": ev.get("volume")}
                t["risk_approved_at"] = now
                self._ev("risk_engine_decision", candle_id=cid, approved=True, message=lc["risk_decision"]["message"])
                if State(lc["state"]) == S.RISK_PENDING:
                    self._go_lc(lc, S.EXECUTING, "The Risk Engine approved the proposal; the order is being submitted.", now)
                    t["submitted_at"] = now
                    self._ev("order_submitted", candle_id=cid, side="buy" if lc["direction"] == BULL else "sell", volume=ev.get("volume"))
                return True
            if stage in ("rejected", "refused"):
                self._refusal(lc, ev, now)
                return True
            if stage == "executed":
                self._filled(lc, ev, spec, now)
                return True
            if stage in ("unconfirmed", "failed"):
                code = ev.get("code")
                unsent = stage == "failed" and ev.get("order_sent") is False
                lc["risk_decision"] = {"status": "approved", "message": lc["risk_decision"].get("message", ""), "code": None}
                self._ev("broker_response", "error", candle_id=cid, stage=stage, message=ev.get("message"), code=code, order_sent=ev.get("order_sent"))
                self._fail(lc, ev.get("message") or "The order could not be confirmed.", now, retryable=False, needs_reconcile=(stage == "unconfirmed" or not unsent))
                return True
            return True

    def _refusal(self, lc: dict, ev: dict, now: float) -> None:
        code, msg = ev.get("code"), ev.get("message") or "refused"
        state, retryable = classify_refusal(code)
        lc["risk_decision"] = {"status": "rejected", "message": msg, "code": code}
        self._ev("risk_engine_decision", "warning", candle_id=lc["candle_id"], approved=False, code=code, message=msg, classified_as=state.value)
        if State(lc["state"]) in ({S.MISSED, S.REJECTED, S.INVALIDATED, S.EXPIRED, S.EXECUTION_FAILED} | COMMITTED):
            return
        if state == S.EXECUTION_FAILED:
            self._fail(lc, f"The order was not sent: {msg}", now, retryable=retryable, needs_reconcile=False)
        else:
            self._go_lc(lc, state, (f"Refused before submission by the executor ({code}): {msg}"), now, force=True)

    def _fail(self, lc: dict, why: str, now: float, retryable: bool, needs_reconcile: bool) -> None:
        can_retry = retryable and lc["attempts"] < self.cfg.max_entry_attempts and not needs_reconcile
        fields = {"needs_reconcile": needs_reconcile, "engaged": False}
        if can_retry:
            fields["retry_at"] = now + self.cfg.retry_delay_seconds
        self._go_lc(lc, S.EXECUTION_FAILED, why + (f" A bounded retry is scheduled in {self.cfg.retry_delay_seconds:g}s." if can_retry else " It will not be retried."), now, force=True, **fields)

    def _filled(self, lc: dict, ev: dict, spec, now: float) -> None:
        fill = ev.get("fill_price")
        side = "buy" if lc["direction"] == BULL else "sell"
        fields = {"ticket": ev.get("ticket"), "engaged": False}
        t = lc["timing"]
        t["filled_at"] = now
        why = f"The broker confirmed position {ev.get('ticket')}."
        if _num(fill):
            fields["entry_price"] = float(fill)
            try:
                tgt = spec.target_price(side, float(fill), self.cfg.target_units)
                fields["target_price"] = tgt
                why += f" Filled at {fill:g}; the {self.cfg.target_units:g}-unit target is {tgt:g}, measured from the ACTUAL fill."
                if _num(lc.get("entry_reference")):
                    sl = logic.slippage_units(side, lc["entry_reference"], float(fill), spec)
                    fields["slippage_units"] = sl
                    tol = self.cfg.max_slippage_units
                    fields["slippage_exceeded"] = bool(tol is not None and sl > tol + 1e-12)
                    self._ev("execution_slippage", "warning" if fields["slippage_exceeded"] else "info", candle_id=lc["candle_id"], reference=lc["entry_reference"], fill=fill,
                             slippage_units=sl, tolerance=tol, exceeded=fields["slippage_exceeded"])
                    why += f" Execution slippage {sl:+.2f} units" + (f" (tolerance {tol:g}: EXCEEDED)." if fields["slippage_exceeded"] else ".")
            except ValueError as exc:
                why += f" (the target could not be recalculated: {exc})"
        else:
            why += " The broker did not report a fill price; the target is the one placed with the order."
        lc["risk_decision"] = {"status": "approved", "message": lc["risk_decision"].get("message", "Approved."), "code": None}
        if _num(t.get("trigger_detected_at")):
            t["trigger_to_fill_ms"] = round((now - t["trigger_detected_at"]) * 1000)
            self._ev("latency", candle_id=lc["candle_id"], **{k: round((t[b] - t[a]) * 1000) for k, a, b in
                     (("detect_to_signal_ms", "trigger_detected_at", "signal_ready_at"), ("signal_to_pickup_ms", "signal_ready_at", "executor_pickup_at"),
                      ("pickup_to_approved_ms", "executor_pickup_at", "risk_approved_at"), ("approved_to_fill_ms", "risk_approved_at", "filled_at"),
                      ("trigger_to_fill_ms", "trigger_detected_at", "filled_at")) if _num(t.get(a)) and _num(t.get(b))},
                     quote_age_at_trigger=t.get("quote_age_at_trigger"))
        self._ev("position_opened", candle_id=lc["candle_id"], ticket=ev.get("ticket"), fill_price=fill, target_price=fields.get("target_price"))
        self._go_lc(lc, S.POSITION_OPEN, why, now, force=True, **fields)
        if lc is not self.lc:
            self.tracked[lc["candle_id"]] = lc

    # ------------------------------------------------------------------------------------------------------------------ positions
    def _follow_target(self, lc: dict, quote: Quote, now: float) -> None:
        if State(lc["state"]) != S.POSITION_OPEN or not _num(lc.get("target_price")):
            return
        buy = lc.get("direction") == BULL
        exit_price = quote.bid if buy else quote.ask                                      # a BUY closes on the bid, a SELL on the ask
        if (buy and exit_price >= lc["target_price"] - 1e-12) or (not buy and exit_price <= lc["target_price"] + 1e-12):
            self._ev("target_reached", candle_id=lc["candle_id"], exit_price=exit_price, target_price=lc["target_price"], ticket=lc.get("ticket"))
            self._go_lc(lc, S.TARGET_REACHED, f"Price {exit_price:g} reached the target {lc['target_price']:g}; the broker's take-profit closes the position.", now, price=quote.bid)

    def position_closed(self, candle_id: str, info: dict, now: float) -> None:
        """The broker's history shows the position closed. `info`: {reason, net, price_close, time_close}."""
        with self._lock:
            lc = self.lc if (self.lc and self.lc["candle_id"] == candle_id) else self.tracked.get(candle_id)
            if lc is None or State(lc["state"]) not in IN_POSITION:
                return
            reason = (info or {}).get("reason") or "unknown"
            if State(lc["state"]) == S.POSITION_OPEN and reason == "tp":
                self._go_lc(lc, S.TARGET_REACHED, "The broker closed the position at its take-profit.", now)
            self._ev("position_closed", candle_id=candle_id, reason=reason, net=(info or {}).get("net"), price_close=(info or {}).get("price_close"), ticket=lc.get("ticket"))
            self._go_lc(lc, S.POSITION_CLOSED, f"Position {lc.get('ticket')} closed ({reason}); net result {info.get('net') if info and info.get('net') is not None else 'unknown'}.", now,
                        closed=dict(info or {}))
            self.tracked.pop(candle_id, None)
            self._rearm_same_candle(lc, now)

    def _rearm_same_candle(self, closed_lc: dict, now: float) -> None:
        """'Allow multiple trades per candle' ON: when the first entry on the CURRENT candle has closed, a fresh lifecycle (id '<candle>#2') watches the same
        candle for ONE more distinct entry. Never more than two; with the setting OFF this does nothing and the baseline one-entry-per-candle rule stands.
        The signal logic is untouched: the new lifecycle runs the same trigger/validation path against the same candle open."""
        try:
            limit = min(int(self.max_entries() or 1), 2)
        except Exception:                                                                   # noqa: BLE001 - a broken setting must fall back to ONE entry
            limit = 1
        entry_no = int(closed_lc.get("entry_no", 1))
        if limit < 2 or entry_no >= limit or self.lc is not closed_lc or closed_lc.get("state") != S.POSITION_CLOSED.value:
            return
        from types import SimpleNamespace
        base = closed_lc.get("base_candle_id") or closed_lc["candle_id"]
        snap = SimpleNamespace(symbol=closed_lc["symbol"], timeframe=closed_lc["timeframe"], open_time=closed_lc["open_time"], open=closed_lc["open"])
        new = self._blank(snap, f"{base}#{entry_no + 1}", now)
        new.update(base_candle_id=base, entry_no=entry_no + 1, config=self.cfg.to_dict())
        self.lc = new
        self._ev("second_entry_armed", candle_id=new["candle_id"], base_candle_id=base, entry_no=entry_no + 1)
        self._go(S.NEW_CANDLE, f"Entry {entry_no} on this candle closed; watching the same candle for one more distinct entry (maximum {limit}).", now)
        self.version += 1

    def open_position_tickets(self) -> dict:
        """candle_id -> ticket of every position this machine is still following (current candle and earlier ones)."""
        with self._lock:
            out = {c: lc["ticket"] for c, lc in self.tracked.items() if lc.get("ticket")}
            if self.lc and State(self.lc["state"]) in IN_POSITION and self.lc.get("ticket"):
                out[self.lc["candle_id"]] = self.lc["ticket"]
            return out

    def open_lifecycles(self) -> list:
        """Copies of every lifecycle that currently holds an open position (for Anti-Twin adoption)."""
        with self._lock:
            out = [dict(lc) for lc in self.tracked.values() if lc.get("ticket")]
            if self.lc and State(self.lc["state"]) == S.POSITION_OPEN and self.lc.get("ticket"):
                out.append(dict(self.lc))
            return [lc for lc in out if lc.get("ticket") and State(lc["state"]) == S.POSITION_OPEN]

    # ------------------------------------------------------------------------------------------------------------------ feed problems
    def on_feed_problem(self, code: str, text: str, now: float) -> None:
        with self._lock:
            first = self.last_feed_problem is None or self.last_feed_problem["code"] != code
            self.last_feed_problem = {"code": code, "text": text, "since": now if first else self.last_feed_problem["since"]}
            if first:
                self.version += 1

    def feed_ok(self, now: float) -> bool:
        with self._lock:
            had = self.last_feed_problem is not None
            self.last_feed_problem = None
            if had:
                self.version += 1
            return had

    # ------------------------------------------------------------------------------------------------------------------ views
    def take_announcement(self) -> str | None:
        with self._lock:
            cid, self._announce = self._announce, None
            return cid

    def snapshot(self) -> dict:
        """A JSON-safe copy of the current lifecycle (and of any earlier candle still in a position)."""
        with self._lock:
            def clean(lc):
                return {k: v for k, v in json.loads(json.dumps(lc, default=str)).items() if not k.startswith("_")}
            return {"current": clean(self.lc) if self.lc else None, "tracked": [clean(v) for v in self.tracked.values()], "version": self.version,
                    "feed_problem": dict(self.last_feed_problem) if self.last_feed_problem else None,
                    "structure": dict(self.structure) if self.structure else None}

    @property
    def state(self) -> State | None:
        with self._lock:
            return State(self.lc["state"]) if self.lc and self.lc["state"] else None
