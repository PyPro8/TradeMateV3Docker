"""RAPID FIRE: configuration, constants and validation.   "Beauty in simplicity."

Rapid Fire is an INDEPENDENT strategy. It does not read market structure, CHoCH, trendlines or trend. It watches the forming M1 candle and
trades small, short-lived price moves directly. Nothing here touches the five existing strategies.

Every value below is a configurable INITIAL value, not an assumption baked into the engine. Two settings deliberately have NO default:

  emergency_points          the software-managed emergency distance. Rapid Fire has no conventional broker-side stop-loss, but the central Risk
                            Engine cannot size or approve a trade without a loss distance. This number is that distance (used for risk accounting
                            and as the software emergency exit). It is never sent to the broker as a stop-loss. The user must choose it.

Take profit: either `profit_per_fire` (money, account currency; the distance is CALCULATED from the broker's tick data, see target.py) or, when that is
empty, the fixed `target_points` distance (the original behaviour, unchanged default).
Anti-Twin: ON/OFF only (the rule is in trademate/antitwin_rule.py). `anti_twin_adverse_points`, `anti_twin_mode` and the `at_*` fields belong to the
retired rules: they are still read so older settings load, but they are ignored.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields

STRATEGY_ID = "rapid_fire"
STRATEGY_NAME = "RAPID FIRE"
PHILOSOPHY = "Beauty in simplicity."
VERSION = "1.0.0"
TIMEFRAME = "M1"                  # the ONLY timeframe. There is no setting for it and nothing ever substitutes another one.
MAX_ENTRIES_LIMIT = 5             # upper bound accepted for "maximum entries per candle" (the initial value is 2)
MAX_ANTI_TWIN_DEPTH = 1           # LEGACY (the old adverse-move rule). The current rule chains up to trademate.antitwin_rule.MAX_CHAIN reversals.


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True)
class RFConfig:
    symbol: str = "VOL_80"
    trigger_points: float = 5.0
    target_points: float = 10.0
    max_entries_per_candle: int = 2
    lot: float | None = None
    emergency_points: float | None = None
    anti_twin_enabled: bool = False
    anti_twin_adverse_points: float | None = None
    max_spread_points: float | None = None            # optional spread filter; None = no filter
    # MONEY target: when set, the take-profit distance is CALCULATED from the broker's tick data (target_points is then ignored). None = the fixed distance above.
    profit_per_fire: float | None = None
    commission_per_lot: float = 0.0                   # round-turn commission, account currency per lot (0 = none; the bridge cannot read it)
    # DEPRECATED, kept only so older saved settings still load. Anti-Twin now has ONE rule and two settings (ON/OFF): see trademate/antitwin_rule.py.
    # These fields are accepted but ignored.
    anti_twin_mode: str = "adverse"
    at_arm_percent: float = 50.0
    at_tp_mode: str = "half"
    at_tp_points: float | None = None
    at_max_reversals: int = 1
    at_commission_points: float = 0.0
    at_slippage_points: float = 0.0
    at_buffer_points: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timeframe"] = TIMEFRAME
        return d


_FIELDS = {f.name for f in fields(RFConfig)}


def validate(cfg: RFConfig) -> list[str]:
    """Structural problems with a configuration (what may be SAVED). Missing-but-required values are reported by `readiness`, not here."""
    p: list[str] = []
    if not isinstance(cfg.symbol, str) or not cfg.symbol.strip() or len(cfg.symbol) > 32 or not cfg.symbol.isprintable():
        p.append("Instrument must be a valid broker symbol name.")
    for name, label in (("trigger_points", "Entry trigger distance"), ("target_points", "Take-profit distance")):
        v = getattr(cfg, name)
        if not _num(v) or v <= 0:
            p.append(f"{label} must be a number above 0 (points).")
    if not isinstance(cfg.max_entries_per_candle, int) or isinstance(cfg.max_entries_per_candle, bool) or not 1 <= cfg.max_entries_per_candle <= MAX_ENTRIES_LIMIT:
        p.append(f"Maximum entries per candle must be a whole number from 1 to {MAX_ENTRIES_LIMIT}.")
    for name, label in (("lot", "Lot size"), ("profit_per_fire", "Profit per fire"), ("emergency_points", "Emergency distance"), ("anti_twin_adverse_points", "Anti-Twin reversal threshold"),
                        ("max_spread_points", "Maximum spread")):
        v = getattr(cfg, name)
        if v is not None and (not _num(v) or v <= 0):
            p.append(f"{label} must be a number above 0, or empty.")
    if not _num(cfg.commission_per_lot) or cfg.commission_per_lot < 0:
        p.append("Commission per lot must be a number from 0 up.")
    if not isinstance(cfg.anti_twin_enabled, bool):
        p.append("Anti-Twin enabled must be true or false.")
    if cfg.anti_twin_mode not in ("adverse", "breakeven"):
        p.append("Anti-Twin mode must be 'adverse' or 'breakeven'.")
    if not _num(cfg.at_arm_percent) or not 1 <= cfg.at_arm_percent <= 99:
        p.append("Anti-Twin arming percent must be from 1 to 99.")
    if cfg.at_tp_mode not in ("half", "custom"):
        p.append("Anti-Twin target mode must be 'half' or 'custom'.")
    if cfg.at_tp_mode == "custom" and (not _num(cfg.at_tp_points) or cfg.at_tp_points <= 0):
        p.append("Anti-Twin custom target must be a number above 0 (points).")
    if cfg.at_tp_points is not None and (not _num(cfg.at_tp_points) or cfg.at_tp_points <= 0):
        p.append("Anti-Twin custom target must be empty or a number above 0.")
    if isinstance(cfg.at_max_reversals, bool) or not isinstance(cfg.at_max_reversals, int) or not 1 <= cfg.at_max_reversals <= 5:
        p.append("Anti-Twin maximum reversals must be a whole number from 1 to 5.")
    for n in ("at_commission_points", "at_slippage_points", "at_buffer_points"):
        if not _num(getattr(cfg, n)) or getattr(cfg, n) < 0:
            p.append(f"{n} must be a number from 0 up.")
    return p


def readiness(cfg: RFConfig) -> list[str]:
    """What is still missing before Rapid Fire may open its first trade. Empty list = configuration complete."""
    out: list[str] = []
    if cfg.lot is None:
        out.append("Set a lot size.")
    if cfg.emergency_points is None:
        out.append("Set the emergency distance (points). The Risk Engine cannot approve a trade without a loss distance, and Rapid Fire has no broker-side stop-loss.")
    return out


def anti_twin_state(cfg: RFConfig) -> tuple[str, str]:
    """(status, plain-language message) for the configuration alone."""
    if not cfg.anti_twin_enabled:
        return "DISABLED", "Anti-Twin is switched off."
    return "ARMED", ("On: the first adverse move of a position (its exit price one tick worse than where it started) closes it and opens an equal-volume opposite position at once. "
                     "Each new position is watched the same way from its own entry price.")


def parse(data: dict, base: RFConfig) -> tuple[RFConfig | None, list[str]]:
    """Merge an API payload over `base`. Unknown keys and the timeframe are refused, never ignored silently."""
    if not isinstance(data, dict):
        return None, ["Send a JSON object."]
    problems: list[str] = []
    values = asdict(base)
    for k, v in data.items():
        if k == "timeframe":
            if str(v).upper() != TIMEFRAME:
                problems.append("Rapid Fire runs on M1 only. The timeframe cannot be changed.")
            continue
        if k not in _FIELDS:
            problems.append(f"Unknown setting '{k}'.")
            continue
        if isinstance(v, str) and k != "symbol":
            v = v.strip()
            v = None if v == "" else v
        if k == "symbol":
            v = v.strip() if isinstance(v, str) else v
        if k in ("trigger_points", "target_points", "lot", "profit_per_fire", "commission_per_lot", "emergency_points", "anti_twin_adverse_points", "max_spread_points", "at_arm_percent", "at_tp_points", "at_commission_points",
                 "at_slippage_points", "at_buffer_points") and isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                problems.append(f"'{k}' is not a number.")
                continue
        if k in ("max_entries_per_candle", "at_max_reversals") and isinstance(v, (str, float)):
            try:
                f = float(v)
                v = int(f) if f == int(f) else v
            except (ValueError, OverflowError):
                pass
        values[k] = v
    if problems:
        return None, problems
    cfg = RFConfig(**values)
    problems = validate(cfg)
    return (None, problems) if problems else (cfg, [])
