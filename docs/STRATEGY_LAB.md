# Command Centre

The Command Centre is the dedicated workspace for TradeMate's read-only multi-timeframe Strategy Engine. It is available from the sidebar at
**Command Centre** (`#strategylab`). Its capability registry is `trademate/strategylab.py`, exposed by `GET /api/strategylab/status`.

## What works

When the configured MT5 DEMO bridge is connected, the Command Centre can:

* Search the connected broker's instrument catalogue and validate a selected symbol, or open **Saved in MT5** to choose directly from instruments
  currently reported in the terminal's Market Watch.
* Run the existing Strategy Engine against live native M5, M15, H1 and H4 candles.
* Show the returned BUY, SELL, WATCH or WAIT decision, market-regime evidence, discovered setup families, timeframe context, structural levels, and invalidation rationale.
* Store an optional preferred name in this browser's `localStorage` and use it in the greeting.
* Run one background, read-only Watchdog for the selected instrument. It reevaluates the Strategy Engine every 60 seconds, tracks candidate
  state transitions and stale/disconnected conditions, and records qualified trade-alert events only when a confirmed BUY/SELL setup has
  complete entry, stop and first-target levels and a new signature.
* Opt in to browser desktop notifications and a generated sound for newly qualified setups. Browser notification permission is requested only after
  an explicit click; sound, volume and alert preferences are stored in this browser.

Analysis is read-only. The page does not size a position, approve risk, or send an order. A BUY/SELL is a strategy proposal, not a prediction or
guarantee. See [MULTI_TIMEFRAME_STRATEGY.md](./MULTI_TIMEFRAME_STRATEGY.md) for data quality, closed-candle policy, and qualification rules.
Candidate discovery does not imply execution eligibility; only the current M15-zone/M5-trigger route can qualify, and every strategy gate
remains mandatory.

## Honest capability states

The page renders capability states from `trademate/strategylab.py`; it does not infer that a disabled or disconnected feature works. Instrument
search, analysis, timeframe context, decision and setup display become available only while the broker reports a valid connection. If status cannot
be read, the page displays an unavailable state and an explanatory message.

| Capability | Current state |
|---|---|
| Broker connection | Live state from the configured MT5 bridge |
| Instrument selector and ANALYSE | Available when connected; otherwise disconnected |
| Multi-timeframe context, decision and setup details | Available when connected; based on the existing Strategy Engine response |
| Continuous Watchdog monitoring | Available when connected; one instrument per TradeMate process, 60-second interval |
| Setup notifications and sound | Opt-in; browser permission, page polling and browser audio support required |
| Trade execution from the Command Centre | Not implemented; use existing TradeMate trade controls and safeguards |
| Account Centre | Read-only display of identity and figures from the connected MT5 snapshot |
| Account login/switching | Not supported by the current bridge; use the MT5 terminal |
| Per-account risk profiles | Not implemented; current risk rules are one local configuration |
| Autopilot | Not implemented; TradeMate does not place orders automatically |

## Account Centre

Open **Account Centre** from the sidebar. It reuses the broker data already polled from `/api/broker` and displays the login, broker, server,
environment, currency, leverage, balance, equity, free margin, open P/L and snapshot age only while the adapter reports a fresh `CONNECTED` account.
On disconnection or unsupported account state it clears those values rather than showing stale identity.

This is a single-terminal integration. The EA bridge reports the account already logged in to MT5 and exposes no login/switch command. TradeMate
therefore has no Add Account, saved-profile selector, in-app connect/disconnect, or account-switch operation. A locally stored account label would
not authenticate the broker or change the terminal, so no fake login or selector is provided. Manage terminal authentication in MT5; Account Centre
will display the new identity only after a fresh supported snapshot arrives. The app does not request or store passwords.

**Switching safety:** commands sent to the bridge are not currently pinned to the broker login. Do not change the connected MT5 login while a
TradeMate command is pending or has an unknown outcome; the bridge could otherwise process a queued command on the terminal's newly active account.
This is a known limitation, not an in-app switching workflow. The adapter rejects non-DEMO accounts and checks the configured server, but does not
enable LIVE execution. Risk rules remain one local configuration and are not isolated by account. Account-specific risk permissions and Autopilot
are not implemented.

The welcome section uses the existing preferred display name and local clock. It keeps the greeting and a short market prompt above the existing
market selector; analysis/status tiles and numbered workflow steps are intentionally omitted. The market selector, Saved in MT5 dropdown, ANALYSE
button and Watchdog controls retain their existing IDs and handlers.

The Watchdog runs in the TradeMate process and is not persisted across a server restart. Candidate-state transitions are held in memory and
deduplicated while the worker runs. Qualified setup signatures are also deduplicated by the worker and in browser-local storage for alerts;
no browser alert is delivered if the Command Centre page is closed or unavailable when an event occurs. A candidate disappearing from fresh
analysis is reported as no longer observed, not assumed invalidated, because the current structure API does not expose a stable identity for
each recently invalidated zone.

## Greeting and preferences

The greeting uses the computer's local clock. An optional preferred name is saved per browser under `trademate.userDisplayName`; it is not synced
between browsers or devices. Clearing or blocking browser storage prevents that preference from persisting. The existing welcome-back behavior also
uses browser-local storage.

## Development and tests

Run TradeMate from the project folder with `./venv/bin/python app.py`. The page and its APIs are served by Flask. Relevant automated tests:

* `tests/test_strategylab.py` and `tests/test_watchdog.py` — capability registry, status routes, safe rendering, monitor lifecycle and alert deduplication.
* `tests/test_labgreeting_js.py` and `tests/labgreeting_test.js` — greeting and browser preference behavior.
* `tests/test_mtf_timeframes.py`, `tests/test_mtf_strategy.py`, `tests/test_mtf_api.py` — native timeframe data, strategy decisions and API.
* `tests/test_browser_strategylab.py` — browser workflow when Playwright/Chromium are installed.

The registry and tests must be updated together when a capability is implemented. Do not mark a feature Available until it works end-to-end.
