"""Candle Momentum v2: a persistent, tick-driven candle-tracking engine. See docs/CANDLE_MOMENTUM.md."""

from trademate.candle_momentum.models import CMConfig, Quote, CandleSnap, State, validate_config  # noqa: F401

_ENGINE = None


def set_engine(engine) -> None:
    """The application registers its one engine here so the strategy registry (manual Analyse, the pipeline's re-evaluation) can reach it."""
    global _ENGINE
    _ENGINE = engine


def get_engine():
    return _ENGINE
