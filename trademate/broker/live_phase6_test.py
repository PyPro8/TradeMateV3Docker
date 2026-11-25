"""LIVE Phase 6 integration test against your connected MT5 DEMO account.

Run (from the TradeMate folder, MT5 open, TradeMateBridge EA running, TradeMate server NOT required):

    python -m trademate.broker.live_phase6_test [SYMBOL] [--manual-g] [--yes]

What it does differently from the older `live_test`: every order goes through TradeMate's REAL web routes
(/api/order/review, /api/order, /api/position/<t>/modify, /api/position/<t>/close), so the Risk Engine and the
execution safeguards are exercised exactly as in the app. Nothing here can bypass them.

Safety rules enforced in code:
  * The account must be positively verified as DEMO, otherwise NO order is sent.
  * One run at a time (lock file), so two runs cannot duplicate demo orders.
  * Every order carries a stop-loss and take-profit. Minimum broker volume. Your saved risk limits are NOT changed.
    If the Risk Engine refuses the smallest test trade, the test is SKIPPED, never forced.
  * Only tickets this run created itself are ever modified or closed. Pre-existing positions are snapshotted first
    and must be unchanged at the end.
  * An 'unknown' execution outcome ABORTS the run: nothing is retried, nothing further is sent.
  * Leftover test positions are closed at the end; any that cannot be closed are reported loudly.
  * Every test is logged to data/live_tests/phase6-<time>.jsonl (purpose, inputs, response, ticket, status).

Real-broker scenarios that cannot be forced safely (lost answer / timeout, broker-side rejection, partial fill) are
listed as SKIPPED. Those are covered only by the simulated tests (tests/test_phase6.py), and this report says so.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from trademate.broker import ConnectionState
from trademate.errors import TradeMateError

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"


class Recorder:
    """Keeps every result, prints it, and appends it to a JSON-lines log."""

    def __init__(self, path: Path):
        self.path = path
        self.items: list[dict] = []
        path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, group: str, name: str, status: str, detail: str = "", *, purpose: str = "", inputs=None,
            response=None, ticket=None) -> str:
        item = {"time": datetime.now().isoformat(timespec="seconds"), "group": group, "name": name, "status": status,
                "detail": detail, "purpose": purpose, "inputs": inputs, "response": response, "ticket": ticket}
        self.items.append(item)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, default=str) + "\n")
        print(f"  [{status}] {name}" + (f"  - {detail}" if detail else ""))
        return status

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i["status"] == status)


class Abort(Exception):
    """Stop the run now (unknown outcome, lost connection). Cleanup still happens."""


class LiveRun:
    def __init__(self, app_module, rec: Recorder, symbol: str):
        self.m = app_module
        self.rec = rec
        self.symbol = symbol
        self.client = app_module.app.test_client()
        self.created: set[int] = set()     # tickets THIS run opened: the only ones it may touch
        self.closed: set[int] = set()

    # ---- small helpers ----
    @property
    def adapter(self):
        return self.m.adapter

    def get(self, url):
        return self.client.get(url).get_json()

    def post(self, url, body=None, raw=None):
        if raw is not None:
            return self.client.post(url, data=raw, content_type="application/json").get_json()
        return self.client.post(url, json=body if body is not None else {}).get_json()

    def positions(self) -> dict[int, dict]:
        return {p["ticket"]: p for p in self.get("/api/broker")["positions"]}

    def spec(self):
        return self.adapter.get_symbol_spec(self.symbol)

    def levels(self, side, info, dist_pts):
        pt, d = info.point, dist_pts * info.point
        entry = info.ask if side == "buy" else info.bid
        sl = entry - d if side == "buy" else entry + d
        tp = entry + 2 * d if side == "buy" else entry - 2 * d
        return round(sl, info.digits), round(tp, info.digits), entry

    def near(self, a, b, info, k=1.5):
        return a is not None and b is not None and abs(a - b) <= k * info.point

    # ---- test groups ----
    def connection(self) -> bool:
        g = "MT5 connection (real)"
        st = self.adapter.get_connection_status()
        if st.state is not ConnectionState.CONNECTED:
            self.rec.add(g, "terminal connected", FAIL, f"{st.state.value}: {st.message}")
            return False
        self.rec.add(g, "terminal connected", PASS, st.message, purpose="bridge heartbeat")
        acct = self.adapter.get_account()
        if acct.environment != "demo":
            self.rec.add(g, "account is DEMO", FAIL, f"environment={acct.environment!r}: ORDER TESTS DISABLED")
            return False
        self.rec.add(g, "account is DEMO", PASS, f"login {acct.login}, {acct.server}, {acct.currency}",
                     purpose="refuse to trade on anything not verified DEMO")
        info = self.spec()
        ok = all(v is not None for v in (info.volume_min, info.volume_step, info.tick_size, info.tick_value,
                                         info.point, info.digits, info.bid, info.ask))
        self.rec.add(g, f"{self.symbol} specification and live price", PASS if ok else FAIL,
                     f"min {info.volume_min}, step {info.volume_step}, tick {info.tick_size}, stops_level {info.stops_level}, "
                     f"bid/ask {info.bid}/{info.ask}", response=info.to_dict())
        if not ok:
            return False
        if info.trade_mode != 4:
            self.rec.add(g, "symbol fully tradable", SKIP, f"trade_mode={info.trade_mode} (4 = full access). Market closed or "
                         "symbol restricted: order tests skipped")
            return False
        self.rec.add(g, "symbol fully tradable", PASS)
        for name, url in (("risk state route", "/api/risk/state"), ("trade history route", "/api/history?days=1")):
            try:
                r = self.get(url)
                self.rec.add(g, name, PASS if r else FAIL, purpose="read-only route works against the real terminal")
            except Exception as exc:  # noqa: BLE001
                self.rec.add(g, name, FAIL, str(exc))
        if self.get("/api/broker")["executions_unresolved"]:
            self.rec.add(g, "no unresolved executions", FAIL, "an earlier order outcome is still unknown: reconcile it "
                         "in TradeMate before running order tests")
            return False
        self.rec.add(g, "no unresolved executions", PASS)
        return True

    def review_and_refusals(self, dist_pts):
        g = "Risk gate (real prices, nothing may reach MT5)"
        info = self.spec()
        sl, tp, _ = self.levels("buy", info, dist_pts)
        before = set(self.positions())
        body = {"symbol": self.symbol, "side": "buy", "volume": info.volume_min, "sl": sl, "tp": tp}
        r = self.post("/api/order/review", body)
        ok = bool(r.get("ok") and r.get("order_sent") is False)
        self.rec.add(g, "review sends nothing and reports estimates", PASS if ok and r.get("estimates") else FAIL,
                     f"stage={r.get('stage')}, approved={r.get('approved')}, msg={r.get('message')}",
                     inputs=body, response=r, purpose="order review is read-only")
        self.rec.add(g, "no position created by review", PASS if set(self.positions()) == before else FAIL)

        far = round(info.ask * 0.8, info.digits)                  # absurdly wide SL: risk far above any sane limit
        refusals = [
            ("risk limit exceeded (SL 20% away)", {"sl": far}, ("rejected_risk",)),
            ("missing stop-loss", {"sl": None}, ("failed", "rejected_risk")),
            ("invalid direction", {"side": "hold"}, ("failed",)),
            ("zero volume", {"volume": 0}, ("failed",)),
            ("BUY with SL above price", {"sl": round(info.ask + 100 * info.point, info.digits)}, ("failed", "rejected_risk")),
        ]
        for name, over, states in refusals:
            b = {**body, **over}
            r = self.post("/api/order", b)
            ok = r.get("state") in states and r.get("order_sent", False) is False
            self.rec.add(g, f"refused: {name}", PASS if ok else FAIL, f"state={r.get('state')}: {r.get('message')}",
                         inputs=b, response=r, purpose="must be refused before reaching MT5")
        for raw in ('{"symbol":"%s","side":"buy","volume":NaN,"sl":%s}' % (self.symbol, sl),
                    '{"symbol":"%s","side":"buy","volume":%s,"sl":Infinity}' % (self.symbol, info.volume_min)):
            r = self.post("/api/order", raw=raw)
            self.rec.add(g, "refused: NaN/Infinity in JSON", PASS if r.get("state") == "failed" else FAIL,
                         f"state={r.get('state')}", inputs=raw, response=r)
        after = set(self.positions())
        self.rec.add(g, "no position created by any refused request", PASS if after == before else FAIL,
                     f"new tickets: {sorted(after - before)}")
        if after - before:                                         # should be impossible; never leave it open
            self.created |= (after - before)

    def full_cycle(self, side, dist_pts):
        g = f"Real DEMO order: {side.upper()}"
        info = self.spec()
        sl, tp, ref = self.levels(side, info, dist_pts)
        body = {"symbol": self.symbol, "side": side, "volume": info.volume_min, "sl": sl, "tp": tp}
        rv = self.post("/api/order/review", body)
        if not rv.get("approved"):
            self.rec.add(g, "Risk Engine approval of the smallest test trade", SKIP,
                         f"{rv.get('stage')}: {rv.get('message')} The Risk Engine was NOT bypassed; "
                         "use another SYMBOL or adjust your own saved limits, then re-run.", inputs=body, response=rv)
            return
        est = rv.get("estimates") or {}
        self.rec.add(g, "Risk Engine approved", PASS, f"volume {rv['approved_volume']}, est. risk {est.get('risk_amount')} "
                     f"{rv.get('currency')}, reference price {rv['order']['entry']}", inputs=body, response=rv)

        before = self.positions()
        r = self.post("/api/order", body)
        if r.get("state") == "unknown":
            self.rec.add(g, "order outcome", FAIL, "UNKNOWN outcome: run aborted, nothing retried. Check the MT5 Trade tab and "
                         "TradeMate's unresolved list before doing anything else.", inputs=body, response=r)
            raise Abort("unknown execution outcome")
        if r.get("state") != "executed":
            self.rec.add(g, "order outcome", FAIL, f"state={r.get('state')}: {r.get('message')}", inputs=body, response=r)
            return
        res = r.get("result") or {}
        ticket = res.get("ticket")
        if not isinstance(ticket, int) or ticket in before:
            self.rec.add(g, "order outcome", FAIL, "executed, but no NEW ticket was returned", inputs=body, response=r)
            return
        self.created.add(ticket)
        pos = (res.get("position") or {})
        self.rec.add(g, "executed and confirmed by MT5", PASS, f"ticket {ticket}, retcode {res.get('retcode')}",
                     inputs=body, response=r, ticket=ticket, purpose="actual MT5 result decides the status")

        live = self.positions().get(ticket)
        self.rec.add(g, "position synchronised from MT5", PASS if live else FAIL, ticket=ticket)
        if not live:
            return
        self.rec.add(g, "direction and approved volume match", PASS if live["direction"] == side and
                     abs(live["volume"] - rv["approved_volume"]) < 1e-9 else FAIL,
                     f"{live['direction']} {live['volume']} (approved {rv['approved_volume']})", ticket=ticket)
        self.rec.add(g, "SL/TP placed at open (never unprotected)",
                     PASS if self.near(live["stop_loss"], sl, info) and self.near(live["take_profit"], tp, info) else FAIL,
                     f"requested SL {sl} TP {tp}; broker SL {live['stop_loss']} TP {live['take_profit']}", ticket=ticket)
        self.rec.add(g, "estimated vs actual entry price", INFO,
                     f"reference {ref}, MT5 actual {live['price_open']}, difference {abs(live['price_open'] - ref):.{info.digits}f}",
                     ticket=ticket)
        self.rec.add(g, "floating P/L is reported by MT5", PASS if live.get("profit") is not None else FAIL,
                     f"{live.get('profit')} {self.adapter.get_account().currency}", ticket=ticket)

        # modify SL, then TP: the confirmed values must be the broker's
        info = self.spec()
        sl2 = round(sl - 10 * info.point if side == "buy" else sl + 10 * info.point, info.digits)
        r = self.post(f"/api/position/{ticket}/modify", {"sl": sl2})
        p = (r.get("result") or {}).get("position") or {}
        self.rec.add(g, "modify SL confirmed by MT5", PASS if r.get("state") == "executed" and self.near(p.get("stop_loss"), sl2, info)
                     and abs((p.get("volume") or 0) - live["volume"]) < 1e-9 else FAIL,
                     f"requested {sl2}, broker {p.get('stop_loss')}, volume {p.get('volume')}; {r.get('message')}",
                     inputs={"sl": sl2}, response=r, ticket=ticket)
        self.rec.add(g, "Risk Engine re-evaluated after modify", PASS if "risk_report" in r else FAIL,
                     str((r.get("risk_report") or {}).get("breaches")), ticket=ticket)
        tp2 = round(tp + 10 * info.point if side == "buy" else tp - 10 * info.point, info.digits)
        r = self.post(f"/api/position/{ticket}/modify", {"tp": tp2})
        p = (r.get("result") or {}).get("position") or {}
        self.rec.add(g, "modify TP confirmed by MT5", PASS if r.get("state") == "executed" and self.near(p.get("take_profit"), tp2, info)
                     else FAIL, f"requested {tp2}, broker {p.get('take_profit')}; {r.get('message')}",
                     inputs={"tp": tp2}, response=r, ticket=ticket)

        # a deliberately invalid modification must be refused and leave the broker's values alone
        cur = self.positions().get(ticket)
        if cur:
            px = cur["price_current"] or cur["price_open"]
            bad = round(px + 100 * info.point if side == "buy" else px - 100 * info.point, info.digits)
            r = self.post(f"/api/position/{ticket}/modify", {"sl": bad})
            again = self.positions().get(ticket) or {}
            self.rec.add(g, "invalid SL modification refused, broker values unchanged",
                         PASS if r.get("state") != "executed" and self.near(again.get("stop_loss"), cur["stop_loss"], info) else FAIL,
                         f"state={r.get('state')}: {r.get('message')}; broker SL still {again.get('stop_loss')}",
                         inputs={"sl": bad}, response=r, ticket=ticket)

        self.close(ticket, g)

    def close(self, ticket, g="Cleanup"):
        if ticket not in self.created:                      # hard rule: never touch anything this run did not open
            self.rec.add(g, "refused to close a ticket this run did not create", FAIL, str(ticket))
            return
        r = self.post(f"/api/position/{ticket}/close", {})
        if r.get("state") == "unknown":
            self.rec.add(g, "close outcome", FAIL, f"UNKNOWN: {r.get('message')} Not retried.", response=r, ticket=ticket)
            raise Abort("unknown close outcome")
        time.sleep(1)
        gone = ticket not in self.positions()
        self.rec.add(g, "close confirmed and position gone from MT5", PASS if r.get("state") == "executed" and gone else FAIL,
                     f"state={r.get('state')}: {r.get('message')}; still listed: {not gone}", response=r, ticket=ticket,
                     purpose="positions only disappear after MT5 confirms")
        if gone:
            self.closed.add(ticket)
            h = json.dumps(self.get("/api/history?days=1"), default=str)
            self.rec.add(g, "closed trade visible in history", INFO if str(ticket) in h else INFO,
                         "found" if str(ticket) in h else "not listed yet (history can lag)", ticket=ticket)

    def manual_g(self, input_fn, snapshot):
        g = "Position opened outside TradeMate (real)"
        print("\n>>> Open ANY demo position now from the MT5 mobile app or another terminal, then press Enter.")
        input_fn("    (this test only READS it and will never modify or close it) ")
        new = {t: p for t, p in self.positions().items() if t not in snapshot and t not in self.created}
        self.rec.add(g, "external position appears in TradeMate", PASS if new else FAIL,
                     f"tickets {sorted(new)}. Close them yourself when done." if new else "nothing new was detected",
                     response=new)

    def cleanup(self):
        left = [t for t in self.created if t not in self.closed and t in self.positions()]
        for t in left:
            try:
                self.close(t, "Cleanup")
            except Abort:
                pass
            except Exception as exc:  # noqa: BLE001
                self.rec.add("Cleanup", "close leftover test position", FAIL, str(exc), ticket=t)
        return [t for t in self.created if t in self.positions()]


def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and time.time() - path.stat().st_mtime > 1800:
        path.unlink()                                          # a crashed run's lock, older than 30 minutes
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, f"{os.getpid()} {datetime.now().isoformat()}".encode())
    os.close(fd)
    return True


def main(argv=None, app_module=None, input_fn=input, data_dir=None) -> int:
    ap = argparse.ArgumentParser(prog="live_phase6_test")
    ap.add_argument("symbol", nargs="?", default="EURUSD")
    ap.add_argument("--manual-g", action="store_true", help="also test detection of a position you open elsewhere")
    ap.add_argument("--yes", action="store_true", help="skip the YES prompt")
    args = ap.parse_args(argv)
    if app_module is None:
        import app as app_module  # noqa: PLC0415 - loads TradeMate's own config and adapter

    data_dir = Path(data_dir if data_dir is not None else app_module.settings.app.data_dir)
    log = data_dir / "live_tests" / f"phase6-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    rec = Recorder(log)
    run = LiveRun(app_module, rec, args.symbol)
    print(f"Log: {log}\n\n== Connection ==")
    try:
        if not run.connection():
            return _report(rec, run, [], 1)
    except TradeMateError as exc:
        rec.add("MT5 connection (real)", "read account and symbol", FAIL, str(exc))
        return _report(rec, run, [], 1)

    acct = run.adapter.get_account()
    print(f"\nDEMO account {acct.login} on {acct.server}. This will place REAL DEMO orders: {args.symbol}, smallest lot, "
          "BUY then SELL, each with SL and TP, and close them.")
    if not args.yes and input_fn("Type YES to continue: ").strip() != "YES":
        print("Cancelled. Nothing was sent.")
        return 1
    lock = data_dir / "live_phase6.lock"
    if not _acquire_lock(lock):
        print("REFUSED: another live test run holds data/live_phase6.lock (delete it only if you are sure none is running).")
        return 1

    outstanding: list[int] = []
    try:
        info = run.spec()
        dist_pts = max(2 * (info.stops_level or 0) + 2 * (info.spread_points or 0), 50)
        snapshot = run.positions()
        snap_keys = {t: (p["stop_loss"], p["take_profit"], p["volume"], p["direction"]) for t, p in snapshot.items()}
        print(f"\n== Risk gate ==  ({len(snapshot)} pre-existing position(s) will not be touched)")
        run.review_and_refusals(dist_pts)
        for side in ("buy", "sell"):
            print(f"\n== {side.upper()} cycle ==")
            run.full_cycle(side, dist_pts)
        if args.manual_g:
            print("\n== External position ==")
            run.manual_g(input_fn, snapshot)
        now = run.positions()
        changed = [t for t, k in snap_keys.items()
                   if t not in now or (now[t]["stop_loss"], now[t]["take_profit"], now[t]["volume"], now[t]["direction"]) != k]
        rec.add("Isolation", "pre-existing positions untouched", PASS if not changed else FAIL,
                f"changed/missing: {changed}" if changed else f"{len(snap_keys)} unchanged",
                purpose="the test must never alter positions it did not create")
    except Abort as exc:
        print(f"\nRUN ABORTED: {exc}")
    except TradeMateError as exc:
        rec.add("Run", "unexpected TradeMate error", FAIL, str(exc))
    finally:
        outstanding = run.cleanup()
        try:
            lock.unlink()
        except OSError:
            pass
    skipped = [("Duplicate-order protection after a lost answer / timeout", "cannot be forced safely on a real broker: SIMULATED ONLY"),
               ("Real broker-side order rejection", "no safe way to force one without bypassing the Risk Engine: SIMULATED ONLY"),
               ("Partial fill", "not supported by the bridge")]
    for name, why in skipped:
        rec.add("Not tested on the real broker", name, SKIP, why)
    return _report(rec, run, outstanding, 0)


def _report(rec: Recorder, run: LiveRun, outstanding: list[int], base: int) -> int:
    print("\n==================== REPORT ====================")
    print(f"PASS {rec.count(PASS)}   FAIL {rec.count(FAIL)}   SKIP {rec.count(SKIP)}   INFO {rec.count(INFO)}")
    for i in rec.items:
        if i["status"] in (FAIL, SKIP):
            print(f"  [{i['status']}] {i['group']}: {i['name']}  {i['detail']}")
    print("Test-created tickets:", sorted(run.created) or "none")
    if outstanding:
        print(f"\n!!! ATTENTION: test positions STILL OPEN in MT5: {sorted(outstanding)}. Close them manually.")
    else:
        print("No test-created positions remain open.")
    print(f"Full log: {rec.path}")
    placed = bool(run.created)
    print("Orders were " + ("sent through the connected bridge in this run." if placed else "NOT sent in this run.")
          + " (Whether that bridge is your real Headway DEMO terminal is up to where you ran this.)")
    return 1 if (rec.count(FAIL) or outstanding or base) else 0


if __name__ == "__main__":
    sys.exit(main())
