"""The user's instrument and per-strategy execution timeframe (data/trade_selection.json). Validated on every read and write; a bad file reads as defaults."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from trademate.strategies.base import EXEC_TIMEFRAMES


class SelectionStore:
    def __init__(self, path: Path, default_tf: str = "M5"):
        self.path = Path(path)
        self.default_tf = default_tf if default_tf in EXEC_TIMEFRAMES else "M5"
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d = d if isinstance(d, dict) else {}
        tfs = d.get("timeframes") if isinstance(d.get("timeframes"), dict) else {}
        sym = d.get("symbol") if isinstance(d.get("symbol"), str) and 0 < len(d["symbol"]) <= 32 and d["symbol"].isprintable() else None
        return {"symbol": sym, "timeframes": {k: v for k, v in tfs.items() if isinstance(k, str) and v in EXEC_TIMEFRAMES}}

    def symbol(self) -> str | None:
        with self._lock:
            return self._read()["symbol"]

    def timeframe(self, strategy_id: str) -> str:
        with self._lock:
            if strategy_id == "trend":
                return "M5"                      # Trend Continuation keeps its established H1 + M5 definition
            return self._read()["timeframes"].get(strategy_id, self.default_tf)

    def save(self, symbol: str | None = None, strategy_id: str | None = None, timeframe: str | None = None) -> None:
        with self._lock:
            d = self._read()
            if symbol:
                d["symbol"] = symbol
            if strategy_id and timeframe:
                if timeframe not in EXEC_TIMEFRAMES:           # never store a value the reader would drop: that silently reverted the user's choice to the default (M5)
                    raise ValueError(f"Unknown timeframe {timeframe!r}. Choose one of: {', '.join(EXEC_TIMEFRAMES)}.")
                d["timeframes"][strategy_id] = timeframe
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(d, fh)
            os.replace(tmp, self.path)
