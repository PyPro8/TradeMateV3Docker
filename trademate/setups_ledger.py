"""One order per setup, ever (even across restarts).

A setup's identity is its strategy candidate id: strategy + direction + timeframe + the candle's open time (Candle Momentum) or the CHoCH's
candle and level (CHoCH). Once an approval for that id is consumed, no further order may be built from it.

`claim()` is atomic and is called at the moment an approval is consumed. If the broker then proves the order did not happen (rejected /
refused before sending), `release()` frees the id so the user may try again; an UNKNOWN or accepted outcome is never released.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger("trademate.ledger")


class SetupLedger:
    MAX = 5000

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._items: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("setups"), dict):
                self._items = {k: v for k, v in data["setups"].items() if isinstance(k, str) and isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("Setup ledger unreadable; starting empty (duplicate protection restarts from now).")

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"setups": self._items}, fh)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("Could not save the setup ledger")

    def is_taken(self, setup_id) -> bool:
        with self._lock:
            return isinstance(setup_id, str) and setup_id in self._items

    def get(self, setup_id) -> dict | None:
        with self._lock:
            return dict(self._items[setup_id]) if self.is_taken(setup_id) else None

    def claim(self, setup_id, **meta) -> bool:
        """True when this call is the one that took the id; False when it was already taken (a duplicate). A missing id cannot be claimed."""
        if not isinstance(setup_id, str) or not setup_id:
            return False
        with self._lock:
            if setup_id in self._items:
                return False
            self._items[setup_id] = {"at": time.time(), **meta}
            while len(self._items) > self.MAX:
                self._items.pop(next(iter(self._items)))
            self._save()
            return True

    def release(self, setup_id) -> None:
        with self._lock:
            if self._items.pop(setup_id, None) is not None:
                self._save()
