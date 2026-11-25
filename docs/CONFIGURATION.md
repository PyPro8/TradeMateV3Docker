# Configuration

## config/settings.toml

Read at start-up, never written by the application or by `launch.sh`. Sections used by the features in this version:

| Section / key | Meaning |
|---|---|
| `[ui] host`, `port` | Where the web interface listens (default `127.0.0.1:5000`). |
| `[ui] timezone` | IANA timezone of "local" time. It is also the **timezone of the daily risk period** (local midnight). Empty = the computer's timezone. `TRADEMATE_TIMEZONE` overrides. |
| `[risk] broker_timezone` | Only used as a fallback to convert broker timestamps when the broker clock cannot be measured. |
| `[strategy] default_strategy` | One of `candle_momentum`, `choch`, `market_structure`, `bull_bear`, `trendline`. |
| `[strategy] choch_target_pips` (100), `market_structure_target_pips` (25), `bull_bear_target_pips` (25) | Default take-profit distances from the actual entry. Overridden by values you save in the interface. |
| `[candle_momentum]` | Candle Momentum defaults (per timeframe). Its saved per-instrument, per-timeframe configuration is in `data/candle_momentum_config.json`. |

## Command Centre settings and their scopes (Settings page)

Saved by the server in `data/trade_settings.json`. The browser only displays them: browser storage cannot override a saved value.

| Setting | Built-in default |
|---|---|
| Main instrument (global) | `Vol_20` |
| Lot size | `0.02` |
| Target distance | `25` |
| Protective stop distance | `100` |

Distances are in the instrument's pip/point unit (`trademate/instruments.py`; a pip size that cannot be verified makes the calculator say so). A value is looked up from the most specific scope that has a **saved** value, otherwise the built-in default:

1. strategy on one instrument, 2. one instrument (a lot normally lives here), 3. one strategy, 4. global.

Defaults apply only to what was never saved. "Forget saved values for this scope" removes that scope's values so the next scope or the default applies. Each value shows where it came from.

**Per-strategy extras (same scopes):** `risk_reward` (Market Structure only; empty = the fixed target distance, otherwise 0.1 to 100, shown as 1:N) and CHoCH's target distance (`target_distance`; if nothing is saved, `[strategy] choch_target_pips`, default 100, applies, and it is not a limit: any positive distance can be saved). Bull/Bear's own settings live in `data/bull_bear_config.json` (per instrument and timeframe; Candle Momentum-compatible keys plus the `anti_twin_enabled` key; the old `at_*` keys are retired and ignored, so an old file still loads). Rapid Fire adds `profit_per_fire` and `commission_per_lot` (see `docs/RAPID_FIRE.md`); `anti_twin_adverse_points` / `anti_twin_mode` are retired. The personal trade journal is `data/trade_journal.json` (kept by Hard Reset). Anti-Twin state is in `data/bull_bear_antitwin.json`, events in `data/bull_bear_events.jsonl`.

How they are used: the lot (saved per-instrument value, else this default) is the Autopilot and pipeline lot; the target and stop apply to CHoCH (target only), Market Structure, Bull/Bear (target and stop cap) and Trendline (stop cap). **Candle Momentum keeps its own saved configuration**; the 25/100 defaults do not change it (its M1 defaults are in `docs/CANDLE_MOMENTUM.md`).

## Execution and safety switches (`data/execution_settings.json`)

| Switch | Default |
|---|---|
| Allow multiple trades per candle | off (one entry per candle). On = at most two distinct entries per candle, per strategy, instrument and timeframe. |
| Automatic lot-size reduction / max retries | on / 2 |
| Consecutive-loss protection / limit | on / 3 |
| Emergency stop also closes positions | off |
| Notification chime | on |

## Data files (`data/`)

Settings (reset by Hard Reset, after a backup): `trade_settings.json`, `execution_settings.json`, `trade_selection.json`, `strategy_selection.json`, `pip_sizes.json`, `lot_sizes.json`, `chart_settings.json`, `chart_indicators.json`, `candle_momentum_config.json`, `risk_rules.json`.
Records (never touched by Hard Reset): `trademate.sqlite3` (trade history), `risk_daily_log.json`, `consecutive_losses.json`, `safety_lock.json`, `notifications.json`, `setup_ledger.json`, `pipeline_log.jsonl`, `candle_momentum_events.jsonl`, `candle_momentum_state.json`, `autopilot_state.json`, `manual_drawings.json`.

## Hard Reset (Settings page)

Shows exactly what will be reset and what is kept. Needs the typed phrase `HARD RESET`. Refused while Autopilot runs or the emergency lock is set. Every reset file is copied to `data/backups/hard-reset-<time>/` first and then removed so the defaults apply; the page lists backups with a **Restore** button (the current files are backed up to `pre-restore-<time>` first). It never touches trade history, risk records, the bridge or any code.

## Trading sessions (Dashboard)

Session windows (Sydney, Tokyo, London, New York) are the conventional local business hours of those centres converted to UTC with each city's daylight-saving rules. They are **not broker data**. Instrument availability comes from the asset class: synthetic indices continuous, crypto normally continuous, forex/metals the forex week (Sunday 22:00 to Friday 22:00 UTC), index CFDs reported as unverified. The bridge does not send MT5's per-symbol trading sessions, so exact per-instrument hours are never claimed.

## Notifications and the chime

`data/notifications.json` keeps every notification with a unique id and sequence number. A completed trade notifies once per ticket (wins and losses). A freshly loaded page asks for a baseline, so history never replays and never plays the sound; each new notification plays `static/audio/chime.wav` at most once per batch and never overlapping. The sound switch and the test button are on the Settings page. Browsers allow sound only after you have interacted with the page; if the test is silent, click or type on the page once and press Test again.

## Crash 100 and Vol_20: what was audited (and what was not)

The central specification service (`trademate/instruments.py`) already separates decimal precision, platform point, tick size, the strategy unit and money per unit, and marks a spec UNVERIFIED when any needed broker value is missing or contradictory. Audit findings:

* **Symbol resolution: one real defect found and fixed.** The asset-class matcher recognised `Vol 20` but **not `Vol_20`** (underscore), so the default main instrument was classified `unknown` instead of `synthetic`. That affected its default pip/point unit and the session panel. `Vol_20`, `Vol20`, `Volatility 20 Index`, `Crash 100 Index`, `Crash 1000 Index` and `EURUSD` are now covered by tests (`tests/test_more_backend_features.py`).
* **Tick size / tick value / contract size / volume limits and steps / account-currency conversion:** read from the broker for the selected symbol. The money calculator and the lot adjustment use only those values (tick values are already in account currency, so there is no FX table to go stale). If any is missing the calculation says it cannot be trusted instead of showing a number.
* **Crash 100 convention:** the project holds a user-confirmed convention (1.00 lot = 1.00 USD per 1.00 price unit) that is trusted only if the broker's own contract reproduces it.
* **Profit/loss, risk sizing, Autopilot compatibility:** they go through the same spec, the same Risk Engine and the same pipeline as every other instrument; Vol_20 and the other instruments were not changed.
* **What the automated tests use:** clearly labelled FIXTURES ("Vol_20 (FIXTURE)", "Crash 100 Index (FIXTURE)") that exercise volume rules, money from tick data, missing metadata and currency. **They are not your broker's real numbers.** With a live MT5 connection, open Settings, select the instrument and read the calculator: it either shows figures from your broker or says why it cannot.
