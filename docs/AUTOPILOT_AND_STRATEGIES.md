# Strategies, Autopilot and the Command Centre (this release)

## Audit findings (what the code really did)
* **No universal fixed-2R target existed.** What looked like it: a per-strategy *minimum* reward-to-risk (`min_rr` 1.5, 2.0 for a counter-trend CHoCH), structural targets
  picked by `pick_targets`, and a pipeline check that rejects a trade whose live reward-to-risk fell below that minimum. The Risk Engine never set a target.
* **Stale WAIT root cause.** The Watchdog kept only the decision *string* and alert events. The decision panel was rendered only by `analyse()` (a click), so a
  Watchdog BUY produced a notification while the panel kept its old WAIT. There was also no per-selection record, so nothing could be checked for staleness or order.
* **Autopilot did not exist** (a placeholder endpoint said so). Both strategies were hard-wired to H1 + M5.

## What changed
| Area | Change |
|---|---|
| `trademate/pips.py` | The single pip / price-distance converter. Forex (4/5-digit, JPY) and metals resolve automatically. Everything else (Boom/Crash/Volatility, indices, crypto, shares) has **no universal pip** and is refused until you define it: type it into the **Pip size** card in the Trading controls (saved in `data/pip_sizes.json`, wins over settings) or set `[pips.overrides]` in `config/settings.toml`. The card shows what 10 / 100 pips mean in price, so you can sanity-check it. |
| `trademate/lotsize.py` | Exact lot validation (min / max / step, Decimal, never rounded) and a per-instrument saved lot (`data/lot_sizes.json`). |
| `strategies/candle_momentum.py` | Strategy A (below). |
| `strategies/choch.py` | Strategy B: selectable execution timeframe; fixed 100-pip target from the actual entry; "already played out" rule. Default (no `target_pips`) behaviour is unchanged. |
| `pipeline.py` | Fixed user lot; pip targets re-measured from the executable price at propose **and** at execute; broker minimum stop distance; one-order-per-setup ledger; `min_rr = 0` allowed for fixed-target strategies. |
| `setups_ledger.py` | Persistent, atomic one-order-per-setup ledger (survives restarts). |
| `decision_hub.py` | The one authoritative decision per (instrument, strategy, timeframe), ordered by tickets, with freshness. |
| `strategy/watchdog.py` | Publishes every analysis to the hub; `retarget()` follows a changed selection without a stop/start gap; in-flight results for an old target are discarded. |
| `autopilot.py` | The Autopilot engine (below). |
| `app.py` | `/api/decision/current`, `/api/selection`, `/api/lot`, `/api/autopilot/{status,start,stop,emergency-stop}`; `/api/strategy/analyse` publishes to the hub. |
| UI | One *Trading controls* component in the Command Centre and in a new **Strategy & Autopilot** tab of the chart side panel (`static/js/commandcentre.js`). |

## Strategy A: Candle Momentum (`candle_momentum`, rule `CANDLE-MOMENTUM-v2`)
**Rebuilt as a persistent, tick-driven engine. Full description, audit of the old behaviour, controls, lifecycle states and diagnostics: [`CANDLE_MOMENTUM.md`](CANDLE_MOMENTUM.md).**
Summary: it follows the forming candle of the chosen timeframe tick by tick (no look-ahead, no CHoCH, no structure). Price above the candle open = bullish, below = bearish.
When price reaches the configured trigger distance from the open, the entry is judged immediately on the current executable quote and, if the target is still reasonably available, proposed to the Risk Engine.
* **Stop** = a configured distance beyond the candle open (mandatory; no stop means no order). **Target** = the configured distance from the **actual fill**.
* The old hard-coded 2-pip "late" window (`candle_late_pips`) is **removed**. Entry validity, slippage tolerance, signal expiry and quote freshness are separate, named, configurable controls.
* **One entry per candle**, persisted across refreshes, Watchdog restarts, reconnects and backend restarts.
* The Watchdog does not poll it: the engine monitors and the Watchdog notifies from the same record; a validated signal wakes the Autopilot immediately.
* Reward-to-risk with the defaults (target 10, stop about 11-13) is below 1. It only pays if it wins more often than it loses. Nothing here claims it does.

## Strategy B: CHoCH Trading (`choch`)
Existing confirmed-swing Market Structure rules, unchanged. Execution timeframe is independent per strategy (M1, M5, M15, M30, H1, H4; its context timeframe is derived: M5/M15→H1, M1→M15, M30/H1→H4).
Target = **100 pips** from the actual entry; the stop stays structural and independent. A CHoCH is refused as *already played out* once price has moved more than 50% of the target beyond its level (`choch_max_extension_fraction`). If the risk is too large the Risk Engine rejects the trade; the target is never changed to fit.
Trend Continuation is unchanged (H1 + M5, structural targets, not timeframe-selectable).

## Lot size
Set per instrument in the Trading controls (`POST /api/lot`). The backend validates exactly against the instrument's real `volume_min / volume_max / volume_step` and `[trading] max_volume`; an off-step value is rejected naming the neighbours (never rounded). The saved value is re-validated at propose time and again at execute time. It affects **future** entries only. With no lot saved, manual proposals keep risk-based sizing and Autopilot will not start.

## Autopilot
* **Watchdog watches; Autopilot executes.** Autopilot reads the hub's current decision (fresh, ≤ `max_decision_age_seconds`, BUY/SELL, an identifiable setup, not yet attempted), then calls the **existing** pipeline: fresh strategy evaluation → levels from the executable price → lot validation → Risk Engine → approval → the normal journal-backed `place_order` → placement check. There is no second execution path.
* **A run** = one strategy-approved trade opened by Autopilot and later closed (TP, SL or any exit) and present in the broker deal history. One Autopilot trade is open at a time.
* **Stop conditions (exactly one, backend-enforced):** number of runs · profit target (sum of **net realised** P&L: profit + commission + swap, account currency, of this run's closed trades; floating P&L is never counted) · elapsed hours (start time persisted, so refresh/restart do not reset it).
* **When reached / emergency stop:** no new entry is ever attempted again; the reason is recorded and shown; open positions are **not** closed (they keep their broker SL/TP) and are still tracked to their close.
* **Restart:** Autopilot never resumes by itself; it returns as *interrupted* and needs an explicit **Resume** (counters and start time kept).
* **Safety:** one attempt per setup; an unconfirmed order stops Autopilot (*blocked*) until you reconcile in MT5; changing instrument/strategy/timeframe while armed is refused; it requires `[trading] enabled`, MT5 connected, and the Watchdog monitoring the *same* selection.

## Command Centre synchronisation
`GET /api/decision/current?symbol&strategy&timeframe` returns the record for exactly that key (never another's). The page polls it every 2 s and renders it into both the Trading controls and the big Decision panel, so no Analyse click is needed while monitoring. Responses carry a client sequence guard (older responses are dropped), selection changes clear the decision immediately, and stale / error / disconnected states are shown as such. The Watchdog's loop runs server-side.

## Honest limits
* Verified only against the in-memory fake MT5 bridge and stubbed brokers. **Nothing was run against a live MT5**: broker minimum stop levels, fills, deal-history latency and the take-profit re-alignment need a demo-account check before you trust it.
* Pip conventions for non-forex instruments are yours to define (Pip size card). A wrong value makes every pip target wrong, so check the preview against your broker's contract.
* Profit-target mode depends on the broker's deal history reporting `net` for the closed position.
* Sound/desktop notifications for Autopilot events use the browser Notification API when permission is already granted.

## Addendum: EMA 20/50 + ATR (`ema_atr`)
Selectable like the others, single timeframe (your execution timeframe), settings under `[strategy]` (`ema_fast_period`, `ema_slow_period`, `atr_period`,
`atr_sl_multiplier`, `atr_tp_multiplier`). Its stop and target are ATR price levels (no pip conversion), so the pip size card does not affect it. Details and
limitations: `docs/EMA_ATR_STRATEGY.md`.


## Autopilot controls: Pause, Stop and Emergency Stop

| Control | Effect | Session | Can new orders be placed afterwards? |
|---|---|---|---|
| **Pause** (state `paused`) | Suspends NEW trading. Open trades are still tracked to their close. | Kept (mode, stop condition, counters, selection, lot). **Resume** continues the same session. | Only after Resume. |
| **Stop** (state `stopped`) | Ends the session cleanly. | Ends. | Only by starting a new session. |
| **Emergency Stop** (state `emergency_stopped`, `emergency_locked: true`) | Sets the persistent emergency lock, stops Autopilot, cancels pending approvals. | Ends. | **No**, from any route (the Risk Engine rejects everything) until you lift the lock with `UNLOCK` in Settings. |

None of them closes open positions unless "Emergency stop also closes every open position" is switched on in Settings (off by default); open positions keep their stop-loss and take-profit. A restart never resumes trading by itself.
