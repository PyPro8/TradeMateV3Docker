> **UPDATE (current behaviour): the daily period is now the computer's LOCAL calendar day (local 00:00), not the broker's day.**
> Everything below that says the boundary is the broker server's midnight, or that the computer's timezone must never be used, describes the *previous* design and is superseded.
> Broker timestamps are still converted with the measured server offset, but the day's start and end are the local midnight of `[ui] timezone` (or the OS timezone), computed with
> daylight-saving rules (`trademate/localday.py`). See `docs/RISK_ENGINE.md` for the full current description, including the persistent daily history (`data/risk_daily_log.json`).

# Daily loss reset, clocks and candle countdown

This document describes three things that belong together because they all depend on **what time it is at the broker**:
the **Today's Loss / Remaining Daily Loss** counters, the **candle countdown** and the **two clocks**.

## 1. Daily loss counters

### Where
**Strategy** page (sidebar) > **Account risk** card: *Today's Loss*, *Remaining Daily Loss*, *Daily status* and a note line that says which trading day is
in use and when it resets. The same numbers come from `GET /api/risk/state` and are used by the Risk Engine and the execution gate before any order.

### How it works
* **Trading day = the broker's day.** MT5 reports every time (candles, deal history) on the broker server's clock. "Today" starts at **00:00 server time**.
* **Today's closed result** = net (profit + swap + commission) of positions closed since that midnight, from MT5's deal history (read-only).
* **Start-of-day balance** = balance now - today's closed result. **Today's result** = closed result + floating profit/swap of open positions.
  **Today's Loss** = the loss part of that (never negative). **Daily limit** = `max_daily_loss_percent` x start-of-day balance.
  **Remaining Daily Loss** = limit - Today's Loss (never below 0). Reaching the limit locks new trades for the day. These rules are unchanged.
* **Nothing is stored.** The counters are recalculated from broker data on every request, so restarting TradeMate never resets them, and
  the broker's midnight resets them whether or not TradeMate was running.
* **The broker's clock** (`trademate/brokertime.py`) is resolved in this order:
  1. *live*: measured from the newest 1-minute candle of up to four symbols (open positions first, then Market Watch); two symbols must agree;
     reused for 60 s;
  2. *cached*: the last live measurement, for at most 12 hours while MT5 cannot supply a fresh candle;
  3. *configured*: `[risk] broker_timezone` (an IANA name such as `Europe/Athens`). It follows the broker's own daylight-saving changes;
  4. *unknown*: the counters show **unknown**, the Daily status shows **UNKNOWN**, and **new trades are refused** ("daily loss limit cannot be verified").
  A live measurement always wins over the configured zone; a disagreement is logged and shown in the note line.
* **Missing data is never zero.** The daily loss is reported as unknown, with the reason, when: trade history cannot be read; the broker's clock
  cannot be determined; a closed trade of today has no result or a closed trade has no close time; an open position has no profit figure;
  or the history reached the bridge's 500-trade limit without reaching back before the start of the day.

### Root cause of the bug that was fixed
`account_state.gather_state` found the broker's offset from one fresh 1-minute candle. **When that failed (weekend or quiet market, a slow
bridge, an illiquid first symbol) it silently used this computer's local timezone**, and compared that day boundary with broker-clock times.
So whenever the computer's calendar date differed from the broker's:

| computer vs broker | effect (verified on simulated data, see the test report) |
|---|---|
| computer **behind** the broker | yesterday's losses kept counting after the broker's midnight: **no reset**, trading stayed locked |
| computer **ahead** of the broker | the counters reset at the computer's midnight, hours **before** the broker's: a real loss disappeared and the lock was lifted early |

The result also flipped whenever the probe worked on one refresh and failed on the next. The fix removes the local-timezone fallback entirely,
caches/cross-checks the measurement, adds the optional `broker_timezone`, and fails safe (unknown) otherwise. Two further silent-zero paths were closed
(missing trade result / profit figure, truncated history).
**What I could not determine:** which of the failure triggers happened on your machine. Check `logs/trademate.log` for
`Daily loss unknown:` and `Measured broker offset ... differs` lines.

### Configuration
```toml
[risk]
broker_timezone = ""     # optional fallback, IANA name of the BROKER's clock, e.g. "Europe/Athens". Never your own timezone.
```
Recommended: set it once you know your broker's timezone (MT5: Market Watch time column vs UTC), so counters stay known when the market is closed.
Windows needs the timezone database: `pip install tzdata` (listed in `requirements.txt`).

### Known limitations
* A deposit or withdrawal during the day is not tracked (it shifts the start-of-day balance).
* A partial close of a position that is still open is not in the closed history the bridge returns (EA limitation; changing the EA was out of scope).
* A floating loss on a position carried across midnight counts toward the new day (the existing design is balance-based: closed + floating).
* A stale 1-minute candle whose age is almost exactly a multiple of 15 minutes looks like a different offset. Two agreeing symbols, the cache and the
  60 s window make this very unlikely, but it is not impossible.
* Needs the EA v0.4+ (`history` command). With an older EA the counters show unknown with the reason.
* Verified only against simulated brokers and the fixture bridge. It has not been run against a live MT5 account.

## 2. Candle countdown

### Where
Every chart toolbar (**Chart** page and the **Dashboard** chart): **closes in mm:ss** (or h:mm:ss for H4/D1) next to the timeframe buttons.

### How it works
`remaining = candle open + timeframe length - broker time now`. The candle open is the newest candle's timestamp from MT5; the broker's UTC offset
comes from `GET /api/market/time` (the same resolver as above, fetched about every 30 s). The text is updated once a second; no data is fetched
and the chart is not redrawn for it. Switching symbol or timeframe clears it until candles for the new selection arrive. Candle data and trading decisions are never touched.

| state | shown | meaning |
|---|---|---|
| live | `04:12` | candle still forming |
| pending | `00:00` (amber) | just closed, waiting up to 15 s for the next candle |
| closed | `-` | no new candle after that: market closed or no ticks |
| stale | `-` (amber) | MT5 disconnected, candles not refreshing for 20 s, or broker time reading older than 15 min |
| unavailable | `-` | no candle yet, unknown timeframe, broker time unknown, or the clocks disagree |

The tooltip gives the reason. Limitation: on a broker daylight-saving change day the D1 (and possibly H4) countdown can be off by one hour.

## 3. Clocks
Top bar, after the Mode pill: **LOCAL** and **NEW YORK**, each with seconds, zone abbreviation, and a tooltip with the zone name and date.
`static/js/clocks.js` uses the browser's `Intl` timezone data, so **Eastern Time switches between EST and EDT by itself** (no offsets in the code).
The Local clock uses this browser's timezone unless you set `[ui] timezone = "Asia/Manila"` (or env `TRADEMATE_TIMEZONE`). An unknown name in the
settings file stops startup with a clear message; if the browser does not recognise it the clock falls back to the browser zone and says so in its tooltip.
There were no clocks before, so nothing was duplicated.

## Tests
`tests/test_daily_loss_reset.py` (broker clock + daily loss, read-only stub adapters and the fixture bridge), `tests/clock_test.js` via
`tests/test_clock_js.py` (clocks, DST, countdown), `tests/test_browser_clocks.py` (real Chromium). See `TESTING.md` / `README.md`.
Nothing in them can submit an order; the stub adapter fails the test if any trading method is called.
