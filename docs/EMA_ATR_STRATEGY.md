> **Retired from the selector.** EMA 20/50 + ATR is no longer one of the five active strategies. Its code is kept (`trademate/strategies/ema_atr.py`, `registry.RETIRED`) so shared indicators and old history still work.

# EMA 20/50 + ATR (`ema_atr`, rule `EMA-ATR-v1`)

Status: implemented and tested on **simulated candles and an in-memory fake MT5 bridge only**. It has not been back-tested or forward-tested and no claim of
profitability is made. It only *proposes*; the existing Risk Engine and execution path decide and send, exactly as for the other strategies.

## How it works
1. **Confirmed candles only.** Only closed candles of the execution timeframe you choose (M1, M5, M15, M30, H1, H4) are used. The forming candle is never read.
2. **Direction filter.** EMA 20 > EMA 50: longs only. EMA 20 < EMA 50: shorts only. Equal: wait.
3. **Entry trigger = the crossover.** BUY when EMA 20 moves from below EMA 50 to above it on the newest closed candle; SELL for the opposite. There is no other
   entry condition and no other indicator. With a direction but no fresh crossover the decision is **WAIT** (the panel shows "Longs only" / "Shorts only").
4. **Once per crossover.** The signal exists only while the crossover candle is the newest closed candle. Its identity is the timeframe plus that candle's open time, so
   repeated looks at the same candle are one setup: the Watchdog does not re-announce it and the setup ledger refuses a second order. The next closed candle is no
   longer a crossover, so the decision returns to WAIT.
5. **Exits (ATR, in price units: no pip or forex assumption).**
   * Stop-loss = entry -/+ **2.0 x ATR**; take-profit = entry +/- **4.0 x ATR**; nominal reward-to-risk 2:1.
   * The entry reference is the close of the crossover candle. Prices are snapped to the instrument's tick size: the stop is never closer and the target never
     nearer than calculated. The pipeline then re-checks the live price, the broker's minimum stop distance, the lot size and the Risk Engine.

## Calculations
* **EMA**: alpha = 2 / (period + 1), seeded with the simple average of the first `period` closes.
* **ATR (Wilder)**: TR = max(high - low, |high - previous close|, |low - previous close|); first ATR = average of the first `period` TRs; then
  ATR = (previous ATR x (period - 1) + TR) / period.
* **History needed**: `max(3 x slow EMA period, 3 x ATR period + 1)` closed candles (150 with the defaults) so the EMA has settled. With fewer the result is WAIT with
  the reason "Not enough history".

## Refusals (no signal, no trade)
Stale or missing data; too little history; any candle with missing/NaN/impossible prices; a newest candle the clock says has not closed; ATR zero, negative or
invalid; invalid settings (fast period not below slow period, non-positive ATR period or multipliers).

## Configuration (`config/settings.toml`, `[strategy]`)
`ema_fast_period = 20`, `ema_slow_period = 50`, `atr_period = 14`, `atr_sl_multiplier = 2.0`, `atr_tp_multiplier = 4.0`. Invalid values stop TradeMate from starting with a
clear message. The timeframe is chosen per strategy in the Command Centre; lot size and Autopilot work as for the other strategies.

## Limitations (read these)
* EMA crossovers lag. In ranges they whipsaw: repeated small losses of about 2 x ATR each are expected there.
* The 2:1 nominal ratio is a design ratio, not an expected outcome; the win rate needed to break even is about one in three before spreads and commissions, and
  nothing here shows the strategy achieves it.
* A signal is only valid for the candle after the crossover. If the Watchdog was not running, or the price has moved or the spread widened, the pipeline's
  eligibility checks can refuse it; it is never entered late.
* The pipeline's existing minimum reward-to-risk (`[strategy] min_rr`, 1.5) still applies at the live price, so a large adverse move after the signal turns it down.
* Needs a pip-free instrument specification only for volume and tick; nothing else about the instrument is assumed.
* Not verified here: a live MT5 broker, real fills, browser rendering of the new strategy in every view.
