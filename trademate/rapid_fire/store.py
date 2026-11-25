"""Persistence for Rapid Fire: the configuration, and the LEDGER (per-candle shot counters + every Rapid Fire trade).

The ledger is what makes the entry limit survive polling, browser refresh, reconnects and restarts: a shot is reserved (and written to disk) BEFORE
the order is sent, keyed by instrument + strategy + M1 candle timestamp. Writes are atomic (temp file + replace); a corrupt file is moved aside, never
silently treated as 'no trades'.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from trademate.rapid_fire.models import RFConfig, STRATEGY_ID, validate

logger = logging.getLogger("trademate.rapid_fire")

# trade statuses
SUBMITTING = "SUBMITTING"                  # reserved + about to be sent. After a restart this means the outcome is unknown.
ENTRY_UNCERTAIN = "ENTRY_UNCERTAIN"        # sent, outcome not provable. Blocks new entries until settled.
ENTRY_FAILED = "ENTRY_FAILED"              # provably not executed
OPEN = "OPEN"
CLOSE_REQUESTED = "CLOSE_REQUESTED"        # transient, inside one step
CLOSE_UNCERTAIN = "CLOSE_UNCERTAIN"        # close sent, closure not confirmed. NEVER re-sent blindly.
CLOSE_FAILED = "CLOSE_FAILED"              # automatic attempts exhausted; the user must act in MT5
CLOSED_AWAITING_RESULT = "CLOSED_AWAITING_RESULT"   # broker-confirmed closed; realised numbers not yet in MT5 history
CLOSED = "CLOSED"                          # closed AND realised result recorded from MT5 history
LIVE = (SUBMITTING, ENTRY_UNCERTAIN, OPEN, CLOSE_REQUESTED, CLOSE_UNCERTAIN, CLOSE_FAILED)
HOLDING = (OPEN, CLOSE_REQUESTED, CLOSE_UNCERTAIN, CLOSE_FAILED)     # a position exists (or may exist) at the broker


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, what: str) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        aside = path.with_suffix(path.suffix + f".corrupt-{int(time.time())}")
        try:
            os.replace(path, aside)
        except OSError:
            pass
        logger.error("Rapid Fire %s file was unreadable; it was moved aside to %s and a fresh one is used.", what, aside.name)
        return None
    return raw if isinstance(raw, dict) else None


class ConfigStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> RFConfig:
        with self._lock:
            raw = _read(self.path, "configuration")
            if not raw:
                return RFConfig()
            known = {k: raw[k] for k in RFConfig.__dataclass_fields__ if k in raw}
            try:
                cfg = RFConfig(**known)
            except TypeError:
                return RFConfig()
            return cfg if not validate(cfg) else RFConfig()

    def save(self, cfg: RFConfig) -> list[str]:
        problems = validate(cfg)
        if problems:
            return problems
        with self._lock:
            _atomic_write(self.path, asdict(cfg))
        return []


class Ledger:
    KEEP_CANDLES = 200
    KEEP_CLOSED = 2000

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        raw = _read(self.path, "ledger") or {}
        self._candles: dict = raw.get("candles") if isinstance(raw.get("candles"), dict) else {}
        self._trades: dict = raw.get("trades") if isinstance(raw.get("trades"), dict) else {}

    # ---- persistence ----
    def _save(self) -> None:
        if len(self._candles) > self.KEEP_CANDLES:
            for k in sorted(self._candles, key=lambda k: self._candles[k].get("candle_time", 0))[:len(self._candles) - self.KEEP_CANDLES]:
                del self._candles[k]
        closed = [t for t in self._trades.values() if t["status"] in (CLOSED, ENTRY_FAILED)]
        if len(closed) > self.KEEP_CLOSED:
            for t in sorted(closed, key=lambda t: t.get("created_at", 0))[:len(closed) - self.KEEP_CLOSED]:
                del self._trades[t["id"]]
        _atomic_write(self.path, {"version": 1, "candles": self._candles, "trades": self._trades})

    # ---- per-candle shot counter ----
    @staticmethod
    def candle_key(symbol: str, candle_time: int) -> str:
        return f"{symbol.upper()}|{STRATEGY_ID}|{int(candle_time)}"

    def shots(self, key: str) -> int:
        with self._lock:
            return int(self._candles.get(key, {}).get("shots", 0))

    def failures(self, key: str) -> int:
        with self._lock:
            return int(self._candles.get(key, {}).get("failures", 0))

    def anchor(self, key: str) -> float | None:
        """The price the NEXT shot in this candle is measured from: the exit price of the previous shot (None = the candle's open)."""
        with self._lock:
            return self._candles.get(key, {}).get("anchor")

    def set_anchor(self, key: str, candle_time: int, price: float) -> None:
        with self._lock:
            rec = self._candles.setdefault(key, {"candle_time": int(candle_time), "shots": 0, "failures": 0})
            rec["anchor"] = float(price)
            self._save()

    def reserve_shot(self, key: str, candle_time: int, maximum: int) -> bool:
        """Atomically take one shot for this candle. False when none is left. Written to disk before returning."""
        with self._lock:
            rec = self._candles.setdefault(key, {"candle_time": int(candle_time), "shots": 0, "failures": 0})
            if rec["shots"] >= maximum:
                return False
            rec["shots"] += 1
            self._save()
            return True

    def release_shot(self, key: str, failed: bool = True) -> None:
        """Give a shot back: ONLY for an order that provably never executed."""
        with self._lock:
            rec = self._candles.get(key)
            if rec and rec["shots"] > 0:
                rec["shots"] -= 1
                if failed:
                    rec["failures"] = rec.get("failures", 0) + 1
                self._save()

    # ---- trades ----
    def new_trade(self, **fields) -> dict:
        with self._lock:
            tid = "rf-" + uuid.uuid4().hex[:10]
            rec = {"id": tid, "created_at": time.time(), "status": SUBMITTING, "ticket": None, "role": "original", "twin_of": None, "twin_id": None,
                   "twin_state": None, "close_reason": None, "close_attempts": 0, "message": "", **fields}
            self._trades[tid] = rec
            self._save()
            return dict(rec)

    def update(self, tid: str, **fields) -> dict:
        with self._lock:
            rec = self._trades[tid]
            rec.update(fields)
            self._save()
            return dict(rec)

    def get(self, tid: str) -> dict | None:
        with self._lock:
            t = self._trades.get(tid)
            return dict(t) if t else None

    def trades(self, status=None) -> list[dict]:
        with self._lock:
            out = [dict(t) for t in self._trades.values() if status is None or t["status"] in status]
        return sorted(out, key=lambda t: t["created_at"])

    def live(self) -> list[dict]:
        return self.trades(LIVE)

    def by_ticket(self) -> dict:
        with self._lock:
            return {t["ticket"]: dict(t) for t in self._trades.values() if t.get("ticket")}

    def has_live(self) -> bool:
        with self._lock:
            return any(t["status"] in LIVE for t in self._trades.values())
