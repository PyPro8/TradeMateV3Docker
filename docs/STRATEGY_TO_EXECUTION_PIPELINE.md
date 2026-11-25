# Strategy -> Risk -> Execution pipeline (controlled mode)

Status: **implemented and tested against a simulated MT5 bridge only. Not yet verified against a real MT5 DEMO terminal** (see "What is NOT verified").
Nothing here enables unattended trading. Every order still needs an explicit `confirm: true` from the user, and still goes through the one
existing MT5 pathway (`adapter.place_order`).

## 1. Project state found (verified by reading the code and running the suite, not from the UI)

| Component | State | Evidence |
|---|---|---|
| Strategy Engine (`trademate/strategy/`) | Functional. Produces BUY / SELL / WATCH / WAIT with entry reference, structural stop, TP1/TP2, RR, invalidation, per-timeframe data status. | `tests/test_mtf_strategy.py`, `tests/test_mtf_api.py` |
| Risk Engine (`trademate/risk/`) | Functional and strict: per-trade, daily-loss, remaining-daily, position count, total active risk, broker min/max/step, rounding DOWN. | `tests/test_risk_*.py` |
| Execution gate (`execution_gate.py`) | Functional for MANUAL orders: re-judges every `/api/order` from live MT5 data; approves only the exact requested volume. | `tests/test_phase6.py`, `tests/test_execution*.py` |
| MT5 adapter / journal / reconcile | Functional: journal before send, UNKNOWN on lost answers, reconcile against the account, identical-order block while unresolved, DEMO-only + expected-server gate. | `tests/test_execution_safety.py` |
| **Strategy -> Risk connection** | **Did not exist** (see D1, D2). | section 2 |
| Pending orders (7B) | Functional (not touched). | |
| Prop-challenge rules | Not implemented. | no code |
| Autopilot | **Not built.** UI checkbox is disabled; `trademate/strategylab.py` reports it as `NOT_YET`. | section 8 |

## 2. Rejection-path audit

Path traced end to end:

```
candles (adapter.get_candles, 4 native timeframes)         strategy/service.py  gather_series / evaluate_symbol
  -> StrategyResult (BUY/SELL/WATCH/WAIT, levels)          strategy/mtf.py      evaluate()
  -> [GAP: nothing consumed the result]                    (see D1)
  -> Risk Engine                                           risk/engine.py       evaluate_trade()
  -> execution gate                                        execution_gate.py    gate_order() / review_order()
  -> MT5 order                                             broker/mt5_adapter.py place_order() -> bridge command "open"
  -> broker answer, journal, account reconciliation        mt5_adapter.py _write(), reconcile(); app.py _execute()
```

### Verified defects

| # | Defect | Where | Effect | Fix |
|---|---|---|---|---|
| D1 | **No code path from a qualified strategy setup to the Risk Engine or to an order.** `StrategyResult.to_trade_proposal()` existed but had no production caller (only tests). | `strategy/mtf.py` `to_trade_proposal`; `app.py` (no route) | A strategy-confirmed BUY/SELL could never reach MT5 except by the user retyping it by hand. | `trademate/pipeline.py` + `/api/pipeline/*` routes |
| D2 | **The proposal object lacked the field the Risk Engine requires.** `to_trade_proposal()` returns `strategy_risk_percent=None` and `volume=None`; `trade_param_problems()` then always reports "Strategy risk percentage is missing". | `risk/engine.py:171` | Even if wired up directly, **every** strategy proposal would have been rejected. Proven by `RealStrategyEngineContractTest.test_the_old_proposal_object_alone_could_never_be_approved` (real engine output, real Risk Engine). | The pipeline supplies the risk percentage (default `[risk] default_risk_percent`, capped by the user's max) and never changes `to_trade_proposal` (its existing test still pins "no size, no risk"). |
| D3 | **Strategy reference price is not the executable price.** The strategy's entry is the close of the last closed M5 candle. `gate_order` judges risk from the live ask/bid and requires the approved volume to equal the requested one. | `strategy/mtf.py:736`, `execution_gate.py:114-121,188-198` | A volume sized at the reference price can fail the execution gate (or silently carry more risk) once the live price differs. | Sizing uses the live ask (BUY) / bid (SELL); a drift limit and a spread limit are separate execution-eligibility checks; the gate re-judges the exact order again at execution. |
| D4 | **Oversized position is rejected instead of reduced.** If the risk-based size exceeds the broker maximum or TradeMate's per-order maximum, `calculate_position_size` rejects ("Widen the stop-loss or lower the risk"). | `risk/position_sizing.py:162-164` | A valid, risk-compliant setup is refused although a smaller position is strictly safer. | In the pipeline only: if the sole failed check is "Broker constraints" and the calculated size exceeds the cap, re-judge a position at the cap (rounded DOWN to the step) through the same Risk Engine. It risks LESS than requested and is reported (`volume_capped`, warning). `calculate_position_size` itself is unchanged, so manual behaviour and its tests are unchanged. A size **below the broker minimum is still rejected**, never rounded up. |

Not defects (verified, deliberately left alone): daily-loss "unknown" blocks trading (fail closed; it happens when the broker clock cannot be measured, e.g. market closed);
`broker constraints` rejection when the minimum lot would exceed the risk; per-trade risk judged on the REQUESTED risk so rounding can never hide an over-limit request;
`gate_order` refusing when the account changed between review and send.

### Rejection reasons -> code responsible

| Reason shown | Machine code | Stage | Code |
|---|---|---|---|
| "WATCH/WAIT is not an executable trade" | `not_qualified` | strategy | `pipeline.qualify` |
| "TF is stale / unavailable: ..." / unknown broker clock | `data_unusable` | strategy | `pipeline.qualify`; freshness itself is `strategy/timeframes.py` `prepare_series` |
| "The proposal is missing: stop-loss, ..." / no candle timestamp | `proposal_incomplete` | strategy | `pipeline.qualify` |
| "BUY needs stop < entry < target" / RR below the strategy's own minimum / direction mismatch | `proposal_inconsistent` | strategy | `pipeline.qualify` |
| MT5 not connected / no symbol spec / no live price | `market_unavailable` | live data | `pipeline.propose`, `prepare_execution` |
| "Trade exceeds maximum risk per trade" | `risk_rejected` | risk | `risk/engine.py:319` |
| "Daily loss limit reached" / "cannot be determined" | `risk_rejected` | risk | `risk/engine.py:268-276` |
| "Maximum simultaneous position limit reached" | `risk_rejected` | risk | `risk/engine.py:321` |
| "Total active risk would exceed the maximum" | `risk_rejected` | risk | `risk/engine.py:322` |
| "Allowed size ... below the broker minimum" | `risk_rejected` | risk | `risk/position_sizing.py:158` |
| "Trading is disabled / close-only / long-only for 'X'" | `risk_rejected` | risk | `risk/position_sizing.py` (trade_mode checks) |
| live price drifted / through stop or target | `entry_drifted` | eligibility | `pipeline.eligibility_problems` |
| spread uneconomic for THIS trade (spread >= stop, or >= target; or the optional user ratio) | `spread_too_wide` | eligibility (execution-cost policy, `trademate/execution_cost.py`) | `pipeline.eligibility_problems` |
| reward-to-risk at the live price below the strategy's minimum | `rr_degraded` | eligibility | `pipeline.eligibility_problems` |
| `[trading] enabled = false` | `trading_disabled` | eligibility | `pipeline.propose`, `prepare_execution` |
| confirm missing | `confirmation_required` | execution | `pipeline.prepare_execution` |
| approval unknown / used / expired / invalidated | `approval_*` | execution | `pipeline.prepare_execution`, `ApprovalStore` |
| account, risk rules, or open positions/pending orders changed | `account_changed`, `risk_rules_changed`, `account_state_changed` | execution | `pipeline.prepare_execution` |
| setup no longer qualifies / thesis changed | `no_longer_qualified`, `proposal_changed` | execution | `pipeline.prepare_execution` |
| risk gate no longer approves the exact order | `risk_revalidation_failed` | execution | `execution_gate.gate_order` via `prepare_execution` |
| invalid/too-close stops, volume step, DEMO/server mismatch, identical unresolved order | outcome `execution_refused` | adapter (pre-send) | `broker/validation.py`, `mt5_adapter.place_order`, `_block_if_unresolved` |
| broker refuses (e.g. 10019 no money, 10016 invalid stops) | outcome `broker_rejected` + retcode + broker text | broker | `mt5_adapter._fail`, `app.py _execute` |
| answer lost / internal error after send | outcome `unknown` -> reconcile -> `executed` / `not_executed` | broker | `app.py _unknown_reply`, `mt5_adapter.reconcile` |

## 3. Responsibilities

* **Strategy Engine** decides qualification. `pipeline.qualify` is a *contract check*, not a second strategy: it requires a BUY/SELL with status
  `confirmed`, every gate passed, the selected candidate `execution_eligible`, all four timeframes `ok`, a known broker clock, a closed-candle
  timestamp per timeframe, symbol, setup family, entry reference, stop, TP1, RR information and an invalidation level, and levels that agree with the direction
  and with the strategy's own `min_rr`. It never repairs or invents a value.
* **Risk Engine** (unchanged) judges the proposal sized from the **live executable price** with the strategy's own stop and target. Limits are never loosened;
  an explicit request above the user's maximum risk is rejected, not clamped.
* **Execution eligibility** (new, separate, `[pipeline]` settings): live price still between stop and target, drift <= 25% of the stop distance, spread <= 25% of the
  stop distance, live reward-to-risk still >= the strategy's minimum, trading enabled. These are execution-time conditions, not strategy rules.
* **Execution** is `adapter.place_order` called from `app.py` only. `pipeline.py` contains no order call (enforced by an AST test).

## 4. The six states (reported separately)

`strategy_qualified -> risk_approved -> execution_eligible -> order_submitted -> broker_accepted -> position_confirmed`

Each is `true`, `false` or `null` (= not reached / unknown). `accepted_unconfirmed` is `(submitted true, accepted true, position null)`: an accepted request is **not**
proof of a position. `unknown` is `(null, null, null)`. After a confirmed order, the broker's position is compared with the approved order (symbol, direction, volume, stop, target) and any
difference is returned as `placement_check` plus a `warning`.

## 5. Endpoints (controlled / manual-confirmation mode)

* `POST /api/pipeline/propose` `{symbol, risk_percent?}` -> runs strategy, risk, eligibility. **Sends nothing.** On success returns an `approval` (id, expiry, symbol, direction, volume, entry, stop, target, risk amount/percent, RR, evidence, invalidation).
* `POST /api/pipeline/execute` `{approval_id, confirm: true}` -> re-validates everything and, only then, sends through `adapter.place_order`. Same outcome contract as `/api/order`.
* `GET /api/pipeline/approvals/<id>` -> approval status and recorded outcome.

Same CSRF origin check as every write route. Approvals live in memory only (a restart discards them). Each approval event is appended to `data/pipeline_log.jsonl`
(best effort; a logging failure never blocks or fails a trade).

### What an approval is bound to

symbol, direction, approved volume, stop, target, the strategy's thesis (setup family, rule id, invalidation level), account login + server + currency, the risk rules in force,
and a fingerprint of every open position (ticket, volume, stop, target) and pending order. It is single-use (atomic `consume`), expires after `approval_ttl_seconds`, and is
invalidated by any of: account change, rules change, position/order set change, thesis change, setup no longer qualifying, price drift, spread, failed risk re-check.
The strategy's *reference entry* is not part of the thesis (it moves with every M5 close); the live price is re-checked instead.

### Execution sequence (`pipeline.prepare_execution`)

confirm -> approval exists / unused / unexpired -> trading enabled -> terminal connected -> account identity -> risk rules -> positions/orders fingerprint ->
**fresh strategy evaluation** (still qualified, same thesis) -> eligibility at the live price -> **`gate_order`** (the existing Risk gate, exact volume) -> atomic consume -> `adapter.place_order` ->
broker answer -> reconciliation by the existing adapter -> placement check -> outcome recorded.
A read failure before the send (terminal down) keeps the approval and sends nothing; a proven change invalidates it.

## 6. Failure handling

| Situation | Behaviour |
|---|---|
| Terminal disconnected / data unavailable / stale candles / no live price | Not qualified or `market_unavailable`; nothing sent |
| Invalid symbol spec, trading disabled for the symbol | Rejected by the Risk Engine with the reason |
| Invalid / too-close stops | Strategy-side inconsistency, or refused by `broker/validation.py` before sending |
| Insufficient margin / broker rejection | `broker_rejected` with the broker's retcode and text; stages `(true,false,false)` |
| Timeout / lost answer | `unknown`; the adapter reconciles by READING the account; `automatic_resubmission: DISABLED`; the approval is already consumed so nothing can resend it |
| Duplicate click / concurrent requests | One wins `consume`; the rest get `approval_already_used`; tested with 4 concurrent threads -> 1 order |
| Identical order while one is unresolved | Blocked by the existing journal guard |
| Account changed during the flow | Approval invalidated |

## 7. Settings

```toml
[pipeline]
approval_ttl_seconds = 60
max_entry_drift_stop_fraction = 0.25
max_spread_stop_fraction = 0     # optional; 0 = off (default). The old universal 0.25 x stop rule was removed
```

These are execution-eligibility policy. They never relax the Risk Engine or the Strategy Engine. Defaults are conservative starting values, not tuned against data.

## 8. Autopilot: what exists and what is missing

Inspected: Autopilot is **not implemented** (disabled UI checkbox, capability registry says "Not built"). The pipeline above is the foundation, not Autopilot.
Remaining before any continuous loop (Analyse -> Discover -> Qualify -> Risk-check -> Execute -> Monitor -> Reconcile -> Record -> Repeat):

1. A scheduler/loop with a hard enable flag, a visible armed state and a one-click stop that also drops all unused approvals.
2. Auto-confirmation policy (today `confirm: true` is a human act). It must be DEMO-only first, per-symbol allow-list, max trades per day, cooldown after a loss.
3. A stop-initiating-trades latch driven by the Risk Engine's daily-loss lock and by repeated broker/uncertain outcomes; today the Risk Engine blocks each trade but nothing latches the loop.
4. Startup reconciliation: refuse to start while any journal record is unresolved or while positions without a stop exist (the Risk Engine already reports these; the loop must treat them as blocking).
5. Position monitoring after entry (invalidation level breach, trailing, partial exit): not implemented; only SL/TP exist.
6. Persisted outcome records for analysis (today: journal + `pipeline_log.jsonl`; no slippage/MAE/MFE).
7. Prop-challenge rules (overall drawdown, daily-loss variants, news locks): not implemented; the Risk Engine has a daily-loss % and total-active-risk %, but **no overall-drawdown limit**.
8. DEMO forward test (below), then a written go/no-go. Unattended LIVE trading is out of scope and must stay disabled.

## 9. What is NOT verified

* **No real MT5 in this environment** (no Wine, no MetaQuotes snapshot folder; `python -m trademate.broker` reports `DISCONNECTED`). Everything above is proven against `tests/fake_bridge.py`, which proves TradeMate's side of the protocol, not Headway's behaviour (retcodes, stops levels, freeze levels, fill/slippage, hedging vs netting, margin).
* Real stop/target placement, real rejection texts, real reconciliation timing.
* Behaviour with symbols whose tick value differs between gain and loss (indices, crypto, crosses) end to end; the Risk Engine handles `tick_value_loss`, but the pipeline was only exercised on EURUSD-like specs plus a synthetic 0.01-tick instrument.

### Next integration check (DEMO, needs your explicit authorization)

1. Compile/attach the EA, confirm `python -m trademate.broker` shows CONNECTED, the account is DEMO and the server equals `[mt5] expected_server`.
2. `python app.py`; call `/api/pipeline/propose` for a symbol where a qualified setup exists (or temporarily run the existing `python -m trademate.broker.real_demo_execution_test EURUSD --place-real-demo-order` with its two opt-ins to verify the order path at minimum volume).
3. Because a real qualified setup is rare, also verify the *mechanics* with a forced proposal: not possible without bypassing qualification, so wait for a real one, or accept that qualification-to-order is only proven by simulation until one appears.
4. After `/api/pipeline/execute`, compare in MT5: symbol, volume, SL, TP, ticket; confirm `placement_check.matches` is true and the journal/`pipeline_log.jsonl` agree.
5. Record the result in a dated DEMO report. Do not enable anything unattended.

## Addendum: strategy-aware pipeline (two selectable strategies)

`pipeline.qualify(payload, expected_strategy_id)` now refuses a result that does not come from the selected strategy (`strategy_mismatch`) and requires data only for the timeframes the result's own
strategy stamp lists (H1 and M5 for both strategies; results without a stamp keep the legacy four-timeframe check). Approvals bind `strategy_id` and `strategy_version`; selecting another strategy cancels all issued
approvals (`ApprovalStore.invalidate_all`) and `prepare_execution` independently refuses an approval made under another strategy (`strategy_changed`). See `docs/STRATEGIES.md`.


## Execution-cost policy (replaces the universal 0.25 x stop spread rule)

The Risk Engine approving a trade and the execution-cost policy blocking it are different stages and are reported as such ("The Risk Engine approved this setup, but the execution-cost policy blocked execution: ...").
All quantities compared are prices (bid, ask, stop, target), so there is no point/pip mix-up. The reported case (spread 12, stop 11, limit 0.25 x 11 = 2.75) was a genuinely unfavourable spread:
the spread exceeded the whole stop. The policy blocks only when the spread is >= the stop distance (stop touched at fill) or >= the target distance (a win nets nothing); in between it only warns.
