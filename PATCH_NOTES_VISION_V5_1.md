# TradeMate Vision v5.1 patch notes

## Autopilot worker restart fix

Fixed a worker-retirement race in `trademate/autopilot.py`. When a stopped Autopilot worker was still technically alive while a new session was started, `_spawn()` could decline to create a worker; the old worker could then exit, leaving the new session armed without a worker. The worker now clears its own thread reference atomically under the same lock used by start/spawn before retiring, and also cleans up its reference on shutdown/unexpected exit. Existing open-trade tracking, persisted state, and risk checks are unchanged.

Added regression tests for stop/start worker lifecycle and idle-worker retirement.

## Anti-Twin source audit

No Anti-Twin strategy change was needed. The existing implementation is active code, not a decorative switch. When enabled, it watches each position from its broker-confirmed fill price. After executable price first departs that entry (favourably, or sufficiently adversely to clear the opening spread plus one tick), a return to or cross of the entry triggers a close. The opposite equal-volume position is opened only after the original close is confirmed and only through the normal Risk Engine gate. A normal TP/SL/manual close does not trigger a twin. Emergency stop, duplicate prevention, bounded retries, and a fixed 20-reversal chain ceiling are implemented.

The behaviour is covered by simulation tests; live broker execution has not been verified by this patch.
