"""Command-line check of the MT5 connection (read-only).

Run:  python -m trademate.broker
"""

import sys

from trademate.broker import ConnectionState, create_adapter
from trademate.broker.mt5_adapter import find_common_files_dir
from trademate.config import load_config
from trademate.errors import ConfigError, TradeMateError


def main() -> int:
    try:
        settings = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    adapter = create_adapter(settings)
    directory, searched = find_common_files_dir(settings.mt5.data_dir)

    print("TradeMate MT5 check (read-only)")
    print(f"Snapshot folder : {directory if directory else 'NOT FOUND'}")
    if directory is None:
        for place in searched:
            print(f"  looked in     : {place}")
    else:
        print(f"Snapshot file   : {directory / settings.mt5.snapshot_file}")

    status = adapter.get_connection_status()
    print(f"Connection      : {status.state.value}")
    print(f"Detail          : {status.message}")
    if status.age_seconds is not None:
        print(f"Snapshot age    : {status.age_seconds:.1f}s")
    for warning in status.warnings:
        print(f"Warning         : {warning}")
    if status.state is not ConnectionState.CONNECTED:
        return 1

    try:
        account = adapter.get_account()
        positions = adapter.get_positions()
        symbols = adapter.get_symbols()
    except TradeMateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"Broker          : {account.broker}")
    print(f"Server          : {account.server}")
    print(f"Environment     : {account.environment.upper()}")
    print(f"Account login   : {account.login}")
    print(f"Currency        : {account.currency}")
    print(f"Balance         : {account.balance}")
    print(f"Equity          : {account.equity}")
    print(f"Margin          : {account.margin}")
    print(f"Free margin     : {account.free_margin}")
    print(f"Open positions  : {len(positions)}")
    print(f"Symbols         : {len(symbols)} in Market Watch")
    for info in symbols[:5]:
        print(f"  {info.name:<10} bid={info.bid} ask={info.ask} spread={info.spread_points} pts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
