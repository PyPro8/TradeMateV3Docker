"""Search the connected broker's whole instrument list.

Run:  python -m trademate.broker.find_symbols gold
"""

import sys

from trademate.broker import ConnectionState, create_adapter
from trademate.config import load_config
from trademate.errors import TradeMateError

MODES = {0: "disabled", 1: "long only", 2: "short only", 3: "close only", 4: "full"}


def main() -> int:
    adapter = create_adapter(load_config())
    status = adapter.get_connection_status()
    if status.state is not ConnectionState.CONNECTED:
        print(f"MT5 is {status.state.value}: {status.message}")
        return 1
    query = " ".join(sys.argv[1:])
    try:
        result = adapter.search_symbols(query, 50)
    except TradeMateError as exc:
        print(exc)
        return 1
    print(f"{result.total_matches} match(es) of {result.total_symbols} broker symbols for {query!r}:")
    for m in result.matches:
        print(f"  {m.name:<14} {MODES.get(m.trade_mode, '?'):<11} {m.description}  [{m.path}]"
              + ("  (in Market Watch)" if m.visible else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
