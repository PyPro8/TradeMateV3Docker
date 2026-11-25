"""REAL DEMO EXECUTION TEST: places ONE real order on your connected Headway MT5 DEMO account and leaves it OPEN.

This is NOT part of the ordinary unit tests and can never run by accident. It needs BOTH:

    1. the environment variable   TRADEMATE_REAL_DEMO_ORDER=YES_PLACE_ONE_DEMO_ORDER
    2. the flag                   --place-real-demo-order

    TRADEMATE_REAL_DEMO_ORDER=YES_PLACE_ONE_DEMO_ORDER python -m trademate.broker.real_demo_execution_test EURUSD --place-real-demo-order

What it does, in order (it stops at the first failed safety check, and then NOTHING has been sent):
  1. Verifies MT5 is reachable, the broker connection is live, and the account is a DEMO account on the server TradeMate
     expects (`[mt5] expected_server` in config/settings.toml, or --expect-server). Live / unverifiable / wrong server: refused.
  2. Verifies the symbol is tradable, reads its real specification, and checks the live price is usable.
  3. Builds ONE order: the broker's minimum volume, with an SL and TP derived from the broker's own stop distances.
  4. Sends it through TradeMate's normal routes, so the Risk Engine judges it first. It is never bypassed or resized here.
  5. Verifies the real MT5 result, re-reads the position from MT5, checks it is THIS test's order, and checks entry / SL / TP.
  6. Leaves the verified position OPEN and tells you how to find it on the MT5 chart. It NEVER closes anything.

If the outcome is uncertain it reports UNKNOWN, reconciles by READING MT5 only, and never sends a second order.
Every step is logged to data/live_tests/real-demo-<time>.jsonl (no credentials are ever read or logged).
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
from trademate.broker.live_phase6_test import _acquire_lock
from trademate.broker.server_check import check_server
from trademate.errors import TradeMateError

ENV_NAME, ENV_VALUE = "TRADEMATE_REAL_DEMO_ORDER", "YES_PLACE_ONE_DEMO_ORDER"
OK, BAD = "PASS", "FAIL"


class _Log:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **data):
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "event": event, **data}, default=str) + "\n")


def block(title: str, rows: list[tuple[str, object]], log: _Log | None = None) -> None:
    """One readable report block. Values are always real data passed in by the caller."""
    print(f"\n[{title}]\n")
    for key, value in rows:
        print(f"{key}: {value}")
    if log:
        log.write(title, **{k: v for k, v in rows})


def _near(a, b, info, k=1.5) -> bool:
    return a is not None and b is not None and abs(a - b) <= k * info.point


def _refuse(log, title, rows):
    block(title, rows + [("Order submitted to MT5", "NO")], log)
    return 1


def main(argv=None, app_module=None, input_fn=input, data_dir=None, environ=None) -> int:
    environ = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(prog="real_demo_execution_test")
    ap.add_argument("symbol", nargs="?", default="EURUSD")
    ap.add_argument("--side", choices=("buy", "sell"), default="buy")
    ap.add_argument("--place-real-demo-order", action="store_true", help="REQUIRED: explicit opt-in to place one real DEMO order")
    ap.add_argument("--expect-server", default=None, help="expected MT5 server (default: [mt5] expected_server from settings.toml)")
    ap.add_argument("--yes", action="store_true", help="skip the final YES prompt")
    args = ap.parse_args(argv)

    # ---- gate 0: explicit authorization. Nothing, not even a read, happens without it. ----
    if not args.place_real_demo_order or environ.get(ENV_NAME) != ENV_VALUE:
        print("[NOT AUTHORIZED]\n\nThis test places a REAL order on your DEMO account, so it needs explicit opt-in:")
        print(f"  environment variable : {ENV_NAME}={ENV_VALUE}   ({'set' if environ.get(ENV_NAME) == ENV_VALUE else 'MISSING'})")
        print(f"  command-line flag    : --place-real-demo-order   ({'given' if args.place_real_demo_order else 'MISSING'})")
        print("Order submitted to MT5: NO")
        return 2

    if app_module is None:
        import app as app_module  # noqa: PLC0415 - loads TradeMate's own config, risk rules and MT5 adapter
    data_dir = Path(data_dir if data_dir is not None else app_module.settings.app.data_dir)
    log = _Log(data_dir / "live_tests" / f"real-demo-{datetime.now():%Y%m%d-%H%M%S}.jsonl")
    adapter = app_module.adapter
    client = app_module.app.test_client()          # the REAL routes: Risk Engine gate, journal, execution engine
    symbol = args.symbol
    print(f"Log: {log.path}")

    # ---- gate 1: MT5 reachable, broker connection live, account verified as DEMO on the expected server ----
    try:
        status = adapter.get_connection_status()
        if status.state is not ConnectionState.CONNECTED:
            return _refuse(log, "MT5 CONNECTION FAILURE", [("State", status.state.value), ("Reason", status.message)])
        if status.age_seconds is not None and status.age_seconds > 15:
            return _refuse(log, "MT5 CONNECTION FAILURE", [("Reason", f"MT5 data is {status.age_seconds:.0f}s old: not live")])
        acct = adapter.get_account()
    except TradeMateError as exc:
        return _refuse(log, "MT5 CONNECTION FAILURE", [("Reason", str(exc))])

    expected = args.expect_server or getattr(app_module.settings.mt5, "expected_server", "") or ""
    env = (acct.environment or "").lower()
    if env != "demo":
        return _refuse(log, "LOCAL CONFIGURATION FAILURE: ACCOUNT NOT VERIFIED AS DEMO",
                       [("Account environment reported by MT5", env or "(unknown)"), ("Login", acct.login), ("Server", acct.server),
                        ("Reason", "Only an account that MT5 itself reports as DEMO is allowed. Nothing was sent.")])
    server_ok, server_reason = check_server(expected, acct.server)       # same rule the order guard uses
    if not server_ok:
        return _refuse(log, "LOCAL CONFIGURATION FAILURE: SERVER NOT VERIFIED",
                       [("Connected server", acct.server or "(not reported)"), ("Expected server", expected or "(not configured)"),
                        ("Reason", server_reason)])
    if acct.free_margin is None or acct.free_margin <= 0:
        return _refuse(log, "LOCAL CONFIGURATION FAILURE: NO FREE MARGIN", [("Free margin", acct.free_margin)])

    # ---- gate 2: symbol tradable, real specification, usable price ----
    try:
        info = adapter.get_symbol_spec(symbol)
    except TradeMateError as exc:
        return _refuse(log, "LOCAL CONFIGURATION FAILURE: SYMBOL", [("Symbol", symbol), ("Reason", str(exc))])
    needed = (info.volume_min, info.volume_step, info.point, info.digits, info.tick_size, info.tick_value, info.bid, info.ask)
    if any(v is None for v in needed):
        return _refuse(log, "LOCAL CONFIGURATION FAILURE: INCOMPLETE SYMBOL SPECIFICATION", [("Symbol", symbol)])
    if info.trade_mode != 4:
        return _refuse(log, "LOCAL CONFIGURATION FAILURE: SYMBOL NOT FULLY TRADABLE",
                       [("Symbol", symbol), ("trade_mode", f"{info.trade_mode} (4 = full access; market closed or restricted)")])
    if not (info.bid > 0 and info.ask > 0 and info.ask >= info.bid):
        return _refuse(log, "LOCAL CONFIGURATION FAILURE: UNUSABLE PRICE", [("Symbol", symbol), ("Bid/Ask", f"{info.bid}/{info.ask}")])
    journal = getattr(adapter, "journal", None)
    if journal is not None and journal.unresolved():
        return _refuse(log, "LOCAL CONFIGURATION FAILURE: UNRESOLVED EARLIER ORDER",
                       [("Reason", "An earlier order outcome is still UNKNOWN. Reconcile it in TradeMate first.")])

    # ---- the one order: broker minimum volume, SL/TP from the broker's own stop distances ----
    side = args.side
    dist = max(2 * (info.stops_level or 0) + 2 * (info.spread_points or 0), 50) * info.point
    entry_ref = info.ask if side == "buy" else info.bid
    sl = round(entry_ref - dist if side == "buy" else entry_ref + dist, info.digits)
    tp = round(entry_ref + 2 * dist if side == "buy" else entry_ref - 2 * dist, info.digits)
    body = {"symbol": symbol, "side": side, "volume": info.volume_min, "sl": sl, "tp": tp}
    block("VERIFIED PRE-FLIGHT", [("Account environment", "DEMO"), ("Broker/server", f"{acct.broker} / {acct.server}"),
                                  ("Login", acct.login), ("Symbol", symbol), ("Direction", side.upper()),
                                  ("Volume (broker minimum)", info.volume_min), ("Reference price", entry_ref),
                                  ("Stop-loss", sl), ("Take-profit", tp)], log)
    if not args.yes and input_fn("\nType YES to place this ONE real DEMO order: ").strip() != "YES":
        print("Cancelled. Order submitted to MT5: NO")
        return 1
    lock = data_dir / "live_phase6.lock"
    if not _acquire_lock(lock):
        print("REFUSED: another live test holds data/live_phase6.lock. Order submitted to MT5: NO")
        return 1
    try:
        return _place_and_verify(client, adapter, app_module, info, acct, body, log)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _positions(client) -> dict:
    return {p["ticket"]: p for p in client.get("/api/broker").get_json()["positions"]}


def _place_and_verify(client, adapter, app_module, info, acct, body, log) -> int:
    symbol, side = body["symbol"], body["side"]
    before = set(_positions(client))
    review = client.post("/api/order/review", json=body).get_json()
    log.write("review", response=review)
    if not review.get("approved"):
        return _risk_report(log, body, review, review.get("message"))

    resp = client.post("/api/order", json=body).get_json()          # exactly ONE submission, never repeated
    log.write("order_response", response=resp)
    outcome = resp.get("outcome")

    if outcome == "risk_rejected":
        return _risk_report(log, body, resp, resp.get("message"))
    if outcome == "broker_rejected":
        b = resp.get("broker") or {}
        block("MT5/BROKER REJECTED", [("Symbol", symbol), ("Direction", side.upper()), ("Order submitted", "YES"),
                                      ("Broker accepted", "NO"), ("Retcode", b.get("retcode")),
                                      ("Reason", b.get("message") or resp.get("message"))], log)
        return 1
    if outcome in ("execution_refused", "invalid_request"):
        block("EXECUTION ENGINE REFUSED" if outcome == "execution_refused" else "LOCAL VALIDATION FAILURE",
              [("Symbol", symbol), ("Direction", side.upper()), ("Reason", resp.get("message")), ("Order submitted to MT5", "NO")], log)
        return 1
    if outcome in ("connection_failed", "internal_error"):
        block("MT5 CONNECTION FAILURE" if outcome == "connection_failed" else "TRADEMATE INTERNAL ERROR",
              [("Symbol", symbol), ("Reason", resp.get("message")),
               ("Order submitted to MT5", "NO" if resp.get("order_submitted") is False else "UNKNOWN")], log)
        return 1
    if outcome in ("unknown", "not_executed") or resp.get("state") == "unknown":
        return _unknown_report(client, adapter, log, body, before, resp)
    if outcome not in ("executed", "accepted_unconfirmed"):
        block("EXECUTION OUTCOME UNKNOWN", [("Reason", f"Unrecognised outcome {outcome!r}: {resp.get('message')}"),
                                            ("Automatic resubmission", "DISABLED")], log)
        return 1

    # accepted by MT5: now PROVE the position from MT5's own state, never from the response alone
    result = resp.get("result") or {}
    ticket = result.get("ticket")
    live = None
    for _ in range(10 if outcome == "accepted_unconfirmed" else 3):
        live = _positions(client).get(ticket) if isinstance(ticket, int) else None
        if live:
            break
        time.sleep(2)
    if live is None:
        block("ORDER ACCEPTED, POSITION NOT YET CONFIRMED",
              [("Symbol", symbol), ("Direction", side.upper()), ("Reported ticket", ticket), ("MT5 confirmation", "PASS" if outcome == "executed" else "PENDING"),
               ("Position reconciliation", "FAIL: the ticket is not in MT5's positions yet"), ("Automatic resubmission", "DISABLED"),
               ("Next", "Look at the MT5 Trade tab and re-run nothing: do NOT send this order again.")], log)
        return 1

    problems = []
    journal = getattr(adapter, "journal", None)
    rec = journal.get(resp.get("execution_id")) if journal is not None and resp.get("execution_id") else None
    if ticket in before:
        problems.append(f"ticket {ticket} existed BEFORE this test: it cannot be this test's order")
    if rec is None or rec.get("ticket") != ticket or rec.get("symbol") != symbol or rec.get("side") != side:
        problems.append("TradeMate's execution journal does not tie this ticket to this test's order")
    if live["symbol"] != symbol or live["direction"] != side:
        problems.append(f"MT5 reports {live['symbol']} {live['direction']}, test sent {symbol} {side}")
    if live["volume"] is None or abs(live["volume"] - body["volume"]) > 1e-9:
        problems.append(f"MT5 volume {live['volume']} differs from approved {body['volume']}")
    if not _near(live["stop_loss"], body["sl"], info):
        problems.append(f"stop-loss: MT5 has {live['stop_loss']}, approved {body['sl']}")
    if not _near(live["take_profit"], body["tp"], info):
        problems.append(f"take-profit: MT5 has {live['take_profit']}, approved {body['tp']}")
    chart_ok = all(isinstance(live.get(k), (int, float)) for k in ("price_open", "stop_loss", "take_profit")) and live["direction"] in ("buy", "sell")
    if not chart_ok:
        problems.append("the position data lacks entry/SL/TP the chart overlay needs: no chart levels would be drawn")

    rows = [("Account environment", "DEMO"), ("Broker/server", f"{acct.broker} / {acct.server}"), ("Symbol", live["symbol"]),
            ("Direction", live["direction"].upper()), ("Ticket", ticket), ("Volume", live["volume"]),
            ("Entry price", live["price_open"]), ("Stop-loss", live["stop_loss"]), ("Take-profit", live["take_profit"]),
            ("Floating P/L", f"{live.get('profit')} {acct.currency}")]
    if problems:
        block("ORDER ACCEPTED BUT NOT FULLY VERIFIED", rows + [("Position status", "OPEN, NOT FULLY VERIFIED"),
              ("MT5 confirmation", "PASS" if outcome == "executed" else "PENDING"), ("Position reconciliation", "FAIL")] +
              [("Discrepancy", p) for p in problems] +
              [("Action", "The position is still OPEN. Inspect it in MT5 and manage it manually; TradeMate did not touch it.")], log)
        return 1
    block("REAL DEMO TRADE VERIFIED", rows + [("Position status", "OPEN AND VERIFIED"), ("MT5 confirmation", "PASS"),
          ("Position reconciliation", "PASS"), ("Stop-loss/take-profit at broker", "PASS"), ("Chart overlay data", "PASS"),
          ("Identified as this test's order", f"yes: new ticket, tied to TradeMate execution {resp.get('execution_id')}")], log)
    print(f"\nOpen the {live['symbol']} chart in MT5 and inspect the position:")
    print(f"  - MT5: Trade tab, ticket #{ticket}. The chart shows the entry line plus the SL and TP levels above.")
    print(f"  - Optional: right-click the Trade tab > Columns > Magic. TradeMate orders carry magic {app_module.settings.trading.magic}.")
    print(f"  - TradeMate: open the {live['symbol']} chart; the entry line shows '{live['direction'].upper()} <floating P/L>' with the SL/TP lines.")
    print("This position was left OPEN on purpose. TradeMate will not close it; close it yourself when you have checked it.")
    return 0


def _risk_report(log, body, payload, reason) -> int:
    d = payload.get("risk") or (payload.get("decision") or {})
    cur = payload.get("currency") or (payload.get("risk") or {}).get("currency") or ""
    req = d.get("requested_risk_amount", d.get("trade_risk_amount"))
    mx = d.get("max_risk_amount", d.get("effective_max_risk_amount"))
    rows = [("Symbol", body["symbol"]), ("Direction", body["side"].upper()), ("Reason", reason)]
    if req is not None:
        rows.append(("Requested risk", f"{req:.2f} {cur}".strip()))
    if mx is not None:
        rows.append(("Maximum permitted risk", f"{mx:.2f} {cur}".strip()))
    block("TRADEMATE REJECTED", rows + [("Order submitted to MT5", "NO")], log)
    return 1


def _unknown_report(client, adapter, log, body, before, resp) -> int:
    block("EXECUTION OUTCOME UNKNOWN",
          [("Symbol", body["symbol"]), ("Direction", body["side"].upper()),
           ("Detail", "TradeMate cannot yet confirm whether the broker accepted the order."), ("Reason", resp.get("message")),
           ("Automatic resubmission", "DISABLED"), ("Blind retry", "NOT PERMITTED"),
           ("Next action", "Reconcile against MT5 orders, positions, and deal history.")], log)
    candidates = []
    for _ in range(8):                                             # READ-ONLY reconciliation: look, never send
        candidates = [p for t, p in _positions(client).items() if t not in before and p["symbol"] == body["symbol"]
                      and p["direction"] == body["side"] and abs((p["volume"] or 0) - body["volume"]) < 1e-9]
        if candidates:
            break
        time.sleep(2)
    if candidates:
        print("\nRead-only reconciliation: MT5 now shows a NEW matching position:")
        for p in candidates:
            print(f"  ticket #{p['ticket']}  {p['symbol']} {p['direction'].upper()} {p['volume']} @ {p['price_open']}  SL {p['stop_loss']}  TP {p['take_profit']}")
        print("It could not be tied to this order with certainty. Verify it in MT5. Nothing was resent or closed.")
    else:
        print("\nRead-only reconciliation: no new matching position yet. Check MT5 (Trade tab and History) before doing anything.")
        print("Do NOT re-run this test until you know whether an order is already open.")
    log.write("unknown_reconcile", candidates=candidates)
    return 1


if __name__ == "__main__":
    sys.exit(main())
