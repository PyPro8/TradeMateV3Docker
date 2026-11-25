> **Note:** Trend Continuation is retired from the selector (see `docs/STRATEGIES.md`). The multi-timeframe engine described here is shared infrastructure and is kept.

# TradeMate Strategy Engine

The read-only strategy implementation is in `trademate/strategy/`:

- `service.py` retrieves broker-native candles and resolves the broker clock.
- `timeframes.py` validates candle timestamps, ordering, freshness, completeness, and closed/forming state.
- `mtf.py` analyzes structure, resolves timeframe context, applies mandatory qualification gates, and creates a proposal.
- `regime.py` estimates deterministic market regimes from validated candles and structural analyses.
- `setups.py` discovers explainable setup-family candidates; discovery is not execution approval.
- `watchdog.py` monitors one broker symbol while the TradeMate server process is running.

Endpoint: `GET /api/strategy/mtf?symbol=EURUSD`. The Command Centre displays the same response. M5, M15, H1 and H4 are fetched as native
broker timeframes; no timeframe is synthesized or substituted.

## Data and candle policy

MT5 candle timestamps are broker-server epoch times. The service measures the server offset from M1 candles (or the existing configured
broker-time fallback) and passes broker time into the pure data validator. A candle opened at `t` is closed only when `now_server >= t +
timeframe_length`. Only closed candles enter structure, setup, regime and qualification analysis. A forming candle may be shown as
provisional evidence but cannot confirm a break or a setup. If the broker clock is unknown, the newest candle is conservatively treated as
forming and the data gate fails.

`prepare_series` sorts candles for analysis but records out-of-order input as a blocking integrity defect. It also reports invalid candles,
conflicting duplicates, future timestamps, grid misalignment, recent gaps and insufficient history. Identical duplicates are removed and
reported. No missing candle is invented. The current defaults require 60 closed bars for M5/M15/H1 and 30 for H4; a gap in the newest 12
bars (6 for H4) is degraded. A newest closed candle more than two timeframe lengths old is `stale`.

A weekend, session break, broker outage and stale feed may all result in old candles. There is no instrument/session calendar, so TradeMate
reports the data as stale and does not claim to know which cause applies. The observed “no qualifying M5 close in the last 12 readings”
can also be a genuine result of the configured 12-closed-M5-bar reaction window: it is not repaired by reusing another timeframe or by
counting a forming candle. M5 being unavailable does not erase H1/H4/M15 analysis, but it blocks an executable proposal that requires an M5
entry trigger.

| Status | Meaning |
|---|---|
| `ok` | Fresh, aligned, sufficiently complete closed-candle data with no recent blocking defect |
| `degraded` | Candles can be inspected, but an integrity defect blocks BUY/SELL |
| `stale` | The newest closed candle exceeds the timeframe-specific freshness interval |
| `insufficient` | Too few closed candles remain for analysis |
| `misaligned` | Candle times are not on that native timeframe's epoch grid |
| `unavailable` | No usable data or the broker adapter could not supply that timeframe |

## Timeframe roles

| Timeframe | Role | Current evidence |
|---|---|---|
| H4 | Broad structural context | Closed BOS/CHoCH, swing bias, invalidation and reversal stage |
| H1 | Intermediate structure and potential reversal | Same structural evidence; a confirmed H1 reversal is required for the current counter-H4 route |
| M15 | Setup location | Structure, confirmed order blocks, partially/unfilled FVGs, liquidity pools, swings, breakout retests |
| M5 | Entry timing | Closed structure breaks and the current policy's reaction-after-zone trigger |

Each result keeps the actual timeframe label and status. A missing H4 does not silently become H1. When one of H4/H1 is available it can provide
partial context, with the missing view explicitly reported. If neither establishes direction the context remains inconclusive. Lower-timeframe
agreement is not counted as a vote that overrides contradictory H4/H1 structure.

## Shared structural direction and quality

Each timeframe uses the shared `trademate/structure/engine.py` detector also used by the Analysis page. `bias` / confirmed direction comes
from its confirmed structural state; `strength` is a separate assessment of how its latest event and confirmed swing pattern agree. A
direction can therefore be `bullish` while its strength is `MIXED` or `EARLY`. Analysis exposes the same confirmed direction separately
from its evidence-quality label. These labels do not decide BUY/SELL: setup discovery, higher-timeframe context, data status, entry,
invalidation, target, reward-to-risk, and risk requirements remain separate gates.

When comparing the pages, use the same symbol, native timeframe, confirmed-candle cutoff and structure configuration. Analysis fetches up
to 500 H1/H4 candles (300 for other supported timeframes); Strategy validates native timeframe history and uses closed candles only.
Requests are not atomic, so a new candle or broker update between requests can change the result. Both APIs expose event IDs, timestamps,
reference swings, prior/resulting state and confirmation status to help audit a genuine mismatch. Analysis also renders the broken level,
reference swing, break method and engine reason in each recent event explanation.

## Regime estimates

The response always includes `market_regimes`, with a record for each supported classification: `classification`, `active`, relevant
`timeframe`, `supporting_evidence`, `conflicting_evidence`, `limitations`, `certainty` and `certainty_basis`. Certainty is qualitative evidence
coverage, not a probability or a calibrated score:

- `high`: H4 and H1 have established structure in the same direction.
- `moderate`: one reliable higher timeframe or a measurable closed-candle pattern supports the classification.
- `low`: evidence is incomplete, contradictory, or does not satisfy a classification rule.

The classifications can overlap:

- `bullish_trend` / `bearish_trend`: direction from available H4/H1 structure. An H4/H1 disagreement disables either trend label and is
  preserved as conflicting evidence.
- `range_bound`: M15 uses the last 24 closed candles. Close-path efficiency must be at most 0.20 and the close range no more than 10 median
  true ranges; an established aligned H4/H1 trend prevents the range label. This is a heuristic range description, not a range forecast.
- `transition_or_potential_reversal`: actual reversal-stage evidence or H4/H1 structural disagreement. A possible/developing reversal is not
  called confirmed.
- `elevated_volatility`: on an available timeframe, the latest 10-bar median true range is at least 2x the preceding 21-bar median. It may
  coexist with a trend and does not predict direction.
- `uncertain_or_insufficient`: required views are missing, higher-timeframe directions conflict, or no directional/range/transition rule is
  supported.

All thresholds and certainty semantics are explicit in `regime.py`; the figures are descriptive heuristics and have not been statistically
calibrated.

## Setup discovery and qualification

The engine now reports `setup_candidates` before a final decision. A candidate contains a stable identity, family, direction, premise,
supporting evidence, contradictory evidence, missing evidence, invalidation conditions, target methodology, state and
`execution_eligible`. It is not a numerical success probability. The currently implemented families are:

| Family | Discovery evidence and unresolved requirements |
|---|---|
| Trend continuation | Measured higher-timeframe trend plus aligned structure or an actual closed BOS; requires the same entry, stop and target gates below |
| Pullback continuation | Opposing M15/M5 structure or an actual live M15 zone within a directional higher-timeframe context; requires a closed M5 reaction and break after the zone touch |
| Liquidity sweep reaction | A confirmed BSL/SSL sweep maps to its opposing candidate direction; a later closed structural event is required before describing it as a reaction |
| Structural reversal | Actual `possible`, `developing` or `confirmed` reversal state and its real confirmation/invalidation conditions |
| Breakout and retest | A closed M15 BOS and a later closed candle retesting the broken level and closing back beyond it; still requires separate M5 entry/stop/target qualification |
| Range-bound opportunity | The measured range hypothesis is reported without inventing a direction; this family is discovery-only and cannot currently produce BUY/SELL |
| Countertrend opportunity | H1 reversal against H4; the current route requires a confirmed H1 reversal and established M15 structure in the new direction |

The specialists' BOS/CHoCH, liquidity, FVG and order-block evidence is reused as implemented. A sweep, block, gap or BOS by itself is not a
trade. A candidate may be meaningful but not executable. Families other than the existing zone-reaction route are discovery/reporting
models only; they are not presented as independently qualified execution strategies.

The current qualified route is `MTF-ZONE-REACTION-v1`. All of these are mandatory:

1. All four timeframes are `ok`, broker time is known, and candles are closed, aligned, sufficiently complete and fresh.
2. Available H4/H1 context permits the direction. For a counter-H4 proposal, H1 reversal must be confirmed and M15 must be established in the
   new direction.
3. A live, confirmed M15 order block or not-fully-filled M15 FVG exists in the proposal direction.
4. A closed M5 candle has reached that zone within `trigger_lookback`, then a closed M5 BOS/CHoCH in the proposal direction occurs after the
   reaction; the M5 bias must also agree.
5. The stop is beyond the zone and reaction extreme on the loss side of the reference entry.
6. TP1 is an actual structural swing/liquidity level in the trade direction with at least configured `min_rr`; TP2 is the next structural
   level when one exists.

No weighted score can override these gates. Missing evidence is not positive evidence. Degraded or missing M5/H4/H1 data can leave a candidate
visible as WATCH but never bypasses the current all-four-timeframe data gate for BUY/SELL. The Risk Engine remains separate and is not called
by analysis.

## Decision semantics

- **BUY / SELL:** this strategy route has a qualified directional proposal and all strategy eligibility gates passed. This does not mean the
  Risk Engine approved it, an account is ready, or an order was sent.
- **WATCH:** an identified direction/candidate remains unresolved. `reasons`, `missing_evidence`, candidate `invalidation_conditions` and
  timeframe evidence state what is being monitored and what can disqualify it.
- **WAIT:** no supported direction/candidate is ready for monitoring, no area of interest exists, data is unusable, or context is
  inconclusive/conflicting without a justified developing reversal. The reason is returned; no price level is fabricated.

The response keeps structural invalidation, setup invalidation, stop-loss, entry reference and targets separate. An entry is the close of the
last closed M5 candle, not a promised broker fill. Only BUY/SELL include proposed levels. `validation_snapshot` is a versioned JSON reporting
shape (`trademate.strategy.validation.v1`) for decision-time data/evidence and proposed levels. It explicitly marks execution/outcome as
unlinked; it is not a persisted journal and contains no fabricated fills or outcomes.

Settings currently supported in `[strategy]`: `min_rr`, `trigger_lookback`, and `sl_buffer_ticks`. Swing settings come from `[structure]`.

## Watchdog, risk, execution and positions

The Watchdog polls one selected symbol every 60 seconds while the server process is alive. It tracks stable candidate identities, new and
strengthening candidates, qualified candidates, candidates no longer observed, stale/incomplete data, disconnection and monitoring failures.
Repeated polling of an unchanged candidate does not create repeated state-change events. Qualified alerts retain the existing event-key
deduplication. Candidate monitoring never submits an order. The loop stops with the application; no background service after shutdown is
claimed.

The strategy package is pure analysis and can only produce the Risk Engine's `TradeProposal` data shape. It does not call risk sizing,
execution, broker write methods, or account credentials. The current UI leaves order entry manual. Server-side order review/submission uses
the existing Risk Engine and Execution Engine; each submission rechecks current account state, broker constraints and risk. A strategy
decision does not bypass those checks.

Actual open positions, manual close and stop/target changes use existing broker-backed position controls and execution safeguards. The
Strategy Engine does not automatically trail stops, move to break-even, partially close, invalidate/exit positions, or reconcile a position's
thesis. Such lifecycle automation is not implemented.

## Outcome validation and limitations

The versioned snapshot reports family, regime, decision, candle times, evidence, gates and analytical levels. TradeMate's existing execution
journal records order attempts and reconciliation state; broker history reports actual closed-trade results. They are not joined to a
strategy candidate or snapshot. Therefore fill/slippage, initial risk, realised result, MAE/MFE, spread/cost and thesis-at-exit are not
collected as a strategy outcome dataset. There is no automatic learning or parameter adaptation, no out-of-sample validation, and no
profitability claim. A safe candidate-to-fill/outcome linkage remains future work.

No historical market-data backtest or broker validation was performed for this work. Synthetic deterministic tests validate code behavior,
not edge or profitability. Instrument-specific spread, sessions, news and broker fill behavior are not modelled. There is no session calendar,
and epoch-aligned native H4 bars can be unavailable or gapped for instruments with trading breaks. The bridge serves at most 500 candles per
request. Account switching remains unsupported: the MT5 terminal authenticates the account, the bridge has no login/switch command, and
commands are not pinned to a login. The adapter remains DEMO-only; the Account Centre truthfully displays only the current broker-reported
identity.

## Tests

Run deterministic strategy and integration tests with:

```sh
python -m unittest tests.test_strategy_intelligence tests.test_mtf_timeframes tests.test_mtf_strategy tests.test_mtf_api tests.test_watchdog -v
python -m unittest tests.test_browser_mtf tests.test_browser_strategylab -v
```

Browser tests require Playwright and Chromium. Synthetic fixtures and read-only feed doubles never place orders. See `TESTING.md` for the
broader project suite and the manual DEMO-only verification procedure.

## Hand-off to the Risk and Execution engines (added with the pipeline checkpoint)

`/api/strategy/mtf` is still read-only and still never sizes or sends. A qualified BUY/SELL is consumed by `trademate/pipeline.py`, which validates the proposal contract
(`pipeline.qualify`), sizes it through the Risk Engine from the LIVE executable price, checks execution eligibility, and issues a single-use approval. The strategy's
`entry` remains a reference price (close of the last closed M5 candle), never the fill price. `StrategyResult.to_trade_proposal()` is unchanged and still carries no risk or size;
the pipeline does not use it. See `STRATEGY_TO_EXECUTION_PIPELINE.md`.
