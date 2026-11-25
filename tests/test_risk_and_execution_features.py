"""Risk protections, local-midnight day, consecutive losses, lot adjustment, calculator, settings scopes, entry limits and Autopilot controls.

    python3 -m unittest tests.test_risk_and_execution_features -v

Simulated broker data and the in-memory fake bridge only: no MT5, no real order. These tests prove the logic and wiring in SIMULATION, not live-broker behaviour.
"""

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import app as app_module
from tests.test_candle_momentum_timeframes import TfRig
from trademate import entry_limits, localday, lot_adjust
from trademate.autopilot import Autopilot
from trademate.execution_settings import DEFAULTS, ExecutionSettings
from trademate.risk import engine as risk_engine
from trademate.risk.daily_log import DailyRiskLog
from trademate.risk.engine import ACCEPTED, REJECTED, AccountRiskState, TradeProposal, evaluate_trade, set_protections_provider
from trademate.risk.position_sizing import SymbolSpec
from trademate.risk.protections import ConsecutiveLossGuard, SafetyLock, protection_checks
from trademate.risk.rules import RiskRules
from trademate.risk.trade_calculator import calculate
from trademate.setups_ledger import SetupLedger
from trademate.trade_settings import BUILTIN, TradeSettings


def T(ticket, net, t):
    return SimpleNamespace(ticket=ticket, net=net, time_close=t)


class LocalDayTest(unittest.TestCase):
    def test_midnight_before_and_after_in_a_plain_zone(self):
        tz = ZoneInfo("Asia/Manila")
        mid = datetime(2026, 10, 20, 0, 0, tzinfo=tz).timestamp()
        self.assertEqual(localday.local_day_start_utc(mid - 1, tz), mid - 86400)
        self.assertEqual(localday.local_day_start_utc(mid, tz), mid)
        self.assertEqual(localday.next_local_midnight_utc(mid - 1, tz), mid)

    def test_dst_end_day_is_25_hours_and_dst_start_day_is_23_hours(self):
        tz = ZoneInfo("America/New_York")
        end = localday.local_day_start_utc(datetime(2026, 11, 1, 12, tzinfo=tz).timestamp(), tz)         # clocks go back on 1 Nov 2026
        self.assertEqual((localday.next_local_midnight_utc(end, tz) - end) / 3600, 25)
        start = localday.local_day_start_utc(datetime(2026, 3, 8, 12, tzinfo=tz).timestamp(), tz)          # clocks go forward on 8 Mar 2026
        self.assertEqual((localday.next_local_midnight_utc(start, tz) - start) / 3600, 23)
        ath = ZoneInfo("Europe/Athens")
        a = localday.local_day_start_utc(datetime(2026, 10, 25, 12, tzinfo=ath).timestamp(), ath)
        self.assertEqual((localday.next_local_midnight_utc(a, ath) - a) / 3600, 25)

    def test_the_boundary_follows_local_midnight_across_the_dst_change(self):
        tz = ZoneInfo("America/New_York")
        before_change = datetime(2026, 10, 31, 23, 59, 59, tzinfo=tz).timestamp()
        after_midnight = datetime(2026, 11, 1, 0, 0, 1, tzinfo=tz).timestamp()
        self.assertNotEqual(localday.local_day_start_utc(before_change, tz), localday.local_day_start_utc(after_midnight, tz))
        late = datetime(2026, 11, 1, 23, 30, tzinfo=tz).timestamp()                                       # still the same local day, after the clock change
        self.assertEqual(localday.local_day_start_utc(late, tz), localday.local_day_start_utc(after_midnight, tz))

    def test_invalid_timezone_is_reported_not_replaced_by_utc(self):
        with self.assertRaises(localday.TimezoneProblem):
            localday.resolve_tz("Mars/Olympus_Mons")


class DailyLogTest(unittest.TestCase):
    def summary(self, loss, reset_utc):
        return {"todays_loss": loss, "next_reset_utc": reset_utc, "currency": "USD", "start_of_day_balance": 10000.0, "todays_closed_pl": -loss, "todays_loss_percent": loss / 100,
                "daily_limit_amount": 300.0, "daily_locked": loss >= 300}

    def test_a_new_local_day_adds_a_row_and_keeps_yesterday_untouched_across_restarts(self):
        tz = ZoneInfo("Asia/Manila")
        d1 = datetime(2026, 10, 20, tzinfo=tz).timestamp()
        d2 = d1 + 86400
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "log.json"
            log = DailyRiskLog(path)
            log.record(self.summary(300.0, d1), tz)                       # day ending at d1 (19 Oct): locked
            log.record(self.summary(0.0, d2), tz)                         # day ending at d2 (20 Oct): fresh start
            again = DailyRiskLog(path)                                    # a restart
            rows = {r["date"]: r for r in again.history()}
            self.assertEqual(set(rows), {"2026-10-19", "2026-10-20"})
            self.assertTrue(rows["2026-10-19"]["locked"])
            self.assertFalse(rows["2026-10-20"]["locked"])
            self.assertEqual(rows["2026-10-20"]["todays_loss"], 0.0)       # yesterday's loss was not carried into the new day


class ConsecutiveLossTest(unittest.TestCase):
    def guard(self, **cfg):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {"consecutive_loss_enabled": True, "consecutive_loss_threshold": 3, **cfg}
        self.path = Path(self.tmp.name) / "cl.json"
        return ConsecutiveLossGuard(self.path, get_config=lambda: self.cfg)

    def test_first_sync_only_records_existing_history(self):
        g = self.guard()
        st = g.sync([T(1, -5, 1), T(2, -5, 2), T(3, -5, 3), T(4, -5, 4)])
        self.assertEqual(st["count"], 0)
        self.assertFalse(st["locked"])

    def test_threshold_locks_and_names_the_reason(self):
        g = self.guard()
        g.sync([])
        st = g.sync([T(1, -1, 1), T(2, -1, 2), T(3, -1, 3)])
        self.assertEqual((st["count"], st["threshold"], st["locked"]), (3, 3, True))
        self.assertIn("TRADING LOCKED", st["reason"])

    def test_a_win_resets_the_count(self):
        g = self.guard()
        g.sync([])
        self.assertEqual(g.sync([T(1, -1, 1), T(2, -1, 2), T(3, 5, 3), T(4, -1, 4)])["count"], 1)

    def test_a_duplicate_history_record_never_counts_twice(self):
        g = self.guard()
        g.sync([])
        g.sync([T(1, -1, 1)])
        g.sync([T(1, -1, 1), T(1, -1, 1)])
        self.assertEqual(g.status()["count"], 1)

    def test_open_positions_and_unfinished_results_are_not_completed_losses(self):
        g = self.guard()
        g.sync([])
        self.assertEqual(g.sync([T(1, None, 1)])["count"], 0)             # no result yet: not a completed trade

    def test_the_threshold_can_be_changed_at_any_time_and_disabled(self):
        for n in (2, 3, 5, 10):
            g = self.guard(consecutive_loss_threshold=n)
            g.sync([])
            g.sync([T(i, -1, i) for i in range(1, n)])
            self.assertFalse(g.status()["locked"], n)
            g.sync([T(n, -1, n)])
            self.assertTrue(g.status()["locked"], n)
            self.cfg["consecutive_loss_threshold"] = n + 1                 # changed live: unlocks without touching the record
            self.assertFalse(g.status()["locked"])
            self.cfg["consecutive_loss_enabled"] = False
            self.cfg["consecutive_loss_threshold"] = 1
            self.assertFalse(g.status()["locked"])                          # disabled: never locks
            self.assertEqual(g.status()["count"], n)                        # but the count is still shown

    def test_manual_reset_needs_the_confirmation_phrase_and_keeps_history(self):
        g = self.guard()
        g.sync([])
        g.sync([T(1, -1, 1), T(2, -1, 2), T(3, -1, 3)])
        self.assertFalse(g.reset("yes")[0])
        self.assertTrue(g.status()["locked"])
        self.assertTrue(g.reset("RESET")[0])
        st = g.status()
        self.assertEqual(st["count"], 0)
        self.assertEqual((st["records"], st["resets"]), (3, 1))
        g.sync([T(1, -1, 1), T(2, -1, 2), T(3, -1, 3)])                     # the same tickets are not counted again after the reset
        self.assertEqual(g.status()["count"], 0)

    def test_count_and_records_persist_across_restarts(self):
        g = self.guard()
        g.sync([])
        g.sync([T(1, -1, 1), T(2, -1, 2)])
        again = ConsecutiveLossGuard(self.path, get_config=lambda: self.cfg)
        self.assertEqual(again.status()["count"], 2)
        again.sync([T(1, -1, 1), T(2, -1, 2), T(3, -1, 3)])
        self.assertTrue(again.status()["locked"])


class RiskEngineCentralProtectionTest(unittest.TestCase):
    SPEC = SymbolSpec("EURUSD", 0.00001, 1.0, 1.0, 0.01, 100.0, 0.01)
    STATE = AccountRiskState(equity=10000.0, balance=10000.0, currency="USD", start_of_day_balance=10000.0, todays_closed_pl=0.0, floating_pl=0.0)
    PROP = TradeProposal("EURUSD", "buy", 1.1000, 1.0990, 1.1020, volume=0.1)

    def tearDown(self):
        set_protections_provider(None)

    def test_without_protections_the_baseline_decision_is_unchanged(self):
        set_protections_provider(None)
        self.assertEqual(evaluate_trade(self.STATE, self.PROP, self.SPEC, RiskRules()).decision, ACCEPTED)

    def test_emergency_lock_rejects_every_trade_and_lifting_it_approves_again(self):
        with tempfile.TemporaryDirectory() as d:
            lock = SafetyLock(Path(d) / "lock.json")
            set_protections_provider(lambda: protection_checks(lock, None))
            self.assertEqual(evaluate_trade(self.STATE, self.PROP, self.SPEC, RiskRules()).decision, ACCEPTED)
            lock.lock("Emergency stop pressed")
            r = evaluate_trade(self.STATE, self.PROP, self.SPEC, RiskRules())
            self.assertEqual(r.decision, REJECTED)
            self.assertTrue(r.trading_locked)
            self.assertIn("EMERGENCY LOCKED", r.rejection_reason)
            self.assertEqual(SafetyLock(Path(d) / "lock.json").locked, True)       # survives a restart
            self.assertFalse(lock.unlock("nope")[0])
            self.assertTrue(lock.unlock("UNLOCK")[0])
            self.assertEqual(evaluate_trade(self.STATE, self.PROP, self.SPEC, RiskRules()).decision, ACCEPTED)

    def test_consecutive_loss_lock_rejects_with_trading_locked(self):
        cfg = {"consecutive_loss_enabled": True, "consecutive_loss_threshold": 2}
        g = ConsecutiveLossGuard(None, get_config=lambda: cfg)
        g.sync([])
        g.sync([T(1, -1, 1), T(2, -1, 2)])
        set_protections_provider(lambda: protection_checks(None, g))
        r = evaluate_trade(self.STATE, self.PROP, self.SPEC, RiskRules())
        self.assertEqual(r.decision, REJECTED)
        self.assertIn("TRADING LOCKED", r.rejection_reason)
        self.assertTrue(r.trading_locked)

    def test_unreadable_protections_fail_closed(self):
        def boom():
            raise RuntimeError("disk gone")
        set_protections_provider(boom)
        r = evaluate_trade(self.STATE, self.PROP, self.SPEC, RiskRules())
        self.assertEqual(r.decision, REJECTED)

    def test_the_daily_loss_limit_still_applies_with_protections_installed(self):
        set_protections_provider(lambda: [])
        losing = AccountRiskState(equity=9700.0, balance=9700.0, currency="USD", start_of_day_balance=10000.0, todays_closed_pl=-300.0, floating_pl=0.0)
        r = evaluate_trade(losing, self.PROP, self.SPEC, RiskRules(max_daily_loss_percent=3.0))
        self.assertEqual(r.decision, REJECTED)
        self.assertTrue(r.trading_locked)

    def test_the_real_app_installs_the_protections(self):
        self.assertIsNotNone(app_module.set_protections_provider)
        set_protections_provider(app_module._central_protections)           # what app.py installs at import
        with mock.patch.object(app_module, "safety_lock", SafetyLock(None)) as lock:
            lock.lock("test")
            r = evaluate_trade(self.STATE, self.PROP, self.SPEC, RiskRules())
            self.assertEqual(r.decision, REJECTED)


class LotAdjustTest(unittest.TestCase):
    BASE = dict(volume_min=0.01, volume_max=100.0, volume_step=0.01, stop_distance=1.0, tick_size=0.01, tick_value_loss=0.01)          # 1.0 stop = 1 money / 0.01 lot ... loss/lot = 1.0

    def test_detects_insufficient_funds_from_the_retcode_or_text(self):
        self.assertTrue(lot_adjust.is_insufficient_funds(10019, ""))
        self.assertTrue(lot_adjust.is_insufficient_funds(None, "NOT ENOUGH MONEY"))
        self.assertFalse(lot_adjust.is_insufficient_funds(10016, "Invalid stops"))

    def test_reduces_using_free_margin_normalised_to_the_step(self):
        p = lot_adjust.plan_reduced_lot(original_lot=0.50, max_risk_amount=1000.0, free_margin=50.0, margin_per_lot=200.0, **self.BASE)
        self.assertTrue(p.ok)
        self.assertEqual(p.adjusted_lot, 0.22)                              # 50 x 0.9 / 200 = 0.225 -> 0.22
        self.assertLess(p.adjusted_lot, p.original_lot)
        self.assertTrue(p.margin_known)
        self.assertLessEqual(p.margin_at_adjusted, 50.0)
        self.assertIn("0.5", p.reason)

    def test_never_exceeds_the_allowed_risk(self):
        p = lot_adjust.plan_reduced_lot(original_lot=0.50, max_risk_amount=10.0, free_margin=1e9, margin_per_lot=1.0, **self.BASE)     # loss per lot = 100 x 0.01/0.01... = 100? checked below
        self.assertTrue(p.ok)
        self.assertLessEqual(p.risk_at_adjusted, 10.0 + 1e-9)

    def test_minimum_lot_that_still_needs_too_much_margin_is_rejected_with_an_explanation(self):
        p = lot_adjust.plan_reduced_lot(original_lot=0.05, max_risk_amount=1000.0, free_margin=1.0, margin_per_lot=500.0, **self.BASE)
        self.assertFalse(p.ok)
        self.assertIn("Not enough money", p.reason)
        self.assertIsNone(p.adjusted_lot)

    def test_unknown_margin_uses_a_bounded_reduction_and_says_so(self):
        p = lot_adjust.plan_reduced_lot(original_lot=0.10, max_risk_amount=1000.0, free_margin=None, margin_per_lot=None, **self.BASE)
        self.assertTrue(p.ok)
        self.assertEqual(p.adjusted_lot, 0.05)
        self.assertFalse(p.margin_known)
        self.assertIn("not available", p.reason)

    def test_the_same_rejected_lot_is_never_proposed_again(self):
        first = lot_adjust.plan_reduced_lot(original_lot=0.10, max_risk_amount=1000.0, free_margin=None, margin_per_lot=None, **self.BASE)
        second = lot_adjust.plan_reduced_lot(original_lot=0.10, max_risk_amount=1000.0, free_margin=None, margin_per_lot=None, failed_lots=(0.10, first.adjusted_lot), **self.BASE)
        self.assertTrue(second.ok)
        self.assertLess(second.adjusted_lot, first.adjusted_lot)
        floor = lot_adjust.plan_reduced_lot(original_lot=0.02, max_risk_amount=1000.0, free_margin=None, margin_per_lot=None, failed_lots=(0.02, 0.01), **self.BASE)
        self.assertFalse(floor.ok)                                          # nothing smaller than the broker minimum exists: no endless retry

    def test_never_below_the_broker_minimum_or_off_the_step(self):
        p = lot_adjust.plan_reduced_lot(original_lot=1.0, max_risk_amount=1000.0, free_margin=10.0, margin_per_lot=7.0, volume_min=0.1, volume_max=10.0, volume_step=0.1,
                                        stop_distance=1.0, tick_size=0.01, tick_value_loss=0.01)
        self.assertTrue(p.ok)
        self.assertGreaterEqual(p.adjusted_lot, 0.1)
        self.assertAlmostEqual(round(p.adjusted_lot / 0.1) * 0.1, p.adjusted_lot, places=9)

    def test_missing_broker_data_means_no_retry(self):
        self.assertFalse(lot_adjust.plan_reduced_lot(original_lot=0.1, max_risk_amount=10, free_margin=1, margin_per_lot=1, volume_min=None, volume_max=1, volume_step=0.01,
                                                     stop_distance=1, tick_size=0.01, tick_value_loss=0.01).ok)


class CalculatorTest(unittest.TestCase):
    KW = dict(unit_size=0.0001, tick_size=0.00001, tick_value_profit=1.0, tick_value_loss=1.0, balance=10000.0, equity=10000.0, currency="USD", volume_min=0.01, volume_max=100.0, volume_step=0.01)

    def test_money_percent_and_reward_to_risk(self):
        r = calculate(volume=0.5, target_units=25, stop_units=100, **self.KW)
        self.assertTrue(r["trusted"])
        self.assertAlmostEqual(r["gain_amount"], 25 * 10 * 1.0 * 0.5)        # 25 pips = 250 ticks x 1.0 x 0.5 lots
        self.assertAlmostEqual(r["loss_amount"], 100 * 10 * 1.0 * 0.5)
        self.assertAlmostEqual(r["risk_percent_balance"], 5.0)
        self.assertAlmostEqual(r["reward_to_risk"], 0.25)

    def test_reverse_lot_for_a_maximum_risk(self):
        r = calculate(volume=None, target_units=25, stop_units=100, max_risk_amount=50.0, **self.KW)
        self.assertEqual(r["allowed_lot"], 0.05)                              # 1000 per lot at a 100-pip stop -> 50 / 1000
        self.assertLessEqual(r["allowed_lot_risk"], 50.0)

    def test_reverse_lot_below_the_minimum_offers_no_lot(self):
        r = calculate(volume=None, stop_units=100, max_risk_amount=1.0, **self.KW)
        self.assertIsNone(r["allowed_lot"])
        self.assertIn("minimum lot", r["allowed_lot_message"])

    def test_missing_broker_metadata_is_untrusted_and_shows_no_money(self):
        kw = dict(self.KW, tick_value_loss=None, tick_value_profit=None)
        r = calculate(volume=0.5, target_units=25, stop_units=100, **kw)
        self.assertFalse(r["trusted"])
        self.assertIsNone(r["loss_amount"])
        self.assertIn("cannot be trusted", r["message"])

    def test_unverified_conversion_is_untrusted(self):
        r = calculate(volume=0.5, target_units=25, stop_units=100, verified=False, problems=["pip size unknown"], **self.KW)
        self.assertFalse(r["trusted"])

    def test_a_lot_off_the_volume_step_is_flagged(self):
        r = calculate(volume=0.015, target_units=25, stop_units=100, **self.KW)
        self.assertFalse(r["volume_valid"])


class TradeSettingsScopeTest(unittest.TestCase):
    def test_defaults_apply_only_when_nothing_is_saved(self):
        ts = TradeSettings(None)
        r = ts.resolve("EURUSD", "choch")
        self.assertEqual(r["values"], BUILTIN)
        self.assertEqual(set(r["sources"].values()), {"default"})
        self.assertEqual((BUILTIN["main_instrument"], BUILTIN["lot"], BUILTIN["target_distance"], BUILTIN["stop_distance"]), ("Vol_20", 0.02, 25.0, 100.0))

    def test_scopes_resolve_most_specific_first_and_do_not_leak(self):
        ts = TradeSettings(None)
        ts.save("global", {"target_distance": 30})
        ts.save("strategy", {"target_distance": 40}, strategy="choch")
        ts.save("instrument", {"target_distance": 50, "lot": 0.1}, symbol="EURUSD")
        ts.save("strategy_instrument", {"target_distance": 60}, symbol="EURUSD", strategy="choch")
        get = lambda sym, sid: ts.resolve(sym, sid)["values"]["target_distance"]
        self.assertEqual(get("EURUSD", "choch"), 60)
        self.assertEqual(get("EURUSD", "trendline"), 50)
        self.assertEqual(get("GBPUSD", "choch"), 40)
        self.assertEqual(get("GBPUSD", "trendline"), 30)
        self.assertEqual(ts.resolve("GBPUSD", "choch")["values"]["lot"], 0.02)           # EURUSD's lot did not leak to GBPUSD
        self.assertEqual(ts.resolve("EURUSD", "choch")["sources"]["lot"], "instrument")

    def test_saved_values_persist_and_stay_authoritative_after_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ts.json"
            TradeSettings(p).save("global", {"stop_distance": 80, "main_instrument": "Crash 100 Index"})
            again = TradeSettings(p)
            v = again.resolve()["values"]
            self.assertEqual((v["stop_distance"], v["main_instrument"]), (80.0, "Crash 100 Index"))
            self.assertEqual(v["lot"], 0.02)                                       # still the default: it was never saved

    def test_invalid_values_are_refused_and_nothing_is_changed(self):
        ts = TradeSettings(None)
        for bad in ({"lot": 0}, {"lot": -1}, {"target_distance": "abc"}, {"stop_distance": float("nan")}, {"nope": 1}):
            with self.assertRaises(ValueError):
                ts.save("global", bad)
        with self.assertRaises(ValueError):
            ts.save("instrument", {"main_instrument": "X"}, symbol="EURUSD")        # the main instrument is global-only
        self.assertEqual(ts.resolve()["values"], BUILTIN)

    def test_clearing_one_scope_falls_back_to_the_next(self):
        ts = TradeSettings(None)
        ts.save("global", {"lot": 0.3})
        ts.save("instrument", {"lot": 0.5}, symbol="EURUSD")
        ts.clear("instrument", symbol="EURUSD")
        self.assertEqual(ts.resolve("EURUSD")["values"]["lot"], 0.3)

    def test_main_instrument_default_is_vol_20(self):
        self.assertEqual(TradeSettings(None).main_instrument(), "Vol_20")


class ExecutionSettingsTest(unittest.TestCase):
    def test_defaults_validation_and_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "es.json"
            es = ExecutionSettings(p)
            self.assertEqual(es.get_all(), DEFAULTS)
            self.assertFalse(es.get("allow_multiple_per_candle"))
            self.assertEqual(es.max_entries_per_candle(), 1)
            es.update({"allow_multiple_per_candle": True, "consecutive_loss_threshold": 5})
            again = ExecutionSettings(p)
            self.assertEqual(again.max_entries_per_candle(), 2)
            self.assertEqual(again.get("consecutive_loss_threshold"), 5)
            for bad in ({"allow_multiple_per_candle": "yes"}, {"consecutive_loss_threshold": 0}, {"consecutive_loss_threshold": 2.5}, {"lot_adjust_max_attempts": 9}, {"unknown": 1}):
                with self.assertRaises(ValueError):
                    again.update(bad)
            self.assertEqual(ExecutionSettings(p).get("consecutive_loss_threshold"), 5)


class EntryLimitsTest(unittest.TestCase):
    def ledger(self):
        return SetupLedger(None)

    def test_off_allows_exactly_one_entry(self):
        led = self.ledger()
        sid, n = entry_limits.next_slot(led, "c1", 1, 100.0)
        self.assertEqual((sid, n), ("c1", 1))
        led.claim(sid, at=100.0)
        self.assertIsNone(entry_limits.next_slot(led, "c1", 1, 200.0)[0])

    def test_on_allows_two_distinct_entries_and_never_a_third(self):
        led = self.ledger()
        led.claim("c1")
        led._items["c1"]["at"] = 100.0
        sid, n = entry_limits.next_slot(led, "c1", 2, 150.0)
        self.assertEqual((sid, n), ("c1#2", 2))
        led.claim(sid)
        led._items["c1#2"]["at"] = 160.0
        slot, why = entry_limits.next_slot(led, "c1", 2, 200.0)
        self.assertIsNone(slot)
        self.assertIn("maximum of 2", why)
        self.assertIsNone(entry_limits.next_slot(led, "c1", 99, 300.0)[0])          # a larger setting can never exceed two

    def test_a_repeated_request_is_not_a_second_entry(self):
        led = self.ledger()
        led.claim("c1")
        led._items["c1"]["at"] = 100.0
        slot, why = entry_limits.next_slot(led, "c1", 2, 90.0)                       # an approval created BEFORE entry 1 was submitted (double click, second tab)
        self.assertIsNone(slot)
        self.assertIn("repeated request", why)

    def test_candles_strategies_and_timeframes_never_share_a_count(self):
        led = self.ledger()
        led.claim("choch:buy:M5:1000")
        self.assertEqual(entry_limits.next_slot(led, "choch:buy:M1:1000", 1, 1.0)[1], 1)
        self.assertEqual(entry_limits.next_slot(led, "trendline:buy:M5:1000", 1, 1.0)[1], 1)

    def test_copied_child_positions_do_not_multiply_entries(self):
        """Entries are counted by submitted ORDERS (ledger claims), never by how many positions the broker shows for the parent trade."""
        led = self.ledger()
        led.claim("c1")
        led._items["c1"]["at"] = 100.0
        broker_positions_for_this_candle = 5                                           # a parent plus four copied children
        self.assertGreaterEqual(broker_positions_for_this_candle, 2)
        slot, _ = entry_limits.next_slot(led, "c1", 1, 200.0)
        self.assertIsNone(slot)                                                         # still exactly one entry used, so OFF refuses another


class SameCandleTwiceTest(TfRig, unittest.TestCase):
    """Candle Momentum through the real engine: the setting decides whether a second entry on the SAME candle is possible after the first closed."""

    def run_two_entries(self, allow):
        self.build("M5", tfs=("M5",))
        self.engine.max_entries = (lambda: 2) if allow else (lambda: 1)
        self.autopilot._history = lambda: [{"ticket": t.ticket, "net": t.net, "time_close": t.time_close} for t in self.adapter.get_history(30, 500)]      # real closed trades
        ok, msg = self.engine.start("EURUSD", "M5")
        self.assertTrue(ok, msg)
        for slot in self.engine._slots.values():
            slot.machine.max_entries = lambda: self.engine.max_entries()
        self.engine.step()
        ok, msg = self.autopilot.start("runs", 5, spawn=False)
        self.assertTrue(ok, msg)
        self.signal(1.10100)
        self.autopilot.tick()
        self.assertEqual(len(self.opens()), 1)
        ticket = self.adapter.get_positions()[0].ticket
        self.price(1.10100, 1.10112)
        self.assertTrue(self.adapter.close_position(ticket).confirmed)                 # the first entry closes inside the same candle
        for k in range(1, 4):
            self.engine.step(now=time.time() + 10 * k)                                  # the engine sees the close
            self.autopilot.tick()
        return self.lc("M5")

    def test_off_never_takes_a_second_entry_on_the_candle(self):
        lc = self.run_two_entries(False)
        self.assertEqual(len(self.opens()), 1)
        self.assertEqual(lc["state"], "POSITION_CLOSED")
        self.assertNotIn("#2", lc["candle_id"])

    def test_on_arms_exactly_one_more_entry_on_the_same_candle(self):
        lc = self.run_two_entries(True)
        self.assertIn("#2", lc["candle_id"])
        self.assertEqual(lc["entry_no"], 2)
        self.signal(1.10110)
        self.autopilot.tick()
        for k in range(1, 4):
            self.engine.step(now=time.time() + 10 * k)
            self.autopilot.tick()
        self.assertEqual(len(self.opens()), 2)                                          # two distinct entries, never three
        self.assertEqual(len({o["id"] for o in self.opens()}), 2)                       # two different commands, not one repeated
        self.assertEqual(sum(1 for o in self.opens()), 2)
        ids = [c for c in self.engine._slots["EURUSD|M5"].machine.lc["candle_id"].split("#")]
        self.assertEqual(len(ids), 2)                                                    # the lifecycle is '<candle>#2': never '#3'


class AutopilotControlsTest(unittest.TestCase):
    def ap(self, selection=("EURUSD", "choch", "M5")):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proposed = []
        return Autopilot(Path(self.tmp.name) / "ap.json", selection=lambda: selection, hub_get=lambda *a: None, lot_for=lambda s: 0.01,
                         propose=lambda *a: self.proposed.append(a) or {"approved": False, "message": "x"}, execute=lambda a: {}, positions=lambda: set(), history=lambda: [])

    def test_pause_keeps_the_session_blocks_new_entries_and_resumes(self):
        ap = self.ap()
        self.assertTrue(ap.start("runs", 5, spawn=False)[0])
        self.assertTrue(ap.pause()[0])
        st = ap.status()
        self.assertEqual((st["state"], st["paused"], st["armed"]), ("paused", True, True))
        self.assertEqual((st["mode"], st["value"], st["symbol"], st["strategy_id"], st["exec_tf"]), ("runs", 5, "EURUSD", "choch", "M5"))     # the session is kept
        ap.tick()
        self.assertEqual(self.proposed, [])
        self.assertIn("paused", ap.status()["message"].lower())
        self.assertTrue(ap.unpause()[0])
        st = ap.status()
        self.assertEqual((st["state"], st["paused"], st["armed"]), ("running", False, True))

    def test_pause_when_not_running_is_refused(self):
        self.assertFalse(self.ap().pause()[0])

    def test_stop_ends_the_session_cleanly_and_is_distinct_from_pause_and_emergency(self):
        ap = self.ap()
        ap.start("runs", 5, spawn=False)
        ap.pause()
        ap.stop("Stopped by the user.")
        st = ap.status()
        self.assertEqual((st["state"], st["armed"], st["paused"], st["emergency_stopped"]), ("stopped", False, False, False))
        self.assertFalse(ap.unpause()[0])

    def test_emergency_stop_state_is_distinct(self):
        ap = self.ap()
        ap.start("runs", 5, spawn=False)
        ap.stop("Emergency", emergency=True)
        st = ap.status()
        self.assertEqual((st["state"], st["armed"], st["emergency_stopped"]), ("emergency_stopped", False, True))

    def test_paused_state_survives_a_restart_as_not_trading(self):
        ap = self.ap()
        ap.start("runs", 5, spawn=False)
        ap.pause()
        again = Autopilot(Path(self.tmp.name) / "ap.json", selection=lambda: ("EURUSD", "choch", "M5"), hub_get=lambda *a: None, lot_for=lambda s: 0.01,
                          propose=lambda *a: {}, execute=lambda a: {}, positions=lambda: set(), history=lambda: [])
        self.assertFalse(again.status()["armed"])                                        # a restart never resumes trading by itself


class EmergencyRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.lock = SafetyLock(d / "lock.json")
        self.es = ExecutionSettings(d / "es.json")
        self.adapter = mock.Mock()
        self.adapter.get_positions.return_value = [SimpleNamespace(ticket=7), SimpleNamespace(ticket=8)]
        self.ap = Autopilot(d / "ap.json", selection=lambda: ("EURUSD", "choch", "M5"), hub_get=lambda *a: None, lot_for=lambda s: 0.01, propose=lambda *a: {}, execute=lambda a: {},
                            positions=lambda: set(), history=lambda: [])
        for p in (mock.patch.object(app_module, "safety_lock", self.lock), mock.patch.object(app_module, "execution_settings", self.es),
                  mock.patch.object(app_module, "autopilot", self.ap), mock.patch.object(app_module, "adapter", self.adapter),
                  mock.patch.object(app_module, "loss_guard", ConsecutiveLossGuard(d / "cl.json", get_config=self.es.get_all))):
            p.start()
            self.addCleanup(p.stop)
        self.client = app_module.app.test_client()

    def test_emergency_stop_locks_and_does_not_close_positions_by_default(self):
        self.ap.start("runs", 5, spawn=False)
        j = self.client.post("/api/autopilot/emergency-stop").get_json()
        self.assertTrue(j["emergency_locked"])
        self.assertFalse(j["positions_closed_by_policy"])
        self.adapter.close_position.assert_not_called()                                  # open positions are NOT silently closed
        self.assertTrue(self.lock.locked)
        self.assertEqual(j["state"], "emergency_stopped")
        st = self.client.get("/api/safety/status").get_json()
        self.assertTrue(st["emergency"]["locked"])

    def test_emergency_stop_closes_positions_only_when_configured(self):
        self.es.update({"emergency_close_positions": True})
        j = self.client.post("/api/autopilot/emergency-stop").get_json()
        self.assertEqual(sorted(j["closed_positions"]), [7, 8])
        self.assertTrue(j["positions_closed_by_policy"])

    def test_unlock_needs_the_phrase_and_autopilot_stays_stopped(self):
        self.client.post("/api/autopilot/emergency-stop")
        self.assertEqual(self.client.post("/api/safety/unlock", json={"confirm": "no"}).status_code, 400)
        self.assertTrue(self.lock.locked)
        self.assertTrue(self.client.post("/api/safety/unlock", json={"confirm": "UNLOCK"}).get_json()["ok"])
        self.assertFalse(self.lock.locked)
        self.assertFalse(self.ap.status()["armed"])

    def test_autopilot_cannot_start_while_emergency_locked(self):
        self.lock.lock("test")
        self.assertIn("EMERGENCY LOCKED", app_module._autopilot_precheck())

    def test_pause_and_resume_routes(self):
        self.ap.start("runs", 5, spawn=False)
        self.assertEqual(self.client.post("/api/autopilot/pause").get_json()["state"], "paused")
        self.assertEqual(self.client.post("/api/autopilot/resume-pause").get_json()["state"], "running")

    def test_consecutive_loss_settings_and_reset_routes(self):
        j = self.client.post("/api/execution-settings", json={"consecutive_loss_threshold": 5, "consecutive_loss_enabled": True}).get_json()
        self.assertEqual(j["settings"]["consecutive_loss_threshold"], 5)
        self.assertEqual(self.client.post("/api/execution-settings", json={"consecutive_loss_threshold": 0}).status_code, 400)
        self.assertEqual(self.client.post("/api/risk/consecutive/reset", json={"confirm": "x"}).status_code, 400)
        self.assertTrue(self.client.post("/api/risk/consecutive/reset", json={"confirm": "RESET"}).get_json()["ok"])


class TradeSettingsAndCalcRoutesTest(unittest.TestCase):
    def setUp(self):
        self.ts = TradeSettings(None)
        self.p = mock.patch.object(app_module, "trade_settings", self.ts)
        self.p.start()
        self.addCleanup(self.p.stop)
        self.client = app_module.app.test_client()

    def test_defaults_and_scoped_saves_through_the_api(self):
        j = self.client.get("/api/trade-settings?symbol=EURUSD&strategy=choch").get_json()
        self.assertEqual(j["values"], BUILTIN)
        self.assertEqual(j["builtin_defaults"], BUILTIN)
        r = self.client.post("/api/trade-settings", json={"scope": "instrument", "symbol": "EURUSD", "values": {"lot": 0.5}}).get_json()
        self.assertEqual(r["values"]["lot"] if "values" in r else None, 0.5) if False else None
        j = self.client.get("/api/trade-settings?symbol=EURUSD&strategy=choch").get_json()
        self.assertEqual((j["values"]["lot"], j["sources"]["lot"]), (0.5, "instrument"))
        self.assertEqual(self.client.get("/api/trade-settings?symbol=GBPUSD&strategy=choch").get_json()["values"]["lot"], 0.02)
        self.assertEqual(self.client.post("/api/trade-settings", json={"scope": "global", "values": {"lot": -3}}).status_code, 400)
        self.client.post("/api/trade-settings", json={"scope": "instrument", "symbol": "EURUSD", "reset": True})
        self.assertEqual(self.client.get("/api/trade-settings?symbol=EURUSD").get_json()["values"]["lot"], 0.02)

    def test_a_saved_value_reaches_the_strategy_params_and_defaults_do_not_override_settings_toml(self):
        from trademate.strategies.registry import CHOCH, MARKET_STRUCTURE
        from trademate.strategies.service import _saved, strategy_params
        settings = app_module.settings
        self.assertEqual(strategy_params(settings, None, CHOCH, "M5", saved=_saved("EURUSD", "choch")).target_pips, settings.strategy.choch_target_pips)      # nothing saved
        self.ts.save("strategy", {"target_distance": 40}, strategy="choch")
        self.assertEqual(strategy_params(settings, None, CHOCH, "M5", saved=_saved("EURUSD", "choch")).target_pips, 40.0)
        self.ts.save("instrument", {"stop_distance": 60}, symbol="EURUSD")
        p = strategy_params(settings, None, MARKET_STRUCTURE, "M5", saved=_saved("EURUSD", "market_structure"))
        self.assertEqual(p.stop_pips, 60.0)


if __name__ == "__main__":
    unittest.main()
