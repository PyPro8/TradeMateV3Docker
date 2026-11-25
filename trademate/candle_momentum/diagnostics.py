"""Structured diagnostics for the Candle Momentum engine.

One event = one JSON object with a fixed core (id, time, event, level, candle_id, symbol, timeframe) plus event-specific fields. Events go to
  * the standard logger "trademate.candle_momentum" (one JSON line each),
  * an in-memory ring buffer (what /api/candle-momentum/events and the Command Centre read),
  * data/candle_momentum_events.jsonl (size-bounded, rotated) so a late entry can be investigated after the fact.

EVERY event carries a candle identifier. Events that cannot belong to a candle (the feed went down before any candle was known) carry the literal
"unassigned". Secrets, passwords and account credentials are never logged: keys that look like one are redacted, and the account login is never an
event field.

Event names: new_candle_detected, direction_changed, trigger_calculated, trigger_crossed, tick_received, quote_gap, entry_evaluation_started,
entry_evaluation_completed, quote_freshness, execution_slippage, remaining_target_distance, risk_engine_decision, order_submitted, broker_response,
position_opened, target_reached, position_closed, entry_missed, entry_invalidated, transition, watchdog_failure, feed_down, feed_recovered, reconnected,
state_restored, retry_scheduled, config_changed, engine_started, engine_stopped, latency.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger("trademate.candle_momentum")
UNASSIGNED = "unassigned"
_SECRET = re.compile(r"pass(word|wd)?|secret|token|credential|api[_-]?key|private", re.IGNORECASE)
MAX_FILE_BYTES = 5 * 1024 * 1024


def _clean(value, depth=0):
    if depth > 4:
        return str(value)
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if _SECRET.search(str(k)) else _clean(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v, depth + 1) for v in list(value)[:50]]
    if isinstance(value, float) and value != value:           # NaN is not valid JSON
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class EventLog:
    def __init__(self, path: Path | None = None, capacity: int = 3000, clock=time.time):
        self.path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.Lock()
        self._ring: deque = deque(maxlen=capacity)
        self._next = 1

    def emit(self, event: str, candle_id: str | None, *, symbol: str | None = None, timeframe: str | None = None, level: str = "info", **fields) -> dict:
        with self._lock:
            rec = {"id": self._next, "t": self._clock(), "event": event, "level": level, "candle_id": candle_id or UNASSIGNED, "symbol": symbol, "timeframe": timeframe,
                   **_clean(fields)}
            self._next += 1
            self._ring.append(rec)
        line = json.dumps(rec, default=str, separators=(",", ":"))
        lvl = {"debug": logging.DEBUG, "warning": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO)
        logger.log(lvl, "%s", line)
        if self.path is not None and level != "debug":
            self._append(line)
        return rec

    def _append(self, line: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > MAX_FILE_BYTES:
                os.replace(self.path, self.path.with_suffix(".jsonl.1"))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:                                         # diagnostics must never break trading logic
            logger.warning("Could not write the Candle Momentum event file.")

    def tail(self, after_id: int = 0, candle_id: str | None = None, symbol: str | None = None, timeframe: str | None = None, limit: int = 200) -> list:
        with self._lock:
            items = [dict(r) for r in self._ring if r["id"] > after_id]
        if candle_id:
            items = [r for r in items if r["candle_id"] == candle_id]
        if symbol:
            items = [r for r in items if r.get("symbol") == symbol]
        if timeframe:
            items = [r for r in items if r.get("timeframe") == timeframe]
        return items[-max(1, min(int(limit), 1000)):]

    def latest_id(self) -> int:
        with self._lock:
            return self._next - 1
