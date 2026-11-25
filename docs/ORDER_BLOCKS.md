# Phase 7C: Order Block (OB) detection engine

Analysis only. Nothing here places an order, sets a stop or target, sizes a position, or talks to the Risk / Execution engines.
Code: `trademate/orderblocks/engine.py` (pure Python; imports only `trademate.structure`). It is shown on the **chart** (`/api/candles?order_blocks=1`
-> `static/js/ob.js` -> `drawOB` in `static/js/app.js`) and on the **Analysis page** (`build_order_blocks_view` in `trademate/analysis.py`), and is
referenced by no other code: never by broker, risk or execution code.

## What it is and is not

TradeMate has candles and nothing else: no volume, order book or order flow. So this engine cannot detect "institutional" activity,
resting orders or intent. An order block here is a *candle zone that price structure alone singles out* as the base of a move that
broke structure. "Confirmed" means only that the structural conditions below were met. A block is not a prediction that price returns
to it or reacts there, carries no quality score, and is not a trade setup.

## Rule (bullish; bearish is the exact mirror: high/low, up/down, bearish/bullish candle swapped)

Built on the Market Structure result (confirmed swings and BOS / CHoCH events), computed on the same candles. A bearish candle has
close < open, a bullish one close > open; a doji is neither.

1. **Candidate.** For each confirmed swing low `L` (pivot candle `o`) with an earlier confirmed swing high, let `H` be the latest swing
   high before `o`. The **source candle** is the last *bearish* candle at or before `o` and after `H`'s pivot. None, or no earlier
   swing high: no block. Zone: upper = source high, lower = source low (full range; open and close are also returned). Several swing
   lows mapping to one source candle are one block. Known from the candle at which `L` became knowable. Not confirmed.
2. **Confirmed.** The structure engine reports a bullish BOS/CHoCH that breaks exactly `H`, on a candle after `o`, not as a
   same-candle two-sided break, **and** `L` is the leg's origin: `o` is the lowest low (earliest if equal, the structure engine's own
   tie-break) between `H`'s pivot and the candle before the break. `confirmed_time` = the later of the break candle and the
   candle at which `L` became knowable (so a break that happens before the swing is knowable is confirmed only when it is).
   Optional `max_leg_candles`: break at most that many candles after the source candle (off by default).
3. **Invalidated (price).** `close` mode (default): a candle closes below the zone's lower boundary; `wick` mode: any trade below
   it. Price is checked first on a candle, so a candle that both breaks structure and violates the zone leaves the block
   invalidated, never confirmed. Violated after confirmation: `invalidated` with `confirmed_time` kept. Invalidation can predate
   identification (the block was never a live candidate).
4. **Expired (structure, not price).** A candidate that can no longer be confirmed: `H` was replaced by a newer swing high without
   being broken, was broken ambiguously, or was broken by a different origin / beyond `max_leg_candles`. Codes `REFERENCE_LOST`,
   `NOT_QUALIFIED`. If `H` was already broken before `o`, no block is created. A candidate stays undecided until the leg's origin is
   knowable.

Statuses: `candidate`, `confirmed`, `invalidated`, `expired` (the last two are final).

## Using it

```python
from trademate.structure import StructureConfig, analyze_cached
from trademate.orderblocks import analyze_order_blocks, OrderBlockConfig
s = analyze_cached(candles, StructureConfig(last_candle_closed=False))                 # candles: [time, o, h, l, c] rows
res = analyze_order_blocks(candles, s, OrderBlockConfig(), symbol="EURUSD", timeframe="M5")
res.data_status        # "ok" | "insufficient" | "invalid" | "inconsistent"   (res.reason says why)
for b in res.blocks:   # oldest source candle first; b.status, b.lower, b.upper, b.source_time, b.confirmed_time, b.explanation ...
    if b.status == "confirmed" and not b.provisional: ...
```

* **Same candles:** the structure result must come from exactly the candles passed (its indices are positions in the cleaned list);
  a mismatch gives `data_status="inconsistent"` and no blocks.
* **Forming candle:** taken from the structure result (`config.last_candle_closed`). A block whose *current* status was set by the
  newest, still-forming candle has `provisional=True`; require `provisional == False` for settled evidence.
* **Bad data (fail closed, as the FVG engine):** an invalid candle, a price <= 0 or conflicting duplicate timestamps gives
  `"invalid"` and no blocks; too few candles gives `"insufficient"`. Out-of-order input is sorted (flagged in `diagnostics`).
* **No look-ahead / no duplicates:** each state change carries the time of the candle that established it, and a prefix of the data
  reports the same identified / confirmed / invalidated / expired times as the full data. `id` = `OB-BULL|BEAR-<source epoch s>`;
  repeated runs, repeated rows and growing series give each block once.
* **Finding the swing / event:** use `origin_time`, `reference_time`, `confirming_event_time`. `swing_id` and `confirming_event_id`
  echo the structure engine's ids, which are formatted with `%g` (6 significant digits) and **can collide at real epoch
  timestamps** (observed: 27 swings, 9 distinct ids). This engine never relies on them; they were left unchanged.

## On the chart and on the Analysis page (Phase 7C repair)

* **Chart data flow.** `GET /api/candles?...&order_blocks=1` adds an `order_blocks` block: the engine's own result for exactly those candles and
  the same structure parameters as the chart's structure overlay (`swing_left`, `swing_right`, `break_mode`), plus `available` (true only when
  `data_status == "ok"`). It works without `structure=1` and ignores the saved `order_blocks_visible` setting. The page always asks for it,
  so the data is held whether or not the zones are shown. Insufficient / invalid / inconsistent data or an engine failure gives
  `available: false`, no block and a reason; the candles still load.
* **Drawing.** `static/js/ob.js` maps `source_time` to a candle index and passes `upper` / `lower` through unchanged (it never reads a candle
  price). A zone runs from the source candle to the newest candle, so it reads as "still valid as far as the engine can tell"; finished blocks
  would end at their own `invalidated_time` / `expired_time` but are not drawn by default (history stays on the Analysis page). Teal-green =
  bullish, orange = bearish; solid + edge bar = confirmed and settled; dashed and fainter = candidate or provisional. Zones are drawn behind
  the candles (and behind fair value gaps), replaced as a whole on every refresh, and never drawn for another symbol / timeframe.
* **Controls.** Toolbar button, Settings checkbox, saved as `order_blocks_visible` (default false); included in Show all / Hide all and the
  layers button. Hiding never stops the analysis.
* **Analysis page.** `GET /api/analysis` returns an `order_blocks` view (counts per status, live and finished lists, readable explanations);
  the Modules list reports it `available` only when the engine ran. Empty result: explicit empty state. Could not run: the reason.
* **Diagnostics hooks** (read-only, used by tests): `TradeMateApp.obFrame()`, `obDrawn()`, `overlays()`, `analysisData()`.

## Limitations and assumptions

* Nothing about volume, order flow or intent. The name "order block" is a pattern label.
* No displacement / candle-size / volatility test and no FVG requirement: a slow grind that finally breaks the high also confirms
  (use `max_leg_candles` to bound it). Whether a break was "impulsive" is not judged.
* Full-candle zones only (no body-only zones). No mitigation / retest tracking, breaker blocks, scoring or multi-timeframe logic.
* Only the leg's deepest origin is confirmed; intermediate pullback candidates expire `NOT_QUALIFIED` by design.
* Depends on the structure parameters (`swing_left`, `swing_right`, `break_mode`): different settings give different blocks.
* Time gaps (weekends, missing bars) are not detected; "adjacent" means adjacent in the supplied data.
* Unconfirmed candidates accumulate until invalidated or expired; filter by `status` / age.
* On the chart only live blocks are drawn and each runs to the newest candle; there is no click / hover detail, no mitigation marker and no
  limit on how many live blocks a busy chart can show.

Tests: `python3 -m unittest tests.test_orderblock_engine -v`.
