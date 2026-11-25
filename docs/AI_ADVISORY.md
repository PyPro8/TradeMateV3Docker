# AI-assisted Candle Momentum: investigation and decision

**Decision: no AI layer is enabled, and none is in the execution path.** Deterministic Candle Momentum is unchanged and remains the only thing that produces signals.

## What was investigated

Whether an optional, advisory AI layer could help Candle Momentum with: candle-group direction, recent candle behaviour, momentum, market structure, potential reversals, temporary opposing candles, and directional exhaustion.

## Why nothing was switched on

The requirement is that AI may influence real entries only after a **measured** improvement over the baseline on historical data and controlled demo trading. That measurement is not possible from this project as delivered:

* There is no recorded set of Candle Momentum signals with outcomes in the project, and no live broker in the build environment, so a baseline-vs-advisory comparison cannot be run honestly.
* Any AI output would have to be checked against prices TradeMate itself supplied; an AI must not invent prices or market data.
* Therefore the advisory layer stays a design, not code, and cannot affect an order.

## Guarantees that are enforced (and tested)

* No module on the execution path (engine, pipeline, Risk Engine, `app.py`) imports an AI client.
* Every approval goes through `evaluate_trade()`; an AI feature would have no other route.
* Candle Momentum's signal logic is untouched and has no dependency on the moving average, on CHoCH or on Market Structure.

## How a future experiment must work (acceptance criteria)

1. Advisory only: it adds a note beside a signal; it never blocks, delays or changes one.
2. It receives only TradeMate's own completed candles and computed structure; it returns text/labels, never prices.
3. Record every signal, the advisory label and the outcome for the same signals.
4. Compare baseline and advisory-labelled outcomes on historical data, then on a demo account, over a pre-agreed number of signals with the net result, win rate and worst losing streak reported side by side.
5. Only if the advisory label separates better from worse outcomes by a margin agreed in advance, consider letting it **reduce** exposure (never raise it, never bypass the Risk Engine). Do not reject every signal that disagrees with it.
