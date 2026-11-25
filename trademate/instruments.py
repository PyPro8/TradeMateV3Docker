"""The ONE central instrument specification + conversion service.  Pure: no Flask, no broker calls (it receives a SymbolInfo-like object).

Five different things are kept apart and never confused:

  1. decimal precision   SYMBOL_DIGITS            how many decimals the broker QUOTES
  2. platform point      SYMBOL_POINT             the smallest quoted step (10**-digits)
  3. tick size           SYMBOL_TRADE_TICK_SIZE   the smallest step the PRICE can actually move / be placed on
  4. strategy unit       "pip" or "point"         the unit the USER thinks in (defined per asset class below, or overridden)
  5. money per unit      account currency         tick_value / tick_size * unit * lots   (from the broker's own tick value)

Every strategy, the pipeline, the Risk Engine and Autopilot must ask THIS module to convert, so a target shown on a card is the same
distance Autopilot sends.  Nothing here invents a number: when a needed broker value is missing, stale or contradictory the spec is
UNVERIFIED, `blocking_reasons` says exactly what is missing, and automatic execution must not proceed.

Asset-class strategy units (the user's preferred convention, initial values; broker overrides win):
  forex major 0.0001 | forex JPY 0.01 | metal (XAU...) 0.10 (0.01 selectable) | crypto 1.00 | index 1.00 | synthetic (Boom/Crash/...) 1.00
Confirmed account convention (verified against the broker's contract before it is trusted): Crash 100, 1 lot = USD 1.00 per 1.00 price unit
(0.01 lot = $0.01/pip, 0.06 lot = $0.06/pip).
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from trademate.pips import PipDefinition

ASSET_CLASSES = ("forex_major", "forex_jpy", "forex_other", "metal", "crypto", "index", "synthetic", "unknown")
DEFAULT_UNITS = {"forex_major": 0.0001, "forex_jpy": 0.01, "forex_other": 0.0001, "metal": 0.10, "crypto": 1.0, "index": 1.0, "synthetic": 1.0}
METAL_UNIT_CHOICES = (0.10, 0.01)
SPEC_MAX_AGE_SECONDS = 300.0           # a specification older than this is STALE (limits / tick values are re-read, never assumed)
# Account conventions the user CONFIRMED, trusted only after the broker's own contract reproduces them.  (regex, expected money per 1 unit per 1 lot)
CONFIRMED_CONVENTIONS = ((re.compile(r"crash\s*[_ ]?100(?!\d)(?!\s*\d)", re.IGNORECASE), 1.0, "Crash 100: 1.00 lot = 1.00 USD per 1.00 pip"),)

_SYNTH = re.compile(r"boom|crash|volatility|vol[\s_]?\d|step\s?index|jump|range\s?break|drift|dex|spot|v\d+\s?\(1s\)|\bR_\d+|1HZ", re.IGNORECASE)
_METAL = re.compile(r"^(XAU|XAG|XPT|XPD|GOLD|SILVER)", re.IGNORECASE)
_CRYPTO = re.compile(r"^(BTC|ETH|LTC|XRP|BCH|SOL|ADA|DOGE|DOT|BNB|XLM|LNK|LINK|AVAX|MATIC)|bitcoin|ethereum|crypto", re.IGNORECASE)
_INDEX = re.compile(r"^(US30|US500|US100|NAS100|USTEC|SPX|NDX|DJ30|DJI|GER30|GER40|DE30|DE40|DAX|UK100|FTSE|JP225|NIKKEI|FRA40|AUS200|HK50|EU50|STOXX)|index|indices", re.IGNORECASE)
_FX = re.compile(r"^[A-Za-z]{6}([._#-].*)?$")
_FX_MAJOR = {"EURUSD", "GBPUSD", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD"}


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pos(x) -> bool:
    return _num(x) and x > 0


def _d(x) -> Decimal:
    return Decimal(str(x))


def classify(symbol: str, path: str = "", description: str = "") -> str:
    """Asset class from the NAME first (synthetics and crypto names are unambiguous), then the broker category path."""
    name = (symbol or "").strip()
    text = f"{name} {path or ''} {description or ''}"
    if _SYNTH.search(name) or re.search(r"synthetic|derived|boom|crash", path or "", re.IGNORECASE):
        return "synthetic"
    if _METAL.match(name):
        return "metal"
    if _CRYPTO.search(name) or re.search(r"crypto", path or "", re.IGNORECASE):
        return "crypto"
    if _INDEX.search(name) or re.search(r"indic|index", path or "", re.IGNORECASE):
        return "index"
    base = re.sub(r"[._#-].*$", "", name).upper()
    if _FX.match(name) or re.search(r"forex|fx|currenc", path or "", re.IGNORECASE):
        if base[3:6] == "JPY" or base.endswith("JPY"):
            return "forex_jpy"
        return "forex_major" if base in _FX_MAJOR else "forex_other"
    return "unknown"


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    asset_class: str
    digits: int | None
    point: float | None
    tick_size: float | None
    tick_value: float | None            # account currency, one tick, one lot (profit side preferred)
    tick_value_loss: float | None
    contract_size: float | None
    volume_min: float | None
    volume_max: float | None
    volume_step: float | None
    stops_level: int | None
    freeze_level: int | None
    currency_base: str
    currency_profit: str
    currency_margin: str
    bid: float | None
    ask: float | None
    unit: float | None                  # strategy pip/point in PRICE units
    unit_source: str                    # override | asset_class | none
    unit_name: str                      # "pip" | "point"
    fetched_at: float | None
    problems: tuple = ()                # verification failures: why this spec cannot be trusted for execution
    unit_problems: tuple = ()           # the subset that makes the strategy UNIT itself undefined (a decision needs only the unit; an order needs everything)
    notes: tuple = ()                   # informational (e.g. convention confirmed)
    expected_money_per_unit_lot: float | None = None   # a CONFIRMED account convention, if one applies

    # ---- verification -------------------------------------------------------------------------------------------------------------
    @property
    def verified(self) -> bool:
        return not self.problems

    @property
    def status(self) -> str:
        return "VERIFIED" if self.verified else "UNVERIFIED"

    def blocking_reasons(self, now: float | None = None, max_age: float = SPEC_MAX_AGE_SECONDS) -> list[str]:
        out = list(self.problems)
        if self.fetched_at is None:
            out.append("The specification has no read time, so it cannot be shown to be current.")
        elif (time.time() if now is None else now) - self.fetched_at > max_age:
            out.append(f"The specification is stale (read more than {max_age:g}s ago); it must be re-read from MT5.")
        return out

    # ---- conversions (Decimal arithmetic: no float drift in a displayed distance) -------------------------------------------------
    def _need_unit(self) -> Decimal:
        if not _pos(self.unit):
            raise ValueError("; ".join(self.problems) or f"No strategy unit is defined for {self.symbol}.")
        return _d(self.unit)

    def units_to_price(self, units: float) -> float:
        if not _num(units) or units <= 0:
            raise ValueError("The distance must be a positive number of strategy units.")
        return float(_d(units) * self._need_unit())

    def price_to_units(self, distance: float) -> float:
        if not _num(distance):
            raise ValueError("The distance must be a finite number.")
        return float(_d(distance) / self._need_unit())

    def price_to_points(self, distance: float) -> float:
        if not _pos(self.point):
            raise ValueError(f"The platform point of {self.symbol} is unknown.")
        return float(_d(distance) / _d(self.point))

    def points_to_price(self, points: float) -> float:
        if not _pos(self.point):
            raise ValueError(f"The platform point of {self.symbol} is unknown.")
        return float(_d(points) * _d(self.point))

    def price_to_ticks(self, distance: float) -> float:
        if not _pos(self.tick_size):
            raise ValueError(f"The tick size of {self.symbol} is unknown.")
        return float(_d(distance) / _d(self.tick_size))

    def snap(self, price: float, direction: str = "nearest") -> float:
        """Snap to the tick grid. 'down' | 'up' | 'nearest'. No tick size -> unchanged (verification already flags that)."""
        if not _num(price) or not _pos(self.tick_size):
            return price
        steps = _d(price) / _d(self.tick_size)
        rnd = {"down": ROUND_FLOOR, "up": ROUND_CEILING}.get(direction, ROUND_HALF_EVEN)
        return float(steps.to_integral_value(rounding=rnd) * _d(self.tick_size))

    def target_price(self, side: str, entry: float, units: float) -> float:
        """Take-profit `units` beyond `entry`, snapped so it is never CLOSER than asked (BUY up, SELL down)."""
        d = _d(self.units_to_price(units))
        if side == "buy":
            return self.snap(float(_d(entry) + d), "up")
        if side == "sell":
            return self.snap(float(_d(entry) - d), "down")
        raise ValueError("side must be 'buy' or 'sell'.")

    def stop_price(self, side: str, entry: float, units: float) -> float:
        """Stop-loss `units` on the loss side of `entry`, snapped so it is never CLOSER than asked (BUY down, SELL up)."""
        d = _d(self.units_to_price(units))
        if side == "buy":
            return self.snap(float(_d(entry) - d), "down")
        if side == "sell":
            return self.snap(float(_d(entry) + d), "up")
        raise ValueError("side must be 'buy' or 'sell'.")

    def money_per_unit(self, lots: float, loss: bool = False) -> float:
        """Account-currency value of ONE strategy unit of movement at `lots`: tick_value / tick_size * unit * lots (the broker's own numbers)."""
        tv = (self.tick_value_loss if loss and _pos(self.tick_value_loss) else self.tick_value)
        if not (_pos(tv) and _pos(self.tick_size)):
            raise ValueError(f"The tick value/size of {self.symbol} is unknown, so money per unit cannot be calculated.")
        if not _pos(lots):
            raise ValueError("The lot size must be a positive number.")
        return float(_d(tv) / _d(self.tick_size) * self._need_unit() * _d(lots))

    def money_for_distance(self, distance_price: float, lots: float, loss: bool = False) -> float:
        """Account-currency P&L of moving `distance_price` at `lots` (always positive; `loss` selects the loss-side tick value)."""
        return float(_d(abs(distance_price)) / self._need_unit() * _d(self.money_per_unit(lots, loss)))

    def money_per_lot(self, loss: bool = False) -> float:
        """Account-currency value of ONE strategy unit for exactly 1.00 lot."""
        return self.money_per_unit(1.0, loss)

    # ---- volume -------------------------------------------------------------------------------------------------------------------
    def validate_volume(self, lots) -> tuple[bool, str]:
        """(ok, reason). NEVER changes the requested lot size: it only says whether the broker would accept it."""
        if not _pos(lots):
            return False, "The lot size must be a positive number."
        if not (_pos(self.volume_min) and _pos(self.volume_max) and _pos(self.volume_step)):
            return False, f"The broker's volume limits for {self.symbol} are unknown, so the lot size cannot be validated."
        v, lo, hi, st = _d(lots), _d(self.volume_min), _d(self.volume_max), _d(self.volume_step)
        if v < lo:
            return False, f"{lots:g} lots is below the broker minimum of {self.volume_min:g} for {self.symbol}."
        if v > hi:
            return False, f"{lots:g} lots is above the broker maximum of {self.volume_max:g} for {self.symbol}."
        if ((v - lo) / st) != ((v - lo) / st).to_integral_value():
            return False, f"{lots:g} lots is not on the broker's volume step of {self.volume_step:g} (minimum {self.volume_min:g})."
        return True, ""

    def to_pip_definition(self) -> PipDefinition:
        """Adapter for the existing strategy code, which carries a PipDefinition. The size is THIS engine's unit; nothing is recomputed there."""
        if _pos(self.unit) and not self.unit_problems:
            return PipDefinition(True, float(self.unit), self.unit_source, f"{self.symbol}: {self.convention_text()}", self.symbol)
        return PipDefinition(False, None, "unknown", "; ".join(self.unit_problems or self.problems) or f"No strategy unit for {self.symbol}.", self.symbol)

    def convention_text(self) -> str:
        if not _pos(self.unit):
            return "no strategy unit defined"
        pts = (self.unit / self.point) if _pos(self.point) else None
        return (f"{self.asset_class.replace('_', ' ')}: 1 {self.unit_name} = {self.unit:g} price ({pts:g} platform points)" if pts else
                f"{self.asset_class.replace('_', ' ')}: 1 {self.unit_name} = {self.unit:g} price") + f" [{self.unit_source}]"

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("symbol", "asset_class", "digits", "point", "tick_size", "tick_value", "tick_value_loss", "contract_size",
                                           "volume_min", "volume_max", "volume_step", "stops_level", "freeze_level", "currency_base", "currency_profit",
                                           "currency_margin", "unit", "unit_source", "unit_name", "fetched_at")}
        d.update(status=self.status, verified=self.verified, problems=list(self.problems), notes=list(self.notes), convention=self.convention_text())
        try:
            d["money_per_unit_1lot"] = self.money_per_lot()
        except ValueError:
            d["money_per_unit_1lot"] = None
        return d


def build_spec(info, overrides: dict | None = None, now: float | None = None, symbol: str | None = None) -> InstrumentSpec:
    """Build the specification from a broker SymbolInfo (anything with those attributes). `overrides`: symbol -> strategy-unit size in price units.
    Never raises: every inconsistency becomes a `problems` entry, and a spec with problems is UNVERIFIED."""
    g = lambda n, default=None: getattr(info, n, default)           # noqa: E731
    symbol = g("name", "") or symbol or ""          # the requested symbol is used when the broker object carries no name
    path = g("path", "") or ""
    cls = classify(symbol, path, g("description", "") or "")
    digits, point, tick = g("digits"), g("point"), g("tick_size")
    tv_p, tv_l, tv = g("tick_value_profit"), g("tick_value_loss"), g("tick_value")
    tick_value = tv_p if _pos(tv_p) else tv
    problems, notes = [], []
    if not (_num(digits) and int(digits) == digits and 0 <= digits <= 10):
        problems.append("SYMBOL_DIGITS is missing or invalid.")
    if not _pos(point):
        problems.append("SYMBOL_POINT is missing or invalid.")
    if not _pos(tick):
        problems.append("SYMBOL_TRADE_TICK_SIZE is missing (the bridge may need to be updated and re-attached).")
    if not _pos(tick_value):
        problems.append("SYMBOL_TRADE_TICK_VALUE is missing, so money per pip/point cannot be calculated.")
    for label, v in (("SYMBOL_VOLUME_MIN", g("volume_min")), ("SYMBOL_VOLUME_MAX", g("volume_max")), ("SYMBOL_VOLUME_STEP", g("volume_step"))):
        if not _pos(v):
            problems.append(f"{label} is missing or invalid.")
    if _pos(g("volume_min")) and _pos(g("volume_max")) and g("volume_min") > g("volume_max"):
        problems.append("The broker's minimum volume is larger than its maximum (inconsistent).")
    if _pos(tick) and _pos(point) and tick < point * (1 - 1e-9):
        problems.append(f"The tick size ({tick:g}) is smaller than the platform point ({point:g}) (inconsistent).")
    if not (_pos(g("bid")) and _pos(g("ask"))):
        problems.append("Current bid/ask prices are not available.")
    elif g("ask") < g("bid"):
        problems.append("The ask price is below the bid price (inconsistent quote).")
    # ---- the strategy unit
    unit, source = None, "none"
    n_before = len(problems)
    ov = (overrides or {}).get(symbol)
    if ov is not None:
        if _pos(ov):
            unit, source = float(ov), "override"
        else:
            problems.append(f"The configured pip/point size for {symbol} ({ov!r}) is not a positive number.")
    elif cls in DEFAULT_UNITS:
        unit, source = DEFAULT_UNITS[cls], "asset_class"
        if cls == "metal" and _pos(point) and point > 0.10:
            unit = float(point)
        if cls in ("forex_major", "forex_jpy", "forex_other") and _num(digits) and _pos(point):
            unit = float(point) * 10 if int(digits) in (5, 3) else float(point)     # forex: 1 pip = 10 points on 5/3-digit quotes
    else:
        problems.append(f"No pip/point convention exists for {symbol or 'this instrument'} (asset class unknown). Define one in the pip settings; TradeMate will not guess.")
    if unit is not None and _pos(tick) and _pos(unit):
        ratio = _d(unit) / _d(tick)
        if ratio < 1 or abs(ratio - ratio.to_integral_value()) > Decimal("1e-9"):
            problems.append(f"The strategy unit {unit:g} is not a whole number of ticks ({tick:g}) for {symbol}; a target in this unit could not be placed exactly.")
    unit_problems = tuple(problems[n_before:])
    # ---- confirmed account convention, validated against the broker's own contract
    expected = None
    for rx, per_unit, label in CONFIRMED_CONVENTIONS:
        if rx.search(symbol):
            expected = per_unit
            if _pos(tick_value) and _pos(tick) and _pos(unit):
                derived = float(_d(tick_value) / _d(tick) * _d(unit))
                if abs(derived - per_unit) <= 1e-6 * max(1.0, per_unit):
                    notes.append(f"Confirmed convention reproduced by the broker's contract: {label}.")
                else:
                    problems.append(f"The confirmed convention ({label}) is NOT reproduced by the broker's contract: it gives {derived:g} per unit per lot, "
                                    f"not {per_unit:g}. Execution is blocked until this is resolved.")
            else:
                problems.append(f"The confirmed convention ({label}) cannot be validated: the broker's tick value or tick size is missing.")
    return InstrumentSpec(
        symbol=symbol, asset_class=cls, digits=int(digits) if _num(digits) else None, point=point if _pos(point) else None, tick_size=tick if _pos(tick) else None,
        tick_value=tick_value if _pos(tick_value) else None, tick_value_loss=tv_l if _pos(tv_l) else (tick_value if _pos(tick_value) else None),
        contract_size=g("contract_size") if _pos(g("contract_size")) else None, volume_min=g("volume_min") if _pos(g("volume_min")) else None,
        volume_max=g("volume_max") if _pos(g("volume_max")) else None, volume_step=g("volume_step") if _pos(g("volume_step")) else None,
        stops_level=g("stops_level"), freeze_level=g("freeze_level"), currency_base=g("currency_base", "") or "", currency_profit=g("currency_profit", "") or "",
        currency_margin=g("currency_margin", "") or "", bid=g("bid"), ask=g("ask"), unit=unit, unit_source=source,
        unit_name="pip" if cls.startswith("forex") else "point" if cls in ("crypto", "index", "synthetic") else "pip" if cls == "metal" else "unit",
        fetched_at=time.time() if now is None else now, problems=tuple(problems), unit_problems=unit_problems, notes=tuple(notes), expected_money_per_unit_lot=expected)
