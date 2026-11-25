"""The ONE authoritative current strategy decision per (instrument, strategy, execution timeframe).

Why this exists (the stale-WAIT bug): the Watchdog used to keep only a decision STRING and a few alert events. The Command Centre's decision
panel was rendered only from the result of a manual ANALYSE click, so a Watchdog BUY/SELL produced a notification while the panel still showed the
old WAIT. Now every analysis, whether the Watchdog's or a manual one, is published here with its full result, and the panel, the notification
text, the pipeline's review and Autopilot all read this same record.

Guarantees
  * Keyed by (symbol, strategy, timeframe): a result for another instrument, strategy or timeframe can never be shown for the current selection.
  * Ordered: every evaluation takes a ticket (`begin()`) BEFORE it reads market data. A result is only accepted if its ticket is newer than
    the one already stored for that key, so a slow, older response can never restore an older decision over a newer one.
  * Freshness is computed on read: a record older than `stale_after` seconds is reported `stale`, never silently as current.
  * Nothing is cached on disk: after a restart there is no decision until a new analysis happens (fail closed).
"""

from __future__ import annotations

import itertools
import threading
import time

STATES = ("fresh", "stale", "error", "disconnected", "data_problem")


class DecisionHub:
    def __init__(self, stale_after: float = 180.0, clock=time.time):
        self._lock = threading.RLock()
        self._tickets = itertools.count(1)
        self._items: dict[tuple, dict] = {}
        self._clock = clock
        self.stale_after = float(stale_after)
        self._version = 0

    def begin(self) -> int:
        """Take an ordering ticket. Call BEFORE fetching market data."""
        return next(self._tickets)

    @staticmethod
    def key(symbol, strategy_id, exec_tf) -> tuple:
        return (symbol, strategy_id, exec_tf)

    def publish(self, ticket: int, symbol: str, strategy_id: str, exec_tf: str, *, state: str, message: str = "", result: dict | None = None,
                source: str = "watchdog") -> bool:
        """Store an outcome. Returns False (and stores nothing) when a newer ticket already holds this key (an out-of-order response)."""
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        k = self.key(symbol, strategy_id, exec_tf)
        with self._lock:
            cur = self._items.get(k)
            if cur is not None and cur["ticket"] >= ticket:
                return False
            self._version += 1
            prev = cur["result"] if cur is not None and result is None else None
            self._items[k] = {"ticket": ticket, "version": self._version, "symbol": symbol, "strategy_id": strategy_id, "exec_tf": exec_tf, "state": state,
                              "message": message, "source": source, "received_at": self._clock(),
                              # an error / disconnect keeps NO decision: the panel must not keep showing an old BUY as if it were current
                              "result": result, "last_good": prev if prev is not None else (cur or {}).get("last_good")}
            if result is not None:
                self._items[k]["last_good"] = None
            return True

    def get(self, symbol, strategy_id, exec_tf) -> dict | None:
        """The current record for this exact key with freshness applied, or None when nothing was ever published for it."""
        with self._lock:
            cur = self._items.get(self.key(symbol, strategy_id, exec_tf))
            if cur is None:
                return None
            rec = dict(cur)
        age = max(0.0, self._clock() - rec["received_at"])
        rec["age_seconds"] = age
        if rec["state"] == "fresh" and age > self.stale_after:
            rec["state"] = "stale"
            rec["message"] = f"The last analysis is {age:.0f}s old (limit {self.stale_after:.0f}s); it is not current."
        rec.pop("last_good", None)
        rec["decision"] = (rec["result"] or {}).get("decision") if rec["state"] == "fresh" else None
        return rec

    def version(self) -> int:
        with self._lock:
            return self._version

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._version += 1
