# MT5 bridge (TradeMateBridge.mq5)

`mt5/TradeMateBridge.mq5` is an Expert Advisor that runs inside MT5. TradeMate never talks to MT5 directly: it reads a snapshot file the EA writes and sends commands as files the EA picks up and answers (the protocol is unchanged in this version).

* Snapshot: account, positions, symbols and quotes, written every second. A snapshot older than `[mt5] stale_after_seconds` means MT5 or the EA stopped.
* Commands (examples): `open`, `close`, `modify`, `candles`, `history`, `symbol_info`. Every command gets a confirmed answer; an order that is not confirmed is reported as unknown, never as sent.
* **Change in this version:** the timeframe table accepts `M2` (`PERIOD_M2`), so M2 execution works. Unknown timeframe names previously fell back to H1 inside the EA; TradeMate validates names before sending, so that fallback is unreachable from the application, but never rely on it.
* **Not exposed by the bridge:** `OrderCalcMargin` (margin per lot) and per-symbol trading sessions. TradeMate says so where it matters (lot adjustment, session hours) instead of inventing them.

Install and start:

1. Copy `mt5/TradeMateBridge.mq5` into MT5's `MQL5/Experts` folder, compile it in MetaEditor (F7), attach it to one chart and allow algorithmic trading.
2. Run `./launch.sh`. On Linux with Wine, `./launch.sh --mt5` does steps 1 and the MT5 start for you through `mt5/wine_mt5_helper.sh` (discovery, install, compile, start; it never closes positions and never restarts MT5 unless you pass `--restart-mt5` inside the helper and zero open positions are proven).
3. **The M2 line was added without being compiled here** (no MT5 in the build environment): compile the EA once and confirm it reports no errors before relying on M2.

Live behaviour against a real broker is not verified by the automated tests; see `TESTING.md`.
