# Development history (archived notes)

The handover and implementation notes from earlier work sessions, kept in ONE file so that nothing they record is lost while the project folder stays tidy. Each section is the original file, unchanged except for this heading. They describe the project at the time they were written; the current behaviour is in the documents listed in README.md.


---

## Archived note: COMMAND_CENTRE_CONTINUATION1_FIX.md

# Command Centre Continuation 1 — Polling and analysis-state fix

## Source of truth
This checkpoint continues from the current `TradeMate-command-centre-interaction-fix.zip`. It does not revert to the older project.

## Change made
`static/js/strategylab.js`: the 10-second `/api/strategylab/status` poll previously cleared the analysis panels whenever the broker capability was not `Available`, and replaced the status line regardless of whether an explicit analysis request was in progress. This could erase the specific failure returned by `/api/strategy/mtf` and obscure progress/error feedback from the user's ANALYSE action.

The status renderer now updates the general status line only when an analysis request is not in progress. It reports broker unavailability without automatically clearing the analysis response/error panels. The `/api/strategy/mtf` endpoint remains authoritative for the detailed analysis/data-quality outcome. No freshness validation was weakened and no setup can be confirmed by this UI change.

## Request flow verified by code inspection
1. Command Centre's `analyse()` posts the selected symbol to `/api/symbols/add`.
2. It then requests `/api/strategy/mtf?symbol=...` with `cache: 'no-store'`.
3. `app.py` delegates to `evaluate_strategy(adapter, symbol, settings, now_utc=time.time())`.
4. The strategy service obtains M5, M15, H1 and H4 candles through the adapter and validates them in `trademate/strategy/timeframes.py`.
5. The response is rendered in the browser or the returned error is shown in the status line.

## Safety
No real orders are placed by this analysis path. Candle freshness, timestamp integrity and unknown-broker-clock safeguards are unchanged. This fix does not establish that the live MT5 feed is fresh.

## Verification
- `node --check static/js/strategylab.js`: passed.
- `python -m compileall -q app.py trademate tests`: passed.
- `python -m unittest tests.test_mtf_timeframes tests.test_mtf_strategy tests.test_strategy_intelligence -v`: 84 passed.
- Browser automation: not run; no browser automation session was available in this environment.
- Live MT5: not run; no live terminal/bridge was connected.
- Full application integration tests: not run in this environment because Flask is unavailable in the system Python environment.

## Remaining limitation
The reported stale-data failure has not been reproduced against the user's running MT5 terminal. If it persists, the per-timeframe diagnostics from `/api/strategy/mtf?symbol=<symbol>` and bridge snapshot/server-clock data are needed to identify whether the feed is genuinely stale or the clock/timestamp interpretation is wrong. Do not weaken freshness rules to hide this condition.

## Recovery
Extract this ZIP into a separate directory and run it using the project's documented setup. Keep the prior working directory unchanged until local interaction and live-data checks are completed.


---

## Archived note: COMMAND_CENTRE_INTERACTION_FIX.md

# Command Centre interaction fix checkpoint

## Change
The Command Centre's instrument search input, saved-MT5-market dropdown, and ANALYSE button are no longer disabled solely because the status endpoint reports the broker disconnected/unavailable or status polling fails. This removes the UI dead-end where controls show a prohibited cursor and the user cannot request an operation to see the actual backend diagnostic.

## Safety boundary
This does not mark the broker as connected and does not bypass server-side checks. `/api/symbols/add`, `/api/symbols/search`, `/api/broker`, and `/api/strategy/mtf` remain responsible for validating the instrument, broker availability, current market data and strategy eligibility. A failed/stale data request must not create a confirmed setup or send an order. Watchdog and Autopilot states remain governed by their existing capability status.

## Validation performed in this environment
- `node --check static/js/strategylab.js`: PASS.
- `python -m compileall -q app.py trademate tests`: PASS.
- `python -m unittest tests.test_watchdog -v`: 7 tests PASS.
- `python -m unittest tests.test_strategylab -v`: BLOCKED; Flask is not installed in this inspection environment.
- `node tests/labgreeting_test.js`: FAIL due to an existing greeting expectation mismatch (`Good morning ` vs `Good morning. Welcome to Command Centre.`). This is unrelated to the interaction change and was not modified in this task.
- Browser/Playwright and live MT5 verification: NOT RUN.

## How to verify
Start TradeMate and open Command Centre. Even if broker status is disconnected or unavailable, the instrument input, saved-market dropdown, and ANALYSE button should not show the disabled/prohibited cursor. Attempting an operation should show the backend's actual error if MT5/data is unavailable. With a valid connection and fresh data, normal analysis should proceed. This checkpoint has not been verified against the user's live MT5 setup.


---

## Archived note: COMMAND_CENTRE_WATCHDOG_CHOCH_ALERTS.md

# Command Centre Watchdog: CHoCH alerts and checkbox fix

## What changed

- Fixed the Watchdog toggle being disabled based on the Market selector's status badge. The toggle now follows the actual broker connection state and remains usable while monitoring so the user can stop it. The server still validates the connection and instrument before starting; the browser change does not bypass backend checks.
- The read-only Watchdog now emits deduplicated alert events for **confirmed, closed-candle CHoCH events** returned by the Strategy Engine, including symbol, timeframe, direction and break level.
- CHoCH alerts are ignored when their timeframe data is not `ok`, when the analysis pass is stale/incomplete, when the event is provisional, or when it has a same-candle conflict. The Watchdog does not alert on an intrabar CHoCH that may disappear before candle close.
- Browser desktop notifications use the existing **Enable browser alerts** permission button. Sound uses the existing browser audio preference, now labelled **Sound for CHoCH alerts**. Browser audio requires a user gesture, so the user must switch sound on once in the page; the browser may also require the page to remain open and permitted to play audio.
- Existing qualified BUY/SELL setup events continue to work. This feature is monitoring/alerting only and never places orders.

## Verification

- `python -m unittest tests.test_watchdog -v`: 9 passed.
- `node --check static/js/strategylab.js`: passed.
- `python -m compileall -q app.py trademate tests`: passed.
- Tests cover one-time confirmed CHoCH event generation, deduplication, and rejection of provisional, conflicted, and stale-data events.

## Limitations

No live MT5 session or interactive browser was available for this checkpoint, so actual desktop notification permission, sound playback on the user's machine, and the live broker connection have not been verified here. Alerts require the TradeMate server and Watchdog to be running; closing the browser prevents browser-based sound/desktop notifications from being delivered by this page.


---

## Archived note: CONTINUATION1_INVESTIGATION_REPORT.md

# Continuation 1 — Command Centre investigation report

## Source of truth
This checkpoint was inspected from `TradeMate-command-centre-interaction-fix.zip`, not from the older ZIP.

## Findings verified from code
- Command Centre browser analysis posts the selected symbol to `/api/symbols/add`, then requests `/api/strategy/mtf?symbol=...` (`static/js/strategylab.js`).
- `/api/strategy/mtf` delegates to `evaluate_strategy(...)` (`app.py`), which gathers M5, M15, H1 and H4 through `trademate/strategy/service.py` and the broker adapter.
- The multi-timeframe validator in `trademate/strategy/timeframes.py` deliberately requires a known broker clock to classify the newest candle as closed and applies candle-age, grid-alignment, history-depth and integrity checks. These safety checks should not be weakened just to remove the stale warning.
- The current UI interaction fix explicitly leaves the instrument input and saved-market dropdown enabled and allows the analysis button while idle, even if broker status is unavailable. This was confirmed by code inspection, not by an actual browser session.
- Status polling runs every 10 seconds. `render()` clears the displayed analysis when the status endpoint says the broker is not available, and overwrites the status line. This can erase an error/result during polling; it is a confirmed UI behaviour, but it does not by itself prove the cause of the user's stale-candle result.

## Tests run in this environment
Command: `python -m unittest discover -v`
- Test cases reported as passing: 439
- Failures: 3
  - Existing Lab greeting expectation mismatch (`Good morning ` vs `Good morning. Welcome to Command Centre.`)
  - Order-block prefix-consistency failure (`test_prefix_consistency_holds_on_random_series`)
  - Project-integrity import check failure, cascading from unavailable Flask imports
- Errors / blocked imports: 36, primarily `ModuleNotFoundError: No module named 'flask'` in this inspection environment.
- Total test count reported by unittest: 478
- Separate command `python -m unittest tests.test_watchdog -v`: 7 passed.
- `node --check static/js/strategylab.js`: passed.
- `python -m compileall -q app.py trademate tests`: passed.

Counts above distinguish successful tests from failures and import-blocked modules; they do not establish live MT5 correctness.

## Not verified
- Browser click behaviour / browser console (no browser automation run).
- Live MT5 bridge snapshot, broker clock, or candle timestamps.
- Whether the current stale-data message is correct or caused by timestamp/clock interpretation.
- Live consistency of Analysis and Command Centre structural facts.

## Decision
No speculative change was made to candle freshness, broker-clock interpretation, or strategy qualification because the reported live failure cannot be reproduced without the user's runtime/MT5 feed. The inspected code shows where to instrument the request and why weakening the validator would be unsafe. This checkpoint is an investigation/recovery artifact, **not a claim that the stale-data issue is fixed**.

## Next evidence needed
Run the app against the actual MT5 terminal and capture the Command Centre response from `/api/strategy/mtf?symbol=...`, the matching bridge snapshot timestamp, and the per-timeframe diagnostics (`status`, `reason`, `newest_closed_time`, `lag_seconds`, `clock_known`, `issues`). Do not include account credentials or other secrets.


---

## Archived note: HANDOVER_PENDING_TEST_FIX.md

# Handover: repair of the failing Phase 7B pending-order browser test

Test: `tests.test_browser_phase7b_pending.PendingUiTest.test_editing_a_pending_order_shows_the_brokers_values_and_cancel_needs_two_clicks`

## Root cause (confirmed by reproduction and a diagnostic, not guessed)

**A test defect (wrong page + ambiguous selector). The application is correct.**

* The pending-orders table is rendered by the shared `open_trades()` template macro, which is used twice: on the
  **Dashboard** (`#view-dashboard`) and on the **Trades** page (`#view-trades`). It is not on the Chart page.
* The test placed the order from the **Chart** page and never left it. Both copies of the pending row were therefore inside
  hidden pages (`hidden`, input box 0 x 0).
* The test's selector `[data-role=pending-body] tr[data-order] input >> nth=0` matched both copies; the first in document
  order is the Dashboard's. Playwright's log said exactly this: `locator resolved to <input ...>` then
  `element is not visible` for 30 s, then `Page.fill: Timeout 30000ms exceeded`.
* Not a timing problem (the element existed and held the broker's value `1.09900`), not an input-state problem
  (not disabled/readonly), and not a rendering defect.
* Four later lines of the same test (APPLY click, CANCEL ORDER click, the `CONFIRM?` check, the confirm click) were
  unscoped in the same way and would have failed on the hidden Dashboard row next.

## What was changed (one file)

`tests/test_browser_phase7b_pending.py` only. No application, template, CSS, EA, risk, gate or broker file was touched.

* New helper `open_view(name)`: clicks the sidebar link as a user does and waits until that page is actually visible.
* The failing test now opens **Trades**, scopes every selector to `#view-trades`, and addresses the Entry/SL/TP inputs by
  column.
* Assertions were **added, none removed or loosened**: exactly one row; the row shows the broker's values
  (`1.09900 / 1.09500 / 1.10500`) before editing; after APPLY the field shows the broker's new value (`1.09850`).
  All original assertions (one `modify_order`, price changed at the broker, first Cancel click only arms, `CONFIRM?`,
  second click sends exactly one `cancel_order`, orders empty) are kept.
* New regression test `test_the_pending_row_is_on_trades_and_dashboard_only_and_is_operable_there`: pins the structure that
  caused this (no pending table on Chart; one row on each of Dashboard and Trades; row hidden while on Chart; on both
  pages its inputs and buttons are visible/enabled/editable; merely looking at the row sends no command).
* Mutation check: with the row temporarily rendering the wrong number format, the repaired test FAILED
  (`['1.10','1.09','1.10'] != ['1.09900','1.09500','1.10500']`), then the change was reverted. The shipped
  `static/js/app.js` is byte-identical to the one in the supplied project.

`HANDOVER_PHASE7B.md`: two lines that still described this test as failing were updated to point here. `README.md` and
`TESTING.md` are unchanged: no user-visible behaviour changed and neither mentions this feature or test.

## Tests actually executed (Python 3.12.3, Flask 3.1.3, Playwright + Chromium; fake bridge only, no real MT5)

| # | Command | Result |
|---|---|---|
| 1 | `python3 -m unittest tests.test_browser_phase7b_pending.PendingUiTest.test_editing_a_pending_order_shows_the_brokers_values_and_cancel_needs_two_clicks` (BEFORE the fix) | **FAIL** (reproduced): `Page.fill: Timeout 30000ms exceeded`, element not visible |
| 2 | same command (AFTER the fix), run 4 times | **PASS** x4 |
| 3 | `python3 -m unittest tests.test_browser_phase7b_pending -v` | **PASS**: Ran 11, OK (10 existing + 1 new regression), 0 skipped |
| 4 | `python3 -m unittest tests.test_pending_orders` | **PASS**: Ran 34, OK |
| 5 | `python3 -m unittest tests.test_execution tests.test_execution_safety tests.test_risk_limits tests.test_risk_sizing tests.test_server_validation tests.test_web tests.test_project_integrity tests.test_testrunner` | **PASS**: Ran 198, OK |
| 6 | `bash tests/run_tests.sh` (full suite) | **PASS**: Ran 707 tests in 290 s, OK, 0 skipped; 4 Node check scripts passed (5 'passed' lines); compile check OK |

The `Traceback` lines in the full-suite log are deliberate fault-injection tests (`RuntimeError: bad`/`boom`, a `Junk`
object) logging handled errors; the suite reports OK.

* **BLOCKED / NOT RUN:** the real-MT5 DEMO scripts (`live_phase6_test`, `real_demo_execution_test`) were NOT run: there is
  no MT5 here and they are opt-in. **No order of any kind was placed on a real broker.**

## Remaining limitations (unchanged by this repair)

* **MT5 execution is NOT verified.** These tests prove the web UI, the gate and the Python side against a *fake* bridge. The
  EA `mt5/TradeMateBridge.mq5` (0.5.0) has still never been compiled in MetaEditor or attached; until it is, TradeMate
  reports `pending_supported=false` and disables the pending types (fail-closed).
* Take Signal is not started; README/TESTING are not yet rewritten for Phase 7B; the Node checks `quotes_test.js` and
  `liquidity_js_test.js` are not in `run_tests.sh`'s Node list (all in HANDOVER_PHASE7B.md, unchanged).
* Pending orders are listed and edited on **Trades** and **Dashboard**, not on the Chart page. That is the existing design; it
  was not changed.

## Where to find and use the feature

1. **Place:** *Chart* page, trade panel on the right. Set **Order type** to Buy Limit / Sell Limit / Buy Stop / Sell Stop,
   enter Lot, **Entry price**, SL (and optional TP), press **Place pending order**, read the review dialog (Risk Engine verdict),
   and confirm with **Place Buy Limit** (etc.). Nothing is sent before you confirm.
2. **See / edit:** open **Trades** (sidebar), section **Pending orders** (also shown on **Dashboard**). Edit Entry, SL or TP
   in the row and press **APPLY**; the fields then show the broker's real values.
3. **Cancel (two clicks):** press **CANCEL ORDER** (it only arms: the button turns into **CONFIRM?** for 3 seconds), then press
   it again to cancel.
4. Pending types stay disabled, with an explanation, until the updated EA is compiled and attached (DEMO account only).


---

## Archived note: HANDOVER_PHASE7B.md

# TradeMate handover: Phase 7B (IN PROGRESS, not finished)

Snapshot of the project exactly as it stands. Nothing was cleaned up, refactored or completed while packaging.
Base: the Phase 7A.1 project (Analysis page, two-way SL calculator, Headway-Demo server check, Full Test page).

## Phase 7B status

| Part | Status |
|---|---|
| Audit | Done (summary below) |
| Part 6: live bid (red) / ask (green) chart lines | **Done and tested.** The chart already drew bid/ask from MT5; repaired colours (red/green, solid), live-only data, stale/unavailable states, spread from the real bid/ask |
| Part 3: Liquidity Mechanics Engine | **Done and tested** (engine, settings, API, Analysis panel, chart overlay, navigation) |
| Part 5: Pending orders | **Mostly done, not finished.** Python side, API, risk gate, UI done; EA code written but NOT compiled; one browser test failing; docs not updated |
| Part 4: TAKE SIGNAL | **NOT STARTED** |
| Part 9: README / TESTING.md rewrite for 7B | **NOT DONE** (README still describes Phase 7A.1) |
| Part 10: final full verification | **NOT DONE** (see tests) |

### Audit findings that shaped the work
* **No signal system exists** (Strategy page says "not implemented yet"; no signal object, store or lifecycle). Take Signal therefore needs a new minimal signal store + lifecycle that feeds the EXISTING gate (`review_order`/`gate_order`) and confirmation dialog. Nothing for it exists yet. Do not fake signals: no strategy engine exists.
* **The EA had no pending-order support** (only open/modify/close as market orders, no orders list in the snapshot).
* The bridge exports **no tick timestamp and no broker clock**, so "live" quote = bridge snapshot fresh + a recent successful poll.

## What was built (files)

**Bid/ask:** `static/js/quotes.js` (pure live/stale/unavailable rules), `static/js/app.js` (draw block "bid (RED) / ask (GREEN)", `TradeMateApp.chartQuotes()`), CSS/palette; tests `tests/quotes_test.js`, `tests/test_quotes_js.py`, `tests/test_browser_phase7b_quotes.py`, `tests/browser_support.py` (shared Playwright harness).

**Liquidity:** `trademate/liquidity/engine.py` (rules documented in its docstring), `trademate/liquidity/__init__.py`; `trademate/analysis.py` (liquidity view model + interpretation, fails soft); `trademate/chart_settings.py` (new validated keys `liquidity_visible`, `liquidity_tolerance_ticks`, `liquidity_sweep_mode`, `liquidity_max_pools`); `app.py` (`/api/candles?liquidity=1`, `/api/analysis` liquidity block); `static/js/liquidity.js`, `static/js/analysis.js`, `static/js/app.js` (overlay, toolbar button, settings controls), `templates/index.html`; `trademate/structure/engine.py` (public `clean_candles`). Tests: `test_liquidity_engine.py`, `test_liquidity_api.py`, `test_liquidity_js.py` + `liquidity_js_test.js`, `test_chart_settings.py` (extended), `test_browser_phase7b_liquidity.py`.

**Pending orders:** `trademate/pending.py` (validation: Buy/Sell Limit, Buy/Sell Stop; stop-limit deliberately unsupported), `trademate/broker/base.py` (`PendingOrder`), `trademate/broker/mt5_adapter.py` (`get_orders`, `supports_pending_orders`, `place/cancel/modify_pending_order`, reconcile), `trademate/execution_gate.py` (`review_pending`, `gate_pending`; **market orders now also count pending orders as potential exposure when the broker reports them**), `app.py` (`/api/pending/review`, `/api/pending`, `/api/pending/<t>/cancel|modify`, `orders` + `pending_supported` in `/api/broker`), `mt5/TradeMateBridge.mq5` (v0.5.0: `open_pending`, `cancel_order`, `modify_order`, `orders` list, `features:["pending_orders"]`, UTC<->server-time conversion for expiry), UI in `templates/index.html` + `static/js/app.js` + `static/js/slcalc.js` (order-type selector, pending table on Dashboard/Trades, review dialog, Edit/two-step Cancel; SL calculator follows the pending entry/side). Tests: `tests/test_pending_orders.py` (fake bridge), `tests/test_browser_phase7b_pending.py`, `tests/fake_bridge.py` (pending support).

**Also changed:** `trademate/liquidity` ids use plain epoch digits (a `:g` format bug that would collide real timestamps was found and fixed during testing); existing tests adjusted only where the shape legitimately changed (`test_chart_settings.py` pinned dicts now include the new defaults; `test_web.py` script/route allowlists include the new local scripts/routes).

## Known bugs / unfinished / risks

1. ~~`tests.test_browser_phase7b_pending`: 1 of 10 FAILS~~ **FIXED** (test defect, not an app defect: the test drove a hidden copy of the pending row from the Chart page). See `HANDOVER_PENDING_TEST_FIX.md`.
2. **EA (`TradeMateBridge.mq5` 0.5.0) is NOT compiled or tested.** Written by pattern from the existing EA; a crude brace check balances but MetaEditor has never compiled it. Until recompiled and attached, TradeMate reports `pending_supported=false` and disables pending types (fail-closed). **No pending order has ever been placed on a real MT5.**
3. **Take Signal not started.**
4. **README.md / TESTING.md not updated for 7B.** `tests/run_tests.sh` and the Full Test group lists (`trademate/testrunner.py`) do not yet name the new 7B test modules/Node checks (`quotes_test.js`, `liquidity_js_test.js`); the new Python modules are picked up by the "General" group automatically, the Node ones are not in any list.
5. The full suite has **not** been re-run since the 7B changes (see tests). Existing Phase 7A browser tests (23) were re-run once after the bid/ask change only.
6. Quote freshness cannot be exact (no tick timestamp from the bridge). Stop-limit orders unsupported by design. Order expiry assumes the EA's server-time conversion works (unverified).
7. Behaviour changes to be aware of: bid/ask colours changed (were violet/gold); chart no longer draws a price from a cached non-watchlist symbol; market orders are limited by existing pending orders.

## Tests actually run, with exact results (in this sandbox, Python 3.12, Node, Playwright/Chromium present)

Last FULL suite (`bash tests/run_tests.sh`), run at the end of Phase 7A.1, BEFORE any 7B code: **575 tests OK, 0 skipped; 5 Node checks passed.** Not re-run on this snapshot.

Run during 7B (module by module):
* `tests.test_browser_phase7a` (existing 7A browser tests, after bid/ask change): **23 OK**
* `tests.test_browser_phase7b_quotes`: **13 OK**; `tests/quotes_test.js`: passed
* `tests.test_browser_phase7b_liquidity`: **11 OK**
* `tests.test_pending_orders` (fake bridge): **34 OK**
* Regression after pending/market-gate change: `test_phase6 test_outcomes test_execution test_execution_safety test_history test_server_validation`: **126 OK**
* `tests.test_browser_phase7b_pending`: was 10 run, 9 OK, 1 ERROR; **now 11 run, 11 OK** after the repair (`HANDOVER_PENDING_TEST_FIX.md`)
* Final check on this exact snapshot (non-browser): `test_liquidity_engine test_liquidity_api test_liquidity_js test_quotes_js test_chart_settings test_pending_orders test_web test_slcalc_js test_analysis test_structure_engine test_structure_regressions test_server_validation test_sl_risk_first test_sl_price_first test_sl_instruments`: **Ran 321 tests, OK**
* `python3 -m compileall`: OK.
* NOT run: the real MT5 DEMO scripts (no MT5 here), any real-MT5 pending-order test, the full suite on this snapshot.

## How to continue
1. Diagnose the one failing pending browser test. 2. Run `bash tests/run_tests.sh` (about 4-5 min) and record the real result. 3. Compile `mt5/TradeMateBridge.mq5` in MetaEditor, re-attach, then test pending orders on the DEMO account only. 4. Implement Take Signal on top of a minimal signal store feeding `review_order`/`gate_order`. 5. Update README.md, TESTING.md, `tests/run_tests.sh`, `trademate/testrunner.py` groups. 6. Real-DEMO checklist stays opt-in (`TESTING.md`).

## Secrets
None in the project: no credentials, keys or tokens (scanned). Runtime data (`data/`, `logs/`) and caches are excluded. The expected MT5 server name `Headway-Demo` is configuration, not a secret.


---

## Archived note: HANDOVER_PHASE7C_FVG_ENGINE.md

# TradeMate handover: Phase 7C, Task 1 (Fair Value Gap detection engine)

Base: `TradeMate-phase7B-pending-fix-2.zip` (Phase 7B + pending-order test fix). Scope: FVG detection engine and its tests ONLY.
**No chart integration, no Analysis-page integration, no API route, no settings, no trade execution and no Risk Engine coupling were
added.** Order Blocks, Premium/Discount, overlays, a Strategy Engine and Take Signal were not touched.

## Audit findings
* No FVG code existed. The only mention was a placeholder `{"id": "fvg", "status": "not_available"}` in `trademate/analysis.py`
  (`MODULES`) and the note in `docs/ANALYSIS.md`. Nothing to reuse or repair; no duplicate created. **That placeholder is unchanged**,
  and a test pins it.
* Candle model: `[time, open, high, low, close]` rows (`broker.base.Candle.to_list()`); time = MT5 server epoch seconds. Candles carry
  **no symbol, timeframe, volume or completion flag**; symbol/timeframe are known only to the caller (`build_analysis(symbol, timeframe, ...)`).
* Existing conventions reused: `trademate.structure.clean_candles` (invalid excluded, duplicate times keep the last, out-of-order sorted);
  `last_candle_closed` config flag (liquidity default False, Analysis page always assumes the newest candle is forming); `confirmed`
  fields; epoch-digit ids; frozen dataclasses with `to_dict()`; package layout `trademate/<engine>/{__init__,engine}.py`; Analysis page
  refuses invalid data instead of guessing.
* Market Structure and Liquidity code: **not modified.**

## Files
Created: `trademate/fvg/__init__.py`, `trademate/fvg/engine.py`, `tests/test_fvg_engine.py` (52 tests), `docs/FAIR_VALUE_GAPS.md`, this file.
Modified (docs only): `README.md` (layout line + one short Phase 7C section), `TESTING.md` (one section), `docs/ANALYSIS.md` (note that the engine exists but is not wired in).
No other file changed (verified with `diff -rq` against the supplied ZIP).

## Detection rules
Bullish: `c3.low > c1.high`, gap `lower=c1.high, upper=c3.low`. Bearish: `c3.high < c1.low`, gap `upper=c1.low, lower=c3.high`. Strict:
touching is not a gap. c2 is not tested; no quality score, size filter or setup meaning. Every 3-candle window is examined.
Result per gap: id `FVG-BULL|BEAR-<c1 epoch>`, direction, upper, lower, size, first/middle/third/formed time, indices, confirmed,
confirmed_time, symbol, timeframe. Pure function of the candles, so repeat/growing runs never duplicate or rewrite a gap.
Call: `from trademate.fvg import analyze_fvg, FVGConfig; analyze_fvg(candles, FVGConfig(last_candle_closed=False), symbol=..., timeframe=...)`.

## Decisions you may want to review
1. **Fail closed on bad data.** Any invalid candle, price <= 0, or conflicting duplicate timestamp gives `data_status="invalid"` and no gaps
   (rather than detecting around it), because dropping a candle makes its neighbours adjacent and can fabricate a gap. One bad candle blanks
   the result. Alternative (segment the series at bad candles) is possible but needs a position-preserving cleaner.
2. **Price <= 0 is rejected** as impossible. The structure engine does not do this; it is applied in the FVG engine only. Instruments that
   can trade at/below zero would need this revisited.
3. **Default `last_candle_closed=False`** (newest candle assumed forming), so an FVG completed by the newest candle is `confirmed=False`.
4. **Time gaps are not detected** (no timeframe length is known); "consecutive" = consecutive in the supplied data.
5. Not implemented, by design (no spec): min gap size, strength/quality, fill/mitigation tracking, expiry, merging, multi-timeframe.

## Tests: exact commands and actual results (Python 3.12.3, Node v22.22.2, Flask 3.1.3, Playwright/Chromium present)
| Command | Result |
|---|---|
| `python3 -m unittest tests.test_fvg_engine -v` | **PASS**: 52 run, 0 failures (first run had 2 failures caused by wrong expectations in my own tests: a concatenated series legitimately contains a third cross-boundary gap; verified by hand against the rule and corrected; engine unchanged) |
| Mutation check (engine temporarily edited, then restored byte-for-byte): `>=`, `<=`, swapped boundaries, forming treated as confirmed, invalid-data gate removed | **PASS**: each mutant made the FVG tests fail (2, 2, 11, 3, 20 failures) |
| `python3 -m unittest tests.test_fvg_engine tests.test_structure_engine tests.test_structure_regressions tests.test_liquidity_engine tests.test_analysis tests.test_project_integrity tests.test_testrunner` | **PASS**: 222 run, OK |
| `python3 -m compileall -q .` | **PASS** |
| `python3 -m unittest discover -s tests -t . -v` (full suite incl. 73 real-Chromium browser tests, run on the final tree) | **PASS**: 759 run, OK, 0 failures, 0 errors, **0 skipped** (307 s) |
| `node tests/{pl,outcome,chart_style,slcalc,quotes,liquidity_js}_test.js` | **PASS**: all six report "all checks passed" |
| Extracted-ZIP check (`python3 -m unittest tests.test_fvg_engine tests.test_project_integrity` run in a fresh extraction of the deliverable) | **PASS**: 58 run, OK |
| Real-MT5 scripts (`real_demo_execution_test`, `live_phase6_test`) | **NOT RUN** (opt-in, need MT5; no automated test places an order) |

An earlier background full-suite attempt was killed with its shell before finishing and is not counted; the 759-test run above is the one reported.

## Known issues / not done
* `README.md` is still largely a Phase 4-era document (it predates this task; "NOT built yet" is stale). Left as is: rewriting it is out of scope.
* `tests/run_tests.sh` and `trademate/testrunner.py` were not changed: the new module runs in the full suite and in the Full Test page's "General" group automatically. No dedicated Full Test group was added.
* The 7B EA (`TradeMateBridge.mq5` 0.5.0) is still uncompiled/untested, as noted in `HANDOVER_PHASE7B.md`. Unrelated to this task.
* Nothing here says anything about profitability: an FVG is a geometric observation.


---

## Archived note: HANDOVER_PHASE7C_FVG_OVERLAY.md

# TradeMate handover: Phase 7C, Task 2 (FVG chart overlay + independent overlay controls)

Baseline: `TradeMate-phase7C-task1-fvg-engine.zip`. No Order Blocks, Premium/Discount, Strategy Engine, trade automation or Take Signal.
FVGs are display-only: nothing connects them to orders, position sizing or the Risk Engine, and the Analysis page is unchanged.

## Where to find and use it
* **Chart page** (sidebar > Chart) and the Dashboard chart: in the chart toolbar, next to the existing Market structure and Liquidity
  buttons, there is now a **dashed-box button = Fair value gaps** and a **layers button = all analysis overlays** (hides all if any are
  shown, otherwise shows all). Blue zone = bullish gap, purple = bearish; solid = confirmed, dashed + fainter = newest candle still forming.
* **Settings > Candlestick Appearance**: three labelled checkboxes (market structure, liquidity, fair value gaps) and **Show all / Hide all**.
* All switches are independent (any combination, including none); the toolbar and Settings stay in step. Default: all hidden.
* Persistence: saved by the server in `data/chart_settings.json`, so it survives page reloads and restarts and is shared by any browser
  using this TradeMate. Not synced between installs. Tested: reload keeps the choice; a failed save reverts the switch.

## How it works (smallest path, existing patterns reused)
* `app.py`: `/api/candles?...&fvg=1` adds an `fvg` block = the engine's own result (`_fvg_for_chart`), independent of `structure=1`.
  Insufficient / invalid candles or an engine failure give `available:false`, no gaps, and the candles still load.
* `static/js/fvg.js` (new, pure, Node-tested): maps the engine's timestamps to candle indices; boundaries pass through unchanged;
  it never reads a candle price, so nothing is recalculated in JS. Unusable gaps are dropped.
* `static/js/app.js`: `drawFVG` (zones behind the candles, replaced as a whole on every refresh, key-guarded against another
  symbol/timeframe), `setFvgVisible`, `setAllOverlays`, toolbar wiring, read-only hooks `fvgFrame()/analysisData()/overlays()`. Only 3
  existing lines changed (the candle fetch URL and the two lists of settings controls to enable); the rest are additions.
* `trademate/chart_settings.py`: `fvg_visible` (default false, validated, atomic with the other keys). `templates/index.html`,
  `static/css/style.css`: the buttons, checkbox, Show/Hide all, script include, one flex rule.

## Decisions to review
1. **Hiding never disables analysis, with one honest exception that is existing behaviour.** The chart always requests and keeps
   structure and FVG data; only drawing is gated. **Liquidity** is still requested only while its overlay is shown: an existing 7B test
   (`test_browser_phase7b_liquidity`, "no liquidity=1 while hidden") pins that, and I did not weaken it. The Liquidity *engine* and the
   Analysis page are unaffected (tested: `/api/analysis` returns structure + liquidity with every overlay hidden).
2. **Zones cover only candle 1 to candle 3.** Not extended to the right, because fill/mitigation is not tracked (extending would show
   already-filled gaps as live). So the zones are short on a busy chart.
3. **Hidden by default** (readable chart); FVG colours (blue/purple) are distinct from candles, bid/ask, structure, liquidity, trade lines.
4. **Not added to the Analysis page.** The `fvg` module there stays `not_available` (pinned by a test).
5. One toolbar "all overlays" button instead of a popover menu: it follows the existing button pattern; labelled checkboxes live in Settings.

## Test changes (no test weakened, deleted or skipped)
* `tests/test_chart_settings.py`: the two full-dict expectations now include `fvg_visible` (new `FVG_DEFAULTS`, as 7B did for liquidity).
* `tests/test_web.py`: the exact served-script list now includes `/static/js/fvg.js`.
* New: `tests/test_fvg_api.py` (33), `tests/test_fvg_js.py` + `tests/fvg_js_test.js`, `tests/test_browser_phase7c_fvg.py` (22 real-Chromium tests).
* The existing `test_toggle_hides_without_stopping_analysis` is unchanged (I ordered the URL as `...&fvg=1&structure=1` so it still passes).

## Test results (Python 3.12.3, Node v22.22.2, Playwright/Chromium present; all run on the final tree)
| Command | Result |
|---|---|
| `python3 -m unittest tests.test_fvg_api tests.test_fvg_js` | **PASS**: 34 tests |
| `python3 -m unittest tests.test_browser_phase7c_fvg` | **PASS**: 22 tests, ~100 s (first run 4 failures, all wrong expectations in my tests: grid line through the sampled pixel, a 7-candle series being "insufficient" for the Analysis page, a 6-candle series containing 2 gaps; plus 3 caused by one real, minor bug I fixed: a hidden chart kept reporting stale zone rectangles through the diagnostics hook) |
| Mutation checks on the chart code (boundary shifted, visibility gate removed, FVG fetched only when shown, key guard removed) | **PASS**: each was caught (the key-guard one only by the source-wiring test; the browser test was renamed so it claims only what it proves) |
| `python3 -m unittest discover -s tests -t . -v` (full suite, 95 real-browser entries) | **PASS**: 815 run, OK, 0 failures, 0 errors, 0 skipped (346 s) |
| `node tests/*_test.js` (7 files) | **PASS** |
| `python3 -m compileall -q .` | **PASS** |
| Fresh extraction of the ZIP: FVG engine/API/JS/browser tests + chart settings, web, integrity, test-runner | **PASS**: 213 run, OK |
| Real-MT5 scripts | **NOT RUN** (opt-in; no automated test places an order, and the browser tests assert no order endpoint is called) |

## Limitations / not done
* Zones are not extended or marked when later price fills a gap; no per-gap hover/click detail or list; only the loaded 300 candles.
* Overlay buttons are icon-only with tooltips/aria-labels (like the existing two); the labelled version is in Settings.
* Real-MT5 behaviour of the new route was not exercised (stub broker only), though it uses the same candle path as structure.
* `README.md` still contains Phase 4-era text elsewhere (pre-existing, untouched).


---

## Archived note: HANDOVER_PHASE7C_INTEGRATION_REPAIR.md

# TradeMate handover: Phase 7C chart and Analysis integration repair

**Baseline used:** `TradeMate-phase7C-task3-order-blocks.zip` (the latest Phase 7C delivery; it contains everything from tasks 1 and 2). The only
file uploaded in the request was the older task 2 ZIP, so this is an assumption: if you meant a different ZIP, tell me.
Not implemented, as instructed: the candle countdown timer, Premium/Discount, any Strategy Engine feature. **No analysis engine was modified**
(`trademate/structure`, `liquidity`, `fvg`, `orderblocks` are byte-identical to the baseline).

## What was wrong, and where to find each repaired feature

| # | Root cause (found by tracing the code) | Where you use it now |
|---|---|---|
| 1 Order blocks invisible | Task 3 built the engine only: no API data, no JS mapper/renderer, no switch, no saved preference. | **Chart** page (or the Dashboard chart): toolbar button with the box-and-edge-bar icon, tooltip "Order blocks"; or **Settings > Candlestick Appearance > "Show order blocks on the chart"**. Hidden by default. Teal-green = bullish, orange = bearish; solid + edge bar = confirmed, dashed/fainter = candidate or still forming. |
| 2 "Not available yet" | `MODULES` in `trademate/analysis.py` hard-coded `fvg` / `order_blocks` as `not_available`; `build_analysis` never called the engines; the page printed that text for every non-`available` module. | **Analysis** (sidebar): new **Fair value gaps** and **Order blocks** panels with the engines' real results, explicit empty states, and the reason when an engine cannot run. The **Modules** list now says *Available* (engine ran), *Unavailable now: reason* (exists but could not run) or *Not available yet* (multi-timeframe, trade setups only). Each card has "Show on chart" buttons. |
| 3 Pending orders missing | They reached the browser (`state.orders`, from `/api/state`) and filled the table, but the chart code never read them (in the chart, "pending" meant a proposed SL/TP edit). | **Chart** page / Dashboard chart: your broker's pending orders for the chart's symbol are drawn as **magenta dash-dot lines at the entry price** with a label like `▲ BUY LIMIT 0.10 @ 1.08350`, a second line `pending · SL · TP · #ticket`, and a right-axis price tag. Always on (no switch). Read-only, not draggable. |
| 4 Chart theme button | The chart toolbar button called the app-wide `toggleTheme()` (Phase 7A merged them; README still said "chart only"). | **Chart moon button** and **Settings > Chart theme (chart only)** (`Same as the application` / `Dark` / `Light`) change only the chart. **Top-bar switch** and **Settings > Theme (whole application)** are unchanged and change the whole app. Default `Same as the application`, so nothing changes until you pin the chart. |

## How it works (smallest compatible changes)
* **Order blocks:** `/api/candles?...&order_blocks=1` (new `_order_blocks_for_chart` in `app.py`) returns the engine's own result using the SAME
  structure parameters as the chart; works without `structure=1`. `static/js/ob.js` (new, pure) maps times to candle indices and passes
  boundaries through unchanged; `drawOB` in `app.js` draws behind the candles from the source candle to the newest candle, live blocks only
  (invalidated/expired are listed on the Analysis page). The data is always requested and kept; the saved `order_blocks_visible` only gates drawing.
* **Analysis page:** `build_fvg_view` / `build_order_blocks_view` + `module_list` in `analysis.py`; `analysis.js` renders them. Module status comes
  from the response (`view.available === true`), never from a name. An engine crash is logged and reported as unavailable without leaking
  internals and without affecting structure or the other engines. The page composes each block's sentence from the engine's fields with
  readable times (the engine's own `explanation` quotes raw epoch seconds, which I found displayed verbatim in my first draft and fixed).
* **Pending orders:** `symOrders()` reads `state.orders` (replaced on every poll, so modified / cancelled / filled orders update; empty when
  disconnected). Drawn in a separate block that is not part of `lines()`, so it can never be dragged or sent anywhere; included in the visible
  price range; an off-screen order gets an edge marker instead of stretching the scale; other symbols' orders are ignored.
* **Chart theme:** a separate `chart_theme` (`auto|dark|light`, in `data/chart_settings.json` with a browser copy) read through
  `chartTheme()` by the chart-drawing code only; `data-chart-theme` styles the chart panel/toolbar. New CSS restores the dark palette for
  "app light + chart dark" (the opposite pairing was already styled).
* **Preferences:** `order_blocks_visible` joins structure/liquidity/fvg (independent, in Show all / Hide all and the layers button, saved
  server-side, validated, atomic). Hiding any overlay never stops its engine. **Liquidity keeps its Phase 7B behaviour** (requested only while its
  overlay is shown): an existing test pins it and I did not weaken it; the Liquidity engine and the Analysis page are unaffected by any switch.

## Files changed
New: `static/js/ob.js`, `tests/ob_js_test.js`, `tests/test_ob_js.py`, `tests/test_repair_api.py` (48), `tests/test_browser_phase7c_repair.py` (44),
this file. Modified: `app.py`, `trademate/analysis.py`, `trademate/chart_settings.py`, `static/js/app.js`, `static/js/analysis.js`,
`templates/index.html`, `static/css/style.css`, `README.md`, `TESTING.md`, `docs/ANALYSIS.md`, `docs/FAIR_VALUE_GAPS.md`, `docs/ORDER_BLOCKS.md`,
`docs/MARKET_STRUCTURE.md` (theme addendum), and tests listed below.

## Existing tests I had to change (each pinned a state these requirements deliberately change; none deleted, skipped or loosened)
* `test_analysis`: module statuses are now per response (`unavailable` vs `not_available`).
* `test_liquidity_api`: an engine that could not run is `unavailable` (with a reason), not `not_available`.
* `test_chart_settings`: new default fields; the chart button handler must call `toggleChartTheme()` and must NOT call `toggleTheme()` / `setTheme(`.
* `test_web`: script list includes `ob.js`.
* `test_fvg_api`, `test_fvg_engine`, `test_orderblock_engine`: my own earlier "no integration yet" guards became "only the analysis and API layers use
  the engine, never trading code"; the "no order block text in the page" check now only forbids premium/discount.
* `test_browser_phase7a`: two tests pinned "the chart's own button = the app-wide switch" (the behaviour you reported). They now use the real
  app-wide controls (top-bar toggle, Settings select) and additionally assert the chart button leaves the application theme alone.
* `test_browser_phase7c_fvg`: four overlays instead of three in the exact-shape assertions.
* `tests/fake_bridge.py` (shared test helper, see below): a lock around `write_snapshot()`.

## Test results (Python 3.12.3, Node v22.22.2, Flask 3.1.3, Playwright/Chromium present; all on the final tree)
| Command | Result |
|---|---|
| `python3 -m unittest tests.test_repair_api tests.test_ob_js` | **PASS**: 49 tests |
| `python3 -m unittest tests.test_browser_phase7c_repair` (real Chromium, ~3 min) | **PASS**: 44 tests |
| Mutation checks: order block edge shifted; visibility gate removed; pending line at a wrong price; chart button calling the app-wide toggle; pending orders ignoring the symbol; zone colours reading the app theme; module list claiming "available" without checking | **PASS**: all 7 caught (one mutation of mine first failed to apply, so I redid it correctly) |
| `python3 -m unittest discover -s tests -t . -v` (full suite, 139 browser entries) | **PASS**: **967 run, OK, 0 failures, 0 errors, 0 skipped, 0 Python warnings** (513 s) |
| `node tests/*_test.js` (8 files) / `python3 -m compileall -q .` | **PASS** |
| Fresh extraction of the ZIP: 343 fast tests (repair API, ob.js, analysis, chart settings, web, integrity, test runner, order block / FVG engines and API, server validation) and the 44 real-browser repair tests | **PASS**: OK, OK |
| Real-MT5 scripts (`real_demo_execution_test`, `live_phase6_test`) | **NOT RUN** (opt-in, need MT5). **Pending-order drawing was verified against the project's FakeBridge and the real adapter/API path, not against a live Headway/MT5 terminal.** |

## Honest development notes
* The first browser run of the new module had 10 failures. Causes were my test mistakes (collapsed `<details>` text, a CSP-blocked string eval,
  stale settings carried between tests, two vacuous dark-theme assertions that read a transparent background, coordinates read before the first
  quote rescaled the chart) plus one real product defect: raw epoch numbers in the Analysis explanation text (fixed).
* **A full-suite run then failed once** in `tests.test_server_validation` (not touched by me) with `FileNotFoundError` inside `FakeBridge.write_snapshot`.
  I proved it is a **pre-existing race** in `tests/fake_bridge.py`: the bridge's own thread and the test thread write the same `.tmp` file. On the
  untouched baseline harness I reproduced the identical error (4 collisions in ~90,000 calls, which even crashed the bridge thread). I added a lock
  (0 collisions in 97,133 calls afterwards) because my new pending-order tests share that helper, then re-ran the full suite: it passed.

## Limitations
* Order block zones run to the newest candle and only live blocks are drawn; no click/hover detail, no mitigation marker; a busy chart can show
  many live blocks. Results depend on the structure parameters.
* A pending order's own SL/TP appear only as text in its label (not as lines), so they cannot be confused with position SL/TP lines. Pending
  lines cannot be dragged: edit or cancel in the Pending Orders table.
* The chart theme choice is saved on this computer (server file + browser copy), not synced between installs.
* Chart/Analysis behaviour was tested in headless Chromium with synthetic candles; real-market behaviour and a live MT5 terminal were not exercised.


---

## Archived note: HANDOVER_PHASE7C_ORDER_BLOCKS.md

# TradeMate handover: Phase 7C, Task 3 (Order Block detection engine)

Baseline: `TradeMate-phase7C-task2-fvg-overlay.zip`. **Engine only.** No chart, API route, Analysis-page or settings integration, no
Premium/Discount, Strategy Engine, trade automation or Take Signal. The FVG overlay and its controls are untouched (the browser
tests for them pass). **No existing code or test file was modified**, verified with `diff -rq` against the supplied ZIP.

## Audit
* No Order Block code existed: only the `order_blocks: not_available` placeholder in `trademate/analysis.py` (pinned by `test_analysis`).
  Nothing to reuse or repair; no duplicate created.
* Reused conventions: `trademate/<engine>/{__init__,engine}.py`; `clean_candles` cleaning; the Liquidity engine's pattern of taking
  `(candles, StructureResult, config)` and the FVG engine's `data_status`/`reason`, fail-closed bad-data policy, labels, `revision`,
  frozen dataclasses with `to_dict()`, `provisional` for the forming candle.
* Available information: candles only (no volume or order flow), plus the structure engine's confirmed swings and BOS/CHoCH events.
  The rule is therefore built from price and structure alone; nothing about institutional activity is claimed.

## Files
Created: `trademate/orderblocks/__init__.py`, `trademate/orderblocks/engine.py`, `tests/test_orderblock_engine.py` (59 tests),
`docs/ORDER_BLOCKS.md`, this file. Modified (docs only): `README.md` (layout line + one short section), `TESTING.md` (one section),
`docs/ANALYSIS.md` (a note that the engine exists but is not wired in).

## Detection rules (full text in `docs/ORDER_BLOCKS.md` and the engine docstring)
* **Candidate:** the last opposite-coloured candle (bullish OB: bearish candle) at or before a confirmed swing extreme and after the
  preceding opposite swing; zone = that candle's full high-low range. Not confirmed.
* **Confirmed:** the structure engine reports a BOS/CHoCH of exactly the swing that started the leg, after the extreme, not a
  same-candle two-sided break, and the extreme is the leg's origin (lowest low / highest high, earliest if equal). `confirmed_time` =
  the later of the break candle and the candle at which the swing became knowable. Optional `max_leg_candles` (off by default).
* **Invalidated (price only):** close beyond the zone's far boundary (default) or a wick beyond it. Checked first on a candle.
* **Expired (structure):** reference swing replaced, ambiguously broken, or broken by a different origin / beyond the leg limit.
* Statuses `candidate | confirmed | invalidated | expired`; `provisional=True` when the still-forming candle set the current status.
* Result fields: direction, upper/lower, source candle time and index/open/close, origin and reference times and level, identified /
  confirmed / invalidated / expired times, expired reason, confirming event type and time, provisional, rule id, one-sentence
  explanation, symbol, timeframe; result-level `data_status`, `reason`, `revision`, `diagnostics`.

## Decisions to review
1. **Confirmation is structure-based, not "large candle"-based.** There is no displacement/size/volatility test, so a slow grind that
   finally breaks the high also confirms; `max_leg_candles` is the optional lever. I did not invent a threshold.
2. **Only the deepest origin of a leg is confirmed.** Intermediate pullback candidates expire `NOT_QUALIFIED` rather than stacking.
3. **A fourth status, `expired`,** keeps price invalidation (reliable, price-based) separate from "can no longer be confirmed"
   (structural bookkeeping). Both are final.
4. **Fail closed on bad data** (invalid candle, price <= 0, conflicting duplicates) and on a structure result that does not match the
   candles (`inconsistent`), because indices would otherwise point at the wrong candles.
5. Zone = full candle range (open/close also returned, so a body zone can be derived). Default invalidation = close, like the
   structure engine's default break mode.

## Observed pre-existing issue (not fixed; outside this task)
The structure engine formats swing and event ids with `%g` (6 significant digits). With real epoch timestamps different swings/events
collide: on one 140-candle series, 27 swings had only 9 distinct ids. The Liquidity engine avoids this with its own helper. I did not
change it (ids are used by existing code/tests). The OB engine never relies on those ids (it uses times and indices); `swing_id` and
`confirming_event_id` echo them, so use `origin_time`, `reference_time` and `confirming_event_time` to locate the swing/event.

## Tests (Python 3.12.3, Node v22.22.2, Playwright/Chromium present; all on the final tree)
| Command | Result |
|---|---|
| `python3 -m unittest tests.test_orderblock_engine -v` | **PASS**: 59 tests |
| Mutation checks (engine temporarily edited, then restored byte-for-byte): no origin check, price not checked first, confirmation ignoring swing knowability, `<=` boundary, no expiry on replaced reference, any-colour source, deciding before the origin is knowable | **PASS**: all 7 caught. The last one **survived at first** (a real gap in my tests), so I added hand-derived fixture E and re-ran: now caught |
| `python3 -m unittest tests.test_orderblock_engine tests.test_fvg_engine tests.test_structure_engine tests.test_structure_regressions tests.test_liquidity_engine tests.test_analysis tests.test_project_integrity tests.test_testrunner tests.test_web` | **PASS**: 295 tests |
| `python3 -m unittest discover -s tests -t . -v` (full suite, 95 real-browser entries incl. the FVG overlay/controls) | **PASS**: 874 run, OK, 0 failures, 0 errors, 0 skipped (359 s) |
| `node tests/*_test.js` (7 files) | **PASS** |
| `python3 -m compileall -q .` | **PASS** |
| Fresh extraction of the ZIP: OB, FVG engine/API/browser, analysis, integrity, test-runner, web tests | **PASS**: 244 run, OK |
| Real-MT5 scripts | **NOT RUN** (opt-in; the new tests have no order path at all) |

Honest notes on development: the first test run had failures that were my own test mistakes (appending a candle legitimately creates
a swing high, hence a bearish candidate; a too-strict invariant; a wrong insufficient-data boundary). Two findings were real and fixed
in the engine: a confirmed block reported the first mapped swing as its origin instead of the swing that was actually the leg's origin,
and the origin tie-break initially contradicted the structure engine's (earliest vs latest equal extreme).

## Limitations
* No volume/order-flow, displacement, FVG requirement, mitigation/retest tracking, body-only zones, breaker blocks, scoring or
  multi-timeframe logic. Results depend on the structure parameters chosen. Unconfirmed candidates accumulate until invalidated/expired.
* Time gaps between candles are not detected. Not exercised against real MT5 data (synthetic candles only).


---

## Archived note: HANDOVER_STRUCTURE_AUDIT.md

# Handover: market-structure audit (CHoCH / BOS)

Changed: `trademate/structure/engine.py` (origin swing after a confirmed CHoCH; `reference_swing_id`, `reference_classification`,
role `origin_swing`), new `trademate/structure/facts.py` (shared direction/quality facts), `trademate/structure/trace.py`
(diagnostic CLI), `trademate/strategy/mtf.py` and `trademate/analysis_summary.py` / `trademate/analysis.py` (both use the shared facts),
docs (`MARKET_STRUCTURE.md`, `ANALYSIS.md`, README, TESTING.md). New tests: `tests/test_structure_audit.py`.

Existing tests changed, with reasons: `test_structure_regressions` (three expected reference pivots named non-swings: t(16)->t(19),
t(18)->t(21), t(34)->t(35)); `test_task2_analysis_and_defaults` (two tests asserted the old "label = mixed" conflation of direction
and quality); `test_browser_task2` (stale section title and badge text from an earlier UI change).

Known failures NOT caused by this work and NOT fixed: `tests.test_orderblock_engine...test_prefix_consistency_holds_on_random_series`
(Order Block engine, REFERENCE_LOST) and `tests.test_labgreeting_js` (greeting text). Both failed on the supplied project before any change.
The live CRASH_100 sequence was not reconstructed; no live-market verification was performed; no orders are sent by any test.


---

## Archived note: HANDOVER_TASK1_DAILY_LOSS_AND_CLOCKS.md

# Handover: Task 1 - daily loss reset, candle countdown, two clocks

Baseline: `TradeMate-phase7C-integration-repair.zip` (audited first; structure, liquidity, FVG and order-block engines are untouched).
Full explanation, configuration and limitations: `docs/DAILY_LOSS_AND_CLOCKS.md`.

## Root cause
When the broker's clock could not be read from a fresh 1-minute candle, `risk/account_state.py` silently used the COMPUTER's timezone for "today" and compared it
with broker-clock times. Computer behind the broker: yesterday's loss kept counting after broker midnight (no reset). Computer ahead: counters reset at the
computer's midnight, hours early, and a real loss/lock disappeared. The probe also flipped between the two from one refresh to the next.

## Files changed / added
Added: `trademate/brokertime.py`, `static/js/clocks.js`, `docs/DAILY_LOSS_AND_CLOCKS.md`, `tests/test_daily_loss_reset.py`, `tests/clock_test.js`,
`tests/test_clock_js.py`, `tests/test_browser_clocks.py`, this file.
Changed: `trademate/risk/account_state.py`, `trademate/risk/engine.py` (extra state fields, message wording), `trademate/config.py`, `config/settings.toml`,
`requirements.txt`, `app.py` (`/api/market/time`, resolver config, template variable), `templates/index.html`, `static/js/app.js`, `static/css/style.css`,
`trademate/testrunner.py` (two new groups, new browser module), `tests/run_tests.sh`, `README.md`, `TESTING.md`.
Existing tests edited (not weakened): `tests/fake_bridge.py` (its M1 candles / history times now behave like a real broker clock, `server_offset`, default 0);
`tests/test_web.py` (allowlists gain `clocks.js` and `/api/market/time`); `tests/test_repair_api.py` (the "no countdown in the page" guard now guards
against a placeholder, because the countdown is real).
Not changed: the EA (`mt5/TradeMateBridge.mq5`), all analysis engines, risk limits and rules.


---

## Archived note: HANDOVER_TASK2_CHART_CLEANUP.md

# Task 2 handover: chart cleanup and beginner-friendly analysis

See README.md ("Task 2") for what/where/how, docs/ANALYSIS.md for the summary rules, TESTING.md for tests.

Files: trademate/analysis_summary.py (new), trademate/analysis.py (fill status, relevant zones, summary hook), trademate/chart_settings.py
(red_black default, colors_chosen migration), static/js/app.js (defaults, wick outline, button removal, stale/unavailable notices,
pending-order freshness, flush on page hide), static/js/analysis.js, templates/index.html, static/css/style.css.

Not verified: anything against a live MT5 / broker. The FVG engine and Order Block engine were not modified.


---

## Archived note: IMPLEMENTATION_REPORT_AUTOPILOT_STRATEGIES.md

# Implementation report: strategies, Autopilot, Command Centre
Design, definitions and limits: docs/AUTOPILOT_AND_STRATEGIES.md.

Test status at packaging: non-browser suite 1337 tests OK. Browser suites OK except two pre-existing failures in test_browser_phase7a
(identical on the original ZIP). Nothing was run against a live MT5: verify on a demo account first.
Existing tests changed (reasons): catalogue now has three strategies; legacy Autopilot placeholder replaced by real Autopilot;
mocks accept the new exec_tf argument; CHoCH service fixtures use a wide pip target and a pip override; browser isolation of new state.


---

## Archived note: IMPLEMENTATION_REPORT_CANDLE_MOMENTUM_EXECUTION_OVERHAUL.md

# Candle Momentum Execution Overhaul — Verification Report

Date: 2026-11-02  
Base: user-provided `TradeMate-updated.zip`  
Working branch: targeted execution-policy changes; no unrelated rebuild.

## Implemented in this package

1. **Candle Momentum entry drift policy** — `trademate/pipeline.py` exempts only `strategy_id == "candle_momentum"` from the generic `max_entry_drift_stop_fraction` restriction. The live strategy is re-evaluated, levels are re-anchored to the current executable quote, and geometry, spread, reward/risk, Risk Engine and broker checks remain active. Other strategies retain their existing drift protection.
2. **Spread diagnostics** — the strategy-unit size is passed into spread assessment so diagnostic facts can include spread in strategy units as well as raw price. Spread economics remain evaluated in raw broker price space to avoid comparing unlike units.
3. **No artificial pre-entry countdown added** — Candle Momentum continues to evaluate on live quote updates and Autopilot has a wake event. The 60-second executor timeout is retained because it handles an unknown broker outcome after engagement/submission; removing it or retrying blindly can duplicate a real order.
4. **Documentation** — README and Candle Momentum documentation explain the policy and the executor timeout distinction.
5. **Regression tests** — tests cover Candle Momentum's exemption from generic drift while preserving the guard for CHoCH, and the existing candle lifecycle and spread cost rules.

## Important safety interpretation of spread

A spread that is greater than or equal to the full live stop distance is still rejected. For a market BUY, the fill is at ask while stops are triggered using bid; for a SELL, the reverse applies. If the spread already spans the stop distance, the stop can be effectively crossed at entry. A wider TP does not fix that. The old universal `0.25 × stop` restriction is not the active default in this implementation (`max_spread_stop_fraction = 0`), but genuine uneconomic geometry is still blocked.

The user's screenshots include both legacy-style 0.25-multiplier messages and spread/stop examples. The package changes the Candle Momentum drift policy and improves spread diagnostics, but the screenshots must be reproduced against the running deployment to establish whether an older process/configuration or another caller is still generating the legacy message.

## Direction mismatch — not certified fixed

The pure direction function maps bid above candle open to bullish/BUY and bid below candle open to bearish/SELL. The screenshots do not contain enough synchronized event data to prove why a bearish display coincided with a BUY proposal. The proposal's exact candle ID, quote timestamp, calculated direction, order side and MT5 submission must be correlated in a reproduced run. No claim is made that this anomaly is fixed in this package.

## Tests actually run

Command:

```bash
python -m unittest tests.test_execution_cost tests.test_candle_momentum -q
```

Result: **94 tests passed**.

Syntax compilation was also previously run:

```bash
python -m compileall -q trademate tests
```

Result: **passed**.

The full Flask/application pipeline suite was not run in this environment because Flask and Flask-SocketIO are not installed and package installation was unavailable. No MT5 terminal, broker connection, demo trade or live trade was used.

## Before live deployment

1. Run the full test suite in the project's normal virtual environment.
2. Reproduce spread and direction events using a simulated MT5 bridge and record the synchronized event timeline.
3. Verify symbol metadata and price units for the actual Storm 200 broker symbol.
4. Validate on a demo account before considering live trading.

This package is a tested targeted implementation, **not a certified production release**. Do not assume profitable or perfectly timed execution.


---

## Archived note: IMPLEMENTATION_REPORT_CANDLE_MOMENTUM_M1_DEFAULTS_NOTIFICATIONS.md

# Candle Momentum update — M1, stop/target defaults, notification timestamps

Date: 2026-11-02

## Changes

1. **M1 execution timeframe:** verified and regression-tested. Candle Momentum's existing selectable execution-timeframe contract already includes M1 (`EXEC_TIMEFRAMES`), the MT5 adapter serves M1, and the engine uses the shared timeframe duration map. No duplicate timeframe implementation was added.
2. **Entry is not at candle open:** preserved the existing trigger model. The default trigger remains 10 strategy units from the candle open. The engine observes the forming candle and waits for the configured directional body movement to reach the trigger; it does not submit merely because a new candle appeared. The current body direction is derived from live price versus candle open; high/low wick excursions do not create the directional signal.
3. **Protective stop:** changed the default `sl_buffer_units` / `candle_sl_buffer_pips` from 1 to **50 strategy units beyond the candle open**.
4. **Target:** changed the default `target_units` / `candle_target_pips` from 10 to **20 strategy units**. Existing fill-confirmation handling recalculates the take-profit from the actual MT5 fill price (BUY: fill + 20 units; SELL: fill - 20 units).
5. **Existing saved configurations:** added a narrow migration for the former built-in defaults only: saved stop `1.0` migrates to `50.0`, and saved target `10.0` migrates to `20.0`. Other custom distances and the trigger setting are preserved.
6. **Notification timestamps:** Candle Momentum lifecycle timeline and desktop Watchdog alerts now show the event's local date/time and elapsed age (seconds/minutes/hours/days ago), rather than relying on a misleading generic `NOW` label.

## Important behaviour retained

- Default trigger distance remains 10 units, so Candle Momentum waits for movement after candle open.
- Bullish body direction maps to BUY; bearish maps to SELL; neutral produces no entry.
- Target is set from the actual broker fill after MT5 confirms the fill.
- Risk Engine, position sizing, broker validation, stop/target validation, duplicate protection, and emergency stop are not bypassed.
- A 50-unit stop is a wider distance, not a promise of lower losses or higher win rate. Risk sizing must account for the wider stop, and broker/Risk Engine rejection remains authoritative.

## Verification

Commands executed:

- `python -m unittest tests.test_candle_momentum tests.test_candlemomentum_js tests.test_watchdog`
- `node --check static/js/candlemomentum.js`
- `node --check static/js/strategylab.js`
- `node tests/candlemomentum_test.js`
- `python -m compileall -q app.py trademate tests`

Results:

- Python focused suite: **101 tests passed**.
- Candle Momentum JavaScript checks: **13 groups passed**.
- JavaScript syntax checks: passed.
- Python compilation: passed.

The Flask-dependent `tests.test_strategylab` suite was not run because Flask is not installed in this execution environment. No MT5 terminal or broker connection was available, so no broker/demo/live execution test was performed.


---

## Archived note: IMPLEMENTATION_REPORT_CANDLE_MOMENTUM_V2.md

# Implementation report: Candle Momentum rebuild (v2)

Read `docs/CANDLE_MOMENTUM.md` for the design, the audit of the old behaviour and the operating guide. This file is the delivery report.

## Verdict, honestly

The engine is implemented, wired through Watchdog, Autopilot, the Risk Engine and the existing pipeline, and tested **in simulation**. **No live MT5 / broker / Deriv Crash 100 test was performed**, no real order
was placed, and the MT5 Expert Advisor was not touched, compiled or run. The strategy has no profitability evidence; with default settings its reward-to-risk is below 1.

## Root cause of the late entries (by code audit; not measured live)

A snapshot function was polled: Watchdog every 60 s, Autopilot every 5 s, plus two more candle fetches during propose / execute. A hard-coded `late_pips = 2.0` refused any entry more than 2 pips past the
trigger **and** looked at the candle's extreme, so after one miss every later poll in that candle repeated "Too late". The pipeline's `max_entry_drift_stop_fraction` (0.25 of the stop, ~2.7-3 pips here) was a second hidden
chase limit. Details and file references: `docs/CANDLE_MOMENTUM.md` section 2.

## What was built

New package `trademate/candle_momentum/`: `models.py` (16 states, legal-transition table, config), `logic.py` (pure rules), `machine.py` (thread-safe persistent state machine), `engine.py` (one worker, tick cadence),
`feed.py`, `store.py` (atomic persistence), `diagnostics.py` (structured events, secrets redacted), `result.py` (pipeline-compatible result), plus `static/js/candlemomentum.js` (Command Centre panel).

Integration (all small, additive): `strategies/service.py` routes Candle Momentum to the engine; `strategy/watchdog.py` monitors through the engine (no polling) and reports its real cadence; `autopilot.py` gains an
immediate `wake()`, an outcome `observer` and `forget_attempt()` (bounded retry); `pipeline.py` gains the optional `max_slippage` tolerance (default behaviour unchanged); `broker/*` gains one-read `get_quote()`;
`config.py` / `settings.toml` gain `[candle_momentum]` and lose `candle_late_pips` (an old file still loads, with a warning); `app.py` wires the engine and adds `/api/candle-momentum/{state,config,events}`.

Untouched: Risk Engine, execution gate, CHoCH, EMA + ATR, MT5 EA, order journal, all saved data under `data/`.

## Files

Changed (22): `README.md`, `TESTING.md`, `app.py`, `config/settings.toml`, `docs/AUTOPILOT_AND_STRATEGIES.md`, `static/css/style.css`, `static/js/commandcentre.js`, `templates/index.html`,
`tests/fake_bridge.py` (additive: scripted candles), `tests/run_tests.sh`, `tests/test_candle_and_choch_targets.py`, `tests/test_web.py`, `trademate/autopilot.py`, `trademate/broker/base.py`,
`trademate/broker/mt5_adapter.py`, `trademate/config.py`, `trademate/pipeline.py`, `trademate/strategies/base.py`, `trademate/strategies/candle_momentum.py`, `trademate/strategies/registry.py`,
`trademate/strategies/service.py`, `trademate/strategy/watchdog.py`.
Added: `docs/CANDLE_MOMENTUM.md`, `IMPLEMENTATION_REPORT_CANDLE_MOMENTUM_V2.md`, `static/js/candlemomentum.js`, `trademate/candle_momentum/` (9 files), `tests/candlemomentum_test.js`, `tests/fake_mt5_bridge.py`,
`tests/test_candle_momentum.py`, `tests/test_candle_momentum_e2e.py`, `tests/test_candlemomentum_js.py`, `tests/test_browser_candle_momentum.py`.

## Defects found and fixed while building (caught by the tests, not by inspection)

* A tuple-`|`-set error in the executor-refusal path (broke Risk Engine rejection handling).
* A missing instrument spec still published a "fresh" decision.
* An absurd pip size produced a **negative trigger price** and traded-state; non-positive trigger / target / stop prices are now refused.
* A stop-then-start race left a slot with **no live worker** (reproduced 20 of 20 on the old logic; fixed with a per-worker stop event and a regression test).
* The panel stayed visible for other strategies (a CSS `display` rule overrode `hidden`).
* "Ticks per second" counted polls; it now counts genuine price updates.

## Test results (exact; every figure below was produced by running it)

Baseline, **untouched** project, per-module: 60 non-browser modules, 1,404 tests, **all pass**. Browser: `test_browser_phase7a` already fails 2 tests on the untouched project
(`test_preset_changes_chart_immediately_and_persists_across_reload`, `test_white_and_black_candles_are_kept_and_warned_about_never_replaced`); unrelated to this work.

Modified project, final run:

| Group | Result |
|---|---|
| Non-browser: 63 modules, 1,497 tests (incl. 102 new in `test_candle_momentum` 86, `_e2e` 15, `_js` 1) | all pass. The first full run had 2 failures caused by my UI change (a CSS-comment text scan and the pinned script list in `test_web`); fixed, `test_chart_settings` 60 OK and `test_web` 15 OK re-run. |
| Node checks (`tests/*_test.js`, 11 files incl. the new one) | all pass |
| Browser, one module at a time: clocks 10, commandcentre 30, mtf 3, **candle_momentum 7**, phase7a1 15, phase7b_liquidity 11, phase7b_pending 11, phase7b_quotes 13, strategylab 18, trading_controls 7, task2 16, phase7c_repair 44 | all OK |
| `test_browser_phase7a` (23) | 2 failures: **identical to the untouched baseline** |
| `test_browser_phase7c_fvg` (22) | **Intermittent.** Baseline 3 of 3 full runs OK. Modified: 2 failures in 9 serial full runs (a different test each time, in a loaded period), 6 consecutive OK afterwards; the three failing tests passed 8 of 8 repeats in isolation on both trees. I could not capture a failing run to read, so the cause is **not proven**; please re-run it on your machine. These are chart-geometry tests that do not involve Candle Momentum code. |
| Parallel browser runs (4 at once) | produced timeouts in `test_browser_commandcentre` (2) that disappeared when run alone (30/30). Run browser modules one at a time. |

## Not run / not proven

* Any live or demo MT5 / broker test; Deriv Crash 100 on a real account (the convention is verified only against **simulated** broker data); the EA's `UpdateIntervalMs` behaviour at 250 ms.
* `bash tests/run_tests.sh` as a single orchestrated command (this sandbox kills long background jobs); the same modules were run individually instead.
* Real latency from trigger to order against MT5 (the 0.35 s figure is against the simulated bridge only).
* Freeze levels: TradeMate has never enforced them; still not.

## Launch and use

1. `python app.py` (or `./trademate.sh`), open <http://127.0.0.1:5000>.
2. In the MT5 EA inputs set `UpdateIntervalMs` to about 250 (default 1000) and re-attach it.
3. Command Centre: pick instrument, strategy **Candle Momentum**, timeframe; the live card appears. Set trigger / target / protective stop / lot; Save.
4. Start the Watchdog (it starts the engine). Watch the card. Only when you want automatic trading: set the lot, choose a stop condition and start Autopilot (DEMO account first).
5. If an entry was late or missed: the card's reason line, then `/api/candle-momentum/events?candle_id=...` (see section 9 of the guide).


---

## Archived note: IMPLEMENTATION_REPORT_COMMAND_CENTRE_UPGRADE.md

# Command Centre upgrade: progress report (phase 1 of several)

## Audit findings (from the code, not the docs)
* Everything runs for ONE symbol: `WatchdogManager` (one thread), `StrategySelectionStore` (one strategy), `Autopilot` (one open trade, one lot store lookup).
* `trademate/pips.py` refused indices, crypto and synthetics (no universal pip). Candle Momentum / CHoCH / EMA+ATR already take a `PipDefinition` via `StrategyParams`.
* The broker interface (`SymbolInfo`) already carries digits, point, tick size/value (profit/loss), volume min/max/step, stops/freeze level, currencies.
* The bridge is an MQL5 EA (`mt5/TradeMateBridge.mq5`). The Python adapter has NO margin calculation: no `OrderCalcMargin` / `OrderCheck` command exists. Section 8 (affordability) needs a new EA command plus an EA recompile.
* Candle Momentum evaluates polled candles, not a tick stream, and has no state machine (WAITING_FOR_NEW_CANDLE ... MISSED_ENTRY). Defaults are 10/10/2/1 pips, not the 5/10 example.
* Baseline: 1402 non-browser tests pass.

## Done in this phase
* `trademate/instruments.py`: central instrument spec + conversion engine (asset class, strategy unit, points, ticks, price<->unit, tick-snapped TP/SL that are never closer than asked, money per unit/lot from broker tick values, volume validation that never rounds, VERIFIED/UNVERIFIED with explicit reasons, staleness, the confirmed Crash 100 convention validated against the broker's contract and NOT applied to other symbols).
* `strategies/service.py` now gets every strategy's pip from that engine (so indices, crypto, synthetics work) and attaches `instrument_spec` to each result.
* `tests/test_instruments.py`: 25 tests across all asset classes. Existing suite: 1402 + new, all pass after the change.

## Not done yet (in order of dependency)
1. Multi-instrument config store (max 5, backend-enforced) with stable ids; per-config Watchdog workers; global/individual start-stop.
2. Route Risk Engine, pipeline and Autopilot money/target maths through `InstrumentSpec` (today they still use their own paths).
3. EA command for `OrderCalcMargin` / `OrderCheck`, adapter method, affordability service and statuses.
4. Candle Momentum tick-driven state machine; CHoCH 100-unit target via the engine.
5. Autopilot multi-position + per-instrument lots + max positions.
6. Command Centre UI redesign.
7. Section 12 of the brief was cut off mid-sentence ("MT5 disconnection and reconnection hand..."); I need the rest.
Nothing here was tested against a real MT5 account.


---

## Archived note: IMPLEMENTATION_REPORT_EMA_ATR.md

# Implementation report: EMA 20/50 + ATR strategy

Scope: one strategy added. Candle Momentum, CHoCH Trading, Trend Continuation, Autopilot, Watchdog, Risk Engine, pipeline, MT5 bridge and the Command Centre layout are byte-for-byte unchanged.

## Files
New: `trademate/strategies/ema_atr.py`, `tests/test_ema_atr.py`, `docs/EMA_ATR_STRATEGY.md`, this report.
Minimal edits: `strategies/registry.py` (registration, appended last), `strategies/base.py` (six additive `StrategyParams` fields), `strategies/service.py` (passes the settings to this strategy only),
`config.py` + `config/settings.toml` (five `[strategy]` keys, validated), `static/js/commandcentre.js` (one explanatory note line), README / TESTING / docs addenda.
Existing tests touched (expected values only, nothing removed or loosened): three catalogue assertions in `tests/test_strategies.py` and the dropdown list in `tests/test_browser_commandcentre.py`, because a fourth strategy now exists.

## Verified (exact results)
* `tests.test_ema_atr`: 39 tests OK. Mutation check: deliberately breaking the strategy (signal on every candle, 3x ATR target, reading the forming candle, ignoring zero ATR) made the tests fail each time; the file was restored.
* Full non-browser suite: 1379 tests OK (1340 existing + 39 new).
* Browser suites `commandcentre` (30), `strategylab` (18), `trading_controls` (7): OK, run serially against the fake bridge.
* `python3 -m compileall app.py trademate tests`: OK.

## Not verified
* No live MT5 / real broker, real fills or real order placement (tests use the in-memory fake bridge; no real trade can occur).
* Other browser suites (charts, clocks, liquidity, FVG, ...) were not re-run for this change; they do not touch the strategy catalogue. Two pre-existing failures in `test_browser_phase7a` (colour-picker hex casing, a preset check) also fail on the original project.
* The strategy has not been back-tested; its profitability is unknown.

## Unrelated observations (recorded, not changed)
* `test_browser_phase7a` has the two pre-existing failures above.
* A signal is only valid for the candle after the crossover; with the existing pipeline `min_rr` of 1.5, a sharp adverse move after the signal turns it down (intended by that existing gate, but worth knowing).


---

## Archived note: IMPLEMENTATION_REPORT_PIPELINE.md

# Implementation report: Strategy -> Risk -> Execution pipeline + project reconciliation

## Two project copies existed; this is the reconciled one
* **A** = `TradeMate-watchdog-choch-alerts-1.zip` (original base).
* **B** = `TradeMate-setup-discovery-watchdog.zip` (later: Watchdog/setup-discovery fixes, the `>` zone-then-break fix in `strategy/mtf.py`, an order-block prefix-consistency fix, `trademate/trade_pipeline.py` with the read-only `GET /api/strategy/mtf/trade-review`). B's own reports say Flask was missing there, so its endpoint and the full suite were never run.
* **This checkpoint = B + my pipeline work**, combined with a 3-way merge against A (the common base): `git merge-file` on the 7 files both changed (`app.py`, `trademate/config.py`, `trademate/testrunner.py`, `config/settings.toml`, `TESTING.md`, `docs/MULTI_TIMEFRAME_STRATEGY.md`, `README.md`); no conflicts except two README headings (both kept). Files only one side touched were taken as they were. B's `trade_pipeline.py` (review only) is kept; my `pipeline.py` (propose + execute) is separate and does not replace it. They overlap in purpose and should be consolidated later.

## What changed (my part)
* `trademate/pipeline.py`: strategy contract check (`qualify`), live-price sizing through the unchanged Risk Engine, volume cap handling, execution eligibility, single-use bound approvals, `prepare_execution` revalidation, stage states, placement check. It contains no order call (AST test).
* `app.py`: `POST /api/pipeline/propose`, `POST /api/pipeline/execute`, `GET /api/pipeline/approvals/<id>`; orders still go only through `adapter.place_order` via the existing `_execute`.
* `[pipeline]` settings (validated), Full Test registration, `tests/test_pipeline.py` (76 tests), `docs/STRATEGY_TO_EXECUTION_PIPELINE.md` (audit, defects D1-D4, contract, failure handling, Autopilot gap list, what is unverified).
* `tests/test_web.py`: the exact allow-list of routes was extended by the 4 new, reviewed routes (3 mine, 1 from B). That is the only existing test edited.

## Defects found
D1 no path from a qualified strategy setup to the Risk Engine/execution; D2 `to_trade_proposal()` has no risk percentage, so the Risk Engine would reject every strategy proposal ("Strategy risk percentage is missing"); D3 strategy entry is a reference close, not the fill price; D4 an oversized risk-based position is rejected rather than reduced (handled in the pipeline only; manual sizing unchanged). Details and code locations: the docs file.

## Tests (actual results)
* Original A, before any change: 1230 run, 13 failures, 3 errors (16), 0 skipped (browser modules `test_browser_phase7a`/`clocks` excluded by the runner's `--no-browser`).
* Merged tree, one full run (`bash tests/run_tests.sh --no-browser`): **1315 run, 14 failures, 2 errors**; Node: 5 checks passed, `labgreeting_test.js` failed.
  * 1 failure was caused by my new routes (`test_web` route allow-list). Fixed as above; `tests.test_web` re-run: 15 passed.
  * 1 failure (`test_browser_phase7c_fvg`, FVG browser test) passed when re-run alone (22 passed): intermittent under load, not explained.
  * **14 remain, all pre-existing in A's baseline and unrelated to trading**: 13 in `test_browser_strategylab` (Command Centre page, e.g. broker state shown as "Unavailable" not "Disconnected", greeting text) and `test_labgreeting_js` (greeting returns "Good morning " instead of "Good morning. Welcome to Command Centre."). Not fixed here; they belong to the Command Centre redesign.
  * The merged tree was NOT re-run in full after the allow-list edit; only `tests.test_web` and the FVG module were re-run. "Not run": nothing else was skipped by the runner.
  * `tests.test_pipeline` alone: 76 passed. `test_pipeline` + `test_strategy_trade_pipeline` + `test_testrunner`: 113 passed.
* Fixed by B (observed gone in the merged run): `test_prefix_consistency_holds_on_random_series`.

## Not verified
No MT5 (no Wine/snapshot folder here; `python -m trademate.broker` = DISCONNECTED). No DEMO order was placed. Simulated bridge results do not prove Headway behaviour. Autopilot is not built. No overall-drawdown limit exists. Real next check: `docs/STRATEGY_TO_EXECUTION_PIPELINE.md` section 9.

## Known limitations
Approvals are in memory (lost on restart, by design). Fixed volume can fail the re-check if price moves against it inside the drift limit (a new proposal re-sizes). Drift/spread limits (25% of stop distance) are untuned defaults.


---

## Archived note: IMPLEMENTATION_REPORT_STRATEGIES_COMMAND_CENTRE.md

# Implementation report: two strategies (H1 + M5), strategy selection, Command Centre redesign

## Where this started (recovery)
Authoritative base = the reconciled checkpoint from the previous milestone (`TradeMate-pipeline-merged-checkpoint.zip`): your later upload (`TradeMate-setup-discovery-watchdog.zip`)
plus my Strategy -> Risk -> Execution pipeline, merged 3-way against the common ancestor. I inspected the real files, ran the full suite before changing anything
(1315 tests: 14 failures, 2 errors; of those 1 was caused by my own routes and was fixed, the rest were pre-existing Command Centre UI/test problems) and kept that ZIP as the recovery checkpoint.
I cannot know what the usage-limit interruption lost beyond what the files and reports show; nothing was assumed.

## Audit findings (before changing strategy code)
* Hidden H4/M15 gates in the old engine (`trademate/strategy/mtf.py`): context resolution used all four timeframes, the "not ok" gate needed M15, H4, H1 and M5 data, a mandatory M15 zone gate,
  and targets came from M15/H1/H4 levels. The old engine is **kept** (its Strategy page and tests are unrelated to this task) but is no longer selectable or used by the Command Centre, Watchdog, trade review or pipeline.
* Pre-existing Command Centre defects: (1) `render()` wrote to a missing `lab-build` element and threw, so every capability showed "Unavailable"; (2) `labgreeting.js` had lost the "Welcome to Command Centre." sentence
  its 76 JS checks and 8 browser tests specify; (3) a browser test disconnected the shared stub broker and never restored it, poisoning later tests.

## What was built
* `trademate/strategies/`: `registry.py` (exactly two strategies), `choch.py`, `trend.py`, `base.py` (shared plumbing), `selection.py` (validated, atomic, persisted, epoch + change listeners), `service.py` (fetches ONLY H1 and M5).
  Rules are in `docs/STRATEGIES.md`. The strategies are independent (an AST test forbids cross-imports) and cannot size or send (AST test forbids risk/execution imports and order calls).
* Routes: `GET /api/strategy/catalogue`, `POST /api/strategy/select`, `GET /api/strategy/analyse`, `GET /api/autopilot/status` (read-only; there is no endpoint that arms Autopilot).
* Pipeline made strategy-aware: `qualify` checks the result came from the selected strategy and only requires that strategy's own timeframes; approvals record strategy id and version; changing strategy cancels all
  pending approvals and a stale approval is refused at execution (`strategy_changed`). Watchdog is bound to its strategy, resets on a switch, and tags events; CHoCH alerts only for the CHoCH strategy (M5 only).
* Command Centre redesigned (template, CSS, JS): header with real account/connection chips, market picker, strategy dropdown with description and H1/M5 chips, ANALYSE MARKET (busy state, duplicate-click guard),
  a large decision card (backend reasons, four stage indicators, expandable evidence), H1/M5 context cards, trade review with real levels, REVIEW TRADE (Risk Engine, sends nothing) then a deliberate two-click
  CONFIRM & EXECUTE with an approval countdown, server-side Watchdog toggle with honest states, Autopilot panel (disabled, lists unmet preconditions), recent activity from real records. Dark and light themes via existing tokens.
* Settings `[strategy]`: `default_strategy`, `counter_min_rr`, `signal_max_age`, `pullback_min_retrace` (validated).

## Existing tests that I edited (and why; none deleted, none weakened to hide a failure)
* `tests/test_web.py`: the exact route allow-list and exact script-URL list gained the new routes / cache-bust version.
* `tests/test_strategylab.py`: the old page asserted "WAITING FOR SETUP"/"does not send orders yet" and that execution is Not Yet Implemented. The new page truthfully has REVIEW TRADE and CONFIRM & EXECUTE (hidden/disabled until a review is approved); the assertions now check that, keep Autopilot "Not Yet Implemented", and keep "nothing ships enabled".
  The Watchdog start assertion now expects the selected strategy id.
* `tests/test_browser_strategylab.py`: analysis route is now `/api/strategy/analyse`; two context cards (H1, M5) instead of four; execution is Available while Autopilot stays Not Yet Implemented; the Watchdog label asserts the real (honest) state instead of a fixed message;
  and a missing `addCleanup` after a test that disconnects the shared stub broker (isolation bug).
* `tests/browser_support.py`: each browser test now stops the process-wide Watchdog and uses a temporary strategy-selection file.
* Code fixes that make pre-existing tests pass: `labgreeting.js` restored to its specified wording; `lab-build` null error removed.

## Test results (actual)
* Original upload, before any change: 1230 run, 13 failed, 3 errors, 0 skipped (`--no-browser`).
* Start of this task (merged checkpoint): 1315 run, 14 failed, 2 errors.
* **Final full run** (`bash tests/run_tests.sh --no-browser`): **1432 run, 2 failed, 0 errors, 0 skipped.** Both failures were the exact route/script lists in `test_web` (caused by my intentional additions); fixed, `tests.test_web` re-run: 15 passed.
  Node checks: all pass (clocks 166, labgreeting 76, plus the other suites).
* After the final edits I re-ran the affected suites: `test_pipeline`, `test_strategies`, `test_testrunner`, `test_project_integrity`, `test_strategylab`, `test_web`, `test_browser_commandcentre`: **262 passed, 0 failed**.
* New this task: `tests/test_strategies.py` 87 tests, `tests/test_browser_commandcentre.py` 30 browser tests (real headless Chromium, fake MT5 bridge). Earlier: `tests/test_pipeline.py` 76.
* Not in the `--no-browser` run: `test_browser_phase7a` + `test_browser_clocks` were run separately: **33 run, 2 failed** (chart colour preset tests: `'#c2a5ff' != '#C2A5FF'`). **They fail identically on the untouched uploaded project**, are about chart presets, and were not touched or fixed.
* Python compile (`compileall`): OK. `node --check` on every JS file: OK.
* Blocked / not run: nothing was skipped by the runner. Blocked by the environment: anything needing real MT5.

## What was and was not verified
* **Verified**: both strategies on deterministic candles (BUY, SELL, WATCH, WAIT, stale/missing data, expiry, failure, counter-H1 rule, H1/M5-only, independence); selection validation, persistence and restart recovery; strategy switch cancelling approvals and the Watchdog;
  the page in headless Chromium (selection, analysis states, review, two-click execution against the fake bridge, rejection, stale approval, Watchdog, Autopilot disabled, 390 px and 320 px layouts, light-theme contrast >= 4.5, keyboard order); I also looked at screenshots of the desktop (dark and light) and phone layouts.
* **Not verified**: real MT5 / Headway behaviour; any DEMO or LIVE trade (none was placed); profitability or edge of either strategy (no back-test or forward test); other browsers than Chromium; real devices; screen readers; behaviour on a real market feed (the strategies were run on synthetic candles).
* Autopilot is **not built**. Unattended trading is not possible. There is still no overall-drawdown limit.

## Known limitations
* Strategy thresholds are untuned starting values. Swing confirmation lags by `swing_right` candles. The Trend strategy has a short window after the resumption break. The measured-move target is an unproven assumption (labelled).
* The legacy four-timeframe engine and its Strategy page remain (documented, not selectable).
* The stub data in the older browser tests is misaligned and has no M5, so the Watchdog correctly reports "Temporarily unavailable" there.
* B's `trade_pipeline.py` (review only) and my `pipeline.py` (propose + execute) overlap and should be consolidated.


---

## Archived note: IMPLEMENTATION_REPORT_TASK2.md

# TradeMate Task 2: implementation report

## 1. What changed
* **Analysis page**: five plain-language sections (Market direction, Liquidity, Fair value gaps, Order blocks, Overall) built by the new
  `trademate/analysis_summary.py`; the previous detail is kept, collapsed, under "Technical details". A CHoCH is never presented as a confirmed
  reversal; sweeps count as evidence only through the structure break that followed; unavailable engines show "Unavailable"; "not enough evidence" is a valid result.
* **Candle colours**: default bullish RED `#E53935`, bearish BLACK `#000000` (preset "Red and Black"); light halo on black wicks/bodies on dark backgrounds. Existing preference system kept.
* **Buy/Sell**: the two oversized chart-bottom buttons removed. Lot size, +SL/+TP and the Trade Panel BUY/SELL (Risk Engine + confirmation) remain.
* **Chart theme**: audited (already chart-only); made persistence robust (an unsaved choice is re-sent on the next load).
* **FVG / Order Block integration**: audited; stale and unavailable states added to the chart; FVG fill status derived on the Analysis page.
* **Pending orders**: audited (already drawn from real broker orders); freshness guard added.

## 2. Files modified
New: `trademate/analysis_summary.py`, `tests/test_task2_analysis_and_defaults.py`, `tests/test_browser_task2.py`, `HANDOVER_TASK2_CHART_CLEANUP.md`, this report.
Modified: `trademate/analysis.py`, `trademate/chart_settings.py`, `static/js/app.js`, `static/js/analysis.js`, `static/css/style.css`, `templates/index.html`, `README.md`, `TESTING.md`, `docs/ANALYSIS.md`, `docs/FAIR_VALUE_GAPS.md`,
and tests updated to the new behaviour: `tests/test_chart_settings.py`, `tests/test_web.py`, `tests/test_browser_phase7a.py`, `tests/test_browser_phase7a1.py`, `tests/test_browser_phase7b_liquidity.py`, `tests/test_browser_phase7c_repair.py`.
The FVG, Order Block, Liquidity and Market Structure engines were NOT modified.

## 3. Already working before this task
Order Block detection; FVG/OB server-to-chart data flow and symbol/timeframe keying; chart-only theme toggle; pending-order overlay at real prices with distinct styling; order entry via the Trade Panel.

## 4. Defects found and fixed
1. Chart kept drawing old FVG/OB zones after candle refreshes stopped. 2. An unavailable FVG/OB result drew nothing and said nothing. 3. Pending orders held in the browser kept drawing as current when broker updates stopped. 4. A chart-theme change made just before a reload could be lost (delayed save). 5. FVG "status" had no fill information (engine does not track fills): derived on the Analysis page, not in the engine.

## 5. Test results (edited tree; Python 3.12, headless Chromium via Playwright; stub/fake broker only)
Baseline before changes: 50 modules, 1033 tests, 1033 passed, 0 failed, 0 skipped.
After: 52 modules, **1086 tests: 1086 passed, 0 failed, 0 skipped, 0 blocked, 0 not run** (921 non-browser + 165 browser). 53 added tests: 36 unit, 16 browser, 1 in test_web.
Existing tests were changed only where they pinned the old default colours, the removed buttons or the old Analysis layout (list in section 2); no assertion was loosened to pass.
During development one `test_browser_phase7c_fvg` pixel test failed once and passed on every other run (3 later full runs); cause not established, probably a timing flake.
Warnings: the only warnings in the logs are application log lines from stub brokers (no MT5 server clock); no Python warnings were raised.
NOT performed: anything requiring MT5 or a live broker (real candles, real pending orders, real order placement). No test submits or modifies a real order.

## 6. Remaining issues / limitations
* Not verified against a real MT5 / broker; the pending-order and zone overlays were tested with the fake bridge and stubs.
* Not visually reviewed by a human: black-on-dark candle readability was checked by pixel tests and design, not by eye.
* Colour migration: an old settings file still holding the previous default colours is moved to red/black; someone who deliberately chose "Classic" earlier is switched too (re-pick it in Settings).
* Price location in zones uses the newest candle's close, not a live quote. One timeframe only. No setup engine. Phase 7C should not be called fully accepted until you have checked it on your real MT5 setup.

## 7. Where to find each feature
Analysis: sidebar "Analysis". Colours: Settings > chart colours ("Red and Black"). Trade buttons: Chart page, Trade Panel. Chart theme: chart moon button / Settings > Chart theme. FVG / OB: chart overlay buttons. Pending orders: magenta dash-dot lines on the chart. Run: `python3 app.py`; tests: `bash tests/run_tests.sh` or see TESTING.md.


---

## Archived note: SETUP_DISCOVERY_WATCHDOG_REPORT.md

# Setup Discovery & Watchdog Alerts — Implementation Report

## Scope and verification boundary

This checkpoint improves the existing strategy/watchdog path. It is analysis-only: the Watchdog does not place orders. No live broker or real-market setup was evaluated during this work. Python/Node automated tests are not a substitute for an interactive browser test of OS desktop notifications or audio playback.

## Existing component flow (verified in source)

1. **Market data** enters the MTF evaluator and is represented per timeframe in `StrategyResult.data`; each timeframe's status is checked before an executable proposal is allowed.
2. **Market structure** is computed per timeframe by `trademate/strategy/mtf.py` through the existing structure engine. Its timeframe analysis exposes bias, swings, confirmed events and reversal stage. Provisional events remain distinct from confirmed closed-candle events.
3. **Liquidity** is computed from the same candle rows and structure result via `analyze_liquidity` in `trademate/strategy/mtf.py` (`_analyze_timeframe`, around the liquidity call). Its BSL/SSL pools and sweep evidence are exposed in timeframe analysis and can contribute to structural target selection/context. It is not a universal hard entry gate for the current MTF pathway.
4. **Order blocks and FVGs** are consumed by `_zones` / `_zone_candidates` in `trademate/strategy/mtf.py`. Only confirmed OB zones are considered; FVGs are filtered by `fvg_fill_status` to `untouched` or `partial` status. These are alternatives for the current M15 zone gate, not a requirement that both exist.
5. **Current MTF qualification** is implemented in `trademate/strategy/mtf.py`: reliable M5/M15/H1/H4 data; permitted directional context (with stricter rules for counter-H4 ideas); a live directional M15 confirmed OB or eligible FVG; a closed M5 candle reaching the zone within `trigger_lookback`; a later confirmed M5 directional BOS/CHoCH while M5 bias agrees; a valid structural stop; a structural target; and the configured minimum reward-to-risk. WATCH/WAIT/inconclusive results are not executable setups. Liquidity is analyzed and may inform target/context evidence, but a sweep is not mandatory for every current family.
6. **Watchdog** is `WatchdogManager` in `trademate/strategy/watchdog.py`. It runs the evaluator in a daemon thread at a configured interval, tracks candidate transitions, suppresses repeated setup signatures, and emits confirmed CHoCH events only when the timeframe and overall pass are reliable.
7. **Delivery** is exposed through `/api/strategylab/status` in `app.py`; `static/js/strategylab.js` polls status, renders monitoring state, and routes unseen event keys to in-page messages, opted-in desktop notifications, and enabled sound.

## Changes in this checkpoint

- The Watchdog now refuses to create a qualified setup alert from a pass already classified as stale or incomplete. Previously the setup event path could run even when the same pass had been marked unreliable.
- Setup alerts now require directionally valid trade geometry: BUY requires `SL < entry < target`; SELL requires `target < entry < SL`. Invalid levels fail closed.
- Qualified setup event payloads now include setup family, reward-to-risk when available (otherwise derived from valid levels), supporting evidence, and candidate invalidation conditions, in addition to symbol/direction/levels and a stable event key.
- The MTF trigger now requires the confirmed directional structure break to occur **after** the zone-touch candle (`>` timestamp), not on the same candle (`>=`). This enforces the documented “reaction, then subsequent break” sequence.
- Fixed browser first-poll handling: a newly created event is no longer silently discarded merely because persisted `lastEventId` was initially null. Event keys remain the deduplication identity; stored event keys prevent repeat alerts across reloads when storage is available. Event-ID rollback is handled when the server process restarts.
- Added Watchdog regressions for stale-data setup suppression, invalid BUY/SELL geometry, and richer setup-event payloads.

## Setup-family/component mapping

The current executable MTF pathway chooses `pullback_continuation`, `trend_continuation`, or `countertrend_opportunity` for its qualified result based on the current context/candidate selection. The common qualification pathway is zone reaction plus later M5 structure break and valid levels. The current implementation does **not** independently qualify every family in the requested aspirational list (such as a dedicated range-bound or breakout/retest family); those should not be advertised as independently implemented just because relevant indicators exist.

| Component | Current consumer | Qualification role |
|---|---|---|
| Confirmed structure / swings | MTF context, M5 trigger, structural stop/target | Directional permission, later closed-candle BOS/CHoCH, structural levels |
| Liquidity pools (BSL/SSL) | MTF timeframe evidence and target selection | Context/target evidence; not mandatory for all current setups |
| Liquidity sweep | Liquidity analysis output / contextual evidence | Not a universal hard gate in the current MTF entry path |
| Confirmed OB | M15 zone candidate | Eligible zone alternative, subject to live status and reaction |
| Untouched/partial FVG | M15 zone candidate | Eligible zone alternative, subject to fill status and reaction |
| Closed M5 reaction candle | MTF zone gate | Recent price intersection with selected M15 zone |
| Later confirmed M5 BOS/CHoCH | MTF trigger gate | Mandatory directional follow-through after zone touch |
| Structural SL/target + RR | MTF gates and StrategyResult | Mandatory valid trade geometry and configured RR threshold |

## Notification behaviour and limitations

- In-page CHoCH alert: `alertMessage` is called for a confirmed CHoCH.
- Desktop notification: only when the browser supports `Notification`, permission is `granted`, and the persisted notification toggle is enabled.
- Sound: only when sound is enabled and the browser's Web Audio API permits playback. Sound uses an oscillator and attempts to resume a suspended AudioContext; actual sound is not certified by a Node syntax check.
- Event history is capped at 100 events. The server's event IDs are process-local; event keys are the stable identity within the current event payload. Browser storage may be unavailable or cleared, in which case cross-reload deduplication is limited.
- The Watchdog remains single-symbol and read-only. It needs the server process, MT5 bridge, broker connection and fresh data to continue checking; it cannot monitor while the host machine/server is stopped or asleep.

## Tests and known issue

Targeted Watchdog, strategy pipeline, MTF strategy, strategy intelligence, liquidity, FVG, order-block and structure tests were run. The first broad run exposed a real prefix-consistency defect in `tests.test_orderblock_engine.IndependentInvariants.test_prefix_consistency_holds_on_random_series`: a later break event could resurrect an order-block candidate after a newer reference swing had already replaced its reference. The engine was corrected to reject a break confirmed after that replacement. The formerly failing randomized invariant test then passed, and the final combined targeted run passed **332 tests**. `node --check static/js/strategylab.js` and `python -m compileall -q trademate tests` also passed.

Two API test modules (`tests.test_liquidity_api` and `tests.test_fvg_api`) could not import because Flask is not installed in this execution environment; they are **blocked**, not passed. The project master test runner could not run for the same reason. Playwright is importable, but a browser-backed run was not completed in this checkpoint; desktop notifications and actual audio remain unverified. No live broker tests were run, and no orders were sent.


---

## Archived note: STRATEGY_TO_RISK_HANDOFF_MILESTONE.md

# TradeMate — Strategy-to-Risk Handoff (Milestone 1)

## What changed

- Added `trademate/trade_pipeline.py`, an orchestration boundary outside the pure strategy package. This preserves the architecture rule that strategy analysis does not import risk sizing or execution code.
- Added `GET /api/strategy/mtf/trade-review?symbol=SYMBOL` in `app.py`. It runs a fresh MTF evaluation and passes only BUY/SELL decisions to the existing Risk Engine.
- The review uses the current MT5 ask for BUY or bid for SELL as the executable entry reference, preserves the strategy's structural stop and first target, and asks the Risk Engine to size using the configured preferred risk percentage.
- WATCH, WAIT, malformed proposals, unavailable quotes, unknown account/day-loss information, and Risk Engine rejection all fail closed.
- The response explicitly says no order was sent and that any later execution must perform a fresh review. This endpoint does not persist approvals or place orders.
- Added six tests for BUY/SELL hand-off, WATCH/WAIT exclusion, invalid stops, unavailable quotes, and missing daily-risk history.

## Rejection-path finding

The project already has a guarded manual execution path:

`POST /api/order` → `gate_order()` → `safe_review()` → `review_order()` → `evaluate_trade()` → existing MT5 adapter/journal → broker reconciliation.

The strategy path was separate:

`GET /api/strategy/mtf` → `evaluate_strategy()` → `evaluate_symbol()` → pure MTF evaluation.

`StrategyResult.to_trade_proposal()` existed as a helper but was not connected to a web risk-review flow. Thus, a qualified strategy result did not itself enter the Risk Engine; this is an integration gap, not evidence that the Risk Engine rejected every strategy signal. The new endpoint bridges that gap for risk review only.

Existing Risk Engine protections are intentionally retained: unknown daily-loss data is a rejection, the market order is reviewed against a fresh quote, and a resize is not silently applied to a user-submitted order.

## Verification

- `python -m compileall -q app.py trademate tests`: passed.
- `python -m unittest tests.test_strategy_trade_pipeline tests.test_mtf_timeframes tests.test_mtf_strategy tests.test_strategy_intelligence -q`: **90 passed**.
- The first broader test attempt exposed an architectural boundary test: the coordinator had initially been placed under `trademate/strategy`. It was moved to `trademate/trade_pipeline.py`, and the boundary test then passed in the final 90-test run.
- Flask is not installed in the available runtime and network access is unavailable, so app-level endpoint tests and the full test suite could not be run here. The route was syntax-compiled but not exercised through Flask.
- No MT5 terminal/broker was connected. No live or demo order was sent. Browser, API integration, broker acceptance, and real position reconciliation remain unverified.

## Next required milestone

Build the controlled confirmation-to-execution flow around this review while reusing the existing `/api/order` risk gate and execution journal. It must re-evaluate strategy and risk immediately before submission, never trust a stored preview as authorization, verify DEMO identity, and reconcile uncertain outcomes before any retry. Add Flask endpoint tests and run the complete suite in an environment with the declared dependencies before calling this end-to-end trading-ready.


---

## Archived note: Update1ByCoPilot.md

# TradeMate Update 1 — Copilot

## Summary

This update continues work in the existing TradeMate Flask application and its project directory. It adds an Account Centre that reports account details from the connected MT5 bridge, while retaining the existing read-only Command Centre analysis and monitoring workflow.

## Changes made

### Account Centre

- Added an **Account Centre** page and sidebar navigation entry.
- Displays broker, server, environment, verified account login, currency, leverage, balance, equity, free margin, open profit/loss, and broker snapshot age.
- Reuses the existing `/api/broker` poll; it does not create a second account connection or store account credentials.
- Shows account figures only while the bridge reports a connected account. Clears the fields when the connection is unavailable.
- Explains the supported account-management and risk limitations in the page.

### Command Centre and existing workflow

The existing Command Centre work remains integrated:

- Broker-backed instrument search and read-only strategy analysis.
- A **Saved in MT5** dropdown alongside the market search field, populated from the connected terminal's Market Watch symbols.
- The dropdown is layered above nearby analysis panels and uses an opaque surface so its options remain legible.
- Native M5, M15, H1, and H4 analysis.
- Personalized greeting and browser-local preferences.
- A single-symbol, read-only Watchdog with start/stop controls and deduplicated setup events.
- Opt-in browser notifications and sound controls.

### Documentation and tests

- Updated the README, `TESTING.md`, and `docs/STRATEGY_LAB.md`.
- Added Account Centre coverage to the relevant UI tests and updated the application route inventory.
- Updated capability descriptions for the current Watchdog, notifications, sound, and execution boundaries.

## Account and trading limitations

- The MT5 bridge reads the account already logged in to the terminal. It does not provide in-app sign-in, saved account profiles, or account switching.
- Credentials are not requested or stored by the Account Centre.
- The adapter rejects non-DEMO accounts. The Account Centre is informational; it does not change the active MT5 account.
- Risk rules currently use one local configuration, not separate per-account profiles.
- Command Centre analysis does not place orders. Manual execution remains on the existing Trade page and is subject to the existing Risk and Execution Engines.
- Autopilot is not implemented.
- The Watchdog runs in the TradeMate process and does not survive a server restart. Browser alerts require the Command Centre page to be open.

## Verification

Focused tests:

```text
Ran 32 tests
30 passed
2 skipped (Playwright browser test classes unavailable)
```

Full unittest suite:

```text
Ran 1,069 tests
1,000 passed
2 failed
67 skipped
```

The two full-suite failures are in test-runner cases that require Node.js. Node.js was not available in the environment, and system installation was blocked because administrator access was unavailable. Browser tests were skipped because Playwright/Chromium was unavailable.

The running Flask page and JavaScript were checked over HTTP and included the Account Centre and updated cache-busted assets. The ZIP archive was checked for integrity and excluded the virtual environment, local data/logs, environment files, databases, bytecode, and caches. Live broker switching and live trading were not verified; no real trades were placed.

## Files to know

- `templates/index.html` — Account Centre markup and navigation.
- `static/js/app.js` — displays current broker identity and clears it when disconnected.
- `trademate/strategylab.py` — Command Centre capability states.
- `tests/test_strategylab.py`, `tests/test_web.py`, and `tests/test_browser_strategylab.py` — related automated coverage.
- `README.md`, `TESTING.md`, and `docs/STRATEGY_LAB.md` — usage, verification, and limitations.

## Run TradeMate

From the project directory:

```bash
./venv/bin/python app.py
```

Open the TradeMate page, then select **Account Centre** or **Command Centre** from the sidebar.

## Project archive

The final project archive created for this update is `../TradeMate-Command-Centre-Final.zip`, alongside the project folder.
