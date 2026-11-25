"""MT5 adapter (snapshot reads + manual DEMO execution).

How it works
------------
MetaTrader 5 runs under Wine. A small Expert Advisor, mt5/TradeMateBridge.mq5,
runs INSIDE the terminal and writes a JSON snapshot (account, positions,
symbols) about once a second into MT5's shared "Common/Files" folder. This
adapter only READS that file. It never talks to MT5 directly, never writes
anything. Orders go the other way through mt5_commands.CommandChannel: a command
file that the EA executes and answers. Every write is confirmed against the
snapshot (the broker's own state) before it is reported as done.

Connection status is derived from the snapshot:
    file missing / stale        -> DISCONNECTED (MT5 or the EA is not running)
    terminal not on the broker  -> CONNECTING (just started) or DISCONNECTED
    unreadable / wrong schema   -> ERROR
    account is not DEMO         -> ERROR (TradeMate refuses non-demo accounts)
    otherwise                   -> CONNECTED
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import dataclasses
import threading
import time
from pathlib import Path

from trademate.broker.base import (
    AccountInfo,
    BrokerAdapter,
    Candle,
    ClosedTrade,
    ConnectionState,
    ConnectionStatus,
    OrderResult,
    PendingOrder,
    Position,
    Price,
    SymbolInfo,
    SymbolMatch,
    SymbolSearch,
)
from trademate.broker.mt5_commands import CommandChannel
from trademate.broker.server_check import check_server
from trademate.broker.validation import check_stops, check_volume
from trademate.pending import PENDING_TYPES, validate_pending
from trademate.config import Mt5Settings, TradingSettings
from trademate.errors import (BrokerUnavailableError, CommandWithdrawnError, ExecutionUnconfirmedError,
                              BrokerRejectedError, OrderRejectedError)
from trademate.journal import UNRESOLVED_STATES, ExecutionJournal, JournalError

logger = logging.getLogger("trademate.broker.mt5")

SCHEMA_VERSION = 2
WATCH_FILE = "trademate_watch.txt"
MAX_WATCH = 100
SYMBOL_NAME_RE = re.compile(r"^[A-Za-z0-9._#@&$+\-]{1,40}$")  # what MT5 allows in names; no spaces/newlines
TIMEFRAMES = ("M1", "M2", "M5", "M15", "M30", "H1", "H4", "D1")
CONNECT_GRACE_SECONDS = 30  # a freshly started bridge may still be connecting
CONFIRM_POLL_SECONDS = 0.05  # how often the account snapshot is re-read while waiting for a position to appear / disappear (was 0.15 s: the wait ends up to 100 ms sooner)

_COMMON_FILES_PATTERNS = (
    "drive_c/users/*/AppData/Roaming/MetaQuotes/Terminal/Common/Files",
    "drive_c/users/*/Application Data/MetaQuotes/Terminal/Common/Files",
)


def find_common_files_dir(configured: Path | None) -> tuple[Path | None, list[str]]:
    """Locate MT5's shared Common/Files folder. Returns (folder or None, places searched)."""
    if configured is not None:
        return (configured if configured.is_dir() else None), [str(configured)]

    searched: list[str] = []
    prefixes: list[Path] = []
    env_prefix = os.environ.get("WINEPREFIX")
    if env_prefix:
        prefixes.append(Path(env_prefix).expanduser())
    prefixes += [Path.home() / ".mt5", Path.home() / ".wine"]

    for prefix in prefixes:
        for pattern in _COMMON_FILES_PATTERNS:
            searched.append(str(prefix / pattern))
            try:
                matches = sorted(prefix.glob(pattern))
            except OSError:
                continue
            for match in matches:
                if match.is_dir():
                    return match, searched
    return None, searched


# ---------- tolerant value readers (the snapshot is external input) ----------
def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _positive(value) -> float | None:
    """SL/TP of 0 means 'none' in MT5."""
    number = _num(value)
    return number if number is not None and number > 0 else None


def _int(value) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _obj(value) -> dict:
    return value if isinstance(value, dict) else {}


class _Intent:
    """Handle on one journal record. Without a journal every call is a no-op (tests and tools only)."""

    def __init__(self, journal: ExecutionJournal | None, expires_in: float, **details):
        self.journal, self.sent, self.id = journal, False, None
        if journal is not None:
            try:
                self.id = journal.begin(expires_in=expires_in, **details)
            except JournalError as exc:   # fail CLOSED: no record, no order
                logger.exception("Cannot record the order intent; refusing to send")
                raise BrokerUnavailableError(
                    f"TradeMate could not record this order in its journal, so it was NOT sent: {exc}") from exc

    def _update(self, **fields) -> None:
        if self.journal is None or self.id is None:
            return
        try:
            self.journal.update(self.id, **fields)
        except JournalError:   # the order is already moving: log loudly, never change what happens to it
            logger.exception("Could not update journal record %s with %s", self.id, fields)

    def stage(self, name: str, cmd_id: str) -> None:
        if name == "created":
            self.sent = True
        self._update(stage=name, cmd_id=cmd_id)

    def note(self, **fields) -> None:
        self._update(**fields)

    def finish(self, state: str, message: str, **fields) -> None:
        self._update(state=state, message=message, **fields)


class MT5Adapter(BrokerAdapter):
    def __init__(self, settings: Mt5Settings, trading: TradingSettings | None = None, mode: str = "development",
                 journal: ExecutionJournal | None = None):
        self.journal = journal  # TradeMate's own record of every write; None = unrecorded (tests / tools only)
        self._settings = settings
        self._trading = trading or TradingSettings()
        self._mode = mode
        self._write_lock = threading.Lock()  # one write at a time
        self._watch: list[str] | None = None  # loaded lazily from the watch file
        self._watch_lock = threading.Lock()

    # ---------- reading the snapshot ----------
    def _status(self, state: ConnectionState, message: str, age: float | None = None,
                warnings: tuple[str, ...] = ()) -> ConnectionStatus:
        return ConnectionStatus(state=state, message=message, age_seconds=age, warnings=warnings)

    def _read(self) -> tuple[dict | None, float | None, ConnectionStatus | None]:
        """Returns (snapshot, age_seconds, problem). If problem is set, snapshot is unusable."""
        cfg = self._settings
        directory, searched = find_common_files_dir(cfg.data_dir)
        if directory is None:
            if cfg.data_dir is not None:
                return None, None, self._status(
                    ConnectionState.ERROR, f"Configured MT5 data folder does not exist: {cfg.data_dir}")
            return None, None, self._status(
                ConnectionState.DISCONNECTED,
                "Could not find MT5's shared files folder. Is MT5 installed in Wine and has it been "
                "started once? You can set [mt5] data_dir in config/settings.toml.")

        path = directory / cfg.snapshot_file
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return None, None, self._status(
                ConnectionState.DISCONNECTED,
                "No snapshot from MT5 yet. Start MT5 and attach the TradeMateBridge EA to a chart.")
        except OSError as exc:
            return None, None, self._status(ConnectionState.ERROR, f"Cannot access the MT5 snapshot: {exc}")

        age = max(0.0, time.time() - mtime)
        if age > cfg.stale_after_seconds:
            return None, age, self._status(
                ConnectionState.DISCONNECTED,
                f"MT5 stopped updating TradeMate (last update {age:.0f}s ago). "
                "Is MT5 running with the TradeMateBridge EA attached?", age)

        data = None
        error: Exception | None = None
        for attempt in range(2):  # a retry covers catching the file mid-replace
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                error = None
                break
            except (OSError, ValueError) as exc:
                error = exc
                time.sleep(0.05)
        if error is not None:
            logger.warning("Snapshot unreadable: %s", error)
            return None, age, self._status(
                ConnectionState.ERROR, "The MT5 snapshot file could not be read (see logs).", age)

        if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
            got = data.get("schema") if isinstance(data, dict) else "n/a"
            return None, age, self._status(
                ConnectionState.ERROR,
                f"Snapshot format mismatch (expected schema {SCHEMA_VERSION}, got {got}). "
                "Re-install the TradeMateBridge EA from this TradeMate version.", age)
        return data, age, None

    # ---------- BrokerAdapter ----------
    def _evaluate(self) -> tuple[ConnectionStatus, dict | None]:
        """One read of the snapshot -> (status, snapshot). The snapshot is None unless CONNECTED."""
        try:
            data, age, problem = self._read()
            if problem is not None:
                return problem, None
            terminal, account, bridge = _obj(data.get("terminal")), _obj(data.get("account")), _obj(data.get("bridge"))

            login = _int(account.get("login")) or 0
            environment = _str(account.get("trade_mode")).lower()
            if login and environment != "demo":
                return self._status(
                    ConnectionState.ERROR,
                    f"The MT5 account environment is '{environment or 'unknown'}'. TradeMate is DEMO-only "
                    "at this stage and refuses to use any other account.", age), None

            if terminal.get("connected") is not True or not login:
                uptime = _num(bridge.get("uptime_seconds"))
                if uptime is not None and uptime < CONNECT_GRACE_SECONDS:
                    return self._status(
                        ConnectionState.CONNECTING,
                        "MT5 bridge just started; waiting for the broker connection.", age), None
                return self._status(
                    ConnectionState.DISCONNECTED,
                    "MT5 is running but is not connected to the broker. "
                    "Check the connection indicator at the bottom-right of MT5.", age), None

            warnings: list[str] = []
            server_ok, server_reason = check_server(self._settings.expected_server, _str(account.get("server")))
            if not server_ok:
                warnings.append(f"{server_reason} New orders are refused.")
            return self._status(
                ConnectionState.CONNECTED, "Receiving live data from MT5.", age, tuple(warnings)), data
        except Exception:  # reading MT5 data must never take TradeMate down
            logger.exception("Unexpected error while reading MT5 data")
            return self._status(
                ConnectionState.ERROR, "Unexpected error while reading MT5 data (see logs)."), None

    def get_connection_status(self) -> ConnectionStatus:
        return self._evaluate()[0]

    def _require_data(self) -> dict:
        status, data = self._evaluate()
        if data is None:
            raise BrokerUnavailableError(status.message)
        return data

    def get_account(self) -> AccountInfo:
        account = _obj(self._require_data().get("account"))
        login = _int(account.get("login"))
        if not login:
            raise BrokerUnavailableError("The MT5 snapshot has no account information.")
        return AccountInfo(
            login=login,
            server=_str(account.get("server")),
            broker=_str(account.get("company")),
            environment=_str(account.get("trade_mode")).lower(),
            currency=_str(account.get("currency")),
            leverage=_int(account.get("leverage")),
            balance=_num(account.get("balance")),
            equity=_num(account.get("equity")),
            margin=_num(account.get("margin")),
            free_margin=_num(account.get("free_margin")),
            margin_level=_num(account.get("margin_level")),
            profit=_num(account.get("profit")),
        )

    def get_positions(self) -> list[Position]:
        positions = []
        for item in self._require_data().get("positions") or []:
            item = _obj(item)
            ticket = _int(item.get("ticket"))
            if ticket is None:
                continue
            positions.append(Position(
                ticket=ticket,
                symbol=_str(item.get("symbol")),
                direction=_str(item.get("type")).lower(),
                volume=_num(item.get("volume")),
                price_open=_num(item.get("price_open")),
                price_current=_num(item.get("price_current")),
                stop_loss=_num(item.get("sl")),
                take_profit=_num(item.get("tp")),
                profit=_num(item.get("profit")),
                swap=_num(item.get("swap")),
            ))
        return positions

    def get_symbols(self) -> list[SymbolInfo]:
        symbols = []
        for item in self._require_data().get("symbols") or []:
            item = _obj(item)
            name = _str(item.get("name"))
            if not name:
                continue
            symbols.append(SymbolInfo(
                name=name,
                description=_str(item.get("description")),
                digits=_int(item.get("digits")),
                point=_num(item.get("point")),
                spread_points=_int(item.get("spread")),
                contract_size=_num(item.get("contract_size")),
                volume_min=_num(item.get("volume_min")),
                volume_max=_num(item.get("volume_max")),
                volume_step=_num(item.get("volume_step")),
                currency_base=_str(item.get("currency_base")),
                currency_profit=_str(item.get("currency_profit")),
                bid=_num(item.get("bid")),
                ask=_num(item.get("ask")),
                stops_level=_int(item.get("stops_level")),
                freeze_level=_int(item.get("freeze_level")),
                trade_mode=_int(item.get("trade_mode")),
            ))
        return symbols

    def get_symbol_price(self, symbol: str) -> Price:
        wanted = symbol.strip().lower()
        symbols = self.get_symbols()
        for info in symbols:
            if info.name == symbol or info.name.lower() == wanted:
                return Price(symbol=info.name, bid=info.bid, ask=info.ask)
        raise BrokerUnavailableError(f"Symbol '{symbol}' is not in the MT5 Market Watch.")

    def get_quote(self, symbol: str):
        """Bid/ask and the age of the snapshot they came from, in ONE snapshot read (the Candle Momentum engine calls this several times a second)."""
        from trademate.broker.base import QuoteReading
        status, data = self._evaluate()
        if data is None:
            raise BrokerUnavailableError(status.message)
        wanted = symbol.strip().lower()
        for item in data.get("symbols") or []:
            item = _obj(item)
            name = _str(item.get("name"))
            if name == symbol or name.lower() == wanted:
                return QuoteReading(name, _num(item.get("bid")), _num(item.get("ask")), status.age_seconds, _num(data.get("written_at_gmt")))
        raise BrokerUnavailableError(f"Symbol '{symbol}' is not in the MT5 snapshot.")

    # =====================================================================
    # Phase 4: execution (manual, DEMO only) and candles
    # =====================================================================
    def _channel(self) -> CommandChannel:
        directory, _ = find_common_files_dir(self._settings.data_dir)
        if directory is None:
            raise BrokerUnavailableError("Could not find MT5's shared files folder.")
        return CommandChannel(directory)

    def _guard(self) -> dict:
        """Everything that must be true before ANY write. Returns the fresh snapshot."""
        if not self._trading.enabled:
            raise OrderRejectedError("Trading is disabled ([trading] enabled = false in config/settings.toml).")
        if self._mode == "live":
            raise OrderRejectedError("TradeMate refuses to trade in 'live' mode. Execution is DEMO-only.")
        status, data = self._evaluate()
        if data is None:
            raise BrokerUnavailableError(status.message)
        account = _obj(data.get("account"))
        if _str(account.get("trade_mode")).lower() != "demo":  # belt and braces: _evaluate already checks
            raise OrderRejectedError("REFUSED: the MT5 account is not a DEMO account.")
        server_ok, server_reason = check_server(self._settings.expected_server, _str(account.get("server")))
        if not server_ok:
            logger.warning("Order refused by the server check: %s", server_reason)   # server names only, never secrets
            raise OrderRejectedError(f"REFUSED: {server_reason}")
        bridge = _obj(data.get("bridge"))
        if bridge.get("trading_enabled") is False:
            raise OrderRejectedError(
                "MT5 is not allowing the bridge to trade. Turn on 'Algo Trading' in the MT5 toolbar and make sure "
                "TradeMateBridge has AllowTrading=true.")
        return data

    def _symbol(self, name: str) -> SymbolInfo:
        """Symbol info from the snapshot, or asked of MT5 on demand (any broker instrument)."""
        try:
            return self.get_symbol_info(name)
        except BrokerUnavailableError as exc:
            # Writes report a plain rejection; "MT5 is down" stays an unavailable error.
            if "does not exist" in str(exc) or "valid symbol" in str(exc):
                raise OrderRejectedError(str(exc)) from exc
            raise

    def _position(self, ticket: int) -> Position | None:
        for pos in self.get_positions():
            if pos.ticket == ticket:
                return pos
        return None

    def _wait_for(self, condition, source=None) -> tuple[bool, Position | None]:
        """Poll the broker's snapshot until condition(items_by_ticket) is true or time runs out.
        `source` = the reader to poll (default: open positions; pass self.get_orders for pending orders)."""
        deadline = time.monotonic() + self._trading.confirm_timeout_seconds
        read = source or self.get_positions
        while True:
            try:
                state = {p.ticket: p for p in read()}
                ok, pos = condition(state)
                if ok:
                    return True, pos
            except BrokerUnavailableError:
                pass
            if time.monotonic() >= deadline:
                return False, None
            time.sleep(CONFIRM_POLL_SECONDS)

    def _run(self, fields: dict, timeout: float | None = None, intent: "_Intent | None" = None) -> dict:
        timeout = timeout or self._trading.command_timeout_seconds
        return self._channel().send(fields, timeout=timeout, on_stage=intent.stage if intent else None)

    # ---------- execution journal: every write is recorded before it is sent ----------
    def _write(self, details: dict, body):
        """Run one write inside a journal record, so its real outcome can never be lost.

        Before the send: failures are plain failures ("not sent"). From the moment the command file exists, ANY
        failure (lost answer, internal bug, crash in the confirmation step) is "unknown", never "failed": the
        order may be live at the broker. Unexpected exceptions are logged in full and re-raised as an
        ExecutionUnconfirmedError so the UI/API report the truth instead of a bare HTTP 500.
        """
        intent = _Intent(self.journal, expires_in=self._trading.command_timeout_seconds + 5, **details)
        try:
            result = body(intent)
        except OrderRejectedError as exc:
            intent.finish("rejected", str(exc))
            raise
        except CommandWithdrawnError as exc:
            intent.finish("not_sent", str(exc))
            raise
        except ExecutionUnconfirmedError as exc:
            exc.execution_id = intent.id
            intent.finish("unknown", str(exc))
            raise
        except BrokerUnavailableError as exc:
            if not intent.sent:
                intent.finish("not_sent", str(exc))
                raise
            intent.finish("unknown", str(exc))
            raise ExecutionUnconfirmedError(
                f"The order was sent but TradeMate lost contact with MT5 before it could confirm it ({exc}). "
                "The outcome is UNKNOWN.", intent.id) from exc
        except Exception as exc:
            logger.exception("Unexpected error during %s (execution %s, command sent: %s)",
                             details.get("action"), intent.id, intent.sent)
            if not intent.sent:
                intent.finish("not_sent", f"Internal error before anything was sent: {exc}")
                raise
            intent.finish("unknown", f"Internal error after the command was sent: {exc}")
            raise ExecutionUnconfirmedError(
                "The command was sent to MT5 but TradeMate hit an internal error afterwards. "
                "The outcome is UNKNOWN.", intent.id) from exc
        intent.finish("confirmed" if result.confirmed else "unconfirmed", result.message,
                      ticket=result.ticket, retcode=result.retcode)
        return dataclasses.replace(result, execution_id=intent.id)

    @staticmethod
    def _same_volume(a, b) -> bool:
        return a is not None and b is not None and abs(float(a) - float(b)) < 1e-6

    def _block_if_unresolved(self, action: str, symbol=None, side=None, volume=None, ticket=None) -> None:
        """Refuse to repeat a write whose earlier attempt has an unresolved outcome (never resend blindly).

        Each unresolved attempt is first reconciled against the live account. Only if it is STILL unresolved and
        identical to the new request is the new one refused.
        """
        if self.journal is None:
            return
        for rec in self.journal.unresolved():
            try:
                rec = self.reconcile(rec["id"])
            except BrokerUnavailableError:
                pass  # cannot look right now: treat as still unresolved
            if rec["state"] not in UNRESOLVED_STATES or rec["action"] != action:
                continue
            if action in ("place", "pending_place"):
                identical = (rec["symbol"] or "").lower() == (symbol or "").lower() and rec["side"] == side \
                    and self._same_volume(rec["volume"], volume)
            else:
                identical = rec["ticket"] == ticket
            if identical:
                what = (f"{(rec['side'] or '').upper()} {rec['volume']} {rec['symbol']}" if action in ("place", "pending_place")
                        else f"{action} #{rec['ticket']}")
                raise OrderRejectedError(
                    f"Blocked: an identical earlier request ({what}) was sent and its outcome is still unknown. "
                    "Check MT5's Trade/History tab. Then use 'Check again' or confirm it was not executed, "
                    "before sending this again.")

    def reconcile(self, exec_id: str) -> dict:
        """Compare one unresolved attempt with the real account and record what is provable. Sends nothing to MT5
        except read-only data requests; it can never place, change or close anything."""
        if self.journal is None:
            raise OrderRejectedError("This adapter keeps no execution journal.")
        rec = self.journal.get(exec_id)
        if rec is None:
            raise OrderRejectedError("Unknown execution record.")
        if rec["state"] not in UNRESOLVED_STATES:
            return rec
        positions = self.get_positions()           # BrokerUnavailableError propagates: cannot check right now
        expired = time.time() > (rec["expires_at"] or 0)   # after expiry the bridge can no longer run the command
        action, state, message = rec["action"], None, None
        if action == "place":
            known = set(rec["before_tickets"])
            floor = max(known) if known else 0
            wanted = lambda sym, side, vol: ((sym or "").lower() == (rec["symbol"] or "").lower()
                                             and side == rec["side"] and self._same_volume(vol, rec["volume"]))
            found = [p for p in positions if p.ticket not in known and wanted(p.symbol, p.direction, p.volume)]
            if found:
                state, message = "reconciled_executed", f"Checked the account: position #{found[0].ticket} exists, so the order DID execute."
                self.journal.update(exec_id, ticket=found[0].ticket)
            else:
                try:
                    closed = [t for t in self.get_history(2, 100)
                              if t.ticket not in known and t.ticket > floor and wanted(t.symbol, t.direction, t.volume)]
                except (BrokerUnavailableError, NotImplementedError):
                    closed = []
                if closed:
                    state, message = "reconciled_executed", (
                        f"Checked MT5 history: position #{closed[0].ticket} was opened and has already closed, "
                        "so the order DID execute.")
                    self.journal.update(exec_id, ticket=closed[0].ticket)
                else:
                    message = ("Checked the account: no matching position found" + (
                        ". The command can no longer run, so it most likely did NOT execute, but this cannot be proven: "
                        "look at MT5's History tab, then confirm below." if expired else
                        " yet. The bridge may still be processing it; check again in a few seconds."))
        elif action in ("pending_place", "pending_cancel", "pending_modify"):
            state, message = self._reconcile_pending(rec, positions, expired)
        elif action == "close":
            if not any(p.ticket == rec["ticket"] for p in positions):
                state, message = "reconciled_executed", f"Checked the account: #{rec['ticket']} is no longer open, so it was closed."
            elif expired:
                state, message = "reconciled_not_executed", f"Checked the account: #{rec['ticket']} is still open, so the close did NOT happen."
            else:
                message = "Position still open; the bridge may still be processing the close."
        elif action == "modify":
            pos = next((p for p in positions if p.ticket == rec["ticket"]), None)
            if pos is None:
                message = f"#{rec['ticket']} is no longer open, so the change cannot be verified."
            else:
                half = self._point_of(pos.symbol) / 2
                same = lambda a, b: (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= half)
                if same(pos.stop_loss, rec["sl"]) and same(pos.take_profit, rec["tp"]):
                    state, message = "reconciled_executed", "Checked the account: SL/TP already hold the requested values."
                elif expired:
                    state, message = "reconciled_not_executed", "Checked the account: SL/TP differ from the request, so the change did NOT happen."
                else:
                    message = "SL/TP not changed yet; the bridge may still be processing it."
        fields = {"message": message or rec["message"]}
        if state:
            fields["state"] = state
        self.journal.update(exec_id, **fields)
        return self.journal.get(exec_id)

    def _point_of(self, name: str) -> float:
        for info in self.get_symbols():
            if info.name == name and info.point:
                return info.point
        return 1e-5

    def resolve(self, exec_id: str, outcome: str) -> dict:
        """The user looked at MT5 and states what happened. Recorded as the user's word, not as proof."""
        if self.journal is None:
            raise OrderRejectedError("This adapter keeps no execution journal.")
        if outcome not in ("executed", "not_executed"):
            raise OrderRejectedError("outcome must be 'executed' or 'not_executed'.")
        rec = self.journal.get(exec_id)
        if rec is None:
            raise OrderRejectedError("Unknown execution record.")
        if rec["state"] not in UNRESOLVED_STATES:
            return rec
        self.journal.update(exec_id, state="reconciled_executed" if outcome == "executed" else "reconciled_not_executed",
                            message=f"Confirmed by the user after checking MT5: {outcome.replace('_', ' ')}.")
        return self.journal.get(exec_id)

    def settle_in_flight(self) -> list[dict]:
        """Safety net for the web layer: an unexpected error escaped while a write was in flight. Mark it UNKNOWN."""
        if self.journal is None:
            return []
        settled = []
        for rec in self.journal.in_flight():
            self.journal.update(rec["id"], state="unknown",
                                message="An internal error interrupted this request while it was in flight.")
            settled.append(self.journal.get(rec["id"]))
        return settled

    @staticmethod
    def _fail(result: dict, what: str) -> BrokerRejectedError:
        message = str(result.get("message") or "no reason given")
        retcode = result.get("retcode")
        code = f" (retcode {retcode})" if retcode else ""
        return BrokerRejectedError(f"{what} rejected by MT5/broker: {message}{code}",
                                   retcode=retcode if isinstance(retcode, int) else None, broker_message=message)

    def place_order(self, symbol, side, volume, stop_loss=None, take_profit=None) -> OrderResult:
        side = (side or "").lower()
        with self._write_lock:
            self._guard()
            self._block_if_unresolved("place", symbol=symbol, side=side, volume=volume)
            before = [p.ticket for p in self.get_positions()]
            details = dict(action="place", symbol=symbol, side=side, volume=volume, sl=stop_loss, tp=take_profit,
                           before_tickets=before)

            def body(intent):
                info = self._symbol(symbol)
                if info.trade_mode is not None and info.trade_mode not in (4, 1 if side == "buy" else 2):
                    raise OrderRejectedError(f"{info.name} does not allow {side} orders right now (market closed or restricted).")
                vol = check_volume(info, volume, self._trading.max_volume)
                reference = info.bid if side == "buy" else info.ask
                sl, tp = check_stops(info, side, reference, stop_loss, take_profit)
                intent.note(symbol=info.name, volume=vol, sl=sl, tp=tp)

                result = self._run({
                    "cmd": "open", "symbol": info.name, "side": side, "volume": vol,
                    "sl": sl or 0, "tp": tp or 0,
                    "deviation": self._trading.deviation_points, "magic": self._trading.magic,
                }, intent=intent)
                if not result.get("ok"):
                    raise self._fail(result, "Order")

                ticket = result.get("ticket") or None
                retcode = result.get("retcode")
                known = set(before)

                def appeared(state):
                    if ticket and ticket in state:
                        return True, state[ticket]
                    new = [p for t, p in state.items() if t not in known and p.symbol == info.name and p.direction == side]
                    return (True, new[0]) if len(new) == 1 else (False, None)

                confirmed, pos = self._wait_for(appeared)
                if confirmed:
                    return OrderResult("place", True, True, f"Executed: {side.upper()} {vol} {info.name}",
                                       retcode, pos.ticket, result.get("price"), pos)
                return OrderResult("place", True, False,
                                   "The broker accepted the order but the position is not visible in the account yet. "
                                   "Check MT5 before sending another one.", retcode, ticket, result.get("price"))

            return self._write(details, body)

    def modify_position(self, ticket, stop_loss, take_profit, change_sl=True, change_tp=True) -> OrderResult:
        with self._write_lock:
            self._guard()
            self._block_if_unresolved("modify", ticket=int(ticket))
            details = dict(action="modify", ticket=int(ticket), sl=stop_loss if change_sl else None,
                           tp=take_profit if change_tp else None)

            def body(intent):
                current = self._position(int(ticket))
                if current is None:
                    raise OrderRejectedError(f"Position {ticket} is not open in the account (already closed?).")
                intent.note(symbol=current.symbol, side=current.direction, volume=current.volume)
                info = self._symbol(current.symbol)
                new_sl = stop_loss if change_sl else current.stop_loss
                new_tp = take_profit if change_tp else current.take_profit
                # Only the fields being CHANGED are re-validated; an untouched one the broker already holds stays.
                sl, tp = check_stops(info, current.direction, current.price_current,
                                     new_sl if change_sl else None, new_tp if change_tp else None)
                sl = sl if change_sl else current.stop_loss
                tp = tp if change_tp else current.take_profit
                intent.note(sl=sl, tp=tp)       # the FINAL levels, what reconciliation must find at the broker
                half = (info.point or 1e-5) / 2
                if all((x is None and y is None) or (x is not None and y is not None and abs(x - y) <= half)
                       for x, y in ((sl, current.stop_loss), (tp, current.take_profit))):
                    return OrderResult("modify", True, True, "No change: the broker already holds those values.",
                                       None, current.ticket, None, current)

                result = self._run({"cmd": "modify", "ticket": current.ticket, "sl": sl or 0, "tp": tp or 0}, intent=intent)
                if not result.get("ok"):
                    raise self._fail(result, "Modification")

                def same(a, b):
                    if a is None or b is None:
                        return a is None and b is None
                    return abs(a - b) <= half

                def applied(state):
                    pos = state.get(current.ticket)
                    return (pos is not None and same(pos.stop_loss, sl) and same(pos.take_profit, tp)), pos

                confirmed, pos = self._wait_for(applied)
                if confirmed:
                    return OrderResult("modify", True, True, "Executed: SL/TP updated at the broker.",
                                       result.get("retcode"), current.ticket, None, pos)
                return OrderResult("modify", True, False,
                                   "The broker accepted the change but the account does not show it yet. "
                                   "The chart keeps showing the broker's real values.",
                                   result.get("retcode"), current.ticket, None, self._position(current.ticket))

            return self._write(details, body)


    # ---------- pending orders (needs the updated EA: it must report features=["pending_orders"]) ----------
    def get_orders(self) -> list[PendingOrder]:
        data = self._require_data()
        out = []
        for item in data.get("orders") or []:
            item = _obj(item)
            ticket = _int(item.get("ticket"))
            kind = _str(item.get("type")).lower()
            if ticket is None or kind not in PENDING_TYPES:
                continue                       # an order type TradeMate does not support is never shown as one it does
            out.append(PendingOrder(ticket=ticket, symbol=_str(item.get("symbol")), order_type=kind, direction=PENDING_TYPES[kind],
                                    volume=_num(item.get("volume")), price=_num(item.get("price_open")), stop_loss=_num(item.get("sl")),
                                    take_profit=_num(item.get("tp")), expiration=_num(item.get("expiration")),
                                    time_setup=_num(item.get("time_setup"))))
        return out

    def supports_pending_orders(self) -> bool:
        """True only when the running EA says so. An older EA has no pending-order commands, so they stay disabled."""
        try:
            return "pending_orders" in [str(f) for f in (_obj(self._require_data().get("bridge")).get("features") or [])]
        except BrokerUnavailableError:
            return False

    def _require_pending(self) -> None:
        if not self.supports_pending_orders():
            raise OrderRejectedError("Pending orders need the updated TradeMateBridge EA (version 0.5.0 or newer). The running EA does not "
                                     "report pending-order support. Recompile and re-attach it in MT5, then try again. Nothing was sent.")

    def _order(self, ticket: int) -> PendingOrder | None:
        return next((o for o in self.get_orders() if o.ticket == ticket), None)

    def place_pending_order(self, symbol, order_type, volume, price, stop_loss=None, take_profit=None, expiration=None) -> OrderResult:
        with self._write_lock:
            self._guard()
            self._require_pending()
            self._block_if_unresolved("pending_place", symbol=symbol, side=order_type, volume=volume)
            before = [o.ticket for o in self.get_orders()]
            details = dict(action="pending_place", symbol=symbol, side=order_type, volume=volume, sl=stop_loss, tp=take_profit,
                           before_tickets=before)

            def body(intent):
                info = self._symbol(symbol)
                clean = validate_pending(info, order_type, volume, price, stop_loss, take_profit, expiration,
                                         self._trading.max_volume, time.time())
                intent.note(symbol=info.name, volume=clean.volume, sl=clean.sl, tp=clean.tp)
                result = self._run({"cmd": "open_pending", "symbol": info.name, "order_type": clean.order_type, "volume": clean.volume,
                                    "price": clean.price, "sl": clean.sl or 0, "tp": clean.tp or 0, "expiration": clean.expiration or 0,
                                    "magic": self._trading.magic}, intent=intent)
                if not result.get("ok"):
                    raise self._fail(result, "Pending order")
                ticket, known = result.get("ticket") or None, set(before)

                def appeared(state):
                    if ticket and ticket in state:
                        return True, state[ticket]
                    new = [o for t, o in state.items() if t not in known and o.symbol == info.name and o.order_type == clean.order_type]
                    return (True, new[0]) if len(new) == 1 else (False, None)

                confirmed, order = self._wait_for(appeared, self.get_orders)
                if confirmed:
                    return OrderResult("pending_place", True, True,
                                       f"Placed: {clean.order_type.replace('_', ' ').upper()} {clean.volume} {info.name} at {clean.price:g}",
                                       result.get("retcode"), order.ticket, clean.price, order)
                return OrderResult("pending_place", True, False,
                                   "The broker accepted the pending order but it is not visible in the account yet. "
                                   "Check MT5 before sending another one.", result.get("retcode"), ticket, clean.price)

            return self._write(details, body)

    def cancel_pending_order(self, ticket) -> OrderResult:
        with self._write_lock:
            self._guard()
            self._require_pending()
            self._block_if_unresolved("pending_cancel", ticket=int(ticket))
            details = dict(action="pending_cancel", ticket=int(ticket))

            def body(intent):
                current = self._order(int(ticket))
                if current is None:
                    raise OrderRejectedError(f"Pending order {ticket} does not exist (already filled, cancelled or expired?).")
                intent.note(symbol=current.symbol, side=current.order_type, volume=current.volume)
                result = self._run({"cmd": "cancel_order", "ticket": current.ticket}, intent=intent)
                if not result.get("ok"):
                    raise self._fail(result, "Cancel")
                confirmed, _ = self._wait_for(lambda state: (current.ticket not in state, None), self.get_orders)
                if confirmed:
                    filled = any(p.ticket == current.ticket for p in self.get_positions())
                    if filled:
                        return OrderResult("pending_cancel", True, False,
                                           f"The order #{current.ticket} FILLED before the cancel took effect: it is now an open position.",
                                           result.get("retcode"), current.ticket, None, None)
                    return OrderResult("pending_cancel", True, True, f"Cancelled: {current.symbol} {current.order_type} #{current.ticket}",
                                       result.get("retcode"), current.ticket, None, None)
                return OrderResult("pending_cancel", True, False, "The broker accepted the cancel but the order is still listed. Check MT5.",
                                   result.get("retcode"), current.ticket, None, self._order(current.ticket))

            return self._write(details, body)

    def modify_pending_order(self, ticket, price=None, stop_loss=None, take_profit=None, expiration=None, change=()) -> OrderResult:
        change = set(change)
        with self._write_lock:
            self._guard()
            self._require_pending()
            self._block_if_unresolved("pending_modify", ticket=int(ticket))
            details = dict(action="pending_modify", ticket=int(ticket), sl=stop_loss if "sl" in change else None,
                           tp=take_profit if "tp" in change else None)

            def body(intent):
                current = self._order(int(ticket))
                if current is None:
                    raise OrderRejectedError(f"Pending order {ticket} does not exist (already filled, cancelled or expired?).")
                info = self._symbol(current.symbol)
                intent.note(symbol=current.symbol, side=current.order_type, volume=current.volume)
                new = validate_pending(info, current.order_type, current.volume,
                                       price if "price" in change else current.price,
                                       stop_loss if "sl" in change else current.stop_loss,
                                       take_profit if "tp" in change else current.take_profit,
                                       expiration if "expiration" in change else None, self._trading.max_volume, time.time())
                intent.note(sl=new.sl, tp=new.tp)
                half = (info.point or 1e-5) / 2
                same = lambda a, b: (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= half)
                want_exp = new.expiration if "expiration" in change else current.expiration
                if same(new.price, current.price) and same(new.sl, current.stop_loss) and same(new.tp, current.take_profit) \
                        and (want_exp or None) == (current.expiration or None):
                    return OrderResult("pending_modify", True, True, "No change: the broker already holds those values.",
                                       None, current.ticket, None, current)
                result = self._run({"cmd": "modify_order", "ticket": current.ticket, "price": new.price, "sl": new.sl or 0, "tp": new.tp or 0,
                                    "expiration": want_exp or 0}, intent=intent)
                if not result.get("ok"):
                    raise self._fail(result, "Modification")

                def applied(state):
                    o = state.get(current.ticket)
                    return (o is not None and same(o.price, new.price) and same(o.stop_loss, new.sl) and same(o.take_profit, new.tp)), o

                confirmed, order = self._wait_for(applied, self.get_orders)
                if confirmed:
                    return OrderResult("pending_modify", True, True, "Executed: pending order updated at the broker.",
                                       result.get("retcode"), current.ticket, new.price, order)
                return OrderResult("pending_modify", True, False, "The broker accepted the change but the account does not show it yet.",
                                   result.get("retcode"), current.ticket, None, self._order(current.ticket))

            return self._write(details, body)

    def _reconcile_pending(self, rec: dict, positions, expired: bool):
        """Settle an unresolved pending-order write from the live account. Read-only. Returns (state|None, message)."""
        orders = self.get_orders()
        action, ticket = rec["action"], rec["ticket"]
        if action == "pending_place":
            known = set(rec["before_tickets"])
            found = [o for o in orders if o.ticket not in known and (o.symbol or "").lower() == (rec["symbol"] or "").lower()
                     and o.order_type == rec["side"] and self._same_volume(o.volume, rec["volume"])]
            if found:
                self.journal.update(rec["id"], ticket=found[0].ticket)
                return "reconciled_executed", f"Checked the account: pending order #{found[0].ticket} exists, so the order DID get placed."
            return None, ("Checked the account: no matching pending order found. The command can no longer run, so it most likely was NOT "
                          "placed, but it may also have filled or been cancelled already: look at MT5's History tab, then confirm below."
                          if expired else "Checked the account: no matching pending order found yet. The bridge may still be processing it.")
        if action == "pending_cancel":
            if any(o.ticket == ticket for o in orders):
                return (("reconciled_not_executed", f"Checked the account: #{ticket} is still pending, so the cancel did NOT happen.")
                        if expired else (None, "The order is still pending; the bridge may still be processing the cancel."))
            if any(p.ticket == ticket for p in positions):
                return "reconciled_not_executed", f"Checked the account: #{ticket} FILLED and is now an open position, so the cancel did NOT happen."
            return "reconciled_executed", f"Checked the account: #{ticket} is no longer pending (and is not an open position), so it was cancelled."
        order = next((o for o in orders if o.ticket == ticket), None)       # pending_modify
        if order is None:
            return None, f"#{ticket} is no longer pending, so the change cannot be verified."
        half = self._point_of(order.symbol) / 2
        same = lambda a, b: (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= half)
        if same(order.stop_loss, rec["sl"]) and same(order.take_profit, rec["tp"]):
            return "reconciled_executed", "Checked the account: the pending order already holds the requested SL/TP."
        return (("reconciled_not_executed", "Checked the account: the pending order differs from the request, so the change did NOT happen.")
                if expired else (None, "Not changed yet; the bridge may still be processing it."))

    def close_position(self, ticket) -> OrderResult:
        with self._write_lock:
            self._guard()
            self._block_if_unresolved("close", ticket=int(ticket))
            details = dict(action="close", ticket=int(ticket))

            def body(intent):
                current = self._position(int(ticket))
                if current is None:
                    raise OrderRejectedError(f"Position {ticket} is not open in the account (already closed?).")
                intent.note(symbol=current.symbol, side=current.direction, volume=current.volume)
                result = self._run({"cmd": "close", "ticket": current.ticket, "deviation": self._trading.deviation_points},
                                   intent=intent)
                if not result.get("ok"):
                    raise self._fail(result, "Close")
                confirmed, _ = self._wait_for(lambda state: (current.ticket not in state, None))
                if confirmed:
                    return OrderResult("close", True, True, f"Executed: closed {current.symbol} #{current.ticket}",
                                       result.get("retcode"), current.ticket, result.get("price"), None)
                return OrderResult("close", True, False,
                                   "The broker accepted the close but the position is still listed. Check MT5.",
                                   result.get("retcode"), current.ticket, result.get("price"), self._position(current.ticket))

            return self._write(details, body)

    def get_candles(self, symbol, timeframe, count=300) -> list[Candle]:
        tf = (timeframe or "").upper()
        if tf not in TIMEFRAMES:
            raise BrokerUnavailableError(f"Unsupported timeframe '{timeframe}'.")
        info = self._symbol_or_unavailable(symbol)
        result = self._channel().send(
            {"cmd": "candles", "symbol": info.name, "timeframe": tf, "count": max(10, min(int(count), 500))},
            timeout=8.0)
        if not result.get("ok"):
            raise BrokerUnavailableError(str(result.get("message") or "MT5 could not supply candles."))
        candles = []
        for bar in result.get("bars") or []:
            if isinstance(bar, list) and len(bar) == 5 and all(_num(v) is not None for v in bar):
                candles.append(Candle(int(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])))
        return candles

    def _symbol_or_unavailable(self, name: str) -> SymbolInfo:
        try:
            return self._symbol(name)
        except OrderRejectedError as exc:
            raise BrokerUnavailableError(str(exc)) from exc

    # =====================================================================
    # Phase 5: dynamic broker symbols
    # =====================================================================
    def _ask_read_only(self, fields: dict, timeout: float = 10.0) -> dict:
        """Read-only commands (search / info). Old bridges answer 'Unknown command'."""
        result = self._channel().send(fields, timeout=timeout)
        if not result.get("ok"):
            message = str(result.get("message") or "MT5 could not answer.")
            if message.startswith("Unknown command"):
                raise BrokerUnavailableError(
                    "The TradeMateBridge EA in MT5 is older than v0.3 and cannot look up symbols. "
                    "Recompile it (F7) and re-attach it to a chart.")
            raise BrokerUnavailableError(message)
        return result

    @staticmethod
    def _check_symbol_name(name: str) -> str:
        name = (name or "").strip()
        if not SYMBOL_NAME_RE.match(name):
            raise BrokerUnavailableError(f"'{name[:40]}' is not a valid symbol name.")
        return name

    def _symbol_from_item(self, item: dict) -> SymbolInfo | None:
        item = _obj(item)
        name = _str(item.get("name"))
        if not name:
            return None
        return SymbolInfo(
            name=name, description=_str(item.get("description")), digits=_int(item.get("digits")),
            point=_num(item.get("point")), spread_points=_int(item.get("spread")),
            contract_size=_num(item.get("contract_size")), volume_min=_num(item.get("volume_min")),
            volume_max=_num(item.get("volume_max")), volume_step=_num(item.get("volume_step")),
            currency_base=_str(item.get("currency_base")), currency_profit=_str(item.get("currency_profit")),
            bid=_num(item.get("bid")), ask=_num(item.get("ask")),
            stops_level=_int(item.get("stops_level")), freeze_level=_int(item.get("freeze_level")),
            trade_mode=_int(item.get("trade_mode")), path=_str(item.get("path")),
            currency_margin=_str(item.get("currency_margin")), tick_size=_num(item.get("tick_size")),
            tick_value=_num(item.get("tick_value")),
            tick_value_profit=_num(item.get("tick_value_profit")),
            tick_value_loss=_num(item.get("tick_value_loss")),
            visible=item.get("visible") if isinstance(item.get("visible"), bool) else None)

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        name = self._check_symbol_name(symbol)
        wanted = name.lower()
        try:  # cheap path: already streaming in the snapshot
            for info in self.get_symbols():
                if info.name.lower() == wanted and info.bid is not None:
                    return info
        except BrokerUnavailableError:
            raise
        result = self._ask_read_only({"cmd": "symbol_info", "symbol": name})
        info = self._symbol_from_item(result.get("symbol"))
        if info is None:
            raise BrokerUnavailableError(f"MT5 returned no data for '{name}'.")
        return info

    def search_symbols(self, query: str, limit: int = 40) -> SymbolSearch:
        query = (query or "").strip()
        if len(query) > 40 or re.search(r"[\r\n=]", query):
            raise BrokerUnavailableError("Search text is not valid.")
        limit = max(1, min(int(limit), 100))
        result = self._ask_read_only({"cmd": "symbols_search", "query": query, "limit": limit})
        matches = []
        for item in result.get("found") or []:
            item = _obj(item)
            name = _str(item.get("name"))
            if name:
                matches.append(SymbolMatch(name, _str(item.get("description")), _str(item.get("path")),
                                           _int(item.get("trade_mode")),
                                           item.get("visible") is True))
        return SymbolSearch(matches, _int(result.get("matches")) or len(matches),
                            _int(result.get("total_symbols")) or 0)

    # =====================================================================
    # Phase 4: money values for TP/SL lines, and trade history
    # =====================================================================
    def get_symbol_spec(self, symbol: str) -> SymbolInfo:
        """Contract + tick data straight from MT5 (never from the lean snapshot, which has no tick value).

        tick_value is already expressed in the ACCOUNT currency by MT5, so price move -> money needs no
        FX table on our side: money = (move / tick_size) * tick_value * lots.
        """
        name = self._check_symbol_name(symbol)
        try:
            result = self._ask_read_only({"cmd": "symbol_info", "symbol": name})
        except ExecutionUnconfirmedError as exc:
            raise BrokerUnavailableError(f"MT5 did not send the contract data for '{name}' in time.") from exc
        info = self._symbol_from_item(result.get("symbol"))
        if info is None:
            raise BrokerUnavailableError(f"MT5 returned no data for '{name}'.")
        if not info.tick_size or not (info.tick_value or info.tick_value_profit):
            raise BrokerUnavailableError(f"MT5 did not report a tick size/value for '{name}'.")
        return info

    def get_history(self, days: int = 30, limit: int = 200) -> list[ClosedTrade]:
        """Closed positions from MT5's deal history, newest first. Needs the v0.4 bridge."""
        days = max(1, min(int(days), 365))
        limit = max(1, min(int(limit), 500))
        try:
            result = self._channel().send({"cmd": "history", "days": days, "limit": limit}, timeout=20.0)
        except ExecutionUnconfirmedError as exc:   # read-only: nothing can have been traded, so say that
            raise BrokerUnavailableError("MT5 did not send the trade history in time. Try again in a moment.") from exc
        if not result.get("ok"):
            message = str(result.get("message") or "MT5 could not supply the history.")
            if message.startswith("Unknown command"):
                raise BrokerUnavailableError(
                    "The TradeMateBridge EA in MT5 is older than v0.4 and cannot send trade history. "
                    "Recompile it (F7) and re-attach it to a chart.")
            raise BrokerUnavailableError(message)
        trades = []
        for item in result.get("trades") or []:
            item = _obj(item)
            ticket = _int(item.get("ticket"))
            if ticket is None:
                continue
            profit, swap, commission = _num(item.get("profit")), _num(item.get("swap")), _num(item.get("commission"))
            trades.append(ClosedTrade(
                ticket=ticket, symbol=_str(item.get("symbol")), direction=_str(item.get("type")).lower(),
                volume=_num(item.get("volume")), price_open=_num(item.get("price_open")),
                price_close=_num(item.get("price_close")),
                stop_loss=_positive(item.get("sl")), take_profit=_positive(item.get("tp")),
                profit=profit, swap=swap, commission=commission,
                net=None if profit is None else profit + (swap or 0.0) + (commission or 0.0),
                time_open=_int(item.get("time_open")), time_close=_int(item.get("time_close")),
                reason=_str(item.get("reason")).lower() or "other"))
        trades.sort(key=lambda t: t.time_close or 0, reverse=True)
        return trades[:limit]

    # ---------- watchlist file (which symbols the bridge keeps streaming) ----------
    def _watch_path(self) -> Path:
        directory, _ = find_common_files_dir(self._settings.data_dir)
        if directory is None:
            raise BrokerUnavailableError("Could not find MT5's shared files folder.")
        return directory / WATCH_FILE

    def _load_watch(self) -> list[str]:
        if self._watch is None:
            try:
                lines = self._watch_path().read_text(encoding="utf-8").splitlines()
            except (OSError, BrokerUnavailableError):
                lines = []
            self._watch = [n for n in (x.strip() for x in lines) if SYMBOL_NAME_RE.match(n)][:MAX_WATCH]
        return self._watch

    def _save_watch(self) -> None:
        path = self._watch_path()
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text("\n".join(self._watch or []) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            raise BrokerUnavailableError(f"Could not write the watchlist for MT5: {exc}") from exc

    def set_watchlist(self, symbols) -> None:
        clean: list[str] = []
        for name in symbols or []:
            if isinstance(name, str) and SYMBOL_NAME_RE.match(name.strip()) and name.strip() not in clean:
                clean.append(name.strip())
        with self._watch_lock:
            if clean == self._load_watch():
                return
            self._watch = clean[:MAX_WATCH]
            self._save_watch()

    def add_to_watchlist(self, symbol: str) -> None:
        name = self._check_symbol_name(symbol)
        with self._watch_lock:
            current = self._load_watch()
            if name not in current and len(current) < MAX_WATCH:
                self._watch = current + [name]
                self._save_watch()
