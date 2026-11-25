# Two-way stop-loss calculator (Phase 7A.1)

In the trade panel (Chart page). **Preview only**: nothing is sent, nothing is authorised. `/api/order` re-reads the price and the
symbol data and the Risk Engine re-judges the order before any execution, exactly as before.

* **Stop Loss Price** mode: type an SL price, see the money lost if it is hit.
* **Risk Amount** mode: type the money you accept to lose, see the SL price that fits.
* BUY/SELL preview toggle, lot size and the live entry stay visible. Switching modes never overwrites what you typed.

Code: `trademate/risk/sl_calculator.py`, `POST /api/risk/sl`, `static/js/slcalc.js`.

## One calculator, not two

Money at a stop is always `loss_per_lot_at_stop(spec, distance) * volume`: the Risk Engine's own function (broker tick size and
tick value, in ACCOUNT currency). The calculator only adds the inverse (price from money) and the checks around it, so the two modes
and the Risk Engine cannot disagree. No pip values, no price-difference shortcuts: EURUSD, USDJPY, gold, indices, crypto and cross
pairs (loss tick value differing from gain tick value) are all covered by `tests/test_sl_*.py`.

## Rules

* The SL is rounded **toward the entry** on the tick grid, so the loss at it is never more than requested. The result shows the
  requested risk and the final risk as separate numbers, and the difference ("prices move in whole steps").
* BUY: stop below entry. SELL: stop above. Anything else is invalid, never flipped.
* Smaller than one tick of risk, or closer than the broker's minimum stop distance: refused with the minimum risk shown. Risk is never raised for you.
* A typed SL that is off the tick grid is **refused** with the nearest valid stop; what you typed is never changed silently.
* Missing entry / direction / volume / tick size / tick value, a disabled or one-sided symbol, an off-step volume: a reason, no number.
* Entry: your typed entry, or the live price a market order would use (BUY = ask, SELL = bid).
* Amounts are labelled: "risk amount" = money lost at the stop, in the account currency (never assumed to be NGN or USD). It is not
  allocated capital and not notional exposure.
* The per-trade limit is judged and shown ("within" / "above ... the Risk Engine would reject this"), but the Risk Engine enforces it at submission.
* Stale answers are dropped (request ids), requests are debounced, and the number you see is hidden ("Recalculating") the moment an input changes.
* In Risk mode, BUY/SELL only sends if the stop was freshly calculated for that same side and still matches. Dragging the SL on the
  chart switches the panel to Stop Loss Price mode.
