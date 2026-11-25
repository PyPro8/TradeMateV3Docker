"""Persistence of the Candle Momentum engine: the per-candle lifecycle records (what makes one-entry-per-candle survive a browser refresh, a Watchdog
restart, a reconnect and a backend restart) and the per-(symbol, timeframe) configuration.

Both are small JSON files under data/, written atomically (temp file + replace) like every other TradeMate store; a corrupt file reads as empty and is
reported, never guessed. Transitions that matter (a trigger, a validation, an order, a position) are written immediately; the high-frequency direction
flicker (BULLISH <-> BEARISH <-> MONITORING) is debounced so the disk is not written on every tick.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from trademate.candle_momentum.models import CMConfig, validate_config

logger = logging.getLogger("trademate.candle_momentum")
MAX_LIFECYCLES = 600
DEBOUNCE_SECONDS = 1.0


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, default=str)
    os.replace(tmp, path)


class StateStore:
    """candle_id -> lifecycle record (a plain dict). Thread-safe."""

    def __init__(self, path: Path | None, clock=time.time):
        self.path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.RLock()
        self._items: dict = {}
        self._dirty = False
        self._last_write = 0.0
        self.problem: str | None = None
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self.problem = "The saved Candle Momentum state could not be read; duplicate protection restarts from now."
            logger.warning(self.problem)
            return
        items = data.get("lifecycles") if isinstance(data, dict) else None
        if isinstance(items, dict):
            self._items = {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("state"), str)}

    def get(self, candle_id: str) -> dict | None:
        with self._lock:
            v = self._items.get(candle_id)
            return json.loads(json.dumps(v, default=str)) if v is not None else None

    def put(self, lifecycle: dict, immediate: bool) -> None:
        cid = lifecycle.get("candle_id")
        if not isinstance(cid, str):
            return
        with self._lock:
            self._items.pop(cid, None)                           # re-insert = newest last
            self._items[cid] = json.loads(json.dumps(lifecycle, default=str))
            self._prune()
            self._dirty = True
            if immediate or self._clock() - self._last_write >= DEBOUNCE_SECONDS:
                self._write()

    def _prune(self) -> None:
        if len(self._items) <= MAX_LIFECYCLES:
            return
        for cid in [k for k, v in self._items.items() if v.get("state") not in ("POSITION_OPEN", "TARGET_REACHED", "EXECUTING")]:
            if len(self._items) <= MAX_LIFECYCLES:
                break
            self._items.pop(cid, None)

    def flush_if_due(self) -> None:
        with self._lock:
            if self._dirty and self._clock() - self._last_write >= DEBOUNCE_SECONDS:
                self._write()

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._write()

    def _write(self) -> None:
        self._dirty = False
        self._last_write = self._clock()
        if self.path is None:
            return
        try:
            _atomic_write(self.path, {"version": 1, "lifecycles": self._items})
        except OSError:
            logger.exception("Could not save the Candle Momentum state")

    def for_key(self, key_prefix: str) -> list:
        with self._lock:
            return [json.loads(json.dumps(v, default=str)) for v in self._items.values() if v.get("config_key") == key_prefix]

    def open_positions(self) -> list:
        with self._lock:
            return [json.loads(json.dumps(v, default=str)) for v in self._items.values() if v.get("state") in ("POSITION_OPEN", "TARGET_REACHED") and v.get("ticket")]


def defaults_from_settings(settings, symbol: str, timeframe: str) -> CMConfig:
    """The configuration of an instrument the user has not customised: [strategy] candle_* (existing) and [candle_momentum] (new) in settings.toml."""
    st, cm = settings.strategy, settings.candle_momentum
    return CMConfig(symbol=symbol, timeframe=timeframe, enabled=True, trigger_units=st.candle_trigger_pips, target_units=st.candle_target_pips,
                    sl_buffer_units=st.candle_sl_buffer_pips,
                    max_slippage_units=cm.max_slippage_units or None, signal_expiry_seconds=cm.signal_expiry_seconds,
                    quote_max_age_seconds=cm.quote_max_age_seconds, max_entry_attempts=cm.max_entry_attempts, retry_delay_seconds=cm.retry_delay_seconds)


class ConfigStore:
    """The user's per-(symbol, timeframe) settings (data/candle_momentum_config.json). Validated on read AND write."""

    def __init__(self, path: Path | None, timeframes: tuple):
        self.path = Path(path) if path else None
        self.timeframes = timeframes
        self._lock = threading.RLock()
        self._mem: dict = {}

    def _read(self) -> dict:
        if self.path is None:
            return dict(self._mem)
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        cfgs = d.get("configs") if isinstance(d, dict) else None
        return cfgs if isinstance(cfgs, dict) else {}

    def get(self, base: CMConfig) -> tuple[CMConfig, bool]:
        """-> (effective config, customised). A saved record that no longer validates is ignored (the defaults apply)."""
        with self._lock:
            configs = self._read()
            saved = configs.get(base.key)
            if isinstance(saved, dict):
                # Migrate only the former built-in defaults. Preserve user-customised distances and trigger settings.
                migrated = dict(saved)
                changed = False
                if migrated.get("sl_buffer_units") == 1.0:
                    migrated["sl_buffer_units"] = 50.0
                    changed = True
                if migrated.get("target_units") == 10.0:
                    migrated["target_units"] = 20.0
                    changed = True
                if changed:
                    configs[base.key] = migrated
                    if self.path is None:
                        self._mem = configs
                    else:
                        try:
                            _atomic_write(self.path, {"version": 1, "configs": configs})
                        except OSError:
                            logger.exception("Could not migrate the saved Candle Momentum defaults")
                    saved = migrated
        if not isinstance(saved, dict):
            return base, False
        cfg = CMConfig.from_dict(saved, base)
        if validate_config(cfg, self.timeframes):
            logger.warning("Saved Candle Momentum settings for %s are invalid; the defaults are used.", base.key)
            return base, False
        return cfg, True

    def save(self, cfg: CMConfig) -> list:
        problems = validate_config(cfg, self.timeframes)
        if problems:
            return problems
        with self._lock:
            cfgs = self._read()
            cfgs[cfg.key] = {k: v for k, v in cfg.to_dict().items() if k not in ("symbol", "timeframe")}
            if self.path is None:
                self._mem = cfgs
                return []
            try:
                _atomic_write(self.path, {"version": 1, "configs": cfgs})
            except OSError:
                logger.exception("Could not save the Candle Momentum settings")
                return ["The settings could not be saved."]
        return []

    def reset(self, symbol: str, timeframe: str) -> None:
        with self._lock:
            cfgs = self._read()
            if cfgs.pop(f"{symbol}|{timeframe}", None) is not None:
                if self.path is None:
                    self._mem = cfgs
                else:
                    try:
                        _atomic_write(self.path, {"version": 1, "configs": cfgs})
                    except OSError:
                        logger.exception("Could not reset the Candle Momentum settings")
