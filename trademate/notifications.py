"""Server-side notification feed with persistent, unique ids (data/notifications.json).

Every notification has a unique `id` (a deterministic key such as "trade_closed:<ticket>") and an increasing `seq`. add() ignores an id that already exists, so a retry,
a polling cycle, a reconnect or a page refresh can never create the same notification twice. The browser asks for `after=<last seq it has seen>`; a first-time client asks only
for the latest seq (a baseline) and shows older items as HISTORY: no toast and no sound. History is preserved (capped at KEEP items).
Completed trades notify once per ticket. The first synchronisation records the trades that already exist as historical (no flood of old alerts after an update).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger("trademate.notifications")
KEEP = 2000


class NotificationLog:
    def __init__(self, path, now=time.time):
        self.path = Path(path) if path else None
        self._now = now
        self._lock = threading.RLock()
        self.s = {"seq": 0, "items": [], "ids": [], "tickets": [], "baseline_done": False}
        if self.path is not None:
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    self.s.update({k: d[k] for k in self.s if k in d})
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                logger.warning("%s is unreadable; notifications restart from now.", self.path)

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
            logger.exception("Could not save notifications")

    def add(self, nid: str, kind: str, title: str, body: str = "", level: str = "info", data: dict | None = None) -> dict | None:
        with self._lock:
            if nid in self.s["ids"]:
                return None
            self.s["seq"] += 1
            item = {"seq": self.s["seq"], "id": nid, "kind": kind, "level": level, "title": title, "body": body, "created_at": self._now(), "data": data or {}}
            self.s["items"].append(item)
            self.s["ids"].append(nid)
            self.s["items"] = self.s["items"][-KEEP:]
            self.s["ids"] = self.s["ids"][-KEEP * 2:]
            self._save()
            return item

    def since(self, after: int, limit: int = 100) -> list:
        with self._lock:
            return [i for i in self.s["items"] if i["seq"] > after][:limit]

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self.s["seq"]

    def recent(self, n: int = 50) -> list:
        with self._lock:
            return list(self.s["items"][-n:])

    def sync_closed_trades(self, trades) -> list:
        """Notify each newly COMPLETED trade once (win or loss). Returns the notifications created now."""
        created = []
        with self._lock:
            rows = [t for t in (trades or []) if getattr(t, "ticket", None) is not None and getattr(t, "net", None) is not None]
            known = set(self.s["tickets"])
            if not self.s["baseline_done"]:
                self.s["tickets"] = [t.ticket for t in rows][-KEEP * 2:]
                self.s["baseline_done"] = True
                self._save()
                return created
            for t in sorted((r for r in rows if r.ticket not in known), key=lambda r: (r.time_close or 0, r.ticket)):
                self.s["tickets"].append(t.ticket)
                win = t.net > 0
                word = "Winning" if win else "Losing" if t.net < 0 else "Break-even"
                item = self.add(f"trade_closed:{t.ticket}", "trade_win" if win else "trade_loss" if t.net < 0 else "trade_flat", f"{word} trade closed: {t.symbol} {t.net:+.2f}",
                                f"{str(t.direction).upper()} {t.volume:g} lots, {t.price_open:g} to {t.price_close:g}.", "win" if win else "loss" if t.net < 0 else "info",
                                {"ticket": t.ticket, "symbol": t.symbol, "net": t.net})
                if item:
                    created.append(item)
            self.s["tickets"] = self.s["tickets"][-KEEP * 2:]
            self._save()
        return created
