"""Data models of the Candle Momentum engine: lifecycle states, the legal transition table, configuration, quotes and candles.

Pure data: no broker, no Flask, no threads. Everything that decides something lives in logic.py (pure functions) and machine.py (the state machine).

Vocabulary used throughout (see docs/CANDLE_MOMENTUM.md):
  strategy unit   the user's pip / point for the instrument (trademate.instruments decides what that is). Distances in a config are in this unit.
  price basis     the BID. MT5 candles are built from bid prices, so candle open and live price are compared like-for-like on the bid.
  executable      the price an order would really fill at: ASK for a BUY, BID for a SELL.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, fields, replace
from enum import Enum

STRATEGY_ID = "bull_bear"
RULE_ID = "BULL-BEAR-v3"
ENGINE_VERSION = "2.0.0"


class State(str, Enum):
    """The sixteen lifecycle states of ONE candle of ONE (symbol, timeframe) configuration."""

    NEW_CANDLE = "NEW_CANDLE"                # a new candle was identified; its open is known; no price judged yet
    MONITORING = "MONITORING"                # tracking the candle; price equals its open (no direction)
    BULLISH = "BULLISH"                      # price is above the candle open; the bullish trigger has not been reached
    BEARISH = "BEARISH"                      # price is below the candle open; the bearish trigger has not been reached
    TRIGGER_REACHED = "TRIGGER_REACHED"      # price crossed the trigger in the candle's direction
    ENTRY_VALIDATION = "ENTRY_VALIDATION"    # the executable quote is being judged (freshness, target still available, protective stop)
    RISK_PENDING = "RISK_PENDING"            # a validated proposal exists; the Risk Engine / executor decide next
    EXECUTING = "EXECUTING"                  # the Risk Engine approved; the order is being submitted
    POSITION_OPEN = "POSITION_OPEN"          # the broker confirmed a position for this candle
    TARGET_REACHED = "TARGET_REACHED"        # price reached the target (the broker's take-profit closes the position)
    POSITION_CLOSED = "POSITION_CLOSED"      # the position is closed (target, stop or manual)
    INVALIDATED = "INVALIDATED"              # the thesis broke before entry (price returned inside the trigger)
    MISSED = "MISSED"                        # the opportunity is gone (target no longer available / price moved away before submission)
    REJECTED = "REJECTED"                    # a safety rule refused the entry (Risk Engine, protective stop, unverified instrument data ...)
    EXECUTION_FAILED = "EXECUTION_FAILED"    # the order could not be placed / confirmed
    EXPIRED = "EXPIRED"                      # the candle closed or the signal outlived its validity without an entry


S = State
LEGAL: dict = {
    S.NEW_CANDLE: {S.MONITORING, S.BULLISH, S.BEARISH, S.EXPIRED},
    S.MONITORING: {S.BULLISH, S.BEARISH, S.EXPIRED},
    S.BULLISH: {S.MONITORING, S.BEARISH, S.TRIGGER_REACHED, S.EXPIRED},
    S.BEARISH: {S.MONITORING, S.BULLISH, S.TRIGGER_REACHED, S.EXPIRED},
    S.TRIGGER_REACHED: {S.ENTRY_VALIDATION, S.EXPIRED},
    S.ENTRY_VALIDATION: {S.RISK_PENDING, S.MISSED, S.INVALIDATED, S.REJECTED, S.EXPIRED},
    S.RISK_PENDING: {S.EXECUTING, S.MISSED, S.INVALIDATED, S.REJECTED, S.EXPIRED, S.EXECUTION_FAILED},
    S.EXECUTING: {S.POSITION_OPEN, S.EXECUTION_FAILED, S.REJECTED, S.MISSED},
    S.POSITION_OPEN: {S.TARGET_REACHED, S.POSITION_CLOSED},
    S.TARGET_REACHED: {S.POSITION_CLOSED},
    S.EXECUTION_FAILED: {S.ENTRY_VALIDATION, S.POSITION_OPEN},     # a bounded retry (only after a provably unsent order) or a late reconciliation
    S.REJECTED: set(), S.MISSED: set(), S.INVALIDATED: set(), S.EXPIRED: set(), S.POSITION_CLOSED: set(),
}
PRE_TRIGGER = {S.NEW_CANDLE, S.MONITORING, S.BULLISH, S.BEARISH}
VALIDATING = {S.TRIGGER_REACHED, S.ENTRY_VALIDATION, S.RISK_PENDING}                  # a live signal that has not been committed to an order
SIGNAL_ACTIVE = {S.RISK_PENDING, S.EXECUTING}                                          # states in which the published decision is BUY / SELL
COMMITTED = {S.EXECUTING, S.POSITION_OPEN, S.TARGET_REACHED, S.POSITION_CLOSED}        # an order was (or is being) sent: never entered again
IN_POSITION = {S.POSITION_OPEN, S.TARGET_REACHED}
TERMINAL = {S.POSITION_CLOSED, S.INVALIDATED, S.MISSED, S.REJECTED, S.EXPIRED, S.EXECUTION_FAILED}
ENTRY_ATTEMPTED = COMMITTED | {S.REJECTED, S.EXECUTION_FAILED, S.MISSED}               # states that mean "an entry decision was made for this candle"
MATERIAL = set(State) - {S.MONITORING, S.BULLISH, S.BEARISH}                           # transitions persisted immediately (direction flicker is debounced)

BULL, BEAR, NEUTRAL = "bullish", "bearish", "neutral"


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True)
class BBConfig:
    """One configuration = one (symbol, timeframe). All distances are in STRATEGY UNITS (pips / points) of that instrument and are converted to price by
    the central instrument engine (trademate.instruments); nothing here knows what a pip is.

    The six timing / validity controls the old strategy lacked are separate and named:
      trigger_units                    trigger crossing detection: how far from the candle open the trigger sits
      max_slippage_units               execution slippage tolerance: how far the live executable price may be worse than the validated quote when the order
                                       is submitted (None = the pipeline's existing [pipeline] max_entry_drift_stop_fraction protection applies unchanged)
      (remaining target distance)      always measured from the ACTUAL executable quote to the level a full target from the trigger would have reached
      signal_expiry_seconds            a reached trigger that has not become an order within this time is EXPIRED
      quote_max_age_seconds            a quote older than this is never used to judge an entry
    """

    symbol: str
    timeframe: str
    enabled: bool = True
    trigger_units: float = 10.0
    target_units: float = 20.0
    sl_buffer_units: float | None = 50.0           # the protective stop: this many units beyond the candle OPEN, on the loss side (the existing policy)
    max_slippage_units: float | None = None
    signal_expiry_seconds: float = 15.0
    quote_max_age_seconds: float = 3.0
    max_entry_attempts: int = 1                   # 1 = no retry. A retry is only ever made after an order that PROVABLY was not sent.
    retry_delay_seconds: float = 2.0
    # ---- Anti-Twin (Bull/Bear only; see docs/BULL_BEAR.md). Disabled by default: an existing saved config behaves exactly as before.
    anti_twin_enabled: bool = False
    at_arm_percent: float = 50.0                  # the original must first move this % of ITS OWN target distance in its favour before a return to break-even can trigger
    at_tp_mode: str = "half"                      # "half": the twin's target is half of the original's target distance; "custom": at_tp_units
    at_tp_units: float | None = None              # custom twin target distance in strategy units (required when at_tp_mode == "custom")
    at_max_reversals: int = 1                     # twins allowed per ORIGINAL trade (a twin may itself reverse until the total is reached)
    at_commission_units: float = 0.0              # round-trip commission, in strategy units, added to the break-even level
    at_slippage_units: float = 0.0                # expected slippage, in strategy units, added to the break-even level
    at_buffer_units: float = 0.0                  # any extra break-even buffer, in strategy units

    @property
    def key(self) -> str:
        return config_key(self.symbol, self.timeframe)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @staticmethod
    def from_dict(d: dict, base: "BBConfig") -> "BBConfig":
        known = {f.name for f in fields(BBConfig)}
        return replace(base, **{k: v for k, v in (d or {}).items() if k in known and k not in ("symbol", "timeframe")})


def config_key(symbol: str, timeframe: str) -> str:
    return f"{symbol}|{timeframe}"


def validate_config(cfg: BBConfig, timeframes: tuple) -> list:
    """-> list of problems (empty = valid). Never repairs anything."""
    p = []
    if not isinstance(cfg.symbol, str) or not cfg.symbol.strip() or len(cfg.symbol) > 32 or not cfg.symbol.isprintable():
        p.append("A valid instrument is required.")
    if cfg.timeframe not in timeframes:
        p.append(f"Timeframe must be one of {', '.join(timeframes)}.")
    if not isinstance(cfg.enabled, bool):
        p.append("enabled must be true or false.")
    for name in ("trigger_units", "target_units"):
        v = getattr(cfg, name)
        if not _num(v) or not 0 < v <= 100000:
            p.append(f"{name} must be a number above 0.")
    # the protective stop is mandatory: a missing / non-positive buffer is reported here AND refuses the entry at runtime (see logic.validate_entry)
    if cfg.sl_buffer_units is not None and (not _num(cfg.sl_buffer_units) or not 0 < cfg.sl_buffer_units <= 100000):
        p.append("sl_buffer_units must be a number above 0 (the protective stop is mandatory).")
    if cfg.max_slippage_units is not None and (not _num(cfg.max_slippage_units) or not 0 < cfg.max_slippage_units <= 100000):
        p.append("max_slippage_units must be empty or a number above 0.")
    for name, hi in (("signal_expiry_seconds", 3600), ("quote_max_age_seconds", 600), ("retry_delay_seconds", 600)):
        v = getattr(cfg, name)
        if not _num(v) or not 0 < v <= hi:
            p.append(f"{name} must be a number above 0 and at most {hi}.")
    if isinstance(cfg.max_entry_attempts, bool) or not isinstance(cfg.max_entry_attempts, int) or not 1 <= cfg.max_entry_attempts <= 3:
        p.append("max_entry_attempts must be a whole number from 1 to 3 (1 = no retry).")
    if not isinstance(cfg.anti_twin_enabled, bool):
        p.append("anti_twin_enabled must be true or false.")
    if not _num(cfg.at_arm_percent) or not 1 <= cfg.at_arm_percent <= 99:
        p.append("at_arm_percent must be a number from 1 to 99 (how far the original must move in its favour before Anti-Twin can arm).")
    if cfg.at_tp_mode not in ("half", "custom"):
        p.append("at_tp_mode must be 'half' or 'custom'.")
    if cfg.at_tp_mode == "custom" and (cfg.at_tp_units is None or not _num(cfg.at_tp_units) or not 0 < cfg.at_tp_units <= 100000):
        p.append("at_tp_units must be a number above 0 when the twin target mode is custom.")
    if cfg.at_tp_units is not None and (not _num(cfg.at_tp_units) or not 0 < cfg.at_tp_units <= 100000):
        p.append("at_tp_units must be empty or a number above 0.")
    if isinstance(cfg.at_max_reversals, bool) or not isinstance(cfg.at_max_reversals, int) or not 1 <= cfg.at_max_reversals <= 5:
        p.append("at_max_reversals must be a whole number from 1 to 5.")
    for name in ("at_commission_units", "at_slippage_units", "at_buffer_units"):
        v = getattr(cfg, name)
        if not _num(v) or not 0 <= v <= 100000:
            p.append(f"{name} must be a number from 0 up.")
    return p


@dataclass(frozen=True)
class Quote:
    """One executable quote as read from the broker bridge. `age_seconds` is how old the SOURCE data was when it was read (snapshot file age)."""

    symbol: str
    bid: float | None
    ask: float | None
    age_seconds: float | None          # None = unknown (never treated as fresh)
    read_at: float                     # wall-clock when TradeMate read it
    snapshot_at: float | None = None   # when the bridge wrote the snapshot (wall clock), if known

    @property
    def valid(self) -> bool:
        return _num(self.bid) and _num(self.ask) and self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "bid": self.bid, "ask": self.ask, "age_seconds": self.age_seconds, "read_at": self.read_at, "snapshot_at": self.snapshot_at}


@dataclass(frozen=True)
class CandleSnap:
    """The forming candle as the broker reports it (times are broker SERVER time, like every candle in TradeMate)."""

    symbol: str
    timeframe: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    newest_closed_time: int | None = None
    fetched_at: float = field(default_factory=time.time)


@dataclass
class Transition:
    """One recorded state change. Every field the specification asks for is here."""

    at: float
    symbol: str
    timeframe: str
    candle_id: str
    from_state: str | None
    state: str
    price: float | None
    candle_open: float | None
    direction: str | None
    trigger_price: float | None
    entry_price: float | None
    target_price: float | None
    explanation: str

    def to_dict(self) -> dict:
        return {"timestamp": self.at, "symbol": self.symbol, "timeframe": self.timeframe, "candle_id": self.candle_id, "from_state": self.from_state,
                "state": self.state, "current_price": self.price, "candle_open": self.candle_open, "direction": self.direction,
                "trigger_price": self.trigger_price, "entry_price": self.entry_price, "target_price": self.target_price, "explanation": self.explanation}
