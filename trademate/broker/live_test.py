"""LIVE end-to-end execution test against your real MT5 DEMO account.

Run:  python -m trademate.broker.live_test [SYMBOL]

This is the proof that TradeMate -> MT5 -> Headway works. It places REAL orders on the DEMO account
(smallest lot), so it asks you to type YES first. It refuses to run on a non-demo account.

Steps: BUY -> modify TP -> modify SL -> close, then SELL -> close. After every step it re-reads the
broker's account state and checks it. Compare each step with the MT5 Trade tab as it runs.
"""

import sys
import time

from trademate.broker import ConnectionState, create_adapter
from trademate.config import load_config
from trademate.errors import ConfigError, TradeMateError


def step(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""))
    return ok


def main() -> int:
    try:
        settings = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    adapter = create_adapter(settings)

    status = adapter.get_connection_status()
    if status.state is not ConnectionState.CONNECTED:
        print(f"MT5 is {status.state.value}: {status.message}")
        return 1
    account = adapter.get_account()
    if account.environment != "demo":
        print("REFUSED: not a DEMO account.")
        return 1
    symbols = {s.name: s for s in adapter.get_symbols()}
    wanted = sys.argv[1] if len(sys.argv) > 1 else next(iter(symbols), "")
    try:  # works for ANY broker symbol: the bridge selects it on demand
        info = adapter.get_symbol_info(wanted)
    except TradeMateError as exc:
        print(f"Symbol {wanted!r}: {exc}")
        return 1
    lot = info.volume_min or 0.01

    print(f"DEMO account {account.login} on {account.server} ({account.broker})")
    print(f"This will place REAL DEMO orders: {wanted}, {lot} lot, BUY then SELL, and close them.")
    if input("Type YES to continue: ").strip() != "YES":
        print("Cancelled. Nothing was sent.")
        return 1

    results = []
    point = info.point or 1e-5
    dist = max(100, (info.stops_level or 0) * 3 + (info.spread_points or 0) * 3) * point
    digits = info.digits or 5

    try:
        for side in ("buy", "sell"):
            print(f"\n== {side.upper()} ==")
            before = {p.ticket for p in adapter.get_positions()}
            r = adapter.place_order(wanted, side, lot)
            results.append(step(f"{side.upper()} accepted and confirmed by account state", r.confirmed,
                                f"ticket {r.ticket}, retcode {r.retcode}"))
            if not r.confirmed:
                continue
            ticket = r.ticket
            results.append(step("position visible in positions list",
                                ticket in {p.ticket for p in adapter.get_positions()} and ticket not in before))
            print("  >>> Check MT5 now: the position should be in the Trade tab.")
            time.sleep(2)
            pos = next(p for p in adapter.get_positions() if p.ticket == ticket)
            ref = pos.price_current or pos.price_open
            tp = round(ref + dist if side == "buy" else ref - dist, digits)
            sl = round(ref - dist if side == "buy" else ref + dist, digits)

            r = adapter.modify_position(ticket, None, tp, change_sl=False, change_tp=True)
            results.append(step(f"TP set to {tp} and confirmed", r.confirmed and r.position is not None
                                and abs((r.position.take_profit or 0) - tp) < point, f"retcode {r.retcode}"))
            r = adapter.modify_position(ticket, sl, None, change_sl=True, change_tp=False)
            results.append(step(f"SL set to {sl} and confirmed", r.confirmed and r.position is not None
                                and abs((r.position.stop_loss or 0) - sl) < point, f"retcode {r.retcode}"))
            print("  >>> Check MT5 now: SL and TP should match the values above.")
            time.sleep(2)

            r = adapter.close_position(ticket)
            results.append(step("close confirmed (position gone from account)", r.confirmed,
                                f"retcode {r.retcode}"))
            results.append(step("position no longer listed", ticket not in {p.ticket for p in adapter.get_positions()}))
    except TradeMateError as exc:
        results.append(step("operation", False, str(exc)))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed.")
    return 0 if passed == len(results) and results else 1


if __name__ == "__main__":
    sys.exit(main())
