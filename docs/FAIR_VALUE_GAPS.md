# Phase 7C: Fair Value Gap (FVG) detection engine

Analysis only. Nothing here places an order, sets a stop or target, sizes a position, or talks to the Risk / Execution engines.
Engine: `trademate/fvg/engine.py` (pure Python; imports only `trademate.structure.clean_candles` from the project). Chart overlay
(task 2): `/api/candles?fvg=1` -> `static/js/fvg.js` -> `drawFVG` in `static/js/app.js`. Analysis page (Phase 7C repair): the `fvg` view of
`/api/analysis` (`build_fvg_view`), shown in the *Fair value gaps* panel; see `docs/ANALYSIS.md`.

## Rules

Three consecutive candles c1, c2, c3 (oldest first), after the structure engine's candle cleaning:

| | condition | lower | upper |
|---|---|---|---|
| bullish | `c3.low > c1.high` | c1.high | c3.low |
| bearish | `c3.high < c1.low` | c3.high | c1.low |

Comparisons are **strict**: touching boundaries are not a gap. c2 (the displacement candle) is not tested at all, and a detected gap is
not graded: there is no quality score, size filter, or "valid setup" idea. Every window of three consecutive candles is examined, so
overlapping windows can each report a gap. A gap is an observation about three candles, not a forecast that price will return,
react or reverse.

Each `FairValueGap` carries: `id` (`FVG-BULL|BEAR-<c1 epoch seconds>`), `direction`, `upper`, `lower`, `size`, `first_time`,
`middle_time`, `third_time`, `formed_time` (= `third_time`: the gap is only knowable once c3 exists), `first/middle/third_index`
(positions in the cleaned, time-sorted list), `confirmed`, `confirmed_time`, `symbol`, `timeframe`.

## Using it

```python
from trademate.fvg import analyze_fvg, FVGConfig
res = analyze_fvg(candles, FVGConfig(last_candle_closed=False), symbol="EURUSD", timeframe="M5")
res.data_status      # "ok" | "insufficient" | "invalid"   (res.reason says why)
res.gaps             # tuple of FairValueGap, oldest first
res.to_dict()        # JSON-ready
```

`candles` are `[time, open, high, low, close]` rows, mappings with those keys, or objects with those attributes (the same as the
structure and liquidity engines; the project's `broker.base.Candle` works). `symbol` / `timeframe` are optional labels copied onto the
result; candles carry neither and they never affect detection. Only a bad `FVGConfig` or a non-text label raises `ValueError`; bad
candles never raise.

## Data handling

* **Duplicates / identical re-runs:** the result is a pure function of the candles. Same data twice, repeated rows, or a series that grows
  on every refresh give the same ids, each gap once. Key on `id` if you keep gaps between refreshes. Nothing earlier is rewritten when
  candles are appended (tested for every prefix).
* **Ordering:** out-of-order input is sorted by time and flagged in `diagnostics.out_of_order`; direction is never taken from the order
  the rows were supplied. Duplicate timestamps keep the last row.
* **Fail closed:** an invalid candle (missing / non-numeric / non-finite value, high < low, open or close outside the range), a price of
  zero or below, or two rows with the same time but different prices gives `data_status="invalid"` and **no gaps**. Dropping such a candle
  would make its neighbours look adjacent and could fabricate a gap, so nothing is guessed. Fewer than 3 usable candles gives
  `"insufficient"`. Missing values are never filled in.
* **Forming candle:** the default `last_candle_closed=False` assumes the newest candle is still forming (the bridge exports no
  completion flag or broker clock; the Analysis page makes the same assumption). A gap whose c3 is that candle has `confirmed=False`,
  `confirmed_time=None`, and can change or vanish when the candle closes. Pass `True` only when the newest candle is known to be closed.

## On the chart (Phase 7C task 2)

* **Data flow:** `GET /api/candles?...&fvg=1` adds an `fvg` block: the engine's own result for exactly those candles (`analyze_fvg` with
  `last_candle_closed=False`, symbol and timeframe as labels) plus `available` (true only when `data_status == "ok"`). It is independent
  of `structure=1`, and the server never looks at the saved visibility. The page always asks for it, so the data is held whether or not
  the zones are shown. If the engine reports insufficient / invalid candles (or fails), `available` is false, no gap is returned and
  nothing is drawn; the candles still load.
* **Drawing:** `static/js/fvg.js` only maps the engine's `first_time` / `third_time` to candle indices and passes `upper` / `lower`
  through unchanged (it never reads a candle's price). A gap whose times are not among the loaded candles, or whose numbers are not
  finite with `upper > lower`, is dropped. The zone is a box from candle 1 to candle 3 drawn behind the candles: blue = bullish,
  purple = bearish (also written as "Bull FVG" / "Bear FVG" when wide enough); `confirmed` = solid, otherwise dashed, fainter and
  "(forming)". The whole set is replaced on every refresh, and data for another symbol / timeframe is never drawn.
* **Controls and persistence:** see `README.md` (Phase 7C). The preference is `fvg_visible` in `data/chart_settings.json`
  (`trademate/chart_settings.py`), default false, validated like the other overlay switches.
* **Hiding vs analysing:** `fvg_visible` only gates drawing. Structure and FVG data are requested on every chart refresh regardless.
  Liquidity keeps its Phase 7B behaviour (an existing test pins it): the chart asks for it only while its overlay is shown; the
  Analysis page computes it regardless of any chart switch.
* **Diagnostics hook:** `TradeMateApp.fvgFrame()` returns what each chart last drew (rectangles in screen coordinates, the price range
  and plot box used), `analysisData()` the analysis results currently held, `overlays()` the four switches (structure, liquidity, fvg, order_blocks). Read-only; used by tests.

## Limitations (deliberate or known)

* A zone covers only the three candles that form the gap. Fills (mitigation) are not tracked by the ENGINE or the chart (the Analysis page derives a fill status from the later candles; see `docs/ANALYSIS.md`), so later price touching, entering or
  closing a gap is not shown or implied, and zones are not extended to the right. Many gaps on a long, busy chart can still overlap.
* Zones are only drawn for the (up to 300) candles the chart loads. There is no click / hover detail and no list of gaps yet.

* No minimum gap size, quality/strength score, fill (mitigation) tracking, expiry, merging of adjacent gaps or multi-timeframe logic:
  no justified specification exists yet.
* "Consecutive" means consecutive in the supplied data. Time gaps (weekends, a missing bar) are not detected because the engine knows no
  timeframe length; the caller must supply a contiguous series.
* One bad candle blanks the whole result (fail-closed policy above), rather than detecting around it.
* Prices <= 0 are rejected as impossible. An instrument that can legitimately trade at or below zero (e.g. a spread) is not supported.
* No Analysis-page entry, mitigation tracking or per-gap detail view exists yet.

Tests: `python3 -m unittest tests.test_fvg_engine -v`.
