"""Single-symbol, read-only background monitoring for the Command Centre."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time

from trademate.strategies.base import EXEC_TIMEFRAMES

logger = logging.getLogger("trademate.strategy.watchdog")

WATCHDOG_INTERVAL_SECONDS = 60
MAX_EVENTS = 100


class WatchdogManager:
    def __init__(self, evaluate_symbol, interval: float = WATCHDOG_INTERVAL_SECONDS, hub=None, engine=None, engines=None):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Watchdog interval must be a positive finite number.")
        self._evaluate_symbol = evaluate_symbol
        self._engines = {**({"candle_momentum": engine} if engine is not None else {}), **(engines or {})}     # strategy id -> its tick engine (Candle Momentum, Bull/Bear)
        self._engine = engine                     # the Candle Momentum tick engine: Candle Momentum is monitored by IT, never by the polling loop below
        self._engine_seq = 0
        self._hub = hub                           # DecisionHub: every analysis is published here with its full result (the panel reads it)
        self._exec_tf: str | None = None
        self._gen = 0                             # bumps whenever the monitored target changes; an evaluation that started under an older one is discarded
        self._wake = threading.Event()
        self._interval = interval
        self._lock = threading.RLock()
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._active = False
        self._symbol: str | None = None
        self._strategy_id: str | None = None
        self._state = "Stopped"
        self._message = "Watchdog is stopped."
        self._last_checked: float | None = None
        self._last_decision: str | None = None
        self._events: list[dict] = []
        self._next_event_id = 1
        self._seen_setups: set[str] = set()
        self._seen_chochs: set[str] = set()
        self._candidate_states: dict[str, dict] = {}
        self._candidate_events: list[dict] = []
        self._next_candidate_event_id = 1

    def start(self, symbol: str, strategy_id: str | None = None, exec_tf: str | None = None) -> tuple[bool, str]:
        """Monitor `symbol` with ONE strategy. A watchdog is bound to the strategy it was started with and never mixes candidates from another."""
        symbol = symbol.strip()
        if not symbol or len(symbol) > 32 or not symbol.isprintable():
            return False, "Select a valid broker instrument before starting the Watchdog."
        with self._lock:
            if self._active:
                if self._symbol == symbol:
                    return True, "Watchdog is already monitoring " + symbol + "."
                return False, "Watchdog is already monitoring " + str(self._symbol) + ". Stop it before selecting another instrument."
            if self._is_engine(strategy_id):
                if exec_tf not in EXEC_TIMEFRAMES:             # the user's timeframe is required: never substitute M5 (or anything else)
                    return False, f"Choose an execution timeframe ({', '.join(EXEC_TIMEFRAMES)}) before monitoring {strategy_id}."
                ok, why = self._eng(strategy_id).start(symbol, exec_tf)
                if not ok:
                    return False, why
            self._symbol = symbol
            self._strategy_id = strategy_id
            self._exec_tf = exec_tf
            self._gen += 1
            self._wake.clear()
            self._state = "Monitoring"
            self._message = "Starting read-only monitoring for " + symbol + "."
            self._last_checked = None
            self._last_decision = None
            self._stop_event = threading.Event()
            self._active = True
            self._thread = threading.Thread(target=self._run, args=(symbol, self._stop_event),
                                            name="trademate-watchdog", daemon=True)
            self._thread.start()
            return True, self._message

    def _is_engine(self, strategy_id) -> bool:
        return strategy_id in self._engines

    def _eng(self, strategy_id):
        return self._engines[strategy_id]

    def _release_engine(self) -> None:
        if self._is_engine(self._strategy_id) and self._symbol and self._exec_tf:
            self._eng(self._strategy_id).stop(self._symbol, self._exec_tf)

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            self._release_engine()
            if not self._active or self._stop_event is None:
                self._state = "Stopped"
                self._message = "Watchdog is stopped."
                return True, self._message
            self._state = "Stopping"
            self._message = "Stopping read-only monitoring."
            self._stop_event.set()
            self._wake.set()
            return True, self._message

    def retarget(self, symbol: str, strategy_id: str | None, exec_tf: str | None) -> tuple[bool, str]:
        """The user changed instrument, strategy or timeframe while monitoring: follow it WITHOUT a stop/start gap. Candidates and alert memory of the old
        target are forgotten (nothing from it may leak into the new one) and an evaluation already in flight for the old target is discarded."""
        symbol = (symbol or "").strip()
        if not symbol or len(symbol) > 32 or not symbol.isprintable():
            return False, "Select a valid broker instrument."
        with self._lock:
            if not self._active:
                return False, "Watchdog is not running."
            if (symbol, strategy_id, exec_tf) == (self._symbol, self._strategy_id, self._exec_tf):
                return True, "Already monitoring this selection."
            if self._is_engine(strategy_id):
                if exec_tf not in EXEC_TIMEFRAMES:             # the user's timeframe is required: never substitute M5 (or anything else)
                    return False, f"Choose an execution timeframe ({', '.join(EXEC_TIMEFRAMES)}) before monitoring {strategy_id}."
                ok, why = self._eng(strategy_id).start(symbol, exec_tf)
                if not ok:
                    return False, why
            self._release_engine()
            self._symbol, self._strategy_id, self._exec_tf = symbol, strategy_id, exec_tf
            self._gen += 1
            self._candidate_states.clear()
            self._candidate_events.clear()
            self._seen_setups.clear()
            self._seen_chochs.clear()
            self._last_decision = None
            self._last_checked = None
            self._state = "Monitoring"
            self._message = f"Now monitoring {symbol} ({strategy_id}, {exec_tf})."
            self._wake.set()
            return True, self._message

    def status(self, after_event_id: int = 0) -> dict:
        with self._lock:
            events = [dict(event) for event in self._events if event["id"] > after_event_id]
            return {
                "active": self._active,
                "symbol": self._symbol,
                "strategy_id": self._strategy_id,
                "exec_tf": self._exec_tf,
                "state": self._state,
                "message": self._message,
                "last_checked": self._last_checked,
                "last_decision": self._last_decision,
                "latest_event_id": self._next_event_id - 1,
                "events": events,
                "candidate_states": [dict(candidate) for candidate in self._candidate_states.values()],
                "candidate_events": [dict(event) for event in self._candidate_events],
                "interval_seconds": self._interval,
                "mode": "tick_engine" if self._is_engine(self._strategy_id) else "poll",
                **({"interval_seconds": self._eng(self._strategy_id).cm.poll_seconds, "engine": self._eng(self._strategy_id).engine_status()} if self._is_engine(self._strategy_id) else {}),
            }

    def reset_for_strategy_change(self) -> None:
        """The selected strategy changed: stop monitoring and forget every candidate, so nothing from the old strategy can be mixed into the new."""
        with self._lock:
            self._release_engine()
            if self._active and self._stop_event is not None:
                self._state = "Stopping"
                self._message = "The selected strategy changed; monitoring is stopping. Start the Watchdog again to monitor the new strategy."
                self._stop_event.set()
                self._wake.set()
            self._candidate_states.clear()
            self._candidate_events.clear()
            self._strategy_id = None

    def _call_evaluate(self, symbol: str, strategy_id: str | None, exec_tf: str | None = None):
        if strategy_id is None:
            return self._evaluate_symbol(symbol)
        if exec_tf is None:
            return self._evaluate_symbol(symbol, strategy_id=strategy_id)
        return self._evaluate_symbol(symbol, strategy_id=strategy_id, exec_tf=exec_tf)

    def _publish(self, ticket, symbol, strategy_id, exec_tf, gen, state, message, result=None) -> None:
        """Hand the outcome to the DecisionHub, unless the target changed while it was being computed."""
        if self._hub is None or ticket is None or strategy_id is None:
            return
        with self._lock:
            if gen != self._gen:
                return
        try:
            self._hub.publish(ticket, symbol, strategy_id, exec_tf or "", state=state, message=message, result=result, source="watchdog")
        except Exception:                                                      # noqa: BLE001 - publishing must never kill the monitor
            logger.exception("Could not publish the decision")

    def _run(self, symbol: str, stop_event: threading.Event) -> None:
        try:
            while not stop_event.is_set():
                with self._lock:
                    symbol, strategy_id, exec_tf, gen = self._symbol, self._strategy_id, self._exec_tf, self._gen
                if self._is_engine(strategy_id):                                           # event-driven: wake on every engine cycle, never sleep a polling interval
                    self._engine_cycle(symbol, strategy_id, exec_tf, gen, stop_event)
                    continue
                ticket = self._hub.begin() if self._hub is not None else None       # ordering ticket BEFORE any market data is read
                try:
                    payload = self._call_evaluate(symbol, strategy_id, exec_tf)
                    with self._lock:
                        if gen != self._gen:                                          # the target changed while this ran: discard, evaluate the new one now
                            continue
                    if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("result"), dict):
                        raise ValueError("Strategy Engine returned an invalid monitoring result.")
                    stamp = payload["result"].get("strategy") if isinstance(payload["result"].get("strategy"), dict) else None
                    if strategy_id is not None and (stamp is None or stamp.get("id") != strategy_id):
                        with self._lock:
                            self._state = "Strategy Changed"
                            self._message = "The analysis came from a different strategy than the one being monitored; monitoring stopped."
                            self._last_checked = time.time()
                            self._last_decision = None
                            self._suspend_candidates("strategy_changed", "The strategy changed.")
                        self._publish(ticket, symbol, strategy_id, exec_tf, gen, "error", "The analysis came from a different strategy than the one monitored.")
                        stop_event.set()
                        break
                    if payload.get("connection") != "CONNECTED":
                        with self._lock:
                            self._state = "Disconnected"
                            self._message = "Broker connection is unavailable; cached candidates are suspended, not fresh."
                            self._last_checked = time.time()
                            self._last_decision = None
                            self._suspend_candidates("disconnected", "Broker connection is unavailable.")
                        self._publish(ticket, symbol, strategy_id, exec_tf, gen, "disconnected", "Broker connection is unavailable; there is no current decision.")
                    else:
                        result = payload["result"]
                        statuses = {item.get("status") for item in result.get("data", {}).values() if isinstance(item, dict)}
                        stale = "stale" in statuses
                        incomplete = bool(statuses and statuses != {"ok"})
                        with self._lock:
                            if stale:
                                self._state = "Stale Data"
                                self._message = "Market data is stale; prior candidate states are suspended until fresh candles arrive."
                            elif incomplete:
                                self._state = "Data Incomplete"
                                self._message = "One or more timeframes are unavailable or degraded; candidates are not treated as fresh."
                            else:
                                self._state = "Waiting for Setup" if result.get("decision") in ("WAIT", "WATCH") else "Monitoring"
                                self._message = "Monitoring " + symbol + "; latest decision: " + str(result.get("decision", "unknown")) + "."
                            self._last_checked = time.time()
                            self._last_decision = result.get("decision")
                            unreliable_state = "stale_data" if stale else "data_incomplete" if incomplete else None
                            self._track_candidates(symbol, result, unreliable_state)
                            if gen == self._gen and self._hub is not None and ticket is not None and strategy_id is not None:
                                self._hub.publish(ticket, symbol, strategy_id, exec_tf or "", state="stale" if stale else "data_problem" if incomplete else "fresh",
                                                  message=self._message, result=result, source="watchdog")
                            event = self._new_setup_event(symbol, result, unreliable_state)
                            if event:
                                self._events.append(event)
                                self._events = self._events[-MAX_EVENTS:]
                            for choch_event in self._new_choch_events(symbol, result, unreliable_state):
                                self._events.append(choch_event)
                            self._events = self._events[-MAX_EVENTS:]
                except Exception:
                    logger.exception("Watchdog analysis failed for %s", symbol)
                    with self._lock:
                        self._state = "Unavailable"
                        self._message = "A monitoring analysis failed; cached candidate states are suspended. See logs/trademate.log; monitoring will retry."
                        self._last_checked = time.time()
                        self._last_decision = None
                        self._suspend_candidates("unavailable", "A monitoring analysis failed.")
                    self._publish(ticket, symbol, strategy_id, exec_tf, gen, "error", "A monitoring analysis failed; there is no current decision.")
                self._wake.wait(self._interval)
                self._wake.clear()
                if stop_event.is_set():
                    break
        finally:
            with self._lock:
                self._suspend_candidates("monitoring_stopped", "Watchdog monitoring stopped.")
                self._active = False
                self._state = "Stopped"
                self._message = "Watchdog is stopped."
                self._stop_event = None

    def _engine_cycle(self, symbol: str, strategy_id: str, exec_tf: str | None, gen: int, stop_event) -> None:
        """One Candle Momentum monitoring pass. The ENGINE owns the tick loop and publishes the authoritative decision itself; the Watchdog reports its
        real status and raises the notification from the SAME result (so a BUY notification can never disagree with the decision panel)."""
        eng = self._eng(strategy_id)
        self._engine_seq = eng.wait_update(self._engine_seq, 1.0)
        if stop_event.is_set():
            return
        try:
            snap = eng.snapshot(symbol, exec_tf)
            result = eng.result(symbol, exec_tf)
        except Exception:                                                     # noqa: BLE001
            logger.exception("Could not read the Candle Momentum engine")
            with self._lock:
                if gen == self._gen:
                    self._state, self._message, self._last_checked, self._last_decision = "Unavailable", "The Candle Momentum engine could not be read; monitoring will retry.", time.time(), None
            return
        with self._lock:
            if gen != self._gen:
                return
            feed = snap.get("feed") or {}
            eng = snap.get("engine") or {}
            if result is None or not eng.get("running"):
                self._state, self._message, self._last_decision = "Unavailable", "The Candle Momentum engine is not running for this selection.", None
                self._last_checked = time.time()
                return
            lc = (snap.get("machine") or {}).get("current") or {}
            if not feed.get("ok"):
                bad = feed.get("problem") or "no current quote"
                self._state = "Disconnected" if "not connected" in str(bad).lower() else "Stale Data"
                self._message = f"The live feed is not current ({bad}); no entry is judged until it recovers."
                unreliable = "disconnected" if self._state == "Disconnected" else "stale_data"
            else:
                self._state = "Waiting for Setup" if result.get("decision") in ("WAIT", "WATCH") else "Monitoring"
                self._message = (f"Monitoring {symbol} tick by tick ({eng.get('cadence_seconds')}s cadence); candle state: {lc.get('state', 'n/a')}.")
                unreliable = None
            self._last_checked = time.time()
            self._last_decision = result.get("decision")
            self._track_candidates(symbol, result, unreliable)
            event = self._new_setup_event(symbol, result, unreliable)
            if event:
                self._events.append(event)
                self._events = self._events[-MAX_EVENTS:]

    def _track_candidates(self, symbol: str, result: dict, unreliable_state: str | None) -> None:
        candidates = result.get("setup_candidates")
        if not isinstance(candidates, list):
            return
        if unreliable_state:
            self._suspend_candidates(unreliable_state, "Current market data is incomplete; cached candidates are not fresh.")
            return
        observed: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict) or not isinstance(candidate.get("candidate_id"), str):
                continue
            candidate_id = candidate["candidate_id"]
            observed.add(candidate_id)
            previous = self._candidate_states.get(candidate_id)
            if candidate.get("status") in ("qualified", "invalidated"):
                state = candidate["status"]
            elif previous is None:
                state = "awaiting_event" if candidate.get("missing_evidence") else "developing"
            elif (len(candidate.get("supporting_evidence", ())) > len(previous.get("supporting_evidence", ()))
                  or len(candidate.get("missing_evidence", ())) < len(previous.get("missing_evidence", ()))):
                state = "strengthening"
            else:
                state = previous.get("state", "developing")
            snapshot = {
                "candidate_id": candidate_id,
                "symbol": symbol,
                "setup_family": candidate.get("setup_family"),
                "direction": candidate.get("direction"),
                "state": state,
                "premise": candidate.get("premise", ""),
                "supporting_evidence": list(candidate.get("supporting_evidence", ())),
                "conflicting_evidence": list(candidate.get("conflicting_evidence", ())),
                "missing_evidence": list(candidate.get("missing_evidence", ())),
                "invalidation_conditions": list(candidate.get("invalidation_conditions", ())),
                "last_updated": time.time(),
            }
            if previous is None:
                self._candidate_transition(snapshot, "new_candidate")
            elif previous.get("state") != state:
                event_type = "candidate_" + state
                self._candidate_transition(snapshot, event_type)
            self._candidate_states[candidate_id] = snapshot

        for candidate_id, previous in list(self._candidate_states.items()):
            if previous.get("symbol") != symbol or candidate_id in observed:
                continue
            if previous.get("state") in ("invalidated", "no_longer_observed"):
                continue
            snapshot = dict(previous, state="no_longer_observed", last_updated=time.time())
            self._candidate_transition(snapshot, "candidate_no_longer_observed")
            self._candidate_states[candidate_id] = snapshot

    def _suspend_candidates(self, state: str, reason: str) -> None:
        for candidate_id, previous in list(self._candidate_states.items()):
            if previous.get("symbol") != self._symbol or previous.get("state") in ("invalidated", "no_longer_observed"):
                continue
            snapshot = dict(previous, state=state, last_updated=time.time(), suspension_reason=reason)
            if previous.get("state") != state:
                self._candidate_transition(snapshot, "candidate_" + state)
            self._candidate_states[candidate_id] = snapshot

    def _candidate_transition(self, candidate: dict, event_type: str) -> None:
        key_data = {"candidate_id": candidate["candidate_id"], "state": candidate["state"]}
        event_key = hashlib.sha256(json.dumps(key_data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self._candidate_events.append({
            "id": self._next_candidate_event_id,
            "event_key": event_key,
            "event_type": event_type,
            "candidate_id": candidate["candidate_id"],
            "symbol": candidate["symbol"],
            "setup_family": candidate.get("setup_family"),
            "direction": candidate.get("direction"),
            "state": candidate["state"],
            "timestamp": time.time(),
            "summary": candidate.get("premise") or candidate.get("suspension_reason") or event_type.replace("_", " "),
        })
        self._next_candidate_event_id += 1
        self._candidate_events = self._candidate_events[-MAX_EVENTS:]

    def _new_choch_events(self, symbol: str, result: dict, unreliable_state: str | None) -> list[dict]:
        """Emit one alert per newly observed, confirmed closed-candle CHoCH.

        Events are only eligible when the overall monitoring pass is reliable and the
        source timeframe itself reports usable data. Provisional/intrabar breaks and
        same-candle conflicts are deliberately ignored.
        """
        if unreliable_state:
            return []
        timeframes = result.get("timeframes")
        data = result.get("data")
        if not isinstance(timeframes, dict) or not isinstance(data, dict):
            return []
        created: list[dict] = []
        stamp = result.get("strategy") if isinstance(result.get("strategy"), dict) else None
        if stamp is not None and not stamp.get("emits_choch_alerts"):
            return []                                  # this strategy's setups do not depend on CHoCH events: no CHoCH alerts
        for timeframe, analysis in timeframes.items():
            if stamp is not None and timeframe not in stamp.get("choch_alert_timeframes", ()):
                continue
            if not isinstance(analysis, dict) or analysis.get("data_status") != "ok":
                continue
            frame_data = data.get(timeframe)
            if not isinstance(frame_data, dict) or frame_data.get("status") != "ok":
                continue
            events = analysis.get("events")
            if not isinstance(events, list):
                continue
            for event in events:
                if not isinstance(event, dict) or event.get("type") != "CHOCH":
                    continue
                if event.get("confirmed") is not True or event.get("same_candle_conflict") is True:
                    continue
                candle_time = event.get("candle_time")
                direction = event.get("direction")
                level = event.get("level")
                if not isinstance(candle_time, (int, float)) or not isinstance(direction, str):
                    continue
                if not isinstance(level, (int, float)) or not math.isfinite(level):
                    continue
                signature = {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "candle_time": candle_time,
                    "direction": direction,
                    "level": level,
                }
                event_key = hashlib.sha256(json.dumps(signature, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                if event_key in self._seen_chochs:
                    continue
                self._seen_chochs.add(event_key)
                created.append({
                    "id": self._next_event_id,
                    "event_key": event_key,
                    "event_type": "choch",
                    "strategy_id": stamp.get("id") if stamp else None,
                    "timestamp": time.time(),
                    "candle_time": candle_time,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "direction": direction,
                    "level": float(level),
                    "summary": f"Confirmed {direction} CHoCH on {timeframe} at {level:g}.",
                })
                self._next_event_id += 1
        return created

    def _new_setup_event(self, symbol: str, result: dict, unreliable_state: str | None = None) -> dict | None:
        # A stale/incomplete/disconnected monitoring pass must never create a fresh trade alert.
        if unreliable_state or result.get("decision") not in ("BUY", "SELL") or result.get("setup_status") != "confirmed":
            return None
        entry = self._level(result.get("entry"))
        stop_loss = self._level(result.get("stop_loss"))
        target = self._level(result.get("take_profit_1"))
        if entry is None or stop_loss is None or target is None:
            return None
        # A setup is only alertable when its trade geometry is internally valid.
        decision = result["decision"]
        if (decision == "BUY" and not stop_loss < entry < target) or (decision == "SELL" and not target < entry < stop_loss):
            return None
        rr = result.get("risk_reward")
        if isinstance(rr, bool) or not isinstance(rr, (int, float)) or not math.isfinite(rr):
            risk = abs(entry - stop_loss)
            rr = abs(target - entry) / risk if risk > 0 else None
        selected_family = result.get("selected_setup_family")
        candidates = result.get("setup_candidates")
        selected = next((c for c in candidates if isinstance(c, dict) and c.get("setup_family") == selected_family and c.get("direction") in ("bullish" if decision == "BUY" else "bearish", decision)), None) if isinstance(candidates, list) else None
        signature = {
            "symbol": symbol,
            "decision": decision,
            "setup_family": selected_family or (selected.get("setup_family") if selected else "strategy_qualified"),
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit_1": target,
        }
        event_key = hashlib.sha256(json.dumps(signature, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if event_key in self._seen_setups:
            return None
        self._seen_setups.add(event_key)
        context = result.get("context") if isinstance(result.get("context"), dict) else {}
        event = {
            "id": self._next_event_id,
            "event_key": event_key,
            "timestamp": time.time(),
            **signature,
            "strategy_id": (result.get("strategy") or {}).get("id") if isinstance(result.get("strategy"), dict) else None,
            "strategy_version": (result.get("strategy") or {}).get("version") if isinstance(result.get("strategy"), dict) else None,
            "risk_reward": float(rr) if isinstance(rr, (int, float)) and not isinstance(rr, bool) and math.isfinite(rr) else None,
            "supporting_evidence": list(result.get("confirmation_evidence", ())) if isinstance(result.get("confirmation_evidence"), (list, tuple)) else [],
            "invalidation_conditions": list(selected.get("invalidation_conditions", ())) if isinstance(selected, dict) else [],
            "summary": context.get("summary", "The Strategy Engine qualified a setup."),
        }
        self._next_event_id += 1
        return event

    @staticmethod
    def _level(value) -> float | None:
        if not isinstance(value, dict):
            return None
        price = value.get("price")
        return float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price) else None
