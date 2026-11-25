# Patch notes: Anti-Twin execution speed fix (v5.2)

* Trigger: first adverse tick (`adverse_move`) replaces return-to-entry; the old rule is an explicit, unselected mode (`break_even_return`).
* New `trademate/antitwin_lane.py` fast lane (Candle Momentum, Bull/Bear); no 0.5 s throttle; close and open in one step.
* Rapid Fire: 0.05 s step while holding, candle fetch deferred, ledger lightened, twin from the actual position.
* Adapter confirmation poll 0.05 s. Twin volume/entry from the real fill; OFF cancels a pending twin.
* Latency stamps (detect, close, confirm, open) in status `anti_twin.latency`; `tests/antitwin_latency.py` measures them.
* Tests: `tests/test_antitwin_speed.py` new; Bull/Bear, Candle Momentum and Rapid Fire anti-twin tests rewritten for the new rule.
* Not changed: EA, MT5 protocol, Risk Engine, Candle Momentum entry logic.
* Recommended EA inputs: UpdateIntervalMs <= 250, CommandPollMs = 100.
