"""Anti-Twin for Candle Momentum and Bull/Bear: a bounded, persisted, optional direction switch.   (docs/ANTI_TWIN.md)

WHAT IT DOES
  An ORIGINAL position is watched from its actual MT5 fill price (its entry reference) on EVERY quote. The first CONFIRMED ADVERSE MOVE of its
  executable exit price (one tick worse than where the position started: trademate/antitwin_rule.py, mode ADVERSE_MOVE) closes the position, the closure
  is CONFIRMED against the account, and, in the SAME step with no extra cycle in between, an EQUAL-VOLUME OPPOSITE position (the twin) is opened through
  the Risk gate. The twin is watched from ITS OWN fill price exactly the same way, so BUY -> SELL -> BUY -> SELL can continue (bounded by MAX_CHAIN).

SETTINGS  ON or OFF. Nothing else: no profit threshold, no minimum favourable move, no loss threshold, no candle close, no waiting for the take-profit.

SPEED
  step() is called by a fast lane (trademate/antitwin_lane.py) on every new quote and again by the engine cycle. Close -> confirm -> open runs in one
  call; every stage is timed with a monotonic clock (`latency` in snapshot()). Nothing is claimed to be instantaneous: the measured numbers are reported.

REPAIR v5.3 (reliable reversal; see docs/ANTI_TWIN.md "Reliable reversal")
  * PRE-FLIGHT: before the original is closed, the opposite order is checked (exact equal volume against the broker's min / max / step, trade mode, stop-level distance of the
    levels that will be sent, the Risk Engine with the original treated as closed, and the loss protections the close itself could trip). If it could not be opened, NOTHING is
    closed: the original stays open under its own stop-loss / take-profit and the reason is reported (state ABORTED_PREFLIGHT, or a short wait when the cause is transient).
  * An UNCONFIRMED / UNKNOWN close is never re-sent and never a dead end: the leg waits for the ACCOUNT to show the position gone, then opens the opposite order.
  * An UNCONFIRMED / UNKNOWN opposite order is never re-sent: it is reconciled against the account and adopted only when MT5 proves its ticket.
  * A reversal is reported as done (OPPOSITE_ORDER_CONFIRMED) only after the new position is verified in MT5: ticket, symbol, side, volume and fill price.
  * `reversal_state` carries the explicit state names: ORIGINAL_POSITION_ACTIVE, REVERSAL_TRIGGERED, CLOSE_REQUESTED, CLOSE_CONFIRMED, OPPOSITE_ORDER_REQUESTED,
    OPPOSITE_ORDER_CONFIRMED, REVERSAL_FAILED, RECOVERY_REQUIRED (plus REVERSAL_ABORTED / REVERSAL_CANCELLED when nothing was closed or the user's switch / lock intervened).
    A close that succeeded but whose opposite order failed is REVERSAL_FAILED: it is never presented as a switch.

WHAT IT NEVER DOES
  * trigger after the take-profit level was reached or the broker closed the position at TP / SL / by hand (those end the watch, no twin);
  * open the opposite trade before the original is confirmed closed;
  * open a second twin for the same leg or the same ticket (the claim is written to disk BEFORE the close is sent);
  * act while the Emergency Stop lock is on (checked at every step, before the close and again before the open);
  * bypass the Risk Engine: the twin is judged by the same central gate as every order;
  * blindly retry an uncertain close / open (that leg becomes UNCERTAIN and needs the user);
  * open a twin long after its original closed (TWIN_DEADLINE: e.g. the bridge was down), or when Anti-Twin was switched OFF in the meantime;
  * loop without end: MAX_CHAIN reversals per chain is a fixed safety ceiling (not a setting); the Risk Engine's own limits apply to every twin.

Twin levels (documented interpretation): the stop distance and the target distance are the ROOT original's (stop distance mirrored on the other side of
the twin's own entry; target = half of the root's target distance, so a long chain does not shrink).
"""

from __future__ import annotations

import collections
import logging
import math
import threading
import time

from trademate import antitwin_rule as rule
from trademate.rapid_fire.executor import EXECUTED, NOT_EXECUTED, REFUSED, UNCONFIRMED, UNKNOWN, Preflight
from trademate.rapid_fire.store import _atomic_write, _read

logger = logging.getLogger("trademate.bull_bear")

WATCHING, ARMED, CLOSING, AWAITING_CLOSE, OPENING = "WATCHING", "ARMED", "CLOSING", "AWAITING_CLOSE", "OPENING"
TWIN_OPENED, TP_HIT, SL_HIT, CLOSED_EXTERNAL, LIMIT_REACHED = "TWIN_OPENED", "TP_HIT", "SL_HIT", "CLOSED_EXTERNAL", "LIMIT_REACHED"
EMERGENCY_CANCELLED, REFUSED_ST, FAILED, UNCERTAIN, TP_TOUCHED = "EMERGENCY_CANCELLED", "REFUSED", "FAILED", "UNCERTAIN", "TP_TOUCHED"
SWITCHED_OFF, ABORTED_PREFLIGHT = "SWITCHED_OFF", "ABORTED_PREFLIGHT"
ACTIVE = (WATCHING, ARMED, CLOSING, AWAITING_CLOSE, OPENING)
TERMINAL = (TWIN_OPENED, TP_HIT, SL_HIT, CLOSED_EXTERNAL, LIMIT_REACHED, EMERGENCY_CANCELLED, REFUSED_ST, FAILED, UNCERTAIN, TP_TOUCHED, SWITCHED_OFF, ABORTED_PREFLIGHT)
MAX_ATTEMPTS = 3
MAX_CHAIN = rule.MAX_CHAIN              # fixed safety ceiling: reversals per chain (not a user setting)
CONFIRM_SECONDS = 15.0
GRACE_SECONDS = 5.0
TWIN_DEADLINE = 15.0                    # a twin is not opened more than this long after its original was confirmed closed (e.g. the bridge was down): stale, abandoned
PREFLIGHT_RETRY_SECONDS = 1.0           # a transient pre-flight failure (no data / connection) is looked at again after this long, not on every quote
RETRY_BACKOFF = 0.15                    # between two bounded retries of a provably-not-executed close / open (a retry is never faster than the bridge can answer)
KEEP_LEGS = 150                         # finished legs kept in the ledger: the file is rewritten on every transition, so it must stay small


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _resp(out) -> dict:
    """The complete execution response of one send, for the log (kind, message, ticket, price, execution id, retryable, Risk decision when there is one)."""
    try:
        d = out.to_dict()
    except Exception:                                                  # noqa: BLE001
        d = {"kind": getattr(out, "kind", None), "message": getattr(out, "message", None)}
    risk = getattr(out, "risk", None)
    if risk:
        d["risk"] = risk
    return d


def reversal_state(leg: dict) -> str:
    """The explicit reversal state of a leg (the names the specification uses), derived from the persisted state and the flags of the transaction."""
    st = leg.get("state")
    closed_known = bool(leg.get("closed_at")) or bool(leg.get("close_confirmed"))
    if st in (WATCHING, ARMED):
        return "ORIGINAL_POSITION_ACTIVE"
    if st == CLOSING:
        return "CLOSE_REQUESTED" if leg.get("close_sent") else "REVERSAL_TRIGGERED"
    if st == AWAITING_CLOSE:
        return "CLOSE_REQUESTED"
    if st == OPENING:
        return "OPPOSITE_ORDER_REQUESTED" if leg.get("open_sent") else "CLOSE_CONFIRMED"
    if st == TWIN_OPENED:
        return "OPPOSITE_ORDER_CONFIRMED"
    if st == UNCERTAIN:
        return "RECOVERY_REQUIRED"
    if st in (FAILED, REFUSED_ST):
        return "REVERSAL_FAILED" if closed_known else "REVERSAL_ABORTED"       # nothing was closed: the original is still open
    if st == ABORTED_PREFLIGHT:
        return "REVERSAL_ABORTED"
    if st in (EMERGENCY_CANCELLED, SWITCHED_OFF):
        return "REVERSAL_FAILED" if closed_known else "REVERSAL_CANCELLED"
    return {TP_HIT: "ORIGINAL_CLOSED_AT_TP", SL_HIT: "ORIGINAL_CLOSED_AT_SL", CLOSED_EXTERNAL: "ORIGINAL_CLOSED_EXTERNALLY", TP_TOUCHED: "ORIGINAL_TP_REACHED",
            LIMIT_REACHED: "REVERSAL_LIMIT_REACHED"}.get(st, str(st))


class AntiTwin:
    def __init__(self, executor, path=None, events=None, locked=lambda: False, clock=time.time, perf=time.perf_counter):
        self.executor, self.path, self.events, self._locked, self._clock = executor, path, events, locked, clock
        self._perf = perf                      # monotonic clock for LATENCY only (the injected `clock` may be a test's virtual clock)
        self._lock = threading.RLock()
        self._ctx: dict | None = None          # the quote / spec / fresh readers of the step in progress (only touched under the lock)
        self._lat: dict = {}                   # leg id -> monotonic stamps of the reversal in progress (not persisted)
        self._snap_cache: dict = {}
        self.latency_log: collections.deque = collections.deque(maxlen=200)   # finished reversals, newest last: the MEASURED latency
        self.legs: dict = {}
        self.chains: dict = {}                 # root leg id -> {"claimed": n, "max": n}
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        raw = _read(self.path, "anti-twin") if self.path else None
        if not raw:
            return
        self.legs, self.chains = raw.get("legs") or {}, raw.get("chains") or {}
        for leg in self.legs.values():                                # a restart in the middle of a send: the outcome is not provable -> never re-send
            if leg["state"] == CLOSING and leg.get("close_sent"):
                leg["state"], leg["reason"] = AWAITING_CLOSE, "Restarted while the close was in flight: confirming against the account."
                leg["state_at"] = self._clock()
            elif leg["state"] == OPENING and leg.get("open_sent"):               # the order may be live: reconcile it with the account, NEVER send it again
                leg["open_unconfirmed"], leg["open_sent_at"] = True, self._clock()
                leg["reason"], leg["state_at"] = "Restarted while the opposite order was in flight: checking the account for it (it is never re-sent).", self._clock()

    def _prune(self) -> None:
        """Keep the ledger small: drop the oldest FINISHED legs beyond KEEP_LEGS (never an active one) and the chains nothing refers to any more."""
        done = [l for l in self.legs.values() if l["state"] in TERMINAL]
        if len(done) <= KEEP_LEGS:
            return
        done.sort(key=lambda l: l.get("created_at", 0))
        for l in done[:len(done) - KEEP_LEGS]:
            self.legs.pop(l["id"], None)
        roots = {l["root"] for l in self.legs.values()}
        for k in [k for k in self.chains if k not in roots]:
            self.chains.pop(k, None)

    def _save(self) -> None:
        if self.path:
            try:
                self._prune()
                _atomic_write(self.path, {"legs": self.legs, "chains": self.chains})
            except OSError:
                logger.exception("Anti-Twin: the ledger could not be written")

    def _ev(self, event, leg, level="info", **f):
        if self.events is not None:
            try:
                self.events.emit("antitwin_" + event, leg.get("candle_id"), symbol=leg.get("symbol"), timeframe=leg.get("timeframe"), leg=leg["id"], level=level, **f)
            except Exception:                                         # noqa: BLE001
                logger.exception("Anti-Twin event could not be logged")

    def _go(self, leg, state, why, **fields):
        leg.update(fields)
        leg["state"], leg["reason"], leg["state_at"] = state, why, self._clock()
        leg["reversal_state"] = reversal_state(leg)
        level = "error" if state in (UNCERTAIN, FAILED) and leg.get("closed_at") else "error" if state == UNCERTAIN else \
            "warning" if state in (FAILED, REFUSED_ST, EMERGENCY_CANCELLED, LIMIT_REACHED, SWITCHED_OFF, ABORTED_PREFLIGHT) else "info"
        self._ev("state", leg, level, to_state=state, reversal_state=leg["reversal_state"], reason=why)
        self._save()

    # ------------------------------------------------------------------ registration
    def adopt(self, lc: dict, cfg, positions, spec) -> str | None:
        """Start watching the ORIGINAL position of a Bull/Bear candle lifecycle. Idempotent (one leg per ticket). -> leg id, or None."""
        ticket = lc.get("ticket")
        with self._lock:
            lid = f"{ticket}-0"
            if ticket is None or lid in self.legs:
                return lid if lid in self.legs else None
            pos = next((p for p in positions if p.ticket == ticket), None)
            if pos is None or not _num(pos.volume) or not _num(pos.price_open):
                return None
            return self._create(lid, lid, 0, lc.get("candle_id"), lc["symbol"], lc["timeframe"], pos, cfg, spec, None)

    def try_adopt(self, lc: dict, cfg, positions, spec) -> str | None:
        """adopt() that never waits: if a reversal is in progress (the lock is busy) it returns None and the engine's next cycle adopts instead."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self.adopt(lc, cfg, positions, spec)
        finally:
            self._lock.release()

    def _create(self, lid, root, level, candle_id, symbol, timeframe, pos, cfg, spec, parent, parent_tp=None) -> str | None:
        entry = float(pos.price_open)
        sl, tp = pos.stop_loss, pos.take_profit
        if not (_num(sl) and sl > 0 and _num(tp) and tp > 0):
            leg = {"id": lid, "root": root, "level": level, "candle_id": candle_id, "symbol": symbol, "timeframe": timeframe, "ticket": pos.ticket, "state": UNCERTAIN,
                   "reason": "The position has no broker-side stop-loss and take-profit, so Anti-Twin cannot size a twin. Not armed.", "state_at": self._clock(), "created_at": self._clock()}
            self.legs[lid] = leg
            self._save()
            return None
        side = pos.direction
        sign = 1 if side == "buy" else -1
        tp_dist, sl_dist = abs(tp - entry), abs(entry - sl)
        try:
            twin_tp = parent_tp if parent_tp is not None else tp_dist / 2
        except (ValueError, AttributeError, TypeError) as exc:
            self.legs[lid] = {"id": lid, "root": root, "level": level, "candle_id": candle_id, "symbol": symbol, "timeframe": timeframe, "ticket": pos.ticket, "state": UNCERTAIN,
                              "reason": f"The instrument conversion is unavailable ({exc}); Anti-Twin is not watching.", "state_at": self._clock(), "created_at": self._clock()}
            self._save()
            return None
        leg = {"id": lid, "root": root, "level": level, "parent": parent, "candle_id": candle_id, "symbol": symbol, "timeframe": timeframe, "ticket": pos.ticket, "side": side,
               "volume": float(pos.volume), "entry": entry, "sl_dist": sl_dist, "tp_dist": tp_dist, "twin_tp_dist": twin_tp, "rule": rule.new_state(side, entry),
               "state": WATCHING, "reason": rule.describe(rule.new_state(side, entry)),
               "state_at": self._clock(), "created_at": self._clock(), "attempts": 0, "close_sent": False, "open_sent": False, "twin": None, "sign": sign}
        self.legs[lid] = leg
        self.chains.setdefault(root, {"claimed": 0, "max": MAX_CHAIN})
        self._ev("watching", leg, entry=entry, tp_distance=tp_dist, sl_distance=sl_dist)
        self._save()
        return lid

    # ------------------------------------------------------------------ the step
    def step(self, quote, spec, positions, closed_lookup=None, enabled=True, cfg=None, fresh_positions=None, fresh_quote=None, block=True, timeframe=None) -> bool:
        """One evaluation of every active leg on the current executable quote. Never raises. Returns False only when `block` is False and a
        reversal is already in progress on another thread (nothing was evaluated).

        `fresh_positions` / `fresh_quote` are optional zero-argument readers of the account and of the live quote. When given, the confirmation that the
        original is closed and the opening of the twin happen in THIS call, on a fresh account read and a fresh quote, instead of waiting for the next
        cycle. Without them the previous (one cycle later) behaviour is kept."""
        if not self._lock.acquire(blocking=block):
            return False
        try:
            self._ctx = {"quote": quote, "spec": spec, "fresh_positions": fresh_positions, "fresh_quote": fresh_quote, "closed_lookup": closed_lookup}
            now = self._clock()
            for leg in list(self.legs.values()):
                if leg["state"] not in ACTIVE or leg["symbol"] != quote.symbol or (timeframe is not None and leg.get("timeframe") != timeframe):
                    continue
                try:
                    self._step_leg(leg, quote, spec, positions, closed_lookup, enabled, cfg, now)
                except Exception:                                      # noqa: BLE001 - a bug must never leave a half-done reversal silently running
                    logger.exception("Anti-Twin step failed for %s", leg["id"])
                    self._go(leg, UNCERTAIN, "An internal error occurred; Anti-Twin stopped for this trade. Check MT5.")
            return True
        finally:
            self._ctx = None
            self._lock.release()

    def active_legs(self) -> list:
        """(symbol, timeframe, state) of every leg that still needs watching: what the fast lane asks."""
        with self._lock:
            return [(l["symbol"], l.get("timeframe"), l["state"]) for l in self.legs.values() if l["state"] in ACTIVE]

    def _sync_from_position(self, leg, pos) -> None:
        """Use what the ACCOUNT says: the actual filled volume (a reversal mirrors it) and the position's own entry price (a twin's order-result price can
        differ from its position's open price). A stale reference is replaced, never used: the watch state restarts from the position's real entry."""
        vol, po = getattr(pos, "volume", None), getattr(pos, "price_open", None)
        changed = False
        if _num(vol) and vol > 0 and abs(vol - leg["volume"]) > 1e-12:
            leg["volume"], changed = float(vol), True
        if _num(po) and po > 0 and abs(po - leg["entry"]) > 1e-12 and not (leg.get("rule") or {}).get("triggered"):
            self._ev("entry_corrected", leg, was=leg["entry"], now=float(po))
            leg["entry"], leg["rule"], changed = float(po), rule.new_state(leg["side"], float(po)), True
        if changed:
            self._save()

    def _step_leg(self, leg, quote, spec, positions, closed_lookup, enabled, cfg, now):
        st = leg["state"]
        if self._locked():                                             # Emergency Stop has absolute priority over a reversal
            if st in (WATCHING, ARMED, AWAITING_CLOSE) or (st == OPENING and not leg.get("open_sent")) or (st == CLOSING and not leg["close_sent"]):
                self._go(leg, EMERGENCY_CANCELLED, "Emergency Stop is active: Anti-Twin is cancelled and opens nothing.")
            elif st == OPENING:                                        # an order is already in flight: it cannot be recalled, but it is still reconciled and reported
                self._confirm_open(leg, positions, now)
            return
        if not enabled and (st == AWAITING_CLOSE or (st == OPENING and not leg.get("open_sent")) or (st == CLOSING and not leg["close_sent"])):
            self._go(leg, SWITCHED_OFF, "Anti-Twin was switched off before the opposite trade was opened: nothing more is sent.")
            return
        pos = next((p for p in positions if p.ticket == leg["ticket"]), None)
        if st in (WATCHING, ARMED):
            if pos is None:
                info = (closed_lookup(leg["ticket"]) if closed_lookup else None) or {}
                reason = info.get("reason")
                if reason is None and now - leg["state_at"] < GRACE_SECONDS and leg.get("missing_since") is None:
                    leg["missing_since"] = now
                    return
                if reason is None and now - (leg.get("missing_since") or now) < GRACE_SECONDS:
                    return
                end = {"tp": TP_HIT, "sl": SL_HIT}.get(reason, CLOSED_EXTERNAL)
                self._go(leg, end, f"The position closed ({reason or 'reason unknown'}). That is not a reversal trigger: no twin.")
                return
            leg.pop("missing_since", None)
            self._sync_from_position(leg, pos)
            if not enabled:
                return
            px = quote.bid if leg["side"] == "buy" else quote.ask
            if not _num(px):
                return
            fav = (px - leg["entry"]) * leg["sign"]
            if fav >= leg["tp_dist"] - 1e-12:
                self._go(leg, TP_TOUCHED, "The take-profit level was reached: a normal target never triggers Anti-Twin.")
                return
            if not isinstance(leg.get("rule"), dict):                                # a ledger written by an older version
                leg["rule"] = rule.new_state(leg["side"], leg["entry"])
            if now < leg.get("next_pf_at", 0.0):                                     # a transient pre-flight failure is being waited out (not re-asked on every quote)
                return
            tick = getattr(spec, "tick_size", None)
            if rule.observe(leg["rule"], quote.bid, quote.ask, tick):               # the first adverse move on the executable exit price (bid/ask handled inside the rule)
                self._trigger(leg, quote, spec, now)
            else:
                text = rule.describe(leg["rule"])
                if leg["reason"] != text:
                    leg["reason"] = text
                    self._save()
            return
        if st == OPENING and leg.get("open_sent"):                    # the order has been sent: only ever RECONCILE it, never send it again
            self._confirm_open(leg, positions, now)
            return
        if now < leg.get("next_try_at", 0.0):
            return
        if st == CLOSING:
            self._do_close(leg, now)
        elif st == AWAITING_CLOSE:
            if pos is None:
                if leg.get("close_unconfirmed"):                       # we do not know our close was what removed it: the broker may have got there first (SL / TP)
                    info = (closed_lookup(leg["ticket"]) if closed_lookup else None) or {}
                    if info.get("reason") in ("tp", "sl"):
                        self._go(leg, TP_HIT if info["reason"] == "tp" else SL_HIT, f"The position was closed by the broker at its {info['reason'].upper()}, not by Anti-Twin: no twin.")
                        return
                self._confirmed(leg, now)
                self._do_open(leg, quote, spec, now)
            elif now - leg["state_at"] > CONFIRM_SECONDS:
                self._close_not_proven(leg)
        elif st == OPENING:
            self._do_open(leg, quote, spec, now)

    def _close_not_proven(self, leg):
        """The close was sent, but after the confirmation window the position is STILL on the account. Ask the journal once (read-only) and report exactly what is known."""
        rec = None
        if leg.get("exec_id_close") and hasattr(self.executor, "reconcile"):
            try:
                rec = self.executor.reconcile(leg["exec_id_close"])
            except Exception:                                          # noqa: BLE001
                rec = None
        if rec and rec.get("state") == "reconciled_not_executed":
            self._go(leg, FAILED, "The close did NOT happen (reconciled with the account): the original is still open and no twin was opened.")
        else:
            self._go(leg, UNCERTAIN, "RECOVERY REQUIRED: the close was sent but the position is still open after the confirmation window. Nothing was opened and nothing will be re-sent. Check MT5.")

    # ------------------------------------------------------------------ the twin's levels and the pre-flight
    @staticmethod
    def _twin_levels(leg, spec, quote):
        """(side, reference price, sl, tp) of the opposite order exactly as it will be sent. None when the quote cannot price it."""
        side = "sell" if leg["side"] == "buy" else "buy"
        px = quote.bid if side == "sell" else quote.ask
        if not (getattr(quote, "valid", False) and _num(px)):
            return side, None, None, None
        sign = 1 if side == "buy" else -1
        sl = spec.snap(px - sign * leg["sl_dist"], "down" if side == "buy" else "up")
        tp = spec.snap(px + sign * leg["twin_tp_dist"], "up" if side == "buy" else "down")
        return side, px, sl, tp

    def _preflight(self, leg, quote, spec, now):
        """Ask, BEFORE anything is closed, whether the opposite order could be sent. Returns None when it may proceed (or when the executor has no pre-flight)."""
        fn = getattr(self.executor, "preflight_open", None)
        if fn is None:
            return None
        side, px, sl, tp = self._twin_levels(leg, spec, quote)
        if px is None:
            return Preflight(False, "No usable quote to price the opposite order.", "connection", retryable=True)
        trig = (leg.get("rule") or {}).get("trigger") or {}
        try:
            pf = fn(leg["symbol"], side, leg["volume"], sl, sl=sl, tp=tp, exclude_tickets=(leg["ticket"],), closing_at_loss=(trig.get("d") or -1) < 0)
            return None if pf.ok else pf
        except Exception:                                              # noqa: BLE001 - never raise out of a step; a failed pre-flight is a transient wait
            logger.exception("Anti-Twin pre-flight failed for %s", leg["id"])
            return Preflight(False, "The pre-flight check could not be completed.", "connection", retryable=True)

    def _trigger(self, leg, quote, spec, now):
        if any(o is not leg and o["ticket"] == leg["ticket"] and o["state"] in (CLOSING, AWAITING_CLOSE, OPENING) for o in self.legs.values()):
            return                                                     # one active reversal operation per position ticket
        chain = self.chains.setdefault(leg["root"], {"claimed": 0, "max": MAX_CHAIN})
        if chain["claimed"] >= chain["max"]:
            self._go(leg, LIMIT_REACHED, f"Safety ceiling reached ({chain['claimed']} reversals in this chain): this trade is left alone.")
            return
        self._ev("reversal_triggered", leg, reversal_state="REVERSAL_TRIGGERED", entry=leg["entry"], bid=quote.bid, ask=quote.ask, volume=leg["volume"],
                 direction=leg["side"], ticket=leg["ticket"])
        pf = self._preflight(leg, quote, spec, now)
        if pf is not None:
            self._ev("preflight_failed", leg, "warning", stage=pf.stage, retryable=pf.retryable, message=pf.message, details=getattr(pf, "details", None))
            if pf.retryable:                                           # transient: nothing was closed, look again shortly (the original keeps its own SL / TP meanwhile)
                leg["rule"]["triggered"] = False
                leg["rule"].pop("trigger", None)
                leg["next_pf_at"] = now + PREFLIGHT_RETRY_SECONDS
                leg["reason"] = f"Reversal postponed, nothing was closed ({pf.stage}): {pf.message}"
                self._save()
                return
            self._go(leg, ABORTED_PREFLIGHT, f"REVERSAL NOT STARTED: {pf.message} The original position was NOT closed; it stays open under its own stop-loss and take-profit.")
            return
        chain["claimed"] += 1                                          # the claim is written BEFORE anything is sent: a second twin for this leg is impossible
        trig = dict((leg.get("rule") or {}).get("trigger") or {})
        self._lat[leg["id"]] = {"detect": self._perf(), "quote_age": getattr(quote, "age_seconds", None), "trigger": trig}
        x = trig.get("exit")
        self._ev("adverse_move", leg, entry=leg["entry"], exit_price=x, bid=quote.bid, ask=quote.ask, quote_age=getattr(quote, "age_seconds", None))
        self._go(leg, CLOSING, f"Adverse move detected (exit price {x if x is not None else '?'} vs entry {leg['entry']:g}): closing the position to switch direction. "
                               f"Reversal {chain['claimed']} in this chain.")
        self._do_close(leg, now)

    def _ticket_gone(self, leg) -> bool:
        """True only when a fresh read of the account PROVES the ticket is no longer open (False when it cannot be read)."""
        fp = (self._ctx or {}).get("fresh_positions")
        if fp is None:
            return False
        try:
            return not any(p.ticket == leg["ticket"] for p in fp())
        except Exception:                                              # noqa: BLE001
            return False

    def _do_close(self, leg, now):
        if self._locked():
            self._go(leg, EMERGENCY_CANCELLED, "Emergency Stop is active: nothing is closed or opened by Anti-Twin.")
            return
        leg["attempts"] += 1
        leg["close_sent"] = True
        self._save()
        lat = self._lat.setdefault(leg["id"], {"detect": self._perf()})
        lat["close_sent"] = self._perf()
        out = self.executor.close(leg["ticket"])
        lat["close_done"] = self._perf()
        self._ev("close", leg, kind=out.kind, message=out.message, fill_price=out.price, response=_resp(out), ticket=leg["ticket"], volume=leg["volume"])
        if out.kind == EXECUTED:
            leg["close_price"] = out.price
            self._go(leg, AWAITING_CLOSE, "Close accepted; confirming that the position is gone from the account before opening the twin.")
            self._after_close(leg, now)
        elif out.kind in (UNCONFIRMED, UNKNOWN):                       # the broker may well have closed it: wait for the ACCOUNT to prove it. Never re-send, never give up early.
            leg["close_price"] = out.price
            self._go(leg, AWAITING_CLOSE, "The close was sent but MT5 has not shown the position gone yet. Waiting for the account (the close is NOT re-sent); "
                                           "the opposite order opens only once the position is proven closed. " + (out.message or ""),
                     close_unconfirmed=True, exec_id_close=out.execution_id)
            self._after_close(leg, now)
        elif out.kind == NOT_EXECUTED and out.retryable and leg["attempts"] < MAX_ATTEMPTS:
            if self._ticket_gone(leg):                                 # the broker got there first (SL / TP / by hand): that is not ours to reverse
                self._go(leg, CLOSED_EXTERNAL, "The position was already closed by the broker or by hand when Anti-Twin tried to close it: no twin.")
                return
            leg["close_sent"] = False
            leg["next_try_at"] = now + RETRY_BACKOFF
            self._save()
        elif out.kind == NOT_EXECUTED:
            self._go(leg, FAILED, "The close could not be completed after bounded attempts; the original remains open. No twin was opened. " + out.message)
        else:
            self._go(leg, UNCERTAIN, "RECOVERY REQUIRED: the close outcome is not provable. Nothing was opened and nothing will be re-sent. Check MT5. " + out.message)

    def _confirmed(self, leg, now):
        self._lat.setdefault(leg["id"], {"detect": self._perf()})["confirmed"] = self._perf()
        self._go(leg, OPENING, "The original is confirmed closed by the account. Opening the opposite trade.", closed_at=now, close_confirmed=True)

    def _after_close(self, leg, now):
        """The close was confirmed by the executor. With a fresh-account reader, verify it and open the twin NOW; without one, the next step does."""
        ctx = self._ctx or {}
        fp = ctx.get("fresh_positions")
        if fp is None:
            return
        try:
            positions = list(fp())
        except Exception:                                              # noqa: BLE001 - cannot prove it this instant: the next step confirms
            return
        if any(p.ticket == leg["ticket"] for p in positions):
            return                                                     # still listed: the next step keeps confirming (bounded by CONFIRM_SECONDS)
        if leg.get("close_unconfirmed"):                               # the executor could not prove it was OUR close that removed it: rule out the broker's own SL / TP
            info = ((ctx.get("closed_lookup") or (lambda t: None))(leg["ticket"])) or {}
            if info.get("reason") in ("tp", "sl"):
                self._go(leg, TP_HIT if info["reason"] == "tp" else SL_HIT, f"The position was closed by the broker at its {info['reason'].upper()}, not by Anti-Twin: no twin.")
                return
        self._confirmed(leg, now)
        quote = ctx["quote"]
        fq = ctx.get("fresh_quote")
        if fq is not None:
            try:
                q = fq()
                if q is not None and getattr(q, "valid", False):
                    quote = q
            except Exception:                                          # noqa: BLE001 - the quote of this step is the fallback
                pass
        self._do_open(leg, quote, ctx["spec"], now)

    def _do_open(self, leg, quote, spec, now):
        if self._locked():
            self._go(leg, EMERGENCY_CANCELLED, "Emergency Stop is active: the twin is not opened.")
            return
        if now - leg.get("closed_at", leg["state_at"]) > TWIN_DEADLINE:
            self._go(leg, FAILED, f"REVERSAL FAILED: the opposite order could not be opened within {TWIN_DEADLINE:g}s of the original's closure and was abandoned "
                                  "(the original stays closed; nothing is open).")
            return
        side, px, sl, tp = self._twin_levels(leg, spec, quote)
        if px is None:
            if leg["attempts"] >= MAX_ATTEMPTS + 2:
                self._go(leg, FAILED, "REVERSAL FAILED: no usable quote to open the opposite order; the original is already closed.")
            leg["attempts"] += 1
            leg["next_try_at"] = now + RETRY_BACKOFF
            return
        leg["open_sent"] = True
        leg["open_sent_at"] = now
        self._save()
        lat = self._lat.setdefault(leg["id"], {"detect": self._perf()})
        lat["open_sent"] = self._perf()
        out = self.executor.open(leg["symbol"], side, leg["volume"], sl, sl=sl, tp=tp)
        lat["open_done"] = self._perf()
        self._ev("open", leg, kind=out.kind, message=out.message, side=side, volume=leg["volume"], sl=sl, tp=tp, fill_price=out.price, response=_resp(out),
                 ticket=out.ticket)
        if out.kind == EXECUTED:
            self._twin_opened(leg, side, out, sl, tp, px)
        elif out.kind in (UNCONFIRMED, UNKNOWN):                       # the order may be live: it is NEVER re-sent. It is reconciled with the account instead.
            leg.update(open_unconfirmed=True, exec_id_open=out.execution_id, open_ticket_hint=out.ticket, open_levels={"side": side, "sl": sl, "tp": tp, "px": px})
            self._go(leg, OPENING, "The opposite order was sent but MT5 has not confirmed it yet. It is NOT re-sent; the account is being checked for it. " + (out.message or ""))
        elif out.kind == REFUSED:
            self._go(leg, REFUSED_ST, "REVERSAL FAILED: the Risk Engine refused the opposite order. The original is already closed; no opposite trade was opened. " + out.message)
            self._finish_latency(leg, "refused")
        elif out.kind == NOT_EXECUTED and out.retryable and leg["attempts"] < MAX_ATTEMPTS + 2:
            leg["open_sent"] = False
            leg["attempts"] += 1
            leg["next_try_at"] = now + RETRY_BACKOFF
            self._save()
        elif out.kind == NOT_EXECUTED:
            self._go(leg, FAILED, "REVERSAL FAILED: the opposite order could not be opened after bounded attempts. The original is closed; nothing is open. " + out.message)
            self._finish_latency(leg, "failed")
        else:
            self._go(leg, UNCERTAIN, "RECOVERY REQUIRED: the opposite order's outcome is not provable. It is NOT re-sent. Check MT5. " + out.message)
            self._finish_latency(leg, "uncertain")

    def _twin_opened(self, leg, side, out, sl, tp, px, pos=None):
        """Report a successful reversal ONLY after the new position is verified: its ticket exists, and its symbol, side and volume are what was asked."""
        pos = pos if pos is not None else out.position
        ticket = out.ticket or (getattr(pos, "ticket", None) if pos is not None else None)
        if ticket is None:
            self._go(leg, UNCERTAIN, "RECOVERY REQUIRED: the opposite order was reported executed but MT5 gave no ticket for it. It is NOT re-sent. Check MT5.")
            self._finish_latency(leg, "uncertain")
            return
        if pos is None:                                                # reported executed without the position object (e.g. a journal reconciliation): look it up on the account
            fp = (self._ctx or {}).get("fresh_positions")
            readable = False
            if fp is not None:
                try:
                    pos, readable = next((p for p in fp() if p.ticket == ticket), None), True
                except Exception:                                      # noqa: BLE001
                    pos = None
            if pos is None and readable:                               # the account is readable and does not show it (yet): keep checking, never claim success
                leg.update(open_unconfirmed=True, open_ticket_hint=ticket, open_levels={"side": side, "sl": sl, "tp": tp, "px": px})
                self._go(leg, OPENING, f"The opposite order is reported executed (ticket {ticket}) but the position is not visible in MT5 yet. Waiting for the account; nothing is re-sent.")
                return
        if getattr(pos, "direction", side) != side or str(getattr(pos, "symbol", leg["symbol"])).upper() != str(leg["symbol"]).upper():
            self._go(leg, UNCERTAIN, f"RECOVERY REQUIRED: MT5 shows position {ticket} as {getattr(pos, 'direction', '?')} {getattr(pos, 'symbol', '?')}, not the {side} {leg['symbol']} "
                                     "that was ordered. It is NOT adopted and nothing is re-sent. Check MT5.")
            self._finish_latency(leg, "uncertain")
            return
        po = getattr(pos, "price_open", None)
        fill = float(po) if _num(po) and po > 0 else float(out.price or px)
        pv = getattr(pos, "volume", None)
        vol = float(pv) if _num(pv) and pv > 0 else leg["volume"]
        partial = vol + 1e-9 < leg["volume"]
        tid = f"{ticket}-{leg['level'] + 1}"
        note = f" PARTIAL FILL: only {vol:g} of {leg['volume']:g} lots filled; it is not topped up." if partial else ""
        self._go(leg, TWIN_OPENED, f"Twin {ticket} opened and verified in MT5 ({side} {vol:g}, {'the filled volume' if partial else 'equal volume'}) at {fill:g}.{note}",
                 twin=tid, open_price=fill, twin_ticket=ticket, twin_volume=vol, twin_fill=fill)
        twin = {"id": tid, "root": leg["root"], "level": leg["level"] + 1, "parent": leg["id"], "candle_id": leg.get("candle_id"), "symbol": leg["symbol"],
                "timeframe": leg["timeframe"], "ticket": ticket, "side": side, "volume": vol, "entry": fill, "sl_dist": leg["sl_dist"],
                "tp_dist": leg["twin_tp_dist"], "twin_tp_dist": leg["twin_tp_dist"], "rule": rule.new_state(side, fill), "state": WATCHING,
                "reason": "Twin watching every quote from its own entry price.", "state_at": self._clock(), "created_at": self._clock(), "attempts": 0,
                "close_sent": False, "open_sent": False, "twin": None, "sign": 1 if side == "buy" else -1, "partial": partial, "reversal_state": "ORIGINAL_POSITION_ACTIVE"}
        self.legs[tid] = twin
        self._save()
        self._finish_latency(leg, "twin_opened")

    def _confirm_open(self, leg, positions, now):
        """An opposite order was sent and not yet proven. Only the ACCOUNT can settle it: the ticket MT5 gave, or the execution journal's reconciliation. Never re-send."""
        lv = leg.get("open_levels") or {}
        side = lv.get("side") or ("sell" if leg["side"] == "buy" else "buy")
        ticket = leg.get("open_ticket_hint")
        rec = None
        if ticket is None and leg.get("exec_id_open") and hasattr(self.executor, "reconcile"):
            try:
                rec = self.executor.reconcile(leg["exec_id_open"])
            except Exception:                                          # noqa: BLE001
                rec = None
            if rec and rec.get("state") == "reconciled_executed" and rec.get("ticket"):
                ticket = rec["ticket"]
            elif rec and rec.get("state") == "reconciled_not_executed":
                self._go(leg, FAILED, "REVERSAL FAILED: the opposite order did NOT execute (reconciled with the account). The original is closed; nothing is open. "
                                      + str(rec.get("message") or ""))
                self._finish_latency(leg, "failed")
                return
        pos = next((p for p in positions if ticket is not None and p.ticket == ticket), None)
        if pos is not None:
            out = type("Out", (), {"ticket": ticket, "position": pos, "price": getattr(pos, "price_open", None)})()
            self._twin_opened(leg, side, out, lv.get("sl"), lv.get("tp"), lv.get("px"), pos=pos)
            return
        if now - leg.get("open_sent_at", leg["state_at"]) > CONFIRM_SECONDS:
            self._go(leg, UNCERTAIN, "RECOVERY REQUIRED: the opposite order was sent but no matching position has appeared in MT5 after the confirmation window. "
                                     "It is NOT re-sent. Check MT5, then open the position yourself if it is missing.")
            self._finish_latency(leg, "uncertain")

    # ------------------------------------------------------------------ measured latency
    def _finish_latency(self, leg, outcome: str) -> None:
        """Turn the monotonic stamps of one reversal into milliseconds, keep them and log them. These are MEASURED, never assumed (rule.latency_record)."""
        lat = self._lat.pop(leg["id"], None)
        if not lat or "detect" not in lat:
            return
        lat["close_price"] = leg.get("close_price")
        rec = rule.latency_record(lat, side=leg["side"], entry=leg["entry"], outcome=outcome, leg=leg["id"], ticket=leg["ticket"], symbol=leg["symbol"])
        rec["at"] = self._clock()
        self.latency_log.append(rec)
        leg["latency"] = {k: v for k, v in rec.items() if k.endswith("_ms")}
        self._ev("latency", leg, **{k: v for k, v in rec.items() if k not in ("leg", "symbol")})
        self._save()

    def latency_summary(self) -> dict:
        with self._lock:
            return rule.latency_summary(list(self.latency_log))

    # ------------------------------------------------------------------ read model
    def snapshot(self, symbol=None, timeframe=None) -> dict:
        """The read model for the UI. It never waits behind a reversal in progress (that can take a second): it then returns the last one it built."""
        key = (symbol, timeframe)
        if not self._lock.acquire(timeout=0.02):
            return self._snap_cache.get(key) or {"legs": [], "chains": {}, "latency": None, "trigger_mode": rule.ACTIVE_MODE, "busy": True}
        try:
            legs = [dict(v) for v in self.legs.values() if (symbol is None or v.get("symbol") == symbol) and (timeframe is None or v.get("timeframe") == timeframe)]
            legs.sort(key=lambda v: v.get("created_at", 0), reverse=True)
            out = {"legs": legs[:50], "chains": {k: dict(v) for k, v in self.chains.items() if any(l["root"] == k for l in legs)}, "latency": self.latency_summary(),
                   "trigger_mode": rule.ACTIVE_MODE}
            self._snap_cache[key] = out
            return out
        finally:
            self._lock.release()

    def has_active(self, ticket) -> bool:
        with self._lock:
            return any(l["ticket"] == ticket and l["state"] in ACTIVE for l in self.legs.values())
