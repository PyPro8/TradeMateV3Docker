"""The user's risk rules (v5.2). Defaults come from settings.toml [risk]; the user can change them in the UI and
the change is saved in TradeMate's own data folder (data/risk_rules.json). No fixed ceiling other than 'a
percentage cannot be 0 or above 100'."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path

logger = logging.getLogger("trademate.risk")

PERCENT_FIELDS = {
    "max_risk_per_trade_percent": "Max risk per trade",
    "max_daily_loss_percent": "Max daily loss",
    "max_total_active_risk_percent": "Max total active risk",
}
MAX_POSITIONS_LIMIT = 1000


@dataclass(frozen=True)
class RiskRules:
    max_risk_per_trade_percent: float = 1.0
    max_daily_loss_percent: float = 3.0
    max_total_active_risk_percent: float = 3.0
    max_positions: int = 3

    def to_dict(self) -> dict:
        return asdict(self)


def parse_rules(data: dict, base: RiskRules) -> RiskRules:
    """Apply the fields present in `data` on top of `base`. Raises ValueError with a user-facing message."""
    changes: dict = {}
    for key, label in PERCENT_FIELDS.items():
        if key not in data:
            continue
        value = data[key]
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a number.") from None
        if isinstance(value, bool) or not math.isfinite(number) or not 0 < number <= 100:
            raise ValueError(f"{label} must be greater than 0 and at most 100 (%).")
        changes[key] = number
    if "max_positions" in data:
        value = data["max_positions"]
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("Max simultaneous positions must be a whole number.") from None
        if isinstance(value, bool) or not math.isfinite(number) or number != int(number) or not 1 <= number <= MAX_POSITIONS_LIMIT:
            raise ValueError(f"Max simultaneous positions must be a whole number from 1 to {MAX_POSITIONS_LIMIT}.")
        changes["max_positions"] = int(number)
    return replace(base, **changes)


class RiskRulesStore:
    """Saves the user's rules next to TradeMate's other local records. Never touches MT5."""

    def __init__(self, path: Path, defaults: RiskRules):
        self.path = Path(path)
        self.defaults = defaults

    def load(self) -> RiskRules:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return parse_rules(data, self.defaults)
        except FileNotFoundError:
            return self.defaults
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable %s (%s); using the defaults from settings.toml.", self.path, exc)
            return self.defaults

    def save(self, rules: RiskRules) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rules.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(self.path)
