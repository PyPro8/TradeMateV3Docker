"""A DETERMINISTIC fake MT5 bridge for the Candle Momentum tests.

No threads, no sleeping, no files, no network: a manual clock plus scripted quotes / candles / positions for any number of symbols and timeframes. Nothing
here can place a real order: it has no order method at all (the engine's feed is read-only). The engine's feed interface (AdapterFeed) is implemented
directly, so tests drive `engine.step()` tick by tick.

(The file-protocol fake `tests/fake_bridge.py` is used separately, through the real MT5Adapter, for the end-to-end Autopilot -> Risk Engine -> pipeline tests.)
"""

from __future__ import annotations

from types import SimpleNamespace

from trademate.candle_momentum.feed import FeedError
from trademate.candle_momentum.models import CandleSnap, Quote
from trademate.instruments import build_spec
from trademate.strategy.timeframes import TF_SECONDS

EURUSD = dict(name="EURUSD", description="Euro vs US Dollar", digits=5, point=1e-05, tick_size=1e-05, tick_value=1.0, tick_value_profit=1.0, tick_value_loss=1.0,
              contract_size=100000.0, volume_min=0.01, volume_max=100.0, volume_step=0.01, stops_level=10, freeze_level=0, path="Forex\\Majors",
              currency_base="EUR", currency_profit="USD", currency_margin="USD")
CRASH100 = dict(name="Crash 100 Index", description="Crash 100 Index", digits=3, point=0.001, tick_size=0.001, tick_value=0.001, tick_value_profit=0.001, tick_value_loss=0.001,
                contract_size=1.0, volume_min=0.01, volume_max=100.0, volume_step=0.01, stops_level=0, freeze_level=0, path="Synthetic\\Crash",
                currency_base="", currency_profit="USD", currency_margin="USD")


class FakeClock:
    def __init__(self, t: float = 1_700_000_100.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> float:
        self.t += s
        return self.t


class FakeFeed:
    """Implements the engine's feed interface for several symbols. Single-symbol convenience: `tick(bid)` acts on the first symbol."""

    def __init__(self, clock, symbols: dict | None = None, offset: int = 0, start: dict | None = None):
        self.clock = clock
        self.specs = {f["name"]: dict(f) for f in (symbols or {"EURUSD": EURUSD}).values()}
        self.symbol = next(iter(self.specs))
        self.offset = offset
        self.is_connected = True
        self.fail_quote = set()
        self.fail_candle = set()
        self.fail_spec = False
        self.quote_age: dict = {}
        self.bids = {n: (start or {}).get(n, 1.10000 if f["digits"] == 5 else 1000.000) for n, f in self.specs.items()}
        self.candles: dict = {}                       # (symbol, tf) -> [open_time, open]
        self.positions: dict = {}
        self.history: dict = {}
        self.spec_overrides: dict = {}
        self.adapter = object()
        self.quote_calls = self.candle_calls = 0

    def _spread(self, symbol):
        return 0.00012 if self.specs[symbol]["digits"] == 5 else 0.002

    def _candle(self, symbol, tf):
        if (symbol, tf) not in self.candles:
            length = TF_SECONDS[tf]
            self.candles[(symbol, tf)] = [int((self.clock() + self.offset) // length * length), self.bids[symbol]]
        return self.candles[(symbol, tf)]

    # ---- scripting
    def tick(self, bid: float, symbol: str | None = None, age: float = 0.0) -> None:
        symbol = symbol or self.symbol
        self.bids[symbol] = round(bid, 8)
        self.quote_age[symbol] = age

    def new_candle(self, tf: str, symbol: str | None = None, open_price: float | None = None) -> None:
        symbol = symbol or self.symbol
        c = self._candle(symbol, tf)
        c[0] += TF_SECONDS[tf]
        c[1] = self.bids[symbol] if open_price is None else open_price

    def roll_to(self, tf: str, symbol: str | None = None) -> None:
        """Move the manual clock to the next boundary of the candle currently held for (symbol, tf) and open the new candle at the current bid."""
        symbol = symbol or self.symbol
        c = self._candle(symbol, tf)
        self.clock.t = float(c[0] + TF_SECONDS[tf] - self.offset)
        self.new_candle(tf, symbol)

    # ---- the feed interface
    def connected(self):
        return (self.is_connected, "ok" if self.is_connected else "MT5 is not connected (test)")

    def quote(self, symbol):
        self.quote_calls += 1
        if symbol in self.fail_quote or not self.is_connected:
            raise FeedError("No quote available (test).")
        b = self.bids[symbol]
        return Quote(symbol, b, round(b + self._spread(symbol), 8), self.quote_age.get(symbol, 0.0), self.clock(), self.clock())

    def candle(self, symbol, timeframe):
        self.candle_calls += 1
        if symbol in self.fail_candle:
            raise FeedError("candles unavailable (test)")
        t, o = self._candle(symbol, timeframe)
        b = self.bids[symbol]
        return CandleSnap(symbol, timeframe, t, o, max(o, b), min(o, b), b, t - TF_SECONDS[timeframe], self.clock())

    def spec(self, symbol, overrides, now):
        if self.fail_spec:
            raise FeedError("spec unavailable (test)")
        f = dict(self.specs[symbol])
        b = self.bids[symbol]
        return build_spec(SimpleNamespace(**f, bid=b, ask=b + self._spread(symbol)), {**self.spec_overrides, **(overrides or {})}, now, symbol)

    def server_offset(self, symbol, now):
        return self.offset

    def open_tickets(self):
        return set(self.positions)

    def closed(self):
        return dict(self.history)
