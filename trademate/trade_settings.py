"""Command Centre trade settings with clearly defined scopes (data/trade_settings.json). The SERVER is the only authority: the browser shows what this returns and never
keeps its own copy that could override it.

Settings: main_instrument, lot, target_distance, stop_distance, risk_reward (risk_reward is used by Market Structure only: when saved it replaces the pip target with N x the stop distance) (distances in the strategy's pip/point unit, converted by trademate.instruments, never a fixed forex pip).
Built-in defaults (used ONLY when nothing is saved for a value):  main_instrument Vol_20, lot 0.02, target_distance 25, stop_distance 100.

Scopes, most specific first; the first scope that has a SAVED value wins, otherwise the built-in default:
    strategy_instrument   one strategy on one instrument        "choch|EURUSD"
    instrument            every strategy on one instrument      "EURUSD"                 (lot is normally saved here: a lot belongs to an instrument)
    strategy              one strategy on every instrument      "choch"
    global                everything                            (main_instrument lives here)
`main_instrument` is global-only. Each value remembers which scope it came from (`sources`) so the UI can say so.
Candle Momentum keeps its own saved configuration (data/candle_momentum_config.json); only its lot and main instrument come from here.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger("trademate.trade_settings")

BUILTIN = {"main_instrument": "Vol_20", "lot": 0.02, "target_distance": 25.0, "stop_distance": 100.0, "risk_reward": None}   # risk_reward: None = not chosen (the strategy's own pip target applies)
RISK_REWARD_RANGE = (0.1, 100.0)
KEYS = tuple(BUILTIN)
SCOPES = ("strategy_instrument", "instrument", "strategy", "global")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate(values: dict, scope: str) -> dict:
    out = {}
    for k, v in values.items():
        if k not in KEYS:
            raise ValueError(f"Unknown setting {k!r}.")
        if k == "main_instrument":
            if scope != "global":
                raise ValueError("The main instrument is a global setting.")
            if not isinstance(v, str) or not v.strip() or len(v.strip()) > 40:
                raise ValueError("The main instrument must be a symbol name.")
            out[k] = v.strip()
        elif k == "risk_reward":
            try:
                n = float(v)
            except (TypeError, ValueError):
                raise ValueError("Risk-to-reward must be a number, for example 3 for 1:3.") from None
            if isinstance(v, bool) or not math.isfinite(n) or not RISK_REWARD_RANGE[0] <= n <= RISK_REWARD_RANGE[1]:
                raise ValueError(f"Risk-to-reward must be from {RISK_REWARD_RANGE[0]:g} to {RISK_REWARD_RANGE[1]:g} (1:{RISK_REWARD_RANGE[0]:g} to 1:{RISK_REWARD_RANGE[1]:g}).")
            out[k] = n
        else:
            try:
                n = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k.replace('_', ' ').capitalize()} must be a number.") from None
            if isinstance(v, bool) or not math.isfinite(n) or not 0 < n <= 1_000_000:
                raise ValueError(f"{k.replace('_', ' ').capitalize()} must be greater than 0.")
            out[k] = n
    return out


def scope_key(scope: str, symbol=None, strategy=None) -> str:
    if scope == "global":
        return ""
    if scope == "instrument" and symbol:
        return symbol
    if scope == "strategy" and strategy:
        return strategy
    if scope == "strategy_instrument" and symbol and strategy:
        return f"{strategy}|{symbol}"
    raise ValueError(f"Scope {scope!r} needs: " + {"instrument": "a symbol", "strategy": "a strategy", "strategy_instrument": "a strategy and a symbol"}.get(scope, "valid fields"))


class TradeSettings:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._mem = {}

    def _read(self) -> dict:
        d = self._mem if self.path is None else {}
        if self.path is not None:
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                d = {}
        out = {s: {} for s in SCOPES}
        if not isinstance(d, dict):
            return out
        for scope in SCOPES:
            block = d.get(scope)
            if scope == "global":
                block = {"": block} if isinstance(block, dict) else {}
            if not isinstance(block, dict):
                continue
            for key, vals in block.items():
                try:
                    out[scope][key] = validate(vals, scope) if isinstance(vals, dict) else {}
                except ValueError:
                    logger.warning("Ignoring an invalid saved value in %s scope %r.", scope, key)
        return out

    def _write(self, data: dict) -> None:
        flat = {s: (data[s].get("", {}) if s == "global" else data[s]) for s in SCOPES}
        if self.path is None:
            self._mem = flat
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(flat, fh, indent=2)
        os.replace(tmp, self.path)

    def resolve(self, symbol=None, strategy=None) -> dict:
        """-> {"values": {key: value}, "sources": {key: scope or "default"}}. Saved values are authoritative; built-in defaults fill only what is not saved."""
        data = self._read()
        chain = []
        if strategy and symbol:
            chain.append(("strategy_instrument", data["strategy_instrument"].get(f"{strategy}|{symbol}", {})))
        if symbol:
            chain.append(("instrument", data["instrument"].get(symbol, {})))
        if strategy:
            chain.append(("strategy", data["strategy"].get(strategy, {})))
        chain.append(("global", data["global"].get("", {})))
        values, sources = {}, {}
        for k in KEYS:
            for scope, vals in chain:
                if k in vals and (k != "main_instrument" or scope == "global"):
                    values[k], sources[k] = vals[k], scope
                    break
            else:
                values[k], sources[k] = BUILTIN[k], "default"
        return {"values": values, "sources": sources}

    def saved_only(self, symbol=None, strategy=None) -> dict:
        r = self.resolve(symbol, strategy)
        return {k: v for k, v in r["values"].items() if r["sources"][k] != "default"}

    def save(self, scope: str, values: dict, symbol=None, strategy=None) -> dict:
        key = scope_key(scope, symbol, strategy)
        clean = validate(values, scope)
        with self._lock:
            data = self._read()
            data[scope].setdefault(key, {}).update(clean)
            self._write(data)
        return self.resolve(symbol, strategy)

    def clear(self, scope: str, symbol=None, strategy=None, keys=None) -> dict:
        """Remove saved values of one scope (so the next scope / the default applies). Nothing else is touched."""
        key = scope_key(scope, symbol, strategy)
        with self._lock:
            data = self._read()
            block = data[scope].get(key, {})
            for k in (keys or list(block)):
                block.pop(k, None)
            if not block:
                data[scope].pop(key, None)
            self._write(data)
        return self.resolve(symbol, strategy)

    def main_instrument(self) -> str:
        return self.resolve()["values"]["main_instrument"]
