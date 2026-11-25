"""The Anti-Twin FAST LANE: a dedicated, quote-driven monitor for Candle Momentum and Bull/Bear.   (docs/ANTI_TWIN.md)

WHY IT EXISTS
  Anti-Twin used to be judged inside the engine's general cycle (every 0.25 s, and then at most every 0.5 s by an extra throttle), on the same thread that
  also fetches candles, specifications and builds the screen. A reversal could therefore be seen up to ~0.75 s late and, after the close, wait another
  full cycle before the opposite order was sent. The lane removes both: it has no other job. It watches ONLY while an Anti-Twin leg is active, reads the
  live quote in a tight loop, and evaluates on every NEW quote. The close -> confirm -> open sequence then runs in one call (AntiTwin.step).

WHAT IT DOES NOT DO
  * It does not decide anything: the rule is trademate/antitwin_rule.py and the state machine is AntiTwin. The lane only feeds them quotes sooner.
  * It does not adopt new positions or read the engine's machines (that stays on the engine's own thread / execution report): it only steps legs that exist.
  * It does not poll faster than the bridge can deliver: a quote that did not change is not re-judged (a periodic pass still checks the account).
  * It does not bypass anything: the same AntiTwin, the same Risk gate, the same Emergency Stop priority.

TESTABLE WITHOUT A THREAD: `run_once()` is one iteration; the thread only calls it.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("trademate.antitwin_lane")

BUSY_SECONDS = 0.02          # pause between iterations while a leg is active (a snapshot read costs about a millisecond)
IDLE_SECONDS = 0.05          # pause while no leg is active: the only cost of the lane is noticing that a leg was created
PERIODIC_SECONDS = 0.25      # an unchanged quote is still re-judged at this cadence (the account is re-read: external closes, volume, entry)
IN_FLIGHT = ("CLOSING", "AWAITING_CLOSE", "OPENING")


class AntiTwinLane:
    def __init__(self, get_anti_twin, feed, resolve, usable, closed_lookup=None, clock=time.time):
        """get_anti_twin() -> the AntiTwin (or None); feed -> the engine's broker window (quote / open_positions);
        resolve(symbol, timeframe) -> (spec, cfg) while that slot is being monitored, else None; usable(quote, cfg) -> True when the quote is fresh enough."""
        self._at, self.feed, self._resolve, self._usable, self._closed, self._clock = get_anti_twin, feed, resolve, usable, closed_lookup, clock
        self._thread: threading.Thread | None = None
        self._stop_ev: threading.Event | None = None
        self._sig: dict = {}
        self._last_eval: dict = {}
        self.iterations = 0
        self.evaluations = 0
        self.last_error: str | None = None
        self.last_run_at: float | None = None

    # ------------------------------------------------------------------ one iteration
    def run_once(self) -> bool:
        """Judge every active leg once if there is something new to judge. True when at least one leg is active (the caller then loops quickly)."""
        self.iterations += 1
        at = self._at()
        if at is None:
            return False
        legs = at.active_legs()
        if not legs:
            return False
        for symbol, timeframe in sorted({(s, tf) for s, tf, _ in legs}, key=lambda k: (k[0], str(k[1]))):
            resolved = self._resolve(symbol, timeframe)
            if resolved is None:
                continue
            spec, cfg = resolved
            try:
                q = self.feed.quote(symbol)
            except Exception as exc:                                   # noqa: BLE001 - a bridge hiccup: try again on the next iteration
                self.last_error = f"{type(exc).__name__}: {exc}"
                continue
            if not self._usable(q, cfg):
                continue
            now = self._clock()
            key = (symbol, timeframe)
            in_flight = any(s == symbol and tf == timeframe and st in IN_FLIGHT for s, tf, st in legs)
            if not (in_flight or (q.bid, q.ask) != self._sig.get(key) or now - self._last_eval.get(key, 0.0) >= PERIODIC_SECONDS):
                continue                                               # same price as the last judgement and nothing in flight: nothing new to learn
            try:
                positions = self.feed.open_positions()
            except Exception as exc:                                   # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                continue
            self._sig[key], self._last_eval[key] = (q.bid, q.ask), now

            def fresh_quote(symbol=symbol, cfg=cfg):
                fq = self.feed.quote(symbol)
                return fq if self._usable(fq, cfg) else None
            ran = at.step(q, spec, positions, self._closed, enabled=cfg.anti_twin_enabled, cfg=cfg, fresh_positions=self.feed.open_positions,
                          fresh_quote=fresh_quote, timeframe=timeframe, block=True)
            if ran:
                self.evaluations += 1
                self.last_run_at = self._clock()
        return True

    # ------------------------------------------------------------------ the thread
    def start(self, stop: threading.Event) -> None:
        """Start (or keep) the lane's thread. It ends by itself when `stop` is set."""
        if self._thread is not None and self._thread.is_alive() and self._stop_ev is stop and not stop.is_set():
            return
        self._stop_ev = stop                                           # a thread still finishing under an older (set) event ends by itself: this one is a fresh lane
        self._thread = threading.Thread(target=self._loop, args=(stop,), name="trademate-antitwin-lane", daemon=True)
        self._thread.start()

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                busy = self.run_once()
            except Exception as exc:                                   # noqa: BLE001 - the lane must survive; the failure is reported, never hidden
                logger.exception("Anti-Twin lane iteration failed")
                self.last_error = f"{type(exc).__name__}: {exc}"
                stop.wait(0.5)
                continue
            stop.wait(BUSY_SECONDS if busy else IDLE_SECONDS)

    def status(self) -> dict:
        return {"running": bool(self._thread and self._thread.is_alive()), "iterations": self.iterations, "evaluations": self.evaluations,
                "last_run_at": self.last_run_at, "last_error": self.last_error}
