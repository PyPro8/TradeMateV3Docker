# Analysis page (Phase 7A.1)

Sidebar > **Analysis**. It shows what the Market Structure Engine has determined for one symbol and timeframe, and WHY.
It is read-only: it never places an order, never proposes an entry, stop or target, and never says a trade exists.

Code: `trademate/analysis.py` (pure view model), `GET /api/analysis?symbol=EURUSD&timeframe=H1`, `static/js/analysis.js`.
Every value comes from the candles of that one request. Nothing is hard-coded; a field the system cannot substantiate is shown as
"not available". `tests/test_analysis.py` proves it (shifting every price by 1000 shifts every output by 1000).

## The four kinds of statement (kept apart on the page)

| Kind | Meaning |
|---|---|
| Observed facts | What the candles and swings literally are: prices, times, labels, counts. |
| Rule-based interpretation | What the written rules (`MARKET_STRUCTURE.md`) conclude from those facts. |
| Conditions to watch | What WOULD change the classification. Always conditional ("If a candle closes above X..."). Never a prediction or a signal. |
| Trade setups | **Not available on this page.** This page analyses one timeframe and never evaluates setups. Multi-timeframe setup evaluation is a separate feature on the Strategy page (`docs/MULTI_TIMEFRAME_STRATEGY.md`). |

BOS and CHoCH are structural classifications, not forecasts. A CHoCH does not by itself confirm a reversal. A BOS does not mean a
trade is high-probability. The page says so, and a test fails if the wording drifts.

## Analysis status

| Status | When | Emitted by |
|---|---|---|
| READY | Analysis finished on healthy data and nothing is developing. **Not a trade signal.** | server |
| WATCH | As READY, but the still-forming candle has made a break that may be undone before it closes. | server |
| STALE DATA | MT5's snapshot stopped updating (older than `[mt5] stale_after_seconds`). Nothing is analysed. | server |
| UNAVAILABLE | No connection to MT5, or no candles came back. Nothing is analysed. | server |
| INVALID DATA | Candles with impossible values or conflicting duplicates. Nothing is analysed (dropping them would silently change which candles are neighbours). | server |
| INSUFFICIENT DATA | Fewer than the minimum candles, or no confirmed swing high or no confirmed swing low yet. | server |
| ANALYSING | A request is in flight. | browser only |
| NO SETUP | **Reserved.** Saying "no setup" needs a setup evaluation; this page has none (see the Strategy page). Never emitted. | nobody |

READY never appears when data is missing, stale, invalid or incomplete (`StatusTest`).

## What is NOT known (honest limits)

* **The broker's clock.** MT5 candle times are the broker's server time. The bridge does not export a clock field; TradeMate uses its
  shared broker-clock resolver, measured from fresh M1 candles or the configured broker-time fallback. The API reports whether that
  clock was verified. If no trustworthy clock is available, the newest candle is treated as **still forming** and is not used as a
  confirmed structural break. Feed liveness (MT5 still updating) is reported separately.
* Times are shown as MT5 reports them, not converted to your time zone.
* `trend` on the engine result includes a break made by the forming candle. The page shows `trend_confirmed` (as of the last CLOSED
  candle) and, separately, what it would be if the forming candle closed as it is.

## Chart integration

Each event has **Show on chart** (opens that symbol/timeframe and rings the event) and **Show on chart with structure overlay**
(also switches the overlay on). Your saved overlay visibility is respected: the first button never changes it. Analytical levels are
drawn thin/dashed with a ring; trade entry, SL and TP lines are separate. A developing event is drawn dotted and fainter and labelled
"(forming)" (or "?" when short). The highlight is tied to symbol + timeframe, so it disappears when either changes.

## Modules: Liquidity, Fair value gaps, Order blocks (Phase 7C repair) and what is still missing

The **Modules** panel is decided per response by `module_list` in `trademate/analysis.py`, from what that response actually contains:

| Status | Meaning |
|---|---|
| `available` | the engine ran on this request's candles and produced a result (it may still have found nothing) |
| `unavailable` | the engine exists but could not run for this request; `reason` says why (too few / invalid candles, MT5 not connected, an internal error) |
| `not_available` | this single-timeframe page does not provide it (multi-timeframe and trade setups live on the Strategy page, Phase 8) |

Only a view with `available: true` counts; a missing view is `unavailable`. No engine is claimed because it merely exists.

* **Fair value gaps** (`fvg` key): `build_fvg_view` runs the FVG engine on the candles (newest candle assumed forming, so a gap it completes
  is `developing`). It needs only candles, so it is analysed or honestly refused whatever the structure analysis says. Lists the newest 10
  gaps with boundaries, candle times and a plain explanation; older ones are counted.
* **Order blocks** (`order_blocks` key): `build_order_blocks_view` runs the order block engine on the candles and the structure result of the
  same request. Lists live blocks (candidate / confirmed, up to 12) and finished ones (invalidated / expired, up to 8) with counts. The
  page's sentence for each block is composed from the engine's fields with readable times (the engine's own `explanation` quotes epoch seconds).
* **Empty and error states:** "found nothing" is `available` with zero items and an explicit message on the page; "could not run" is
  `unavailable` with the reason. An engine crash is logged and reported as unavailable without leaking internals and without affecting the
  structure analysis or the other engines. Hiding a chart overlay changes nothing here.
* **Liquidity** keeps its own context (`liquidity_ctx`); its module status follows the same rule.
* Adding another engine later: return its view under a new key in `build_analysis`, add it to `MODULES` and `ENGINE_MODULES`; the page
  renders the Modules list from the response.

## Plain-language summary (Task 2)

`GET /api/analysis` also returns `summary` (built by `trademate/analysis_summary.py`; `null` if building it ever failed, in which case the page shows the detailed results unchanged; absent when no analysis was possible, e.g. a stale feed). The page renders five sections from it and keeps the old panels under **Technical details**.

**Structure direction & quality.** The API keeps `confirmed_direction` (the shared engine's last closed-candle direction) separate from
`quality` (`aligned`, `developing`, `early`, `mixed` or `unavailable`). The Analysis badge reports both, in the same spirit as the Strategy
page's `bias (strength)` display. `label` is the confirmed direction (`bullish` / `bearish`), or `unclear` when none exists; it is never `mixed`. Evidence problems appear only in `quality`.
The forming candle is reported separately and cannot change `confirmed_direction`. The Strategy engine uses the same underlying structure
facts, but still applies separate data-quality, higher-timeframe-context and setup-qualification rules.

* `unclear`: no confirmed break has set a direction, or structure did not run.
* `mixed` (quality): the last confirmed break was a BOS but the latest confirmed swings oppose it.
* `early` (quality): the last confirmed break was a CHoCH with no follow-through BOS yet (the swing pattern normally lags a CHoCH).
* A forming-candle break is `provisional`: it is listed in the context, never changes direction or quality, and makes the OVERALL verdict `mixed` if it opposes the confirmed direction.
* A confirmed `bullish` / `bearish` direction is not a trade signal. If the last break was a CHoCH, the page adds a caution: it is an early structural change, **never** presented as a confirmed trading reversal.

**Liquidity.** Nearest active buy-side / sell-side levels to the newest close, and up to three confirmed sweeps. A sweep counts as evidence only through what followed it: a CHoCH against the sweep = *reversal* evidence, any other later break = *continuation* evidence, none = *no evidence yet* (closing back inside the level is mentioned, not counted), a break on the same candle = *unclear* (the order is unknowable).

**Fair value gaps.** The two nearest not-fully-filled gaps per side. Zone = engine boundaries. Status is derived (the engine does not track fills): *Open* (no later candle reached the gap), *Partly filled* (n %), *Filled* (a later wick crossed the whole gap); a fill caused only by the newest, possibly forming, candle is marked provisional.

**Order blocks.** The two nearest live blocks per side, with the engine's own status (candidate / confirmed). Candidates are not confirmed.

**Overall.** Structure direction plus the sweep follow-through plus whether price is inside a confirmed zone. Zones merely existing above or below price are not treated as direction. Always lists conflicting evidence and what remains uncertain; says **not enough evidence** when direction is unclear.

None of it is advice, a prediction or a signal.

### Comparing Analysis with Strategy

Compare the same symbol, timeframe, candle cutoff, and `structure` configuration. Analysis fetches up to 500 candles for H1/H4 (300 for
other supported timeframes) and uses the shared broker-clock resolver. Strategy fetches M5/M15/H1/H4 with its own quality gates and
analyzes only closed candles. Separate requests are not atomic; a candle can close or update between them. Both views use the shared
`trademate/structure/engine.py`, so matching closed candles/configuration should produce matching structural direction and event identities.
Analysis and Strategy now read direction and quality from the same `structural_facts` (`trademate/structure/facts.py`): `early`/`mixed` quality on one page is `EARLY`/`MIXED` strength on the other, for the same confirmed direction. A direction/break identity mismatch with otherwise matching inputs is a defect to
report with both read-only JSON responses and the candle snapshot.
