"""The ONE place where "pips" become price distances (and back).  Pure functions: no broker calls, no Flask.

A pip is not a universal unit. Its meaning depends on the instrument, so it is resolved from the instrument's verified specification
(digits, point, broker category path, name) in this order, and the answer always says WHICH rule produced it:

  1. override      an explicit size in `[pips.overrides]` (settings.toml): symbol name -> price units per pip. Always wins.
  2. forex         5- or 3-digit quotes: 1 pip = 10 points (0.0001 / 0.01).  4- or 2-digit quotes: 1 pip = 1 point.
  3. metal         XAU / XAG symbols: 1 pip = 10 points (XAUUSD at 2 digits = 0.10).  A convention, overridable.
  4. unknown       anything else (indices, synthetic indices such as Boom/Crash/Volatility, crypto, shares, ...) has NO universal pip.
                   It is REFUSED (ok = False) with an instruction to define it in `[pips.overrides]`; it is never guessed and never
                   treated as "1 point", because 1 point on a 3-digit index is not what a forex trader means by a pip.

Every price produced here is snapped to the instrument's tick size, in the direction that never makes a stop less protective or a
target easier than asked for.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

_FX_NAME = re.compile(r"^[A-Za-z]{6}([._#-].*)?$")
_METAL = re.compile(r"^(XAU|XAG|XPT|XPD)", re.IGNORECASE)
_FX_PATH = re.compile(r"forex|fx|currenc", re.IGNORECASE)
_NOT_FX_PATH = re.compile(r"synthetic|derived|index|indices|crypto|stock|share|energ|commod|cfd", re.IGNORECASE)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True)
class PipDefinition:
    ok: bool
    size: float | None          # price units in one pip
    rule: str                   # override | forex | metal | unknown
    reason: str                 # human explanation (always set)
    symbol: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "size": self.size, "rule": self.rule, "reason": self.reason, "symbol": self.symbol}


def resolve_pip(symbol: str, digits, point, path: str = "", overrides: dict | None = None) -> PipDefinition:
    """The pip size of one instrument, or a refusal that says why. `overrides` maps symbol name -> price units per pip."""
    name = symbol or ""
    ov = (overrides or {}).get(name)
    if ov is not None:
        if not _num(ov) or ov <= 0:
            return PipDefinition(False, None, "override", f"The configured pip size for {name} ({ov!r}) is not a positive number.", name)
        return PipDefinition(True, float(ov), "override", f"Pip size {ov:g} for {name} comes from [pips.overrides] in settings.toml.", name)
    if not (_num(digits) and _num(point) and point > 0 and int(digits) == digits and 0 <= digits <= 10):
        return PipDefinition(False, None, "unknown", f"The broker did not report valid digits/point for {name or 'this instrument'}, so its pip cannot be determined.", name)
    digits = int(digits)
    if _METAL.match(name):
        return PipDefinition(True, point * 10, "metal", f"{name}: metal convention, 1 pip = 10 points = {point * 10:g}.", name)
    looks_fx = bool(_FX_PATH.search(path or "")) or (bool(_FX_NAME.match(name)) and not _NOT_FX_PATH.search(path or ""))
    if looks_fx and digits in (5, 3):
        return PipDefinition(True, point * 10, "forex", f"{name}: {digits}-digit forex quote, 1 pip = 10 points = {point * 10:g}.", name)
    if looks_fx and digits in (4, 2):
        return PipDefinition(True, point, "forex", f"{name}: {digits}-digit forex quote, 1 pip = 1 point = {point:g}.", name)
    return PipDefinition(False, None, "unknown",
                         f"No pip convention is defined for {name or 'this instrument'} (digits {digits}, point {point:g}"
                         + (f", category '{path}'" if path else "") + "). Define its pip size in [pips.overrides] in config/settings.toml, "
                         "for example: \"" + (name or "SYMBOL") + "\" = 1.0. TradeMate will not guess.", name)


def pip_for_info(info, overrides: dict | None = None) -> PipDefinition:
    """From a broker SymbolInfo (or anything with name/digits/point/path)."""
    return resolve_pip(getattr(info, "name", ""), getattr(info, "digits", None), getattr(info, "point", None), getattr(info, "path", "") or "", overrides)


def pips_to_price(pips: float, pip: PipDefinition) -> float:
    """Price distance of `pips` pips. Raises ValueError when the pip is undefined or the count is not a positive number."""
    if not pip.ok or not _num(pip.size):
        raise ValueError(pip.reason)
    if not _num(pips) or pips <= 0:
        raise ValueError("The number of pips must be a positive number.")
    return float(Decimal(str(pips)) * Decimal(str(pip.size)))


def price_to_pips(distance: float, pip: PipDefinition) -> float:
    if not pip.ok or not _num(pip.size):
        raise ValueError(pip.reason)
    if not _num(distance):
        raise ValueError("The distance must be a finite number.")
    return float(Decimal(str(distance)) / Decimal(str(pip.size)))


def snap_to_tick(price: float, tick_size, *, direction: str = "nearest") -> float:
    """Snap `price` to the tick grid. direction: 'down' | 'up' | 'nearest'. Without a usable tick size the price is returned unchanged."""
    if not _num(price) or not _num(tick_size) or tick_size <= 0:
        return price
    p, t = Decimal(str(price)), Decimal(str(tick_size))
    steps = p / t
    if direction == "down":
        n = steps.to_integral_value(rounding=ROUND_FLOOR)
    elif direction == "up":
        n = steps.to_integral_value(rounding=ROUND_CEILING)
    else:
        n = steps.quantize(Decimal(1))
    return float(n * t)


def target_price(side: str, entry: float, pips: float, pip: PipDefinition, tick_size=None) -> float:
    """Take-profit `pips` pips beyond `entry`. Snapped so the target is never CLOSER than asked (BUY rounds up, SELL rounds down)."""
    d = pips_to_price(pips, pip)
    if side == "buy":
        return snap_to_tick(entry + d, tick_size, direction="up")
    if side == "sell":
        return snap_to_tick(entry - d, tick_size, direction="down")
    raise ValueError("side must be 'buy' or 'sell'.")


def protective_stop(side: str, price: float, tick_size=None) -> float:
    """Snap a stop so it is never closer to entry than computed (BUY rounds down, SELL rounds up)."""
    return snap_to_tick(price, tick_size, direction="down" if side == "buy" else "up")


def min_stop_problem(side: str, entry: float, sl: float, tp: float, point, stops_level) -> str | None:
    """The broker's minimum stop distance (stops_level, in points) applies to BOTH stop-loss and take-profit."""
    if not (_num(point) and point > 0) or not _num(stops_level) or stops_level <= 0:
        return None
    need = stops_level * point
    eps = point * 1e-6
    if abs(entry - sl) + eps < need:
        return f"The stop-loss is {abs(entry - sl):g} from the price; the broker requires at least {need:g} ({int(stops_level)} points)."
    if abs(tp - entry) + eps < need:
        return f"The take-profit is {abs(tp - entry):g} from the price; the broker requires at least {need:g} ({int(stops_level)} points)."
    return None


class PipStore:
    """Pip sizes the user entered in the browser (data/pip_sizes.json): symbol -> price units per pip. They win over [pips.overrides] in settings.toml.
    Atomic writes; a corrupt file reads as empty. Validation of the value (against the instrument's tick) happens in the route, not here."""

    def __init__(self, path):
        import threading
        from pathlib import Path
        self.path = Path(path)
        self._lock = threading.RLock()

    def all(self) -> dict:
        import json
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8")).get("pips", {})
            except (OSError, ValueError, AttributeError):
                return {}
            return {k: float(v) for k, v in raw.items() if isinstance(k, str) and _num(v) and v > 0} if isinstance(raw, dict) else {}

    def get(self, symbol):
        return self.all().get(symbol)

    def _write(self, pips: dict) -> None:
        import json
        import os
        import tempfile
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pips": pips}, fh)
        os.replace(tmp, self.path)

    def set(self, symbol: str, size: float) -> None:
        with self._lock:
            d = self.all()
            d[symbol] = float(size)
            self._write(d)

    def clear(self, symbol: str) -> None:
        with self._lock:
            d = self.all()
            if d.pop(symbol, None) is not None:
                self._write(d)
