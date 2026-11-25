"""Central, persistent safety protections consulted by the Risk Engine for EVERY proposed trade (every strategy, Autopilot, manual and AI route).

SafetyLock          the Emergency Stop lock. While locked, no new order is approved. Only an explicit, confirmed unlock lifts it. Survives restarts.
ConsecutiveLossGuard  the user-configurable consecutive-loss protection: enable/disable, threshold, live count, manual authorised reset, persistent,
                    counts COMPLETED losing trades once each (by ticket), a completed win resets the count, open losing positions are never counted.
Both keep their history: nothing is deleted when a lock is lifted or a counter is reset (the records only grow, capped at a generous size).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger("trademate.protections")
RECORDS_KEPT = 1000
PROCESSED_KEPT = 5000


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _load(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("%s is unreadable; treating it as empty (the file is left untouched).", path)
        return {}


class SafetyLock:
    UNLOCK_PHRASE = "UNLOCK"

    def __init__(self, path, now=time.time):
        self.path = Path(path) if path else None
        self._now = now
        self._lock = threading.RLock()
        d = _load(self.path)
        self.s = {"locked": bool(d.get("locked", False)), "reason": d.get("reason"), "since": d.get("since"), "history": list(d.get("history", []))[-RECORDS_KEPT:]}

    def _save(self):
        if self.path is not None:
            try:
                _atomic_write(self.path, self.s)
            except OSError:
                logger.exception("Could not save the safety lock")

    def lock(self, reason: str, source: str = "emergency_stop") -> dict:
        with self._lock:
            if not self.s["locked"]:
                self.s.update(locked=True, reason=reason, since=self._now())
                self.s["history"].append({"event": "locked", "at": self._now(), "reason": reason, "source": source})
                self.s["history"] = self.s["history"][-RECORDS_KEPT:]
                self._save()
            return self.status()

    def unlock(self, confirm: str) -> tuple[bool, str]:
        with self._lock:
            if confirm != self.UNLOCK_PHRASE:
                return False, f"Type {self.UNLOCK_PHRASE} to confirm lifting the emergency lock."
            if not self.s["locked"]:
                return True, "The emergency lock was not set."
            self.s["history"].append({"event": "unlocked", "at": self._now(), "reason": self.s["reason"]})
            self.s.update(locked=False, reason=None, since=None)
            self._save()
            return True, "Emergency lock lifted. Autopilot stays stopped until you start it again."

    @property
    def locked(self) -> bool:
        with self._lock:
            return bool(self.s["locked"])

    def status(self) -> dict:
        with self._lock:
            return {"locked": self.s["locked"], "reason": self.s["reason"], "since": self.s["since"], "records": len(self.s["history"])}


class ConsecutiveLossGuard:
    RESET_PHRASE = "RESET"

    def __init__(self, path, *, get_config, now=time.time):
        """get_config() -> {"consecutive_loss_enabled": bool, "consecutive_loss_threshold": int} (the user's live settings)."""
        self.path = Path(path) if path else None
        self._cfg = get_config
        self._now = now
        self._lock = threading.RLock()
        d = _load(self.path)
        self.s = {"initialised": bool(d.get("initialised", False)), "count": int(d.get("count", 0) or 0), "processed": list(d.get("processed", []))[-PROCESSED_KEPT:],
                  "records": list(d.get("records", []))[-RECORDS_KEPT:], "resets": list(d.get("resets", []))[-RECORDS_KEPT:]}

    def _save(self):
        if self.path is not None:
            try:
                _atomic_write(self.path, self.s)
            except OSError:
                logger.exception("Could not save the consecutive-loss record")

    def sync(self, closed_trades) -> dict:
        """Fold newly COMPLETED trades (objects with .ticket, .net, .time_close) into the counter, each ticket once, oldest first.
        The very first sync only records what already exists as history (it must not retroactively lock a new installation)."""
        with self._lock:
            trades = [t for t in (closed_trades or []) if getattr(t, "ticket", None) is not None]
            done = set(self.s["processed"])
            if not self.s["initialised"]:
                self.s["processed"] = [t.ticket for t in trades][-PROCESSED_KEPT:]
                self.s["initialised"] = True
                self._save()
                return self.status()
            fresh = sorted((t for t in trades if t.ticket not in done and getattr(t, "net", None) is not None), key=lambda t: (t.time_close or 0, t.ticket))
            changed = False
            for t in fresh:
                result = "loss" if t.net < 0 else "win" if t.net > 0 else "flat"
                if result == "loss":
                    self.s["count"] += 1
                elif result == "win":
                    self.s["count"] = 0
                self.s["processed"].append(t.ticket)
                self.s["records"].append({"ticket": t.ticket, "net": t.net, "result": result, "count_after": self.s["count"], "time_close": t.time_close, "counted_at": self._now()})
                changed = True
            if changed:
                self.s["processed"] = self.s["processed"][-PROCESSED_KEPT:]
                self.s["records"] = self.s["records"][-RECORDS_KEPT:]
                self._save()
            return self.status()

    def reset(self, confirm: str, by: str = "user") -> tuple[bool, str]:
        with self._lock:
            if confirm != self.RESET_PHRASE:
                return False, f"Type {self.RESET_PHRASE} to confirm resetting the consecutive-loss counter."
            self.s["resets"].append({"at": self._now(), "by": by, "count_before": self.s["count"]})
            self.s["resets"] = self.s["resets"][-RECORDS_KEPT:]
            self.s["count"] = 0
            self._save()
            return True, "The consecutive-loss counter was reset. Historical records are kept."

    def status(self) -> dict:
        cfg = self._cfg()
        enabled, threshold = bool(cfg["consecutive_loss_enabled"]), int(cfg["consecutive_loss_threshold"])
        with self._lock:
            count = self.s["count"]
        locked = enabled and count >= threshold
        return {"enabled": enabled, "threshold": threshold, "count": count, "locked": locked,
                "reason": (f"TRADING LOCKED: {count} consecutive losing trades reached the limit of {threshold}. Reset the counter to trade again." if locked else None),
                "records": len(self.s["records"]), "resets": len(self.s["resets"])}


def protection_checks(lock: SafetyLock | None, guard: ConsecutiveLossGuard | None) -> list:
    """The extra Risk Engine checks: [{name, passed, detail}]."""
    out = []
    if lock is not None:
        st = lock.status()
        out.append({"name": "Emergency lock", "passed": not st["locked"],
                    "detail": "No emergency lock is set." if not st["locked"] else f"EMERGENCY LOCKED: {st['reason'] or 'Emergency Stop pressed'}. No new orders are approved until the lock is lifted."})
    if guard is not None:
        g = guard.status()
        out.append({"name": "Consecutive losses", "passed": not g["locked"],
                    "detail": (f"{g['count']} of {g['threshold']} consecutive losses." if g["enabled"] else "Consecutive-loss protection is switched off.") if not g["locked"] else g["reason"]})
    return out
