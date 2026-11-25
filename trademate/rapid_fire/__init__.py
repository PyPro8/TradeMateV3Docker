"""RAPID FIRE: an independent M1 scalping strategy (see docs/RAPID_FIRE.md)."""

from trademate.rapid_fire.models import PHILOSOPHY, RFConfig, STRATEGY_ID, STRATEGY_NAME, TIMEFRAME  # noqa: F401

_engine = None


def set_engine(engine) -> None:
    global _engine
    _engine = engine


def get_engine():
    return _engine
