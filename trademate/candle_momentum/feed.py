"""The engine's only window onto the broker: quotes, the forming candle, the instrument specification, positions and history. A thin adapter over
BrokerAdapter so tests can substitute a deterministic fake. Read-only: this module cannot send an order."""

from __future__ import annotations

import time

from trademate.candle_momentum.models import CandleSnap, Quote
from trademate.errors import BrokerError
from trademate.instruments import build_spec
from trademate.risk.account_state import resolve_server_clock


class FeedError(Exception):
    pass


class AdapterFeed:
    def __init__(self, adapter, clock=time.time, resolver=None):
        self.adapter, self._clock, self._resolver = adapter, clock, resolver

    def connected(self) -> tuple:
        from trademate.broker.base import ConnectionState
        st = self.adapter.get_connection_status()
        return st.state is ConnectionState.CONNECTED, st.message

    def quote(self, symbol: str) -> Quote:
        try:
            r = self.adapter.get_quote(symbol)
        except (BrokerError, NotImplementedError) as exc:
            raise FeedError(str(exc) or "No quote available.") from exc
        return Quote(symbol, r.bid, r.ask, r.age_seconds, self._clock(), r.written_at)

    def candle(self, symbol: str, timeframe: str) -> CandleSnap:
        try:
            bars = self.adapter.get_candles(symbol, timeframe, 3)
        except (BrokerError, NotImplementedError) as exc:
            raise FeedError(f"{timeframe} candles could not be fetched: {exc}") from exc
        if not bars:
            raise FeedError(f"The broker returned no {timeframe} candles.")
        bars = sorted(bars, key=lambda b: b.time)
        f = bars[-1]
        return CandleSnap(symbol, timeframe, int(f.time), f.open, f.high, f.low, f.close, int(bars[-2].time) if len(bars) > 1 else None, self._clock())

    def spec(self, symbol: str, overrides: dict | None, now: float):
        try:
            info = self.adapter.get_symbol_spec(symbol)
        except (BrokerError, NotImplementedError) as exc:
            raise FeedError(f"The instrument specification could not be read: {exc}") from exc
        return build_spec(info, overrides or {}, now, symbol)

    def server_offset(self, symbol: str, now: float):
        try:
            c = resolve_server_clock(self.adapter, now, self._resolver, first=(symbol,))
            return c.offset_seconds if c.known else None
        except Exception:                                           # noqa: BLE001 - a clock lookup must never stop monitoring
            return None

    def open_tickets(self) -> set:
        try:
            return {p.ticket for p in self.adapter.get_positions()}
        except (BrokerError, NotImplementedError) as exc:
            raise FeedError(str(exc)) from exc

    def open_positions(self) -> list:
        try:
            return list(self.adapter.get_positions())
        except (BrokerError, NotImplementedError) as exc:
            raise FeedError(str(exc)) from exc

    def closed_info(self, ticket):
        return self.closed().get(ticket)

    def closed(self) -> dict:
        try:
            return {t.ticket: {"reason": t.reason, "net": t.net, "price_close": t.price_close, "time_close": t.time_close} for t in self.adapter.get_history(30, 300)}
        except (BrokerError, NotImplementedError) as exc:
            raise FeedError(str(exc)) from exc
