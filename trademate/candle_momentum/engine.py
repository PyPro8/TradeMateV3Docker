"""The Candle Momentum engine: ONE background worker that continuously monitors every active (symbol, timeframe) configuration.

Per cycle (`step`, default every 0.25 s - this is the real tick-monitoring cadence, unrelated to how often a browser refreshes):
    1. read the instrument specification when due (pip conversion, tick value, limits)           -> UNVERIFIED blocks execution
    2. find out whether a NEW candle began (broker-clock boundary + periodic verification)       -> machine.begin_candle
    3. read the quote; a missing / stale / disconnected feed is reported, never papered over     -> machine.on_quote (direction, trigger, entry validation)
    4. signal expiry, executor time-out, bounded retry                                           -> machine.on_time
    5. follow open positions to TARGET_REACHED / POSITION_CLOSED
    6. publish the authoritative decision to the DecisionHub (what the Command Centre, the Watchdog and Autopilot all read)
    7. wake the executor the instant a validated signal exists (no polling delay)

The engine never sends an order. A validated signal is handed to the existing Autopilot -> Risk Engine -> pipeline path (wired in app.py) and the outcome
comes back through `report_execution`.

`step()` is synchronous and deterministic (a clock is injected), so it is what the tests drive; the worker thread just calls it in a loop.
"""

from __future__ import annotations

import logging
import math
import threading
import time

from trademate.candle_momentum import logic
from trademate.candle_momentum.diagnostics import UNASSIGNED, EventLog
from trademate.candle_momentum.feed import AdapterFeed, FeedError
from trademate.candle_momentum.machine import CandleMachine
from trademate.candle_momentum.models import (BULL, ENGINE_VERSION, IN_POSITION, RULE_ID, SIGNAL_ACTIVE, STRATEGY_ID, TERMINAL, CMConfig, State, config_key,
                                              validate_config)
from trademate.candle_momentum.store import ConfigStore, StateStore, defaults_from_settings
from trademate.antitwin_lane import AntiTwinLane
from trademate.strategies.base import EXEC_TIMEFRAMES
from trademate.strategy.timeframes import OK, STALE, TF_SECONDS, UNAVAILABLE, PreparedSeries

logger = logging.getLogger("trademate.candle_momentum")
BOUNDARY_RETRY_SECONDS = 0.25
POSITION_POLL_SECONDS = 1.0
CLOSE_GRACE_SECONDS = 30.0
GAP_WARN_SECONDS = 2.0


class _Slot:
    """Runtime state of one configuration (not persisted: the lifecycle is)."""

    def __init__(self, machine: CandleMachine, cfg: CMConfig):
        self.machine, self.cfg = machine, cfg
        self.spec = self.spec_at = None
        self.spec_error: str | None = None
        self.offset = None
        self.offset_at = 0.0
        self.candle_checked_at = 0.0
        self.candle_error: str | None = None
        self.snap = None
        self.at_checked_at = 0.0
        self.last_quote_at: float | None = None
        self.last_bid_ask = None
        self.last_quote = None
        self.quote_error: str | None = None
        self.feed_down_since: float | None = None
        self.stale_logged = False
        self.ticks = 0                       # price updates actually seen (bid/ask changed)
        self.polls = 0                       # quote reads (the engine's cadence; >= ticks)
        self.tick_window: list = []
        self.pos_checked_at = 0.0
        self.pos_missing_since: dict = {}
        self.published_version = -1
        self.published_at = 0.0
        self.monitoring = True
        self.step_at: float | None = None


class CandleMomentumEngine:
    def __init__(self, feed, settings, config_store: ConfigStore, state_store: StateStore | None, events: EventLog, hub=None, clock=time.time,
                 pip_overrides=lambda: {}, on_signal=None, release_hook=None, async_fetch: bool = False, autopilot_status=None, threaded: bool = True, max_entries=None, anti_twin=None):
        self.anti_twin = anti_twin                                 # a trademate.bull_bear.antitwin.AntiTwin instance with ITS OWN ledger (None = not wired)
        self.max_entries = max_entries or (lambda: 1)
        self.feed, self.settings, self.cfg_store, self.state_store, self.events, self.hub = feed, settings, config_store, state_store, events, hub
        self._clock, self._pip_overrides, self.on_signal, self.release_hook = clock, pip_overrides, on_signal, release_hook
        self.autopilot_status = autopilot_status or (lambda: None)
        self._lock = threading.RLock()
        self._slots: dict = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._update = threading.Condition()
        self._update_seq = 0
        self._async = async_fetch
        self._threaded = threaded                                  # False: tests drive step() by hand (no worker thread)
        self._bg_lock = threading.Lock()
        self._bg_busy: set = set()
        self.last_step_at: float | None = None
        self.last_error: str | None = None
        self._lane = AntiTwinLane(lambda: self.anti_twin, feed, self._lane_resolve, self._lane_usable, self.feed_closed_lookup, clock)   # Anti-Twin's own quote-driven monitor
        self.cm = settings.candle_momentum

    # ------------------------------------------------------------------------------------------------------------------ control
    def effective_config(self, symbol: str, timeframe: str) -> tuple:
        return self.cfg_store.get(defaults_from_settings(self.settings, symbol, timeframe))

    def start(self, symbol: str, timeframe: str) -> tuple:
        """Begin (or keep) monitoring. Idempotent: calling it twice for the same key never creates a second slot or a second worker."""
        if timeframe not in EXEC_TIMEFRAMES:
            return False, f"Unknown timeframe {timeframe!r}."
        cfg, _ = self.effective_config(symbol, timeframe)
        problems = validate_config(cfg, EXEC_TIMEFRAMES)
        if problems:
            return False, " ".join(problems)
        with self._lock:
            k = cfg.key
            if k in self._slots:
                self._slots[k].monitoring = True
                self._spawn()
                return True, f"Already monitoring {symbol} {timeframe}."
            self._slots[k] = _Slot(CandleMachine(cfg, self.events, self.state_store, self._clock), cfg)
            self._slots[k].machine.max_entries = lambda: self.max_entries()
            self.events.emit("engine_started", None, symbol=symbol, timeframe=timeframe, poll_seconds=self.cm.poll_seconds)
            self._spawn()
        return True, f"Monitoring {symbol} {timeframe} tick by tick."

    def stop(self, symbol: str, timeframe: str) -> None:
        """Stop judging NEW entries for this key. A slot that still follows an open position stays (position tracking only) until it closes."""
        with self._lock:
            slot = self._slots.get(config_key(symbol, timeframe))
            if slot is None:
                return
            slot.monitoring = False
            if not slot.machine.open_position_tickets():
                self._slots.pop(slot.cfg.key, None)
                self.events.emit("engine_stopped", slot.machine.lc["candle_id"] if slot.machine.lc else None, symbol=symbol, timeframe=timeframe)
            if not any(s.monitoring or s.machine.open_position_tickets() for s in self._slots.values()):
                self._stop.set()

    def stop_all(self) -> None:
        with self._lock:
            for s in list(self._slots.values()):
                s.monitoring = False
        self._stop.set()

    def reload_config(self, symbol: str, timeframe: str) -> None:
        with self._lock:
            slot = self._slots.get(config_key(symbol, timeframe))
            if slot is not None:
                slot.cfg, _ = self.effective_config(symbol, timeframe)
                slot.machine.set_config(slot.cfg)
                slot.published_version = -1
                self.events.emit("config_changed", slot.machine.lc["candle_id"] if slot.machine.lc else None, symbol=symbol, timeframe=timeframe, config=slot.cfg.to_dict())

    def is_monitoring(self, symbol: str, timeframe: str) -> bool:
        with self._lock:
            s = self._slots.get(config_key(symbol, timeframe))
            return bool(s and s.monitoring and (not self._threaded or (self._thread is not None and self._thread.is_alive())))

    def _spawn(self) -> None:
        if not self._threaded:
            return
        if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
            self._lane.start(self._stop)                               # no-op while it is alive
            return
        # Each worker owns its OWN stop event. A worker that was just told to stop (and is still finishing its last cycle) keeps its set event and exits by itself;
        # a start() right after a stop() therefore gets a fresh, running worker instead of finding the dying one "alive" and leaving the slot unmonitored.
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), name="trademate-candle-momentum", daemon=True)
        self._thread.start()
        self._lane.start(self._stop)

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                self.step()
                self.last_error = None
            except Exception as exc:                                  # noqa: BLE001 - the monitor must survive; the failure is reported, never hidden
                logger.exception("Candle Momentum cycle failed")
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.events.emit("watchdog_failure", None, level="error", error=self.last_error)
            stop.wait(max(0.01, self.cm.poll_seconds - (time.monotonic() - t0)))

    def wait_update(self, seen: int, timeout: float) -> int:
        with self._update:
            if self._update_seq == seen:
                self._update.wait(timeout)
            return self._update_seq

    # ------------------------------------------------------------------------------------------------------------------ one cycle
    def step(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            try:
                self._step_slot(slot, now)
            except Exception as exc:                                  # noqa: BLE001 - one bad configuration must not stop the others
                logger.exception("Candle Momentum slot %s failed", slot.cfg.key)
                slot.machine.on_feed_problem("internal_error", f"{type(exc).__name__}: {exc}", now)
                self.events.emit("watchdog_failure", slot.machine.lc["candle_id"] if slot.machine.lc else None, symbol=slot.cfg.symbol, timeframe=slot.cfg.timeframe,
                                 level="error", error=str(exc))
        self.last_step_at = now
        if self.state_store is not None:
            self.state_store.flush_if_due()
        with self._lock:
            for k in [k for k, s in self._slots.items() if not s.monitoring and not s.machine.open_position_tickets()]:
                self._slots.pop(k, None)
        with self._update:
            self._update_seq += 1
            self._update.notify_all()

    def _step_slot(self, slot: _Slot, now: float) -> None:
        cfg, m = slot.cfg, slot.machine
        slot.step_at = now
        self._refresh_spec(slot, now)
        if slot.monitoring and cfg.enabled:
            self._refresh_candle(slot, now)
            self._read_quote(slot, now)
            m.on_time(now, slot.spec)
            cid = m.take_announcement()
            if cid:
                self._announce(slot, cid)
            lc = m.lc
            if lc and lc["state"] == State.ENTRY_VALIDATION.value and lc.get("_retrying") and not lc.get("_released"):
                lc["_released"] = True                                 # the previous order provably never left; free the one-order-per-setup claims for ONE bounded retry
                if self.release_hook:
                    self.release_hook(lc["candle_id"])
        self._follow_positions(slot, now)
        self._anti_twin_step(slot, now)
        self._publish(slot, now)

    # ---- specification / clock
    def _refresh_spec(self, slot: _Slot, now: float) -> None:
        if slot.spec is not None and now - slot.spec_at < self.cm.spec_refresh_seconds:
            return
        def job():
            try:
                spec = self.feed.spec(slot.cfg.symbol, self._pip_overrides(), now)
                slot.spec, slot.spec_at, slot.spec_error = spec, now, None
            except FeedError as exc:
                slot.spec_error = str(exc)
                slot.spec_at = now if slot.spec is None else slot.spec_at
            off = self.feed.server_offset(slot.cfg.symbol, now)
            if off is not None:
                slot.offset, slot.offset_at = off, now
        self._bg(slot, "spec", job)

    def _bg(self, slot: _Slot, name: str, job) -> None:
        if not self._async:
            job()
            return
        key = (slot.cfg.key, name)
        with self._bg_lock:
            if key in self._bg_busy:
                return
            self._bg_busy.add(key)
        def run():
            try:
                job()
            except Exception:                                          # noqa: BLE001
                logger.exception("Candle Momentum background job %s failed", name)
            finally:
                with self._bg_lock:
                    self._bg_busy.discard(key)
        threading.Thread(target=run, name=f"cm-{name}", daemon=True).start()

    # ---- candle boundary
    def _expected_open(self, slot: _Slot, now: float) -> int | None:
        if slot.offset is None:
            return None
        length = TF_SECONDS[slot.cfg.timeframe]
        return int(math.floor((now + slot.offset) / length) * length)

    def _refresh_candle(self, slot: _Slot, now: float) -> None:
        lc = slot.machine.lc
        expected = self._expected_open(slot, now)
        boundary_due = lc is None or (expected is not None and lc["open_time"] < expected)
        period = BOUNDARY_RETRY_SECONDS if boundary_due or expected is None else self.cm.candle_verify_seconds
        if expected is None:
            period = min(period, 1.0)                                  # clock unknown: verify against the broker every second instead of trusting a guess
        if now - slot.candle_checked_at < period:
            return
        slot.candle_checked_at = now
        def job():
            try:
                snap = self.feed.candle(slot.cfg.symbol, slot.cfg.timeframe)
            except FeedError as exc:
                slot.candle_error = str(exc)
                return
            slot.candle_error, slot.snap = None, snap
            slot.machine.begin_candle(snap, now)
        self._bg(slot, "candle", job)

    # ---- quotes
    def _read_quote(self, slot: _Slot, now: float) -> None:
        m, cfg = slot.machine, slot.cfg
        cid = m.lc["candle_id"] if m.lc else None
        try:
            ok, why = self.feed.connected()
            if not ok:
                raise FeedError(f"MT5 is not connected ({why}).")
            q = self.feed.quote(cfg.symbol)
        except FeedError as exc:
            slot.quote_error = str(exc)
            if slot.feed_down_since is None:
                slot.feed_down_since = now
                self.events.emit("feed_down", cid, symbol=cfg.symbol, timeframe=cfg.timeframe, level="warning", reason=slot.quote_error)
            m.on_feed_problem("feed_down", slot.quote_error, now)
            return
        if slot.feed_down_since is not None:
            self.events.emit("reconnected", cid, symbol=cfg.symbol, timeframe=cfg.timeframe, down_seconds=round(now - slot.feed_down_since, 2))
            slot.feed_down_since = None
        age = logic.quote_age(q, now)
        if not q.valid or age is None or age > cfg.quote_max_age_seconds:
            slot.quote_error = "The quote is missing or invalid." if not q.valid else f"The quote is {'of unknown age' if age is None else f'{age:.1f}s old'} (limit {cfg.quote_max_age_seconds:g}s)."
            if not slot.stale_logged:
                slot.stale_logged = True
                self.events.emit("quote_freshness", cid, symbol=cfg.symbol, timeframe=cfg.timeframe, level="warning", fresh=False, quote_age=age, limit=cfg.quote_max_age_seconds)
            m.on_feed_problem("stale_quote", slot.quote_error, now)
            return
        slot.quote_error, slot.stale_logged = None, False
        if m.feed_ok(now):
            self.events.emit("feed_recovered", cid, symbol=cfg.symbol, timeframe=cfg.timeframe)
        if slot.last_quote_at is not None and now - slot.last_quote_at > max(GAP_WARN_SECONDS, 4 * self.cm.poll_seconds):
            self.events.emit("quote_gap", cid, symbol=cfg.symbol, timeframe=cfg.timeframe, level="warning", gap_seconds=round(now - slot.last_quote_at, 3))
        slot.last_quote_at, slot.last_quote = now, q
        slot.polls += 1
        changed = (q.bid, q.ask) != slot.last_bid_ask
        slot.last_bid_ask = (q.bid, q.ask)
        if changed:                                                    # a genuine PRICE UPDATE (what the broker actually delivered), not merely a poll of the same quote
            slot.ticks += 1
            slot.tick_window = [t for t in slot.tick_window if now - t < 10.0] + [now]
        lc = m.lc
        if changed and lc and lc["state"] in {s.value for s in (State.TRIGGER_REACHED, State.ENTRY_VALIDATION, State.RISK_PENDING, State.EXECUTING)}:
            self.events.emit("tick_received", cid, symbol=cfg.symbol, timeframe=cfg.timeframe, bid=q.bid, ask=q.ask, quote_age=age)
        else:
            logger.debug("tick %s %s bid=%s ask=%s age=%s", cfg.symbol, cfg.timeframe, q.bid, q.ask, age)
        if slot.spec is None:
            m.on_feed_problem("no_spec", slot.spec_error or "The instrument specification is not available yet.", now)
            return
        m.on_quote(q, slot.spec, now)

    def _announce(self, slot: _Slot, candle_id: str) -> None:
        self._publish(slot, self._clock(), force=True)
        if self.on_signal:
            try:
                self.on_signal(slot.cfg.symbol, slot.cfg.timeframe, candle_id)
            except Exception:                                          # noqa: BLE001
                logger.exception("The signal hook failed")

    # ---- positions
    def _follow_positions(self, slot: _Slot, now: float) -> None:
        tickets = slot.machine.open_position_tickets()
        if not tickets or now - slot.pos_checked_at < POSITION_POLL_SECONDS:
            return
        slot.pos_checked_at = now
        try:
            open_now = self.feed.open_tickets()
        except FeedError:
            return
        for cid, ticket in tickets.items():
            if ticket in open_now:
                slot.pos_missing_since.pop(cid, None)
                continue
            try:
                closed = self.feed.closed().get(ticket)
            except FeedError:
                closed = None
            first = slot.pos_missing_since.setdefault(cid, now)
            if closed is not None:
                slot.machine.position_closed(cid, closed, now)
                slot.pos_missing_since.pop(cid, None)
            elif now - first > CLOSE_GRACE_SECONDS:
                slot.machine.position_closed(cid, {"reason": "unknown (not found in the broker's history)", "net": None}, now)
                slot.pos_missing_since.pop(cid, None)

    # ------------------------------------------------------------------------------------------------------------------ executor reports
    def report_execution(self, event: dict) -> bool:
        """Called by the Autopilot observer with what the executor did with a signal. Never raises."""
        try:
            slot = self._slots.get(config_key(event.get("symbol", ""), event.get("exec_tf", "")))
            if slot is None:
                return False
            ok = slot.machine.apply_execution(event, slot.spec, self._clock())
            if ok and event.get("stage") == "executed":
                self.adopt_now(slot.cfg.symbol, slot.cfg.timeframe)    # Anti-Twin watches the new position from this moment, not from the next cycle
            self._publish(slot, self._clock(), force=True)
            return ok
        except Exception:                                              # noqa: BLE001
            logger.exception("Could not apply an execution report")
            return False

    # ------------------------------------------------------------------------------------------------------------------ views
    def _anti_twin_step(self, slot: _Slot, now: float) -> None:
        """Adopt freshly opened originals, then let Anti-Twin judge the legs of this instrument on the live quote. Never raises.
        Judging also happens in the fast lane (trademate/antitwin_lane.py) on every new quote; this call is the same step again (idempotent), it never waits
        behind a reversal that the lane is carrying out, and it has NO throttle: the old 0.5 s gate is gone."""
        at, q = self.anti_twin, slot.last_quote
        if at is None or q is None or slot.spec is None or not slot.monitoring:
            return
        try:
            cfg = slot.cfg
            positions = self.feed.open_positions()
            if cfg.anti_twin_enabled:
                for lc in slot.machine.open_lifecycles():
                    at.try_adopt(lc, cfg, positions, slot.spec)
            at.step(q, slot.spec, positions, self.feed_closed_lookup, enabled=cfg.anti_twin_enabled, cfg=cfg, fresh_positions=self.feed.open_positions,
                    timeframe=cfg.timeframe, block=False)
        except FeedError:
            return
        except Exception:                                              # noqa: BLE001
            logger.exception("Anti-Twin could not run")

    # ---- the Anti-Twin fast lane (shared implementation: trademate/antitwin_lane.py)
    def _lane_resolve(self, symbol: str, timeframe):
        slot = self._slots.get(config_key(symbol, timeframe)) if timeframe else None
        if slot is None or not slot.monitoring or slot.spec is None:
            return None
        return slot.spec, slot.cfg

    def _lane_usable(self, q, cfg) -> bool:
        age = logic.quote_age(q, self._clock())
        return bool(q.valid and age is not None and age <= cfg.quote_max_age_seconds)

    def adopt_now(self, symbol: str, timeframe: str) -> None:
        """Start watching a position the moment its execution is reported (called from the execution report), instead of at the next engine cycle.
        Best effort and non-blocking: the engine cycle adopts it anyway."""
        at = self.anti_twin
        slot = self._slots.get(config_key(symbol, timeframe))
        if at is None or slot is None or slot.spec is None or not slot.cfg.anti_twin_enabled:
            return
        try:
            positions = self.feed.open_positions()
            for lc in slot.machine.open_lifecycles():
                at.try_adopt(lc, slot.cfg, positions, slot.spec)
        except Exception:                                              # noqa: BLE001 - never let a bookkeeping shortcut disturb an execution report
            logger.debug("Anti-Twin immediate adoption skipped", exc_info=True)

    def feed_closed_lookup(self, ticket):
        try:
            return self.feed.closed_info(ticket)
        except FeedError:
            return None

    def snapshot(self, symbol: str, timeframe: str, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        with self._lock:
            slot = self._slots.get(config_key(symbol, timeframe))
        cfg, custom = (slot.cfg, True) if slot else self.effective_config(symbol, timeframe)
        out = {"symbol": symbol, "timeframe": timeframe, "engine": self.engine_status(now), "config": cfg.to_dict(), "monitoring": bool(slot and slot.monitoring),
               "version": ENGINE_VERSION, "rule_id": RULE_ID, "server_time": now}
        if slot is None:
            out.update(machine=None, conversion=None, quote=None, feed=None)
            return out
        spec = slot.spec
        out["machine"] = slot.machine.snapshot()
        out["anti_twin"] = self.anti_twin.snapshot(symbol, timeframe) if self.anti_twin is not None else None
        out["conversion"] = self._conversion(slot, now)
        q = slot.last_quote
        rate = (len(slot.tick_window) - 1) / (slot.tick_window[-1] - slot.tick_window[0]) if len(slot.tick_window) > 1 and slot.tick_window[-1] > slot.tick_window[0] else None
        out["quote"] = {"bid": q.bid, "ask": q.ask, "age_seconds": logic.quote_age(q, now), "read_at": q.read_at} if q else None
        out["feed"] = {"ok": self._feed_ok(slot), "problem": slot.quote_error or slot.candle_error or (slot.spec_error if slot.spec is None else None),
                       "price_updates": slot.ticks, "price_updates_per_second": round(rate, 2) if rate else None, "polls": slot.polls, "last_quote_at": slot.last_quote_at,
                       "last_step_at": slot.step_at, "cadence_seconds": self.cm.poll_seconds}
        return out

    def _conversion(self, slot: _Slot, now: float) -> dict:
        spec = slot.spec
        if spec is None:
            return {"status": "UNVERIFIED", "reasons": [slot.spec_error or "The instrument specification has not been read yet."]}
        reasons = spec.blocking_reasons(now)
        d = {"status": "VERIFIED" if not reasons else "UNVERIFIED", "reasons": reasons, "unit": spec.unit, "unit_name": spec.unit_name, "unit_source": spec.unit_source,
             "asset_class": spec.asset_class, "tick_size": spec.tick_size, "point": spec.point, "digits": spec.digits, "convention": spec.convention_text(),
             "notes": list(spec.notes), "spec_age_seconds": None if slot.spec_at is None else round(now - slot.spec_at, 1)}
        try:
            d["trigger_price_distance"] = spec.units_to_price(slot.cfg.trigger_units)
            d["target_price_distance"] = spec.units_to_price(slot.cfg.target_units)
            if _pos(slot.cfg.sl_buffer_units):
                d["stop_price_distance"] = spec.units_to_price(slot.cfg.sl_buffer_units)
        except ValueError:
            pass
        return d

    def engine_status(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        alive = (self._thread is not None and self._thread.is_alive()) or (not self._threaded and bool(self._slots))
        return {"running": alive, "mode": "tick_engine", "cadence_seconds": self.cm.poll_seconds, "last_step_at": self.last_step_at,
                "last_step_age_seconds": None if self.last_step_at is None else round(max(0.0, now - self.last_step_at), 3), "last_error": self.last_error,
                "active_configurations": sorted(self._slots), "version": ENGINE_VERSION, "anti_twin_lane": self._lane.status()}

    # ------------------------------------------------------------------------------------------------------------------ authoritative result
    def result(self, symbol: str, timeframe: str, now: float | None = None) -> dict | None:
        """The pipeline-compatible result of this configuration: the ONE record the hub, the Command Centre, the Watchdog and Autopilot all use."""
        now = self._clock() if now is None else now
        with self._lock:
            slot = self._slots.get(config_key(symbol, timeframe))
        if slot is None:
            return None
        from trademate.candle_momentum.result import build_result_dict
        return build_result_dict(self.snapshot(symbol, timeframe, now), slot.snap, slot.spec, slot.cfg, now, offset=slot.offset, feed_ok=self._feed_ok(slot))

    @staticmethod
    def _feed_ok(slot: _Slot) -> bool:
        return slot.quote_error is None and slot.feed_down_since is None and slot.candle_error is None and slot.spec is not None

    def _publish(self, slot: _Slot, now: float, force: bool = False) -> None:
        if self.hub is None:
            return
        m = slot.machine
        if not force and m.version == slot.published_version and now - slot.published_at < 1.0:
            return
        slot.published_version, slot.published_at = m.version, now
        ticket = self.hub.begin()
        cfg = slot.cfg
        if not cfg.enabled:
            self.hub.publish(ticket, cfg.symbol, STRATEGY_ID, cfg.timeframe, state="data_problem", message="Candle Momentum is disabled for this instrument and timeframe.", source="engine")
            return
        if slot.feed_down_since is not None:
            self.hub.publish(ticket, cfg.symbol, STRATEGY_ID, cfg.timeframe, state="disconnected", message=slot.quote_error or "The broker feed is down.", source="engine")
            return
        res = self.result(cfg.symbol, cfg.timeframe, now)
        if res is None or not self._feed_ok(slot):
            self.hub.publish(ticket, cfg.symbol, STRATEGY_ID, cfg.timeframe, state="data_problem", message=slot.quote_error or slot.candle_error or "Waiting for market data.", source="engine")
            return
        self.hub.publish(ticket, cfg.symbol, STRATEGY_ID, cfg.timeframe, state="fresh", message="Live tick engine.", result=res, source="engine")

    # ------------------------------------------------------------------------------------------------------------------ one-shot (manual Analyse; no monitoring, nothing persisted)
    def evaluate_once(self, symbol: str, timeframe: str, now: float | None = None) -> dict:
        """The same rules on a transient machine, for a manual Analyse when nobody is monitoring. It reads the broker once, persists nothing and
        respects what the persistent store already knows about this candle."""
        now = self._clock() if now is None else now
        cfg, _ = self.effective_config(symbol, timeframe)
        slot = _Slot(CandleMachine(cfg, EventLog(None, 200), _ReadOnly(self.state_store), self._clock), cfg)
        slot.monitoring = False
        try:
            slot.spec = self.feed.spec(symbol, self._pip_overrides(), now)
            slot.spec_at = now
            slot.offset = self.feed.server_offset(symbol, now)
            slot.snap = self.feed.candle(symbol, timeframe)
            q = self.feed.quote(symbol)
        except FeedError as exc:
            slot.quote_error = str(exc)
            slot.candle_error = str(exc)
            q = None
        if slot.snap is not None:
            slot.machine.begin_candle(slot.snap, now)
        if q is not None and slot.spec is not None:
            slot.last_quote = q
            slot.machine.on_quote(q, slot.spec, now)
        from trademate.candle_momentum.result import build_result_dict
        with self._lock:
            tmp = self._slots
            self._slots = {**tmp, cfg.key: slot}
            try:
                snap = self.snapshot(symbol, timeframe, now)
            finally:
                self._slots = tmp
        snap["monitoring"] = False
        return build_result_dict(snap, slot.snap, slot.spec, cfg, now, offset=slot.offset, feed_ok=q is not None and slot.snap is not None, source="manual")


class _ReadOnly:
    """A view of the persistent store that can be read but never written (the manual one-shot must not claim or alter a candle's record)."""

    def __init__(self, store):
        self._s = store

    def get(self, cid):
        return self._s.get(cid) if self._s else None

    def for_key(self, key):
        return self._s.for_key(key) if self._s else []

    def put(self, lifecycle, immediate):
        return None


def _pos(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0
