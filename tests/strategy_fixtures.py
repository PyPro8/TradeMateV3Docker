"""Deterministic H1/M5 scenarios for the two selectable strategies. Candles only: no MT5, no network, no randomness, no orders."""

from __future__ import annotations

import dataclasses

from tests.mtf_fixtures import E, NOW, SEC, mirror, zigzag_end
from trademate.strategies.base import StrategyParams
from trademate.strategies.registry import CATALOGUE, evaluate_strategy_result
from trademate.strategy import StrategyConfig, prepare_series

CFG = StrategyConfig(swing_left=3, swing_right=3, break_mode="close", min_rr=1.5)
PARAMS = StrategyParams()
CLOCK = {"known": True, "source": "test", "detail": "", "now_server": NOW, "offset_seconds": 0}


def prepared(m5_rows, h1_rows, now=NOW, forming_m5=None):
    out = {"M5": prepare_series([list(r) for r in m5_rows], "M5", now), "H1": prepare_series([list(r) for r in h1_rows], "H1", now)}
    return out


def run(strategy_id, m5_rows, h1_rows, params=PARAMS, now=NOW, symbol="TEST"):
    return evaluate_strategy_result(strategy_id, symbol, prepared(m5_rows, h1_rows, now), params, CFG, dict(CLOCK, now_server=now))


# ---------------------------------------------------------------- H1 series
def h1_up():
    """Confirmed higher highs (150, 175, 190) and higher lows (116, 136, 155); last close 172 holds above the last swing low."""
    return zigzag_end([60, 90, 78, 110, 96, 130, 116, 150, 136, 175, 155, 190, 172], 6, SEC["H1"])


def h1_down():
    return mirror(h1_up(), 130.0)


def h1_range():
    return zigzag_end([100, 120, 100, 120, 100, 120, 100, 120, 100, 120, 100, 120, 100, 115], 6, SEC["H1"])


def h1_mixed():
    """Higher highs but LOWER lows: neither bullish nor bearish."""
    return zigzag_end([60, 90, 78, 110, 96, 130, 116, 150, 136, 175, 125, 190, 172], 6, SEC["H1"])


def h1_few_swings():
    return zigzag_end([100, 110, 105, 112, 108, 114, 110, 116, 112, 118, 114], 6, SEC["H1"])


# ---------------------------------------------------------------- M5 series: CHoCH
def m5_choch_up(final=148.0):
    """Downtrend (lower highs 176..146, lower lows to 134), then a closed rally through the last lower high 146 (bullish CHoCH)."""
    return zigzag_end([180, 170, 176, 164, 170, 158, 164, 152, 158, 146, 152, 140, 146, 134, final], 6, SEC["M5"])


def m5_choch_down(final=None):
    return mirror(m5_choch_up(), 160.0 if final is None else final)


# ---------------------------------------------------------------- M5 series: trend continuation
def m5_trend_up(pullback=162.0, final=172.0, leg=6):
    """Uptrend, impulse low L0=158 -> high H=170, pullback to `pullback`, then a rally to `final` (closes above 170 when final > 170)."""
    pts = [124, 134, 128, 140, 134, 146, 140, 152, 146, 158, 152, 164, 158, 170, pullback, final]
    return zigzag_end(pts, leg, SEC["M5"])


def m5_trend_down(pullback=162.0, final=172.0):
    return mirror(m5_trend_up(pullback, final), 130.0)         # same price scale as h1_down() (last close 88)


def append_closes(rows, closes, sec=300, wick=0.05):
    """Continue a series with candles that close at `closes` (each opens at the previous close)."""
    out = [list(r) for r in rows]
    t, prev = out[-1][0] + sec, out[-1][4]
    for c in closes:
        out.append([t, prev, max(prev, c) + wick, min(prev, c) - wick, c])
        prev, t = c, t + sec
    return out


def forming_candle(rows, o, h, l, c, sec=300):
    """A candle that opened right after the last closed one and has not closed yet (use now = its open + 100 s)."""
    return list(rows) + [[rows[-1][0] + sec, o, h, l, c]]


def h1_one_swing():
    return zigzag_end([50, 100, 150, 200, 170], 15, SEC["H1"])


def m5_trend_up_pullback_only(final=166.0):
    return m5_trend_up(pullback=162.0, final=final)


def run_now(strategy_id, m5_rows, h1_rows, params=PARAMS):
    """Evaluate with 'now' 30 s after the newest closed M5 candle (so extended series are never 'in the future')."""
    return run(strategy_id, m5_rows, h1_rows, params, now=m5_rows[-1][0] + SEC["M5"] + 30)
