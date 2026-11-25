# TradeMate strategies: the five active strategies

Exactly five strategies are selectable in the Command Centre. Each keeps its own identity, version, rule id, saved configuration and history. Hovering the strategy name (or tapping the **i** icon beside it, which also works on a phone and from the keyboard) shows its explanation. Every strategy only *proposes*; the Risk Engine (`docs/RISK_ENGINE.md`) decides, and nothing is traded on your behalf without your Autopilot or confirm click. None of them has been back-tested or forward-tested here; no profitability is claimed.

| Strategy | Explanation shown in the interface |
|---|---|
| **Candle Momentum** | Analyses candles in relation to their surrounding candle group to identify directional momentum and trading opportunities. |
| **CHoCH Trading** | Identifies a Change of Character in market structure and trades the resulting structural shift. |
| **Market Structure Trading** | Uses higher highs, higher lows, lower highs and lower lows to establish market direction and identify entries. |
| **Bull/Bear Trading** | Trades candles that agree with established upward or downward movement. |
| **Trendline Trading** | Uses lines connecting relevant highs or lows to identify market direction, entries and targets. |

Trend Continuation and EMA 20/50 + ATR were **retired from the selector** (their code, the shared structure engine, indicators and market-data services are kept, and old decisions and history still resolve). All strategies run on the execution timeframe you choose (M1, M2, M5, M15, M30, H1, H4).

## Candle Momentum
Unchanged original logic; see `docs/CANDLE_MOMENTUM.md` (timeframe selection, same-candle second entry, defaults).

## CHoCH Trading
Detects a confirmed Change of Character, takes the direction and the broken structural level, enters, sets stop-loss and take-profit, and goes through the Risk Engine. Default target distance: **100 pips**, configurable (`[strategy] choch_target_pips`, or save a target for the strategy in Settings). Full rules below (section "CHoCH Trading").

## Market Structure Trading (`market_structure`, rule `MARKET-STRUCTURE-v1`)
* Direction comes only from the confirmed swing structure of the chosen timeframe: newest swing high is a Higher High **and** newest swing low a Higher Low = bullish; Lower High and Lower Low = bearish. Anything else (HH with LL, equal highs, too few swings) = **WAIT**: direction is never invented from contradictory evidence.
* A straight line is drawn through the last two swing highs (returned as `structure_lines`).
* Enters when the structure is established, on the close of the newest candle, **without an extra confirmation candle**. The signal expires after `signal_max_age` closed candles (12) so an old structure is not traded late.
* Stop beyond the protected swing (+ buffer), capped at your protective-stop distance. Target: by default a fixed, configurable distance from the actual entry (25 pips). **Optional risk-to-reward (Command Centre settings → Market Structure risk-to-reward):** with a ratio chosen (1:1, 1:2, 1:3, 1:4 or custom 0.1 to 100) the target is `ratio × the FINAL stop distance` (after the protective-stop cap), BUY and SELL alike, shown in the trade information as `1:N risk-to-reward`. Stop calculation and structure detection are unchanged. One entry per structure state.

## Bull/Bear Trading (`bull_bear`, rule `BULL-BEAR-v3`), replaced in v3
**v3 REPLACED the v1 candle-group rules** (by decision). Bull/Bear is now a modified copy of the Candle Momentum live engine that additionally requires a CHoCH-confirmed market structure, plus an optional Anti-Twin reversal. Full description: `docs/BULL_BEAR.md`.
* Market Structure Trading now has an optional **configurable risk-to-reward** (1:1 to 1:5 or a custom ratio; see below). Without a chosen ratio nothing changes.

## Trendline Trading (`trendline`, rule `TRENDLINE-v1`), independent of Market Structure
* Bearish: the last two confirmed swing highs descend (lower highs); a resistance line is drawn through them. The line is intact while no close after the second high is above it. **SELL** when the newest close is below it. Bullish: the mirror with ascending lows and a support line, **BUY** above it.
* Target: the *indicated level*, a parallel line through the extreme opposite swing between the anchors, projected to the signal candle; no valid target beyond the entry = no trade. Stop beyond the second anchor (capped at your protective-stop distance). Needs reward-to-risk of at least `min_rr`.
* The drawn lines are returned as `trendlines`. Your **manual** chart lines are a separate store and are never read by any strategy.

Tests: `tests/test_five_strategies.py`.

---

# (Earlier document, still accurate for CHoCH Trading) TradeMate strategies: CHoCH Trading and Trend Continuation

Status: implemented and tested on simulated candles and a simulated MT5 bridge. **Neither strategy has been back-tested or forward-tested; no claim of
profitability, win rate or edge is made or implied.** They are hypotheses to investigate. A strategy only *proposes*; the Risk Engine decides whether a
proposal is permitted and the Execution Engine sends it, only after you confirm.

> There is beauty in simplicity. Two strategies, two timeframes, rules you can read in a minute.

## In plain English (so you can explain TradeMate)

* **H1 = the big picture, M5 = the entry.** Both strategies look only at the one-hour chart (H1) for context and the five-minute chart (M5) for the actual setup
  and its confirmation. Nothing else (no M15, no H4) is read, fetched or required.
* **CHoCH Trading** asks: *"Has the five-minute market just changed its mind?"* In a downtrend the market keeps making lower highs. If a five-minute candle
  **closes** above the latest lower high, the character of the market may have changed (a "Change of Character", CHoCH). The strategy proposes buying after
  that close, with the stop below the low that now protects the move and a target at the next structural level. A CHoCH is a *possible* turn, never proof.
* **Trend Continuation** asks: *"Is the hourly trend clear, and has the five-minute pullback finished?"* If H1 shows rising highs and rising lows, the strategy
  waits for a five-minute dip, then for a five-minute candle to **close** back above the previous high, which suggests the trend is resuming. It proposes
  buying with the stop below the dip. (Mirror image for falling markets.)
* The two strategies never use each other's signals. Pick one in the Command Centre dropdown; that choice drives analysis, trade review and the Watchdog.

## What BUY, SELL, WATCH and WAIT mean

| Decision | Meaning | Can it become an order? |
|---|---|---|
| **BUY / SELL** | The selected strategy found a complete, confirmed setup with valid levels (stop < entry < target for BUY, target < entry < stop for SELL) and enough reward-to-risk. | Only after REVIEW TRADE (Risk Engine) **and** CONFIRM & EXECUTE. |
| **WATCH** | Something is developing but a required condition is outstanding (e.g. the break is only on the unclosed candle, the pullback has not resumed, or no target gives enough reward-to-risk). The reasons say what is missing. | Never. |
| **WAIT** | No qualifying setup, or the data cannot be trusted (stale/missing/misaligned H1 or M5, unknown broker clock), or the setup expired or failed. Bad data is never read as bullish or bearish. | Never. |

BUY and SELL are never shown because a request succeeded or a candle moved. They come only from the strategy's own evaluation.

## Six separate questions (never collapsed into one yes/no)

1. **Market context**: what does H1 show? (CHoCH: aligned/counter/unclear, informs only. Trend: bullish/bearish/unclear, required.)
2. **Setup candidate**: does the setup exist at all? (a fresh unfailed CHoCH; a valid pullback)
3. **Confirmation**: did a *closed* M5 candle confirm it?
4. **Trade-level validity**: structural stop, structural target, correct geometry, reward-to-risk met.
5. **Risk approval**: the Risk Engine (position size, limits, account state). Not the strategy's job.
6. **Execution eligibility**: is the live market still where the proposal assumed (price drift, spread)? Then your explicit confirmation.

The result shows stages 1 to 4 (`stages`); 5 and 6 are the pipeline (`docs/STRATEGY_TO_EXECUTION_PIPELINE.md`).

## Strategy A: CHoCH Trading (`choch`, rule `CHOCH-H1M5-v1`, version 1.0.0)

All on **closed** M5 candles; the structure break is close-based, never a wick.

* **CHoCH**: the structure engine reports a confirmed break (type CHOCH) of a *confirmed* swing, opposite to the previous M5 trend, and it is the **latest** closed
  break. Same-candle conflicts and breaks whose reference swing was not yet confirmed when the candle closed are rejected.
* **Fresh**: at most `signal_max_age` (default 12) closed M5 candles since the CHoCH candle. Older = expired = WAIT.
* **Failed**: any closed M5 candle after the CHoCH closed back through the CHoCH level (or the latest close is not beyond it) = WAIT.
* **Superseded**: the latest closed break is a BOS (continuation), not a CHoCH = WAIT.
* **Provisional**: only the forming (unclosed) M5 candle shows an opposite break = WATCH, never BUY/SELL.
* **H1 context informs but does not veto.** H1 bias aligned / counter / unclear is reported. The one explicit restriction: a CHoCH *against* a clear H1 direction needs
  reward-to-risk of at least `counter_min_rr` (default 2.0) instead of `min_rr` (default 1.5). Unusable H1 data = WAIT (never treated as "neutral").
* **Entry** reference = last closed M5 close (a reference, not the fill). **Stop** = the structure engine's protected swing (confirmed, unbroken) plus the configured buffer.
  **Target** = nearest confirmed H1/M5 swing (or active liquidity pool) beyond entry that meets the required reward-to-risk.
* **Invalidation**: a closed M5 candle back through the CHoCH level or beyond the protected swing; or the signal ageing past `signal_max_age`.
* Does **not** require an order block, FVG or liquidity sweep.

## Strategy B: Trend Continuation (`trend`, rule `TREND-H1M5-v1`, version 1.0.0)

* **H1 trend** from *confirmed* swings only (provisional swings are ignored): bullish = last two confirmed highs rising **and** last two confirmed lows rising, with the last
  H1 close still above the latest swing low; bearish = the mirror; otherwise **unclear = WAIT**.
* **M5 impulse leg**: H = the most recent confirmed M5 swing high, L0 = the confirmed swing low before it; leg = H - L0.
* **Pullback**: P = the lowest closed-candle low since H. It counts only if it retraced at least `pullback_min_retrace` (default 25%) of the leg, stayed above L0 (else it is not a
  pullback any more), and stayed above the latest confirmed H1 swing low.
* **Resumption (confirmation)**: a **closed** M5 candle after P closes beyond H. Fresh within `signal_max_age` candles; failed if the latest close is back inside the old
  range or any later candle broke P; only the forming candle beyond H = WATCH.
* **Entry** reference = last closed M5 close. **Stop** = beyond P (plus buffer). **Target** = nearest confirmed H1/M5 swing (or liquidity pool) with reward-to-risk >= `min_rr`, or the
  *measured move* (P + leg), which is an **unproven assumption** and is labelled as one.
* **Invalidation**: a closed M5 candle beyond P, or a break of the H1 swing structure.
* Does **not** use, wait for, or depend on a CHoCH. Does **not** require H4, M15, an order block, FVG or sweep.

### Known limitations (honest list)

* Not back-tested; thresholds (`min_rr`, `counter_min_rr`, `signal_max_age`, `pullback_min_retrace`) are sensible starting values, not tuned.
* Swing confirmation lags by `swing_right` candles, so a swing is confirmed late by design (this avoids repainting but delays setups).
* The Trend strategy has a short window after the resumption break: once a new swing high confirms, the cycle resets and it waits for the next pullback.
* The entry is a *reference* (last M5 close). The pipeline re-checks the live price (drift and spread limits) and sizes from it.
* Targets rely on swings already in the 300-candle window; a market at all-time extremes may have no structural target and stays WATCH.
* The legacy four-timeframe engine (`/api/strategy/mtf`, rule `MTF-ZONE-REACTION-v1`) still exists for the Strategy page and its tests, but is **not selectable** and is not used by
  the Command Centre, the Watchdog or the pipeline. Its M15-zone and H4-context rules are *not* part of either new strategy.

## Selection, persistence and what happens to pending proposals

* The Command Centre dropdown lists exactly the registered strategies (`GET /api/strategy/catalogue`) and changes the selection with `POST /api/strategy/select`.
  The server accepts only exact registered identifiers (`choch`, `trend`); anything else is rejected and changes nothing.
* The choice is saved in `data/strategy_selection.json` (atomic write) and recovered after a restart. If the file is missing, corrupt or names an unregistered strategy, the configured
  `[strategy] default_strategy` is used and the page says so. With no valid selection, analysis fails safely with an explanation.
* **Changing strategy** (a real change) cancels every pending approval, stops the Watchdog and forgets its candidates, and clears the page's analysis and review.
  An analysis still running when you switch is discarded when it returns (409 `stale`). An old approval is also refused at execution (`strategy_changed`) even if cancelling failed.
* Every proposal and approval records `strategy_id`, `strategy_version` and the rule id; they appear in the review, in `data/pipeline_log.jsonl`, and on Watchdog events.

## Watchdog

Server-side (a background thread in the app, not a browser flag). Started from the toggle, it is **bound to the strategy selected at that moment** and the chosen instrument, and
re-analyses on an interval. A result stamped by a different strategy stops it ("Strategy Changed"). Setup alerts are deduplicated by candidate and lifecycle; CHoCH alerts exist only for
the CHoCH strategy (M5 only). The label shows Off / Active / Temporarily unavailable (with the real reason, e.g. Data Incomplete, Disconnected, Stale Data) / Stopping.

## Autopilot

**Not built.** The control is disabled and labelled unavailable; `GET /api/autopilot/status` lists what it would need and which of those preconditions are currently met. There is no endpoint
that arms it. TradeMate never places an order automatically. Requirements before any Autopilot work are in `docs/STRATEGY_TO_EXECUTION_PIPELINE.md` section 8.

## Settings (`config/settings.toml`, `[strategy]`)

`min_rr`, `trigger_lookback` (legacy engine), `sl_buffer_ticks`, `default_strategy`, `counter_min_rr`, `signal_max_age`, `pullback_min_retrace`. Validated at start-up; an invalid value stops the app with a clear message.

## Running and testing

`python app.py` then open the Command Centre. Tests: `bash tests/run_tests.sh` (everything), `python3 -m unittest tests.test_strategies tests.test_pipeline` (strategies and pipeline, no browser),
`python3 -m unittest tests.test_browser_commandcentre tests.test_browser_strategylab` (real Chromium against a fake bridge; skipped if Playwright is missing). No automated test can place a real order.

---

## Addendum: EMA 20/50 + ATR
A further strategy, **EMA 20/50 + ATR** (`ema_atr`), is registered next to the others. It uses only closed candles of the execution timeframe, enters on an
EMA 20/50 crossover and exits at 2 x ATR (stop) and 4 x ATR (target). The other strategies are unchanged. Full description: [EMA_ATR_STRATEGY.md](EMA_ATR_STRATEGY.md).

## Anti-Twin (v5)
Candle Momentum, Rapid Fire and Bull/Bear each have an independent Anti-Twin ON/OFF using one shared rule (reverse on the first adverse move after entry, evaluated at tick level). See `docs/ANTI_TWIN.md`.
