"""Deterministic fixtures for the multi-timeframe Strategy Engine tests. No MT5, no network, no randomness, no orders.

The four timeframes are generated INDEPENDENTLY (they are not aggregations of each other); the engine never assumes they are. Every
series ends at E, a multiple of 14400 s, so every timeframe grid (M5, M15, H1 and H4) is aligned.
"""

from __future__ import annotations

from trademate.broker.base import Candle, ConnectionState, ConnectionStatus
from trademate.errors import BrokerUnavailableError
from trademate.strategy import DataRules, StrategyConfig, prepare_series
from trademate.strategy.mtf import (BULL, ESTABLISHED, NEUTRAL, NONE, R_NONE, TimeframeAnalysis)
from trademate.strategy.timeframes import OK, TF_SECONDS

E = 18000 * 98000          # end of every series (broker-clock epoch); the last candle OPENS at E - length
NOW = E + 30               # "now" on the broker clock: 30 s after the last candle closed, so no candle is forming
SEC = dict(TF_SECONDS)


def zigzag_end(points, leg, sec, end=E, wick=0.05):
    """Candles that move linearly through `points`, `leg` candles per leg, the last candle opening at end - sec."""
    n = (len(points) - 1) * leg
    t, prev, rows = end - n * sec, points[0], []
    for a, b in zip(points, points[1:]):
        for k in range(1, leg + 1):
            c, o = a + (b - a) * k / leg, prev
            rows.append([t, o, max(o, c) + wick, min(o, c) - wick, c])
            prev, t = c, t + sec
    return rows


def ladder(start, pairs, up=6, down=3):
    """Waypoints of a steady uptrend: up `up`, back `down`, repeated (higher highs and higher lows)."""
    pts = [start]
    for _ in range(pairs):
        pts.append(pts[-1] + up)
        pts.append(pts[-1] - down)
    return pts


def mirror(rows, pivot):
    """The same market upside down around `pivot` (bullish becomes bearish, highs become lows)."""
    return [[r[0], 2 * pivot - r[1], 2 * pivot - r[3], 2 * pivot - r[2], 2 * pivot - r[4]] for r in rows]


# ---- the reference BUY scenario (hand-checked against the real engines; see tests/test_mtf_strategy.py) ----
def buy_rows():
    h1 = ladder(60, 22)
    h1 = h1 + [h1[-1] + 24, h1[-1] + 8, h1[-1] + 32, h1[-1] + 14]
    shift = 141 - h1[-1]
    h1 = [p + shift for p in h1]
    base15 = ladder(100, 6)
    m15 = [p + (126 - base15[-1]) for p in base15] + [140, 146, 142, 138, 136]
    base5 = ladder(100, 8)
    m5 = [p + (140 - base5[-1]) for p in base5] + [146, 144, 140, 142, 138, 140, 136, 139, 137, 141]
    return {"H4": zigzag_end(h1, 6, 14400), "H1": zigzag_end(h1, 6, 3600),
            "M15": zigzag_end(m15, 8, 900), "M5": zigzag_end(m5, 6, 300)}


def sell_rows():
    return {tf: mirror(rows, 150.0) for tf, rows in buy_rows().items()}


def build_series(rows_by_tf, now=NOW, rules=None):
    """Prepare the four independently fetched native timeframe series."""
    rules = rules or DataRules()
    return {tf: prepare_series(rows_by_tf[tf], tf, now, rules) for tf in ("M5", "M15", "H1", "H4")}


# ---- synthetic per-timeframe analyses for the context rules (no candles needed) ----
def ta(tf, bias=BULL, strength=ESTABLISHED, stage=R_NONE, toward=None, status=OK, analyzed=True, **kw):
    a = TimeframeAnalysis(timeframe=tf, data_status=status, data_reason="" if status == OK else f"{tf} is {status} (test)")
    a.analyzed = analyzed
    if analyzed:
        a.bias, a.strength = bias, (strength if bias != NEUTRAL else NONE)
        a.reversal = {"stage": stage, "toward": toward, "evidence": [], "provisional": False, "confirm_level": None, "conditions": []}
        a.invalidation = {"level": kw.pop("invalidation", None), "basis": "test"}
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def four(h4=None, h1=None, m15=None, m5=None):
    """A dict of four analyses, each defaulting to established bullish."""
    return {"H4": h4 or ta("H4"), "H1": h1 or ta("H1"), "M15": m15 or ta("M15"), "M5": m5 or ta("M5")}


# ---- read-only stub adapter: NO order method exists, so nothing built on it can trade ----
class ReadOnlyFeed:
    """Connected feed with candles, symbol spec and symbols only. Any other attribute (place_order, close_position, ...) raises."""

    def __init__(self, rows_by_tf, now=NOW, offset=0, status=None, fail=()):
        self.rows, self.now, self.offset = rows_by_tf, now, offset
        self.status = status or ConnectionStatus(ConnectionState.CONNECTED, "ok", 0.2)
        self.fail, self.requested = set(fail), []

    def get_connection_status(self):
        return self.status

    def get_symbols(self):
        return []

    def get_symbol_spec(self, symbol):
        class Spec:
            tick_size = 0.01
            digits = 2
        return Spec()

    def get_candles(self, symbol, timeframe, count=300):
        tf = timeframe.upper()
        self.requested.append(tf)
        if tf in self.fail or tf not in ("M1", "M5", "M15", "H1", "H4") or (tf != "M1" and tf not in self.rows):
            raise BrokerUnavailableError(f"Unsupported or unavailable timeframe '{timeframe}'.")
        if tf == "M1":      # the broker-clock probe: a fresh 1-minute candle whose open time is the broker's "now" (UTC + offset)
            t = int(self.now - 1 + self.offset) // 60 * 60
            return [Candle(t - 60 * (9 - i), 100.0, 100.5, 99.5, 100.0) for i in range(10)]
        return [Candle(*c) for c in self.rows[tf]]

    def __getattr__(self, name):                       # pragma: no cover - only reached when something tries to trade
        raise AssertionError(f"The strategy touched {name!r} on a read-only feed (it must never trade or size).")


DEFAULT_CFG = StrategyConfig(trigger_lookback=24)     # the reference scenario's zone reaction is 22 M5 candles before the end
