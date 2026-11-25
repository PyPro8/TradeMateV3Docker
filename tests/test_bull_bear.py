"""Bull/Bear v3 (structure-gated Candle Momentum copy) and its Anti-Twin.

    python3 -m unittest tests.test_bull_bear -v

SAFETY: simulation only. The engine runs on tests/fake_mt5_bridge.py FakeFeed (no order method exists there); Anti-Twin runs against an in-memory executor that
records the calls it receives. Nothing here can place a real order, and passing proves the rules and wiring in simulation, NOT live-broker behaviour.
"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from tests.fake_mt5_bridge import FakeClock, FakeFeed
from tests.strategy_fixtures import CFG, h1_up, m5_choch_down, m5_choch_up
from trademate.bull_bear import structure as bbs
from trademate.bull_bear.antitwin import (ARMED, AWAITING_CLOSE, CLOSING, EMERGENCY_CANCELLED, FAILED, LIMIT_REACHED, SL_HIT, TP_HIT, TP_TOUCHED, TWIN_OPENED,
                                          UNCERTAIN, REFUSED_ST, WATCHING, AntiTwin)
from trademate.bull_bear.diagnostics import EventLog
from trademate.bull_bear.engine import BullBearEngine
from trademate.bull_bear.models import BBConfig, validate_config
from trademate.bull_bear.store import ConfigStore, StateStore
from trademate.candle_momentum.feed import FeedError
from trademate.config import Settings
from trademate.decision_hub import DecisionHub
from trademate.rapid_fire.executor import EXECUTED, NOT_EXECUTED, REFUSED, Outcome
from trademate.strategies.base import EXEC_TIMEFRAMES

PIP = 0.0001
BULL, BEAR = "bullish", "bearish"


def choch(direction, t=1000, ok=True, **kw):
    ev = {"type": "CHOCH", "direction": direction, "level": 1.1, "candle_time": t, "confirmed": ok, "same_candle_conflict": False,
          "reference_pivot_confirmed_time": t - 300}
    ev.update(kw)
    return ev


def bos(direction, t):
    return {"type": "BOS", "direction": direction, "level": 1.1, "candle_time": t, "confirmed": True, "same_candle_conflict": False, "reference_pivot_confirmed_time": t - 300}


# ============================================================================================================== structure state (pure)
class StructureStateTest(unittest.TestCase):
    def test_no_choch_means_no_direction_and_no_entry(self):
        for ev in ([], [bos(BULL, 100)], [choch(BULL, ok=False)], [choch(BULL, same_candle_conflict=True)], [choch(BULL, reference_pivot_confirmed_time=2000)]):
            st = bbs.structure_from_events(ev)
            self.assertEqual(st["state"], bbs.NO_CHOCH, ev)
            self.assertIsNone(st["direction"])
            self.assertFalse(bbs.gate(BULL, st)["ok"])
            self.assertFalse(bbs.gate(BEAR, st)["ok"])
            self.assertEqual(bbs.gate(BULL, st)["code"], bbs.NO_CHOCH)

    def test_bullish_choch_then_bullish_candle_is_allowed(self):
        st = bbs.structure_from_events([choch(BULL)])
        self.assertEqual((st["state"], st["direction"]), (bbs.ESTABLISHED, BULL))
        self.assertTrue(bbs.gate(BULL, st)["ok"])

    def test_bearish_choch_then_bearish_candle_is_allowed(self):
        st = bbs.structure_from_events([choch(BEAR)])
        self.assertEqual(st["direction"], BEAR)
        self.assertTrue(bbs.gate(BEAR, st)["ok"])

    def test_candle_against_the_structure_is_never_allowed(self):
        self.assertEqual(bbs.gate(BULL, bbs.structure_from_events([choch(BEAR)]))["code"], bbs.NOT_ALIGNED)
        self.assertEqual(bbs.gate(BEAR, bbs.structure_from_events([choch(BULL)]))["code"], bbs.NOT_ALIGNED)
        self.assertFalse(bbs.gate(None, bbs.structure_from_events([choch(BULL)]))["ok"])               # a doji / undecided candle

    def test_a_bos_after_the_choch_keeps_the_direction(self):
        st = bbs.structure_from_events([choch(BULL, 1000), bos(BULL, 1300), bos(BULL, 1600)])
        self.assertEqual((st["direction"], st["choch_time"]), (BULL, 1000))

    def test_a_new_opposing_choch_invalidates_the_old_direction(self):
        st = bbs.structure_from_events([choch(BULL, 1000), bos(BULL, 1300), choch(BEAR, 1600)])
        self.assertEqual((st["direction"], st["previous_direction"], st["reversals"]), (BEAR, BULL, 1))
        self.assertFalse(bbs.gate(BULL, st)["ok"])
        self.assertTrue(bbs.gate(BEAR, st)["ok"])
        back = bbs.structure_from_events([choch(BULL, 1000), choch(BEAR, 1600), choch(BULL, 1900)])
        self.assertEqual((back["direction"], back["reversals"]), (BULL, 2))

    def test_an_invalid_opposing_choch_does_not_invalidate(self):
        st = bbs.structure_from_events([choch(BULL, 1000), choch(BEAR, 1600, ok=False)])
        self.assertEqual(st["direction"], BULL)

    def test_the_real_structure_engine_establishes_the_direction_from_candles(self):
        from tests.strategy_fixtures import CLOCK
        for rows, want in ((m5_choch_up(), BULL), (m5_choch_down(), BEAR)):
            st = bbs.analyse(rows, "M5", rows[-1][0] + 330, CFG, "TEST")
            self.assertEqual(st["direction"], want, st)

    def test_too_little_data_is_no_direction_never_a_guess(self):
        st = bbs.analyse(m5_choch_up()[:5], "M5", None, CFG, "TEST")
        self.assertEqual(st["state"], bbs.NO_CHOCH)
        self.assertFalse(bbs.gate(BULL, st)["ok"])


# ============================================================================================================== the engine (Candle Momentum lifecycle + gate)
class BBFeed(FakeFeed):
    """FakeFeed + what the Bull/Bear engine adds to the feed interface. `struct` is the scripted market structure."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.struct = bbs.structure_from_events([])
        self.struct_calls = 0
        self.pos_list = []

    def structure(self, symbol, timeframe, cfg, now_server=None):
        self.struct_calls += 1
        return self.struct

    def open_positions(self):
        return list(self.pos_list)

    def closed_info(self, ticket):
        return self.history.get(ticket)


class Rig:
    def __init__(self, struct=None, cfg_over=None, anti_twin=None):
        self.clock = FakeClock()
        self.feed = BBFeed(self.clock)
        if struct is not None:
            self.feed.struct = bbs.structure_from_events(struct)
        self.events = EventLog(None)
        self.cfg_store = ConfigStore(None, EXEC_TIMEFRAMES)
        self.engine = BullBearEngine(self.feed, Settings(), self.cfg_store, StateStore(None, self.clock), self.events, hub=DecisionHub(clock=self.clock), clock=self.clock,
                                     threaded=False, anti_twin=anti_twin)
        if cfg_over:
            base, _ = self.engine.effective_config("EURUSD", "M5")
            self.cfg_store.save(BBConfig.from_dict(cfg_over, base))
        ok, why = self.engine.start("EURUSD", "M5")
        assert ok, why
        self.feed._candle("EURUSD", "M5")

    def step(self, bid=None, dt=0.25):
        if bid is not None:
            self.feed.tick(bid)
        self.clock.advance(dt)
        self.engine.step()

    @property
    def slot(self):
        return self.engine._slots["EURUSD|M5"]

    def lc(self):
        return self.slot.machine.lc

    def decision(self):
        return self.engine.result("EURUSD", "M5")["decision"]

    def run_to_trigger(self, direction):
        self.step(1.10000)
        self.step(1.10000 + (10 if direction == BULL else -10) * PIP)


class BullBearEntryTest(unittest.TestCase):
    def test_bullish_choch_then_a_bullish_candle_produces_a_buy(self):
        r = Rig([choch(BULL)])
        r.run_to_trigger(BULL)
        self.assertEqual(r.lc()["state"], "RISK_PENDING", r.lc()["reason"])
        self.assertEqual(r.decision(), "BUY")

    def test_bearish_choch_then_a_bearish_candle_produces_a_sell(self):
        r = Rig([choch(BEAR)])
        r.run_to_trigger(BEAR)
        self.assertEqual(r.lc()["state"], "RISK_PENDING", r.lc()["reason"])
        self.assertEqual(r.decision(), "SELL")

    def test_a_bullish_candle_in_bearish_structure_is_not_traded(self):
        r = Rig([choch(BEAR)])
        r.run_to_trigger(BULL)
        self.assertNotIn(r.lc()["state"], ("RISK_PENDING", "EXECUTING", "TRIGGER_REACHED"))
        self.assertIn(r.decision(), ("WATCH", "WAIT"))
        self.assertIn("structure", r.lc()["block_reason"])
        self.assertEqual(r.lc()["gate"]["code"], bbs.NOT_ALIGNED)

    def test_a_bearish_candle_in_bullish_structure_is_not_traded(self):
        r = Rig([choch(BULL)])
        r.run_to_trigger(BEAR)
        self.assertNotIn(r.decision(), ("BUY", "SELL"))
        self.assertEqual(r.lc()["gate"]["code"], bbs.NOT_ALIGNED)

    def test_no_entry_before_a_choch(self):
        r = Rig([])
        for d in (BULL, BEAR):
            r2 = Rig([])
            r2.run_to_trigger(d)
            self.assertNotIn(r2.decision(), ("BUY", "SELL"))
            self.assertEqual(r2.lc()["gate"]["code"], bbs.NO_CHOCH)
            self.assertIn("CHoCH", r2.lc()["block_reason"])

    def test_structure_unavailable_means_no_entry(self):
        r = Rig()
        r.feed.struct = bbs.unavailable("test data outage")
        r.run_to_trigger(BULL)
        self.assertNotIn(r.decision(), ("BUY", "SELL"))

    def test_a_new_opposing_choch_invalidates_the_old_direction_mid_candle(self):
        r = Rig([choch(BULL)])
        r.step(1.10000)
        r.step(1.10030)
        self.assertEqual(r.lc()["state"], "BULLISH")
        self.assertTrue(r.lc()["gate"]["ok"])
        r.slot.machine.structure = bbs.structure_from_events([choch(BULL, 1000), choch(BEAR, 1600)])      # the engine refreshes this on a candle boundary
        r.step(1.10040)
        self.assertFalse(r.lc()["gate"]["ok"])
        self.assertIsNone(r.lc()["trigger_price"])                                                         # the trigger is withdrawn
        r.step(1.10200)                                                                                    # price runs far past the old trigger: still nothing
        self.assertNotIn(r.decision(), ("BUY", "SELL"))

    def test_after_the_reversal_the_new_direction_trades(self):
        r = Rig([choch(BULL)])
        r.step(1.10000)
        r.step(1.09970)                                                                                    # a bearish candle in bullish structure: blocked
        self.assertFalse(r.lc()["gate"]["ok"])
        r.slot.machine.structure = bbs.structure_from_events([choch(BULL, 1000), choch(BEAR, 1600)])
        r.step(1.09960)
        r.step(1.09900)                                                                                    # trigger 10 units below the open
        self.assertEqual(r.decision(), "SELL")

    def test_a_signal_in_flight_is_withdrawn_if_the_structure_flips_before_the_order(self):
        r = Rig([choch(BULL)])
        r.run_to_trigger(BULL)
        self.assertEqual(r.lc()["state"], "RISK_PENDING")
        m = r.slot.machine
        m.structure = bbs.structure_from_events([choch(BULL, 1000), choch(BEAR, 1600)])
        m.lc["state"] = "ENTRY_VALIDATION"                                                                 # re-validation (a bounded retry) judges the CURRENT structure
        m.on_quote(r.feed.quote("EURUSD"), r.slot.spec, r.clock())
        self.assertEqual(r.lc()["state"], "INVALIDATED")
        self.assertNotIn(r.decision(), ("BUY", "SELL"))

    def test_structure_is_read_on_a_candle_boundary_not_on_every_tick(self):
        r = Rig([choch(BULL)])
        for i in range(8):
            r.step(1.10000 + i * 0.00001)
        self.assertLessEqual(r.feed.struct_calls, 2)

    def test_the_old_candle_group_rule_is_gone(self):
        import inspect
        from trademate.strategies import bull_bear
        self.assertNotIn("group_direction", inspect.getsource(bull_bear))


class BullBearConfigTest(unittest.TestCase):
    def test_candle_momentum_compatible_settings_and_defaults(self):
        c = BBConfig("EURUSD", "M5")
        for name in ("trigger_units", "target_units", "sl_buffer_units", "max_slippage_units", "signal_expiry_seconds", "quote_max_age_seconds", "max_entry_attempts", "retry_delay_seconds"):
            self.assertTrue(hasattr(c, name), name)
        self.assertFalse(c.anti_twin_enabled)                                           # off unless the user turns it on
        self.assertEqual(validate_config(c, EXEC_TIMEFRAMES), [])

    def test_old_saved_config_without_anti_twin_keys_still_loads(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bull_bear_config.json"
            p.write_text('{"version": 1, "configs": {"EURUSD|M5": {"trigger_units": 12, "target_units": 30, "sl_buffer_units": 40}}}')
            store = ConfigStore(p, EXEC_TIMEFRAMES)
            cfg, custom = store.get(BBConfig("EURUSD", "M5"))
            self.assertEqual((cfg.trigger_units, cfg.target_units), (12, 30))
            self.assertFalse(cfg.anti_twin_enabled)
            self.assertEqual(cfg.at_max_reversals, 1)

    def test_validation_of_the_anti_twin_settings(self):
        base = BBConfig("EURUSD", "M5")
        bad = [dict(at_arm_percent=0), dict(at_arm_percent=100), dict(at_tp_mode="x"), dict(at_tp_mode="custom"), dict(at_tp_mode="custom", at_tp_units=-1),
               dict(at_max_reversals=0), dict(at_max_reversals=6), dict(at_max_reversals=True), dict(at_commission_units=-1), dict(at_slippage_units=float("nan")),
               dict(anti_twin_enabled="yes")]
        for kw in bad:
            self.assertTrue(validate_config(replace(base, **kw), EXEC_TIMEFRAMES), kw)
        for kw in (dict(at_tp_mode="custom", at_tp_units=15), dict(at_max_reversals=5), dict(at_arm_percent=75.5), dict(anti_twin_enabled=True)):
            self.assertEqual(validate_config(replace(base, **kw), EXEC_TIMEFRAMES), [], kw)

    def test_anti_twin_settings_persist_across_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            cfg = replace(BBConfig("EURUSD", "M5"), anti_twin_enabled=True, at_tp_mode="custom", at_tp_units=12.0, at_max_reversals=3)
            self.assertEqual(ConfigStore(p, EXEC_TIMEFRAMES).save(cfg), [])
            got, _ = ConfigStore(p, EXEC_TIMEFRAMES).get(BBConfig("EURUSD", "M5"))
            self.assertEqual((got.anti_twin_enabled, got.at_tp_mode, got.at_tp_units, got.at_max_reversals), (True, "custom", 12.0, 3))

    def test_candle_momentum_and_bull_bear_configs_do_not_share_a_file_or_a_key_space(self):
        from trademate.hard_reset import RESET_FILES
        self.assertIn("bull_bear_config.json", RESET_FILES)
        self.assertIn("candle_momentum_config.json", RESET_FILES)
        from trademate.bull_bear import logic as bl
        from trademate.candle_momentum import logic as cl
        self.assertNotEqual(bl.make_candle_id("EURUSD", "M5", 1000, 1.1), cl.make_candle_id("EURUSD", "M5", 1000, 1.1))


# ============================================================================================================== Anti-Twin
class FakeExec:
    """Stands in for the central executor (Risk gate + journaled adapter). Records every call, and plays the account: close removes the position, open adds one."""

    def __init__(self, positions, close_kind=EXECUTED, open_kind=EXECUTED, remove_on_close=True):
        self.positions, self.calls = positions, []
        self.close_kind, self.open_kind, self.remove_on_close = close_kind, open_kind, remove_on_close
        self.next_ticket = 900
        self.last_quote = None

    def close(self, ticket):
        self.calls.append(("close", ticket))
        if self.close_kind == EXECUTED and self.remove_on_close:
            self.positions[:] = [p for p in self.positions if p.ticket != ticket]
        return Outcome(self.close_kind, "close " + self.close_kind, retryable=self.close_kind == NOT_EXECUTED)

    def open(self, symbol, side, volume, reference_stop, sl=None, tp=None):
        self.calls.append(("open", side, volume, sl, tp))
        if self.open_kind != EXECUTED:
            return Outcome(self.open_kind, "open " + self.open_kind, retryable=self.open_kind == NOT_EXECUTED)
        self.next_ticket += 1
        price = self.last_quote.bid if side == "sell" else self.last_quote.ask
        self.positions.append(SimpleNamespace(ticket=self.next_ticket, symbol=symbol, direction=side, volume=volume, price_open=price, stop_loss=sl, take_profit=tp))
        return Outcome(EXECUTED, "open", self.next_ticket, price)

    def names(self):
        return [c[0] for c in self.calls]


class ATRig:
    SPEC = FakeFeed(FakeClock()).spec("EURUSD", {}, 0)

    def __init__(self, side="buy", volume=0.03, locked=False, path=None, **cfg):
        self.clock = FakeClock()
        self.locked = locked
        self.pos = SimpleNamespace(ticket=500, symbol="EURUSD", direction=side, volume=volume, price_open=1.1000,
                                   stop_loss=1.0950 if side == "buy" else 1.1050, take_profit=1.1020 if side == "buy" else 1.0980)
        self.positions = [self.pos]
        self.exec = FakeExec(self.positions)
        self.path = path
        self.events = EventLog(None)
        self.at = AntiTwin(self.exec, path, self.events, locked=lambda: self.locked, clock=self.clock)
        self.cfg = replace(BBConfig("EURUSD", "M5"), anti_twin_enabled=True, **cfg)
        self.lc = {"ticket": 500, "candle_id": "cid", "symbol": "EURUSD", "timeframe": "M5"}
        self.history = {}
        self.at.adopt(self.lc, self.cfg, self.positions, self.SPEC)

    def quote(self, bid, spread=0.00012):
        return SimpleNamespace(symbol="EURUSD", bid=bid, ask=round(bid + spread, 8), valid=True)

    def tick(self, bid, dt=1.0, spread=0.00012):
        self.clock.advance(dt)
        q = self.quote(bid, spread)
        self.exec.last_quote = q
        self.at.step(q, self.SPEC, list(self.positions), lambda t: self.history.get(t), enabled=self.cfg.anti_twin_enabled, cfg=self.cfg,
                     fresh_positions=lambda: list(self.positions), fresh_quote=lambda: q)         # ONE step: close -> confirm -> open

    def bid_for(self, leg, fav, spread=0.00012):
        """The bid that puts `leg` `fav` price units in profit on ITS exit price (bid for a buy, ask for a sell)."""
        return leg["entry"] + fav if leg["side"] == "buy" else leg["entry"] - fav - spread

    def leg(self, lid="500-0"):
        return self.at.legs[lid]

    def arm_and_return(self, arm=1.1006, back=1.1000):
        self.tick(arm)
        self.tick(back)


class AntiTwinTriggerTest(unittest.TestCase):
    """The shared rule (trademate/antitwin_rule.py, mode ADVERSE_MOVE) through the Bull/Bear / Candle Momentum AntiTwin.
    Entry 1.1000, spread 1.2 pips, tick 1 point. A BUY exits on the bid, a SELL on the ask. The FIRST adverse tick (one tick worse than where the position
    started) closes the position and opens the opposite one in the SAME step. Break-even and the original target are not triggers."""

    BUY_ADVERSE = 1.0998            # bid 2 pips under the entry: past the 1.2 pip spread + one tick

    @staticmethod
    def go(r):
        r.tick(1.0998 if r.pos.direction == "buy" else 1.1002 - 0.00012 + 0.00012)     # a BUY's bid falls / a SELL's ask rises (ask = bid + spread)

    @staticmethod
    def flip(r, leg, away=0.0003):
        r.tick(r.bid_for(leg, -away))                                                          # 3 pips against the leg's own exit price

    def test_original_reaching_tp_never_triggers(self):
        r = ATRig()
        for bid in (1.1005, 1.1012, 1.1019, 1.1021, 1.1019, 1.1000, 1.0990):                   # it touched the target, THEN fell back through the entry
            r.tick(bid)
        self.assertEqual(r.leg()["state"], TP_TOUCHED)
        self.assertEqual(r.exec.calls, [])

    def test_the_broker_closing_at_tp_sl_or_by_hand_is_not_a_trigger(self):
        for reason, want in (("tp", TP_HIT), ("sl", SL_HIT), ("manual", "CLOSED_EXTERNAL")):
            r = ATRig()
            r.tick(1.1012)
            r.positions.clear()
            r.history[500] = {"reason": reason}
            r.tick(1.1000)
            self.assertEqual(r.leg()["state"], want, reason)
            self.assertEqual(r.exec.calls, [], reason)

    def test_the_first_adverse_tick_switches_without_waiting_for_a_return(self):
        r = ATRig()
        r.tick(1.0998)                                                                         # ONE tick, never in profit, never back at the entry
        self.assertEqual(r.exec.names(), ["close", "open"], "close and open happen in the SAME step")
        self.assertEqual(r.exec.calls[1][1], "sell")
        self.assertEqual(r.leg()["state"], TWIN_OPENED)
        self.assertEqual(r.leg()["twin"], "901-1")

    def test_buy_that_keeps_falling_is_not_held(self):
        r = ATRig()
        r.tick(1.0998)
        r.tick(1.0990)
        r.tick(1.0970)
        self.assertEqual(r.exec.names()[:2], ["close", "open"])
        self.assertEqual([p.direction for p in r.positions][0], "sell")

    def test_sell_moves_adversely_and_is_reversed_to_a_buy(self):
        r = ATRig("sell")
        r.tick(1.1002)                                                                         # the ask (1.10032) rises 3.2 pips above the entry
        self.assertEqual(r.exec.names(), ["close", "open"])
        self.assertEqual(r.exec.calls[1][1], "buy")

    def test_break_even_and_the_return_to_entry_are_not_triggers(self):
        r = ATRig()
        r.tick(1.1006)                                                                         # in profit
        r.tick(1.1000)                                                                         # back at the entry (bid == entry): the OLD trigger. Not any more.
        r.tick(1.09995)
        self.assertEqual(r.exec.calls, [])
        r.tick(1.0998)                                                                         # the first adverse move
        self.assertEqual(r.exec.names(), ["close", "open"])
        s = ATRig("sell")
        s.tick(1.0992 - 0.00012)
        s.tick(1.0999)
        self.assertEqual(s.exec.calls, [])
        s.tick(1.1002)
        self.assertEqual(s.exec.names(), ["close", "open"])

    def test_the_spread_alone_is_not_an_adverse_move(self):
        r = ATRig()
        for bid in (1.09988, 1.09988, 1.099875, 1.09988, 1.0999, 1.1000):                     # sits one spread under the entry (as any buy does) and wobbles half a tick
            r.tick(bid)
        self.assertEqual(r.exec.calls, [])
        self.assertEqual(r.leg()["state"], WATCHING)

    def test_one_tick_beyond_the_spread_is_the_threshold(self):
        r = ATRig()
        r.tick(1.09988)                                                                        # the opening level: exit price == entry - spread
        r.tick(1.099871)                                                                       # less than one tick worse
        self.assertEqual(r.exec.calls, [])
        r.tick(1.09987)                                                                        # exactly one tick worse
        self.assertEqual(r.exec.names(), ["close", "open"])

    def test_a_buy_is_judged_on_the_bid_and_a_sell_on_the_ask(self):
        r = ATRig()
        r.tick(1.09988)
        r.tick(1.09988, spread=0.00001)                                                        # the ask collapses, the bid (the BUY's exit) is unchanged
        self.assertEqual(r.exec.calls, [])
        r.tick(1.09987, spread=0.00030)                                                        # the bid is a tick worse although the ask is far higher
        self.assertEqual(r.exec.names(), ["close", "open"])
        s = ATRig("sell")
        s.tick(1.1000)                                                                         # ask 1.10012 == the SELL's opening exit level
        s.tick(1.09990, spread=0.00022)                                                        # the bid is far lower (profit for nobody), the ask is unchanged
        self.assertEqual(s.exec.calls, [])
        s.tick(1.10001, spread=0.00012)                                                        # the ask 1.10013: a tick worse
        self.assertEqual(s.exec.names(), ["close", "open"])

    def test_a_gap_beyond_the_threshold_triggers_like_one_tick(self):
        r = ATRig()
        r.tick(1.1006)
        r.tick(1.0900)
        self.assertEqual(r.exec.names(), ["close", "open"])

    def test_there_is_no_profit_threshold_and_the_old_settings_are_ignored(self):
        r = ATRig(at_arm_percent=90.0, at_commission_units=5.0, at_slippage_units=5.0, at_buffer_units=5.0)
        r.tick(1.0998)
        self.assertEqual(r.exec.names(), ["close", "open"])

    def test_opposite_opens_only_after_the_original_is_confirmed_closed(self):
        r = ATRig()
        r.exec.remove_on_close = False                                                         # the broker accepts the close but the position is still listed
        self.go(r)
        self.assertEqual(r.exec.names(), ["close"])
        self.assertEqual(r.leg()["state"], AWAITING_CLOSE)
        r.tick(1.0997)
        r.tick(1.0996)
        self.assertEqual(r.exec.names(), ["close"], "nothing may open while the original is still on the account")
        r.positions.clear()                                                                    # now the account shows it gone
        r.tick(1.0996)
        self.assertEqual(r.exec.names(), ["close", "open"])

    def test_without_the_fresh_account_reader_the_old_two_pass_behaviour_is_kept(self):
        r = ATRig()
        q = r.quote(1.0998)
        r.exec.last_quote = q
        r.clock.advance(1)
        r.at.step(q, r.SPEC, list(r.positions), None, enabled=True, cfg=r.cfg)
        self.assertEqual(r.exec.names(), ["close"])
        r.at.step(q, r.SPEC, list(r.positions), None, enabled=True, cfg=r.cfg)
        self.assertEqual(r.exec.names(), ["close", "open"])

    def test_close_not_confirmed_in_time_is_uncertain_and_opens_nothing(self):
        r = ATRig()
        r.exec.remove_on_close = False
        self.go(r)
        for _ in range(20):
            r.tick(1.0996)
        self.assertEqual(r.leg()["state"], UNCERTAIN)
        self.assertEqual(r.exec.names(), ["close"])

    def test_twin_has_equal_volume(self):
        for v in (0.01, 0.03, 0.5, 1.25):
            r = ATRig(volume=v)
            self.go(r)
            self.assertEqual(r.exec.calls[1][2], v)
            self.assertEqual([p.volume for p in r.positions], [v])

    def test_twin_uses_the_actual_filled_volume_of_a_partial_fill(self):
        r = ATRig(volume=0.10)
        orig_open = r.exec.open

        def partial(symbol, side, volume, reference_stop, sl=None, tp=None):
            out = orig_open(symbol, side, volume, reference_stop, sl=sl, tp=tp)
            r.positions[-1].volume = 0.04                                                      # only 0.04 of 0.10 filled
            out.position = r.positions[-1]
            return out
        r.exec.open = partial
        self.go(r)
        twin = r.at.legs[r.leg()["twin"]]
        self.assertEqual(twin["volume"], 0.04)
        self.assertTrue(twin["partial"])
        self.assertIn("PARTIAL", r.leg()["reason"])

    def test_the_actual_volume_of_the_closed_position_is_what_the_twin_mirrors(self):
        r = ATRig(volume=0.10)
        r.tick(1.1001)
        r.pos.volume = 0.06                                                                    # partly closed by hand in MT5 before the reversal
        r.tick(1.1001)
        self.go(r)
        self.assertEqual(r.exec.calls[1][2], 0.06)

    def test_the_twin_levels_are_half_the_target_and_the_original_stop_distance(self):
        r = ATRig()
        self.go(r)
        _, side, vol, sl, tp = r.exec.calls[1]
        entry = r.exec.last_quote.bid
        self.assertAlmostEqual(entry - tp, 0.0010, places=5)                                   # original target 20 pips -> twin target 10 pips (sell: below)
        self.assertAlmostEqual(sl - entry, 0.0050, places=4)                                   # the original's 50-pip stop, mirrored above the twin's entry

    def test_every_twin_has_its_own_entry_reference_and_the_chain_continues(self):
        r = ATRig()
        self.go(r)                                                                             # BUY -> SELL (the twin fills at the bid)
        t1 = r.at.legs[r.leg()["twin"]]
        self.assertEqual((t1["level"], t1["state"], t1["side"]), (1, WATCHING, "sell"))
        self.assertEqual(t1["rule"]["entry"], t1["entry"])
        self.assertNotEqual(t1["entry"], r.leg()["entry"])
        self.flip(r, t1)                                                                       # SELL -> BUY
        t2 = r.at.legs[t1["twin"]]
        self.assertEqual((t2["level"], t2["side"]), (2, "buy"))
        self.assertEqual(t2["rule"]["entry"], t2["entry"])
        self.flip(r, t2)                                                                       # BUY -> SELL
        t3 = r.at.legs[t2["twin"]]
        self.assertEqual((t3["level"], t3["side"]), (3, "sell"))
        self.flip(r, t3)                                                                       # SELL -> BUY: BUY -> SELL -> BUY -> SELL -> BUY
        self.assertEqual(r.exec.names(), ["close", "open"] * 4)
        self.assertEqual([c[1] for c in r.exec.calls if c[0] == "open"], ["sell", "buy", "sell", "buy"])
        self.assertEqual(r.at.chains["500-0"]["claimed"], 4)
        self.assertEqual(r.leg()["state"], TWIN_OPENED)                                        # the original's record is untouched by its twins' lives

    def test_the_twin_is_watched_from_its_own_fill_even_if_the_order_result_price_differs(self):
        r = ATRig()
        orig_open = r.exec.open

        def slipped(symbol, side, volume, reference_stop, sl=None, tp=None):
            out = orig_open(symbol, side, volume, reference_stop, sl=sl, tp=tp)
            out.price = r.positions[-1].price_open - 0.0005                                    # a misleading result price; the position's price_open is the truth
            return out
        r.exec.open = slipped
        self.go(r)
        r.tick(1.0996)                                                                         # its first observation replaces the misleading reference with the position's own
        t1 = r.at.legs[r.leg()["twin"]]
        self.assertAlmostEqual(t1["entry"], r.positions[-1].price_open, places=8)
        self.assertAlmostEqual(t1["rule"]["entry"], r.positions[-1].price_open, places=8)

    def test_the_chain_has_a_fixed_safety_ceiling(self):
        from trademate.antitwin_rule import MAX_CHAIN
        r = ATRig()
        self.go(r)
        for _ in range(MAX_CHAIN + 5):
            live = [l for l in r.at.legs.values() if l["state"] == WATCHING]
            if not live:
                break
            self.flip(r, live[0])
        self.assertEqual(r.exec.names().count("open"), MAX_CHAIN)
        self.assertEqual(r.exec.names().count("close"), MAX_CHAIN)
        self.assertIn(LIMIT_REACHED, [l["state"] for l in r.at.legs.values()])

    def test_emergency_stop_overrides_everything(self):
        r = ATRig(locked=True)
        self.go(r)
        self.assertEqual(r.exec.calls, [])
        self.assertEqual(r.leg()["state"], EMERGENCY_CANCELLED)
        r2 = ATRig()
        self.go(r2)
        self.assertEqual(r2.exec.names(), ["close", "open"])                                   # sanity: it works when not locked

    def test_emergency_stop_between_the_close_and_the_open_cancels_the_twin(self):
        r = ATRig()
        r.exec.remove_on_close = False
        self.go(r)
        self.assertEqual(r.exec.names(), ["close"])
        r.locked = True
        r.positions.clear()
        r.tick(1.0996)
        self.assertEqual(r.exec.names(), ["close"], "the twin must not open under an Emergency Stop")
        self.assertEqual(r.leg()["state"], EMERGENCY_CANCELLED)

    def test_switching_it_off_between_the_close_and_the_open_stops_the_twin(self):
        r = ATRig()
        r.exec.remove_on_close = False
        self.go(r)
        r.cfg = replace(r.cfg, anti_twin_enabled=False)
        r.positions.clear()
        r.tick(1.0996)
        self.assertEqual(r.exec.names(), ["close"])
        self.assertEqual(r.leg()["state"], "SWITCHED_OFF")

    def test_duplicate_reversal_is_prevented(self):
        r = ATRig()
        self.go(r)
        for _ in range(10):                                                                    # the same price keeps being re-evaluated
            r.tick(1.0998)
        self.assertEqual(r.exec.names(), ["close", "open"])
        self.assertEqual(r.at.adopt(r.lc, r.cfg, r.positions, r.SPEC), "500-0")                # re-adopting the same ticket creates nothing new
        self.assertEqual(len([l for l in r.at.legs.values() if l["level"] == 0]), 1)

    def test_one_active_reversal_per_ticket_even_with_a_second_leg_record(self):
        r = ATRig()
        r.exec.remove_on_close = False
        self.go(r)                                                                             # leg 500-0 is AWAITING_CLOSE for ticket 500
        dup = dict(r.leg())
        dup.update(id="dup", state=WATCHING, rule=__import__("trademate.antitwin_rule", fromlist=["x"]).new_state("buy", 1.1000), created_at=0)
        r.at.legs["dup"] = dup
        r.tick(1.0990)
        self.assertEqual(r.exec.names().count("close"), 1, "a second record for the same ticket must not start a second reversal")

    def test_duplicate_prevention_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "at.json"
            r = ATRig(path=path)
            self.go(r)
            self.assertEqual(r.exec.names(), ["close", "open"])
            again = AntiTwin(r.exec, path, EventLog(None), locked=lambda: False, clock=r.clock)
            self.assertEqual(again.legs["500-0"]["state"], TWIN_OPENED)
            again.adopt(r.lc, r.cfg, r.positions, r.SPEC)
            self.assertEqual(r.exec.names(), ["close", "open"])
            self.assertEqual(again.chains["500-0"]["claimed"], 1)

    def test_a_restart_while_the_open_was_in_flight_never_resends(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "at.json"
            r = ATRig(path=path)
            r.exec.open_kind = "unknown"
            self.go(r)
            # v5.3: an unproven opposite order is RECONCILED with the account (it is never re-sent), and only after the window is it RECOVERY_REQUIRED
            self.assertEqual(r.leg()["state"], "OPENING")
            self.assertEqual(r.leg()["reversal_state"], "OPPOSITE_ORDER_REQUESTED")
            again = AntiTwin(r.exec, path, EventLog(None), locked=lambda: False, clock=r.clock)
            for _ in range(20):
                r.clock.advance(1)
                again.step(r.quote(1.0996), r.SPEC, list(r.positions), None)
            self.assertEqual(r.exec.names().count("open"), 1)
            self.assertEqual(again.legs["500-0"]["state"], UNCERTAIN)
            self.assertEqual(again.legs["500-0"]["reversal_state"], "RECOVERY_REQUIRED")

    def test_a_twin_left_waiting_past_the_deadline_is_abandoned_not_opened_late(self):
        r = ATRig()
        r.exec.remove_on_close = False
        self.go(r)                                                                             # closed, waiting for confirmation (e.g. the bridge went quiet)
        r.positions.clear()
        r.clock.advance(60)                                                                    # the bridge returns a minute later
        r.exec.positions.clear()
        r.at.legs["500-0"]["closed_at"] = r.clock() - 60
        r.at.legs["500-0"]["state"] = "OPENING"
        r.tick(1.0996)
        self.assertEqual(r.exec.names(), ["close"], "a stale reversal must not be opened a minute later")
        self.assertEqual(r.leg()["state"], FAILED)

    def test_uncertain_close_opens_nothing_and_is_not_retried(self):
        r = ATRig()
        r.exec.close_kind = "unknown"
        r.exec.remove_on_close = False
        self.go(r)
        for _ in range(5):
            r.tick(1.0996)
        self.assertEqual(r.exec.names(), ["close"])
        self.assertEqual(r.leg()["state"], "AWAITING_CLOSE")                 # v5.3: waiting for the ACCOUNT to prove the position gone; opens nothing, re-sends nothing
        for _ in range(20):
            r.tick(1.0996)
        self.assertEqual(r.exec.names(), ["close"])
        self.assertEqual(r.leg()["state"], UNCERTAIN)                        # the position never left the account: RECOVERY_REQUIRED, still nothing opened
        self.assertEqual(r.leg()["reversal_state"], "RECOVERY_REQUIRED")

    def test_failed_close_is_bounded_and_leaves_the_original_alone(self):
        r = ATRig()
        r.exec.close_kind = NOT_EXECUTED
        self.go(r)
        for _ in range(10):
            r.tick(1.0996)
        self.assertEqual(r.exec.names().count("close"), 3)
        self.assertEqual(r.leg()["state"], FAILED)
        self.assertEqual(r.exec.names().count("open"), 0)

    def test_a_close_that_finds_the_position_already_gone_is_not_reversed(self):
        r = ATRig()
        r.exec.close_kind = NOT_EXECUTED
        r.exec.remove_on_close = False
        orig = r.exec.close

        def gone(ticket):
            out = orig(ticket)
            r.positions.clear()                                                                # the broker's own SL closed it a moment earlier
            return out
        r.exec.close = gone
        self.go(r)
        self.assertEqual(r.leg()["state"], "CLOSED_EXTERNAL")
        self.assertEqual(r.exec.names(), ["close"])

    def test_risk_engine_refusal_means_no_twin_and_a_known_flat_state(self):
        r = ATRig()
        r.exec.open_kind = REFUSED
        self.go(r)
        for _ in range(5):
            r.tick(1.0996)
        self.assertEqual(r.leg()["state"], REFUSED_ST)
        self.assertEqual(r.exec.names().count("open"), 1)
        self.assertEqual(r.positions, [])

    def test_a_rejected_twin_is_retried_a_bounded_number_of_times_then_abandoned(self):
        r = ATRig()
        r.exec.open_kind = NOT_EXECUTED
        self.go(r)
        for _ in range(12):
            r.tick(1.0996)
        self.assertEqual(r.leg()["state"], FAILED)
        self.assertEqual(r.exec.names().count("open"), 5)                                      # MAX_ATTEMPTS + 2, never an endless loop
        self.assertEqual(r.positions, [])

    def test_disabled_anti_twin_never_acts(self):
        r = ATRig()
        r.cfg = replace(r.cfg, anti_twin_enabled=False)
        self.go(r)
        self.assertEqual(r.exec.calls, [])

    def test_the_position_without_stops_is_not_watched(self):
        r = ATRig()
        bad = SimpleNamespace(ticket=600, symbol="EURUSD", direction="buy", volume=0.01, price_open=1.1, stop_loss=None, take_profit=None)
        self.assertIsNone(r.at.adopt({"ticket": 600, "candle_id": "x", "symbol": "EURUSD", "timeframe": "M5"}, r.cfg, [bad], r.SPEC))

    def test_every_reversal_is_timed_and_the_close_is_never_called_break_even(self):
        r = ATRig()
        self.go(r)
        rec = r.at.latency_log[-1]
        for k in ("detect_to_close_ms", "close_roundtrip_ms", "open_roundtrip_ms", "detect_to_open_ms"):
            self.assertIsNotNone(rec[k], k)
        self.assertGreaterEqual(rec["detect_to_open_ms"], rec["detect_to_close_ms"])
        self.assertEqual(r.at.snapshot()["latency"]["count"], 1)


# ============================================================================================================== engine + Anti-Twin end to end (simulated)
class EngineAntiTwinIntegrationTest(unittest.TestCase):
    def test_an_open_bull_bear_position_is_adopted_and_reversed(self):
        clock_holder = {}
        pos = []
        ex = FakeExec(pos)
        r = Rig([choch(BULL)], cfg_over={"anti_twin_enabled": True})
        r.engine.anti_twin = AntiTwin(ex, None, r.events, locked=lambda: False, clock=r.clock)
        r.run_to_trigger(BULL)
        self.assertEqual(r.decision(), "BUY")
        lc = r.lc()
        base = dict(symbol="EURUSD", strategy_id="bull_bear", exec_tf="M5", candidate_id=lc["candle_id"])
        r.engine.report_execution({"stage": "proposing", **base})
        r.engine.report_execution({"stage": "approved", "volume": 0.02, **base})
        r.engine.report_execution({"stage": "executed", "ticket": 321, "fill_price": 1.1010, **base})
        self.assertEqual(r.lc()["state"], "POSITION_OPEN")
        p = SimpleNamespace(ticket=321, symbol="EURUSD", direction="buy", volume=0.02, price_open=1.1010, stop_loss=1.0960, take_profit=1.1030)
        pos.append(p)
        r.feed.pos_list = pos
        for bid in (1.1015, 1.1005):                                                          # in profit, then the first adverse move (1.1005 is 0.5 pip under the open level)
            r.feed.tick(bid)
            ex.last_quote = r.feed.quote("EURUSD")
            for _ in range(2):
                r.clock.advance(0.6)
                r.engine.step()
        self.assertEqual(ex.names(), ["close", "open"], r.engine.anti_twin.snapshot())
        self.assertEqual(ex.calls[1][1:3], ("sell", 0.02))
        snap = r.engine.snapshot("EURUSD", "M5")
        self.assertEqual(snap["anti_twin"]["legs"][0]["level"] in (0, 1), True)
        self.assertEqual(snap["machine"]["structure"]["direction"], BULL)


# ============================================================================================================== real pipeline (simulated broker)
class BullBearEndToEndTest(unittest.TestCase):
    """bull_bear engine -> Autopilot -> pipeline -> Risk Engine -> adapter -> fake bridge. The structure is scripted at the feed (the structure engine itself is
    proved on real candles in StructureStateTest); everything after the structure gate is the real path."""

    def build(self, struct):
        import app as app_module
        from tests.test_candle_momentum_timeframes import TfRig
        from trademate import bull_bear as bb_pkg
        from trademate.autopilot import Autopilot
        from trademate.bull_bear.feed import AdapterFeed
        from trademate.strategies.selection import StrategySelectionStore
        rig = self.rig = type("R", (TfRig, unittest.TestCase), {"runTest": lambda s: None})()
        rig.build("M5", tfs=("M5",))
        self.addCleanup(rig._tmp.cleanup)
        self.addCleanup(rig.doCleanups)                                                                  # stops the rig's patches (adapter, stores): they must not leak
        feed = AdapterFeed(rig.adapter)
        feed.structure = lambda *a, **k: bbs.structure_from_events(struct)
        eng = BullBearEngine(feed, rig.settings, ConfigStore(None, EXEC_TIMEFRAMES), StateStore(None), EventLog(None), hub=rig.hub, threaded=False)
        bb_pkg.set_engine(eng)
        self.addCleanup(bb_pkg.set_engine, None)
        rig.selection.save(symbol="EURUSD", strategy_id="bull_bear", timeframe="M5")
        rig.autopilot = Autopilot(Path(rig._tmp.name) / "ap2.json", selection=lambda: ("EURUSD", "bull_bear", "M5"), hub_get=rig.hub.get, lot_for=lambda s: 0.01,
                                  propose=app_module._autopilot_propose, execute=app_module._autopilot_execute, positions=lambda: {p.ticket for p in rig.adapter.get_positions()},
                                  history=lambda: [], observer=lambda ev: eng.report_execution(ev) if ev.get("strategy_id") == "bull_bear" else None)
        eng.on_signal = lambda *a: rig.autopilot.wake()
        eng.release_hook = lambda cid: (rig.ledger.release(cid), rig.autopilot.forget_attempt(cid))
        for p in (__import__("unittest").mock.patch.object(app_module, "strategy_store", StrategySelectionStore(Path(rig._tmp.name) / "ss2.json", "bull_bear")),):
            p.start()
            self.addCleanup(p.stop)
        ok, why = eng.start("EURUSD", "M5")
        self.assertTrue(ok, why)
        eng.step()
        ok, why = rig.autopilot.start("runs", 5, spawn=False)
        self.assertTrue(ok, why)
        self.eng = eng

    def test_aligned_signal_trades_once_through_the_risk_engine(self):
        self.build([choch(BULL)])
        self.rig.price(1.10100, 1.10112)
        self.eng.step()
        self.assertEqual(self.rig.hub.get("EURUSD", "bull_bear", "M5")["decision"], "BUY")
        self.rig.autopilot.tick()
        self.assertEqual([o["side"] for o in self.rig.opens()], ["buy"])
        self.assertEqual(self.eng._slots["EURUSD|M5"].machine.lc["risk_decision"]["status"], "approved")
        for _ in range(3):
            self.rig.price(1.10110, 1.10122)
            self.eng.step()
            self.rig.autopilot.tick()
        self.assertEqual(len(self.rig.opens()), 1)

    def test_counter_structure_signal_sends_nothing(self):
        self.build([choch(BEAR)])
        self.rig.price(1.10100, 1.10112)                                                      # a bullish candle in BEARISH structure
        self.eng.step()
        self.rig.autopilot.tick()
        self.assertEqual(self.rig.opens(), [])

    def test_no_choch_sends_nothing(self):
        self.build([])
        self.rig.price(1.10100, 1.10112)
        self.eng.step()
        self.rig.autopilot.tick()
        self.assertEqual(self.rig.opens(), [])


if __name__ == "__main__":
    unittest.main()
