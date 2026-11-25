# RAPID FIRE: "Beauty in simplicity."

An independent strategy engine. It does **not** use the Candle Momentum / CHoCH / Market Structure / Bull-Bear / Trendline code, signals or
settings, and none of those were changed to add it. It lives in `trademate/rapid_fire/` and is switched on and off from its own panel
(Strategy page, `#rf-panel`) or `/api/rapid-fire/*`.

## What it does

1. **Instrument and timeframe.** `VOL_80` (editable), **M1 only** (any other timeframe is refused). The symbol is matched by exact broker
   name; nothing is substituted. If the broker has no such symbol the engine is BLOCKED and says so.
2. **Entry.** In the current M1 candle, when price moves `trigger_points` (default 5) from the reference price, it sends a market order in
   that direction. The reference is the **M1 open** for the first entry of a candle and the **last exit price** for the next one (so a second
   shot needs a fresh 5-point move rather than firing at once).
3. **Take profit.** `target_points` (default 10) from the broker-confirmed fill. There is **no broker-side TP or SL**: the engine watches the
   quote and sends a close request when the target is reached, then waits for MT5 to confirm the position is gone.
4. **Per-candle limit.** At most `max_entries_per_candle` (default 2, hard cap 5) entries per M1 candle. The counter is reserved
   *before* an order is sent and saved to `data/rapid_fire_state.json`, so polling, a page refresh, a TradeMate restart or an MT5
   reconnect cannot grant extra entries. It is enforced in the backend, not the browser.
5. **Lot.** Configurable (`lot`, required). The **lot helper** suggests the largest valid lot whose *estimated* gross profit at the target
   distance does not exceed a wanted amount (for example 50), using the broker's `tick_value / tick_size × point`. It never assumes that
   10 points equals a given sum, rounds down to the broker's volume step, and reports when even the minimum lot earns more.
6. **One Rapid Fire position at a time.** A new entry waits for the previous trade to be confirmed closed.

## Money target (profit per fire)

Set `profit_per_fire` (account currency) and `commission_per_lot` (per lot, round turn, account currency; the bridge cannot read commission, so you enter it).
The take-profit distance is **calculated from MT5 metadata**, never from pip assumptions (`trademate/rapid_fire/target.py`):

    ticks    = ceil((profit_per_fire + commission_per_lot * lot) / (tick_value * lot))
    distance = ticks * tick_size         points = distance / point

The spread is shown (points) and the TP price is the **bid** for a BUY exit / **ask** for a SELL exit, from the fill, so the spread is paid at the open and the
TP is measured from the real fill. The panel (`#rf-plan`) and `/api/rapid-fire/status` (`target_plan`) show points, TP price and the commission note.
If tick size, tick value, point, lot or quote is missing or invalid, there is **no entry** and the config problem is reported. Without `profit_per_fire`,
the old `target_points` mode applies unchanged (no default was changed).

## Chart lines

For open Rapid Fire positions that are present in MT5 and managed by the engine, the chart draws read-only dotted lines: entry, `RF TP`, `RF EMERGENCY`
with price, distance in points and money. They come from `/api/rapid-fire/chart-levels`, are refreshed from the broker snapshot, flagged stale when
quotes are old, and disappear when the position closes. No line is drawn for anything that is not a live position.

## Anti-Twin

ON/OFF only (`anti_twin_enabled`). See `docs/ANTI_TWIN.md`: the position's own MT5 entry is remembered; at the first adverse move of its executable exit price (one tick worse than where it started, spread excluded) the position is closed, the closure confirmed, and an equal-volume opposite position is opened, repeatedly, each with its own reference. Emergency exit and Emergency Stop outrank it. The old `anti_twin_adverse_points` / `anti_twin_mode` are retired and ignored. The twin does not use a candle entry.

## Risk Engine integration (no arbitrary stop-loss)

Every order passes `gate_order` (the same central gate all strategies use). The Risk Engine sizes risk from a stop distance, and Rapid Fire
has no broker SL, so the required setting **`emergency_points`** is the software stop distance used for the gate. A small additive hook
(`set_software_stops_provider` in `trademate/risk/account_state.py`) makes these positions count toward Current Active Risk at that level,
instead of being invisible. Nothing in the Risk Engine's rules or maths was changed. Start is refused until a lot and an emergency exit are set.

## Limits of software-managed exits (read this)

Because there is no broker TP/SL, **if TradeMate, the file bridge, or the MT5 terminal stops, nothing closes the position**. The panel warns
when the engine thread is not running, when quotes are stale, or when a close request fails repeatedly (cap: 5 attempts, then "close it
in MT5"). A fast market can also fill the close at a worse price than the target (slippage). Use on a demo account first.

## Safety behaviours

* Start waits for the *next* M1 candle (no entry from a candle that was already running).
* A restart never auto-resumes entries; open Rapid Fire positions are managed again (target / emergency) but new entries stay off until Start.
* An order whose outcome is unknown (timeout) is reconciled against MT5's execution journal; if still unknown, entries are blocked and the
  panel offers **It filled / It did not fill** (you supply the ticket).
* Emergency Stop and Live mode block the engine. Results shown (net, win rate) come only from MT5 deal history, i.e. broker-confirmed.

## Trade history

`/api/history` rows for Rapid Fire trades carry an optional `rapid_fire` object (strategy, timeframe, shot, role, twin link, target, how it
was closed). The History page shows a RAPID FIRE badge and ORIGINAL / ANTI-TWIN badge in the Symbol column. Gross, costs and net are MT5's.
`/api/rapid-fire/trades` lists every attempt with execution status.

## API

`GET /api/rapid-fire/status` · `GET|POST /api/rapid-fire/config` · `POST /start` · `POST /stop` · `GET /trades` ·
`POST /trades/<id>/resolve` · `GET /lot-suggestion?profit=`. State files: `data/rapid_fire_config.json`, `data/rapid_fire_state.json`,
event log `data/rapid_fire_events.jsonl`.

## Interpretations I made (the brief was silent)

"5-point entry trigger" is measured from the M1 open, then from the last exit price; one position at a time; twin does not count as a shot;
no auto-resume after restart; the emergency exit is mandatory. Change these only deliberately.

## NOT verified

Behaviour against a **live MT5 / broker**, real VOL_80 contract metadata (tick value, point, minimum lot), real slippage and the naira
amount per shot (TradeMate has no exchange rate and reports account currency only). Tests use the real `MT5Adapter` with a fake bridge.
