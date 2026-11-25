# Candle Momentum v2: the tick-driven engine

Rule id `CANDLE-MOMENTUM-v2`. Code: `trademate/candle_momentum/`. Status: implemented and tested **in simulation** (a deterministic fake feed and the fake MT5 bridge).
It has **not** been run against a live broker and it has **not** been back-tested or forward-tested. Nothing here claims it is profitable: with the default 10 / 10 / 1 settings the
reward-to-risk is below 1, so it only pays if it wins more often than it loses.

## 1. What it does (one paragraph)

For every (instrument, timeframe) you monitor, one persistent engine follows the **currently forming candle**. Price above the candle's open = bullish, below = bearish, equal = neutral.
The moment price reaches your trigger distance from the open (open + 5 pips for a bullish candle, say) the entry is judged **immediately** on the current executable quote and, if the target is
still reasonably available, handed to the existing Risk Engine and execution pipeline. The take-profit is your target distance measured from the **actual fill**. One entry per candle. It uses no
market structure, CHoCH, support/resistance, trendline or EMA.

## 2. Why the previous implementation produced repeated late-entry warnings (audit findings)

This section answers the audit questions. Everything below was established by reading the code in the supplied project; the **old tests** in the project also assert the same behaviour
(`test_late_entry_is_rejected`, `test_earlier_overshoot_then_pullback_is_still_late`, now retired, see section 10). I did **not** measure live latency: I have no MT5 here.

| Question | v1 answer (what the code did) |
|---|---|
| Where was Candle Momentum implemented? | `trademate/strategies/candle_momentum.py::evaluate()`, a **snapshot function**: given candles fetched a moment ago, return BUY / SELL / WATCH / WAIT. No state, no memory of earlier ticks. |
| Where was the trigger calculated? | Only inside that function: candle open ± `trigger_pips` converted with the pip size. |
| Where was BUY / SELL / WAIT decided? | The same function, through `strategies.registry.evaluate_strategy_result` → `strategies.base.build_result`. |
| Source of "Too late"? | The same function, from `StrategyParams.late_pips`. |
| Hard-coded chase limit? | `late_pips = 2.0`: in `config/settings.toml` (`candle_late_pips`), `StrategySettings`, `StrategyParams` and `strategies/service.py`. The entry was refused when price **or the candle's extreme** was more than 2 pips past the trigger. |
| How did Watchdog evaluate it? | `strategy/watchdog.py` slept `WATCHDOG_INTERVAL_SECONDS = 60`, then fetched candles and ran the snapshot function, then slept again. It never saw a tick. |
| How did Autopilot receive it? | It polled the decision hub every `poll_seconds = 5`. |
| How did the Command Centre get state? | The page polled `/api/decision/current` every 2 s and drew the hub record. (Display only; it never decided.) |
| How did ticks reach the strategy? | They did not. The "current price" was the close of the forming candle at the moment the candles were fetched through the bridge's file-command channel. |
| How did the Risk Engine approve? | `pipeline.propose` → `qualify` → `evaluate_trade`; `prepare_execution` re-validates and runs `gate_order`. **Both stages re-ran the whole snapshot strategy** (two more candle fetches), re-applying the late rule each time. |
| Position monitoring / exits? | Stop-loss and take-profit are placed on the order and held by the broker; `_realign_after_fill` re-sets the TP from the actual fill; Autopilot tracks closure from positions + deal history. |
| Pip / point conversion? | `trademate/instruments.py::build_spec` (the central instrument engine) and `pips.py`. v2 uses it for every distance. |

**The causal chain.** A 10-pip trigger is crossed at an arbitrary moment. A snapshot strategy sees it only when something polls: up to 60 s later (Watchdog), then up to 5 s later (Autopilot),
then through up to two more candle fetches (propose, execute). Price routinely travels more than 2 pips in that time, so the 2-pip rule fired. Worse, the rule also looked at the candle's
**extreme**, so once price had overshot at any moment, every later poll in the same candle also said "Too late" even if price was back at the trigger: that is the *repeated* warning.
A second, less visible guard, the pipeline's `max_entry_drift_stop_fraction = 0.25`, is about 2.7-3 pips for a 10-pip trigger with a 1-pip stop buffer, and acted as another hidden chase limit.
Latency, not the market, was the main cause. The 2-pip number itself had no stated basis.

## 3. How v2 works

```
MT5 snapshot (bid/ask, age)  ──►  QuoteFeed  ──►  CandleMomentumEngine (one worker thread, 0.25 s cadence)
MT5 forming candle (open)    ──►  (read once per candle + verification)        │
                                                                                ▼
                                       CandleMachine (one per symbol/timeframe)  direction → trigger → ENTRY VALIDATION
                                                                                │  validated signal (RISK_PENDING)
                       DecisionHub (BUY/SELL) ◄── publish ──────────────────────┤  wake Autopilot immediately
                                                                                ▼
                    Autopilot → pipeline.propose → Risk Engine → prepare_execution → place_order → broker
                                                                                │  observer: approved / refused / executed / failed
                                                                                ▼
                                                    engine: POSITION_OPEN → TARGET_REACHED → POSITION_CLOSED
```

* **Candle identity**: `sha256(symbol | timeframe | open time | open price)`. It contains no direction: a candle that turns from bullish to bearish is still the same candle. A revised open
  price for the same open time is treated as a data revision, never as a new candle.
* **Price basis**: the candle open is a **bid** price, so direction and trigger crossing are judged on the **bid**. Entry validation uses the **executable** price (ask for a BUY, bid for a SELL).
* **Direction**: bullish if bid > open, bearish if bid < open, neutral if equal on the instrument's tick grid. It can change while the candle forms; each change recomputes the trigger.
* **Trigger**: `open ± trigger distance`, converted by the instrument engine and snapped to the tick grid. An exact touch counts as a crossing. A trigger or target that would be a non-positive
  price is refused as an invalid conversion.
* **Immediate evaluation**: the crossing and the entry validation happen in the same cycle. If a tick jumps beyond the trigger, the *current executable quote* is judged. It is never rejected merely
  because it is more than N pips beyond.
* **Protective stop**: the existing policy, `sl_buffer` units beyond the candle open on the loss side. It is **mandatory**: if it cannot be established the entry is REJECTED and nothing is sent.
* **Target**: `target distance` from the **actual fill**. The take-profit the broker holds is re-aligned to it after the fill (existing `_realign_after_fill`). The configured target is never silently replaced and there is no universal 2R.

### The separate, named controls (nothing is a hidden constant)

| Control | Meaning | Default | Where |
|---|---|---|---|
| `trigger_units` | trigger crossing: distance from the candle open | 10 | `[strategy] candle_trigger_pips`, per instrument/timeframe in the UI |
| `target_units` | target distance from the actual fill | 10 | `[strategy] candle_target_pips`, UI |
| `sl_buffer_units` | protective stop beyond the candle open (mandatory) | 1 | `[strategy] candle_sl_buffer_pips`, UI |
| *(remaining target distance)* | measured from the **actual executable quote** to the level a full target from the trigger would have reached. At or beyond that level the entry is always MISSED. | n/a | computed |
| `max_slippage_units` | **execution slippage tolerance**: how much worse than the validated quote the live price may be when the order is submitted (adverse moves only) | unset | `[candle_momentum]`, UI |
| `signal_expiry_seconds` | **signal expiry**: a reached trigger that has not become an order in this time is EXPIRED | 15 | `[candle_momentum]`, UI |
| `quote_max_age_seconds` | **quote freshness**: no entry is ever judged on an older quote | 3 | `[candle_momentum]`, UI |
| `max_entry_attempts`, `retry_delay_seconds` | bounded retry (1 = never retry) | 1, 2 s | `[candle_momentum]`, UI |
| `poll_seconds` | how often the engine reads the quote (the real monitoring cadence) | 0.25 | `[candle_momentum]` |

These defaults are **policy values, not tuned against data.** `max_target_consumed_fraction = 0.5` mirrors the CHoCH strategy's existing `choch_max_extension_fraction`; it is a fraction of
*your* target, so it scales with the instrument and the settings rather than being a pip count. Change it if you disagree.
Slippage is measured **after** the fill too (`slippage_units`, flagged if it exceeds the tolerance) and is never confused with the trigger distance.

Candle Momentum is a live-trigger strategy, so its original trigger/reference quote is not used as a generic 25%-of-stop entry-drift ceiling at final submission. The pipeline re-evaluates the live strategy proposal, obtains the current executable bid/ask, re-anchors its configured pip target to that live executable price, and rechecks stop/target geometry, minimum reward-to-risk, spread economics, the Risk Engine and broker limits. This is not unlimited chasing: a price beyond the stop/target geometry, an exhausted target, inadequate live reward-to-risk, excessive spread, stale quote, or failed Risk Engine/broker check still blocks execution. Other strategies retain their configured generic entry-drift guard. `max_slippage_units` remains available for explicit Candle Momentum adverse-slippage policy where configured.

## 4. Lifecycle states

| State | Meaning | Published decision |
|---|---|---|
| `NEW_CANDLE` | a new candle was identified; nothing judged yet | WAIT |
| `MONITORING` | tracking; price equals the open, no direction | WAIT |
| `BULLISH` / `BEARISH` | direction known, trigger not yet reached | WATCH (never traded) |
| `TRIGGER_REACHED` | price crossed the trigger | WATCH |
| `ENTRY_VALIDATION` | judging the executable quote (waits for a fresh quote if needed, up to signal expiry) | WATCH |
| `RISK_PENDING` | a **validated** executable signal exists; the Risk Engine / executor decide next | **BUY / SELL** |
| `EXECUTING` | the Risk Engine approved; the order is being submitted | **BUY / SELL** |
| `POSITION_OPEN` | the broker confirmed a position for this candle | WAIT (executed) |
| `TARGET_REACHED` | price reached the target; the broker's take-profit closes it | WAIT |
| `POSITION_CLOSED` | the position is closed | WAIT |
| `INVALIDATED` | price came back inside the trigger before the order was committed | WAIT |
| `MISSED` | the opportunity is gone (target no longer reasonably available, or price moved away before submission). **Never** shown as an entry. | WAIT |
| `REJECTED` | a safety rule refused it: Risk Engine, invalid lot, missing protective stop, UNVERIFIED instrument data ... | WAIT |
| `EXECUTION_FAILED` | the order could not be placed or its outcome is unknown | WAIT |
| `EXPIRED` | the candle closed or the signal outlived its expiry without an entry | WAIT |

Every transition records: timestamp, symbol, timeframe, candle id, current price, candle open, direction, trigger price, entry price, target price, state and an explanation. They are persisted
(`data/candle_momentum_state.json`) and visible in the Command Centre timeline and in `/api/candle-momentum/state`.

BUY / SELL appear **only** in `RISK_PENDING` / `EXECUTING`. A notification and the decision panel read the same record, so they cannot disagree.

## 5. One entry per candle, restarts, reconnects

* The candle id and its lifecycle are persisted. A browser refresh only *reads*; it changes nothing.
* A second signal for the same candle is impossible: after any entry decision the lifecycle is terminal for entries, and the setup ledger and the Autopilot's attempted-list refuse a second order too.
* **Backend restart**: a candle with an open position resumes tracking it (no new entry). A candle whose signal had been validated but not sent is closed to entries (`EXPIRED`, "TradeMate restarted ..."), and a candle whose order may have been in flight becomes `EXECUTION_FAILED` with `needs_reconcile`: it is **never** retried or re-entered after a restart. Fail-safe over opportunity.
* **Watchdog stop / start**, **MT5 reconnect**: the persisted lifecycle for the candle is reused; nothing executes twice. Monitoring starting mid-candle with price already beyond the trigger is judged like a jump (logged as `first_observation_beyond_trigger`) and the same validity rules apply.
* **Retries** are off by default. With `max_entry_attempts > 1`, a retry happens only after an order that **provably was not sent** (`market_unavailable`, `connection_failed`), after `retry_delay_seconds`, within the signal's validity, and releases only that one setup's claims. An unknown outcome (sent but unconfirmed) is never retried.

## 6. Configure it

1. **Command Centre**: choose the instrument, strategy *Candle Momentum* and timeframe (M1, M5, M15, ... as long as the broker serves it). The **Candle Momentum** card appears with the live state and a *Settings* section (enable/disable, trigger, target, protective stop, entry-validity fraction, slippage tolerance, signal expiry, quote freshness limit, attempts). Settings are saved per instrument + timeframe (`data/candle_momentum_config.json`), validated by the server, and cannot be changed while Autopilot runs. Lot size is the existing Lot size card.
2. **Defaults** for anything you have not customised: `config/settings.toml` `[strategy]` (`candle_trigger_pips`, `candle_target_pips`, `candle_sl_buffer_pips`) and `[candle_momentum]`.
3. An old `settings.toml` that still contains `candle_late_pips` keeps loading; the key is ignored and a warning is logged.

### Important: set the EA's snapshot interval

The engine can only see prices as fast as the MT5 bridge writes them. The Expert Advisor's input **`UpdateIntervalMs` defaults to 1000**, i.e. one snapshot per second. The engine polls every 0.25 s,
but it will see at most one new price per second (the card shows the real rate as "broker price updates per second"). For scalping, set `UpdateIntervalMs` to about **250** in the EA's inputs
(minimum effective is the EA's own command-poll interval). This release does **not** modify the EA.

## 7. Watchdog and Autopilot

* **Watchdog** (Command Centre → start monitoring, or `POST /api/strategylab/watchdog/start`): for Candle Momentum it starts the engine for that instrument/timeframe and becomes its notifier; it
  does not poll. Starting twice never creates a second worker. Its status reports `mode: tick_engine` and the engine's real cadence, not a polling interval.
* **Autopilot**: unchanged rules (one stop condition, one open trade at a time, Risk Engine approval, one attempt per setup). New: the engine **wakes** the Autopilot the instant a validated signal exists, so it does not wait for its 5-second poll. Autopilot does nothing for a candle that is merely bullish or bearish.
  It requires `[trading] enabled`, MT5 connected and the Watchdog monitoring the same selection.
* If Autopilot is already managing an open trade it will not take the next signal; that signal then expires. (Existing Autopilot rule, unchanged.)

## 8. Instrument conversion

Every distance goes through `trademate/instruments.py`. A forex pip, a metal pip, an index point and a synthetic index unit are different things there, and the engine never assumes one. For the Deriv
Crash 100 account convention you stated (0.01 lot = $0.01 per pip, 0.10 = $0.10, 0.06 = $0.06) the instrument engine treats 1 pip = 1.0 price unit and **verifies** it against the broker's tick
size / tick value / contract size; if the broker's data does not reproduce that convention, or required data is missing or stale, the instrument is **UNVERIFIED**, the card says exactly why, and automatic
execution is blocked. Nothing is guessed. (Tests: `test_crash100_*`.) The broker's minimum stop distance (`stops_level`) is enforced by the existing pipeline (`min_stop_problem`), not by the engine. **Freeze levels are not enforced anywhere in TradeMate** (neither before nor after this change); the engine reads and displays the instrument data but does not add a freeze-level check. That is a known gap, listed in section 11.

## 9. Diagnosing a late or missed entry

`GET /api/candle-momentum/events?candle_id=<id>` (also `symbol`, `timeframe`, `after_id`, `limit`) and `data/candle_momentum_events.jsonl`. Every event carries the candle id.
Events: `new_candle_detected`, `direction_changed`, `trigger_calculated`, `trigger_crossed`, `tick_received` (while a signal is live), `quote_gap`, `quote_freshness`, `entry_evaluation_started`,
`entry_evaluation_completed`, `remaining_target_distance`, `risk_engine_decision`, `order_submitted`, `broker_response`, `execution_slippage`, `position_opened`, `target_reached`, `position_closed`,
`entry_missed`, `entry_invalidated`, `feed_down`, `feed_recovered`, `reconnected`, `watchdog_failure`, `state_restored`, `retry_scheduled`, `latency`, `transition`.

| Symptom | Look at |
|---|---|
| Missing ticks | `quote_gap` events; "broker price updates / s" on the card (EA `UpdateIntervalMs`) |
| Stale prices | `quote_freshness` with `fresh:false`, hub state `data_problem` |
| Slow evaluation / delayed hand-off | the `latency` event: `detect_to_signal_ms`, `signal_to_pickup_ms`, `pickup_to_approved_ms`, `approved_to_fill_ms`, `trigger_to_fill_ms`, `quote_age_at_trigger` |
| Incorrect trigger | `trigger_calculated` + the card's "Instrument conversion" row |
| Broker execution delay | `approved_to_fill_ms` and `broker_response` |
| Excessive slippage | `execution_slippage`; a MISSED reason mentioning the slippage guard |
| Genuinely missed | `entry_missed`: the reason gives units beyond the trigger, the % of the target consumed and the remaining units |

## 10. Tests

See `TESTING.md` ("Candle Momentum v2"). New: `tests/test_candle_momentum.py` (engine, state machine, persistence, faults, instruments), `tests/test_candle_momentum_e2e.py` (real Autopilot, Risk Engine,
pipeline, adapter and fake bridge; real threads; HTTP API), `tests/test_browser_candle_momentum.py` (real browser), `tests/candlemomentum_test.js` (panel logic). All use fakes: **no test can place a real order.**
The eleven v1 snapshot tests in `tests/test_candle_and_choch_targets.py` asserted the removed behaviour (including the 2-pip rule) and were retired on purpose; the mapping to their replacements is in
that file's `CandleMomentumSnapshotHookTest` docstring. The CHoCH, pips, lot, hub and ledger tests in that file are untouched.

## 11. Known limitations

* **Not a true tick stream.** Prices arrive at the EA's snapshot cadence (default 1 s). Movement between snapshots is invisible to the engine; a spike that reverses inside one interval is never seen.
* **Candle open comes from the broker's forming candle** over the file-command channel (a few hundred milliseconds), read at each expected boundary (using the measured broker clock) and re-verified every `candle_verify_seconds`. If the broker has not created the new bar yet, the engine keeps asking; until it has the real open it makes no judgement. If the broker clock is unknown, candles are verified every second instead.
* **Latency remains.** After a signal the Autopilot still runs the full existing pipeline (account reads, Risk Engine, re-validation, order, placement check), which costs real time over the file bridge. The `latency` event measures it; v2 does not skip or weaken any of those checks.
* **Bid-basis comparison** and the spread are included in "units beyond the trigger", so a wide spread consumes part of the validity window.
* Defaults for the validity fraction, expiry and freshness are policy values, not tuned.
* One Autopilot trade at a time (existing rule); a signal arriving while one is open expires.
* A restart during a live signal forfeits that candle (deliberate).
* Broker **freeze levels** are not checked by TradeMate (pre-existing gap; not added here).
* The EA (`mt5/TradeMateBridge.mq5`) is unchanged and was not compiled or run here; no live-MT5, live-broker or Deriv Crash 100 validation was performed.


## Update: no percentage-based late-entry rejection

`validate_entry` no longer returns MISSED for "price already travelled X% of the target beyond the trigger" (`target_consumed`) or for "price is already past trigger + target" (`target_passed`).
The `max_target_consumed_fraction` setting is gone (an old settings file that still has it loads, logs a warning and ignores it). However far price has moved beyond the trigger, the entry is proposed
at the CURRENT executable quote and the target is measured from that quote / the actual fill. What still refuses an entry: unverified instrument conversion, invalid or stale quote, missing protective stop,
price back inside the trigger (INVALIDATED), then downstream the Risk Engine, the execution-cost policy and the broker.


## Execution overhaul note (2026-11-02)

- Candle Momentum now bypasses the generic `[pipeline] max_entry_drift_stop_fraction` rule because its trigger/reference is not a stable final-entry anchor. It still undergoes fresh strategy qualification, live-price geometry, live reward-to-risk, spread, Risk Engine and broker validation at submission.
- The execution-cost assessment receives the instrument's strategy-unit size so diagnostic facts report spread, stop and target in both raw price and strategy units. The underlying stop/target economics remain price-to-price comparisons; no pip size is guessed.
- The 60-second `EXECUTOR_TIMEOUT_SECONDS` is an **unknown-outcome reconciliation timeout**, not a pre-trade countdown. It must not be removed or shortened to force retries: an MT5 request may have reached the broker even if its response is delayed. Autopilot's `_wake` event is explicitly signalled when a Candle Momentum decision is ready, so its loop can wake immediately rather than waiting for the normal poll interval.
- The 0.25-second Candle Momentum quote cadence is separate from the executor timeout. End-to-end latency still depends on the MT5 bridge tick cadence, quote freshness, risk evaluation and broker response.


## Updated user defaults (2026-11-02)

- Default execution timeframe remains user-selectable and includes **M1** (`EXEC_TIMEFRAMES`). Candle Momentum accepts M1, M5, M15, M30, H1 and H4.
- Default trigger distance remains **10 strategy units** from the candle open. A newly opened candle is monitored, not traded immediately: price must first travel the configured trigger distance in the candle's developing body direction. Bullish body movement maps to BUY; bearish body movement maps to SELL; neutral does not qualify.
- Default protective stop beyond the candle open is **50 strategy units** (`candle_sl_buffer_pips` / `sl_buffer_units`).
- Default target distance is **20 strategy units from the actual broker fill**, recalculated after MT5 confirms the fill.
- The 50-unit stop is a distance, not a promise that every instrument's 50 units are economically suitable. Risk sizing and broker stop/volume constraints remain authoritative; if the risk engine rejects the resulting size, the trade must not be sent.
- Lifecycle and desktop Watchdog notifications display the event's actual local date/time and elapsed age (for example, “3 minutes ago”) instead of relying on an ambiguous “NOW” label.


## Choosing the execution timeframe (M1, M2, M5, M15, M30, H1 or H4)

Candle Momentum trades on the timeframe YOU select in the Command Centre ("Execution timeframe"). The choice is saved per strategy (`data/trade_selection.json`) and survives a restart.
Nothing is ever substituted: M1 is not turned into M5 or the reverse. (The selector used to offer only M5 for Candle Momentum because a response field was overwritten; that is fixed.)

The selected timeframe drives every stage: the engine monitors the forming candle of THAT timeframe (a `symbol|timeframe` slot), the candle identity includes the timeframe (so M1 and M5 candles never share
duplicate protection), the decision is published under that timeframe, Autopilot is bound to it when started (it refuses to trade a different timeframe if you change it afterwards; stop and start it again),
and the order is created from that decision. Switching needs no restart; each timeframe keeps its own configuration and lifecycle state. The signal logic is the same for every timeframe and was not changed.
An unsupported timeframe is rejected loudly: it cannot be saved, and the Watchdog refuses to start without one instead of assuming M5.

## Trade the same candle twice (optional, off by default)

Settings > Execution & safety > "Allow multiple trades per candle". OFF = baseline: one entry per candle. ON = after the first entry on the current candle has **closed**, the engine arms one more lifecycle
(`<candle id>#2`) on the same candle with the same signal logic, so at most two distinct entries per candle, per strategy, instrument and timeframe. A repeated request is never a second entry: an approval created
before the first entry was submitted is refused, and copied child positions at the broker are never counted as entries. After a restart only the first lifecycle is restored (conservative).

Tests: `tests/test_candle_momentum_timeframes.py`, `tests/test_browser_candle_momentum_timeframe_choice.py`, `tests/test_risk_and_execution_features.py` (SameCandleTwiceTest).


## Investigation: why Candle Momentum can enter against a continuing candle group

**Finding (by design, measured by a characterisation test, not changed):** the original logic decides direction from the **forming candle against its own open price only** (`candle_direction`), and the trigger is the open plus or minus the configured number of units (`trigger_price`). `validate_entry` receives the forming candle's open, the trigger and the current quote; **no earlier candle is an input**. So when the current candle has fallen the trigger distance below its open, Candle Momentum signals SELL even if the previous several candles were bullish, and the same in the other direction. A single opposing candle inside a continuing group is therefore indistinguishable, to the strategy, from a genuine change. `tests/test_candle_momentum_timeframes.py::BaselineBehaviourCharacterisationTest` pins this behaviour.

**What was not done, on purpose:** you asked that the original signal logic is not redesigned, delayed or given new confirmation requirements. Nothing was changed, and no arbitrary delay or ban on opposing candles was added.

**How to separate the cases without altering the signal (for a later, measured step):** a temporary opposing candle is one whose body is small relative to the group and whose open sits inside the group's range; a genuine change usually breaks the group's last swing or follows exhaustion (shrinking bodies, a failed extension). The advisory route in `docs/AI_ADVISORY.md` (labels only, never blocking) is the safe way to measure whether such a distinction improves results before any rule is allowed to touch an entry. Bull/Bear Trading (`docs/STRATEGIES.md`) is the strategy that *does* require the candle group to agree.
