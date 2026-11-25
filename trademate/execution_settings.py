"""Persistent execution / safety settings the user controls from the interface (data/execution_settings.json).

Scope: GLOBAL (they apply to every strategy and instrument). Per-strategy and per-instrument values live in trademate/trade_settings.py.
Every value is validated on read AND write; an unreadable or invalid file reads as the documented defaults and is never silently rewritten.

    allow_multiple_per_candle   OFF (default): at most ONE entry per applicable candle.  ON: at most TWO distinct entries.
    auto_lot_adjust             ON (default): after an "insufficient funds / margin" rejection, retry ONCE-OR-TWICE with a smaller, re-validated lot (see lot_adjust.py).
    lot_adjust_max_attempts     1..3 (default 2): hard cap on the number of reduced retries for one setup.
    emergency_close_positions   OFF (default): Emergency Stop blocks new orders ONLY. ON: it also closes open positions (documented in docs/AUTOPILOT.md).
    consecutive_loss_enabled / consecutive_loss_threshold   see risk/protections.py (kept here so one file holds the user's switches).
    sound_enabled               notification chime on/off (the browser also keeps its own per-device mute).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger("trademate.execution_settings")

DEFAULTS = {
    "allow_multiple_per_candle": False,
    "auto_lot_adjust": True,
    "lot_adjust_max_attempts": 2,
    "emergency_close_positions": False,
    "consecutive_loss_enabled": True,
    "consecutive_loss_threshold": 3,
    "sound_enabled": True,
}
BOOLS = ("allow_multiple_per_candle", "auto_lot_adjust", "emergency_close_positions", "consecutive_loss_enabled", "sound_enabled")
INTS = {"lot_adjust_max_attempts": (1, 3), "consecutive_loss_threshold": (1, 1000)}


def validate(patch: dict) -> dict:
    """-> the cleaned subset of `patch`. Raises ValueError with a user-facing message for anything invalid (nothing is coerced silently)."""
    out = {}
    for k, v in patch.items():
        if k in BOOLS:
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false.")
            out[k] = v
        elif k in INTS:
            lo, hi = INTS[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"{k} must be a whole number from {lo} to {hi}.")
            out[k] = v
        else:
            raise ValueError(f"Unknown setting {k!r}.")
    return out


class ExecutionSettings:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._mem: dict = {}

    def _read(self) -> dict:
        if self.path is None:
            return dict(self._mem)
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(d, dict):
            return {}
        try:
            return validate({k: v for k, v in d.items() if k in DEFAULTS})
        except ValueError:
            logger.warning("%s holds an invalid value; the documented defaults apply until it is saved again.", self.path)
            return {}

    def get_all(self) -> dict:
        with self._lock:
            return {**DEFAULTS, **self._read()}

    def get(self, key):
        return self.get_all()[key]

    def update(self, patch: dict) -> dict:
        clean = validate(patch)
        with self._lock:
            merged = {**self._read(), **clean}
            if self.path is None:
                self._mem = merged
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(merged, fh, indent=2)
                os.replace(tmp, self.path)
            return {**DEFAULTS, **merged}

    def max_entries_per_candle(self) -> int:
        return 2 if self.get("allow_multiple_per_candle") else 1
