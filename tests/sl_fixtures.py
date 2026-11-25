"""Shared instrument specifications for the stop-loss calculator tests. Tick values are per ONE lot, in ACCOUNT currency,
exactly as MT5 reports them. They are test data, not market data."""

from trademate.risk.position_sizing import SymbolSpec


def spec(name, tick, tv, vmin=0.01, vmax=100.0, step=0.01, tv_loss=None, trade_mode=4):
    return SymbolSpec(name=name, tick_size=tick, tick_value=tv, tick_value_loss=tv_loss, volume_min=vmin, volume_max=vmax,
                      volume_step=step, trade_mode=trade_mode)


# name -> (spec, digits, entry price used in tests)
INSTRUMENTS = {
    "EURUSD": (spec("EURUSD", 0.00001, 1.0), 5, 1.10012),                                  # 100k contract, USD account: $1 per tick
    "USDJPY": (spec("USDJPY", 0.001, 0.666667), 3, 150.000),                               # tick value converted from JPY
    "XAUUSD": (spec("XAUUSD", 0.01, 1.0), 2, 2000.30),                                     # 100 oz contract
    "US30": (spec("US30", 0.1, 0.1), 1, 38000.0),                                          # index CFD
    "BTCUSD": (spec("BTCUSD", 0.01, 0.01, vmin=0.01, step=0.01), 2, 60000.00),             # 1 coin contract
    "EURGBP": (spec("EURGBP", 0.00001, 1.26, tv_loss=1.27), 5, 0.85000),                   # cross pair: loss value differs from gain
}
