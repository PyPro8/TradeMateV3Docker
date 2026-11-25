"""Autopilot: executes setups the selected strategy has produced and the EXISTING Risk Engine approves. Nothing else.

It owns no strategy logic, no sizing and no second execution path. Each cycle it:
  1. settles its own open trades from the broker (positions + deal history),
  2. checks the ONE configured stop condition (backend-enforced),
  3. reads the current decision from the DecisionHub (the same record the Command Centre shows) and refuses anything stale, absent or WAIT/WATCH,
  4. calls `propose` (the pipeline: fresh strategy evaluation -> levels from the executable price -> lot validation -> Risk Engine -> approval),
  5. calls `execute` (the pipeline's prepare_execution + the normal order journal path, with the Autopilot arming as the confirmation).

A RUN = one strategy-approved trade, opened by Autopilot, that has since been closed (TP, SL or any other exit) and appears in the broker's deal history.
Only one Autopilot trade is open at a time: that is what makes a "run" countable and a cycle a cycle.
Profit target = the sum of NET realised P&L (profit + commission + swap, account currency) of this Autopilot run's closed trades. Floating P&L is never counted.
Elapsed = wall-clock since the run started; persisted, so refreshes and restarts do not reset it.

When a stop condition is reached (or on emergency stop) no NEW entry is ever attempted again. Open positions are NOT abandoned and NOT force-closed:
they keep the stop-loss and take-profit the broker holds, and Autopilot keeps tracking them until they close. Use the normal Close button to exit sooner.
After an application restart Autopilot never resumes by itself: it comes back as "interrupted" and needs an explicit Resume (the run's counters and start time are kept).
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger("trademate.autopilot")

MODES = ("runs", "profit", "hours")
MODE_LABELS = {"runs": "Number of runs", "profit": "Profit target", "hours": "Elapsed hours"}
ATTEMPTS_KEPT = 500


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate_stop_config(mode, value, max_runs: int = 1000, max_hours: float = 720.0) -> tuple[bool, str, float | int | None]:
    """Exactly one mode with its own value. Returns (ok, message, normalised_value)."""
    if mode not in MODES:
        return False, "Choose exactly one stop condition: number of runs, profit target or elapsed hours.", None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return False, f"{MODE_LABELS[mode]} needs a number.", None
    if not _num(value):
        return False, f"{MODE_LABELS[mode]} needs a number.", None
    if mode == "runs":
        if value != int(value) or value < 1:
            return False, "Number of runs must be a whole number of at least 1.", None
        if value > max_runs:
            return False, f"Number of runs cannot exceed {max_runs}.", None
        return True, "", int(value)
    if mode == "profit":
        if value <= 0 or value > 1e9:
            return False, "The profit target must be an amount greater than 0 (account currency).", None
        return True, "", float(value)
    if value <= 0:
        return False, "Elapsed hours must be greater than 0.", None
    if value > max_hours:
        return False, f"Elapsed hours cannot exceed {max_hours:g}.", None
    return True, "", float(value)


def check_stop(mode: str, value, started_at: float, now: float, completed_runs: int, net_pnl: float) -> tuple[bool, dict]:
    """Pure. (reached, progress). Progress always carries the selected mode's numbers for the UI."""
    if mode == "runs":
        return completed_runs >= value, {"mode": mode, "current": completed_runs, "target": value, "unit": "runs", "fraction": min(1.0, completed_runs / value)}
    if mode == "profit":
        return net_pnl >= value - 1e-9, {"mode": mode, "current": round(net_pnl, 2), "target": value, "unit": "currency", "fraction": max(0.0, min(1.0, net_pnl / value))}
    elapsed = max(0.0, now - started_at) / 3600.0
    return elapsed >= value, {"mode": mode, "current": round(elapsed, 4), "target": value, "unit": "hours", "fraction": min(1.0, elapsed / value)}


class Autopilot:
    """All broker / pipeline access is injected, so the rules above are testable without MT5.

    selection()          -> (symbol, strategy_id, exec_tf)
    hub_get(sym, sid, tf) -> DecisionHub record or None
    lot_for(symbol)      -> validated saved lot or None
    propose(symbol, sid, tf, volume) -> {"approved": bool, "approval_id", "message", "code", ...}
    execute(approval_id) -> {"outcome": "executed"|"accepted_unconfirmed"|..., "ticket": int|None, "message", "order_sent": bool}
    positions()          -> set of open position tickets (raises on failure)
    history()            -> [{"ticket", "net", "time_close"}] closed trades (raises on failure)
    precheck()           -> None or a message that forbids arming (trading disabled, MT5 down ...)
    """

    def __init__(self, path, *, selection, hub_get, lot_for, propose, execute, positions, history, precheck=lambda: None, currency=lambda: None,
                 poll_seconds=5.0, max_decision_age=30.0, max_runs=1000, max_hours=720.0, now=time.time, observer=None, max_entries=lambda: 1):
        self._max_entries = max_entries                  # entries allowed per candle (the user's setting; 1 = one setup, one order)
        self.path = Path(path) if path else None
        self._selection, self._hub_get, self._lot_for = selection, hub_get, lot_for
        self._propose, self._execute, self._positions, self._history = propose, execute, positions, history
        self._precheck, self._currency = precheck, currency
        self.poll, self.max_age, self.max_runs, self.max_hours, self._now = poll_seconds, max_decision_age, max_runs, max_hours, now
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()                   # set by a strategy engine the instant a validated signal exists: no waiting for the next poll
        self._observer = observer                        # observer(dict): what happened to a signal (proposing / approved / rejected / refused / executed / failed / unconfirmed)
        self._thread: threading.Thread | None = None
        self._events: list[dict] = []
        self._next_event = 1
        self.s = self._blank()
        self._load()

    # ------------------------------------------------------------------ state
    @staticmethod
    def _blank() -> dict:
        return {"state": "off", "armed": False, "mode": None, "value": None, "started_at": None, "run_id": None, "completed_runs": 0, "net_pnl": 0.0,
                "outstanding": [], "attempted": [], "stopped_reason": None, "stopped_at": None, "last_message": "Autopilot is off.", "symbol": None,
                "strategy_id": None, "exec_tf": None, "lot": None, "trades": [], "entries": {}, "paused": False}

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("Autopilot state unreadable; starting off.")
            return
        if not isinstance(data, dict):
            return
        self.s = {**self._blank(), **{k: v for k, v in data.items() if k in self._blank()}}
        if self.s["armed"]:                       # never resume trading by itself after a restart
            self.s.update(armed=False, state="interrupted", last_message="TradeMate restarted while Autopilot was running. It is paused: press Resume to continue "
                                                                       "this run (counters and start time are kept). Open positions keep their stop-loss and take-profit.")
            self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.s, fh)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("Could not save Autopilot state")

    def _event(self, level: str, text: str) -> None:
        with self._lock:
            self._events.append({"id": self._next_event, "ts": self._now(), "level": level, "text": text})
            self._next_event += 1
            del self._events[:-100]
        logger.info("Autopilot: %s", text)

    # ------------------------------------------------------------------ control
    def start(self, mode, value, *, resume: bool = False, spawn: bool = True) -> tuple[bool, str]:
        ok, msg, norm = validate_stop_config(mode, value, self.max_runs, self.max_hours)
        if not ok:
            return False, msg
        with self._lock:
            if self.s["armed"]:
                return False, "Autopilot is already running. Stop it before changing its stop condition."
            problem = self._precheck()
            if problem:
                return False, problem
            symbol, sid, tf = self._selection()
            lot = self._lot_for(symbol)
            if lot is None:
                return False, f"Set and save a valid lot size for {symbol} before starting Autopilot."
            keep = resume and self.s["state"] == "interrupted" and self.s["run_id"] and self.s["mode"] == mode and self.s["value"] == norm
            if keep:
                self.s.update(armed=True, state="running", last_message="Autopilot resumed.")
            else:
                if self.s["outstanding"]:
                    return False, "Autopilot still tracks an open trade from the previous run. Wait for it to close (or close it), then start a new run."
                self.s = {**self._blank(), "armed": True, "state": "running", "mode": mode, "value": norm, "started_at": self._now(),
                          "run_id": f"run-{int(self._now())}", "last_message": "Autopilot started."}
            self.s.update(symbol=symbol, strategy_id=sid, exec_tf=tf, lot=lot)
            self._save()
        self._event("info", f"Autopilot {'resumed' if keep else 'started'}: {MODE_LABELS[mode]} = {norm:g}; {symbol}, {sid}, {tf}, {lot:g} lots.")
        if spawn:
            self._spawn()
        return True, "Autopilot is running."

    def stop(self, reason: str = "Stopped by the user.", *, emergency: bool = False) -> tuple[bool, str]:
        """Immediately no new entries. Open positions are left to their stop-loss / take-profit."""
        with self._lock:
            if not self.s["armed"] and self.s["state"] not in ("running", "paused"):
                return True, "Autopilot is not running."
            self.s.update(armed=False, paused=False, state="emergency_stopped" if emergency else "stopped", stopped_reason=reason, stopped_at=self._now(),
                          last_message=reason + (" Open positions keep their stop-loss and take-profit." if self.s["outstanding"] else ""))
            self._save()
        self._event("warning", reason)
        return True, self.s["last_message"]

    def pause(self) -> tuple[bool, str]:
        """Temporarily suspend NEW trading. The session (mode, stop condition, counters, selection, lot) is kept; open trades are still tracked to their close."""
        with self._lock:
            if not self.s["armed"]:
                return False, "Autopilot is not running, so there is nothing to pause."
            if self.s.get("paused"):
                return True, "Autopilot is already paused."
            self.s.update(paused=True, state="paused", last_message="Autopilot is paused: no new trades are opened. The session is kept; press Resume to continue it.")
            self._save()
        self._event("warning", "Autopilot paused by the user.")
        return True, self.s["last_message"]

    def unpause(self) -> tuple[bool, str]:
        with self._lock:
            if not (self.s["armed"] and self.s.get("paused")):
                return False, "Autopilot is not paused."
            self.s.update(paused=False, state="running", last_message="Autopilot resumed after the pause.")
            self._save()
        self._event("info", "Autopilot resumed after a pause.")
        return True, self.s["last_message"]

    def _spawn(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="autopilot", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """A signal is ready: look at it NOW. Harmless when Autopilot is not armed (it never trades unarmed)."""
        self._wake.set()

    def forget_attempt(self, candidate_id: str) -> bool:
        """Free ONE setup for ONE deliberate bounded retry. Only the strategy engine calls this, and only after an order that provably was not sent."""
        with self._lock:
            if candidate_id in self.s["attempted"]:
                self.s["attempted"] = [c for c in self.s["attempted"] if c != candidate_id]
                self._save()
                return True
        return False

    def _observe(self, **ev) -> None:
        if self._observer is None:
            return
        try:
            self._observer(ev)
        except Exception:                                    # noqa: BLE001 - reporting must never affect trading
            logger.exception("Autopilot observer failed")

    def _loop(self) -> None:
        """Worker loop with race-safe retirement.

        The worker reference must be cleared *under the same lock* used by
        start()/_spawn() before this thread returns. Otherwise start() can see
        the old thread as alive, decline to spawn a replacement, and then have
        that old thread exit immediately afterwards (leaving Autopilot armed
        with no worker).
        """
        current = threading.current_thread()
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:                                # noqa: BLE001 - fail closed; keep the monitor alive
                    logger.exception("Autopilot cycle failed")
                with self._lock:
                    if not self.s["armed"] and not self.s["outstanding"]:
                        # Atomic retirement: a concurrent start() cannot observe
                        # this thread as alive after deciding not to spawn.
                        if self._thread is current:
                            self._thread = None
                        return
                self._wake.wait(self.poll)
                self._wake.clear()
        finally:
            # Also clean up on shutdown or an unexpected worker exit. Identity
            # protects a newer worker spawned while this one was winding down.
            with self._lock:
                if self._thread is current:
                    self._thread = None

    # ------------------------------------------------------------------ one cycle
    def tick(self) -> None:
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            self._settle()
            self._check_stop()
            with self._lock:
                if not self.s["armed"]:
                    return
            self._try_entry()
        finally:
            self._tick_lock.release()

    def _settle(self) -> None:
        with self._lock:
            out = list(self.s["outstanding"])
        if not out:
            return
        try:
            open_now = set(self._positions())
            closed = {t["ticket"]: t for t in self._history()}
        except Exception as exc:                                  # noqa: BLE001
            with self._lock:
                self.s["last_message"] = f"Cannot read positions/history from the broker ({exc}); no new entry until it can."
            return
        for o in out:
            tk = o["ticket"]
            if tk in open_now:
                continue
            h = closed.get(tk)
            if h is None:
                continue                                          # closed but not in history yet: keep waiting, entries stay blocked
            net = h.get("net")
            with self._lock:
                self.s["outstanding"] = [x for x in self.s["outstanding"] if x["ticket"] != tk]
                self.s["completed_runs"] += 1
                if _num(net):
                    self.s["net_pnl"] = round(self.s["net_pnl"] + net, 2)
                self.s["trades"] = (self.s["trades"] + [{"ticket": tk, "net": net if _num(net) else None, "closed_at": h.get("time_close"), "candidate_id": o.get("candidate_id")}])[-200:]
                self._save()
            self._event("info", f"Run {self.s['completed_runs']} finished: position {tk} closed, net {net if _num(net) else 'unknown'}.")

    def _check_stop(self) -> None:
        with self._lock:
            if not self.s["armed"] or self.s["mode"] is None:
                return
            reached, prog = check_stop(self.s["mode"], self.s["value"], self.s["started_at"], self._now(), self.s["completed_runs"], self.s["net_pnl"])
            if not reached:
                return
            cur, tgt = prog["current"], prog["target"]
            reason = {"runs": f"Stop condition reached: {cur} of {tgt} runs completed.",
                      "profit": f"Stop condition reached: net realised profit {cur} reached the target {tgt:g}.",
                      "hours": f"Stop condition reached: {tgt:g} hours elapsed."}[self.s["mode"]]
            self.s.update(armed=False, state="finished", stopped_reason=reason, stopped_at=self._now(),
                          last_message=reason + (" No new trades will be opened; the open position keeps its stop-loss and take-profit." if self.s["outstanding"] else " No new trades will be opened."))
            self._save()
        self._event("success", reason)

    def _idle(self, text: str) -> None:
        with self._lock:
            self.s["last_message"] = text

    def _try_entry(self) -> None:
        with self._lock:
            if self.s.get("paused"):
                self.s["last_message"] = "Autopilot is paused: no new trades are opened."
                return
            if self.s["outstanding"]:
                self.s["last_message"] = "Managing an open Autopilot trade; the next entry waits until it closes."
                return
            symbol, sid, tf = self._selection()
            if (symbol, sid, tf) != (self.s["symbol"], self.s["strategy_id"], self.s["exec_tf"]):
                self.s["last_message"] = "The instrument, strategy or timeframe changed since Autopilot started. It will not trade the new selection; stop and start it again."
                return
        rec = self._hub_get(symbol, sid, tf)
        if rec is None or rec.get("state") != "fresh":
            return self._idle("Waiting for a fresh analysis (Watchdog must be running)." if rec is None else f"No current decision ({rec.get('state')}): {rec.get('message', '')}")
        if rec.get("age_seconds", 0) > self.max_age:
            return self._idle("The latest analysis is too old to act on.")
        result = rec.get("result") or {}
        decision = result.get("decision")
        if decision not in ("BUY", "SELL"):
            return self._idle(f"Waiting for a setup (current decision: {decision}).")
        cands = [c for c in result.get("setup_candidates", []) if c.get("execution_eligible")]
        cid = cands[0].get("candidate_id") if cands else None
        if not cid:
            return self._idle("The current decision carries no identifiable setup; not executing.")
        with self._lock:
            tried = self.s["attempted"].count(cid)
            done = int((self.s.get("entries") or {}).get(cid, 0))
            try:
                limit = min(int(self._max_entries() or 1), 2)
            except Exception:                                  # noqa: BLE001 - a broken setting means ONE entry
                limit = 1
            second_ok = tried >= 1 and done == tried and done < limit     # every earlier attempt on this setup really opened a trade and the setting allows one more
            if tried >= 1 and not second_ok:                   # keep the refusal reason visible instead of overwriting it every cycle
                if not self.s["last_message"].startswith(("Setup not approved", "Order not placed", "Opened")):
                    self.s["last_message"] = "This setup was already attempted; waiting for the next one."
                return
        lot = self._lot_for(symbol)
        if lot is None:
            return self._idle(f"No valid saved lot size for {symbol}.")
        # last look at the stop condition, then mark the setup attempted BEFORE anything is sent (one attempt per setup, whatever happens)
        self._check_stop()
        with self._lock:
            if not self.s["armed"]:
                return
            self.s["attempted"] = (self.s["attempted"] + [cid])[-ATTEMPTS_KEPT:]
            self.s["lot"] = lot
            self._save()
        base = dict(symbol=symbol, strategy_id=sid, exec_tf=tf, candidate_id=cid)
        self._observe(stage="proposing", **base)
        rep = self._propose(symbol, sid, tf, lot)
        if not rep.get("approved"):
            msg = f"Setup not approved: {rep.get('message', 'refused')}"
            self._idle(msg)
            self._event("warning", msg)
            self._observe(stage="rejected", code=rep.get("code"), message=rep.get("message"), **base)
            return
        self._observe(stage="approved", volume=lot, message=rep.get("message"), **base)
        res = self._execute(rep["approval_id"])
        outcome = res.get("outcome")
        if outcome == "executed" and res.get("ticket"):
            self._observe(stage="executed", ticket=res["ticket"], fill_price=res.get("fill_price"), **base)
            with self._lock:
                self.s["outstanding"].append({"ticket": res["ticket"], "candidate_id": cid, "approval_id": rep["approval_id"], "opened_at": self._now()})
                self.s.setdefault("entries", {})[cid] = int(self.s.get("entries", {}).get(cid, 0)) + 1
                if len(self.s["entries"]) > ATTEMPTS_KEPT:
                    self.s["entries"].pop(next(iter(self.s["entries"])))
                eff = (res.get("lot_adjustment") or {}).get("adjusted_lot") or lot
                self.s["last_message"] = f"Opened {decision} {symbol} ({eff:g} lots" + (f", reduced from {lot:g}: insufficient funds" if eff != lot else "") + "); tracking it to its close."
                self._save()
            self._event("success", self.s["last_message"])
        elif outcome in ("accepted_unconfirmed", "unknown") or res.get("order_sent"):
            self._observe(stage="unconfirmed" if outcome in ("accepted_unconfirmed", "unknown") else "failed", code=outcome, message=res.get("message"), order_sent=bool(res.get("order_sent")), **base)
            with self._lock:
                self.s.update(armed=False, state="blocked", stopped_reason="An order's outcome is unconfirmed.", stopped_at=self._now(),
                              last_message="An order was sent but not confirmed. Autopilot stopped: check MT5 and reconcile before starting again.")
                self._save()
            self._event("error", self.s["last_message"])
        else:
            msg = f"Order not placed: {res.get('message', 'refused')}"
            self._idle(msg)
            self._event("warning", msg)
            self._observe(stage="refused", code=res.get("code") or outcome, message=res.get("message"), order_sent=False, **base)

    # ------------------------------------------------------------------ views
    def status(self, after_event_id: int = 0) -> dict:
        with self._lock:
            s = dict(self.s)
            events = [e for e in self._events if e["id"] > after_event_id]
        prog = None
        if s["mode"]:
            _, prog = check_stop(s["mode"], s["value"], s["started_at"] or self._now(), self._now(), s["completed_runs"], s["net_pnl"])
        return {"ok": True, "state": s["state"], "armed": s["armed"], "mode": s["mode"], "mode_label": MODE_LABELS.get(s["mode"]), "value": s["value"],
                "progress": prog, "completed_runs": s["completed_runs"], "net_pnl": s["net_pnl"], "currency": self._currency(), "open_trades": len(s["outstanding"]),
                "started_at": s["started_at"], "stopped_reason": s["stopped_reason"], "stopped_at": s["stopped_at"], "message": s["last_message"],
                "symbol": s["symbol"], "strategy_id": s["strategy_id"], "exec_tf": s["exec_tf"], "lot": s["lot"], "trades": s["trades"][-20:],
                "paused": bool(s.get("paused")), "emergency_stopped": s["state"] == "emergency_stopped",
                "events": events, "server_time": self._now(), "limits": {"max_runs": self.max_runs, "max_hours": self.max_hours}}
