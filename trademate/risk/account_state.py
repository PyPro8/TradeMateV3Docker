"""Builds the Risk Engine's view of the account from the data TradeMate already synchronises from MT5.

    account (balance, equity)         adapter.get_account()
    open positions                    adapter.get_positions()            -> active risk
    tick data per position symbol     adapter.get_symbol_spec(symbol)
    closed trades today               adapter.get_history()              -> today's realised result
Everything is read-only.

Daily loss: start-of-day balance = balance now - net result of trades closed today. Today's result = closed result
+ floating profit/swap of open positions (i.e. equity - start-of-day balance). Deposits/withdrawals are not tracked.
"Today" is the computer's LOCAL calendar day (local 00:00 to 00:00, DST-safe, trademate.localday); MT5's history is on the broker's server clock, which is converted
with the measured offset. The server's clock is resolved by
trademate.brokertime (live measurement -> recent cache -> configured `broker_timezone`). If NONE of those is available the
daily loss is reported as UNKNOWN (never zero, and never the computer's local day: that silently used the wrong
day boundary, which is why Today's Loss / Remaining Daily Loss did not reset at the broker's midnight).
Missing loss data is never read as 0: a closed trade without a result or close time, an open position without a profit
figure, or a history that may be cut short (the bridge limit was reached) all make the daily loss UNKNOWN with a reason.
"""

from __future__ import annotations

import logging
import time

from trademate import brokertime, localday
from trademate.brokertime import day_start_from_offset, next_reset_utc  # noqa: F401  (day_start_from_offset is re-exported)
from trademate.errors import BrokerError, BrokerUnavailableError
from trademate.risk.engine import AccountRiskState, position_risk
from trademate.risk.position_sizing import SymbolSpec

logger = logging.getLogger("trademate.risk")
DAY = 86400
_LOCAL_TZ_NAME = {"name": ""}


_SOFTWARE_STOPS = {"fn": None}


def set_software_stops_provider(fn) -> None:
    """Optional: fn() -> {ticket: price}. A position WITHOUT a broker-side stop-loss but WITH a software-managed emergency level registered here (Rapid Fire)
    is measured against that level, so it counts toward active risk instead of making every later trade 'incalculable'. A broker-side stop always wins.
    Nothing registered (or a provider that fails) = exactly the previous behaviour: a position without a stop is unprotected / risk unknown."""
    _SOFTWARE_STOPS["fn"] = fn


def _software_stops() -> dict:
    fn = _SOFTWARE_STOPS["fn"]
    if fn is None:
        return {}
    try:
        out = fn()
        return out if isinstance(out, dict) else {}
    except Exception:                                    # noqa: BLE001 - fail closed: no level means the risk stays unknown
        logger.exception("Software stop provider failed; positions without a broker stop keep an unknown risk")
        return {}


def set_local_timezone(name: str | None) -> None:
    """[ui] timezone (IANA name); empty = the computer's own timezone."""
    _LOCAL_TZ_NAME["name"] = name or ""
HISTORY_DAYS = 3        # the broker day is at most ~24 h old; 3 days leaves a wide margin
HISTORY_LIMIT = 500     # the bridge never returns more than this many closed positions


def to_spec(info) -> SymbolSpec:
    return SymbolSpec(name=info.name, tick_size=info.tick_size, tick_value=info.tick_value,
                      tick_value_loss=info.tick_value_loss, volume_min=info.volume_min,
                      volume_max=info.volume_max, volume_step=info.volume_step, trade_mode=info.trade_mode)


def detect_server_offset(adapter, symbols: list[str], now_utc: float) -> tuple[int | None, str]:
    """Broker server offset from UTC from FRESH 1-minute candles. (None, why) when it cannot be trusted. One-off measurement:
    the application uses the cached resolver in trademate.brokertime instead."""
    return brokertime.ServerClockResolver.probe(adapter, symbols, now_utc)


def build_state(account, positions, specs: dict, closed_trades, day_start: int | None, day_source: str,
                history_error: str = "", *, history_limit: int | None = None, day_start_known_at: float | None = None,
                server_offset: int | None = None, next_reset: int | None = None, software_stops: dict | None = None) -> AccountRiskState:
    """Pure assembly. `specs` maps symbol -> SymbolSpec (missing = unknown). closed_trades None = unavailable.

    Anything that would turn missing data into a silent zero makes the daily loss UNKNOWN instead (todays_closed_pl = None
    and `history_error` says why): a closed trade of today without a result, a closed trade without a close time, a history
    that reached `history_limit` without reaching back before the day started, an open position without a profit figure."""
    soft = software_stops or {}
    risks = tuple(position_risk(p.ticket, p.symbol, p.direction, p.volume, p.price_open,
                                p.stop_loss if (p.stop_loss and p.stop_loss > 0) else soft.get(p.ticket), specs.get(p.symbol),
                                p.take_profit, (p.profit or 0.0) + (p.swap or 0.0)) for p in positions)
    problems = [history_error] if history_error else []
    floating = None
    missing_profit = [p for p in positions if p.profit is None]
    if missing_profit:
        problems.append("MT5 sent no profit figure for open position " + ", ".join(f"#{p.ticket}" for p in missing_profit)
                        + ", so the floating result is unknown.")
    else:
        floating = sum((p.profit or 0.0) + (p.swap or 0.0) for p in positions)
    closed = None
    start = None
    if closed_trades is not None and day_start is not None:
        closed_trades = list(closed_trades)
        undated = [t for t in closed_trades if t.time_close is None]
        todays = [t for t in closed_trades if t.time_close is not None and t.time_close >= day_start]
        no_result = [t for t in todays if t.net is None]
        cut_short = (history_limit is not None and len(closed_trades) >= history_limit
                     and min((t.time_close for t in closed_trades if t.time_close is not None), default=day_start) >= day_start)
        if undated:
            problems.append(f"{len(undated)} closed trade(s) in MT5's history have no close time, so they cannot be placed in a trading day.")
        if no_result:
            problems.append("Closed trade " + ", ".join(f"#{t.ticket}" for t in no_result[:5]) + " today has no result in MT5's history.")
        if cut_short:
            problems.append(f"MT5's history stopped at its {history_limit}-trade limit before reaching the start of the trading day, "
                            "so today's closed trades may be incomplete.")
        if not (undated or no_result or cut_short):
            closed = sum(t.net for t in todays)
            if account.balance is not None:
                start = account.balance - closed
    return AccountRiskState(equity=account.equity, balance=account.balance, currency=account.currency,
                            start_of_day_balance=start, todays_closed_pl=closed, floating_pl=floating,
                            positions=risks, day_start_source=day_source, history_error=" ".join(problems),
                            day_start=day_start, server_offset_seconds=server_offset, next_reset_utc=next_reset)


def resolve_server_clock(adapter, now_utc: float | None = None, resolver=None, first=()) -> brokertime.ServerClock:
    """The broker's clock (see trademate.brokertime). Probes the symbols in `first`, then the Market Watch symbols."""
    now_utc = time.time() if now_utc is None else now_utc

    def names():
        out = list(first)
        out += [n for n in (s.name for s in _safe_symbols(adapter)) if n not in out]
        return out

    return (resolver or brokertime.DEFAULT_RESOLVER).resolve(adapter, names, now_utc)


def gather_state(adapter, now_utc: float | None = None, resolver=None) -> AccountRiskState:
    """Read everything from MT5 through the BrokerAdapter. Raises BrokerError only if the account itself is unavailable."""
    now_utc = time.time() if now_utc is None else now_utc
    # Account and positions are separate reads of MT5's live snapshot. Read positions, account, positions again, and only
    # accept them when the position list did not change in between, so equity and the positions belong to one moment.
    key = lambda ps: sorted((p.ticket, p.volume, p.stop_loss, p.take_profit) for p in ps)
    for _attempt in range(3):
        positions = adapter.get_positions()
        account = adapter.get_account()
        if key(adapter.get_positions()) == key(positions):
            break
    else:
        raise BrokerUnavailableError("MT5 positions kept changing while the risk state was read. Try again.")
    specs = {}
    for name in sorted({p.symbol for p in positions}):
        try:
            specs[name] = to_spec(adapter.get_symbol_spec(name))
        except (BrokerError, NotImplementedError) as exc:
            logger.warning("No tick data for open position symbol %s: %s", name, exc)

    clock = resolve_server_clock(adapter, now_utc, resolver, first=sorted(specs))
    if not clock.known:
        logger.warning("Daily loss unknown: %s", clock.detail)
        return build_state(account, positions, specs, None, None, "unknown", "Broker timestamps cannot be converted to local time: " + clock.detail, software_stops=_software_stops())
    try:
        tz = localday.resolve_tz(_LOCAL_TZ_NAME["name"])
    except localday.TimezoneProblem as exc:
        logger.warning("Daily loss unknown: %s", exc)
        return build_state(account, positions, specs, None, None, "unknown", str(exc), software_stops=_software_stops())
    # The day is the computer's LOCAL day. Its start is computed in UTC, then expressed on the broker's clock so it can be compared with MT5's timestamps.
    day_start_utc = localday.local_day_start_utc(now_utc, tz)
    day_start = int(round(day_start_utc + clock.offset_seconds))
    history, error = None, ""
    try:
        history = adapter.get_history(days=HISTORY_DAYS, limit=HISTORY_LIMIT)
    except (BrokerError, NotImplementedError) as exc:
        error = str(exc) or "Trade history is not supported by this adapter."
    label = getattr(tz, "key", None) or str(tz)
    return build_state(account, positions, specs, history, day_start, f"local midnight ({label}), broker clock via {clock.source}: {clock.detail}", error,
                       history_limit=HISTORY_LIMIT, server_offset=clock.offset_seconds,
                       next_reset=int(round(localday.next_local_midnight_utc(now_utc, tz))), software_stops=_software_stops())


def _safe_symbols(adapter):
    try:
        return adapter.get_symbols()
    except BrokerError:
        return []
