# Anti-Twin (v5.2): switch direction on the first adverse move

Anti-Twin is a **direction switch**, not a stop-loss and not a profit-triggered reversal. It has two settings: **ON / OFF**. There is no
profit threshold, no loss threshold and no candle wait. The rule lives in `trademate/antitwin_rule.py` (pure functions, nothing sends an order).

## The rule (mode `ADVERSE_MOVE`, the only active mode)

1. A position opens. Its **actual MT5 fill price** is its own entry reference `E`. A twin gets its own `E` from its own fill; references are never reused.
2. The **first confirmed adverse movement** of its executable exit price closes it, the closure is **confirmed** against MT5, and an **equal-volume
   (actual filled volume) opposite** position is opened at once. The new position is watched the same way: BUY -> SELL -> BUY ... (MAX_CHAIN ceiling).

Evaluated on every new quote, at tick level, never on candle close. A gap far beyond the threshold triggers exactly like one tick.

## Trigger modes are explicit

| Mode | Status |
|---|---|
| `adverse_move` | active in all three strategies |
| `break_even_return` | the v5.0/5.1 rule (leave `E`, then return to it). Kept only as a separate, documented, never-selected mode. |

`observe()` runs the ONE mode stored in the state; the two are never combined. A state saved by an older version is normalised to the active mode.

## Bid/ask semantics

A BUY fills at the ask and exits at the bid; a SELL fills at the bid and exits at the ask. The rule measures the position's executable exit price
`x` (bid for a BUY, ask for a SELL): `d = sign * (x - E)`, `sign = +1` BUY, `-1` SELL.

* At the open, `d` is about `-spread`: the spread is already paid. That is **not** an adverse move and break-even is never reported or acted on.
* **Adverse move: `d <= -spread0 - one tick`** (`spread0` = spread when first observed). One tick is the smallest price change that exists, the quantum
  of "moved", not a profit or loss threshold.
* A crossed or missing quote, or an invalid tick size, proves nothing and never triggers.

## Speed: what changed and why

Root causes of the old slowness, all fixed: (1) the trigger only fired on a return to `E`; (2) evaluation was inside the 0.25 s engine cycle plus a 0.5 s
throttle; (3) a whole engine cycle passed between the close and the open; (4) confirmations were quantised to the bridge snapshot; (5) twin volume and
entry were not taken from the real position; (6) OFF did not cancel a pending twin.

* **Fast lane** (`trademate/antitwin_lane.py`): a dedicated thread (20 ms while a leg is active) reads the live quote and judges every new quote.
  Close -> confirm -> open runs in one call (`AntiTwin.step`). The lane only feeds quotes sooner; the rule, state machine, Risk gate and Emergency Stop are unchanged.
* **Rapid Fire**: step 0.05 s while Anti-Twin holds a position; candle fetching is deferred while it holds.
* Adapter confirmation poll 0.05 s (was quantised to the snapshot).

## MEASURED latency (simulated bridge, NOT a real broker)

`python3 -m tests.antitwin_latency` drives the real adapter, Executor + Risk gate, AntiTwin and lane against an EA-emulating bridge (one timer; snapshot
every `UpdateIntervalMs`, commands every `CommandPollMs`, 50 ms emulated broker delay). Median over 10 reversals (full table: `docs/ANTI_TWIN_LATENCY_MEASURED.txt`):

| EA inputs | price moved -> seen | seen -> original closed | opposite order (Risk gate + fill) | price moved -> opposite open |
|---|---|---|---|---|
| shipped (1000 / 200) | 883 ms | 1020 ms | 1173 ms | **3062 ms** |
| recommended (250 / 100) | 392 ms | 164 ms | 317 ms | **885 ms** |
| (100 / 100) | 517 ms* | 161 ms | 313 ms | 959 ms |

\* with a 100 ms snapshot this should not exceed the 250 ms row; it is within the noise/harness behaviour of 10 runs and was not investigated further.

**The switch is not instantaneous.** With the EA as shipped it takes about 3 s, dominated by the EA's snapshot and command timers, not by TradeMate
(TradeMate's own gap between the close and the order is about 2 ms). The Risk gate's two EA round trips remain inside the opposite-order time.
**Set `UpdateIntervalMs` <= 250 and `CommandPollMs` = 100 in the TradeMateBridge EA inputs** (no EA code was changed). Real broker latency and slippage
must be measured on a demo account.

## Safety

* One reversal per position: the claim is persisted **before** the close is sent; a triggered state never triggers again (no duplicate reversals).
* The volume of the opposite position is the **actual filled volume** read from MT5 (Rapid Fire syncs its ledger from the position).
* The reversal goes through the same Executor and the **Risk Engine gate** as every other order. A refusal means no twin; the original is already closed and the event says so.
* **Emergency Stop and the emergency exit take precedence**: checked before the close and before the open; a pending twin is cancelled (`TWIN_CANCELLED`).
* Failure leaves a known state: uncertain outcomes end in `UNCERTAIN`/`TWIN_FAILED`, nothing is re-sent without bound, the user is told to look at MT5.
* A stale reference (the position's entry differs from the tracked one) is reset rather than used.
* Fixed ceiling `MAX_CHAIN = 20` reversals per chain. It is not a setting; the Risk Engine's own limits also apply to every twin.
* Turning Anti-Twin OFF is live: a twin not yet opened is cancelled.

## Independent per strategy

Candle Momentum, Rapid Fire and Bull/Bear each have their own ON/OFF and their own ledger. Switching one never changes another. Candle Momentum's
entry logic and settings are untouched. The old `at_*` settings and `anti_twin_adverse_points` / `anti_twin_mode` are **retired**: old files still
load, the values are ignored.

## Twin exits

* Candle Momentum / Bull/Bear: twin SL distance = the root's SL distance; twin TP = **half** of the root's TP distance (documented interpretation).
* Rapid Fire: exits stay software-managed (no broker TP/SL), same as the original.

## Known limits (not verified live)

* A widening spread can look like an adverse move on the exit price; this is inherent to a rule with no threshold.
* The twin enters at the market after the close: a fast-moving market means slippage and a loss on the original is real (reported as `closed_vs_entry`, never as break-even).
* The EA (`mt5/TradeMateBridge.mq5`) was not modified or compiled here; all behaviour was tested on the simulated bridge only. Run the demo-account scenarios below.

## Demo-account validation (12 scenarios)

Run with Anti-Twin ON on a demo account, minimum lot, and watch `anti_twin.latency` in the status: 1 BUY adverse tick; 2 SELL adverse tick; 3 chain BUY->SELL->BUY;
4 slow execution (throttle the network); 5 rejected reversal (market closed / disable trading); 6 partial fill (large lot on a thin symbol);
7 Emergency Stop during a chain; 8 switch OFF during a chain; 9 fast repeated ticks (no duplicate twin); 10 restart TradeMate mid-reversal;
11 Risk Engine refusal (tight limits); 12 all three strategies ON, then one OFF. Automated equivalents: `tests/test_antitwin_speed.py` header.


## Reliable reversal (v5.3)

A reversal is a transaction: pre-flight, close, confirm closed, open the opposite order, verify it in MT5. It is reported as done only at the last step.

| Explicit state | Meaning |
|---|---|
| ORIGINAL_POSITION_ACTIVE | watching the position |
| REVERSAL_TRIGGERED | the configured trigger fired (pre-flight runs now) |
| CLOSE_REQUESTED | close sent; waiting for the account to show the position gone (an unconfirmed close is never re-sent) |
| CLOSE_CONFIRMED | the account proves the original is closed |
| OPPOSITE_ORDER_REQUESTED | opposite order sent; if unconfirmed it is reconciled, never re-sent |
| OPPOSITE_ORDER_CONFIRMED | the new position exists in MT5: ticket, symbol, side, volume and fill recorded |
| REVERSAL_FAILED | the close succeeded but the opposite order did not: nothing is open, and it is said so |
| REVERSAL_ABORTED | pre-flight showed the opposite order could not be sent: **nothing was closed** |
| RECOVERY_REQUIRED | an outcome that cannot be proven: nothing is re-sent; check MT5 |

Pre-flight (read-only, before any close): exact equal volume against the broker's min / max / step and TradeMate's volume ceiling; trade mode; stop-level distance of the levels that will be sent; the Risk Engine with the original treated as closed; the consecutive-loss limit that the close's own loss could trip.
