"""The user's selected strategy: a small JSON file under data/ (same convention as risk_rules.json and chart settings).

Validated on every read and write. A missing, corrupt or unregistered value falls back to the configured default and says so; it never
selects something unregistered. `epoch` increases on every REAL change and is what invalidates proposals made under the previous strategy.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from trademate.strategies.registry import STRATEGY_IDS, get_strategy

logger = logging.getLogger("trademate.strategies")


class StrategySelectionStore:
    def __init__(self, path: Path, default_id: str):
        if get_strategy(default_id) is None:
            raise ValueError(f"The default strategy {default_id!r} is not registered.")
        self.path = Path(path)
        self.default_id = default_id
        self._lock = threading.RLock()
        self.epoch = 0
        self._listeners = []

    def on_change(self, callback) -> None:
        """callback(old_id, new_id) runs after a REAL change (stop the Watchdog, invalidate approvals, ...)."""
        self._listeners.append(callback)

    def load(self) -> dict:
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {"id": self.default_id, "stored": False, "problem": None}
            except (OSError, ValueError):
                logger.warning("Strategy selection file is unreadable; using the default.")
                return {"id": self.default_id, "stored": False, "problem": "The saved strategy selection could not be read; the default is in use."}
            sid = data.get("strategy") if isinstance(data, dict) else None
            if get_strategy(sid) is None:
                return {"id": self.default_id, "stored": False, "problem": "The saved strategy is not a registered strategy; the default is in use."}
            return {"id": sid, "stored": True, "problem": None}

    def selected_id(self) -> str:
        return self.load()["id"]

    def select(self, strategy_id) -> tuple[bool, str, str | None]:
        """-> (ok, message, previous_id). Rejects anything not in the catalogue. Atomic write."""
        if get_strategy(strategy_id) is None:
            return False, "Unknown strategy. Choose one of: " + ", ".join(STRATEGY_IDS) + ".", None
        with self._lock:
            old = self.selected_id()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"strategy": strategy_id}, fh)
                os.replace(tmp, self.path)
            except OSError:
                logger.exception("Could not save the strategy selection")
                return False, "The strategy selection could not be saved, so it was not changed.", old
            if strategy_id != old:
                self.epoch += 1
                for cb in list(self._listeners):
                    try:
                        cb(old, strategy_id)
                    except Exception:                                  # noqa: BLE001 - one failing listener must not undo a valid selection
                        logger.exception("Strategy-change listener failed")
            return True, "Strategy selected.", old
