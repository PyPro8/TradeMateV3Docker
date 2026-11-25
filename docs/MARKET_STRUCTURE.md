# Phase 7A: Market Structure Engine and chart overlay

Analysis and display only. Nothing here places an order, sets a stop or target, sizes a position or talks to the
Risk / Execution engines. Code: `trademate/structure/engine.py` (pure Python, no Flask, no broker).

## Rules

**Swing points.** With `swing_left = L` and `swing_right = R`, candle *i* is a pivot **high** if its high is strictly
above the highs of the L candles before it and at least equal to the highs of the R candles after it. A pivot **low**
is the mirror on lows. *Equal highs/lows:* the earliest candle of an equal run is the pivot. An outside candle can be
both a pivot high and a pivot low; both are kept. Consecutive pivots of the same type are allowed.

**Confirmation / no lookahead.** A pivot is only known once candle *i + R* exists. Each swing has `pivot_time` (where it
happened) and `confirmed_time` (when it became knowable). Classification and breaks use a swing only from its
confirmed candle on. Running on a prefix of the data gives the same results as the full run for everything confirmed
inside that prefix (tested for every prefix, both break modes).

**Classification.** A pivot high is `HH` / `LH` / `EQH` against the previous confirmed pivot high; a pivot low is
`HL` / `LL` / `EQL` against the previous pivot low. The first of each type has `classification = None` (no evidence,
nothing invented). `equal_tolerance` (absolute price) widens "equal".

**Structural methodology: one swing/external layer.** TradeMate currently has one pivot scale per timeframe, controlled
by `swing_left` / `swing_right`. It does not separately detect or label internal structure. A meaningful swing is a
confirmed fractal at this configured width; smaller fluctuations that do not qualify as those pivots are not structural
events.

**Direction bootstrap.** Direction begins undefined. The first break of a confirmed swing high or low is recorded as a
bootstrap BOS and establishes bullish or bearish direction. This is a starting state, not proof of a mature trend. A
range with no qualifying break stays undetermined; the engine does not infer direction from one HH/LL label.

**Continuation references and BOS.** In bullish structure, a bullish BOS may break the latest confirmed, unbroken HH
or EQH pivot high. In bearish structure, a bearish BOS may break the latest confirmed, unbroken LL or EQL pivot low.
A later pivot of an ineligible type (for example, a lower high during bullish structure) does not replace the
continuation reference and cannot create a BOS merely because price crosses it. A newly confirmed eligible pivot
supersedes the earlier eligible reference; a broken reference is consumed and not revived.

**Protected references and CHoCH.** A bearish CHoCH against bullish structure must break the latest confirmed higher
low (HL); a bullish CHoCH against bearish structure must break the latest confirmed lower high (LH). These are the
protected low/high references. Later LL/LH pivots do not replace a bullish protected low; later HH/HL pivots do not
replace a bearish protected high. The protected reference is consumed when broken. If no eligible HL/LH exists, an
opposing minor swing break is not promoted to CHoCH and does not change direction. A qualifying CHoCH changes the
structural direction, but does not by itself confirm a trading reversal. Subsequent events are classified against the
newly established direction.

**Origin swing (added by the CHoCH audit).** After a confirmed CHoCH the engine also protects the other side: a bearish CHoCH
makes the latest confirmed swing high known before the break (the origin of the leg that broke structure) the protected high; a
bullish CHoCH makes the latest confirmed swing low the protected low. Without this, structure could stay "bearish" while price
closed above every old high, because no lower high had formed yet. A later confirmed LH / HL replaces the origin swing through the
normal protected rule. Events broken through an origin swing carry `reference_role = origin_swing`. Only confirmed swings known
before the breaking candle are used; a forming candle never creates one, and a bootstrap BOS never creates one (no established
direction, so nothing is being changed).

**Break confirmation and ambiguity.** `break_mode = "close"` (default) requires a strict close beyond the reference
level; `"wick"` requires a strict high/low beyond it. When one candle breaks both qualifying directional references,
both events are returned with `same_candle_conflict = true`. The resulting direction is undetermined because the
intrabar order is unknowable; the engine does not infer a sequence.

**Data handling.** Invalid candles (NaN/inf, non-numbers, bools, high < low, open/close outside the range, wrong
shape) are excluded and listed in `diagnostics.invalid`. Duplicate timestamps keep the last occurrence
(`duplicates_removed`, `duplicates_conflicting`). Out-of-order input is sorted (`out_of_order`). Fewer than
`L + R + 1` usable candles gives an empty result with `insufficient_data = true`.

**Forming candle.** A forming break is shown as unconfirmed and may show a provisional `trend_after`, but does not
consume confirmed references, update protected/continuation levels, or alter `trend_confirmed`. A pivot whose right-
side confirmation candle is forming is displayed as unconfirmed but cannot enter a confirmed reference set. The next
refresh may remove or change provisional evidence. The Strategy Engine excludes forming candles from its structural
state. The Analysis API uses the shared broker-clock measurement when available; without a verified clock it treats
the newest candle as forming.

## Event fields and explanations
The pure engine event includes `id, type (BOS|CHOCH), direction, level, reference_pivot_time,
reference_pivot_confirmed_time, candle_time, break_price, break_mode, trend_before, trend_after, reference_role,
reason, confirmed, confirmed_time, same_candle_conflict`. `reference_swing_id` and `reference_classification` identify the broken swing exactly. `reference_role` is `bootstrap`, `continuation_swing`,
`protected_swing` or `origin_swing`. The Analysis API adds `symbol`, `timeframe`, formatted timestamps/prices and an explanation naming
the reference, break method, prior/resulting direction and why the event qualified. Strategy timeframe events expose
the same event facts with the actual symbol/timeframe. Swings include `id, kind, price, pivot_time, confirmed_time,
classification, confirmed`. All event timestamps and prices come from the input candles.

## Configure
`config/settings.toml` `[structure]`: `swing_left`, `swing_right` (1-50), `break_mode`, `equal_tolerance`.
Per request: `/api/candles?symbol=EURUSD&timeframe=H1&structure=1&swing_left=2&swing_right=2&break_mode=wick`.
In Python: `analyze_market_structure(candles, StructureConfig(...))`. Results are cached (16 entries) because the
same candles are re-requested every few seconds; the engine is O(n * (L+R)).

## Chart
Chart toolbar button (zig-zag icon) or Settings > Candlestick Appearance checkbox toggles the overlay (default off,
saved). Labels HH/HL/LH/LL/EQH/EQL sit at the pivot candle; dashed lines run from the reference pivot to the
breaking candle, labelled BOS or CHoCH. On the Analysis page, expand an event under **Recent structural events** to
see its broken swing, timestamp, close/wick rule, previous/resulting state and engine reason; use **Show on chart** to
inspect its source candles. The overlay is replaced wholesale on every refresh and keyed to the loaded
symbol/timeframe, so nothing stale or duplicated is drawn. It has no hit-targets and is separate from the entry /
SL / TP lines.

## Candlestick appearance
Settings > **Candlestick Appearance** keeps the original nine presets and adds three non-green adaptive choices—Arctic and Rose, Violet and Ember, and Silver and Flame—with
visible colour chips, colour pickers and hex fields (#RGB or #RRGGBB), plus Custom. Changes show immediately and are saved after ~0.3 s.
The three added presets use high-contrast variants for dark and light chart backgrounds. They follow the effective chart theme (application theme
when chart theme is set to `auto`, or the explicitly pinned chart theme). Custom colours and all original preset colours remain unchanged.
- Persisted by the server in `data/chart_settings.json` (same mechanism as `data/risk_rules.json`) via
  `GET/POST /api/chart/settings`. Strict validation, unknown keys rejected, partial updates keep other fields,
  failed updates change nothing. A non-custom preset always has exactly its two colours for the active chart theme; colours matching a preset
  variant exactly select it, otherwise Custom.
- The light/dark theme is a separate `theme` field in the same file (see "Light / Dark theme"). Changing theme updates colors only for one of
  the three added adaptive presets; original presets and Custom colors are preserved.
- Colour rule: close > open bullish, close < open bearish, **close = open (doji) neutral** (theme axis colour).
  Only drawing changes, never the data. SL/TP/P&L colours are unaffected.
- Low contrast against the chart background shows a warning and adds a thin outline round the
  body; your colours are never replaced.

## Light / Dark theme (addendum)
Before this addendum the only theme was chart-only (a chart toolbar button, stored in the browser). The rest of the app
was dark-only and Settings > Theme was a disabled placeholder. It is now one application-wide theme, **and (Phase 7C repair) the chart keeps
its own, independent theme control**:
- **Application switch:** top-bar half-circle button (`role="switch"`, `aria-checked`, keyboard operable) and the Settings > Interface >
  Theme select are the same switch and stay in step. They change the whole application.
- **Chart theme (chart only):** the chart toolbar's moon button and the Settings > *Chart theme (chart only)* select
  (`Same as the application` / `Dark` / `Light`) change only the chart (canvas, candles, grid, panel, toolbar and the zone colours); they never
  touch the application theme. Default `auto` = follow the application theme, so nothing changes until the chart is pinned. Saved as
  `chart_theme` in `data/chart_settings.json` (browser copy `trademate.chartThemeMode`). CSS: `[data-chart-theme="light"]` rules (app dark +
  chart light) and `html[data-theme="light"] [data-chart-theme="dark"]` (app light + chart dark).
- **Mechanism:** `data-theme="light|dark"` on `<html>`; the light palette is CSS variables plus a few overrides in
  `static/css/style.css` ("Application theme" section). Dashboard, chart, trade panel, strategy/risk, trades, history,
  journal, backtest, settings, dialogs and the chart canvas (including structure overlay colours) follow it.
- **Persistence:** `theme` in `data/chart_settings.json` (server, survives refresh/restart/new browser). The browser keeps a
  copy (`trademate.chartTheme`) only so the page paints in the right theme before the server answers. If the server has
  nothing saved yet but the browser remembers a theme from an older version, that choice is migrated, not lost.
- **Adaptive candle palettes:** Arctic and Rose, Violet and Ember, and Silver and Flame follow the effective chart theme. Changing the
  application theme updates them when chart theme is `auto`; changing chart theme updates them when a chart theme is pinned. Manual Custom
  colors and legacy saved palettes remain unchanged.
- **Independence:** chart and application theme controls still change only their respective theme setting; they do not switch trade colors,
  overlays or other preferences. Color selection does not change either theme.
- **Not redesigned:** layout and components are unchanged; dark theme looks as before. Pre-existing low-contrast items in the
  dark theme (version footer, disabled placeholder fields) were left alone.

## Tests
```
tests/run_tests.sh                 # full regression (unit, API, Node, real-browser)
tests/run_tests.sh --phase7a       # Phase 7A only
tests/run_tests.sh --no-browser    # skip Chromium tests
python3 -m unittest tests.test_structure_engine tests.test_structure_api tests.test_chart_settings -v
node tests/chart_style_test.js
```
Browser tests need `pip install playwright && playwright install chromium`; without it they report SKIPPED.
No automated test sends an order; the real-MT5 scripts remain separate opt-in.

## Manual verification (MT5 open)
1. Chart page: toggle the zig-zag button; labels appear/disappear, candles and trade lines unchanged.
2. Spot-check a few HH/LL labels and a BOS/CHoCH line against the candles; change timeframe/symbol: markers follow.
3. Settings > Candlestick Appearance: verify all original presets remain alongside the three additions; choose each added palette, confirm the two colors and candle preview, then reload.
4. Type `#ff0000` in the bullish field: preset becomes Custom, bearish unchanged. Type `zzz`: field marked invalid, nothing saved.
5. With a recommended preset selected and chart theme set to `auto`, switch application theme: verify that the preset identity remains selected and
   the dark/light colors change. Pin the chart theme and switch the application theme again: verify the selected chart palette follows the pin.
   Repeat with Custom colors and verify they remain unchanged. Reload / restart: theme and colors are restored.
6. Drag SL/TP and place a demo trade as usual: unaffected.

## Phase 7A.1 additions

**`trend_confirmed` (engine).** `trend` and `active_*` include a break made by the candle that is still FORMING. The new
`trend_confirmed` is the trend as of the last CLOSED candle and ignores such a developing break. Additive; no rule changed.

**Analysis view model (`trademate/analysis.py`, `GET /api/analysis?symbol=&timeframe=`).** Read-only. Built only from the live
candles of the request. Statements are kept apart: observed facts / rule-based reading / conditions to watch (conditional, never
a prediction) / setups (the Analysis page evaluates none: reported as NOT AVAILABLE; the multi-timeframe Strategy Engine, `docs/MULTI_TIMEFRAME_STRATEGY.md`, is separate). Every event carries an explanation: the level broken,
the breaking candle, close vs wick, and why it is BOS or CHoCH. A CHoCH is never called a confirmed reversal.

Status codes emitted: READY (analysis finished, nothing developing; NOT a trade signal), WATCH (the forming candle shows an
unconfirmed break), STALE DATA (MT5 snapshot stopped updating), UNAVAILABLE (no MT5 data), INVALID DATA (impossible values or
conflicting duplicates: nothing analysed), INSUFFICIENT DATA (too few candles, or no confirmed swing high / low yet).
ANALYSING is browser-only. NO SETUP is reserved and never emitted (it needs a setup engine).

**Clock handling.** The MT5 bridge does not export a broker clock field. TradeMate attempts to measure/reuse the broker offset from fresh
M1 candles (or the configured broker timezone). Analysis and Strategy use that shared resolver to decide whether the newest candle is
closed; if no trustworthy clock is available, the newest candle is treated as forming. Separate requests can still see different broker
snapshots if a candle closes or updates between them.

## Structure audit correction

The audit found a classification defect in the previous methodology: every latest confirmed high/low could be used as a BOS or CHoCH
reference. This allowed a countertrend break of an unprotected pivot to flip direction and allowed a same-direction break of a minor
opposing pivot to be called BOS. Analysis also always treated its last candle as forming and fetched 300 candles, while Strategy used
the broker-clock cutoff and fetched up to 500 H1/H4 candles. The detector itself was shared, but its inputs could differ by one candle
and by older context.

The corrected engine requires the protected HL/LH rules above for CHoCH and HH/EQH or LL/EQL continuation references for directional
BOS. Event records now retain prior/resulting direction, reference role and a reason. Provisional breaks do not consume or modify
confirmed swing references. `trend` may show provisional direction; `trend_confirmed` and the exposed protected/continuation references
remain based on confirmed candles. Strategy consumes that same confirmed structure state and uses protected levels for structural
invalidation. Analysis now uses the shared broker clock and Strategy's H1/H4 fetch history limits.

The deterministic multi-event H1 analogue in `tests/test_structure_regressions.py` is synthetic, not a reconstruction of the user's
CRASH_100 chart. It records every event's timestamp, level, reference swing and classification, prior/resulting direction, confirmation,
role and reason. It specifically exercises bullish CHoCH followed by bullish BOS continuation and then a bearish CHoCH. After a confirmed
bullish CHoCH, the engine's state is bullish: another qualifying bullish break must be BOS, not a second bullish CHoCH, unless an
intervening bearish CHoCH first changes the state back. The prior detector could generate confusing classifications because it used the
latest pivot as a reference in either direction instead of selecting references from the maintained structural state.

The exact live sequence cannot be validated without its timestamped CRASH_100 candle set. Do not infer that the reported second CHoCH
was legitimate or erroneous from the synthetic analogue. Check whether it had a distinct qualifying protected reference, whether an
intervening transition occurred, and whether both events used the same confirmed candles/configuration. The deterministic regression
proves the selected methodology's state transition, not what happened on the user's live chart.

To capture the live diagnostic without placing orders: open Analysis, select **CRASH_100 / H1**, expand each recent event, then open the
browser Network panel and save the read-only GET response for `/api/analysis?symbol=CRASH_100&timeframe=H1`. Also capture
`/api/strategy/mtf?symbol=CRASH_100` and `/api/candles?symbol=CRASH_100&timeframe=H1&count=500&structure=1`. Compare the input cutoff,
`config`, event `id`/`candle_time`, `reference_role`, reference pivot timestamp/price, and `trend_before`/`trend_after`. The API responses
contain market data only; do not attach account/session data or credentials. Requests made at different times can naturally differ when
new candles arrive.

Remaining limitations: no separate internal/external structure engine; the configured pivot width is the single structural scale.
The bootstrap BOS only establishes a starting direction. `undetermined` is not "sideways"; no symbol/session calendar interprets gaps.
A same-candle opposing break remains ambiguous and leaves direction undetermined. Analysis and Strategy requests are not atomic snapshots,
so their timestamps must be checked before attributing a live discrepancy to classification logic.

## CHoCH / BOS audit (exact definitions, shared facts, diagnostic trace)

**Exact definitions (TradeMate).**
- *Previous structural direction*: the direction set by the last confirmed break (`trend_confirmed`); undefined until the first break.
- *BOS*: a confirmed break, in the direction of that structure, of the latest unbroken continuation swing (HH/EQH bullish, LL/EQL
  bearish); or the first qualifying break from an undefined direction (bootstrap, `trend_before = None`).
- *CHoCH*: a confirmed break AGAINST that structure of its protected level: the latest HL (bullish structure) or LH (bearish structure),
  or the origin swing when no HL/LH exists yet. Nothing else is a CHoCH: not a continuation break, not a pullback that does not close
  beyond the level (close mode), and not a break when no direction or no eligible level exists.
- *Meaningful vs minor*: only confirmed pivots at the configured width are structure. Minor fluctuations that are not pivots, and
  pivots of the wrong type for the current direction, are never reference levels.
- *Confirmation*: `break_mode = close` needs a close beyond the level, `wick` needs a high/low beyond it.
- *Duplicates*: every crossed level is consumed, so price staying beyond a level emits nothing more; each swing breaks once per direction.
- *After an event*: direction becomes the event direction; the broken level is consumed; confirmed swings update references.
- *Provisional*: a break by the still-forming candle has `confirmed = false`, changes only `trend` (not `trend_confirmed`) and consumes nothing.
- *Ambiguous / undefined*: with no direction only a bootstrap BOS is possible; a same-candle double break leaves direction undetermined.
- *Internal structure is not supported*: there is one pivot scale (`swing_left` / `swing_right`). No event is labelled internal or external.

**Root causes found.**
1. *Stuck direction (engine).* After a CHoCH the opposing protected level had to be an LH/HL formed later. A rally with no LH left the
   engine "bearish" while price closed far above all old highs, with no event. Fixed by the origin swing.
2. *Two interpretations of the same facts.* The Analysis page turned its direction label into "mixed" whenever the swing pattern was not
   exactly HH+HL / LH+LL, even right after a CHoCH (where the pattern naturally lags), while the Strategy page reported the same candles as
   "bullish/bearish, early". Both now call `trademate/structure/facts.py::structural_facts`; `confirmed_direction`, `quality` (`early`,
   `mixed`, `aligned`, `developing`), `last_confirmed_event` and `provisional` are separate fields. The Analysis `label` is the confirmed
   direction; a forming-candle break is provisional context only (it still makes the OVERALL verdict "mixed").
3. *Stale expectations in the existing H1-analogue test.* It named reference pivots t(16), t(18), t(34) that are not swings at all; the
   engine's t(19), t(21), t(35) are the actual EQL / LH / HL pivots (the same test already asserted those classes). Corrected.

**The reported CRASH_100 sequence is unverified**: the live candles were not available and nothing here reconstructs them. Two
CHoCH events in a row are always opposite directions, each with its own reference swing (bearish through an HL, then bullish through an
LH/origin swing), so "bullish CHoCH, another CHoCH, bullish BOS" is possible only with an intervening bearish CHoCH plus a later bullish
re-bootstrap or a same-candle conflict. Capture the live trace with:

    python3 -m trademate.structure.trace candles.json --left 3 --right 3 --mode close

(`candles.json` = the `candles` list from `/api/candles?symbol=CRASH_100&timeframe=H1&count=500`; add `--forming` if the newest candle is
open). It prints every swing, event, reference swing, previous/resulting direction, confirmation state and reason, plus the final
protected/continuation levels.

**A CHoCH is not SELL and a BOS is not BUY.** Facts carry no decision. The Strategy Engine still requires setup, entry, invalidation,
target, reward-to-risk and Risk Engine approval.
