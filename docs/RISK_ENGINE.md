# Risk Engine

Every proposed trade, from every strategy, from Autopilot, from the manual confirm button and from any future AI feature, is approved by **one** function: `trademate.risk.engine.evaluate_trade()`.
No route can reach an order without it: the pipeline calls it when it proposes and again when it executes, and the central protections below are installed into it, so they cannot be forgotten by a caller.

## What it checks

| Check | Rule |
|---|---|
| Risk per trade | The money lost at the stop must not exceed the configured percent of equity (default 1 %). |
| Daily loss | Closed losses plus floating loss today must stay under the configured percent of the start-of-day balance (**3 %**). Reaching it locks trading for the rest of the local day. |
| Total active risk | Risk of all open trades plus the new one must stay under the configured percent (default 5 %). |
| Maximum positions | Default 3. |
| **Emergency lock** | While the Emergency Stop lock is set, nothing is approved. |
| **Consecutive losses** | When enabled, nothing is approved once the configured number of completed losing trades in a row is reached. Shows `TRADING LOCKED` and the reason. |
| Broker constraints | Volume minimum, maximum and step, tick size and value, from the broker's own specification. |

If the emergency lock or the consecutive-loss record cannot be read, the engine **fails closed** (it rejects). The duplicate-order ledger (one setup, one order, or two when allowed) is checked in the pipeline next to it.

## Daily period: the computer's local midnight

The trading day is the **computer's local calendar day**, 00:00 to 00:00, in the timezone set by `[ui] timezone` in `config/settings.toml` (an IANA name such as `Asia/Manila`), or the operating system's timezone when it is empty. It is **not** the broker server's day.

* MT5's history timestamps are on the broker clock. They are converted with the measured server offset (`trademate/brokertime.py`) before being compared with the local day's boundaries.
* The boundaries come from the timezone's own rules (`trademate/localday.py`, `zoneinfo`), so a daylight-saving change makes a day 23 or 25 hours long and never shifts the boundary by an hour.
* Yesterday's losses are never carried into today: the counters are recomputed from the broker history every time. A loss closed exactly at 00:00:00 belongs to the new day.
* If the broker clock cannot be measured, or the timezone is invalid, the daily loss is reported as **UNKNOWN** and new trades are blocked (it never guesses).
* **Historical records** are kept: `data/risk_daily_log.json` has one row per local day (start balance, closed P/L, loss, percent, limit, locked). A new day adds a row; earlier rows are never edited or deleted, and the file survives restarts and Hard Reset.

## Consecutive-loss protection (configurable)

Settings page, "Execution & safety" (also `POST /api/execution-settings`):

* Enable or disable it; set the maximum number of consecutive completed losing trades (any whole number from 1 to 1000, for example 2, 3, 5 or 10). Changing the number takes effect at once, without touching the record.
* The page shows the current count and the configured limit. When the limit is reached the status shows **TRADING LOCKED** and the reason.
* A completed winning trade resets the count. A break-even trade changes nothing. Open losing positions are never counted. Each ticket is counted once (a repeated history row cannot add a second loss).
* `Reset the counter` needs the typed word `RESET`; it sets the count to 0 and keeps every historical record (`data/consecutive_losses.json`: records and resets only grow).
* The count persists across restarts. On the very first start, trades that already exist are recorded as history and not counted, so installing the feature cannot lock a new installation retroactively.
* **Default: enabled, limit 3.** Switch it off or raise the limit on the Settings page if that is not what you want.

## Intelligent lot-size adjustment after "NOT ENOUGH MONEY"

When MT5 refuses an order with retcode 10019 (or the text "not enough money"):

1. The refused order proved nothing was opened, so its duplicate-ledger slot is released for the reduced order only.
2. TradeMate re-reads the account (balance, equity, free margin) and the instrument's volume minimum, maximum and step.
3. It recalculates the monetary risk at the intended stop with the broker's tick size and tick value.
4. It calculates a smaller lot: strictly smaller than any lot already refused, within the allowed risk, within the free margin when the margin per lot is known, within the broker's limits, rounded **down** to the volume step.
5. The reduced size goes through the **whole** Risk Engine again (daily loss, consecutive losses, emergency lock, per-trade and total risk). Only if it is approved is a new, different order sent.
6. At most `lot_adjust_max_attempts` (1 to 3, default 2) reduced retries per setup. The same order is never re-sent.
7. If the minimum lot still does not fit, the trade is rejected with a clear explanation. Nothing is forced.

The original lot, the adjusted lot and the reason are shown in the Autopilot message and returned in `lot_adjustment`. The saved lot size is not changed. Switch the feature off on the Settings page ("Automatic lot-size reduction").

**Honest limitation:** the MT5 bridge does not expose `OrderCalcMargin`, so the margin required per lot is **not available**. When it is unknown TradeMate does not invent it: it uses a bounded reduction (halving, normalised to the step) and says so in the reason. If an adapter supplies `margin_per_lot(symbol, side, price)`, the margin-based calculation is used automatically.

## Emergency lock

`Emergency stop` sets a persistent lock (`data/safety_lock.json`), stops Autopilot, cancels pending approvals and makes the Risk Engine reject every order from every route, including after a restart. It does **not** close open positions unless `emergency_close_positions` is switched on in Settings (off by default). Lifting the lock needs the typed word `UNLOCK` and does not restart Autopilot.
