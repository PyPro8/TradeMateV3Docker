"""Server-side stores for the chart's moving-average indicator and the user's MANUAL line drawings (the server is the authority; the browser only displays them).

Moving average   data/chart_indicators.json   {"ma": {"enabled", "type" SMA|EMA|WMA, "period" 2..500, "timeframe" M1..D1 or "chart"}}. Display only: it is NEVER an input of any strategy.
Manual drawings  data/manual_drawings.json    {"<symbol>": [{"id", "t1", "p1", "t2", "p2"}]}  anchored to time + price, so a line stays where it was drawn when the timeframe changes.
                 Kept completely apart from the automatic strategy trendlines (those are computed by the Trendline strategy and returned in its result, never stored here).
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from pathlib import Path

MA_DEFAULT = {"enabled": False, "type": "EMA", "period": 20, "timeframe": "chart"}
MA_TYPES = ("SMA", "EMA", "WMA")
MA_TIMEFRAMES = ("chart", "M1", "M2", "M5", "M15", "M30", "H1", "H4", "D1")
MAX_LINES = 50


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_ma(d: dict) -> dict:
    if not isinstance(d, dict):
        raise ValueError("Moving average settings must be an object.")
    out = dict(MA_DEFAULT)
    for k, v in d.items():
        if k == "enabled":
            if not isinstance(v, bool):
                raise ValueError("enabled must be true or false.")
        elif k == "type":
            if v not in MA_TYPES:
                raise ValueError(f"type must be one of {', '.join(MA_TYPES)}.")
        elif k == "period":
            if isinstance(v, bool) or not isinstance(v, int) or not 2 <= v <= 500:
                raise ValueError("period must be a whole number from 2 to 500.")
        elif k == "timeframe":
            if v not in MA_TIMEFRAMES:
                raise ValueError(f"timeframe must be one of {', '.join(MA_TIMEFRAMES)}.")
        else:
            raise ValueError(f"Unknown moving-average setting {k!r}.")
        out[k] = v
    return out


def validate_lines(lines) -> list:
    if not isinstance(lines, list) or len(lines) > MAX_LINES:
        raise ValueError(f"Send a list of at most {MAX_LINES} lines.")
    out, seen = [], set()
    for ln in lines:
        if not isinstance(ln, dict) or not all(_num(ln.get(k)) for k in ("t1", "p1", "t2", "p2")):
            raise ValueError("Every line needs numeric t1, p1, t2 and p2.")
        lid = str(ln.get("id") or "")[:40]
        if not lid or lid in seen:
            raise ValueError("Every line needs a unique id.")
        seen.add(lid)
        out.append({"id": lid, "t1": float(ln["t1"]), "p1": float(ln["p1"]), "t2": float(ln["t2"]), "p2": float(ln["p2"])})
    return out


class _JsonFile:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._mem: dict = {}

    def read(self) -> dict:
        if self.path is None:
            return json.loads(json.dumps(self._mem))
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def write(self, d: dict) -> None:
        if self.path is None:
            self._mem = d
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
        os.replace(tmp, self.path)


class IndicatorStore(_JsonFile):
    def get_ma(self) -> dict:
        try:
            return validate_ma(self.read().get("ma") or {})
        except ValueError:
            return dict(MA_DEFAULT)

    def set_ma(self, patch: dict) -> dict:
        with self._lock:
            merged = validate_ma({**self.get_ma(), **patch})
            self.write({**self.read(), "ma": merged})
            return merged


class DrawingStore(_JsonFile):
    def get(self, symbol: str) -> list:
        try:
            return validate_lines(self.read().get(symbol) or [])
        except ValueError:
            return []

    def set(self, symbol: str, lines) -> list:
        if not isinstance(symbol, str) or not symbol or len(symbol) > 40:
            raise ValueError("A symbol is required.")
        clean = validate_lines(lines)
        with self._lock:
            d = self.read()
            d[symbol] = clean
            self.write(d)
        return clean
