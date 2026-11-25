"""Broker-neutral models and the BrokerAdapter interface.

The rest of TradeMate talks only to BrokerAdapter. It never needs to know that
MT5 (or any other system) is behind it.

Phase 4 adds MANUAL execution (place / modify / close) and candles. Every write
returns an OrderResult that is only "confirmed" once the broker's own state
(the account snapshot) shows the change. The frontend is never the source of truth.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from enum import Enum


class ConnectionState(str, Enum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class ConnectionStatus:
    state: ConnectionState
    message: str
    age_seconds: float | None = None  # how old the latest data is, if known
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        data["warnings"] = list(self.warnings)
        return data


@dataclass(frozen=True)
class AccountInfo:
    login: int
    server: str
    broker: str  # company name reported by the terminal
    environment: str  # "demo" / "real" / ...
    currency: str
    leverage: int | None
    balance: float | None
    equity: float | None
    margin: float | None
    free_margin: float | None
    margin_level: float | None
    profit: float | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    direction: str  # "buy" / "sell"
    volume: float | None
    price_open: float | None
    price_current: float | None
    stop_loss: float | None
    take_profit: float | None
    profit: float | None
    swap: float | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PendingOrder:
    """A pending (not yet filled) order as the broker reports it. It is NOT a position: it holds no exposure until it fills."""

    ticket: int
    symbol: str
    order_type: str            # "buy_limit" | "sell_limit" | "buy_stop" | "sell_stop" (stop-limit types are not supported)
    direction: str             # "buy" / "sell"
    volume: float | None
    price: float | None        # the entry price the order waits for
    stop_loss: float | None
    take_profit: float | None
    expiration: float | None   # epoch seconds (broker clock) or None = good till cancelled
    time_setup: float | None
    status: str = "pending"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Price:
    symbol: str
    bid: float | None
    ask: float | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class QuoteReading:
    """One bid/ask reading plus how old its source was. `age_seconds` is the age of the bridge snapshot the price came from (None = unknown)."""

    symbol: str
    bid: float | None
    ask: float | None
    age_seconds: float | None
    written_at: float | None = None


@dataclass(frozen=True)
class SymbolInfo:
    name: str
    description: str
    digits: int | None
    point: float | None
    spread_points: int | None
    contract_size: float | None
    volume_min: float | None
    volume_max: float | None
    volume_step: float | None
    currency_base: str
    currency_profit: str
    bid: float | None
    ask: float | None
    stops_level: int | None = None  # minimum SL/TP distance from price, in points
    freeze_level: int | None = None
    trade_mode: int | None = None  # MT5 SYMBOL_TRADE_MODE (4 = full access)
    # Filled by on-demand lookups (not in the lean snapshot):
    path: str = ""  # broker category, e.g. "Forex\\Majors" or "Indices\\US"
    currency_margin: str = ""
    tick_size: float | None = None
    tick_value: float | None = None
    visible: bool | None = None  # selected in the MT5 Market Watch
    # Per-tick money value in ACCOUNT currency for one lot. MT5 reports separate values for a gain and a loss
    # (they differ on cross-currency instruments). Older bridges only send tick_value.
    tick_value_profit: float | None = None
    tick_value_loss: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SymbolMatch:
    """One hit from searching the broker's whole instrument catalogue."""

    name: str
    description: str
    path: str
    trade_mode: int | None  # 0 disabled, 1 long only, 2 short only, 3 close only, 4 full
    visible: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SymbolSearch:
    matches: list  # list[SymbolMatch]
    total_matches: int
    total_symbols: int

    def to_dict(self) -> dict:
        return {"matches": [m.to_dict() for m in self.matches], "total_matches": self.total_matches,
                "total_symbols": self.total_symbols, "truncated": self.total_matches > len(self.matches)}


@dataclass(frozen=True)
class OrderResult:
    """Outcome of a write operation (place / modify / close)."""

    operation: str  # "place" | "modify" | "close"
    ok: bool  # the broker accepted the request
    confirmed: bool  # the broker's account state shows the change
    message: str
    retcode: int | None = None
    ticket: int | None = None
    price: float | None = None
    position: Position | None = None  # broker-confirmed position (None after a close)
    execution_id: str | None = None  # TradeMate's journal record of this attempt

    def to_dict(self) -> dict:
        data = asdict(self)
        data["position"] = self.position.to_dict() if self.position else None
        return data


@dataclass(frozen=True)
class ClosedTrade:
    """One closed position, assembled from the broker's deal history (never invented)."""

    ticket: int  # position id
    symbol: str
    direction: str  # "buy" / "sell" (what the position was, not the closing deal)
    volume: float | None
    price_open: float | None
    price_close: float | None
    stop_loss: float | None
    take_profit: float | None
    profit: float | None  # gross trade profit in account currency
    swap: float | None
    commission: float | None
    net: float | None  # profit + swap + commission: what actually hit the balance
    time_open: int | None  # unix seconds, broker server time (same clock as the candles)
    time_close: int | None
    reason: str  # why it closed: sl / tp / so / client / mobile / web / expert / other

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Candle:
    time: int  # unix seconds (broker server time as reported by MT5)
    open: float
    high: float
    low: float
    close: float

    def to_list(self) -> list:
        return [self.time, self.open, self.high, self.low, self.close]


class BrokerAdapter(ABC):
    """What TradeMate needs from any broker / execution system."""

    @abstractmethod
    def get_connection_status(self) -> ConnectionStatus:
        """Never raises. Always returns a status, even if the broker is down."""

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """Raises BrokerUnavailableError if data is not available."""

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Raises BrokerUnavailableError if data is not available."""

    @abstractmethod
    def get_symbols(self) -> list[SymbolInfo]:
        """Raises BrokerUnavailableError if data is not available."""

    @abstractmethod
    def get_symbol_price(self, symbol: str) -> Price:
        """Raises BrokerUnavailableError if data is not available or the symbol is unknown."""

    def get_quote(self, symbol: str) -> "QuoteReading":
        """Bid/ask with the age of its source, from ONE read where the adapter can. Default: price + connection status (two reads)."""
        status = self.get_connection_status()
        price = self.get_symbol_price(symbol)
        return QuoteReading(price.symbol, price.bid, price.ask, status.age_seconds)

    # ---------- Phase 4: execution and chart data ----------
    @abstractmethod
    def get_candles(self, symbol: str, timeframe: str, count: int = 300) -> list[Candle]:
        """Raises BrokerUnavailableError if the broker cannot supply candles."""

    @abstractmethod
    def get_orders(self) -> list["PendingOrder"]:
        raise NotImplementedError("This adapter does not report pending orders.")

    def supports_pending_orders(self) -> bool:
        return False

    def place_pending_order(self, symbol, order_type, volume, price, stop_loss=None, take_profit=None, expiration=None):
        raise NotImplementedError("This adapter cannot place pending orders.")

    def cancel_pending_order(self, ticket):
        raise NotImplementedError("This adapter cannot cancel pending orders.")

    def modify_pending_order(self, ticket, price=None, stop_loss=None, take_profit=None, expiration=None, change=()):
        raise NotImplementedError("This adapter cannot modify pending orders.")

    def place_order(self, symbol: str, side: str, volume: float,
                    stop_loss: float | None = None, take_profit: float | None = None) -> OrderResult:
        """Market order. Raises OrderRejectedError / ExecutionUnconfirmedError / BrokerUnavailableError."""

    @abstractmethod
    def modify_position(self, ticket: int, stop_loss: float | None, take_profit: float | None,
                        change_sl: bool = True, change_tp: bool = True) -> OrderResult:
        """Change SL and/or TP of an open position. None for a changed field removes it."""

    @abstractmethod
    def close_position(self, ticket: int) -> OrderResult:
        """Close an open position completely."""

    # ---------- Phase 5: any instrument the broker offers ----------
    @abstractmethod
    def search_symbols(self, query: str, limit: int = 40) -> SymbolSearch:
        """Search the broker's WHOLE catalogue (name, description, category)."""

    @abstractmethod
    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        """Full info for any broker symbol; selects it on demand. Raises BrokerUnavailableError if unknown."""

    @abstractmethod
    def set_watchlist(self, symbols: list[str]) -> None:
        """Tell the bridge which symbols to keep streaming prices for."""

    @abstractmethod
    def add_to_watchlist(self, symbol: str) -> None:
        """Add one symbol to the streamed set."""

    # ---------- Phase 4 (chart + history) ----------
    def get_symbol_spec(self, symbol: str) -> SymbolInfo:
        """Contract/tick data needed to turn a price move into account-currency money."""
        raise NotImplementedError

    def get_history(self, days: int = 30, limit: int = 200) -> list[ClosedTrade]:
        """Closed trades from the broker's own history, newest first."""
        raise NotImplementedError

    # ---------- execution safety (implemented by adapters that keep an execution journal) ----------
    journal = None  # ExecutionJournal | None

    def reconcile(self, exec_id: str) -> dict:
        """Compare an unresolved write with the real account state."""
        raise NotImplementedError

    def resolve(self, exec_id: str, outcome: str) -> dict:
        """Record what the user found in the broker terminal for an unresolved write."""
        raise NotImplementedError

    def settle_in_flight(self) -> list[dict]:
        """Mark writes interrupted by an unexpected error as UNKNOWN."""
        return []
