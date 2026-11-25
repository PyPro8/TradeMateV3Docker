"""Historical daily risk records (data/risk_daily_log.json): one row per LOCAL calendar day, written whenever the risk state is read.

The log only grows: a new local day starts a new row; yesterday's row is never edited again or deleted, so the history survives the daily reset and restarts.
The daily-loss COUNTERS themselves are recomputed from the broker's history every time (no stored counter can go stale or carry yesterday's loss into today).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("trademate.risk")
KEEP_DAYS = 1500


class DailyRiskLog:
    def __init__(self, path, now=time.time):
        self.path = Path(path) if path else None
        self._now = now
        self._lock = threading.RLock()
        self.days: dict = {}
        if self.path is not None:
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict) and isinstance(d.get("days"), dict):
                    self.days = d["days"]
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                logger.warning("%s is unreadable; the daily risk history restarts from now (the file is left untouched until the next write).", self.path)

    def record(self, summary: dict, tz) -> None:
        """Upsert TODAY's row from a summarize_state() dict. Rows of earlier days are never modified."""
        if not isinstance(summary, dict) or summary.get("todays_loss") is None or not summary.get("next_reset_utc"):
            return
        day = datetime.fromtimestamp(summary["next_reset_utc"] - 1, tz).date().isoformat()
        with self._lock:
            first = self.days.get(day, {}).get("first_seen", self._now())
            self.days[day] = {"date": day, "currency": summary.get("currency"), "start_of_day_balance": summary.get("start_of_day_balance"),
                              "todays_closed_pl": summary.get("todays_closed_pl"), "todays_loss": summary.get("todays_loss"),
                              "todays_loss_percent": summary.get("todays_loss_percent"), "daily_limit_amount": summary.get("daily_limit_amount"),
                              "locked": bool(summary.get("daily_locked")), "first_seen": first, "updated_at": self._now()}
            for old in sorted(self.days)[:-KEEP_DAYS]:
                self.days.pop(old, None)
            self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"days": self.days}, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("Could not save the daily risk log")

    def history(self) -> list:
        with self._lock:
            return [self.days[k] for k in sorted(self.days)]
