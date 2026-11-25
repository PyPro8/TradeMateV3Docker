"""RAPID FIRE engine.

One thread, one lock, one `step()`. Everything that can be tested is in `step()`; the thread only calls it.

ENTRY (M1 only)   price (bid) is compared with a reference: the OPEN of the forming M1 candle for the first shot, the previous shot's EXIT price for
                  the next one (so every shot needs a fresh move). `trigger_points` above it -> BUY, below it -> SELL (platform points from the
                  broker's own symbol data). At most `max_entries_per_candle` shots per candle, one position at a time, never
                  before the first NEW candle after Start. The shot is reserved in the ledger (on disk) BEFORE the order is sent.
EXIT              no broker-side stop-loss and no broker-side take-profit. The engine watches the executable exit price (bid for a BUY, ask for a SELL)
                  and, when it is `target_points` from the broker-confirmed fill, sends ONE close request through the journaled adapter. Closure is
                  confirmed from the account. The realised result is read from MT5's own history, never computed.
EMERGENCY         `emergency_points` against the fill closes the position (software-managed). Also the loss distance the Risk Engine uses.
TAKE PROFIT       either the fixed `target_points`, or (when `profit_per_fire` is set) a distance CALCULATED at entry from the broker's tick data and the lot
                  (target.py), frozen into the trade. It stays a software-managed level (no broker-side TP is sent).
ANTI-TWIN         optional, ON/OFF. The first confirmed ADVERSE MOVE of the position's executable exit price (antitwin_rule.py, mode ADVERSE_MOVE: one tick
                  worse than where it started) closes it, CONFIRMS it is closed, then opens the opposite side with the same volume in the SAME step and
                  links the two. The twin is watched from ITS OWN fill price (a chain, bounded by at_rule.MAX_CHAIN). The Emergency Stop lock and the
                  emergency exit always win. While a position is held with Anti-Twin ON the engine steps every FAST_STEP_SECONDS and the blocking M1
                  candle fetch runs AFTER the decisions of the step, so it can never delay a reversal.

Limits of software-managed exits are documented in docs/RAPID_FIRE.md: with TradeMate, the bridge or the terminal down, nothing closes the position.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from trademate.broker.base import ConnectionState
from trademate.errors import BrokerError
from trademate.lotsize import validate_for_info
from trademate import antitwin_rule as at_rule
from trademate.rapid_fire import metrics, target as rf_target
from trademate.rapid_fire.executor import EXECUTED, NOT_EXECUTED, REFUSED, UNKNOWN, Executor, Outcome, Preflight
from trademate.rapid_fire.models import (MAX_ANTI_TWIN_DEPTH, PHILOSOPHY, RFConfig, STRATEGY_ID, STRATEGY_NAME, TIMEFRAME, VERSION, anti_twin_state,
                                         parse, readiness)
from trademate.rapid_fire import store as S

logger = logging.getLogger("trademate.rapid_fire")

STEP_SECONDS = 0.25
FAST_STEP_SECONDS = 0.05             # the step period while a position is held and Anti-Twin is ON (a snapshot read costs about a millisecond)
QUOTE_MAX_AGE = 5.0
SPEC_TTL = 10.0
CANDLE_POLL = 1.0
CANDLE_STALE_SECONDS = 120          # a forming M1 candle older than this (broker clock known) means the feed is stale
MAX_CLOSE_ATTEMPTS = 5
CLOSE_RETRY_SECONDS = 1.0
ENTRY_COOLDOWN = 2.0
ENTRY_QUOTE_MAX_AGE = 2.0           # an ENTRY is decided only on a quote at most this old (positions keep being managed on QUOTE_MAX_AGE): a 5 s old bid is not a signal
ENTRY_CANDLE_MAX_AGE = 0.3          # the M1 candle (time, open) behind an entry is re-read when its copy is older than this, so a candle that just rolled over is never used
PREFLIGHT_RETRY_SECONDS = 1.0       # a transient Anti-Twin pre-flight failure is asked again after this long (nothing was closed)
MAX_FAILURES_PER_CANDLE = 3
TWIN_ATTEMPTS = 3
TWIN_DEADLINE = 15.0                # an Anti-Twin that cannot be opened within this many seconds of the original's closure is abandoned
ABSENT_CONFIRM = 1.0                # a position must be missing from the account for this long before it counts as closed (snapshot glitches)
RESULT_POLL = 2.0
EPS = 1e-9


@dataclass
class Market:
    ok: bool = False
    problem: str = "Not read yet."
    info: object | None = None
    symbol: str = ""
    bid: float | None = None
    ask: float | None = None
    candle_time: int | None = None
    candle_open: float | None = None
    currency: str = ""
    quote_ok: bool = False
    quote_age: float | None = None      # age of the bridge snapshot the quote came from, when it was read
    trade_ok: bool = False              # connected, specification complete, instrument tradable and a fresh quote: everything an order needs EXCEPT the M1 candle


class RapidFireEngine:
    def __init__(self, adapter, config_store, ledger, executor: Executor, guard=None, server_offset=None, event_path: Path | None = None, clock=time.time):
        self.adapter, self.config_store, self.ledger, self.executor = adapter, config_store, ledger, executor
        self.guard = guard or (lambda: None)                 # -> reason string when trading is not permitted at all
        self.server_offset = server_offset or (lambda: None)  # -> broker clock minus UTC (seconds) or None
        self.clock = clock
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False
        self._arm_after: int | None = None
        self._market = Market()
        self._spec_at = 0.0
        self._candle_at = 0.0
        self._candle: tuple | None = None
        self._account = (0.0, "")
        self._positions: dict = {}
        self._pos_problem = ""
        self._absent: dict = {}
        self._next_close: dict = {}
        self._next_entry_at = 0.0
        self._next_result_at = 0.0
        self._block_reason = ""
        self._last_execution: dict | None = None
        self._last_error = ""
        self._events: deque = deque(maxlen=200)
        self._event_path = Path(event_path) if event_path else None
        self._last_step = 0.0
        self._candle_pending = False                         # a candle fetch deferred to the end of the step (position held, Anti-Twin ON)
        self._lat: dict = {}                                 # original trade id -> monotonic stamps of the reversal in progress
        self.latency_log: deque = deque(maxlen=200)          # finished reversals: the MEASURED latency (see at_rule.latency_summary)

    # ------------------------------------------------------------------ configuration
    @property
    def config(self) -> RFConfig:
        return self.config_store.load()

    def save_config(self, data: dict) -> tuple[RFConfig | None, list[str]]:
        with self._lock:
            cfg, problems = parse(data, self.config)
            if cfg is None:
                return None, problems
            problems = self.config_store.save(cfg)
            return (None, problems) if problems else (cfg, [])

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> tuple[bool, str]:
        with self._lock:
            cfg = self.config
            missing = readiness(cfg)
            if missing:
                return False, "Rapid Fire cannot start: " + " ".join(missing)
            why = self.guard()
            if why:
                return False, why
            self._refresh_market(self.clock(), force=True)
            m = self._market
            if not m.ok:
                return False, "Rapid Fire cannot start: " + m.problem
            self.running = True
            self._arm_after = m.candle_time          # no entry in the candle that is already running
            self._event("started", symbol=m.symbol, armed_after_candle=m.candle_time)
        self.ensure_thread()
        return True, "Rapid Fire started. It will take its first shot in the next new M1 candle."

    def stop(self) -> str:
        with self._lock:
            self.running = False
            self._event("stopped")
            live = [t for t in self.ledger.live() if t["status"] in S.HOLDING]
        if live:
            return (f"Rapid Fire stopped: no new entries. {len(live)} open position(s) stay under software management until they hit their target "
                    "or emergency level.")
        return "Rapid Fire stopped."

    def ensure_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="rapid-fire", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        idle = 0
        while not self._stop.is_set():
            try:
                self.step()
                self._last_error = ""
            except Exception as exc:                          # noqa: BLE001 - never let the loop die; say so
                logger.exception("Rapid Fire step failed")
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._stop.wait(1.0)
            with self._lock:
                busy = self.running or self.ledger.has_live()
            idle = 0 if busy else idle + 1
            if idle > 20:                                     # nothing to do for a while: the thread ends, start()/boot() restarts it
                return
            self._stop.wait(self._step_period())

    def _step_period(self) -> float:
        """Fast while a position is held and Anti-Twin is ON (every quote matters), the normal period otherwise."""
        try:
            if self.config.anti_twin_enabled and self.ledger.has_live():
                return FAST_STEP_SECONDS
        except Exception:                                    # noqa: BLE001 - never let the cadence decision stop the loop
            pass
        return STEP_SECONDS

    # ------------------------------------------------------------------ the one step
    def step(self, now: float | None = None) -> None:
        with self._lock:
            now = self.clock() if now is None else now
            self._last_step = now
            self._refresh_market(now)
            self._manage(now)
            self._open_pending_twins(now)
            if self.running:
                self._maybe_enter(now)
            self._finalise(now)
            self._fetch_candle_deferred(now)

    # ------------------------------------------------------------------ market reading
    def _refresh_market(self, now: float, force: bool = False) -> None:
        cfg = self.config
        m = Market(symbol=cfg.symbol, problem="")
        try:
            st = self.adapter.get_connection_status()
            if st.state != ConnectionState.CONNECTED:
                m.problem = f"MT5 is not connected ({st.state.value}). {st.message}".strip()
                self._market = m
                return
            if force or now - self._spec_at >= SPEC_TTL or self._market.info is None or self._market.symbol != cfg.symbol:
                info = self.adapter.get_symbol_spec(cfg.symbol)
                self._spec_info, self._spec_at = info, now
            info = getattr(self, "_spec_info", None)
            if info is None or getattr(info, "name", "").upper() != cfg.symbol.upper():
                m.problem = (f"The broker's instrument '{getattr(info, 'name', '?')}' is not '{cfg.symbol}'. Rapid Fire never substitutes another instrument.")
                self._market = m
                return
            m.info, m.symbol = info, info.name
            bad = [n for n in ("point", "tick_size", "tick_value", "volume_min", "volume_max", "volume_step")
                   if not (isinstance(getattr(info, n, None), (int, float)) and getattr(info, n) > 0)]
            if bad:
                m.problem = f"The broker's symbol data for {info.name} is incomplete ({', '.join(bad)}), so nothing can be calculated or traded."
                self._market = m
                return
            if info.trade_mode is not None and info.trade_mode not in (4,):
                m.problem = f"{info.name} is not open for full trading right now (trade mode {info.trade_mode})."
            q = self.adapter.get_quote(info.name)
            if q.bid and q.ask and q.bid > 0 and q.ask > 0 and q.age_seconds is not None and q.age_seconds <= QUOTE_MAX_AGE:
                m.bid, m.ask, m.quote_ok, m.quote_age = q.bid, q.ask, True, q.age_seconds
            else:
                m.problem = m.problem or "The live quote is missing or stale."
            m.trade_ok = m.quote_ok and not m.problem
            if now - self._candle_at >= CANDLE_POLL or force or self._candle is None:
                if not force and self._candle is not None and self._hold_with_anti_twin():
                    self._candle_pending = True              # a position is held with Anti-Twin ON: decide first, fetch the candle at the end of this step
                else:
                    problem = self._fetch_candle(info.name, now)
                    if problem:
                        m.problem = m.problem or problem
            if self._candle and now - self._candle[2] <= 5.0:
                m.candle_time, m.candle_open = self._candle[0], self._candle[1]
                off = self.server_offset()
                if off is not None and (now + off) - m.candle_time >= CANDLE_STALE_SECONDS:
                    m.candle_time = m.candle_open = None
                    m.problem = m.problem or "The M1 candle feed is stale (the newest candle is older than two minutes)."
            elif not m.problem:
                m.problem = "No current M1 candle."
            try:
                if now - self._account[0] > 10 or not self._account[1]:
                    self._account = (now, self.adapter.get_account().currency or "")
            except BrokerError:
                pass
            m.currency = self._account[1]
            m.ok = m.quote_ok and m.candle_time is not None and not m.problem
            if m.ok:
                m.problem = ""
        except BrokerError as exc:
            m.problem = str(exc) or "MT5 data is not available."
        except NotImplementedError as exc:
            m.problem = str(exc) or "This adapter does not support Rapid Fire."
        self._market = m

    def _hold_with_anti_twin(self) -> bool:
        try:
            return bool(self.config.anti_twin_enabled and self.ledger.has_live())
        except Exception:                                    # noqa: BLE001
            return False

    def _fetch_candle(self, symbol: str, now: float) -> str:
        """Read the forming M1 candle (a blocking round trip to the EA). Returns a problem text, or '' when the cache is still good / was refreshed."""
        self._candle_at = now
        try:
            bars = sorted(self.adapter.get_candles(symbol, TIMEFRAME, 3), key=lambda c: c.time)
            if bars:
                self._candle = (bars[-1].time, bars[-1].open, now)
        except BrokerError as exc:
            if not self._candle or now - self._candle[2] > 5:
                return f"M1 candles are unavailable: {exc}"
        return ""

    def _fetch_candle_deferred(self, now: float) -> None:
        if not self._candle_pending:
            return
        self._candle_pending = False
        m = self._market
        if m.symbol:
            self._fetch_candle(m.symbol, now)

    # ------------------------------------------------------------------ position management
    def _manage(self, now: float) -> None:
        try:
            positions = {p.ticket: p for p in self.adapter.get_positions()}
            self._pos_problem = ""
        except BrokerError as exc:
            self._pos_problem = str(exc) or "Positions are unavailable."
            return
        self._positions = positions
        for t in self.ledger.live():
            st = t["status"]
            if st == S.SUBMITTING:
                self.ledger.update(t["id"], status=S.ENTRY_UNCERTAIN, message="TradeMate stopped while this order was being sent. Check MT5, then resolve it.")
                self._event("entry_uncertain", id=t["id"], reason="restart_during_submit")
            elif st == S.ENTRY_UNCERTAIN:
                self._settle_entry(t, positions, now)
            elif st in (S.OPEN, S.CLOSE_REQUESTED, S.CLOSE_UNCERTAIN):
                self._manage_position(t, positions, now)

    def _settle_entry(self, t, positions, now) -> None:
        eid = t.get("exec_id_open")
        rec = self.executor.reconcile(eid) if eid else None
        if rec and rec.get("state") == "reconciled_not_executed":
            self._fail_entry(t, rec.get("message") or "Reconciled: the order did not execute.")
            if t.get("role") == "anti_twin" and t.get("twin_of"):
                self.ledger.update(t["twin_of"], twin_state="TWIN_FAILED", twin_message="REVERSAL FAILED: the opposite order did NOT execute (reconciled with the account). The original is closed; nothing is open.")
                self._event("twin_failed", original=t["twin_of"], reason="reconciled_not_executed")
        elif rec and rec.get("state") == "reconciled_executed" and rec.get("ticket") in positions:
            pos = positions[rec["ticket"]]
            self.ledger.update(t["id"], status=S.OPEN, ticket=rec["ticket"], volume=pos.volume, message="Reconciled: the order executed.")
            self._arm_levels(self.ledger.get(t["id"]), pos)
            if t.get("role") == "anti_twin" and t.get("twin_of"):   # the unconfirmed opposite order is now PROVEN on the account: only now is the reversal reported as done
                ok = pos.direction == t["side"] and pos.symbol.upper() == t["symbol"].upper()
                self.ledger.update(t["twin_of"], twin_state="TWIN_OPEN" if ok else "TWIN_UNCERTAIN",
                                   twin_message=("Opposite position opened and verified in MT5 (reconciled)." if ok else "MT5 shows a position that does not match the opposite order. Check MT5."))
                self._event("twin_confirmed", original=t["twin_of"], ticket=rec["ticket"], verified=ok)

    def _fail_entry(self, t, message: str) -> None:
        self.ledger.update(t["id"], status=S.ENTRY_FAILED, message=message)
        if t.get("candle_key"):
            self.ledger.release_shot(t["candle_key"])

    @staticmethod
    def _at_fields(cfg, depth: int) -> dict:
        """Chain depth of this trade. Whether Anti-Twin acts is read LIVE from the settings (OFF means off, now); the entry reference is the MT5 fill."""
        return {"at_mode": at_rule.ACTIVE_MODE, "depth": depth, "at_rule": None}

    def _adverse_move(self, t, now) -> bool:
        """Anti-Twin: the first confirmed adverse move of this position's executable exit price (antitwin_rule.py). True = a close was requested.
        The guard (emergency lock) always wins."""
        if not self.config.anti_twin_enabled or t.get("depth", 0) >= at_rule.MAX_CHAIN or t["twin_state"] is not None or self.guard():
            return False
        m, entry = self._market, t.get("entry_price")
        if not (m.quote_ok and entry and m.info is not None):
            return False
        if now < t.get("next_pf_at", 0.0):                        # a transient pre-flight failure is being waited out
            return False
        st = t.get("at_rule") or at_rule.new_state(t["side"], entry)
        if st.get("entry") != float(entry):                       # a stale reference (another position's) can never trigger this one
            st = at_rule.new_state(t["side"], entry)
        before = dict(st)
        fire = at_rule.observe(st, m.bid, m.ask, m.info.tick_size)
        if fire:
            pf = self._reversal_preflight(t)
            if pf is not None:                                    # the opposite order could not be sent: close NOTHING, report why, leave the original under its emergency exit
                self._event("anti_twin_preflight_failed", id=t["id"], ticket=t["ticket"], stage=pf.stage, retryable=pf.retryable, message=pf.message)
                if pf.retryable:
                    st["triggered"] = False
                    st.pop("trigger", None)
                    self.ledger.update(t["id"], at_rule=st, next_pf_at=now + PREFLIGHT_RETRY_SECONDS, twin_message="Reversal postponed, nothing was closed: " + pf.message)
                else:
                    self.ledger.update(t["id"], at_rule=st, twin_state="TWIN_ABORTED",
                                       twin_message="REVERSAL NOT STARTED: " + pf.message + " The original position was NOT closed; it stays open under its emergency exit.")
                return False
        if st != before:
            self.ledger.update(t["id"], at_rule=st)
        if fire:
            self._lat[t["id"]] = {"detect": time.perf_counter(), "quote_age": m.quote_age, "trigger": dict(st.get("trigger") or {})}
            self._event("anti_twin_trigger", id=t["id"], ticket=t["ticket"], entry=entry, bid=m.bid, ask=m.ask, quote_age=m.quote_age, mode=at_rule.ACTIVE_MODE)
            self._request_close(t, "anti_twin", now)
            return True
        return False

    def _reversal_preflight(self, t):
        """Ask the executor, BEFORE the original is closed, whether the opposite order could be sent (volume rules, stop level, Risk Engine with the original treated as
        closed, loss protections). None = it may proceed (also when the executor has no pre-flight). Read-only."""
        fn = getattr(self.executor, "preflight_open", None)
        m = self._market
        if fn is None or m.info is None:
            return None
        side = "sell" if t["side"] == "buy" else "buy"
        vol = t.get("volume") or t.get("lot")
        em = t.get("emergency_points")
        if not (m.quote_ok and vol and em):
            return Preflight(False, "No fresh quote or no emergency distance to price the opposite order.", "connection", retryable=True)
        point = m.info.point
        ref = (m.ask - em * point) if side == "buy" else (m.bid + em * point)
        try:
            pf = fn(m.symbol, side, vol, ref, exclude_tickets=(t["ticket"],), closing_at_loss=True)
        except Exception:                                         # noqa: BLE001 - never let a pre-flight bug stop the step; it is a transient wait
            logger.exception("Rapid Fire: the Anti-Twin pre-flight crashed")
            return Preflight(False, "The pre-flight check could not be completed.", "connection", retryable=True)
        return None if pf.ok else pf

    def _arm_levels(self, t, pos) -> dict:
        """Fix the target / emergency / twin price levels from the broker-confirmed FILL and the point size, once."""
        fill = pos.price_open
        point = t.get("point")
        if not (fill and point):
            return t
        sign = 1 if t["side"] == "buy" else -1
        f = {"entry_price": fill, "target_price": fill + sign * t["target_points"] * point,
             "emergency_price": None if not t.get("emergency_points") else fill - sign * t["emergency_points"] * point,
             "twin_price": None if not t.get("twin_points") else fill - sign * t["twin_points"] * point}
        if t.get("entry_time") is None:
            f["entry_time"] = self._last_step
        return self.ledger.update(t["id"], **f)

    def _manage_position(self, t, positions, now) -> None:
        pos = positions.get(t["ticket"])
        tid = t["id"]
        if pos is None:
            first = self._absent.setdefault(tid, now)
            if now - first >= ABSENT_CONFIRM:
                self._mark_closed(t, now, t.get("close_reason") or "external")
            return
        self._absent.pop(tid, None)
        if pos.volume and abs(pos.volume - (t.get("volume") or 0.0)) > 1e-12 and t["status"] == S.OPEN:      # the ACTUAL filled volume is what a reversal mirrors
            t = self.ledger.update(tid, volume=pos.volume)
        if t["status"] == S.CLOSE_UNCERTAIN:
            self._settle_close(t, now)
            return
        if t.get("target_price") is None:
            t = self._arm_levels(t, pos)
            if t.get("target_price") is None:
                return
        m = self._market
        if not m.quote_ok:
            return                                           # cannot judge without a fresh quote; reported as a warning
        px = m.bid if t["side"] == "buy" else m.ask
        buy = t["side"] == "buy"
        tp = px >= t["target_price"] - EPS if buy else px <= t["target_price"] + EPS
        twin = (t.get("twin_price") is not None and t["role"] == "original" and t["twin_state"] in (None, "CLOSING_ORIGINAL")
                and (px <= t["twin_price"] + EPS if buy else px >= t["twin_price"] - EPS))
        emergency = (t.get("emergency_price") is not None and (px <= t["emergency_price"] + EPS if buy else px >= t["emergency_price"] - EPS))
        if t["twin_state"] == "CLOSING_ORIGINAL":
            self._request_close(t, "anti_twin", now)           # resume an interrupted reversal
        elif tp:
            self._request_close(t, "take_profit", now)
        elif twin:                                           # LEGACY adverse-move level of a trade opened by an older version
            self._request_close(t, "anti_twin", now)
        elif emergency:                                      # the emergency exit outranks a reversal
            self._request_close(t, "emergency", now)
        else:
            self._adverse_move(t, now)

    def _request_close(self, t, reason: str, now: float) -> None:
        tid = t["id"]
        if now < self._next_close.get(tid, 0.0):
            return
        if t["close_attempts"] >= MAX_CLOSE_ATTEMPTS:
            self.ledger.update(tid, status=S.CLOSE_FAILED, message=f"{MAX_CLOSE_ATTEMPTS} close attempts failed. Close position #{t['ticket']} in MT5 yourself.")
            if t.get("twin_state") == "CLOSING_ORIGINAL":
                self.ledger.update(tid, twin_state="TWIN_FAILED", twin_message="REVERSAL FAILED: the close could not be completed; the original is still open and no opposite position was opened.")
            self._event("close_failed", id=tid, ticket=t["ticket"])
            return
        extra = {"twin_state": "CLOSING_ORIGINAL"} if reason == "anti_twin" else {}
        t = self.ledger.update(tid, status=S.CLOSE_REQUESTED, close_reason=reason, close_requested_at=now, close_attempts=t["close_attempts"] + 1, **extra)
        self._event("close_requested", id=tid, ticket=t["ticket"], reason=reason, attempt=t["close_attempts"])
        lat = self._lat.setdefault(tid, {"detect": time.perf_counter()}) if reason == "anti_twin" else None
        if lat is not None:
            lat["close_sent"] = time.perf_counter()
        out = self.executor.close(t["ticket"])
        if lat is not None:
            lat["close_done"] = time.perf_counter()
            lat["close_price"] = out.price
        self._last_execution = {"at": now, "action": f"close ({reason})", **out.to_dict()}
        if out.executed:
            self._mark_closed(t, now, reason, exit_hint=out.price)
        elif out.kind in (UNKNOWN, "unconfirmed"):
            self.ledger.update(tid, status=S.CLOSE_UNCERTAIN, exec_id_close=out.execution_id, message=out.message)
            self._event("close_uncertain", id=tid, message=out.message)
        else:
            self.ledger.update(tid, status=S.OPEN, message=out.message)
            self._next_close[tid] = now + CLOSE_RETRY_SECONDS
            self._event("close_not_executed", id=tid, message=out.message)

    def _settle_close(self, t, now) -> None:
        """Close sent but the position is still listed. Never re-send blindly: only a journal reconciliation that PROVES it did not happen re-opens the path."""
        rec = self.executor.reconcile(t["exec_id_close"]) if t.get("exec_id_close") else None
        if rec and rec.get("state") == "reconciled_not_executed":
            self.ledger.update(t["id"], status=S.OPEN, message="Close did not execute (reconciled); the position is open again.")
            self._next_close[t["id"]] = now + CLOSE_RETRY_SECONDS
        elif not t.get("exec_id_close") and now - (t.get("close_requested_at") or now) > 5:
            self.ledger.update(t["id"], status=S.OPEN)

    def _mark_closed(self, t, now, reason, exit_hint=None) -> None:
        f = {"status": S.CLOSED_AWAITING_RESULT, "close_reason": reason, "exit_time": now}
        if exit_hint:
            f["exit_price_hint"] = exit_hint
        if t["twin_state"] == "CLOSING_ORIGINAL":
            f["twin_state"] = "OPENING_TWIN"
        self.ledger.update(t["id"], **f)
        self._absent.pop(t["id"], None)
        m = self._market
        ref = exit_hint or ((m.bid if t["side"] == "buy" else m.ask) if m.quote_ok else None)
        if ref and m.candle_time:
            self.ledger.set_anchor(self.ledger.candle_key(m.symbol or t["symbol"], m.candle_time), m.candle_time, ref)   # the next shot needs a FRESH move from here
        self._event("closed", id=t["id"], ticket=t["ticket"], reason=reason)

    # ------------------------------------------------------------------ Anti-Twin: open the opposite side AFTER the original is confirmed closed
    def _open_pending_twins(self, now: float) -> None:
        for o in self.ledger.trades():
            if o["twin_state"] == "OPENING_TWIN" and o["status"] in (S.CLOSED_AWAITING_RESULT, S.CLOSED):
                self._open_twin(o, now)

    def _open_twin(self, o, now) -> None:
        oid = o["id"]
        if any(t.get("twin_of") == oid and t["status"] != S.ENTRY_FAILED for t in self.ledger.trades()):
            return                                            # one Anti-Twin per original: it exists (or is being settled)
        if o.get("depth", 0) >= at_rule.MAX_CHAIN:
            self.ledger.update(oid, twin_state="TWIN_FAILED", twin_message="Safety ceiling reached: no further reversal in this chain.")
            return
        if self.guard():
            self.ledger.update(oid, twin_state="TWIN_CANCELLED", twin_message="Trading is not permitted right now (Emergency Stop or a safety lock): the opposite position was NOT opened.")
            self._event("twin_cancelled", id=oid, reason="guard")
            return
        if not self.config.anti_twin_enabled:
            self.ledger.update(oid, twin_state="TWIN_CANCELLED", twin_message="Anti-Twin was switched off before the opposite position was opened: nothing was opened.")
            self._event("twin_cancelled", id=oid, reason="switched_off")
            return
        if now - (o.get("exit_time") or now) > TWIN_DEADLINE:
            self.ledger.update(oid, twin_state="TWIN_FAILED", twin_message="The Anti-Twin could not be opened in time and was abandoned (the original stays closed).")
            self._event("twin_abandoned", id=oid)
            return
        if now < o.get("next_twin_at", 0.0):
            return
        m = self._market
        if not m.trade_ok:                                    # a fresh quote and a tradable instrument are what an order needs; the M1 candle is not (a candle outage must never strand a reversal)
            return
        if o.get("twin_attempts", 0) >= TWIN_ATTEMPTS:
            self.ledger.update(oid, twin_state="TWIN_FAILED", twin_message="The Anti-Twin order failed repeatedly and was abandoned (the original stays closed).")
            return
        side = "sell" if o["side"] == "buy" else "buy"
        point = m.info.point
        ref = (m.ask - o["emergency_points"] * point) if side == "buy" else (m.bid + o["emergency_points"] * point)
        at_extra = {"at_mode": at_rule.ACTIVE_MODE, "depth": o.get("depth", 0) + 1, "at_rule": None}
        tgt = o["target_points"]                              # the twin earns the same target as the position it replaces (same lot, same money)
        twin = self.ledger.new_trade(symbol=m.symbol, timeframe=TIMEFRAME, side=side, volume=o["volume"], role="anti_twin", twin_of=oid, point=point,
                                     candle_time=m.candle_time, shot=None, target_points=tgt, emergency_points=o["emergency_points"],
                                     twin_points=None, trigger_points=o["trigger_points"], lot=o["volume"], spread_points=m.info.spread_points, **at_extra)
        self.ledger.update(oid, twin_id=twin["id"], twin_attempts=o.get("twin_attempts", 0) + 1)
        self._event("twin_submit", original=oid, twin=twin["id"], side=side, volume=o["volume"])
        lat = self._lat.setdefault(oid, {"detect": time.perf_counter()})
        lat["open_sent"] = time.perf_counter()
        out = self.executor.open(m.symbol, side, o["volume"], ref)
        lat["open_done"] = time.perf_counter()
        self._last_execution = {"at": now, "action": "anti-twin open", **out.to_dict()}
        if out.executed and out.position is not None and (out.position.direction != side or out.position.symbol.upper() != m.symbol.upper()):
            pos = out.position                                    # MT5 shows something other than what was ordered: never reported as a successful reversal
            self.ledger.update(twin["id"], status=S.ENTRY_UNCERTAIN, exec_id_open=out.execution_id, ticket=pos.ticket, message="MT5 shows a position that does not match the order.")
            self.ledger.update(oid, twin_state="TWIN_UNCERTAIN", twin_message=f"RECOVERY REQUIRED: MT5 shows #{pos.ticket} as {pos.direction} {pos.symbol}, not {side} {m.symbol}. Check MT5.")
            self._event("twin_mismatch", original=oid, ticket=pos.ticket, ordered=side, found=pos.direction)
        elif out.executed:
            pos = out.position
            ticket = out.ticket or (pos.ticket if pos else None)
            vol = pos.volume if pos is not None and pos.volume else o["volume"]
            twin = self.ledger.update(twin["id"], status=S.OPEN, ticket=ticket, volume=vol, exec_id_open=out.execution_id, message=out.message,
                                      partial=bool(vol + EPS < o["volume"]))
            self.ledger.update(oid, twin_state="TWIN_OPEN", twin_message=("Opposite position opened and verified in MT5." if not twin["partial"] else
                               f"Opposite position opened but only {vol:g} of {o['volume']:g} lots filled (partial fill). Not topped up."),
                               twin_ticket=ticket, twin_volume=vol, twin_fill=(pos.price_open if pos is not None else None))
            if pos is not None:
                self._arm_levels(twin, pos)
        elif out.uncertain:
            self.ledger.update(twin["id"], status=S.ENTRY_UNCERTAIN, exec_id_open=out.execution_id, message=out.message)
            self.ledger.update(oid, twin_state="TWIN_UNCERTAIN", twin_message="RECOVERY REQUIRED: the opposite order's outcome is not yet proven. It is NOT re-sent; "
                               "the account is being checked and the position is adopted only if MT5 shows it.")
        else:
            self.ledger.update(twin["id"], status=S.ENTRY_FAILED, message=out.message)
            if out.retryable:
                self.ledger.update(oid, twin_id=None, next_twin_at=now + 1.0, twin_message=out.message)
            else:
                self.ledger.update(oid, twin_state="TWIN_REJECTED" if out.kind == REFUSED else "TWIN_FAILED", twin_message="REVERSAL FAILED: " + out.message)
        self._event("twin_result", original=oid, kind=out.kind, message=out.message)
        if not (out.kind != EXECUTED and out.retryable):      # the reversal is over (opened, refused, failed or uncertain); a retryable failure is still in progress
            self._finish_latency(oid, o, out)

    def _finish_latency(self, oid: str, o: dict, out) -> None:
        """Turn the monotonic stamps of one reversal into milliseconds. MEASURED, never assumed (at_rule.latency_record)."""
        lat = self._lat.pop(oid, None)
        if not lat:
            return
        rec = at_rule.latency_record(lat, side=o["side"], entry=o.get("entry_price"), outcome=out.kind, leg=oid, ticket=o.get("ticket"), symbol=o.get("symbol"))
        rec["at"] = self.clock()
        self.latency_log.append(rec)
        self._event("anti_twin_latency", **{k: v for k, v in rec.items() if k not in ("leg", "symbol")}, id=oid)

    # ------------------------------------------------------------------ entries
    def _entry_block(self, now: float, cfg: RFConfig, m: Market) -> str:
        why = self.guard()
        if why:
            return why
        missing = readiness(cfg)
        if missing:
            return " ".join(missing)
        if not m.ok:
            return m.problem
        if self._pos_problem:
            return "Positions are unavailable: " + self._pos_problem
        if self._arm_after is not None and m.candle_time <= self._arm_after:
            return "Waiting for the next new M1 candle after Start."
        if self.ledger.has_live() or any(o["twin_state"] == "OPENING_TWIN" for o in self.ledger.trades()):
            return "A Rapid Fire position (or its settlement) is still open: one at a time."
        if now < self._next_entry_at:
            return "Cooling down after a failed order."
        key = self.ledger.candle_key(m.symbol, m.candle_time)
        if self.ledger.failures(key) >= MAX_FAILURES_PER_CANDLE:
            return f"{MAX_FAILURES_PER_CANDLE} orders failed in this candle; waiting for the next one."
        if self.ledger.shots(key) >= cfg.max_entries_per_candle:
            return "No entries left in this candle."
        chk = validate_for_info(cfg.lot, m.info)
        if not chk.ok:
            return chk.message
        tp = self._target_plan(cfg, m)
        if not tp["ok"]:
            return "Take profit cannot be calculated: " + tp["problem"]
        if cfg.max_spread_points is not None and m.info.spread_points is not None and m.info.spread_points > cfg.max_spread_points:
            return f"Spread {m.info.spread_points} points is above the {cfg.max_spread_points:g} limit."
        return ""

    def _target_plan(self, cfg: RFConfig, m: Market) -> dict:
        """The take-profit distance for a NEW trade. Money mode: calculated from the broker's data (never guessed). Fixed mode: the configured points, as before."""
        if cfg.profit_per_fire is None:
            if m.info is not None and getattr(m.info, "point", None):
                dist = cfg.target_points * m.info.point
                return {"ok": True, "problem": "", "mode": "points", "required_points": cfg.target_points, "required_price_distance": dist, "profit_per_fire": None,
                        "currency": m.currency or None, "lot": cfg.lot}
            return {"ok": True, "problem": "", "mode": "points", "required_points": cfg.target_points, "profit_per_fire": None}
        p = rf_target.plan(m.info, cfg.lot, cfg.profit_per_fire, cfg.commission_per_lot, m.bid, m.ask, m.currency)
        p["mode"] = "money"
        return p

    def _entry_inputs_stale(self, now: float, m: Market) -> str:
        """Last check before an order is reserved: is the quote fresh, and is the M1 candle (time, open) the trigger was measured from STILL the forming candle?
        The candle copy can be up to CANDLE_POLL old; for the first moments of a new minute that made the previous candle's open the reference and produced an entry in the
        previous candle's direction. The candle is re-read when its copy is older than ENTRY_CANDLE_MAX_AGE; if it changed, nothing is sent this step."""
        if m.quote_age is not None and m.quote_age > ENTRY_QUOTE_MAX_AGE:
            return f"The quote is {m.quote_age:.1f}s old: an entry is decided only on a fresh quote."
        if self._candle is not None and now - self._candle[2] > ENTRY_CANDLE_MAX_AGE:
            before = (m.candle_time, m.candle_open)
            problem = self._fetch_candle(m.symbol, now)
            if problem:
                return problem
            c = self._candle
            if c is not None and (c[0], c[1]) != before:
                m.candle_time, m.candle_open = c[0], c[1]
                self._event("entry_deferred_candle_changed", was=before[0], now_candle=c[0])
                return "The M1 candle changed while the signal was being judged: it is judged again on the new candle."
        return ""

    def _maybe_enter(self, now: float) -> None:
        cfg, m = self.config, self._market
        self._block_reason = self._entry_block(now, cfg, m)
        if self._block_reason:
            return
        point = m.info.point
        anchor = self.ledger.anchor(self.ledger.candle_key(m.symbol, m.candle_time))
        reference = anchor if anchor is not None else m.candle_open
        moved = m.bid - reference
        if moved >= cfg.trigger_points * point - EPS:
            side = "buy"
        elif -moved >= cfg.trigger_points * point - EPS:
            side = "sell"
        else:
            self._block_reason = f"Waiting for a {cfg.trigger_points:g}-point move away from the {'last exit price' if anchor is not None else 'M1 open'}."
            return
        stale = self._entry_inputs_stale(now, m)
        if stale:                                              # the signal was judged on a quote / candle copy that may no longer be true: decide again on fresh data
            self._block_reason = stale
            return
        key = self.ledger.candle_key(m.symbol, m.candle_time)
        if not self.ledger.reserve_shot(key, m.candle_time, cfg.max_entries_per_candle):
            return
        shot = self.ledger.shots(key)
        plan = self._target_plan(cfg, m)                      # validated in _entry_block; frozen into the trade so a later settings change never alters it
        t = self.ledger.new_trade(symbol=m.symbol, timeframe=TIMEFRAME, side=side, volume=cfg.lot, role="original", candle_time=m.candle_time, candle_key=key,
                                  shot=shot, point=point, trigger_points=cfg.trigger_points, target_points=plan["required_points"], lot=cfg.lot,
                                  profit_per_fire=cfg.profit_per_fire, tp_mode=plan["mode"], tp_commission=plan.get("commission"),
                                  emergency_points=cfg.emergency_points, twin_points=None, spread_points=m.info.spread_points,
                                  **self._at_fields(cfg, 0), signal_price=m.bid, candle_open=m.candle_open, trigger_reference=reference)
        ref = (m.ask - cfg.emergency_points * point) if side == "buy" else (m.bid + cfg.emergency_points * point)
        self._event("entry_submit", id=t["id"], side=side, shot=shot, candle=m.candle_time, bid=m.bid)
        out = self.executor.open(m.symbol, side, cfg.lot, ref)
        self._last_execution = {"at": now, "action": f"{side} entry", **out.to_dict()}
        if out.executed and out.position is not None and out.position.direction != side:
            pos = out.position                                  # MT5 reports the opposite direction to the order: never silently managed as if it matched
            self.ledger.update(t["id"], status=S.ENTRY_UNCERTAIN, ticket=pos.ticket, exec_id_open=out.execution_id,
                               message=f"DIRECTION MISMATCH: ordered {side}, MT5 shows #{pos.ticket} as {pos.direction}. Check MT5 and resolve it.")
            self._event("entry_direction_mismatch", id=t["id"], ordered=side, found=pos.direction, ticket=pos.ticket)
        elif out.executed:
            pos = out.position
            ticket = out.ticket or (pos.ticket if pos else None)
            t = self.ledger.update(t["id"], status=S.OPEN, ticket=ticket, volume=(pos.volume if pos is not None and pos.volume else cfg.lot),
                                   exec_id_open=out.execution_id, message=out.message, entry_time=now)
            if pos is not None:
                self._arm_levels(t, pos)
        elif out.uncertain:
            self.ledger.update(t["id"], status=S.ENTRY_UNCERTAIN, exec_id_open=out.execution_id, message=out.message)
        else:
            self._fail_entry(t, out.message)
            self._next_entry_at = now + ENTRY_COOLDOWN
        self._event("entry_result", id=t["id"], kind=out.kind, message=out.message)

    # ------------------------------------------------------------------ realised results (from MT5's own history)
    def _finalise(self, now: float) -> None:
        waiting = self.ledger.trades((S.CLOSED_AWAITING_RESULT,))
        if not waiting or now < self._next_result_at:
            return
        self._next_result_at = now + RESULT_POLL
        try:
            history = {h.ticket: h for h in self.adapter.get_history(3, 500)}
        except (BrokerError, NotImplementedError):
            return
        for t in waiting:
            h = history.get(t["ticket"])
            if h is None or h.time_close is None:
                continue
            self.ledger.update(t["id"], status=S.CLOSED, exit_price=h.price_close, exit_time_broker=h.time_close, gross=h.profit, commission=h.commission,
                               swap=h.swap, net=h.net, broker_reason=h.reason, result_source="mt5_history")
            self._event("result", id=t["id"], net=h.net)

    # ------------------------------------------------------------------ manual resolution of an uncertain entry
    def resolve_entry(self, tid: str, outcome: str, ticket: int | None = None) -> tuple[bool, str]:
        with self._lock:
            t = self.ledger.get(tid)
            if not t or t["status"] != S.ENTRY_UNCERTAIN:
                return False, "That trade is not waiting to be resolved."
            if outcome == "not_executed":
                self._fail_entry(t, "You confirmed in MT5 that this order did not execute.")
                return True, "Marked as not executed. Its shot was released."
            if outcome == "executed":
                pos = self._positions.get(ticket) if ticket else None
                if pos is None:
                    return False, "Give the ticket of an open MT5 position."
                if pos.direction != t["side"] or pos.symbol.upper() != t["symbol"].upper():
                    return False, "That position is not the same instrument and direction."
                t = self.ledger.update(tid, status=S.OPEN, ticket=ticket, volume=pos.volume, message="You confirmed this position in MT5.")
                self._arm_levels(t, pos)
                return True, "Adopted: Rapid Fire now manages that position."
            return False, "Outcome must be 'executed' or 'not_executed'."

    # ------------------------------------------------------------------ risk-accounting hook (account_state)
    def software_stops(self) -> dict:
        """ticket -> software emergency price, for positions that have no broker-side stop. The Risk Engine uses it so these positions count
        toward active risk instead of making every later trade 'incalculable'."""
        out = {}
        for t in self.ledger.trades(S.HOLDING):
            if t.get("ticket") and t.get("emergency_price"):
                out[t["ticket"]] = t["emergency_price"]
        return out

    # ------------------------------------------------------------------ chart lines
    def chart_levels(self) -> dict:
        """The REAL exit levels of the open Rapid Fire positions, for the chart. Only positions that are on the MT5 account right now are listed, and only
        levels this engine actually manages (software take-profit / emergency exit). Nothing is invented; a closed position has no entry here."""
        with self._lock:
            info, cur, out = self._market.info, self._market.currency or None, []
            stale = bool(self._pos_problem)
            for t in self.ledger.trades(S.HOLDING):
                pos = self._positions.get(t.get("ticket")) if t.get("ticket") else None
                if pos is None or t.get("target_price") is None:
                    continue
                vol = pos.volume or t.get("volume")
                def lvl(price, points, loss):
                    if price is None:
                        return None
                    return {"price": price, "points": points, "money": metrics.estimate(info, vol, points, loss) if info is not None else None}
                out.append({"ticket": t["ticket"], "symbol": t["symbol"], "side": t["side"], "volume": vol, "role": t["role"], "entry": pos.price_open,
                            "tp": lvl(t.get("target_price"), t.get("target_points"), False), "sl": lvl(t.get("emergency_price"), t.get("emergency_points"), True),
                            "managed_by": "software", "currency": cur})
            return {"ok": True, "levels": out, "stale": stale, "note": "Rapid Fire exits are software-managed levels (no broker-side TP/SL); the lines show exactly those levels."}

    # ------------------------------------------------------------------ status for the UI / dashboard
    def _event(self, event: str, **f) -> None:
        ev = {"t": round(self._last_step or self.clock(), 3), "event": event, **f}
        self._events.append(ev)
        if self._event_path:
            try:
                self._event_path.parent.mkdir(parents=True, exist_ok=True)
                with self._event_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(ev) + "\n")
            except OSError:
                pass

    def events(self, limit: int = 50) -> list:
        return list(self._events)[-limit:]

    def status(self) -> dict:
        with self._lock:
            cfg, m = self.config, self._market
            trades = self.ledger.trades()
            holding = [t for t in trades if t["status"] in S.HOLDING]
            closed = [t for t in trades if t["status"] == S.CLOSED and t.get("net") is not None]
            wins = sum(1 for t in closed if t["net"] > 0)
            losses = sum(1 for t in closed if t["net"] < 0)
            floating = None
            if holding and all(t.get("ticket") in self._positions for t in holding):
                floating = sum((self._positions[t["ticket"]].profit or 0) + (self._positions[t["ticket"]].swap or 0) for t in holding)
            missing = readiness(cfg)
            if self.running:
                state, reason = ("RUNNING", self._block_reason or "Watching the M1 candle.") if m.ok else ("BLOCKED", m.problem)
            elif missing:
                state, reason = "NOT_CONFIGURED", " ".join(missing)
            else:
                state, reason = "STOPPED", "Not trading. Press Start."
            tplan = self._target_plan(cfg, m) if (cfg.profit_per_fire is None or cfg.lot) else {"ok": False, "mode": "money", "problem": "Set a lot size to calculate the take profit.", "required_points": None}
            at_status, at_msg = anti_twin_state(cfg)
            if at_status == "ARMED" and any(t["twin_state"] in ("CLOSING_ORIGINAL", "OPENING_TWIN") for t in trades):
                at_status = "ACTIVE"
            info = m.info
            key = self.ledger.candle_key(m.symbol or cfg.symbol, m.candle_time) if m.candle_time else None
            used = self.ledger.shots(key) if key else 0
            warnings = []
            if holding and not (self._thread and self._thread.is_alive()):
                warnings.append("Open Rapid Fire position(s) are NOT being managed: the engine thread is not running.")
            if holding and not m.quote_ok:
                warnings.append("No fresh quote: take-profit and emergency monitoring is paused.")
            if self._pos_problem:
                warnings.append("Positions unavailable: " + self._pos_problem)
            if any(t["status"] == S.CLOSE_FAILED for t in trades):
                warnings.append("A close request failed repeatedly. Close the position in MT5.")
            if any(t["status"] == S.ENTRY_UNCERTAIN for t in trades):
                warnings.append("An order's outcome is uncertain. New entries are blocked until it is resolved.")
            if any(t["status"] == S.CLOSED_AWAITING_RESULT and self._last_step - (t.get("exit_time") or 0) > 120 for t in trades):
                warnings.append("A closed trade's result is not in MT5's history yet.")
            if self._last_error:
                warnings.append("Engine error: " + self._last_error)
            return {
                "ok": True, "strategy": {"id": STRATEGY_ID, "name": STRATEGY_NAME, "philosophy": PHILOSOPHY, "version": VERSION, "independent": True},
                "running": self.running, "state": state, "state_reason": reason,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "config": cfg.to_dict(), "readiness": {"ready": not missing, "problems": missing},
                "instrument": {"broker_symbol": m.symbol or None, "available": bool(info), "point": getattr(info, "point", None),
                               "tick_size": getattr(info, "tick_size", None), "tick_value": getattr(info, "tick_value", None),
                               "volume_min": getattr(info, "volume_min", None), "volume_max": getattr(info, "volume_max", None),
                               "volume_step": getattr(info, "volume_step", None), "spread_points": getattr(info, "spread_points", None),
                               "bid": m.bid, "ask": m.ask, "currency": m.currency or None, "problem": m.problem or None,
                               "money_per_point_per_lot": metrics.money_per_point_per_lot(info) if info else None,
                               "estimated_profit_per_shot": metrics.estimate(info, cfg.lot, cfg.target_points) if info else None,
                               "estimated_loss_at_emergency": metrics.estimate(info, cfg.lot, cfg.emergency_points, True) if info and cfg.emergency_points else None,
                               "estimate_note": "Estimates from the broker's tick data (gross, before spread, commission, swap). Realised results come only from MT5 history."},
                "target_plan": tplan,
                "candle": {"time": m.candle_time, "open": m.candle_open, "reference": (self.ledger.anchor(key) if key and self.ledger.anchor(key) is not None else m.candle_open),
                           "reference_kind": ("last exit price" if key and self.ledger.anchor(key) is not None else "M1 open"), "shots_used": used, "max": cfg.max_entries_per_candle,
                           "shots_remaining": max(0, cfg.max_entries_per_candle - used)},
                "positions": [self._public(t) for t in holding],
                "anti_twin": {"status": at_status, "message": at_msg, "enabled": cfg.anti_twin_enabled, "threshold_points": cfg.anti_twin_adverse_points,
                              "max_chain": at_rule.MAX_CHAIN, "mode": at_rule.ACTIVE_MODE, "latency": at_rule.latency_summary(list(self.latency_log)),
                              "step_seconds": self._step_period()},
                "last_execution": self._last_execution,
                "pnl": {"realised_net": sum(t["net"] for t in closed) if closed else None, "floating": floating, "closed_count": len(closed),
                        "wins": wins, "losses": losses, "win_rate": (wins / len(closed)) if closed else None, "currency": m.currency or None,
                        "source": "MT5 deal history (broker-confirmed closures only)"},
                "warnings": warnings, "events": self.events(15),
            }

    @staticmethod
    def reversal_state(t) -> str:
        """The explicit reversal state of a trade (the names the specification uses), derived from its twin_state."""
        ts = t.get("twin_state")
        if t.get("role") == "anti_twin" and ts is None:
            return "ORIGINAL_POSITION_ACTIVE"
        return {None: "ORIGINAL_POSITION_ACTIVE", "CLOSING_ORIGINAL": "CLOSE_REQUESTED",
                "OPENING_TWIN": "OPPOSITE_ORDER_REQUESTED" if t.get("twin_id") else "CLOSE_CONFIRMED", "TWIN_OPEN": "OPPOSITE_ORDER_CONFIRMED",
                "TWIN_UNCERTAIN": "RECOVERY_REQUIRED", "TWIN_FAILED": "REVERSAL_FAILED", "TWIN_REJECTED": "REVERSAL_FAILED", "TWIN_ABORTED": "REVERSAL_ABORTED",
                "TWIN_CANCELLED": "REVERSAL_CANCELLED"}.get(ts, str(ts))

    @staticmethod
    def _public(t) -> dict:
        keys = ("id", "ticket", "symbol", "timeframe", "side", "volume", "role", "twin_of", "twin_id", "twin_state", "twin_message", "status", "entry_price",
                "target_price", "emergency_price", "twin_price", "shot", "candle_time", "close_reason", "message", "partial", "spread_points", "entry_time",
                "target_points", "emergency_points", "profit_per_fire", "tp_mode", "depth", "twin_ticket", "twin_volume", "twin_fill")
        return {**{k: t.get(k) for k in keys}, "reversal_state": RapidFireEngine.reversal_state(t)}

    def trade_rows(self, limit: int = 300) -> list[dict]:
        """Every Rapid Fire trade with its realised numbers (None until MT5's history has them)."""
        with self._lock:
            by_id = {t["id"]: t for t in self.ledger.trades()}
            rows = []
            for t in sorted(by_id.values(), key=lambda t: t["created_at"], reverse=True)[:limit]:
                rows.append({**self._public(t), "strategy": STRATEGY_NAME, "entry_time": t.get("entry_time"), "exit_price": t.get("exit_price"),
                             "exit_time_broker": t.get("exit_time_broker"), "gross": t.get("gross"), "commission": t.get("commission"), "swap": t.get("swap"),
                             "net": t.get("net"), "execution_status": t["status"], "closed_by": t.get("close_reason"),
                             "twin_of_ticket": by_id.get(t.get("twin_of"), {}).get("ticket") if t.get("twin_of") else None,
                             "twin_ticket": by_id.get(t.get("twin_id"), {}).get("ticket") if t.get("twin_id") else None,
                             "role_label": "ANTI-TWIN" if t["role"] == "anti_twin" else "ORIGINAL"})
            return rows

    def history_tag(self, ticket) -> dict | None:
        """The optional `rapid_fire` object /api/history adds to a closed trade that Rapid Fire opened."""
        t = self.ledger.by_ticket().get(ticket)
        if not t:
            return None
        other = self.ledger.get(t["twin_of"]) if t.get("twin_of") else (self.ledger.get(t["twin_id"]) if t.get("twin_id") else None)
        return {"strategy": STRATEGY_NAME, "timeframe": TIMEFRAME, "shot": t.get("shot"), "role": t["role"], "closed_by": t.get("close_reason"),
                "twin_of": (self.ledger.get(t["twin_of"]) or {}).get("ticket") if t.get("twin_of") else None,
                "twin_ticket": (other or {}).get("ticket") if t["role"] == "original" and t.get("twin_id") else None,
                "target_points": t.get("target_points"), "execution_status": t["status"]}
