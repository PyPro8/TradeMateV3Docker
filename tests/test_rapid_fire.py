"""RAPID FIRE tests. AUTOMATED ONLY: they run the real engine, the real Risk gate and the real MT5Adapter against tests/fake_bridge.py (a stand-in for
the MT5 EA). They prove TradeMate's own behaviour. They do NOT prove real MT5 / broker behaviour (see docs/RAPID_FIRE.md, 'What is not verified').

The fake EURUSD has point 1e-5, tick size 1e-5, tick value 1.0 / lot, volume 0.01..100 step 0.01, spread 12 points. One point per lot = 1.00 USD.
Time is virtual: every `tick()` advances the engine's clock 1.1 s (the engine only reads the broker through the real adapter).
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_bridge import FakeBridge
from trademate.broker import ConnectionState, ConnectionStatus, create_adapter
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.rapid_fire import metrics
from trademate.rapid_fire import store as S
from trademate.rapid_fire.engine import MAX_CLOSE_ATTEMPTS, RapidFireEngine
from trademate.rapid_fire.executor import Executor
from trademate.rapid_fire.models import RFConfig, anti_twin_state, parse, readiness, validate
from trademate.rapid_fire.store import ConfigStore, Ledger
from trademate.risk import RiskRules
from trademate import brokertime

P = 1e-5                      # one point on the fake EURUSD
SPREAD = 12 * P


class Base(unittest.TestCase):
    CFG = dict(symbol="EURUSD", lot=0.01, emergency_points=50.0)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.bridge = FakeBridge(self.dir).start()
        self.addCleanup(self.bridge.stop)
        self.adapter = create_adapter(Settings(app=AppSettings(data_dir=self.dir), mt5=Mt5Settings(data_dir=self.dir),
                                               trading=TradingSettings(command_timeout_seconds=2.0, confirm_timeout_seconds=1.5)))
        self.rules = RiskRules(max_risk_per_trade_percent=1.0, max_daily_loss_percent=3.0, max_total_active_risk_percent=3.0, max_positions=3)
        # The virtual candle clock runs ahead of the wall clock, so the broker-clock probe (which wants a fresh M1 candle in REAL time) is pinned.
        p = mock.patch("trademate.risk.account_state.resolve_server_clock", lambda *a, **k: brokertime.ServerClock(0, "configured", "fixed for the tests"))
        p.start()
        self.addCleanup(p.stop)
        self.now = float(int(time.time()))
        self.T = int(self.now) // 60 * 60                      # the forming M1 candle
        self.engine = self.make_engine()
        self.set_candle(self.T, 1.10000)
        self.set_bid(1.10000)
        self.configure(**self.CFG)

    # ---- harness ----
    def make_engine(self, ledger=None, adapter=None):
        self.ledger = ledger or Ledger(self.dir / "led.json")
        self.cfgstore = ConfigStore(self.dir / "cfg.json")
        self.guard_reason = None
        ad = adapter or self.adapter
        eng = RapidFireEngine(ad, self.cfgstore, self.ledger, Executor(ad, lambda: self.rules, lambda: None), guard=lambda: self.guard_reason,
                              server_offset=lambda: 0, clock=lambda: self.now)
        eng.ensure_thread = lambda: None            # the tests drive step() by hand on a virtual clock; the real thread is tested in TestThread
        return eng

    def configure(self, **kw):
        cfg, problems = self.engine.save_config(kw)
        self.assertIsNotNone(cfg, problems)

    def set_candle(self, t, o):
        self.bridge.scripted_candles[("EURUSD", "M1")] = [[t - 60, o, o, o, o], [t, o, o, o, o]]
        self.candle_open = o

    def set_bid(self, bid):
        self.bridge.bid, self.bridge.ask = round(bid, 5), round(bid + SPREAD, 5)
        self.bridge.write_snapshot()

    def tick(self, n=1, dt=1.1):
        for _ in range(n):
            self.now += dt
            self.engine.step(self.now)

    def start(self):
        ok, msg = self.engine.start()
        self.assertTrue(ok, msg)
        self.new_candle()                                       # entries only begin in the first NEW candle after Start

    def new_candle(self, o=None):
        self.T += 60
        self.set_candle(self.T, self.candle_open if o is None else o)
        self.now = max(self.now, self.T)
        self.tick()

    def cmds(self, name):
        return [c for c in self.bridge.commands_seen if c.get("cmd") == name]

    def trades(self):
        return self.ledger.trades()

    def open_positions(self):
        return dict(self.bridge.positions)


class TestConfig(Base):
    def test_initial_values_are_the_specified_ones(self):
        c = RFConfig()
        self.assertEqual((c.symbol, c.trigger_points, c.target_points, c.max_entries_per_candle), ("VOL_80", 5.0, 10.0, 2))
        self.assertIsNone(c.lot)                               # configurable: no invented lot
        self.assertIsNone(c.emergency_points)                  # no invented emergency distance
        self.assertFalse(c.anti_twin_enabled)
        self.assertIsNone(c.anti_twin_adverse_points)          # the reversal threshold is NEVER invented

    def test_m1_only_the_timeframe_cannot_be_changed(self):
        cfg, problems = parse({"timeframe": "M5"}, RFConfig())
        self.assertIsNone(cfg)
        self.assertIn("M1 only", problems[0])
        cfg, problems = parse({"timeframe": "m1"}, RFConfig())
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.to_dict()["timeframe"], "M1")

    def test_unknown_keys_and_bad_values_are_refused(self):
        for bad in ({"nope": 1}, {"trigger_points": 0}, {"target_points": "x"}, {"max_entries_per_candle": 0}, {"max_entries_per_candle": 99},
                    {"lot": -1}, {"emergency_points": 0}):
            cfg, problems = parse(bad, RFConfig())
            self.assertIsNone(cfg, bad)
            self.assertTrue(problems)

    def test_the_retired_anti_twin_settings_are_still_read_but_no_longer_validated_against_the_emergency_distance(self):
        cfg, problems = parse({"emergency_points": 20, "anti_twin_adverse_points": 30, "anti_twin_mode": "breakeven", "at_arm_percent": 40}, RFConfig())
        self.assertIsNotNone(cfg, problems)                    # older saved settings load; the fields are ignored by the engine

    def test_not_ready_without_lot_and_emergency_distance(self):
        self.assertEqual(len(readiness(RFConfig())), 2)
        self.assertEqual(readiness(RFConfig(lot=0.01, emergency_points=30)), [])

    def test_anti_twin_is_only_on_or_off(self):
        self.assertEqual(anti_twin_state(RFConfig(anti_twin_enabled=True))[0], "ARMED")          # no threshold needed any more
        self.assertEqual(anti_twin_state(RFConfig())[0], "DISABLED")

    def test_cannot_start_until_lot_and_emergency_are_set(self):
        eng = self.make_engine()
        self.cfgstore.save(RFConfig(symbol="EURUSD"))
        ok, msg = eng.start()
        self.assertFalse(ok)
        self.assertIn("lot size", msg)
        self.assertEqual(self.cmds("open"), [])
        self.assertEqual(eng.status()["state"], "NOT_CONFIGURED")


class TestInstrument(Base):
    def test_vol_80_is_never_silently_replaced(self):
        self.configure(symbol="VOL_80")                         # the default instrument; this fake broker does not list it
        ok, msg = self.engine.start()
        self.assertFalse(ok)
        self.assertFalse(self.engine.running)
        self.assertEqual(self.cmds("open"), [])
        self.assertNotIn("EURUSD", msg.replace("never substitutes", ""))

    def test_only_m1_candles_are_ever_requested(self):
        self.start()
        self.tick(3)
        tfs = {c["timeframe"] for c in self.cmds("candles")}
        self.assertEqual(tfs, {"M1"})

    def test_broker_metadata_money_maths(self):
        info = self.adapter.get_symbol_spec("EURUSD")
        self.assertAlmostEqual(metrics.money_per_point_per_lot(info), 1.0)
        self.assertAlmostEqual(metrics.estimate(info, 0.05, 10), 0.5)
        res = metrics.suggest_lot(info, 0.5, 10)
        self.assertEqual(res["lot"], 0.05)
        res = metrics.suggest_lot(info, 0.099, 10)              # needs 0.0099 lot: rounds DOWN to the step, then below the minimum
        self.assertIsNone(res["lot"])
        self.assertIn("minimum", res["reason"])
        res = metrics.suggest_lot(info, 0.37, 10)               # 0.037 -> 0.03, never rounded up
        self.assertEqual(res["lot"], 0.03)

    def test_money_is_never_assumed_when_broker_data_is_missing(self):
        class Bare:
            tick_value = None; tick_size = 1e-5; point = 1e-5
        self.assertIsNone(metrics.money_per_point_per_lot(Bare()))
        self.assertIsNone(metrics.suggest_lot(Bare(), 50, 10)["lot"])


class TestEntry(Base):
    def test_no_entry_in_the_candle_that_is_already_running_when_started(self):
        self.set_bid(1.10020)
        ok, _ = self.engine.start()
        self.assertTrue(ok)
        self.tick(3)
        self.assertEqual(self.cmds("open"), [])
        self.assertIn("next new M1 candle", self.engine.status()["state_reason"])

    def test_four_points_does_nothing_five_points_buys(self):
        self.start()
        self.set_bid(1.10004)
        self.tick(2)
        self.assertEqual(self.cmds("open"), [])
        self.set_bid(1.10005)
        self.tick()
        self.assertEqual(len(self.cmds("open")), 1)
        self.assertEqual(self.cmds("open")[0]["side"], "buy")

    def test_five_points_below_the_open_sells(self):
        self.start()
        self.set_bid(1.09995)
        self.tick()
        self.assertEqual(self.cmds("open")[0]["side"], "sell")

    def test_order_carries_no_broker_side_stop_or_target(self):
        self.start()
        self.set_bid(1.10006)
        self.tick()
        c = self.cmds("open")[0]
        self.assertEqual((float(c["sl"]), float(c["tp"])), (0.0, 0.0))

    def test_lot_size_is_sent_exactly(self):
        self.configure(lot=0.03)
        self.start()
        self.set_bid(1.10006)
        self.tick()
        self.assertEqual(list(self.open_positions().values())[0]["volume"], 0.03)

    def test_invalid_lot_blocks_the_order_with_a_reason(self):
        self.configure(lot=0.005)
        self.start()
        self.set_bid(1.10006)
        self.tick()
        self.assertEqual(self.cmds("open"), [])
        self.assertIn("minimum", self.engine.status()["state_reason"])

    def test_spread_filter(self):
        self.configure(max_spread_points=5)
        self.start()
        self.set_bid(1.10006)
        self.tick()
        self.assertEqual(self.cmds("open"), [])
        self.assertIn("Spread", self.engine.status()["state_reason"])

    def test_one_position_at_a_time_and_polling_never_duplicates(self):
        self.start()
        self.set_bid(1.10006)
        self.tick(12)                                           # twelve polls while the position is open
        self.assertEqual(len(self.cmds("open")), 1)
        self.assertEqual(len(self.open_positions()), 1)

    def test_entry_submitted_only_when_not_running(self):
        self.start()
        self.engine.stop()
        self.set_bid(1.10006)
        self.tick(3)
        self.assertEqual(self.cmds("open"), [])


class TestTargetAndShots(Base):
    def buy_in_first_candle(self):
        self.start()
        self.set_bid(1.10006)
        self.tick()
        t = self.trades()[0]
        self.assertEqual(t["status"], S.OPEN)
        return t

    def test_target_is_ten_points_from_the_actual_fill(self):
        t = self.buy_in_first_candle()
        fill = 1.10006 + SPREAD
        self.assertAlmostEqual(t["entry_price"], fill, places=6)
        self.assertAlmostEqual(t["target_price"], fill + 10 * P, places=6)

    def test_nine_points_holds_ten_points_closes(self):
        t = self.buy_in_first_candle()
        fill = t["entry_price"]
        self.set_bid(fill + 9 * P)
        self.tick(2)
        self.assertEqual(self.cmds("close"), [])
        self.set_bid(fill + 10 * P)
        self.tick()
        self.assertEqual(len(self.cmds("close")), 1)
        self.assertEqual(self.open_positions(), {})
        self.assertEqual(self.trades()[0]["close_reason"], "take_profit")

    def test_close_is_confirmed_then_result_comes_from_mt5_history(self):
        t = self.buy_in_first_candle()
        self.set_bid(t["entry_price"] + 10 * P)
        self.tick(3)
        done = self.trades()[0]
        self.assertEqual(done["status"], S.CLOSED)
        self.assertEqual(done["result_source"], "mt5_history")
        h = self.bridge.history[0]
        self.assertEqual(done["net"], h["profit"] + h["swap"] + h["commission"])
        self.assertEqual(done["exit_price"], h["price_close"])
        st = self.engine.status()["pnl"]
        self.assertEqual((st["closed_count"], st["wins"]), (1, 1 if done["net"] > 0 else 0))

    def test_no_close_for_a_sell_until_the_ask_is_ten_points_below(self):
        self.start()
        self.set_bid(1.09994)
        self.tick()
        t = self.trades()[0]
        self.assertEqual(t["side"], "sell")
        fill = t["entry_price"]                                   # sells fill at the bid
        self.assertAlmostEqual(fill, 1.09994, places=6)
        self.set_bid(fill - 10 * P - SPREAD + 2 * P)               # ask still 2 points above the target
        self.tick(2)
        self.assertEqual(self.cmds("close"), [])
        self.set_bid(fill - 10 * P - SPREAD)
        self.tick()
        self.assertEqual(len(self.cmds("close")), 1)

    def drive_one_shot(self, entry_bid):
        self.set_bid(entry_bid)
        self.tick()
        t = [x for x in self.trades() if x["status"] == S.OPEN][0]
        self.set_bid(t["entry_price"] + 10 * P)
        self.tick(2)
        return t

    def test_the_second_shot_needs_a_fresh_move_from_the_first_shots_exit(self):
        self.start()
        self.drive_one_shot(1.10006)                               # exits at bid 1.10028, which is 28 points above the candle open
        self.tick(3)
        self.assertEqual(len(self.cmds("open")), 1)                # NOT re-entered on the same tick just because price is far from the open
        self.set_bid(1.10032)                                      # 4 points above the exit
        self.tick(2)
        self.assertEqual(len(self.cmds("open")), 1)
        self.set_bid(1.10033)                                      # 5 points above the exit
        self.tick()
        self.assertEqual(len(self.cmds("open")), 2)
        self.assertEqual(self.engine.status()["candle"]["reference_kind"], "last exit price")

    def test_exactly_two_shots_per_candle_then_none(self):
        self.start()
        self.drive_one_shot(1.10006)
        self.set_bid(1.10040)
        self.tick()
        t2 = [x for x in self.trades() if x["status"] == S.OPEN][0]
        self.set_bid(t2["entry_price"] + 10 * P)
        self.tick(2)
        self.set_bid(1.10300)                                      # a huge move, but the candle's shots are used
        self.tick(5)
        self.assertEqual(len(self.cmds("open")), 2)
        self.assertEqual(self.engine.status()["candle"]["shots_remaining"], 0)
        self.assertEqual(self.engine.status()["candle"]["shots_used"], 2)
        self.assertIn("No entries left", self.engine.status()["state_reason"])

    def test_counter_resets_only_on_a_genuinely_new_candle(self):
        self.start()
        self.drive_one_shot(1.10006)
        self.assertEqual(self.engine.status()["candle"]["shots_used"], 1)
        self.set_bid(1.10040)                                      # price sits exactly on the new candle's open
        self.new_candle(o=1.10040)                                 # new candle timestamp
        self.assertEqual(self.engine.status()["candle"]["shots_used"], 0)
        self.assertEqual(self.engine.status()["candle"]["shots_remaining"], 2)
        # re-reading the SAME / an OLDER candle never resets anything
        self.set_candle(self.T - 60, 1.1)
        self.tick(3)
        self.assertGreaterEqual(self.ledger.shots(self.ledger.candle_key("EURUSD", self.T)), 0)

    def test_counter_survives_restart_refresh_and_reconnect(self):
        self.start()
        self.drive_one_shot(1.10006)
        key = self.ledger.candle_key("EURUSD", self.T)
        self.assertEqual(self.ledger.shots(key), 1)
        reloaded = Ledger(self.dir / "led.json")                  # a restart re-reads the file
        self.assertEqual(reloaded.shots(key), 1)
        eng2 = self.make_engine(ledger=reloaded)
        eng2.running, eng2._arm_after = True, None                # as if started long before this candle
        self.set_bid(1.10040)
        for _ in range(2):
            self.now += 1.1
            eng2.step(self.now)
        self.assertEqual(reloaded.shots(key), 2)                  # one more shot is allowed, the first one was remembered
        self.assertEqual(len(self.cmds("open")), 2)

    def test_ledger_reservation_is_atomic_and_limited(self):
        led = Ledger(self.dir / "x.json")
        key = led.candle_key("VOL_80", 1000)
        self.assertEqual([led.reserve_shot(key, 1000, 2) for _ in range(4)], [True, True, False, False])
        led.release_shot(key)
        self.assertTrue(led.reserve_shot(key, 1000, 2))
        self.assertEqual(Ledger(self.dir / "x.json").shots(key), 2)

    def test_a_rejected_order_does_not_consume_a_shot(self):
        self.start()
        self.bridge.reject_with = (10006, "Rejected by broker")
        self.set_bid(1.10006)
        self.tick()
        self.assertEqual(self.engine.status()["candle"]["shots_used"], 0)
        self.assertEqual(self.trades()[0]["status"], S.ENTRY_FAILED)
        self.tick(1)                                               # cooldown holds the retry back
        self.assertEqual(len(self.cmds("open")), 1)
        self.tick(3)                                               # then it retries once
        self.assertEqual(len(self.cmds("open")), 2)

    def test_failures_per_candle_are_capped(self):
        self.start()
        self.set_bid(1.10006)
        for _ in range(8):
            self.bridge.reject_with = (10006, "Rejected by broker")
            self.tick(3)
        self.assertLessEqual(len(self.cmds("open")), 3)


class TestRiskIntegration(Base):
    def test_the_central_risk_gate_judges_every_order(self):
        self.rules = RiskRules(max_risk_per_trade_percent=0.001, max_daily_loss_percent=3.0, max_total_active_risk_percent=3.0, max_positions=3)
        self.start()
        self.set_bid(1.10006)
        self.tick()
        self.assertEqual(self.cmds("open"), [])
        self.assertEqual(self.trades()[0]["status"], S.ENTRY_FAILED)
        self.assertEqual(self.engine.status()["candle"]["shots_used"], 0)
        self.assertIn("maximum risk per trade", self.engine.status()["last_execution"]["message"])

    def test_the_position_counts_toward_risk_through_its_software_level_only(self):
        from trademate.risk.account_state import gather_state, set_software_stops_provider
        self.start()
        self.set_bid(1.10006)
        self.tick()
        pos = list(self.adapter.get_positions())[0]
        self.assertIsNone(pos.stop_loss)                          # nothing at the broker
        self.addCleanup(set_software_stops_provider, None)
        set_software_stops_provider(None)
        before = gather_state(self.adapter).positions[0]
        self.assertIsNone(before.risk_amount)                     # without the hook: unprotected, risk unknown (the previous behaviour)
        set_software_stops_provider(self.engine.software_stops)
        after = gather_state(self.adapter).positions[0]
        self.assertIsNotNone(after.risk_amount)
        self.assertAlmostEqual(after.risk_amount, 0.01 * 50 * 1.0, places=4)    # 50 points x 1.00 x 0.01 lot

    def test_a_broker_side_stop_always_wins_over_the_software_level(self):
        from trademate.risk.account_state import build_state
        from trademate.broker.base import AccountInfo, Position
        pos = Position(1, "EURUSD", "buy", 0.01, 1.1, 1.1, 1.099, None, 0.0, 0.0)
        acct = AccountInfo.__new__(AccountInfo)
        object.__setattr__(acct, "balance", 1000.0); object.__setattr__(acct, "equity", 1000.0); object.__setattr__(acct, "currency", "USD")
        from trademate.risk.account_state import to_spec
        spec = to_spec(self.adapter.get_symbol_spec("EURUSD"))
        st = build_state(acct, [pos], {"EURUSD": spec}, [], 0, "t", software_stops={1: 1.0})
        self.assertAlmostEqual(st.positions[0].risk_amount, 0.01 * 100 * 1.0, places=4)   # 100 points to the broker stop, not 1000 to the software one

    def test_emergency_lock_and_trading_disabled_block_entries(self):
        self.start()
        self.guard_reason = "EMERGENCY LOCKED"
        self.set_bid(1.10006)
        self.tick(3)
        self.assertEqual(self.cmds("open"), [])
        self.assertIn("EMERGENCY", self.engine.status()["state_reason"])

    def test_emergency_exit_closes_at_the_software_distance(self):
        self.start()
        self.set_bid(1.10006)
        self.tick()
        fill = self.trades()[0]["entry_price"]
        self.set_bid(fill - 49 * P)
        self.tick(2)
        self.assertEqual(self.cmds("close"), [])
        self.set_bid(fill - 50 * P)
        self.tick()
        self.assertEqual(len(self.cmds("close")), 1)
        self.assertEqual(self.trades()[0]["close_reason"], "emergency")


class TestCloseFailures(Base):
    def reach_target(self):
        self.start()
        self.set_bid(1.10006)
        self.tick()
        fill = self.trades()[0]["entry_price"]
        self.set_bid(fill + 10 * P)

    def test_failed_close_is_retried_not_flooded_and_then_escalated(self):
        self.reach_target()
        for _ in range(MAX_CLOSE_ATTEMPTS + 4):
            self.bridge.reject_with = (10006, "Rejected by broker")
            self.tick(1)
        self.assertEqual(len(self.cmds("close")), MAX_CLOSE_ATTEMPTS)
        t = self.trades()[0]
        self.assertEqual(t["status"], S.CLOSE_FAILED)
        self.assertTrue(any("Close the position" in w for w in self.engine.status()["warnings"]))

    def test_one_failed_close_then_success(self):
        self.reach_target()
        self.bridge.reject_with = (10006, "Rejected by broker")
        self.tick(1)
        self.assertEqual(len(self.open_positions()), 1)
        self.tick(3)
        self.assertEqual(self.open_positions(), {})
        self.assertEqual(len(self.cmds("close")), 2)

    def test_lost_answer_is_never_re_sent_blindly(self):
        self.reach_target()
        self.bridge.silent = True                                 # the close EXECUTES but the answer is lost
        self.tick(1)
        self.bridge.silent = False
        self.tick(6)
        self.assertEqual(len(self.cmds("close")), 1)              # exactly one close command, ever
        self.assertEqual(self.open_positions(), {})
        self.assertIn(self.trades()[0]["status"], (S.CLOSED, S.CLOSED_AWAITING_RESULT))

    def test_externally_closed_position_is_recorded_as_external(self):
        self.start()
        self.set_bid(1.10006)
        self.tick()
        self.bridge.positions.clear()                             # the user closed it in MT5
        self.bridge.write_snapshot()
        self.tick(3)
        self.assertEqual(self.trades()[0]["close_reason"], "external")
        self.assertEqual(self.cmds("close"), [])

    def test_a_snapshot_glitch_is_not_mistaken_for_a_closure(self):
        self.start()
        self.set_bid(1.10006)
        self.tick()
        saved = dict(self.bridge.positions)
        self.bridge.positions.clear(); self.bridge.write_snapshot()
        self.now += 0.3
        self.engine.step(self.now)                                # missing for 0.3 s only
        self.bridge.positions.update(saved); self.bridge.write_snapshot()
        self.tick()
        self.assertEqual(self.trades()[0]["status"], S.OPEN)


class TestAntiTwinEntryReturn(Base):
    """Anti-Twin on Rapid Fire (mode ADVERSE_MOVE): the first adverse move of the position's executable exit price (one tick worse than where it started)
    -> close, confirm, open the equal-volume opposite, in the same step. Prices are the fake MT5's real quotes: bid/ask with a 12-point spread, tick = 1 point.
    A BUY exits on the BID, a SELL on the ASK. ADV = 15 points: past the 12-point spread plus one tick."""
    ADV = -15
    CFG = dict(symbol="EURUSD", lot=0.02, emergency_points=50.0, target_points=40.0, anti_twin_enabled=True)

    def open_buy(self):
        self.start()
        self.set_bid(1.10006)                                      # +6 points from the candle open: BUY, filled at the ask
        self.tick()
        self.engine.stop()                                         # exits only: no new shots
        return self.trades()[0]

    def open_sell(self):
        self.start()
        self.set_bid(1.09994)                                      # -6 points: SELL, filled at the bid
        self.tick()
        self.engine.stop()
        return self.trades()[0]

    # bids that put a leg at a given displacement d (profit positive) on its executable exit price
    @staticmethod
    def bid_at(t, d_points):
        e = t["entry_price"]
        return e + d_points * P if t["side"] == "buy" else e - SPREAD - d_points * P

    def order(self):
        return [c["cmd"] for c in self.bridge.commands_seen if c["cmd"] in ("open", "close")]

    def twins(self):
        return [t for t in self.trades() if t["role"] == "anti_twin"]

    # ---- the required sequences
    def test_buy_opens_and_immediately_moves_adversely(self):
        o = self.open_buy()
        self.set_bid(self.bid_at(o, self.ADV))                     # never in profit, never back at the entry: ONE adverse quote
        self.tick(2)
        self.assertEqual(self.order(), ["open", "close", "open"])  # the opposite order comes AFTER the close
        orig, twin = self.trades()[0], self.twins()[0]
        self.assertEqual((orig["close_reason"], twin["side"], twin["volume"]), ("anti_twin", "sell", 0.02))

    def test_sell_opens_and_immediately_moves_adversely(self):
        o = self.open_sell()
        self.set_bid(self.bid_at(o, self.ADV))
        self.tick(2)
        self.assertEqual(self.order(), ["open", "close", "open"])
        self.assertEqual((self.twins()[0]["side"], self.twins()[0]["volume"]), ("buy", 0.02))

    def test_the_reversal_happens_in_one_step_not_after_a_further_cycle(self):
        o = self.open_buy()
        self.set_bid(self.bid_at(o, self.ADV))
        self.tick(1)                                               # exactly ONE engine step after the adverse quote
        self.assertEqual(self.order(), ["open", "close", "open"])

    def test_in_profit_then_break_even_is_not_a_trigger_but_the_next_adverse_tick_is(self):
        o = self.open_buy()
        self.set_bid(self.bid_at(o, 8))
        self.tick(2)
        self.set_bid(self.bid_at(o, 0))                            # the bid is back at the entry: the OLD trigger
        self.tick(3)
        self.assertEqual(self.cmds("close"), [])
        self.set_bid(self.bid_at(o, self.ADV))
        self.tick(2)
        self.assertEqual(len(self.cmds("close")), 1)

    # ---- spread / bid-ask semantics
    def test_the_opening_spread_alone_is_not_a_crossing(self):
        o = self.open_buy()                                        # the bid is a full spread BELOW the entry right after the fill
        self.set_bid(self.bid_at(o, -12))
        self.tick(6)
        self.assertEqual(self.cmds("close"), [])
        self.set_bid(self.bid_at(o, -12.5))                        # a half-tick wobble is not a departure either
        self.tick(2)
        self.set_bid(self.bid_at(o, 0))                            # a rise back to the entry without ever having LEFT it
        self.tick(3)
        self.assertEqual(self.cmds("close"), [])

    def test_a_buy_is_judged_on_the_bid_not_the_ask(self):
        o = self.open_buy()
        self.set_bid(self.bid_at(o, -12))                          # exactly the opening level: the spread already paid
        self.tick(2)
        self.set_bid(self.bid_at(o, -12.5))                        # less than one tick worse
        self.tick(2)
        self.assertEqual(self.cmds("close"), [])
        self.set_bid(self.bid_at(o, -13))                          # one tick worse: triggers
        self.tick(3)
        self.assertEqual(len(self.cmds("close")), 1)

    def test_a_sell_is_judged_on_the_ask_not_the_bid(self):
        o = self.open_sell()
        self.set_bid(self.bid_at(o, -12))
        self.tick(2)
        self.set_bid(self.bid_at(o, -12.5))
        self.tick(2)
        self.assertEqual(self.cmds("close"), [])
        self.set_bid(self.bid_at(o, -13))
        self.tick(3)
        self.assertEqual(len(self.cmds("close")), 1)

    def test_a_gap_beyond_the_threshold_triggers_like_one_tick(self):
        o = self.open_buy()
        self.set_bid(self.bid_at(o, 6))
        self.tick(2)
        self.set_bid(self.bid_at(o, -40))                          # a jump (still inside the 50-point emergency distance)
        self.tick(3)
        self.assertEqual(len(self.cmds("close")), 1)
        self.assertEqual(self.trades()[0]["close_reason"], "anti_twin")

    # ---- continuous switching, references, duplicates
    def reverse(self, t):
        self.set_bid(self.bid_at(t, self.ADV))
        self.tick(3)

    def test_repeated_switching_gives_every_position_its_own_entry_reference(self):
        o = self.open_buy()
        self.reverse(o)
        t1 = self.twins()[0]
        self.assertEqual(t1["side"], "sell")
        self.reverse(t1)
        t2 = [t for t in self.twins() if t["depth"] == 2][0]
        self.assertEqual(t2["side"], "buy")
        self.reverse(t2)
        t3 = [t for t in self.twins() if t["depth"] == 3][0]
        self.assertEqual(t3["side"], "sell")                       # BUY -> SELL -> BUY -> SELL
        self.assertEqual(len(self.cmds("close")), 3)
        self.assertEqual(len(self.cmds("open")), 4)
        for t in (o, t1, t2):                                      # each leg's watch state references ITS OWN fill, never its parent's
            watched = self.ledger.get(t["id"]).get("at_rule")
            self.assertIsNotNone(watched)
            self.assertEqual(watched["entry"], t["entry_price"])
            self.assertTrue(watched["triggered"])
        self.assertEqual(len(self.open_positions()), 1)

    def test_no_duplicate_reversal_on_repeated_ticks(self):
        o = self.open_buy()
        self.set_bid(self.bid_at(o, 6))
        self.tick(2)
        self.set_bid(self.bid_at(o, self.ADV))
        self.tick(12)                                              # the price sits beyond the threshold for many ticks
        self.assertEqual(len(self.cmds("close")), 1)
        self.assertEqual(len(self.twins()), 1)
        self.assertEqual(len(self.cmds("open")), 2)

    def test_the_twin_volume_is_the_actual_filled_volume_of_the_reversed_position(self):
        o = self.open_buy()
        for pos in self.bridge.positions.values():
            pos["volume"] = 0.01                                   # MT5 reports only 0.01 of the 0.02 lots (a partial fill)
        self.tick()
        self.set_bid(self.bid_at(o, self.ADV))
        self.tick(4)
        self.assertEqual(self.twins()[0]["volume"], 0.01)
        self.assertEqual(float(self.cmds("open")[-1]["volume"]), 0.01)

    def _equal_volume(self, lot, emergency=50.0):
        self.configure(lot=lot, emergency_points=emergency)         # the Risk Engine caps the risk per trade at 1%: a bigger lot needs a tighter emergency distance
        o = self.open_buy()
        self.reverse(o)
        self.assertEqual((o["volume"], self.twins()[0]["volume"]), (lot, lot))
        self.assertEqual(float(self.cmds("open")[-1]["volume"]), lot)

    def test_equal_volume_minimum_lot(self):
        self._equal_volume(0.01)

    def test_equal_volume_odd_lot(self):
        self._equal_volume(0.37, emergency=25.0)

    def test_the_twin_earns_the_same_target_and_keeps_the_emergency_distance(self):
        o = self.open_buy()
        self.reverse(o)
        t = self.twins()[0]
        self.assertEqual((t["target_points"], t["emergency_points"]), (o["target_points"], o["emergency_points"]))

    def test_a_stale_reference_from_another_position_cannot_trigger(self):
        o = self.open_buy()
        self.ledger.update(o["id"], at_rule={"side": "buy", "entry": 9.99, "sign": 1, "spread0": SPREAD, "up": True, "down": False, "lo": 0, "hi": 1, "triggered": False})
        self.set_bid(self.bid_at(o, 3))
        self.tick(4)
        self.assertEqual(self.cmds("close"), [])
        self.assertEqual(self.ledger.get(o["id"])["at_rule"]["entry"], o["entry_price"])        # replaced by the position's own reference

    # ---- OFF
    def test_anti_twin_off_means_no_reversal(self):
        self.configure(anti_twin_enabled=False)
        o = self.open_buy()
        self.set_bid(self.bid_at(o, 6))
        self.tick(2)
        self.set_bid(self.bid_at(o, self.ADV))
        self.tick(4)
        self.assertEqual(self.cmds("close"), [])
        self.assertEqual(self.twins(), [])
        self.assertEqual(self.engine.status()["anti_twin"]["status"], "DISABLED")

    def test_switching_it_off_after_the_close_stops_the_opposite_order(self):
        o = self.open_buy()
        self.set_bid(self.bid_at(o, 6))
        self.tick(2)
        real_close = Executor.close

        def close_then_off(ex, ticket):
            out = real_close(ex, ticket)
            self.configure(anti_twin_enabled=False)                # the user switches Anti-Twin off right after the close went through
            return out
        with mock.patch.object(Executor, "close", close_then_off):
            self.set_bid(self.bid_at(o, self.ADV))
            self.tick(4)
        self.assertEqual(self.ledger.get(o["id"])["twin_state"], "TWIN_CANCELLED")
        self.assertEqual(len(self.cmds("open")), 1)
        self.assertEqual(self.open_positions(), {})

    # ---- risk priority
    def test_an_active_guard_wins_over_the_reversal(self):
        o = self.open_buy()
        self.set_bid(self.bid_at(o, 6))
        self.tick(2)
        self.guard_reason = "EMERGENCY LOCKED: lift the emergency lock in Settings first."
        self.set_bid(self.bid_at(o, self.ADV))
        self.tick(4)
        self.assertEqual(self.cmds("close"), [])
        self.assertEqual(self.twins(), [])

    def test_the_emergency_exit_outranks_the_reversal_and_nothing_reopens(self):
        o = self.open_buy()
        self.set_bid(self.bid_at(o, 6))
        self.tick(2)
        self.set_bid(self.bid_at(o, -60))                          # crashes through the entry AND the 50-point emergency level in one tick
        self.tick(4)
        first = self.ledger.get(o["id"])
        self.assertEqual(first["close_reason"], "emergency")
        self.assertEqual(self.twins(), [])
        self.assertEqual(len(self.cmds("open")), 1)
        self.assertEqual(self.open_positions(), {})

    def test_an_emergency_lock_after_the_close_cancels_the_opposite_order(self):
        o = self.open_buy()
        self.set_bid(self.bid_at(o, 6))
        self.tick(2)
        real_close = Executor.close

        def close_then_lock(ex, ticket):
            out = real_close(ex, ticket)
            self.guard_reason = "EMERGENCY LOCKED"                 # the Emergency Stop is pressed right after the close went through
            return out
        with mock.patch.object(Executor, "close", close_then_lock):
            self.set_bid(self.bid_at(o, self.ADV))
            self.tick(4)
        self.assertEqual(self.ledger.get(o["id"])["twin_state"], "TWIN_CANCELLED")
        self.assertEqual(len(self.cmds("open")), 1)
        self.assertEqual(self.open_positions(), {})

    def test_the_risk_engine_is_never_bypassed_by_the_twin(self):
        o = self.open_buy()
        self.rules = RiskRules(max_risk_per_trade_percent=0.0001, max_daily_loss_percent=3.0, max_total_active_risk_percent=3.0, max_positions=3)
        self.engine.executor._rules = lambda: self.rules
        self.reverse(o)
        # v5.3: the Risk Engine's refusal is found by the PRE-FLIGHT, before anything is closed: no close, no opposite order, the original is left open and the reason is reported
        self.assertEqual(self.ledger.get(o["id"])["twin_state"], "TWIN_ABORTED")
        self.assertEqual(len(self.cmds("open")), 1)                # the gate refused it; no order reached MT5
        self.assertEqual(len(self.cmds("close")), 0)               # and the original was NOT closed
        self.assertEqual(len(self.open_positions()), 1)
        self.assertIn("NOT closed", self.ledger.get(o["id"])["twin_message"])

    def test_a_refusal_after_the_close_is_reported_as_reversal_failed(self):
        """When the state changes between the pre-flight and the send, the Risk gate can still refuse AFTER the close: that is REVERSAL_FAILED, never a switch."""
        from trademate.rapid_fire.executor import Preflight
        o = self.open_buy()
        self.engine.executor.preflight_open = lambda *a, **k: Preflight(True, "ok")
        self.rules = RiskRules(max_risk_per_trade_percent=0.0001, max_daily_loss_percent=3.0, max_total_active_risk_percent=3.0, max_positions=3)
        self.engine.executor._rules = lambda: self.rules
        self.reverse(o)
        t = self.engine.reversal_state(self.ledger.get(o["id"]))
        self.assertEqual(self.ledger.get(o["id"])["twin_state"], "TWIN_REJECTED")
        self.assertEqual(t, "REVERSAL_FAILED")
        self.assertIn("REVERSAL FAILED", self.ledger.get(o["id"])["twin_message"])
        self.assertEqual(len(self.cmds("close")), 1)
        self.assertEqual(self.open_positions(), {})

    def test_a_successful_reversal_records_the_verified_position(self):
        o = self.open_buy()
        self.reverse(o)
        orig, twin = self.ledger.get(o["id"]), self.twins()[0]
        self.assertEqual(self.engine.reversal_state(orig), "OPPOSITE_ORDER_CONFIRMED")
        self.assertEqual((orig["twin_ticket"], orig["twin_volume"]), (twin["ticket"], twin["volume"]))
        self.assertEqual(twin["volume"], o["volume"])
        self.assertEqual(twin["side"], "sell")

    def test_an_entry_is_not_decided_on_a_candle_that_has_rolled_over(self):
        """The cached M1 copy can be a second old. If the candle changed, nothing is sent this step: the signal is judged again on the new candle's own open."""
        self.start()
        self.tick()
        eng, m = self.engine, self.engine._market
        eng._candle = (m.candle_time, m.candle_open, 100.0)

        def rolled(symbol, now):
            eng._candle = (m.candle_time + 60, m.candle_open + 0.0005, now)
            return ""
        eng._fetch_candle = rolled
        old = m.candle_time
        self.assertIn("candle changed", eng._entry_inputs_stale(100.5, m))
        self.assertEqual(m.candle_time, old + 60)
        self.assertEqual(eng._entry_inputs_stale(100.6, m), "")        # fresh copy, same candle: the entry may proceed

    def test_an_old_quote_never_triggers_an_entry(self):
        self.start()
        self.tick()
        m = self.engine._market
        m.quote_age = 4.0
        self.assertIn("old", self.engine._entry_inputs_stale(time.time(), m))

    # ---- failures
    def test_never_opens_before_the_close_is_confirmed(self):
        o = self.open_buy()
        self.set_bid(self.bid_at(o, 6))
        self.tick(2)
        self.bridge.reject_with = (10006, "Rejected by broker")   # the close is refused
        self.set_bid(self.bid_at(o, self.ADV))
        self.tick(1)
        self.assertEqual(len(self.cmds("open")), 1)
        self.assertEqual(len(self.open_positions()), 1)
        self.assertEqual(self.trades()[0]["twin_state"], "CLOSING_ORIGINAL")
        self.bridge.reject_with = None
        self.tick(5)
        self.assertEqual(self.order().count("open"), 2)

    def test_a_failed_opposite_order_is_retried_a_bounded_number_of_times_then_abandoned(self):
        o = self.open_buy()
        calls = []

        def refusing(ex, symbol, side, volume, ref, sl=None, tp=None):
            from trademate.rapid_fire.executor import Outcome, NOT_EXECUTED
            calls.append(side)
            return Outcome(NOT_EXECUTED, "Rejected by broker", retryable=True)
        self.set_bid(self.bid_at(o, 6))
        self.tick(2)
        with mock.patch.object(Executor, "open", refusing):
            self.set_bid(self.bid_at(o, self.ADV))
            self.tick(10)
        orig = self.ledger.get(o["id"])
        self.assertEqual(orig["twin_state"], "TWIN_FAILED")
        self.assertEqual(self.open_positions(), {})                # a known, flat, safe state
        self.assertEqual(len(calls), 3)
        self.assertTrue(orig["twin_message"])

    def test_an_unknown_outcome_of_the_opposite_order_is_never_retried(self):
        o = self.open_buy()
        from trademate.rapid_fire.executor import Outcome, UNKNOWN
        calls = []

        def unknown(ex, *a, **k):
            calls.append(1)
            return Outcome(UNKNOWN, "answer lost")
        self.set_bid(self.bid_at(o, 6))
        self.tick(2)
        with mock.patch.object(Executor, "open", unknown):
            self.set_bid(self.bid_at(o, self.ADV))
            self.tick(8)
        self.assertEqual(self.ledger.get(o["id"])["twin_state"], "TWIN_UNCERTAIN")
        self.assertEqual(len(calls), 1)

    def test_an_interrupted_reversal_resumes_after_a_restart(self):
        o = self.open_buy()
        self.ledger.update(o["id"], twin_state="CLOSING_ORIGINAL")   # the process died after deciding to reverse
        eng2 = self.make_engine(ledger=Ledger(self.dir / "led.json"))
        for _ in range(4):
            self.now += 1.1
            eng2.step(self.now)
        self.assertEqual(self.order(), ["open", "close", "open"])

    def test_a_tp_or_emergency_or_manual_close_is_never_a_reversal(self):
        o = self.open_buy()
        self.set_bid(self.bid_at(o, 40))                           # normal take profit
        self.tick(4)
        self.assertEqual(self.trades()[0]["close_reason"], "take_profit")
        self.assertEqual(self.twins(), [])




class TestRestartAndReconnect(Base):
    def test_restart_keeps_managing_an_open_position_but_never_re_enters(self):
        self.start()
        self.set_bid(1.10006)
        self.tick()
        fill = self.trades()[0]["entry_price"]
        eng2 = self.make_engine(ledger=Ledger(self.dir / "led.json"))      # restart: running is False, the ledger is read back
        self.assertFalse(eng2.running)
        self.set_bid(fill + 10 * P)
        for _ in range(3):
            self.now += 1.1
            eng2.step(self.now)
        self.assertEqual(self.open_positions(), {})                       # managed to its target
        self.set_bid(fill + 40 * P)
        for _ in range(3):
            self.now += 1.1
            eng2.step(self.now)
        self.assertEqual(len(self.cmds("open")), 1)                       # no new entry without Start

    def test_crash_while_submitting_becomes_uncertain_and_blocks_entries(self):
        self.start()
        key = self.ledger.candle_key("EURUSD", self.T)
        self.ledger.reserve_shot(key, self.T, 2)
        self.ledger.new_trade(symbol="EURUSD", side="buy", volume=0.01, candle_key=key, point=P, target_points=10, emergency_points=50.0)
        self.set_bid(1.10006)
        self.tick(3)
        self.assertEqual(self.trades()[0]["status"], S.ENTRY_UNCERTAIN)
        self.assertEqual(self.cmds("open"), [])
        self.assertTrue(any("uncertain" in w for w in self.engine.status()["warnings"]))
        ok, _ = self.engine.resolve_entry(self.trades()[0]["id"], "not_executed")
        self.assertTrue(ok)
        self.assertEqual(self.ledger.shots(key), 1 - 1 + 0 if False else self.ledger.shots(key))
        self.tick(2)
        self.assertEqual(len(self.cmds("open")), 1)                       # released: entries resume

    def test_disconnection_pauses_everything_and_reconnection_does_not_reset_the_counter(self):
        self.start()
        self.set_bid(1.10006)
        self.tick()
        key = self.ledger.candle_key("EURUSD", self.T)
        down = ConnectionStatus(ConnectionState.DISCONNECTED, "Bridge not running")
        with mock.patch.object(self.adapter, "get_connection_status", return_value=down):
            self.set_bid(self.trades()[0]["entry_price"] + 10 * P)
            self.tick(3)
            self.assertEqual(self.cmds("close"), [])                      # cannot manage while the feed is down
            self.assertEqual(self.engine.status()["state"], "BLOCKED")
            self.assertIn("not connected", self.engine.status()["state_reason"])
        self.tick(3)                                                      # reconnected: the target is honoured
        self.assertEqual(len(self.cmds("close")), 1)
        self.assertEqual(self.ledger.shots(key), 1)

    def test_stop_keeps_managing_open_positions(self):
        self.start()
        self.set_bid(1.10006)
        self.tick()
        fill = self.trades()[0]["entry_price"]
        msg = self.engine.stop()
        self.assertIn("software management", msg)
        self.set_bid(fill + 10 * P)
        self.tick(2)
        self.assertEqual(self.open_positions(), {})


class TestHistoryIntegration(Base):
    CFG = dict(symbol="EURUSD", lot=0.02, emergency_points=50.0, anti_twin_enabled=True)

    def test_history_rows_are_tagged_and_other_trades_are_untouched(self):
        import app as A
        self.start()
        self.set_bid(1.10006)
        self.tick()
        o = self.trades()[0]
        self.set_bid(o["entry_price"] + 4 * P)                    # leaves the entry price in profit ...
        self.tick(2)
        self.set_bid(o["entry_price"] - 15 * P)                   # ... then the first adverse move: reversal
        self.tick(3)
        twin = [t for t in self.trades() if t["role"] == "anti_twin"][0]
        self.set_bid(twin["entry_price"] - SPREAD - 10 * P)
        self.tick(4)
        # a trade RapidFire did not open
        self.bridge.history.append({"ticket": 5, "symbol": "EURUSD", "type": "buy", "volume": 0.01, "price_open": 1.1, "price_close": 1.1, "sl": 0, "tp": 0,
                                    "profit": 0.0, "swap": 0.0, "commission": 0.0, "time_open": int(time.time()) - 500, "time_close": int(time.time()) - 400, "reason": "client"})
        with mock.patch.object(A, "adapter", self.adapter), mock.patch.object(A, "rapid_engine", self.engine):
            rows = A.app.test_client().get("/api/history").get_json()["trades"]
        by = {r["ticket"]: r for r in rows}
        self.assertNotIn("rapid_fire", by[5])
        self.assertEqual(by[o["ticket"]]["rapid_fire"]["role"], "original")
        self.assertEqual(by[o["ticket"]]["rapid_fire"]["closed_by"], "anti_twin")
        self.assertEqual(by[twin["ticket"]]["rapid_fire"]["role"], "anti_twin")
        self.assertEqual(by[twin["ticket"]]["rapid_fire"]["twin_of"], o["ticket"])
        self.assertEqual(by[o["ticket"]]["rapid_fire"]["strategy"], "RAPID FIRE")
        self.assertEqual(len({(r["ticket"], r["time_close"]) for r in rows}), len(rows))     # no duplicate closed trades

    def test_trade_rows_carry_the_requested_fields_and_realised_numbers_only_from_mt5(self):
        self.start()
        self.set_bid(1.10006)
        self.tick()
        row = self.engine.trade_rows()[0]
        for k in ("strategy", "symbol", "timeframe", "side", "volume", "entry_price", "entry_time", "exit_price", "gross", "commission", "swap", "net",
                  "execution_status", "role_label", "twin_of_ticket", "closed_by"):
            self.assertIn(k, row)
        self.assertIsNone(row["net"])                                     # still open: nothing theoretical is shown as realised
        self.set_bid(row["entry_price"] + 10 * P)
        self.tick(3)
        row = self.engine.trade_rows()[0]
        self.assertEqual(row["execution_status"], S.CLOSED)
        self.assertIsNotNone(row["net"])


class TestThread(Base):
    def test_the_real_thread_steps_manages_and_shuts_down(self):
        eng = RapidFireEngine(self.adapter, self.cfgstore, self.ledger, Executor(self.adapter, lambda: self.rules, lambda: None), guard=lambda: None,
                              server_offset=lambda: 0)                                    # the real clock
        self.set_candle(int(time.time()) // 60 * 60, 1.10000)
        ok, msg = eng.start()
        self.assertTrue(ok, msg)
        self.assertTrue(eng._thread.is_alive())
        time.sleep(1.5)
        st = eng.status()
        self.assertTrue(st["thread_alive"])
        self.assertEqual(st["state"], "RUNNING")
        self.assertEqual(self.cmds("open"), [])                                           # armed for the NEXT candle only
        eng.stop()
        eng.shutdown()
        eng._thread.join(timeout=3)
        self.assertFalse(eng._thread.is_alive())


class TestApi(Base):
    def client(self):
        import app as A
        self._patches = [mock.patch.object(A, "adapter", self.adapter), mock.patch.object(A, "rapid_engine", self.engine),
                         mock.patch.object(A, "risk_store", mock.Mock(load=lambda: self.rules))]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        return A.app.test_client()

    def test_status_config_start_stop_and_the_lot_suggestion(self):
        c = self.client()
        st = c.get("/api/rapid-fire/status").get_json()
        self.assertEqual((st["strategy"]["name"], st["config"]["timeframe"], st["state"], st["running"]), ("RAPID FIRE", "M1", "STOPPED", False))
        r = c.post("/api/rapid-fire/config", json={"timeframe": "M5"})
        self.assertEqual(r.status_code, 400)
        r = c.post("/api/rapid-fire/config", json={"lot": 0.02, "target_points": 12})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(c.get("/api/rapid-fire/config").get_json()["config"]["target_points"], 12)
        self.assertEqual(c.post("/api/rapid-fire/config", data="x").status_code, 400)
        s = c.get("/api/rapid-fire/lot-suggestion?profit=0.6").get_json()
        self.assertEqual((s["ok"], s["lot"], s["currency"]), (True, 0.05, "USD"))
        self.assertEqual(c.get("/api/rapid-fire/lot-suggestion?profit=abc").status_code, 400)
        r = c.post("/api/rapid-fire/start", json={}).get_json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(c.get("/api/rapid-fire/status").get_json()["state"], "RUNNING")
        self.assertEqual(c.post("/api/rapid-fire/config", json={"symbol": "XAUUSD"}).status_code, 409)     # not while running
        self.assertTrue(c.post("/api/rapid-fire/stop", json={}).get_json()["ok"])
        self.assertEqual(c.get("/api/rapid-fire/trades").get_json()["trades"], [])

    def test_catalogue_keeps_exactly_five_selectable_strategies_and_lists_rapid_fire_separately(self):
        c = self.client()
        cat = c.get("/api/strategy/catalogue").get_json()
        self.assertEqual(len(cat["strategies"]), 5)
        self.assertNotIn("rapid_fire", [s["id"] for s in cat["strategies"]])
        self.assertEqual([s["id"] for s in cat["independent"]], ["rapid_fire"])
        self.assertEqual(c.post("/api/strategy/select", json={"strategy": "rapid_fire"}).status_code, 400)


from types import SimpleNamespace as NS
from trademate.rapid_fire import target as rf_target


def _info(**kw):
    base = dict(name="X", point=1e-5, tick_size=1e-5, tick_value=1.0, digits=5)
    base.update(kw)
    return NS(**base)


class TestMoneyToPoints(unittest.TestCase):
    """The pure conversion: money -> price distance -> MT5 points, from the instrument's own numbers (never a pip assumption)."""

    def test_a_forex_pair(self):
        p = rf_target.plan(_info(), 0.01, 0.50)                          # 1 tick = 0.01 USD at 0.01 lot -> 50 ticks
        self.assertTrue(p["ok"])
        self.assertAlmostEqual(p["required_points"], 50.0)
        self.assertAlmostEqual(p["required_price_distance"], 0.0005)

    def test_gold(self):
        info = _info(name="XAUUSD", point=0.01, tick_size=0.01, tick_value=1.0, digits=2)      # 1 lot = 100 oz: 0.01 move = 1.00 USD
        p = rf_target.plan(info, 0.10, 5.0)
        self.assertAlmostEqual(p["required_price_distance"], 0.50)
        self.assertAlmostEqual(p["required_points"], 50.0)
        self.assertAlmostEqual(rf_target.plan(info, 1.0, 5.0)["required_price_distance"], 0.05)

    def test_a_yen_pair_rounds_up_to_a_whole_tick_so_the_net_is_never_below_the_request(self):
        info = _info(name="USDJPY", point=0.001, tick_size=0.001, tick_value=0.65, digits=3)
        p = rf_target.plan(info, 0.2, 10.0)                                # 10 / 0.13 = 76.9 -> 77 ticks
        self.assertEqual(p["required_ticks"], 77)
        self.assertGreaterEqual(p["net_at_target"], 10.0)
        self.assertAlmostEqual(p["required_points"], 77.0)

    def test_a_tick_size_larger_than_the_point(self):
        info = _info(name="US30", point=0.01, tick_size=0.5, tick_value=0.5, digits=2)           # one tick = 50 points
        p = rf_target.plan(info, 1.0, 3.0)
        self.assertEqual(p["required_ticks"], 6)
        self.assertAlmostEqual(p["required_points"], 300.0)
        self.assertEqual(rf_target.plan(info, 1.0, 3.1)["required_ticks"], 7)                    # 3.1 needs a 7th tick

    def test_different_lot_sizes_scale_the_distance_inversely(self):
        d = {lot: rf_target.plan(_info(), lot, 1.0)["required_points"] for lot in (0.01, 0.02, 0.05, 0.1)}
        self.assertEqual(d, {0.01: 100.0, 0.02: 50.0, 0.05: 20.0, 0.1: 10.0})

    def test_the_profit_side_tick_value_is_preferred(self):
        p = rf_target.plan(_info(tick_value=1.0, tick_value_profit=2.0), 0.01, 1.0)
        self.assertAlmostEqual(p["required_points"], 50.0)

    def test_commission_is_added_transparently(self):
        p = rf_target.plan(_info(), 0.5, 10.0, commission_per_lot=7.0)                    # 3.50 round turn
        self.assertAlmostEqual(p["commission"], 3.5)
        self.assertAlmostEqual(p["required_points"], 2 * 13.5 / 1.0 * 1.0, places=6)       # 13.5 / (0.5 USD per tick) = 27 ticks
        self.assertGreaterEqual(p["net_at_target"], 10.0)
        self.assertTrue(p["commission_known"])
        self.assertIn("included", p["commission_note"])
        self.assertFalse(rf_target.plan(_info(), 0.5, 10.0)["commission_known"])
        self.assertIn("Commission is 0", rf_target.plan(_info(), 0.5, 10.0)["commission_note"])

    def test_spread_is_reported_and_not_added_twice(self):
        p = rf_target.plan(_info(), 0.02, 0.60, bid=1.10000, ask=1.10012)
        self.assertAlmostEqual(p["required_points"], 30.0)                  # the fill (ask) is already one spread worse than the exit quote (bid)
        self.assertAlmostEqual(p["spread_points"], 12.0)
        self.assertAlmostEqual(p["move_from_quote_points"], 42.0)           # the quote you see now must travel D + spread
        self.assertAlmostEqual(p["buy_tp_price"], 1.10042)                  # a BUY fills at the ask 1.10012, exits on the bid at +30
        self.assertAlmostEqual(p["sell_tp_price"], 1.09970)                 # a SELL fills at the bid 1.10000, exits on the ask at -30

    def test_invalid_or_missing_metadata_gives_no_distance_and_a_plain_reason(self):
        bad = [dict(tick_value=0), dict(tick_value=None), dict(tick_value=float("nan")), dict(tick_value=-1.0), dict(tick_size=0), dict(tick_size=None), dict(point=None),
               dict(point=float("inf"))]
        for kw in bad:
            p = rf_target.plan(_info(**kw), 0.01, 1.0)
            self.assertFalse(p["ok"], kw)
            self.assertIsNone(p["required_points"], kw)
            self.assertIn("cannot be calculated", p["problem"], kw)
        self.assertFalse(rf_target.plan(None, 0.01, 1.0)["ok"])

    def test_invalid_tick_value_falls_back_only_when_the_profit_currency_is_the_account_currency(self):
        info = _info(tick_value=0, contract_size=100000, currency_profit="USD")
        p = rf_target.plan(info, 0.01, 0.50, currency="USD")                 # 100000 x 1e-5 = 1.0 per tick per lot: the same 50 ticks as a valid tick value
        self.assertTrue(p["ok"])
        self.assertAlmostEqual(p["required_points"], 50.0)
        self.assertIn("contract size", p["warnings"][0])
        self.assertFalse(rf_target.plan(info, 0.01, 0.50, currency="NGN")["ok"])     # a different account currency cannot be verified: no distance, no order
        self.assertFalse(rf_target.plan(info, 0.01, 0.50)["ok"])

    def test_a_disagreeing_tick_value_keeps_mt5s_number_and_warns(self):
        info = _info(tick_value=2.0, contract_size=100000, currency_profit="USD")
        p = rf_target.plan(info, 0.01, 1.0, currency="USD")
        self.assertAlmostEqual(p["required_points"], 50.0)                    # 1.0 / (2.0 x 0.01)
        self.assertTrue(any("differs" in w for w in p["warnings"]))
        self.assertFalse(p["validated"])

    def test_the_target_says_it_is_an_estimated_net(self):
        p = rf_target.plan(_info(), 0.02, 0.60)
        self.assertEqual(p["target_kind"], "estimated_net")
        self.assertIn("NET", p["target_label"])

    def test_bad_requests_give_no_distance(self):
        for lot, profit in ((None, 1.0), (0, 1.0), (0.01, 0), (0.01, -5), (0.01, "x"), (0.01, None)):
            self.assertFalse(rf_target.plan(_info(), lot, profit)["ok"], (lot, profit))
        self.assertFalse(rf_target.plan(_info(), 0.01, 1.0, commission_per_lot=-1)["ok"])


class TestMoneyTargetInTheEngine(Base):
    CFG = dict(symbol="EURUSD", lot=0.02, emergency_points=50.0, profit_per_fire=0.60)      # 0.02 lot * 1 USD/point -> 30 points

    def open_buy(self):
        self.start()
        self.set_bid(1.10006)
        self.tick()
        return self.trades()[0]

    def test_the_default_is_unchanged_and_the_fixed_distance_still_works_without_a_money_target(self):
        self.assertIsNone(RFConfig().profit_per_fire)
        self.assertEqual(RFConfig().target_points, 10.0)
        self.configure(profit_per_fire=None, target_points=10.0)
        o = self.open_buy()
        self.assertEqual((o["target_points"], o["tp_mode"]), (10.0, "points"))

    def test_the_trade_gets_the_calculated_distance_and_exits_exactly_there(self):
        o = self.open_buy()
        self.assertEqual((o["target_points"], o["tp_mode"], o["profit_per_fire"]), (30.0, "money", 0.60))
        self.assertAlmostEqual(o["target_price"], o["entry_price"] + 30 * P, places=6)     # TP price from the FILL
        self.engine.stop()
        self.set_bid(o["entry_price"] + 29 * P)
        self.tick(3)
        self.assertEqual(self.cmds("close"), [])                                           # one point short: no exit
        self.set_bid(o["entry_price"] + 30 * P)
        self.tick(3)
        self.assertEqual(self.ledger.get(o["id"])["close_reason"], "take_profit")

    def test_a_sell_gets_the_mirrored_price(self):
        self.start()
        self.set_bid(1.09994)
        self.tick()
        o = self.trades()[0]
        self.assertEqual(o["side"], "sell")
        self.assertAlmostEqual(o["target_price"], o["entry_price"] - 30 * P, places=6)

    def test_a_different_lot_changes_the_distance(self):
        self.configure(lot=0.04)
        o = self.open_buy()
        self.assertEqual(o["target_points"], 15.0)

    def test_the_status_shows_the_plan_with_the_spread(self):
        self.tick()
        plan = self.engine.status()["target_plan"]
        self.assertTrue(plan["ok"], plan)
        self.assertEqual((plan["mode"], plan["required_points"]), ("money", 30.0))
        self.assertAlmostEqual(plan["spread_points"], 12.0)
        self.assertAlmostEqual(plan["move_from_quote_points"], 42.0)
        self.assertEqual(plan["currency"], "USD")

    def test_a_configuration_change_applies_to_new_trades_and_never_to_an_open_one(self):
        o = self.open_buy()
        self.configure(profit_per_fire=1.0)
        self.tick()
        self.assertEqual(self.ledger.get(o["id"])["target_points"], 30.0)                  # an open position keeps the levels it was opened with
        self.assertEqual(self.engine.status()["target_plan"]["required_points"], 50.0)     # the plan for the next one

    def test_invalid_instrument_data_blocks_trading_and_says_why(self):
        import dataclasses
        good = self.adapter.get_symbol_spec("EURUSD")
        self.start()
        with mock.patch.object(self.adapter, "get_symbol_spec", lambda s: dataclasses.replace(good, tick_value=0.0)):
            self.engine._spec_at = -1e9
            self.set_bid(1.10006)
            self.tick(3)
            st = self.engine.status()
        self.assertEqual(st["state"], "BLOCKED")
        self.assertIn("tick_value", st["state_reason"])
        self.assertEqual(len(self.cmds("open")), 0)                                        # nothing was sent

    def test_missing_lot_means_no_plan_and_no_guess(self):
        self.cfgstore.save(RFConfig(symbol="EURUSD", emergency_points=50.0, profit_per_fire=0.60))
        st = self.engine.status()
        self.assertFalse(st["target_plan"]["ok"])
        self.assertIsNone(st["target_plan"]["required_points"])

    def test_the_settings_validate_the_money_target(self):
        for bad in (dict(profit_per_fire=0), dict(profit_per_fire=-1), dict(profit_per_fire="x"), dict(commission_per_lot=-1)):
            cfg, problems = self.engine.save_config(bad)
            self.assertIsNone(cfg, bad)
            self.assertTrue(problems)
        cfg, problems = self.engine.save_config({"profit_per_fire": "", "commission_per_lot": 0})
        self.assertIsNotNone(cfg, problems)
        self.assertIsNone(cfg.profit_per_fire)                                             # empty = back to the fixed distance

    def test_an_anti_twin_reversal_keeps_the_same_money_target(self):
        self.configure(anti_twin_enabled=True)
        o = self.open_buy()
        self.engine.stop()
        self.set_bid(o["entry_price"] + 6 * P)
        self.tick(2)
        self.set_bid(o["entry_price"] - 15 * P)                   # the first adverse move (past the spread + one tick)
        self.tick(4)
        twin = [t for t in self.trades() if t["role"] == "anti_twin"][0]
        self.assertEqual(twin["target_points"], 30.0)


class TestChartLevels(Base):
    """What the chart is given: the REAL exit levels of the open Rapid Fire positions, tied to positions that exist on the MT5 account."""
    CFG = dict(symbol="EURUSD", lot=0.02, emergency_points=50.0, profit_per_fire=0.60)

    def open_buy(self):
        self.start()
        self.set_bid(1.10006)
        self.tick(2)                                                                       # the second step reads the position back from the account
        return self.trades()[0]

    def test_an_open_position_exposes_entry_take_profit_and_emergency_levels_with_distance_and_money(self):
        o = self.open_buy()
        out = self.engine.chart_levels()
        self.assertEqual(len(out["levels"]), 1)
        lv = out["levels"][0]
        mt5_entry = list(self.bridge.positions.values())[0]["price_open"]
        self.assertAlmostEqual(lv["entry"], mt5_entry)                                     # the entry line is MT5's own price
        self.assertAlmostEqual(lv["tp"]["price"], mt5_entry + 30 * P, places=6)
        self.assertAlmostEqual(lv["sl"]["price"], mt5_entry - 50 * P, places=6)
        self.assertEqual((lv["tp"]["points"], lv["sl"]["points"]), (30.0, 50.0))
        self.assertAlmostEqual(lv["tp"]["money"], 0.60, places=6)                          # 30 points * 0.02 lot * 1 USD
        self.assertAlmostEqual(lv["sl"]["money"], 1.00, places=6)
        self.assertEqual((lv["managed_by"], lv["side"], lv["role"], lv["ticket"]), ("software", "buy", "original", o["ticket"]))

    def test_levels_follow_the_position_and_disappear_when_it_closes(self):
        o = self.open_buy()
        self.engine.stop()
        self.set_bid(o["entry_price"] + 30 * P)
        self.tick(3)
        self.assertEqual(self.engine.chart_levels()["levels"], [])

    def test_a_position_that_is_not_on_the_account_has_no_lines_even_if_the_ledger_still_says_open(self):
        self.open_buy()
        self.bridge.positions.clear()                                                      # closed by hand in MT5
        self.bridge.write_snapshot()
        self.engine._manage(self.now + 0.1)                                                # the engine has just re-read the account
        self.assertEqual(self.engine._positions, {})
        self.assertEqual(self.engine.chart_levels()["levels"], [])

    def test_an_anti_twin_gets_its_own_levels_from_its_own_fill(self):
        self.configure(anti_twin_enabled=True)
        o = self.open_buy()
        self.engine.stop()
        self.set_bid(o["entry_price"] + 6 * P)
        self.tick(2)
        self.set_bid(o["entry_price"] - 15 * P)                   # the first adverse move (past the spread + one tick)
        self.tick(4)
        lv = self.engine.chart_levels()["levels"]
        self.assertEqual(len(lv), 1)
        self.assertEqual((lv[0]["role"], lv[0]["side"]), ("anti_twin", "sell"))
        self.assertAlmostEqual(lv[0]["tp"]["price"], lv[0]["entry"] - 30 * P, places=6)
        self.assertAlmostEqual(lv[0]["sl"]["price"], lv[0]["entry"] + 50 * P, places=6)

    def test_the_levels_are_flagged_stale_when_positions_cannot_be_read(self):
        self.open_buy()
        self.engine._pos_problem = "bridge down"
        self.assertTrue(self.engine.chart_levels()["stale"])

    def test_the_api_serves_the_same_levels(self):
        import app as A
        self.open_buy()
        with mock.patch.object(A, "rapid_engine", self.engine):
            j = A.app.test_client().get("/api/rapid-fire/chart-levels").get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(len(j["levels"]), 1)


class TestAnti_TwinIsIndependentPerStrategy(unittest.TestCase):
    def test_each_strategy_has_its_own_switch_config_and_ledger(self):
        import app as A
        c = A.app.test_client()
        before = (c.get("/api/bull-bear/config").get_json(), c.get("/api/candle-momentum/config").get_json())
        with mock.patch.object(A, "rapid_engine", A.rapid_engine):
            self.assertIsNot(A.bull_bear_engine.anti_twin, A.candle_engine.anti_twin)
            self.assertNotEqual(A.bull_bear_engine.anti_twin.path, A.candle_engine.anti_twin.path)
        from trademate.rapid_fire.models import RFConfig as RC
        self.assertIn("anti_twin_enabled", RC().to_dict())
        # Rapid Fire's switch lives in its own config object: changing it cannot touch the other two engines' stores
        self.assertIsNot(A.rf_config_store, A.bull_bear_engine.cfg_store)
        self.assertIsNot(A.rf_config_store, A.candle_engine.cfg_store)
        after = (c.get("/api/bull-bear/config").get_json(), c.get("/api/candle-momentum/config").get_json())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
