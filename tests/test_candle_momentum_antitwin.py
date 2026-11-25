"""Candle Momentum with the optional Anti-Twin (the same rule as Bull/Bear, its OWN ledger). Simulation only (fake feed, in-memory executor).

    python3 -m unittest tests.test_candle_momentum_antitwin -v
"""

import unittest
from dataclasses import replace
from types import SimpleNamespace

from tests.test_bull_bear import FakeExec
from tests.test_candle_momentum import Rig
from trademate.bull_bear.antitwin import AntiTwin
from trademate.candle_momentum.models import CMConfig, validate_config
from trademate.strategies.base import EXEC_TIMEFRAMES


class CMFeed:
    pass


class CMAntiTwinTest(unittest.TestCase):
    def build(self, enabled=True):
        pos = []
        ex = FakeExec(pos)
        rig = Rig(cfg_over={"anti_twin_enabled": enabled})
        rig.feed.open_positions = lambda: list(pos)
        rig.feed.closed_info = lambda t: None
        rig.engine.anti_twin = AntiTwin(ex, None, rig.events, locked=lambda: False, clock=rig.clock)
        rig.start()
        return rig, ex, pos

    def run_to_position(self, rig, pos):
        rig.step(1.10000)
        rig.step(1.10105)
        lc = rig.engine._slots["EURUSD|M5"].machine.lc
        self.assertEqual(lc["state"], "RISK_PENDING", lc["reason"])
        base = dict(symbol="EURUSD", strategy_id="candle_momentum", exec_tf="M5", candidate_id=lc["candle_id"])
        rig.engine.report_execution({"stage": "proposing", **base})
        rig.engine.report_execution({"stage": "approved", "volume": 0.02, **base})
        rig.engine.report_execution({"stage": "executed", "ticket": 321, "fill_price": 1.1010, **base})
        pos.append(SimpleNamespace(ticket=321, symbol="EURUSD", direction="buy", volume=0.02, price_open=1.1010, stop_loss=1.0960, take_profit=1.1030))

    def cycle(self, rig, ex, bid):
        rig.feed.tick(bid)
        ex.last_quote = rig.feed.quote("EURUSD")
        for _ in range(2):
            rig.clock.advance(0.6)
            rig.engine.step()

    def test_enabled_reverses_on_the_first_adverse_move(self):
        rig, ex, pos = self.build()
        self.run_to_position(rig, pos)
        for bid in (1.1015, 1.1005):                                           # in profit, then the first adverse move (0.5 pip under the open level)
            self.cycle(rig, ex, bid)
        self.assertEqual(ex.names(), ["close", "open"])
        self.assertEqual(ex.calls[1][1:3], ("sell", 0.02))                     # equal volume, opposite side
        self.assertIsNotNone(rig.engine.snapshot("EURUSD", "M5")["anti_twin"])

    def test_a_loss_is_reversed_at_once_and_a_return_to_entry_is_not_awaited(self):
        rig, ex, pos = self.build()
        self.run_to_position(rig, pos)
        self.cycle(rig, ex, 1.1000)                                            # straight down from the entry, never back
        self.assertEqual(ex.names(), ["close", "open"])

    def test_break_even_alone_does_nothing(self):
        rig, ex, pos = self.build()
        self.run_to_position(rig, pos)
        for bid in (1.1015, 1.1010, 1.10095):                                  # back at the entry (and just under it: only the spread)
            self.cycle(rig, ex, bid)
        self.assertEqual(ex.calls, [])

    def test_the_old_half_second_throttle_is_gone(self):
        rig, ex, pos = self.build()
        self.run_to_position(rig, pos)
        rig.feed.tick(1.0990)
        ex.last_quote = rig.feed.quote("EURUSD")
        rig.clock.advance(0.01)                                                # a cycle only 10 ms after the previous one must still judge the quote
        rig.engine.step()
        self.assertEqual(ex.names(), ["close", "open"])

    def test_disabled_by_default_and_when_off(self):
        rig, ex, pos = self.build(enabled=False)
        self.run_to_position(rig, pos)
        for bid in (1.1015, 1.1005, 1.0990):
            self.cycle(rig, ex, bid)
        self.assertEqual(ex.calls, [])
        self.assertFalse(CMConfig("EURUSD", "M5").anti_twin_enabled)

    def test_settings_validate_and_an_old_config_still_loads(self):
        base = CMConfig("EURUSD", "M5")
        self.assertEqual(validate_config(base, EXEC_TIMEFRAMES), [])
        for kw in (dict(at_arm_percent=0), dict(at_tp_mode="custom"), dict(at_max_reversals=9), dict(at_buffer_units=-1)):
            self.assertTrue(validate_config(replace(base, **kw), EXEC_TIMEFRAMES), kw)
        self.assertEqual(CMConfig.from_dict({"trigger_units": 12}, base).at_max_reversals, 1)


if __name__ == "__main__":
    unittest.main()
