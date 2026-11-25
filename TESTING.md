# TradeMate testing: CompleteNov12 results

Run everything with `bash tests/run_tests.sh` (each test module in its own process; about 15 minutes with the browser tests; `--no-browser` skips them). All tests use a **simulated** MT5 bridge (`tests/fake_bridge.py`) and clearly labelled FIXTURES, and every test process uses a throwaway data folder (`tests/__init__.py`), never your real `data/`.

**Result of the final run: 85 test modules, 1,913 tests: 1,907 passed, 6 failed, 0 skipped.** The Node.js checks (11 files) all pass.

**The 6 failures also fail on the untouched `TradeMate-CandleMomentum-M1-Update.zip` and were not caused by this version** (each was re-run on that original and fails identically). They are stale expectations or environment-sensitive browser checks, left as they are on purpose:
* `test_candle_momentum_e2e`: 3 tests (`test_26_successful_simulated_execution_end_to_end`, `test_explicit_slippage_tolerance_replaces_the_generic_guard`, `test_price_runs_away_between_signal_and_order_is_missed_not_chased`). They still assume the old 10-pip target and 1.0999 stop; the M1 update changed Candle Momentum's defaults (target 20, stop 50). I did not edit them, because changing their numbers would change what they prove about Candle Momentum.
* `test_fixed_target_execution`: `test_pip_size_can_be_set_in_the_browser_validated_and_cleared` (same old 10-pip expectation).
* `test_browser_phase7a`: `test_preset_changes_chart_immediately_and_persists_across_reload` and `test_white_and_black_candles_are_kept_and_warned_about_never_replaced`.

**Tests that I changed, and why (none was deleted or loosened):** the catalogue/selector tests now expect the five strategies; tests that used the retired Trend Continuation / EMA 20/50 + ATR as "the other strategy" use Market Structure instead (EMA/ATR's pipeline tests keep running by declaring it "selected" directly); the five broker-midnight daily-loss tests were rewritten to local-midnight with stricter cases (the brief requires local midnight); the web-shell inventory tests list the new routes and scripts; the check-list tests expect the two central protection checks; a CHoCH-description assertion now looks at the strategy's own text because the timeframe selector legitimately lists every timeframe.

**New tests (about 280):** `test_candle_momentum_timeframes` (M1/M2/M5 signal-to-order, switching, per-timeframe duplicate protection, no silent fallback, the premature-reversal characterisation), `test_five_strategies`, `test_new_strategies_execution`, `test_risk_and_execution_features` (local midnight and DST, consecutive losses, lock, lot adjustment maths, calculator, settings scopes, entry limits, same-candle twice, Pause/Stop/Emergency), `test_more_backend_features` (insufficient-funds end to end, notifications, Hard Reset, sessions, chart stores, Vol_20 / Crash 100 fixtures), `test_browser_ui_features` (20 real-browser tests: maximise/restore/responsive, adaptive toolbar, moving average, manual lines, live Trade History, notification de-duplication and chime, settings persistence, Hard Reset, calculator, session panel) and `test_browser_candle_momentum_timeframe_choice`.

**Not verified:** nothing was run against a live MT5 account or a real broker. The `M2` line added to `mt5/TradeMateBridge.mq5` was **not compiled** (no MT5 here). The margin required per lot is not available from the bridge, so lot reduction uses a bounded reduction until an adapter supplies it. The AI layer is not enabled (`docs/AI_ADVISORY.md`).

# TradeMate CompleteNov12: test results and how to run them

**Run everything:** `bash tests/run_tests.sh` (about 15 minutes with the real-browser tests; `--no-browser` skips them, about 4 minutes). It runs every test module in its **own process**, because the application keeps persistent safety state (Emergency Stop lock, consecutive-loss record, notifications), and prints **passed, failed and skipped separately**. Every test process uses a throwaway data folder (`tests/__init__.py`), never your real `data/`.
Single module: `python3 -m unittest tests.test_five_strategies -v`.

## Result of the final run (simulated broker only)

| | Ran | Passed | Failed | Skipped |
|---|---|---|---|---|
| Python tests, non-browser | 1660 | 1656 | 4 | 0 |
| Python tests, real browser (headless Chromium) | 253 | 251 | 2 | 0 |
| **Python total** | **1913** | **1907** | **6** | **0** |
| JavaScript checks (Node, 7 scripts run by the runner) | all | all passed | 0 | 0 |

**The 6 failures all exist, unchanged, on the untouched baseline checkpoint** (each was re-run there and fails the same way); none is caused by this version, and none was hidden or edited away:

* `tests.test_candle_momentum_e2e` (3): `test_26_successful_simulated_execution_end_to_end`, `test_explicit_slippage_tolerance_replaces_the_generic_guard`, `test_price_runs_away_between_signal_and_order_is_missed_not_chased`. They still assert the pre-M1-update Candle Momentum defaults (10-pip target, 1.0990 stop); the M1 update changed the defaults to 20 / 50. They were not edited because that would mean choosing which numbers are "right" for a strategy you asked not to be touched.
* `tests.test_fixed_target_execution` (1): `test_pip_size_can_be_set_in_the_browser_validated_and_cleared` expects a 10-pip Candle Momentum preview; the current default is 20.
* `tests.test_browser_phase7a` (2): `test_preset_changes_chart_immediately_and_persists_across_reload`, `test_white_and_black_candles_are_kept_and_warned_about_never_replaced` (chart palette tests).
* A full single-process run of the baseline could not be completed in the build environment, so the baseline total is not claimed; only these specific tests were compared.

Existing tests that were **changed** (the behaviour they pinned is deliberately different now, stated here so it is auditable): the strategy-catalogue and selection tests (`test_strategies`, `test_ema_atr`, `test_browser_commandcentre`) now expect the five strategies and treat Trend Continuation and EMA/ATR as retired but still resolvable; `test_web` lists the new routes and scripts; `test_phase5_complete` expects the two central protection checks; the five broker-midnight tests in `test_daily_loss_reset` were rewritten for the local-midnight day (with the same strictness, using a local timezone that differs from the broker's so they cannot pass by accident); one assertion in the strategy-lab test was narrowed to the strategy's own information (the timeframe selector now legitimately lists every timeframe). No test was deleted or skipped to get a pass.

## New tests

`test_candle_momentum_timeframes` (M1, M2, M5 through the real signal -> Risk Engine -> order path; switching; per-timeframe duplicate protection; no silent fallback; the original-behaviour characterisation), `test_browser_candle_momentum_timeframe_choice`, `test_five_strategies`, `test_new_strategies_execution`, `test_risk_and_execution_features` (local midnight and DST, daily history, consecutive losses, central protections, lot adjustment, calculator, settings scopes, entry limits, same-candle second entry, Pause / Stop / Emergency), `test_more_backend_features` (insufficient-funds end to end, notifications, Hard Reset, sessions, chart stores, Vol_20 / Crash 100 FIXTURES, duplicate prevention, AI-advisory guard), `test_browser_ui_features` (maximise / restore / responsive, adaptive toolbar, moving average, manual lines, Trade History live update, notification de-duplication and the chime, Settings persistence, Hard Reset confirmation, calculator, sessions).

## What these tests do NOT prove

* **Nothing was verified against a live MT5 account or a real broker.** The bridge is replaced by `tests/fake_bridge.py`; Vol_20 and Crash 100 data are clearly labelled FIXTURES, not your broker's numbers.
* The `M2` line added to `mt5/TradeMateBridge.mq5` has **not been compiled** (no MT5 here). Compile the EA once and check it reports no errors.
* The MT5 bridge does not expose margin per lot, so the margin-based lot reduction runs in its documented bounded-reduction mode until an adapter supplies it.
* The strategies are not back-tested or forward-tested, and no profitability is claimed. The AI-assisted layer is not enabled (see `docs/AI_ADVISORY.md`).
* The browser audio test replaces `HTMLAudioElement.play` with a counter: it proves the chime is requested exactly once and never for history, not that your speakers play it. Use the Test button on the Settings page.

---

# Candle Momentum v2 (tick-driven engine)

**No test here can place a real order.** Engine tests use `tests/fake_mt5_bridge.py` (deterministic, manual clock, no threads/files/network, no order method). End-to-end tests use the real
`MT5Adapter` -> Risk Engine -> pipeline path against `tests/fake_bridge.py` (an in-memory stand-in for the MT5 EA). Passing proves the rules and wiring in **simulation**, not live-broker behaviour.

```
python3 -m unittest tests.test_candle_momentum -v          # 86: direction, trigger, jumps, validity, one-entry-per-candle, restart recovery, feed faults, instruments, state machine, diagnostics, config
python3 -m unittest tests.test_candle_momentum_e2e -v      # 15: real Autopilot + Risk Engine + pipeline + adapter + fake bridge; Watchdog; HTTP API; ALL on real threads (~0.35 s trigger -> simulated order)
python3 -m unittest tests.test_candlemomentum_js           # runs node tests/candlemomentum_test.js (panel logic)
python3 -m unittest tests.test_browser_candle_momentum -v  # 7: the panel in headless Chromium (skipped, not passed, without Playwright)
```

Mapping of the specification's 30 required scenarios (all in `tests/test_candle_momentum.py` unless noted):
1-3 `DirectionTests` (bullish / bearish / neutral) · 4-5 `TriggerTests` · 6 `test_06*` (jump beyond the trigger, incl. beyond the old 2 pips, and far beyond = MISSED) · 7 `test_07` and the threaded e2e test (no manual Analyse) ·
8 `test_08` · 9 `test_09` · 10 `OneEntryPerCandleTests` · 11 `test_11` (+ Watchdog duplicate start in e2e) · 12 `test_12` (+ API repeat in e2e) · 13 `PersistenceTests` (position resumes, pending signal expires,
executing = unknown) · 14 `test_14` · 15 `test_15` · 16-17 `test_16`, `test_17` · 18 `test_20_target_calculated_from_actual_fill` · 19 `test_19` · 20-21 `test_21` + pipeline guard test · 22 `test_22` (+ real Risk Engine in e2e) ·
23 `test_23` (+ real broker minimum in e2e) · 24 `test_24`, `test_24b` · 25 `test_25`, unknown pip, absurd pip · 26 `test_26` (+ full-chain e2e `test_26_successful_simulated_execution_end_to_end`) ·
27-28 `test_27`, `test_28` (+ real adapter in e2e) · 29 `test_29` (M1/M5/M15) · 30 `test_30` (EURUSD and Crash 100 Index).

The eleven v1 Candle Momentum snapshot tests in `tests/test_candle_and_choch_targets.py` asserted the removed behaviour (including the 2-pip "late" rule) and were **retired on purpose**; the replacement
mapping is in that file. The CHoCH / pips / lot / hub / ledger tests in it are unchanged.

Full regression (this is how it was run; each module in its own process):
```
for m in tests/test_*.py ...: python3 -m unittest tests.<module>     # non-browser: run 4 at a time is fine
python3 -m unittest tests.test_browser_<module>                      # browser modules: run ONE AT A TIME (parallel Chromium runs time out)
for f in tests/*_test.js; do node $f; done
```
Results are recorded in `IMPLEMENTATION_REPORT_CANDLE_MOMENTUM_V2.md`.

# Strategies and Command Centre tests

* `python3 -m unittest tests.test_strategies -v`: registry, validated persisted selection, H1/M5-only qualification of both strategies (BUY, SELL, WATCH, WAIT, stale/missing data, expiry, failed
  setups, counter-H1 rule, independence), strategy switching with pending proposals, Watchdog binding, Autopilot honesty. Candles only.
* `python3 -m unittest tests.test_browser_commandcentre tests.test_browser_strategylab -v`: the actual page in headless Chromium against the fake MT5 bridge (selection, analysis states, review, two-click
  execution, rejection, stale approvals, Watchdog, Autopilot, phone widths, light theme contrast, keyboard). Skipped, not passed, without Playwright/Chromium. These never place a real order.

# Strategy -> Risk -> Execution pipeline tests

`python3 -m unittest tests.test_pipeline -v` (also the "Strategy -> Risk -> Execution pipeline" group on the Full Test page). Every order goes to `tests/fake_bridge.py` through the real adapter:
**no MT5, no network, no real order.** Covers qualification contract (valid BUY/SELL, WATCH/WAIT, incomplete, stale data, inconsistent levels), the real Strategy Engine output through the real Risk Engine,
oversize/undersize handling, daily-loss and position limits, invalid symbol specs, eligibility (drift, spread, trading disabled, disconnected), stale-approval invalidation (expiry, account, rules, positions,
pending orders, thesis change, setup no longer qualifying), broker rejection/timeouts/lost answers, duplicate and concurrent execution, placement check, and that manual ordering is unchanged.
Simulated results do not prove live broker behaviour. See `docs/STRATEGY_TO_EXECUTION_PIPELINE.md` for the real DEMO check that is still required.

# Manual DEMO test checklist (do this with MT5 open next to TradeMate)

Setup: recompile + attach `TradeMateBridge.mq5` v0.2, Algo Trading on, `python app.py`, open http://127.0.0.1:5000.
Dashboard should show **MT5: CONNECTED**, Execution **DEMO READY**.

1. **Quotes** - Current Market: press the up/down arrows (or arrow keys). Symbol and bid change; chart follows.
   Add a symbol with "+ Add symbol" (it must already be in MT5 Market Watch).
2. **BUY** - Chart page, symbol, lot 0.01, click BUY. Status: Sending... then Executed ✓. Check MT5 Trade tab: position is there.
   Open Trades shows it. P/L turns **blue** when positive, **red** when negative.
3. **SELL** - same with SELL.
4. **TP** - drag the blue TP line, check the "Move TP ... -> ..." bar, press APPLY AT BROKER.
   Executed ✓; MT5 Trade tab shows the same TP. Try an invalid TP (too close to price): you get Failed ✗ with the reason and the line goes back.
5. **SL** - same with the red SL line. (No SL yet? press "+ SL" on the chart.)
6. **Close** - Open Trades: CLOSE, then CONFIRM?. Row disappears only after Executed ✓; MT5 shows it closed in History.
7. **External sync** - open a position from the MT5 mobile app: it appears in TradeMate within ~2 s. Change its SL, or close it, on mobile: TradeMate follows.
8. **Failure honesty** - quit MT5 and click BUY: you must see Failed ✗ (nothing sent), never Executed.

## Dynamic symbol test (Phase 5) - needs MT5 + the v0.3 EA

1. **Existing symbol still trades:** pick your working symbol, BUY 0.01, check MT5, close it.
2. **Find a non-preset instrument:** `python -m trademate.broker.find_symbols gold` (or oil, US30, BTC...).
   Pick one that is NOT in the Live Market table.
3. **Add it:** Dashboard -> Search broker symbols -> type -> click the result. It appears as a chip and is selected.
4. **Market data / chart:** bid/ask appear within a couple of seconds; the chart loads candles
   (first load of a new symbol may show "No candles yet" for a moment: it retries every 5 s).
5. **Trade:** lot size must respect that symbol's minimum/step. BUY it. Status: Sending... -> Executed ✓.
6. **MT5:** the position is in the Trade tab (and the symbol is now in Market Watch).
7. **TradeMate:** it shows in Open Trades with live P/L (blue/red); SL/TP drag and CLOSE work.
Optional: `python -m trademate.broker.live_test <SYMBOL>` runs steps 5-7 plus TP/SL/close and SELL automatically.


## Phase 4 / execution-safety checks

    python3 -m compileall .
    python3 -m unittest discover -s tests -t .
    node tests/pl_test.js            # TP/SL pips + money maths (also run by the unit tests)

`tests/test_execution_safety.py` covers: lost answer after execution, command taken but never answered (no blind
resend), internal error after / before the send, journal failure (fails closed), reconcile + resolve, and that
deleting the local history sends nothing to MT5. `tests/test_project_integrity.py` checks every internal import.
Those tests use `tests/fake_bridge.py`, a stand-in for the EA. They prove TradeMate's side. A final check against
the real MT5 DEMO terminal is still yours to do: place a trade, then close it, then open the History page.

## Phase 5 / Jigsaw 5.1 - Risk-based position sizing (calculation only, places NO order)

**Automated (no MT5 needed):** `python -m unittest tests.test_risk_sizing -v`
Covers TEST 1-8: basic maths (gold, EURUSD, USDJPY, cross pair), 0.5% / 2% risk, wider / tighter SL, invalid BUY / SELL,
minimum / maximum / volume-step handling, invalid risk / prices / symbol, and that the route sends no order.

**Live, with MT5 open:** `./launch.sh` -> open **Strategy** page -> **Risk Calculator**.
1. Pick a symbol, **USE LIVE PRICE**, set a Stop Loss below (BUY) or above (SELL) the entry, Risk 1% -> CALCULATE.
   Check: Maximum Risk = 1% of the Equity shown on the Dashboard; Estimated Loss <= Maximum Risk and close to it.
2. Risk 0.5 -> volume roughly halves. Risk 2 -> roughly doubles (or is rejected above the broker/TradeMate maximum).
3. Move the SL farther -> volume falls. Closer -> volume rises (or is rejected above the maximum).
4. BUY with SL above entry, SELL with SL below entry -> REJECTED with a reason.
5. A tiny risk with a very wide SL -> REJECTED "below the broker minimum" (never silently rounded up).
6. Cross-check one result by hand in MT5: Estimated Loss = (|entry - SL| / tick size) x tick value x volume.

## Phase 5 / Risk Engine v5.2 - Risk limits & protection (decides only, places NO order)

**1. Controlled scenarios (no MT5, no demo trades):** `python -m trademate.risk.scenarios`
Synthetic $52,000 account. Prints PASS/FAIL and the exact reason for TEST 1-7 (plus 4b).

**2. Automated:** `python -m unittest tests.test_risk_limits tests.test_risk_sizing -v`

**3. Live check with MT5 (read-only):** `./launch.sh` -> Strategy page.
- *Risk Engine* card: Equity/Balance match MT5. Open Positions and Current Active Risk follow your real positions
  (each position's risk = open price -> its SL, from MT5 tick data). A position with no SL shows "+ unknown" and blocks approvals.
- Change a limit (e.g. Max Risk / Trade 5) -> SAVE LIMITS. Any value above 0 up to 100 is accepted; it survives restart.
- *Proposed Trade* card: pick a symbol, USE LIVE PRICE, type an SL by hand (the multi-timeframe panel above it proposes levels but does not fill this form), EVALUATE.
  Expect APPROVED + volume, or REJECTED + the exact reason, plus a per-check list.

## Phase 5 completion (5.1-5.5) - Risk Engine + entry-line P&L

`python -m unittest tests.test_phase5_complete -v` and `node tests/pl_test.js` (also run by the normal suite).
Covers: per-trade limit (4% vs 3% rejected, exactly 3% passes), position count, total active risk, ALL failed checks listed,
rounding down + risk of the rounded volume, below-minimum rejection, execution-ready result, no order capability,
BUY/SELL entry-line P&L (positive/negative for both), open / SL change / volume change / close detection, MT5 disconnected.

Manual with MT5: open a trade in MT5 -> Risk Engine card shows it within ~3 s (one row, with P&L and risk at SL); the chart's entry
line shows e.g. `BUY +25.40 USD` / `SELL -9.45 USD`, updating live. Close it in MT5 -> it disappears from both.

## Phase 6 - Risk-gated execution (needs no new install: Flask only, tests use unittest)

**Automated (no MT5 needed):** `python3 -m unittest tests.test_phase6 -v` (fake bridge, proves TradeMate's side only).
Covers spec tests A (approval before any command), B (rejection never reaches the bridge), C (resize suggestion, entry/SL/TP kept),
D (invalid input incl. NaN/Infinity), F (broker rejection, no false position), J (lost answer -> unknown, retry sends nothing),
plus the post-modify risk report, breach reporting that closes nothing, and an externally opened position being listed.

**Manual DEMO checklist (NOT run by the automated tests - you must do these with MT5 open):**
1. **E** BUY 0.01 with an SL: click BUY -> review dialog shows risk/reward -> Confirm -> `Executed` and the position is in MT5's Trade tab.
2. **Risk rejection live:** lower "max risk per trade" in Settings, repeat: "Rejected by Risk Engine", MT5 shows nothing.
3. **Resize live:** use a large lot: dialog offers "Use X lots instead"; confirm; MT5 shows X lots with your SL/TP.
4. **G** open a position in the MT5 mobile app: it appears in TradeMate within ~2 s with the right ticket/SL/TP.
5. **H** drag SL/TP, APPLY: shown values must equal MT5's. If a limit is now breached, a "Risk Engine:" toast says so; nothing is closed.
6. **I** CLOSE then CONFIRM?: the row leaves only after `Executed`; MT5 History agrees.
7. **K** the entry line label reads `BUY +12.34 USD` / `SELL -4.70 USD` (your account currency) and tracks MT5's floating P/L.
8. **Disconnect:** quit MT5, click BUY: refused, nothing sent, no position shown.

## Phase 6 LIVE DEMO runner (run this yourself, on the machine with MT5)

    python -m trademate.broker.live_phase6_test EURUSD            # add --manual-g to also test a position you open on your phone

Needs: MT5 open and logged in to the **DEMO** account, `TradeMateBridge` EA attached and running. No new install.
It asks you to type `YES` first, then: verifies DEMO (refuses otherwise), checks the Risk gate refuses bad orders (nothing reaches MT5),
opens a 0.01-lot BUY and SELL **each with SL and TP**, confirms them against MT5, modifies SL then TP, tries an invalid modification
(must be refused), closes them, and confirms your pre-existing positions were not touched. One run at a time (`data/live_phase6.lock`).
If the Risk Engine refuses the smallest trade for your limits, that step is SKIPPED, never forced. An unknown outcome aborts the run
with nothing retried. Log: `data/live_tests/phase6-<time>.jsonl`. Send me the printed REPORT and that log file.
Not testable safely on a real broker (reported SKIP): lost answer/timeout, broker-side rejection, partial fill: simulated tests only.

## REAL DEMO EXECUTION TEST: one real order, left open for you to inspect in MT5

Ordinary tests never place orders (fake bridge only). This script is separate and needs explicit opt-in (env variable AND flag):

    cd <your TradeMate folder>
    source venv/bin/activate                      # your existing virtual environment (whatever you normally activate)
    python -m unittest discover -s tests -t .      # ordinary suite: SAFE, places no orders, repeatable
    TRADEMATE_REAL_DEMO_ORDER=YES_PLACE_ONE_DEMO_ORDER python -m trademate.broker.real_demo_execution_test EURUSD --place-real-demo-order

Optional: `--side sell`, another SYMBOL, `--expect-server <server>` (default = `[mt5] expected_server` in config/settings.toml).
Requires MT5 open on the DEMO account with TradeMateBridge running. It refuses (nothing sent) if: not authorized, MT5 unreachable or
stale, account not reported as demo by MT5, server differs from the expected one, symbol not fully tradable, price unusable, an
earlier order is still unresolved. It sends ONE order (broker minimum volume, SL and TP set), through the Risk Engine, verifies
it against MT5, and leaves it OPEN. It never closes anything. Reports: `[REAL DEMO TRADE VERIFIED]`, `[TRADEMATE REJECTED]`,
`[MT5/BROKER REJECTED]`, `[MT5 CONNECTION FAILURE]`, `[EXECUTION OUTCOME UNKNOWN]`, `[ORDER ACCEPTED BUT NOT FULLY VERIFIED]`.
Log: `data/live_tests/real-demo-<time>.jsonl`.
Note: the older `python -m trademate.broker.live_test` sends orders WITHOUT a stop-loss and bypasses the Risk Engine; prefer the scripts above.

## Phase 7A - Market structure + candle appearance

Run `tests/run_tests.sh` (full regression) or `tests/run_tests.sh --phase7a`. Details, rules and the manual chart checklist:
`docs/MARKET_STRUCTURE.md`. Covers: ordered confirmed swings and HH/HL/LH/LL, protected-swing CHoCH, directional BOS, wick vs close,
duplicate-event prevention, state transitions, provisional isolation, ambiguous state, the synthetic multi-event analogue (not live
CRASH_100 data), bad candles, duplicates, ordering, pivot confirmation timing, and no-lookahead on every prefix. Also covers adaptive
preset colours and contrast, hex validation, persistence, theme switching, and browser pixel checks of candle colours / overlay / mobile
width. Browser tests report SKIPPED if Playwright is absent.

Focused structure/Analysis/Strategy parity regressions:
```
python -m unittest tests.test_structure_engine tests.test_structure_regressions tests.test_structure_api tests.test_analysis tests.test_mtf_timeframes tests.test_mtf_strategy tests.test_mtf_api -v
```
These are deterministic and place no orders. The Analysis-vs-Strategy parity test compares the shared direction, configuration and event
facts on identical candle inputs; it does not claim the live broker feeds are atomic or that the reported CRASH_100 history was reproduced.

Theme addendum: `tests/test_browser_phase7a.py` (theme tests) toggles the app-wide theme in real Chromium, checks persistence in a fresh
browser profile, legacy migration, candle-colour independence, overlay palette, phone reachability and a WCAG text-contrast audit of every
page in the light theme; `tests/test_chart_settings.py` covers the `theme` field and its isolation from the colours.

## Phase 7A.1: Analysis, stop-loss calculator, server check, Full Test

**Run everything:** `bash tests/run_tests.sh` (about 3 minutes; dots are progress, it is not hung). Only 7A.1: `bash tests/run_tests.sh --phase7a1`.
The script uses your project virtual environment automatically (`venv/` or `.venv/` or the activated one). If Node.js is missing the
JavaScript checks are reported as NOT RUN, never as passed. If it says "Permission denied", use `bash tests/run_tests.sh`.

### Full Test page (a browser dashboard for the same tests)

Start TradeMate (`python app.py` or `./launch.sh`) and open **http://127.0.0.1:5000/full-test** (the same page is at
`/Full Test.html`; in the sidebar under "Developer"). It runs the repository's own `unittest` groups and Node checks and shows
PASS / FAIL / PARTIAL / BLOCKED per group and overall, counts of passed / failed / skipped / not run, durations, failure traces, and
the last run's time. Skipped tests are NOT passes; a partial or subset run is never an overall PASS.

* The file `Full Test.html` at the project root is the page. Opened directly from disk (file://) it cannot run tests: browsers do not
  let a file page call the local server, and the runner deliberately has no cross-origin access. It says so and links to the
  server URL. Use the URL above.
* Safety: it can only start allowlisted group ids (never a command or path from the browser), only in development/demo mode, only
  when the server is bound to this computer (`[ui] host`), only for requests from this computer, with a header a foreign web page
  cannot send. Runs happen in background subprocesses, one at a time, with a timeout; output is shown as plain text.
* **It never places an order.** The runner strips `TRADEMATE_REAL_DEMO_ORDER` from the test environment. The real-MT5 tests are
  listed on the page as NOT RUN with their terminal commands.
* **Simulated brokers only.** Passing this suite does not prove real MT5 order execution; only the opt-in demo test does.

### Headway-Demo server check

`[mt5] expected_server = "Headway-Demo"` in `config/settings.toml` (precedence: env `TRADEMATE_EXPECTED_SERVER` > file > built-in
default). Orders are REFUSED when the connected server differs, is not reported, or no expected server is configured
(`trademate/broker/server_check.py`; case and extra spaces are ignored, nothing else: "Headway-Demo2" does not match). Closing and
modifying existing positions go through the same guard. The server name shown in MT5: Navigator > Accounts, or the terminal's
Journal tab right after login ("authorized on <server>"); TradeMate shows the server it reads in the status line, and the real-demo script prints it.
"Demo" in a server name is never trusted alone: the account must also be reported as DEMO by MT5.

### Authorised demo-only test (explicit opt-in, separate from the suite)

`TRADEMATE_REAL_DEMO_ORDER=YES_PLACE_ONE_DEMO_ORDER python -m trademate.broker.real_demo_execution_test EURUSD --place-real-demo-order`
(see the section above). `python -m trademate.broker.live_phase6_test` is the longer BUY/SELL/modify/close cycle. Neither runs from the suite or the Full Test page.

### What the new tests cover

`test_structure_regressions` (engine rules), `test_analysis` (status states, evidence, honesty, API), `test_sl_risk_first`,
`test_sl_price_first`, `test_sl_instruments` (calculator + `/api/risk/sl`), `test_server_validation`, `test_command_expiry`,
`test_testrunner` (runner safety), `test_slcalc_js` + `tests/slcalc_test.js` (panel logic), `test_browser_phase7a1` (real Chromium: Analysis
page, stop-loss panel, Full Test page, HTML-injection, contrast, mobile).
Fixed: MT5 command expiry used `int()` and could be up to 1 s SHORTER than requested; under load commands expired and some tests failed
intermittently (reproduced 3 failures in 4 stressed runs, 0 in 8 after the fix); it now rounds up.

## Phase 7C: Fair Value Gap engine (pure Python; no MT5, no Flask, no orders)

    python3 -m unittest tests.test_fvg_engine -v          # focused: 52 tests, well under a second
    python3 -m unittest tests.test_fvg_engine tests.test_structure_engine tests.test_structure_regressions tests.test_liquidity_engine tests.test_analysis tests.test_project_integrity

Covers: valid bullish / bearish gaps, no gap, exactly touching boundaries (rejected, both directions), insufficient history, repeated
analysis (identical result, repeated rows, growing series, prefix = no lookahead), shuffled and newest-first input, malformed / zero /
negative / non-finite candles and conflicting duplicates (fail closed, no gap), boundaries and timestamps, the still-forming candle
(`last_candle_closed`), input shapes, a brute-force reference comparison on random series, and that the engine has no trade/broker
imports or fields. It is picked up by `bash tests/run_tests.sh` and by the Full Test page's "General" group automatically.

### Phase 7C task 2: FVG chart overlay and the overlay controls

    python3 -m unittest tests.test_fvg_api tests.test_fvg_js -v          # server payload, saved preference, page wiring, fvg.js (Node): fast
    node tests/fvg_js_test.js                                             # the Node checks on their own
    python3 -m unittest tests.test_browser_phase7c_fvg -v                 # real headless Chromium, ~100 s; SKIPPED (not passed) without Playwright

The browser tests draw deterministic candles and check the zones in **pixels**: each zone edge must sit where the neighbouring
candle's wick ends (so the boundary is tied to the real price scale), the zone spans candle 1 to candle 3 by timestamp, unconfirmed
zones are fainter and dashed, repeated refreshes never stack zones, symbol / timeframe changes replace them, all 8 combinations of the
three overlay buttons work, hiding keeps the analysis data, the settings panel (Show all / Hide all) persists across a reload, garbage
or unavailable FVG data never crashes the chart, trade-level lines / zoom / pan still work with every overlay on, light theme, and a
phone-width screen. The read-only stub broker has no order method; the tests assert that no order endpoint is called.

## Phase 7C task 3: Order Block engine (pure Python; no MT5, no Flask, no orders)

    python3 -m unittest tests.test_orderblock_engine -v        # focused: 59 tests, about a second
    python3 -m unittest tests.test_orderblock_engine tests.test_fvg_engine tests.test_structure_engine tests.test_structure_regressions tests.test_liquidity_engine tests.test_analysis tests.test_project_integrity

Hand-derived fixtures check: bullish / bearish candidates, structural confirmation (BOS and CHoCH), unconfirmed large moves, expiry
(replaced reference, wrong origin, `max_leg_candles`, ambiguous break), price invalidation (close vs wick, same-candle conflict),
confirmation timestamps and the "not knowable yet" case, insufficient / malformed / mis-ordered / inconsistent input, repeated analysis
and duplicate prevention, and the forming candle. Property tests on random series recompute every rule from the raw candles, check that
every prefix of the data agrees with the full run (no look-ahead), and that bullish and bearish results are exact mirrors. It is picked
up by `bash tests/run_tests.sh` and the Full Test page's "General" group automatically.

## Phase 7C integration repair: order blocks on the chart, FVG / order blocks on the Analysis page, pending orders on the chart, chart-only theme

    python3 -m unittest tests.test_repair_api tests.test_ob_js -v          # fast (about a second): API, saved settings, source-level guarantees, ob.js
    node tests/ob_js_test.js                                                # the Node checks on their own
    python3 -m unittest tests.test_browser_phase7c_repair -v                # real headless Chromium, about 3 minutes; SKIPPED (not passed) without Playwright

`test_repair_api` checks `/api/candles?order_blocks=1` against hand-derived candles (default 3/3 swings), the Analysis page's FVG and order
block views (genuine results, empty states, error states, one engine crashing without affecting the others, hidden overlays changing
nothing), the module list (an engine is `available` only if it ran), `chart_theme` / `order_blocks_visible` validation and persistence, and
source-level guarantees (pending-order drawing contains no network or order code and is not part of the draggable lines; the chart button
cannot call the application-wide theme code; distinct overlay colours; no demo data).
`test_browser_phase7c_repair` draws deterministic candles and checks in **pixels**: a zone spans the source candle's real wick, fills stop at
the newest candle, candidates are fainter and dashed, finished blocks are not drawn, repeated refreshes never stack zones, all 16 overlay
combinations work, hiding keeps the data, garbage data never crashes the chart; the Analysis page's panels, empty / error states and module
list; pending orders (through the fake MT5 bridge and the real adapter) at their real prices for every order type, an order placed exactly at a
candle's high is drawn on that wick, moved / cancelled / filled orders update, other symbols and far-away orders are handled honestly,
rendering sends no order command, pending lines are not draggable; and the chart theme button changing only the chart in both pairings.

Existing tests changed in this repair (each pinned a state the requirements deliberately changed; none was deleted, skipped or loosened):
`test_analysis` (module statuses are per response), `test_liquidity_api` (`unavailable` rather than `not_available` when an engine could not run),
`test_chart_settings` (new default fields; the chart theme button now calls `toggleChartTheme()` and must not call `toggleTheme()`),
`test_web` (script list), `test_fvg_api` / `test_fvg_engine` / `test_orderblock_engine` ("no integration yet" guards became "only the analysis and
API layers use the engine"), `test_browser_phase7a` (the application-wide controls are the top-bar toggle and the Settings select; the chart's
own button is chart-only), `test_browser_phase7c_fvg` (four overlays instead of three).


## Daily loss, clocks and candle countdown (manual check, after the automated tests)

1. **Strategy** page > Account risk: *Today's Loss* and *Remaining Daily Loss* show amounts (not "unknown"). The note line says
   `Day = broker server time (...)`. If it says unknown, read the reason (market closed / MT5 disconnected) and consider setting `[risk] broker_timezone`.
2. Compare with MT5: History tab, sum of today's closed trades (broker time) + floating P/L = Today's result.
3. After the broker's midnight (MT5 Market Watch clock) the counters start again without restarting TradeMate; restarting TradeMate never changes them.
4. Top bar: LOCAL and NEW YORK clocks tick every second; compare NEW YORK with a trusted clock (EST in winter, EDT in summer).
5. Chart toolbar: "closes in" follows the MT5 chart's own countdown (MT5: Options > Charts > Show... or the candle's close time) to within a second or two; change timeframe
   and symbol: the number changes at once. Close MT5 or stop the bridge: the countdown shows a dash.

## Task 2: Analysis summary, candle defaults, button removal, zone / pending-order overlays

    python3 -m unittest tests.test_task2_analysis_and_defaults -v     # pure Python: colour default + migration, summary rules, FVG fill status, engine -> payload data flow
    python3 -m unittest tests.test_browser_task2 -v                   # real headless Chromium, stub / fake broker only, ~2 minutes; SKIPPED (not passed) without Playwright

The browser file covers: the oversized buttons are gone and the Trade Panel order entry remains (and sends nothing until confirmed), the
red / black default, chart theme independent of the application theme and surviving reload, zones at the engine's prices for this symbol and
timeframe, stale and unavailable zone notices, the five Analysis sections and their unavailable state, and that rendering pending orders sends no
broker command and never shows stale orders as current. No test uses a real MT5 or a live account; anything needing MT5 is NOT RUN.
Existing tests that pinned the old default colours, the removed buttons or the old Analysis layout were updated to the new behaviour (they
now open **Technical details** before reading the detailed panels).

## Phase 8: multi-timeframe Strategy Engine

**Automated (no MT5, no orders; generated candles and a read-only stub feed that has no order method):**

```
python3 -m unittest tests.test_strategy_intelligence tests.test_mtf_timeframes tests.test_mtf_strategy tests.test_mtf_api tests.test_watchdog -v
python3 -m unittest tests.test_browser_mtf -v      # Strategy page panel in headless Chromium (requires Playwright/Chromium)
```

The deterministic tests cover: closed vs forming candles and broker-clock uncertainty; missing, duplicate, conflicting, out-of-order, invalid,
misaligned, stale, future and insufficient candles; native H4 retrieval with no aggregation/substitution; context with partial timeframe data;
bullish, bearish, range, transition, volatility and uncertain regimes; trend/pullback, sweep/reaction, reversal, breakout/retest, range and
countertrend candidate evidence; BUY/SELL and the mandatory data, zone, trigger, stop and target gates; validation-snapshot fields; the API,
`[strategy]` settings and the analysis-only/Risk/Execution boundaries. Watchdog tests exercise candidate-state transitions, deduplication,
stale-data suspension, broker disconnection and ensure candidate discovery never creates a trade alert or invokes an order method.

**Manual (not done by the automated suite):** open Strategy, press **Analyse** on a symbol with MT5 connected and compare each timeframe's bias, invalidation
level and the zone/trigger with the MT5 chart. Check that native H4 data status is `ok` on an instrument with sufficient broker history.

**Broker / demo testing:** none. Multi-timeframe decisions have not been compared with a live or demo feed, and no order is ever placed by this feature.
Historical market-data backtesting and outcome learning are also **not run**. The validation snapshot is not persisted or joined to broker fills
or closed-trade history.

## Command Centre

Run `python3 -m unittest tests.test_strategylab tests.test_watchdog tests.test_mtf_timeframes tests.test_mtf_strategy tests.test_mtf_api -v`.
The fake/read-only broker tests cover capability status, native H4 analysis, symbol validation, watchdog start/stop, one active symbol, setup-event
deduplication and the fact that the monitor never calls an order interface. `tests.test_browser_strategylab` covers layout, the broker-backed
search/analyse path, Account Centre's verified-identity display/clearing, preferred-name save/clear, and graceful rendering when browser storage is
blocked in Chromium; it is **SKIPPED, not passed**, without Playwright/Chromium.
`node tests/labgreeting_test.js` exercises greeting, browser-local display-name persistence and validation; the alert preferences are exercised in the
browser workflow when Chromium is available.

**Manual DEMO check:** with MT5 connected, select a symbol and run ANALYSE; confirm the result is from the live broker and no order is sent. Switch
Watchdog on and verify its symbol and last-check time, then switch it off. Desktop notifications require an explicit permission click; alerts and
sound are opt-in. Verify that these settings survive a reload in the same browser. The Watchdog runs only while the TradeMate server process is alive,
monitors one symbol at a time, and browser alerts are delivered only while the Command Centre page is open.

**Account Centre:** open it from the sidebar while the MT5 bridge is connected. Confirm broker, server, environment and login match the fresh MT5
snapshot; disconnect the bridge and verify the identity fields clear. There is no TradeMate account sign-in/switching workflow because the bridge
does not support login changes or pin pending commands to an account identity. Do not switch the connected terminal while a TradeMate command is
pending or unresolved. Per-account risk profiles are also unsupported. Automated tests use a stub account only and do not verify real broker
authentication or account switching.

**Welcome/name:** from Settings, save a preferred display name; return to Command Centre and verify it appears in the local-time greeting. Clear it
and verify the generic greeting returns. The display-name and welcome-back preferences remain browser-local. The welcome layout keeps the market
controls unchanged and removes the redundant status/metric/step content.

## Market-structure audit (CHoCH / BOS) - automated

    python3 -m unittest tests.test_structure_audit tests.test_structure_engine tests.test_structure_regressions \
        tests.test_structure_api tests.test_analysis tests.test_task2_analysis_and_defaults tests.test_mtf_strategy -v

`tests/test_structure_audit.py` covers: bullish/bearish CHoCH, BOS both ways, HH/HL/LH/LL ordering, pullback and continuation
non-CHoCH, duplicate prevention, post-CHoCH transitions and subsequent BOS, the stuck-direction defect (origin swing), provisional vs
confirmed, undefined/ambiguous structure, same-candle conflict, no-lookahead prefixes, an H1 analogue (synthetic, NOT the live
CRASH_100 chart), identical structural facts on Analysis and Strategy, malformed/duplicate/out-of-order/insufficient/stale data, the
"internal structure is not supported" statement, and the diagnostic trace tool. Live check (manual, read-only): export CRASH_100 H1
candles from `/api/candles?...&count=500`, run `python3 -m trademate.structure.trace candles.json`, and compare each printed event
with the Analysis page's expanded event explanation. This has NOT been done by the automated suite.

### Command Centre interaction when broker data is unavailable

The Command Centre instrument input, saved-market dropdown, and ANALYSE button remain interactive when broker status is disconnected or unavailable. This lets the user request an operation and see the backend's specific error instead of encountering disabled controls with a prohibited cursor. This is a UI usability change only: symbol validation, MT5 snapshot freshness, candle freshness, and setup qualification remain enforced by the backend. A disconnected or stale feed must not produce a confirmed setup.

## New in this release
`tests/test_candle_and_choch_targets.py`, `test_autopilot.py`, `test_fixed_target_execution.py`, `test_watchdog_hub.py` (no browser) and
`test_browser_trading_controls.py` (Playwright). Two pre-existing failures in `test_browser_phase7a` (colour-picker hex casing, a preset check)
also fail on the original project and are unrelated to this work.

## EMA 20/50 + ATR
`python3 -m unittest tests.test_ema_atr -v` (39 tests; simulated candles and the in-memory fake bridge, no real orders). Covers the EMA filter both ways, bullish and
bearish crossover, once-only signalling, ATR (Wilder) values, BUY/SELL stop and target placement, insufficient history, incomplete/forming candles, zero or invalid
ATR, invalid settings, registration/selection, the Watchdog and decision hub, qualification by the pipeline, Risk Engine rejection (nothing reaches the broker),
invalid lot, and duplicate-setup refusal. The three catalogue assertions in `tests/test_strategies.py` and the dropdown assertion in
`tests/test_browser_commandcentre.py` were extended because a fourth strategy now exists; no assertion was removed or loosened.


# TradeMate Vision tests

    python3 -m unittest tests.test_rapid_fire -v          # Rapid Fire engine, Anti-Twin, restart, history, API (real MT5Adapter + FakeBridge, virtual clock)
    python3 -m unittest tests.test_browser_navigation -v  # every route, refresh/back/forward, malformed hashes, drawer, no duplicates (Chromium; skipped without Playwright)

These use a simulated bridge. They do **not** prove behaviour on a live MT5 / broker; see "NOT verified" in `docs/RAPID_FIRE.md`.

    python3 -m unittest tests.test_browser_targetmonitor -v   # the gauge: positive / negative / zero / reached / stale / missing broker (Chromium, stub data)
    node tests/targetmonitor_test.js                            # the gauge's pure decision logic (also run by tests/test_targetmonitor_js.py)

## Strategy enhancements (Bull/Bear v3, Anti-Twin, Market Structure R:R, CHoCH distance)
* `python3 -m unittest tests.test_bull_bear tests.test_strategy_enhancements -v` (simulation only: simulated candles, a fake broker and an in-memory executor; nothing can place a real order).
* `python3 -m unittest tests.test_browser_bullbear -v` (headless Chromium; skipped when Playwright is missing).
* These prove the rules and wiring in simulation, **not** behaviour on a live MT5 account (see `docs/BULL_BEAR.md`, "Needs live verification").

## v5: Rapid Fire money target, shared Anti-Twin, journal, navigation capsule
    python3 -m pytest tests/test_rapid_fire.py tests/test_bull_bear.py tests/test_candle_momentum_antitwin.py tests/test_trade_journal.py -q
    python3 -m pytest tests/test_browser_rapidfire_nav_journal.py tests/test_browser_navigation.py tests/test_browser_bullbear.py -q   # Playwright
Everything runs against the simulated bridge (FakeBridge); live MT5 behaviour is not covered. Known pre-existing failures are listed in the release notes.

## v5.2: Anti-Twin execution speed
    python3 -m unittest tests.test_antitwin_speed tests.test_bull_bear tests.test_candle_momentum_antitwin -v
    python3 -m tests.antitwin_latency --runs 10      # measured latency on the EA-emulating bridge (about a minute per setting)
