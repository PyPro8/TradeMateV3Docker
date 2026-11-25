"""Broker integration. The rest of TradeMate should import from here only."""

from trademate.broker.base import (
    AccountInfo,
    BrokerAdapter,
    Candle,
    ClosedTrade,
    ConnectionState,
    ConnectionStatus,
    OrderResult,
    Position,
    Price,
    SymbolInfo,
    SymbolMatch,
    SymbolSearch,
)
from trademate.broker.mt5_adapter import MT5Adapter
from trademate.config import Settings
from trademate.journal import ExecutionJournal


def create_adapter(settings: Settings) -> BrokerAdapter:
    """Build the broker adapter. Only MT5 exists for now."""
    journal = ExecutionJournal(settings.app.data_dir / "trademate.sqlite3")
    return MT5Adapter(settings.mt5, settings.trading, settings.mode.value, journal=journal)


__all__ = [
    "AccountInfo", "BrokerAdapter", "Candle", "ClosedTrade", "OrderResult", "ConnectionState", "ConnectionStatus",
    "MT5Adapter", "Position", "Price", "SymbolInfo", "SymbolMatch", "SymbolSearch", "create_adapter",
]
