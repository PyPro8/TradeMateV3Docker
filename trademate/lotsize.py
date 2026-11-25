"""Lot size: exact validation against the instrument's REAL volume rules, and the user's saved choice per instrument.

Rules (documented in docs/AUTOPILOT_AND_STRATEGIES.md):
  * Nothing is ever rounded. A value that is not exactly on the broker's grid (volume_min + n * volume_step) is REJECTED with the
    nearest valid neighbours named, so the user decides. A different lot size is never substituted silently.
  * Arithmetic uses Decimal, so 0.07 against a 0.01 step is judged exactly, not with float error.
  * The limits are the instrument's own volume_min / volume_max / volume_step from MT5, plus TradeMate's own per-order cap
    (`[trading] max_volume`). If the instrument's rules are unknown the volume is refused (fail closed).
  * The saved value is only a PREFERENCE for FUTURE entries. It is re-validated by the backend against the live specification at the
    moment of use; the browser's copy is never trusted. It never touches a position that is already open.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger("trademate.lotsize")


@dataclass(frozen=True)
class VolumeCheck:
    ok: bool
    volume: float | None
    message: str
    code: str = ""           # invalid_input | unknown_rules | below_min | above_max | above_cap | off_step | ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "volume": self.volume, "message": self.message, "code": self.code,
                "volume_min": self.minimum, "volume_max": self.maximum, "volume_step": self.step}


def _dec(x) -> Decimal | None:
    if isinstance(x, bool):
        return None
    try:
        if isinstance(x, str):
            x = x.strip()
            if not x:
                return None
        d = Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")


def validate_volume(value, vmin, vmax, vstep, cap=None) -> VolumeCheck:
    """Exact validation. Never rounds."""
    v = _dec(value)
    if v is None or v <= 0:
        return VolumeCheck(False, None, "Enter the lot size as a number greater than zero (for example 0.10).", "invalid_input")
    lo, hi, st = _dec(vmin), _dec(vmax), _dec(vstep)
    if lo is None or hi is None or st is None or lo <= 0 or hi < lo or st <= 0:
        return VolumeCheck(False, None, "The broker's volume rules (minimum, maximum, step) for this instrument are not available, so no lot size can be accepted.", "unknown_rules")
    f = lambda d: float(d)  # noqa: E731
    base = dict(minimum=f(lo), maximum=f(hi), step=f(st))
    if v < lo:
        return VolumeCheck(False, None, f"{_fmt(v)} lots is below this instrument's minimum of {_fmt(lo)}.", "below_min", **base)
    if v > hi:
        return VolumeCheck(False, None, f"{_fmt(v)} lots is above this instrument's maximum of {_fmt(hi)}.", "above_max", **base)
    cap_d = _dec(cap) if cap is not None else None
    if cap_d is not None and v > cap_d:
        return VolumeCheck(False, None, f"{_fmt(v)} lots is above TradeMate's per-order limit of {_fmt(cap_d)} ([trading] max_volume).", "above_cap", **base)
    n = (v - lo) / st
    if n != n.to_integral_value():
        below = lo + (n.to_integral_value(rounding="ROUND_FLOOR")) * st
        above = below + st
        near = f"The nearest valid sizes are {_fmt(below)} and {_fmt(above)}." if above <= hi else f"The nearest valid size is {_fmt(below)}."
        return VolumeCheck(False, None, f"{_fmt(v)} lots does not fit this instrument's volume step of {_fmt(st)} (steps start at {_fmt(lo)}). {near}", "off_step", **base)
    return VolumeCheck(True, f(v), f"{_fmt(v)} lots is valid (minimum {_fmt(lo)}, maximum {_fmt(hi)}, step {_fmt(st)}).", "", **base)


def validate_for_info(value, info, cap=None) -> VolumeCheck:
    """`info` is a broker SymbolInfo (or anything with volume_min / volume_max / volume_step)."""
    return validate_volume(value, getattr(info, "volume_min", None), getattr(info, "volume_max", None), getattr(info, "volume_step", None), cap)


class LotStore:
    """Per-instrument saved lot size (data/lot_sizes.json). Atomic writes; a corrupt file reads as 'nothing saved'."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("Lot size file unreadable; treating it as empty.")
            return {}
        raw = data.get("lots") if isinstance(data, dict) else None
        return {k: float(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0} if isinstance(raw, dict) else {}

    def get(self, symbol: str) -> float | None:
        with self._lock:
            return self._read().get(symbol)

    def set(self, symbol: str, volume: float) -> None:
        """Caller must have validated `volume` against the instrument first."""
        with self._lock:
            lots = self._read()
            lots[symbol] = float(volume)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"lots": lots}, fh)
            os.replace(tmp, self.path)
