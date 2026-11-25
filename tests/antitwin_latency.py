"""MEASURED Anti-Twin latency on an EA-emulating simulated bridge.   (docs/ANTI_TWIN.md)

    python3 -m tests.antitwin_latency                 # prints the table (about a minute)
    python3 -m tests.antitwin_latency --runs 20       # more repetitions

WHAT IS REAL: the TradeMate side is the real code path - MT5Adapter (command files, confirmation polling), the central Executor with the Risk gate,
AntiTwin, and the fast lane reading the live quote. WHAT IS EMULATED: MetaTrader. `EABridge` behaves like the TradeMateBridge EA's single timer:
every CommandPollMs it picks up command files, executes them, and writes the account snapshot only every UpdateIntervalMs; a trade command also takes
`broker_ms` to be answered. The price moves in the "broker" at an instant the harness records, at a random phase against the EA's timer, so the
numbers include the staleness of the snapshot, exactly the part that dominates on a real terminal.

WHAT THIS IS NOT: a measurement of a real broker. Real execution latency, slippage and requotes come from MT5 and the broker and can only be measured
on a demo account (`python -m trademate.broker.live_test`). These numbers are the TradeMate + bridge-timing part, with the broker delay a parameter.
"""

from __future__ import annotations

import argparse
import random
import statistics
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.fake_bridge import FakeBridge
from trademate import brokertime
from trademate.antitwin_lane import AntiTwinLane
from trademate.bull_bear.antitwin import AntiTwin
from trademate.bull_bear.feed import AdapterFeed
from trademate.bull_bear.models import BBConfig
from trademate.broker import create_adapter
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.rapid_fire.executor import Executor
from trademate.risk.rules import RiskRules


class EABridge(FakeBridge):
    """FakeBridge that runs on ONE timer like the EA: commands at every poll, the snapshot only every `snapshot_ms`, trade commands answered after `broker_ms`."""

    def __init__(self, directory, snapshot_ms=1000, poll_ms=200, broker_ms=50):
        super().__init__(directory)
        self.snapshot_s, self.poll_s, self.broker_s = snapshot_ms / 1000.0, poll_ms / 1000.0, broker_ms / 1000.0
        self._defer = False

    def write_snapshot(self):
        if not self._defer:
            super().write_snapshot()

    def _handle(self, path):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        if "cmd=open" in text or "cmd=close" in text:
            time.sleep(self.broker_s)                                  # the broker's execution time
        super()._handle(path)

    def _loop(self):
        last = 0.0
        while not self._stop.is_set():
            t = time.perf_counter()
            self._defer = True                                         # a real EA does not publish a snapshot because a command ran
            try:
                for f in sorted(self.dir.glob("trademate_cmd_*.txt")):
                    self._handle(f)
            finally:
                self._defer = False
            if time.perf_counter() - last >= self.snapshot_s:
                super().write_snapshot()
                last = time.perf_counter()
            time.sleep(max(0.0, self.poll_s - (time.perf_counter() - t)))


class Harness:
    def __init__(self, snapshot_ms, poll_ms, broker_ms):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.bridge = EABridge(d, snapshot_ms, poll_ms, broker_ms).start()
        self._clock_patch = mock.patch("trademate.risk.account_state.resolve_server_clock", lambda *a, **k: brokertime.ServerClock(0, "configured", "fixed"))
        self._clock_patch.start()
        self.adapter = create_adapter(Settings(app=AppSettings(data_dir=d), mt5=Mt5Settings(data_dir=d),
                                               trading=TradingSettings(command_timeout_seconds=5.0, confirm_timeout_seconds=6.0)))
        self.rules = RiskRules(max_risk_per_trade_percent=1.0, max_daily_loss_percent=3.0, max_total_active_risk_percent=3.0, max_positions=3)
        self.executor = Executor(self.adapter, lambda: self.rules, lambda: None)
        self.feed = AdapterFeed(self.adapter)
        self.cfg = replace(BBConfig("EURUSD", "M5"), anti_twin_enabled=True)
        time.sleep(snapshot_ms / 1000.0 + 0.3)
        self.spec = self.feed.spec("EURUSD", {}, time.time())
        self.at = AntiTwin(self.executor, d / "at.json", None, locked=lambda: False)
        self.lane = AntiTwinLane(lambda: self.at, self.feed, lambda s, tf: (self.spec, self.cfg),
                                 lambda q, cfg: bool(q.valid and q.age_seconds is not None and q.age_seconds <= 5.0), self.feed.closed_info)
        self.stop = threading.Event()
        self.lane.start(self.stop)

    def close(self):
        self.stop.set()
        self._clock_patch.stop()
        self.bridge.stop()
        self._tmp.cleanup()

    def one_run(self, rng, side="buy"):
        b = self.bridge
        b.positions.clear()
        b.bid, b.ask = 1.10000, 1.10012
        time.sleep(b.snapshot_s + b.poll_s + 0.1)                      # let the EA publish the reset
        px = b.ask if side == "buy" else b.bid
        sign = 1 if side == "buy" else -1
        out = self.executor.open("EURUSD", side, 0.01, px - sign * 0.0050, sl=round(px - sign * 0.0050, 5), tp=round(px + sign * 0.0020, 5))
        assert out.executed, out.message
        self.at.adopt({"ticket": out.ticket, "candle_id": "c", "symbol": "EURUSD", "timeframe": "M5"}, self.cfg, self.feed.open_positions(), self.spec)
        n = len(self.at.latency_log)
        time.sleep(rng.uniform(0.0, 0.6))                              # a random phase against the EA's timer
        t0 = time.perf_counter()
        move = -0.0003 * sign                                          # 3 pips against the position
        b.bid, b.ask = round(b.bid + move, 5), round(b.ask + move, 5)  # the broker's price moves NOW; the EA will publish it at its next snapshot
        deadline = time.time() + 15
        while len(self.at.latency_log) <= n and time.time() < deadline:
            time.sleep(0.005)
        if len(self.at.latency_log) <= n:
            return None
        rec = self.at.latency_log[-1]
        opens = [c for c in b.commands_seen if c.get("cmd") == "open"]
        return {"move_to_detect_ms": (rec["t_detect"] - t0) * 1000, "move_to_twin_ms": (rec["t_open_done"] - t0) * 1000, **rec, "outcome": rec["outcome"], "opens": len(opens)}


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def measure(snapshot_ms, poll_ms, broker_ms, runs, seed=11):
    h = Harness(snapshot_ms, poll_ms, broker_ms)
    rng = random.Random(seed)
    rows = []
    try:
        for i in range(runs):
            r = h.one_run(rng, "buy" if i % 2 == 0 else "sell")
            if r:
                rows.append(r)
        assert all(r["outcome"] == "twin_opened" for r in rows), [r["outcome"] for r in rows]
    finally:
        h.close()
    return rows


def summarise(rows):
    def col(k):
        v = [r[k] for r in rows if r.get(k) is not None]
        return {"n": len(v), "median": round(statistics.median(v)), "p90": round(pct(v, 90)), "max": round(max(v))}
    return {k: col(k) for k in ("move_to_detect_ms", "detect_to_close_ms", "close_to_open_sent_ms", "open_roundtrip_ms", "detect_to_open_ms", "move_to_twin_ms")}


CONFIGS = (("EA as shipped      (UpdateIntervalMs=1000, CommandPollMs=200)", 1000, 200),
           ("recommended        (UpdateIntervalMs=250,  CommandPollMs=100)", 250, 100),
           ("fastest sensible   (UpdateIntervalMs=100,  CommandPollMs=100)", 100, 100))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--broker-ms", type=int, default=50)
    a = ap.parse_args()
    print(f"Simulated bridge, emulated broker execution delay {a.broker_ms} ms per trade command, {a.runs} reversals per setting (BUY and SELL alternating).")
    print("All values in milliseconds: median / p90 / max.\n")
    for name, snap, poll in CONFIGS:
        s = summarise(measure(snap, poll, a.broker_ms, a.runs))
        print(name)
        for k, label in (("move_to_detect_ms", "price moved -> TradeMate saw it"), ("detect_to_close_ms", "seen -> original closed+confirmed"),
                         ("close_to_open_sent_ms", "closed -> opposite order sent"), ("open_roundtrip_ms", "opposite order (Risk gate+fill)"),
                         ("detect_to_open_ms", "seen -> opposite open+confirmed"), ("move_to_twin_ms", "price moved -> opposite open (TOTAL)")):
            v = s[k]
            print(f"   {label:40s} {v['median']:6d} / {v['p90']:6d} / {v['max']:6d}")
        print()


if __name__ == "__main__":
    main()
