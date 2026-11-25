# Bull/Bear Trading v3 and Anti-Twin

Status: implemented and tested **in simulation** (simulated candles, a fake broker and an in-memory executor). It has not been run against a live MT5 account, and
no claim of profitability is made. See "Needs live verification" at the end.

## 1. What changed (a deliberate replacement)
The v1 rules (a five-candle group direction plus a same-direction preceding candle) were **removed**. The id `bull_bear`, its name, its tooltip and its saved
settings (`bull_bear_target_pips`) are kept. The new engine is a renamed, modified copy of the Candle Momentum engine in `trademate/bull_bear/`. Candle Momentum
itself (`trademate/candle_momentum/`) is untouched, and the two share no config file, event log, state file or candle id.

## 2. The rule
* The forming candle is judged tick by tick exactly like Candle Momentum (trigger distance from the candle open, protective stop beyond the open, target from the actual fill, same Autopilot -> Risk Engine -> pipeline path; the engine never sends an order itself).
* **Plus the structure gate** (`trademate/bull_bear/structure.py`, pure functions):
  * the market structure is read from the **closed** candles of the chosen timeframe by the existing structure engine;
  * the **latest valid CHoCH** (confirmed, no same-candle conflict, reference swing confirmed before the break) establishes the direction: bullish CHoCH = bullish structure, bearish = bearish;
  * BOS events after it are continuation, the direction stays;
  * a **newer opposing valid CHoCH invalidates** the old direction and establishes the opposite one;
  * **no CHoCH = no direction = no entry**;
  * the candle is eligible only if **candle direction == structure direction**. A bullish candle in bearish structure (or the reverse) is never traded. If the structure flips while a signal is waiting for validation, the signal is withdrawn (`INVALIDATED`).
  * If the structure cannot be read (data missing, engine failure) there is no direction and no trade.
* The structure is re-read when a new candle opens (not on every tick). The panel shows the structure direction, the CHoCH status, and the gate's verdict for the current candle.

## 3. Anti-Twin (ON/OFF, v5)
`anti_twin_enabled` is the only Anti-Twin setting (default off, independent of Candle Momentum and Rapid Fire). The rule is the shared one in `docs/ANTI_TWIN.md`:
the position's actual fill is its reference; once price has left it and returns to or crosses it, the position is closed, the closure confirmed, and an equal-volume opposite position is opened through the Risk gate. No profit threshold, no waiting for candle close. Emergency Stop is checked before the close and before the open. Twin SL distance = the root's, twin TP = half of the root's TP. Fixed chain ceiling 20. The former `at_*` keys are retired (old files load, values ignored). `TP_TOUCHED` still ends the watch.

## 4. Market Structure risk-to-reward
Command Centre settings -> "Market Structure risk-to-reward": fixed target (default, unchanged), 1:1, 1:2, 1:3, 1:4 or a custom ratio (0.1 to 100). The target is `ratio x final stop distance`, for BUY and SELL, shown in the trade information as `1:N risk-to-reward`. It is saved per scope like the other trade settings (`risk_reward`). Stop calculation and structure detection are unchanged. Premise note: Market Structure never had a fixed 4R: its target was 25 pips, so the ratio is an opt-in mode with the previous behaviour as the default.

## 5. CHoCH target distance
The CHoCH target was already a setting (`choch_target_pips`, default 100, overridden by a saved `target_distance`); 100 was a default, not a hard-coded limit. The Command Centre settings now offer presets (50, 100, 150, 200) and any custom positive number, show the active value and where it comes from, and the tests prove the saved distance reaches the order unchanged (the pipeline re-measures it from the actual fill).

## 6. Migration
* Existing `bull_bear` settings: `bull_bear_target_pips` is the new engine's default target. No v1 candle-group key existed in saved files, so nothing needs migrating.
* An existing `trade_settings.json` loads unchanged (`risk_reward` is simply absent = not chosen).
* New files appear only when used: `data/bull_bear_config.json`, `bull_bear_state.json`, `bull_bear_events.jsonl`, `bull_bear_antitwin.json`. Hard reset knows them.
* Behaviour change to expect: Bull/Bear no longer trades candle groups; it needs a CHoCH on the chosen timeframe first.

## 7. API
`GET /api/bull-bear/state?symbol=&timeframe=`, `GET/POST /api/bull-bear/config`, `GET /api/bull-bear/events`. Same shapes as the Candle Momentum endpoints plus `machine.structure`, `anti_twin` and `emergency_locked`.

## 8. Tests
`tests/test_bull_bear.py` (structure state, gating, config, Anti-Twin, end-to-end on the fake bridge), `tests/test_strategy_enhancements.py` (R:R and CHoCH distance), `tests/test_browser_bullbear.py` (panel and settings UI).

## 9. Needs live verification
Structure and entry behaviour on real MT5 candles; the broker's actual close/open timing and the Risk gate's response to a twin (for example a position-count limit refusing the twin after the original closed); real commission and slippage values; behaviour across a restart with real positions.
