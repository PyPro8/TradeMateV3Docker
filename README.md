# TradeMate

## Futuristic UI redesign and Target Monitor

Presentation only. Strategies, Risk Engine, position sizing, execution, Autopilot, Watchdog, the MT5 adapter, `TradeMateBridge.mq5`, the file bridge and
every API are unchanged; the only new read paths are existing endpoints.

**Design system** (`static/css/style.css` + `static/css/design.css`, tokens as CSS variables, `data-theme="dark|light"`, the saved theme preference is kept):
navy / indigo / electric-blue dark theme, and a separately designed light theme (white and pale-grey surfaces, dark text, soft borders), not an inversion.
Components: buttons (`.btn`, `.btn-primary`, a visually distinct emergency control), cards and panels (`.panel`, `.kpi`, `.strat-card`), status chips
(`.tc-chip[data-tone]`), inputs / selects, tables (`.data` in `.table-scroll`), tabs / segmented controls, the "i" help button, progress and the gauge.
Touch targets are at least 40px of hit area on phones; motion respects `prefers-reduced-motion`; nothing animates continuously.

**Dashboard order:** account overview (equity and balance, floating P/L, free margin, open positions, account currency and DEMO/LIVE) -> **Target Monitor**
-> Trading status (MT5, trading mode, Autopilot, Watchdog, selected strategy, instrument, execution timeframe, Rapid Fire, Risk Engine, execution,
most recent order attempt) -> results (today, realised, used margin, closed trades, win rate) -> performance charts (per-trade P/L, win/loss split, recent trades).

**Target Monitor** (`static/js/targetmonitor.js`, `#target-monitor`): a semicircular instrument-cluster gauge with an electric-blue to purple arc, scale and needle.
It is read-only: it only calls `GET /api/autopilot/status` and never starts, stops or changes anything. Session profit is Autopilot's `net_pnl`, the target is its
configured profit target (`value` when the stop condition is "Profit target"), the currency is the account's as MT5 reports it (never assumed). Account balance is never used.
It says so in words, instead of drawing a value, when: no session or no profit target is configured (another stop condition), the target is zero or invalid,
session profit is unavailable, the broker is disconnected, data is stale (over 20 s) or unreadable, Autopilot is paused / stopped / emergency-stopped, or the target is reached.
Negative profit keeps the arc empty and is labelled "BELOW START". The decision logic is a pure function (`model()`), tested in Node.

**Navigation:** one router (`static/js/router.js`); sidebar on desktop, a drawer on phones; every route id and the browser history are preserved.
**Chart:** all existing features kept; on phones the timeframe row and the tool row are separate, touch-sized rows and the trade panel sits below the chart.

Not changed because the data does not exist: a "profit/loss history" beyond the last 30 days of MT5 deals, and any per-strategy performance.

## TradeMate Vision (UI overhaul + RAPID FIRE)

* One router (`static/js/router.js`) owns page navigation: every sidebar item opens its page, the active item follows, refresh / back / forward
  and odd addresses (`#CHART`, `#chart/`, unknown) work, and the mobile drawer closes on every tap. The Dashboard no longer carries a second chart.
* Dashboard rebuilt on real data only (balance, equity, margins, floating / realised / today P&L, positions, closed trades, win rate, MT5,
  active strategy, Rapid Fire, Autopilot, Risk Engine). Anything MT5 cannot supply shows `---` with the reason.
* Strategy page is a hub of six strategies; the "i" help opens by tap. New design layer: `static/css/design.css` (light and dark themes kept).
* New independent strategy **RAPID FIRE** with optional **Anti-Twin**: see `docs/RAPID_FIRE.md`.
* **v5:** Rapid Fire money target (profit per fire to TP distance from MT5 tick data), entry/TP/emergency chart lines, one shared ON/OFF **Anti-Twin** rule for Candle Momentum, Rapid Fire and Bull/Bear (`docs/ANTI_TWIN.md`), a personal Trade Journal notepad (`trademate/trade_journal.py`, optional import of Rapid Fire trades that never overwrites your notes), and a single-row scrolling chart toolbar capsule.

## CompleteNov12: what this version is

**Start it with one command:** `./launch.sh` (then open http://127.0.0.1:5000). `./launch.sh --check` only runs the checks. See "Run" below and `docs/CONFIGURATION.md`.

* **Bull/Bear v3 (structure-gated, with optional Anti-Twin), configurable Market Structure risk-to-reward and CHoCH target distance:** see `docs/BULL_BEAR.md` and `docs/STRATEGIES.md`.
* **Exactly five strategies:** Candle Momentum, CHoCH Trading, Market Structure Trading, Bull/Bear Trading, Trendline Trading, each with a hover / tap explanation (`docs/STRATEGIES.md`).
* **Candle Momentum is unchanged** and trades on the timeframe **you** choose (M1, M2, M5, M15, M30, H1, H4); the choice drives data, analysis, signal, risk, order and tracking, and never silently becomes another timeframe (`docs/CANDLE_MOMENTUM.md`).
* **Central Risk Engine** for every route: local-midnight daily loss with a 3 % limit and kept history, a configurable consecutive-loss limit, an emergency lock, and intelligent lot reduction after "not enough money" (`docs/RISK_ENGINE.md`).
* **Settings page:** Command Centre settings with clear scopes (defaults Vol_20, 0.02 lots, target 25, stop 100), a stop/target money calculator and reverse lot calculator using the broker's own tick data, execution and safety switches (including "Allow multiple trades per candle"), the notification chime, and a safe **Hard Reset** (backs up first; never touches history or risk records).
* **Autopilot controls:** Pause, Stop and Emergency Stop are three distinct states; Emergency Stop blocks every new order from every route and does not close positions unless you switch that on.
* **Chart:** Maximize works, the toolbar adapts to the trade panel, a configurable moving average (display only), and a free manual line tool (draw, drag either end, move, delete) kept separate from automatic trendlines.
* **Notifications and Trade History** update on their own, each notification once, with a real chime file (`static/audio/chime.wav`).
* **Trading sessions** and a grouped instrument list on the Dashboard; the AI investigation and its decision are in `docs/AI_ADVISORY.md`; the MT5 bridge in `docs/MT5_BRIDGE.md`; tests in `TESTING.md`.

**Not verified:** nothing in this version has been run against a live MT5 account. The automated tests use a simulated bridge and clearly labelled fixtures. Older notes of earlier sessions are archived in `docs/HISTORY.md`.

Documents: `README.md` (this file), `TESTING.md`, `docs/CONFIGURATION.md`, `docs/RISK_ENGINE.md`, `docs/STRATEGIES.md`, `docs/CANDLE_MOMENTUM.md`, `docs/AUTOPILOT_AND_STRATEGIES.md`, `docs/MT5_BRIDGE.md`, `docs/DAILY_LOSS_AND_CLOCKS.md`, `docs/STOP_LOSS_CALCULATOR.md`, `docs/AI_ADVISORY.md`, plus the market-structure, FVG, order-block and pipeline documents in `docs/`.

---

## Latest checkpoint: two strategies (H1 + M5) and the redesigned Command Centre

TradeMate now has exactly two selectable strategies, both reading **only H1 (context) and M5 (setup and confirmation)**:

* **CHoCH Trading**: a confirmed M5 change of character, H1 as context (informs, never vetoes; a counter-H1 CHoCH just needs a higher reward-to-risk).
* **Trend Continuation**: a confirmed H1 trend (higher highs and higher lows, or the mirror), an M5 pullback, then a closed M5 candle showing the trend resuming.

In plain English: the hourly chart says what the market has been doing; the five-minute chart says whether now is a sensible moment to act. They are independent, they propose,
the Risk Engine decides, and nothing is sent without your explicit confirmation. Neither strategy has been back-tested; no profitability is claimed. Full rules, the meaning of
BUY / SELL / WATCH / WAIT, selection and persistence, what happens to pending proposals when you switch, Watchdog and Autopilot: [`docs/STRATEGIES.md`](docs/STRATEGIES.md).

**Command Centre** (redesigned, same page and routes): header with real MT5/account status, market picker, **strategy dropdown** (server-validated, saved in `data/strategy_selection.json`, recovered after a restart),
**ANALYSE MARKET**, a large decision card (BUY / SELL / WATCH / WAIT with the backend's own reasons, four stage indicators, expandable evidence), H1 and M5 context cards, **REVIEW TRADE**
(Risk Engine sizing, sends nothing) then a deliberate two-click **CONFIRM & EXECUTE**, a server-side **WATCHDOG** toggle, an honest **AUTOPILOT** panel (not built; shows what it would need), and recent activity.
Switching strategy cancels pending approvals, stops the Watchdog and clears the page.

* New backend: `trademate/strategies/` (registry, `choch.py`, `trend.py`, selection store, service), routes `/api/strategy/catalogue|select|analyse`, `/api/autopilot/status`.
* Verified in simulation and in headless Chromium against the fake bridge. **Not verified:** real MT5, DEMO or LIVE trading, profitability. Autopilot is **not built**; unattended trading is not possible.
* Defects found and fixed while doing this: the Command Centre status renderer threw on a missing element (every capability showed "Unavailable"), the greeting had lost its "Welcome to Command Centre." sentence its tests specify,
  and a browser test left the shared stub broker disconnected for the tests after it.
* Report: [`IMPLEMENTATION_REPORT_STRATEGIES_COMMAND_CENTRE.md`](IMPLEMENTATION_REPORT_STRATEGIES_COMMAND_CENTRE.md).

## Latest Strategy-to-Risk handoff checkpoint

See [`STRATEGY_TO_RISK_HANDOFF_MILESTONE.md`](STRATEGY_TO_RISK_HANDOFF_MILESTONE.md) for the new read-only endpoint `GET /api/strategy/mtf/trade-review?symbol=SYMBOL`, which passes only qualified BUY/SELL proposals through the existing Risk Engine for preview. It sends no orders; actual execution still requires a fresh review through the existing `/api/order` gate. Flask endpoint and MT5 integration tests remain unverified in the current environment.

## Latest checkpoint: Strategy -> Risk -> Execution pipeline (controlled mode)

A qualified BUY/SELL from the Strategy Engine can now travel through the Risk Engine and the existing MT5 execution pathway in two explicit steps:
`POST /api/pipeline/propose` (strategy, risk, execution eligibility; **sends nothing**) and `POST /api/pipeline/execute` (`confirm: true`; everything is re-validated, then
the order goes through the same `adapter.place_order` as manual trades). Before this checkpoint nothing connected the Strategy Engine to the Risk Engine, and the proposal object
could never have passed it (no risk percentage). Full audit, defects, contract, failure handling and what is still unverified:
[`docs/STRATEGY_TO_EXECUTION_PIPELINE.md`](docs/STRATEGY_TO_EXECUTION_PIPELINE.md); change report: [`IMPLEMENTATION_REPORT_PIPELINE.md`](IMPLEMENTATION_REPORT_PIPELINE.md).

* **Verified only against the simulated bridge** (`tests/test_pipeline.py`, no real order possible). **No real MT5 DEMO verification has been done yet**; the exact next check is in the docs.
* Autopilot is still **not built**. Unattended LIVE trading is not enabled and must stay disabled. There is no overall-drawdown limit yet (daily loss, per-trade risk, total active risk and position count exist).
* New optional `[pipeline]` settings in `config/settings.toml` (approval lifetime, entry drift and spread limits). They never loosen the Risk Engine.
* Run the new tests: `python3 -m unittest tests.test_pipeline -v`. The whole suite: `bash tests/run_tests.sh` (see `TESTING.md` for the current counts and the known pre-existing failures).

## Latest Command Centre Watchdog checkpoint

See [`COMMAND_CENTRE_WATCHDOG_CHOCH_ALERTS.md`](COMMAND_CENTRE_WATCHDOG_CHOCH_ALERTS.md) for the Watchdog checkbox correction, confirmed CHoCH alert events, safety gates, and verification results.


## Latest Command Centre continuation checkpoint

The Command Centre status poll must not overwrite an in-progress analysis status or erase the specific analysis response merely because the independent broker-status endpoint reports unavailable. See `COMMAND_CENTRE_CONTINUATION1_FIX.md` for the change, request path, verification results and remaining live-MT5 limitation. This UI correction does **not** bypass candle freshness checks or prove that the broker feed is current.

## IMPORTANT before you run this version

1. **Recompile the EA once.** `mt5/TradeMateBridge.mq5` is now v0.4 (adds the read-only `history` command and the
   tick values used for the TP/SL money display). In MetaEditor press F7, then remove TradeMateBridge from its chart
   and attach it again. Without this, orders, charts and everything else keep working, but Trade History shows
   "EA older than v0.4" and TP/SL show pips only.
2. **Use the whole folder.** This archive is the complete project. Do not copy single files out of it into another
   copy: the app needs `trademate/` (config, errors, journal, broker/...) and `tests/test_project_integrity.py`
   fails loudly if anything is missing.

## Execution safety (how an order is tracked)

Every BUY/SELL/CLOSE/SL-TP change is written to a local journal (`data/trademate.sqlite3`) BEFORE it is sent, and
updated as it moves: command created -> received by the bridge -> answered -> confirmed in the account. If the answer
is lost or TradeMate hits an internal error after sending, the order is **UNKNOWN**, never "failed":
TradeMate checks the real account (`Check MT5 again`), refuses to send the identical order again until it is
resolved, and shows an amber bar until it is. The exception is always logged in full in `logs/trademate.log`.

**Delete Trading History** (History page) deletes only this local journal. It never contacts MT5, never closes or
changes a trade, and never touches the broker's account history (use "Show all MT5 history" to see it again).

TradeMate is a Python-based trading engine that will eventually communicate with
MetaTrader 5 (MT5), connected to a Headway account. It is being built one small
piece at a time, like a jigsaw puzzle.

## What is new (daily-loss reset, clocks, candle countdown)

* **Today's Loss / Remaining Daily Loss now reset at the BROKER's midnight** (MT5 server time), never at your computer's. If the broker's clock cannot be
  determined, or any loss data is missing, they show **unknown** and new trades are refused: missing data is never treated as zero. Root cause, behaviour and
  limits: `docs/DAILY_LOSS_AND_CLOCKS.md`. Optional setting: `[risk] broker_timezone`.
* **Two clocks** in the top bar: LOCAL and NEW YORK (EST/EDT automatic). Optional setting: `[ui] timezone`.
* **Candle countdown** ("closes in mm:ss") in every chart toolbar.
* Where: counters on the **Strategy** page (Account risk card); clocks in the top bar; countdown in the **Chart** and **Dashboard** chart toolbars.

## Current stage

**Phase 4 (history of this file; the application now also includes the Phase 5 Risk Engine, Phase 6 execution and Phase 7 analysis, below): chart trading on the DEMO account.** TradeMate is a Flask web app; the browser is the UI.

- Phase 1-3: config, logging, UI shell, MT5 read connection (account, positions, Market Watch prices).
- Phase 4 (this version):
  - **Current Market** has a watchlist. Use the up/down arrows (or the arrow keys) to cycle symbols; the chart follows.
  - **Chart** with real MT5 candles, lot size and optional SL/TP. Orders are placed with the **BUY / SELL buttons in the Trade Panel** (the oversized buttons that used to sit under the chart were removed in Task 2).
  - **Entry / SL / TP lines on the chart. Drag SL or TP**, check the proposed change, press *Apply at broker*.
  - **Open Trades** with live P/L (**blue = profit, red = loss, grey = flat**), editable SL/TP and CLOSE.
  - Every write shows *Sending... -> Executed ✓ / Failed ✗ <broker reason>*. Success is shown only after
    the broker's own account state confirms it. The broker is the source of truth, not the page.

Manual only: TradeMate never trades by itself, and it refuses any account that is not a DEMO account.

## Requirements

- Python 3.11 or newer
- **Flask** (installed from `requirements.txt` in the step below). You must install it before running.

## Install

From the project folder:

```
python -m venv .venv
```

Activate it:

- Windows (PowerShell): `.venv\Scripts\Activate.ps1`
- Windows (cmd): `.venv\Scripts\activate.bat`
- macOS / Linux: `source .venv/bin/activate`

Install dependencies:

```
pip install -r requirements.txt
```

## Connect TradeMate to MT5 (Ubuntu + Wine)

TradeMate cannot import MT5 directly (the `MetaTrader5` Python package is Windows-only). Instead an Expert
Advisor inside MT5, `mt5/TradeMateBridge.mq5` (v0.5, see the notice at the top), does two things:

- **Read:** writes a snapshot file about once a second (account, positions, symbols); TradeMate reads it.
- **Write:** looks for small command files that TradeMate drops in the same folder (`trademate_cmd_*.txt`),
  runs them with the MT5 trade API (`CTrade`) and answers with `trademate_res_*.json` containing the broker's
  real return code. Commands expire after a few seconds, so nothing can fire late. The EA refuses to trade on
  a non-DEMO account and only acts on commands you trigger by clicking.

One-time setup:

1. In MT5: **File > Open Data Folder**, open `MQL5/Experts`, and copy `mt5/TradeMateBridge.mq5` there.
2. In MT5 press **F4** (MetaEditor), open the file and press **F7** to compile. It should say 0 errors.
3. In MT5, open a chart (any symbol), then drag **TradeMateBridge** from the Navigator onto it.
   In the EA's **Common** tab tick **Allow Algo Trading**. The Experts tab should say
   "TradeMateBridge 0.5.0 started. Trading ENABLED (DEMO accounts only)". Make sure **Algo Trading** is switched
   on in the MT5 toolbar. **If you used v0.1, recompile and re-attach the EA: the snapshot format changed.**
4. Check the connection from a terminal:

```
python -m trademate.broker
```

It prints the connection state and, when connected, the account and prices it sees. Compare them with MT5.

TradeMate looks for the snapshot in your Wine prefix under
`drive_c/users/<you>/AppData/Roaming/MetaQuotes/Terminal/Common/Files`. If it cannot find it, find the folder with
`find ~ -path "*MetaQuotes/Terminal/Common/Files" 2>/dev/null` and put it in `config/settings.toml` as `data_dir`.

Connection states: **CONNECTED** (fresh data from a demo account), **CONNECTING** (bridge just started),
**DISCONNECTED** (MT5 or the EA is not running, or MT5 is offline from the broker), **ERROR** (unreadable
snapshot, wrong version, or a non-demo account). TradeMate keeps running in every state.

No passwords are needed or stored: MT5 is already logged in.

## Chart workspace (Phase 6)

- The Chart page fills the window; the Dashboard chart sits right under the account cards and is viewport-sized.
- **Resize:** drag the grip under the chart (height) or the grip between chart and trade panel (width). Double-click a grip to reset. Sizes are remembered.
- **Maximize:** toolbar button; **Esc** returns. The **Panel** button hides/shows the trade panel. Lots and BUY/SELL stay on the chart.
- **Pan/zoom:** drag to pan; +/- or Ctrl+wheel (plain wheel on the Chart page) to zoom; pinch on touch; double-click or Reset to restore.
- **Quote lines:** dotted amber = ASK, dotted lavender = BID (real MT5 prices), shaded spread. Entry is a long-dash line, SL red, TP blue.
- **Pending orders:** your broker's real pending orders for the chart's symbol are drawn as **magenta dash-dot lines at their entry price**
  with a label such as `▲ BUY LIMIT 0.10 @ 1.08350` (a second line shows `pending`, SL, TP and the ticket) and a price tag on the right axis.
  They are read-only (not draggable; edit or cancel them in the Pending Orders table), come from the same broker data as that table, and
  disappear when the order is cancelled or filled (a fill then shows as an open position). An order outside the visible price range gets an
  edge marker instead of stretching the chart. Drawing them never sends anything to the broker.
- **Crosshair:** exact price (right), exact time (bottom), candle OHLC in the header.
- **Themes (two independent controls):** the **top-bar half-circle switch** and **Settings > Theme (whole application)** change the whole
  application. The **chart's moon button** and **Settings > Chart theme (chart only)** change only the chart (candles, grid, panel and toolbar);
  the rest of the application keeps its theme. The chart theme defaults to **Same as the application**, so nothing changes until you pin it
  with the moon button (or the select); choose *Same as the application* in Settings to release it again. Both are saved on this computer.

## MT5 / TradeMate Startup & Recovery

Order execution depends on MT5 allowing the `TradeMateBridge` EA to trade. After MT5 or TradeMate restarts,
TradeMate can report **"MT5 is not allowing the bridge to trade."** even though Algo Trading looks ON, the EA has
`AllowTrading = true` and "Allow Algo Trading" is ticked. Re-attaching the EA fixes it (verified on the Headway demo).

Normal startup:

1. Open MT5.
2. Make sure **Algo Trading** is ON (toolbar button).
3. Make sure `TradeMateBridge` is attached to **one** MT5 chart (smiley face, top-right of the chart).
4. Start TradeMate (`python app.py`).
5. Place a small demo Buy/Sell and check the position appears in MT5's Trade tab.

If TradeMate says MT5 is not allowing the bridge to trade:

1. Remove `TradeMateBridge` from the chart (right-click chart -> Expert Advisors -> Remove).
2. Re-attach it: Navigator -> Expert Advisors -> drag `TradeMateBridge` onto the chart.
3. EA Properties -> **Common** tab: tick **Allow Algo Trading**.
4. EA Properties -> **Inputs** tab: confirm `AllowTrading = true`.
5. Click **OK**.
6. Reconnect / restart TradeMate.
7. Place a small demo Buy/Sell to verify execution, and confirm the position appears in MT5.

The EA only needs to be attached to **one chart**. It trades any symbol (it selects the symbol itself), so you do
**not** attach it to every chart or symbol.

## Any broker instrument (Phase 5)

TradeMate is not limited to a preset list or to MT5's Market Watch.

- **Search:** type in *Search broker symbols* (Dashboard, Current Market). The bridge searches the broker's
  **whole** catalogue by name, description and category (e.g. "gold", "oil", "US30", "crypto"). Symbol names differ
  between brokers (`XAUUSD`, `GOLD`, `XAUUSD.r`...), so search rather than guess. Results flag instruments that are
  not fully tradable (long only, close only, disabled).
- **Add:** click a result. The bridge selects it in MT5 on demand and returns its real rules (digits, point,
  spread, lot min/max/step, stop level, contract size, margin currency, category). It joins your watchlist and the
  arrows/chart/trade panel work on it. Lot size follows that symbol's own step.
- **Efficient:** the snapshot stays small. It carries (1) symbols with open positions, (2) your watchlist
  (`trademate_watch.txt`, written by TradeMate), (3) up to `MaxSymbols` (30) Market Watch symbols. The full catalogue is
  only searched on request, so brokers with thousands of instruments are fine. `MaxSymbols` now only limits the
  generic Market Watch part, not what you can add or trade.
- **Where the old limit was:** the EA only exported 30 Market Watch symbols and TradeMate refused any symbol that was
  not in that export, even though the EA could select it. TradeMate now asks MT5 on demand. Order execution itself is unchanged.
- **Upgrade the EA to v0.3:** recompile (F7) and re-attach (see recovery steps above). Older EAs still trade but answer
  searches with "older than v0.3".
- CLI check: `python -m trademate.broker.find_symbols gold`, and `python -m trademate.broker.live_test XAUUSD`
  runs the full demo BUY/SELL/TP/SL/close cycle on that symbol.

## Test real execution (TradeMate -> MT5 -> Headway)

The automated tests use a stand-in for MT5, so they cannot prove your broker accepts orders. This does:

```
python -m trademate.broker.live_test EURUSD
```

It asks you to type YES, then on the DEMO account: BUY (minimum lot) -> set TP -> set SL -> close, then
SELL -> close, checking the broker's account state after every step. Watch MT5's Trade tab while it runs.
Then repeat by hand from the chart (steps in `TESTING.md`).

Guard rails live in `[trading]` in `config/settings.toml` (`enabled`, `max_volume`, `deviation_points`).

## Run

**The one documented command: `./launch.sh`.** It finds the project folder itself, uses `venv/` or `.venv/` (creating `.venv/` only if neither exists), checks Python 3.11+, the dependencies and `config/settings.toml`, checks the port is free, and starts the app. It never edits your configuration or deletes data. Options: `--check`, `--no-browser`, `--mt5` (Linux + Wine: install/compile the EA and start MT5 first).

Manual start (equivalent): `python app.py`.

```
python app.py
```

Then open <http://127.0.0.1:5000> in your browser. Press Ctrl+C in the terminal to stop.
The server only listens on your own computer. Host and port are in `config/settings.toml` under `[ui]`.

The Phase 1 check still works too: `python -m trademate` prints the foundation confirmation.

## Test

```
bash tests/run_tests.sh               # everything: Python tests + Node checks (real-browser tests skip themselves without Playwright/Chromium)
bash tests/run_tests.sh --no-browser  # without the browser tests
python -m unittest tests.test_daily_loss_reset tests.test_clock_js -v     # only the daily-loss / clock tests
node tests/clock_test.js              # only the clock/countdown JavaScript checks
python -m unittest tests.test_browser_clocks -v   # clocks and countdown in real Chromium (needs: pip install playwright && playwright install chromium)
```

The suite is safe: it uses a fake bridge and read-only stub brokers and never sends an order to MT5. Tests that need a real MT5 are not in the suite.
`python -m unittest discover -s tests -v` also works but takes several minutes with the browser tests.

## Configuration

Settings live in `config/settings.toml`. Environment variables override the file:

| Variable | Purpose | Values |
|---|---|---|
| `TRADEMATE_MODE` | Run mode | `development`, `demo`, `live` |
| `TRADEMATE_LOG_LEVEL` | Log verbosity | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `TRADEMATE_CONFIG` | Alternate settings file path | any path |
| `TRADEMATE_TIMEZONE` | The Local clock's timezone (overrides `[ui] timezone`) | IANA name, e.g. `Asia/Manila` |

Timezone settings: `[risk] broker_timezone` (fallback for the broker's trading day, see `docs/DAILY_LOSS_AND_CLOCKS.md`) and `[ui] timezone` (Local clock). Both default to empty.

Never put passwords or secrets in `settings.toml`.

## Project layout

```
TradeMate/
  app.py                   Flask app: config, logging, pages, read-only /api/broker
  requirements.txt         Flask (+ tzdata on Windows)
  templates/index.html     The interface (all pages are sections of this one file)
  static/css/style.css     Styling. Helixa blue is the --primary variable
  static/js/app.js         Page switching, mobile menu, read-only broker display
  static/js/mtf.js         Strategy page: multi-timeframe analysis panel (read-only)
  config/settings.toml     Settings file
  mt5/TradeMateBridge.mq5  Read-only Expert Advisor you install inside MT5
  trademate/
    broker/base.py         BrokerAdapter interface and data models
    broker/mt5_adapter.py  Reads the MT5 snapshot, decides the connection state
    broker/__main__.py     `python -m trademate.broker` connection check
    brokertime.py          Broker (MT5 server) clock: trading-day boundary and candle-timeframe lengths
    risk/account_state.py  Builds the daily-loss state from MT5 data (unknown, never zero, when data is missing)
  static/js/clocks.js      Two clocks (local + Eastern Time) and the candle countdown
    fvg/engine.py          Fair Value Gap detection (Phase 7C, pure analysis; shown on the chart and the Analysis page)
    orderblocks/engine.py  Order Block detection (Phase 7C, pure analysis; shown on the chart and the Analysis page)
    strategy/              MTF Strategy Engine: data quality, regime estimates, setup candidates, qualification and read-only Watchdog
    config.py, logging_setup.py, errors.py   Foundation
  tests/                   Unit tests
```

## NOT built yet

(Earlier versions of this list said order placement, the chart, risk management and a Strategy Engine were missing. Manual DEMO trading, the chart, the
Risk Engine, the analysis engines and, since Phase 8, a **multi-timeframe Strategy Engine that proposes setups** exist: see below.)

- **Automatic trading / Autopilot.** TradeMate only trades when you click. The Strategy Engine proposes; it never sizes or sends an order.
- Validation of the Strategy Engine's rules on a live or demo broker, and backtesting (the Backtest page is a placeholder)
- Multi-account profiles/sign-in (MT5 terminal authentication, one active terminal connection)

## Phase 7A: market structure

Optional chart overlay (HH/HL/LH/LL, BOS, CHoCH) from a deterministic, lookahead-free engine, plus candle colour presets in
Settings, and an application-wide Light/Dark theme (top-bar switch, saved). Analysis only: it never trades. See `docs/MARKET_STRUCTURE.md`; run `tests/run_tests.sh`.

The shared structure engine uses confirmed swing highs/lows, directional continuation references for BOS, and protected HL/LH references
for CHoCH, with an origin-swing fallback after a CHoCH (see the audit section of `docs/MARKET_STRUCTURE.md`). Analysis and Strategy share one `structural_facts` function; a confirmed direction and its evidence quality are displayed separately.
The detector methodology, event audit fields, synthetic sequence regressions, and live diagnostic instructions are in
`docs/MARKET_STRUCTURE.md`; capture a live sequence with `python3 -m trademate.structure.trace candles.json`; Analysis and Strategy comparison guidance is in `docs/ANALYSIS.md` and
`docs/MULTI_TIMEFRAME_STRATEGY.md`.

## Phase 7A.1: Analysis, stop-loss calculator, Headway-Demo check, Full Test

* **Analysis** (sidebar): what the Market Structure Engine determined and why. Read-only, no signals. `docs/ANALYSIS.md`.
* **Two-way stop loss** (trade panel): risk amount to SL price, or SL price to risk, in your account currency, using the Risk Engine's own maths. Preview only. `docs/STOP_LOSS_CALCULATOR.md`.
* **Expected server `Headway-Demo`**: new orders are refused if MT5 is connected to anything else, or the server cannot be verified. Override: `TRADEMATE_EXPECTED_SERVER`.
* **Full Test** (sidebar > Developer, or http://127.0.0.1:5000/full-test): run the automated tests from the browser. `TESTING.md`.
* Run all tests from a terminal: `bash tests/run_tests.sh`.

## Phase 7C: Fair Value Gap (FVG) engine and chart overlay

`trademate/fvg/` finds three-candle Fair Value Gaps in a candle list. Bullish: candle 3's low is **strictly above** candle 1's high
(gap = candle 1 high .. candle 3 low). Bearish: candle 3's high is **strictly below** candle 1's low (gap = candle 3 high .. candle 1
low). Touching boundaries are not a gap. It is a geometric observation, not a prediction, quality rating or trade signal, and it
never trades, sizes or talks to the Risk Engine.

```python
from trademate.fvg import analyze_fvg, FVGConfig
res = analyze_fvg(candles, FVGConfig(last_candle_closed=False), symbol="EURUSD", timeframe="M5")   # candles: [time, o, h, l, c] rows
for g in res.gaps:  print(g.direction, g.lower, g.upper, g.third_time, g.confirmed)
```

Run its tests: `python3 -m unittest tests.test_fvg_engine -v`. Rules, data handling and limitations: `docs/FAIR_VALUE_GAPS.md`.

**Showing FVGs on the chart.** Open the **Chart** page (sidebar) or the chart on the Dashboard. In the chart toolbar, the **dashed-box
button** shows or hides fair value gaps (hover text: "Fair value gaps"). Blue zones are bullish gaps, purple zones are bearish gaps;
a solid outline is confirmed, a **dashed** fainter zone means the newest candle is still forming and the gap may change or vanish.
Each zone spans candle 1 to candle 3 between the engine's own boundaries (nothing is recalculated in the browser). They are hidden by
default so the chart stays readable. A gap is an observation about three candles, not a prediction, a quality rating or a trade signal,
and it is never connected to orders or position sizing.

**Overlay controls (each one independent).** In the same toolbar, next to the existing buttons: **Market structure**, **Liquidity**,
**Fair value gaps**, **Order blocks**, plus a **layers button** that hides all analysis overlays if any are shown, otherwise shows all of
them. The same switches are in **Settings > Candlestick Appearance** as four labelled checkboxes with **Show all** / **Hide all**. Any
combination works, including all four or none. Hiding an overlay only stops it being drawn: the analysis engines, the Analysis page
and the data the chart holds are unaffected (the chart still requests structure, FVG and order block data while they are hidden;
Liquidity keeps its Phase 7B behaviour of being requested only while its overlay is shown).

**Do the choices persist?** Yes, on the TradeMate machine: they are saved in `data/chart_settings.json` (with the colours and theme),
so they survive reloading the page and restarting TradeMate, and any browser that opens this TradeMate server sees the same choices.
They are not synced between different TradeMate installs. If saving fails the switch returns to what is actually saved.

Tests: `python3 -m unittest tests.test_fvg_api tests.test_fvg_js tests.test_browser_phase7c_fvg -v` (the last one needs Playwright
and Chromium and is reported as skipped without them). More in `TESTING.md`.

## Phase 7C: Order Blocks (engine, chart overlay and Analysis page)

`trademate/orderblocks/` finds order blocks from candles plus the Market Structure result. A **bullish** candidate is the last bearish
candle at or before a confirmed swing low (mirror for bearish); it becomes **confirmed** only when the structure engine reports a
BOS/CHoCH that breaks the swing high the leg started from *and* that swing low was the leg's origin; it is **invalidated** when price
closes through the zone (optionally: wicks through it), and **expires** when its reference level was replaced or broken by a different
leg. Confirmation times never use candles after the evidence. TradeMate has no volume or order-flow data, so a block is a
price-structure pattern, not evidence of institutional activity, and not a prediction or trade signal. It is never connected to orders or
sizing.

**Seeing order blocks on the chart.** Chart page (or the Dashboard chart) > the toolbar button with the **box and edge bar** icon (hover:
"Order blocks"), or Settings > Candlestick Appearance > *Show order blocks on the chart*. Hidden by default. **Teal-green = bullish, orange =
bearish.** A zone runs from the source candle to the newest candle between the engine's own upper and lower boundary (the source candle's
high and low). **Solid with an edge bar = confirmed; dashed and fainter = candidate** (not confirmed) or still-forming. Only live blocks
(candidate or confirmed) are drawn; invalidated and expired ones are listed on the Analysis page. Nothing is recalculated in the browser, and
if the engine cannot run (too few or invalid candles) nothing is drawn.

**Analysis page.** *Analysis* in the sidebar now has **Fair value gaps** and **Order blocks** panels with the engines' real results (zone,
source candle, status, a one-sentence reason, and *Show on chart* buttons), an explicit empty state when none exist, and the reason when an
engine could not run. The **Modules** list says *Available* only when the engine ran for that request, *Unavailable now: <reason>* when it
exists but could not run, and *Not available yet* for what this single-timeframe page does not provide (multi-timeframe and trade setups: these live on the Strategy page, Phase 8).

```python
from trademate.structure import StructureConfig, analyze_cached
from trademate.orderblocks import analyze_order_blocks
res = analyze_order_blocks(candles, analyze_cached(candles, StructureConfig(last_candle_closed=False)), symbol="EURUSD", timeframe="M5")
confirmed = [b for b in res.blocks if b.status == "confirmed" and not b.provisional]
```

Run its tests: `python3 -m unittest tests.test_orderblock_engine tests.test_ob_js tests.test_repair_api tests.test_browser_phase7c_repair -v`
(the last needs Playwright and Chromium; without them it is reported as skipped). Exact rules, how to consume the result, chart/Analysis data
flow and limitations: `docs/ORDER_BLOCKS.md`; chart and Analysis behaviour of all four repairs: `TESTING.md`.

## Task 2: simpler Analysis page, red / black candles, no oversized Buy / Sell buttons, zone and order overlays

**What changed and where to find it**

| Change | Where | How it works |
|---|---|---|
| **Plain-language Analysis** | sidebar **Analysis** | Five sections on top: *1 Market direction, 2 Liquidity, 3 Fair value gaps, 4 Order blocks, 5 Overall interpretation*. Everything the page used before is still there, collapsed under **Technical details**. Built by `trademate/analysis_summary.py` from the engines' own results; `GET /api/analysis` returns it as `summary`. If an engine cannot run, its section says **Unavailable** and why; nothing is invented. See `docs/ANALYSIS.md`. |
| **Candlestick palettes** | Settings > Candlestick Appearance | The original nine presets remain available; Arctic and Rose, Violet and Ember, and Silver and Flame are three additional non-green options that adapt to the effective chart theme. Custom colors remain unchanged. |
| **Buy / Sell buttons removed** | Chart page | The two oversized buttons under the chart are gone. The **lot size**, **+SL / +TP** and the **Trade Panel BUY / SELL** buttons remain, with the same Risk Engine check and confirmation. |
| **Chart theme** | Chart moon button / Settings > Chart theme | Changes only the chart; the application theme is untouched. A change is now also saved if you reload within a fraction of a second. |
| **FVG / Order Block overlays** | Chart overlay buttons | Zones are the engines' zones at the engines' prices for the shown symbol and timeframe. If the chart data stops refreshing the zones are hidden and the chart says **ZONES HIDDEN: CHART DATA IS NOT CURRENT**; if an engine cannot run it says **<engine> UNAVAILABLE: reason**. |
| **Pending orders on the chart** | Chart (magenta dash-dot lines) | Real broker pending orders at their entry prices. If the broker feed is not fresh they are hidden with **PENDING ORDERS HIDDEN: NO FRESH BROKER DATA**. Drawing never sends, changes or cancels an order. |

**FVG status on the Analysis page.** The FVG engine detects gaps but does not track fills. The Analysis page derives *Open / Partly filled / Filled* from the engine's zone and the candles after it formed (wicks count). Order block status is the engine's own.

**Run the tests:** `python3 -m unittest tests.test_task2_analysis_and_defaults tests.test_browser_task2 -v` (the browser file needs Playwright + Chromium; it uses a stub / fake broker only). The whole suite: `bash tests/run_tests.sh`. See `TESTING.md`.

**Known limitations.** Position of price relative to a zone uses the newest candle's close, not a live quote. The Analysis page analyses one timeframe at a time and evaluates no setups, so nothing on it is a trade signal. Multi-timeframe setup evaluation is a separate feature on the Strategy page (Phase 8, below). Nothing was tested against a live MT5 / broker.

## Multi-timeframe Strategy Engine (M5, M15, H1, H4)

The read-only Strategy Engine validates native closed candles, estimates overlapping market regimes, discovers evidence-backed setup
candidates, and returns **BUY, SELL, WATCH or WAIT** with evidence and reasons. Candidate discovery covers continuation, pullback, liquidity
sweep reaction, structural reversal, breakout/retest, range and countertrend hypotheses. The current executable qualification route remains
the stricter M15 zone-reaction plus closed M5 trigger policy; discovery families do not independently authorize trades.

Open sidebar **Strategy** and press **Analyse**, or use `GET /api/strategy/mtf?symbol=EURUSD`. The response includes per-timeframe structure,
freshness, regimes with qualitative certainty (not probabilities), candidate supporting/conflicting/missing evidence, invalidation and
qualification gates. If data is missing, context and meaningful candidates may remain visible, but the all-four-timeframe data gate prevents
BUY/SELL. No candle or price is fabricated. Entry is an analytical M5 close reference, not a broker fill.

The engine produces only analysis and a `TradeProposal` shape. It does not call the Risk Engine or place orders; order entry remains manual
and uses existing server-side Risk and Execution safeguards. The Watchdog tracks candidate state changes while the app runs and only
notifies on deduplicated qualified setups. It does not execute trades.

No historical strategy backtest or broker validation was run; no profitability or learning claim is made. Decision-time evidence is exposed
as a versioned validation snapshot, but it is not persisted or linked to broker fills/outcomes. See `docs/MULTI_TIMEFRAME_STRATEGY.md` for
definitions, exact thresholds, required qualification rules, data handling, Watchdog limitations and the outcome-validation gap.

Run relevant tests:

```
python -m unittest tests.test_strategy_intelligence tests.test_mtf_timeframes tests.test_mtf_strategy tests.test_mtf_api tests.test_watchdog -v
python -m unittest tests.test_browser_mtf tests.test_browser_strategylab -v
bash tests/run_tests.sh
```

## Command Centre

The **Command Centre** is the read-only cockpit for the existing multi-timeframe Strategy Engine. When the MT5 DEMO bridge is connected, it can search
and validate broker instruments, choose directly from the instruments reported in MT5 Market Watch using the **Saved in MT5** dropdown, run M5/M15/H1/H4
analysis, and display the actual decision, evidence, levels and invalidation. An optional single-symbol
Watchdog reevaluates every 60 seconds; browser desktop alerts and sound are opt-in and require the page to be open for alert delivery. Preferences are
stored in browser-local storage. The **Account Centre** page displays the fresh account identity and figures reported by MT5; it does not sign in,
store account profiles, or switch accounts. The bridge has no login/switch command and only reports one terminal's current identity; a saved label
would not authenticate or switch an account, so TradeMate does not offer a misleading profile selector. Do not change the connected terminal's
login while a TradeMate command is pending or has an unknown outcome: bridge commands are not pinned to a broker login. The adapter rejects
non-DEMO accounts. Risk settings are currently one local configuration, not per-account. The Command Centre greeting uses the existing preferred
display name setting and local clock; the welcome area contains no invented market status. The Command Centre does not send orders or enable Autopilot.
See `docs/STRATEGY_LAB.md` for capability states, limitations and tests.

Relevant tests: `python3 -m unittest tests.test_strategylab tests.test_watchdog tests.test_mtf_timeframes tests.test_mtf_strategy tests.test_mtf_api -v`.
The browser workflow is covered by `tests.test_browser_strategylab` when Playwright and Chromium are installed.

### Command Centre interaction when broker data is unavailable

The Command Centre instrument input, saved-market dropdown, and ANALYSE button remain interactive when broker status is disconnected or unavailable. This lets the user request an operation and see the backend's specific error instead of encountering disabled controls with a prohibited cursor. This is a UI usability change only: symbol validation, MT5 snapshot freshness, candle freshness, and setup qualification remain enforced by the backend. A disconnected or stale feed must not produce a confirmed setup.

## Setup discovery and Watchdog alerts

The current MTF Strategy Engine uses confirmed market structure and multi-timeframe context, a live M15 confirmed order block **or** an unfilled/partially filled FVG, a recent closed M5 zone reaction, a subsequent confirmed directional M5 BOS/CHoCH, and valid structural stop/target levels meeting the configured reward-to-risk requirement. Liquidity pools and sweep evidence are analyzed and can inform context/targets, but a liquidity sweep is not a mandatory gate for every current setup. The Watchdog is read-only and only alerts on qualified BUY/SELL results with valid trade geometry and reliable data. See [`SETUP_DISCOVERY_WATCHDOG_REPORT.md`](SETUP_DISCOVERY_WATCHDOG_REPORT.md) for the component map, notification behaviour, exact limitations, and this checkpoint's test status.

To receive desktop alerts, grant browser notification permission and enable the desktop-notification toggle in the Strategy Lab; enable sound separately. Browser/site permission, a running TradeMate server, a connected MT5 bridge and fresh data are required. The Watchdog monitors one symbol at a time and does not run while the host machine/server is stopped or asleep. Notifications do not imply that an order was placed or that a setup will be profitable.

## Candle Momentum v2 (rebuilt)
Candle Momentum is now a persistent engine that follows the forming candle tick by tick, enters the moment price crosses your trigger distance from the candle open, and measures the target from the
actual fill. The old hard-coded 2-pip "late" limit is gone; entry validity, slippage tolerance, signal expiry and quote freshness are separate configurable controls. One entry per candle, kept across
refreshes and restarts. It still goes through the Risk Engine and the existing execution pipeline. **Read `docs/CANDLE_MOMENTUM.md` first** (what changed, why the old one was late, configuration, lifecycle
states, diagnosing missed entries, limitations). Set the MT5 EA input `UpdateIntervalMs` to about 250 (default 1000) so the engine sees prices fast enough. Simulated tests only: no live-broker validation.
New tests: `tests/test_candle_momentum.py`, `tests/test_candle_momentum_e2e.py`, `tests/test_browser_candle_momentum.py`, `tests/candlemomentum_test.js` (see `TESTING.md`).

## Strategies, Autopilot and Trading controls
Candle Momentum (a live, tick-driven candle engine; see `docs/CANDLE_MOMENTUM.md`), CHoCH Trading (100-pip target), a per-instrument lot size, Autopilot with one stop condition (runs / net profit / hours),
and a backend-authoritative decision that updates without clicking Analyse. See `docs/AUTOPILOT_AND_STRATEGIES.md` (definitions, safety rules, limits).
Pip sizes for non-forex instruments are entered in the Pip size card of the Trading controls (or `[pips.overrides]`).

## EMA 20/50 + ATR strategy
A selectable strategy: BUY/SELL on a confirmed EMA 20/50 crossover (closed candles only, once per crossover), stop-loss 2 x ATR, take-profit 4 x ATR, any timeframe.
Settings in `config/settings.toml` `[strategy]` (`ema_fast_period`, `ema_slow_period`, `atr_period`, `atr_sl_multiplier`, `atr_tp_multiplier`).
How it works, calculations and limitations: `docs/EMA_ATR_STRATEGY.md`. Not back-tested; no profitability claim.


## Candle Momentum entry and execution-cost changes

* Percentage-based late-entry (MISSED) rejection was removed; entries are proposed at the current executable quote and the target comes from the actual fill. See `docs/CANDLE_MOMENTUM.md`.
* The universal "spread <= 0.25 x stop" execution rule was replaced by a per-trade execution-cost policy; `[pipeline] max_spread_stop_fraction` is now an optional user limit (0 = off). See `docs/STRATEGY_TO_EXECUTION_PIPELINE.md`.


## Command Centre layout: Autopilot command bar

The Autopilot panel is now the first thing on the Command Centre page (above the welcome line and the market selector); it used to sit at the bottom of the right-hand rail.
It is the same single component as before (same `data-tc` hooks, same backend state via `/api/selection`, `/api/decision/current`, `/api/autopilot/*`), moved and restyled, not duplicated, so every copy still shows one authoritative state.

* Desktop: instrument / strategy / timeframe across the top, the live decision and lot size on the left, Autopilot (state chip, Start, Stop, Emergency stop, the latest result message, then the stop-condition choice) on the right.
* Phone: Autopilot state and its buttons come first, then the decision, then the selectors. Buttons are at least 46 px high.
* The pip-size card is folded into an "advanced" disclosure; the Candle Momentum settings were already folded.
* Watchdog is switched on in the toolbar directly below the bar; its status also shows next to the decision badge in the bar.


## Pip / point size is detected automatically

The Pip card in the Command Centre now reads the SAME central engine as the strategies and the pipeline (`trademate/instruments.py`, `build_spec`), not the older `pips.py` resolver (which refused Crash/Boom, indices and crypto as "unknown").
The unit comes from the broker's own contract (digits, point, tick size, tick value, volume rules, stops/freeze levels) plus the asset class, and the card shows: the unit, whether the specification is VERIFIED or UNVERIFIED (and exactly which broker field is missing),
and the money value of one unit for 1.00 lot. Nothing has to be typed; the override box is optional and its saved value is clearly labelled "set by you". If the contract cannot be verified, automatic execution is blocked and the reason is shown.
The Crash 100 account convention (1.00 lot = 1.00 USD per 1.00 pip) is only trusted when the broker's tick value reproduces it.
* Autopilot Start is refused when the selected instrument's pip/point conversion is UNVERIFIED (the message names the missing broker field).


## Candle Momentum execution pipeline correction (2026-11-02)
Candle Momentum no longer uses the generic `[pipeline] max_entry_drift_stop_fraction` ceiling at final execution. It is a live-trigger strategy: it is re-evaluated against the current broker quote, and its target is re-anchored from the current executable entry. Stop/target geometry, remaining reward-to-risk, spread economics, fresh Risk Engine approval and broker validation remain mandatory. The spread assessment now also receives the instrument strategy-unit size for transparent unit reporting. The 60-second executor timeout is an unknown-outcome reconciliation safeguard, not a countdown; it is deliberately retained to prevent unsafe duplicate submissions when MT5 confirmation is delayed. See `docs/CANDLE_MOMENTUM.md`.
