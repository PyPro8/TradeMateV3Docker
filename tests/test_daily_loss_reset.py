"""Regression tests: Today's Loss / Remaining Daily Loss must reset at the BROKER's day boundary.

ROOT CAUSE (see docs/DAILY_LOSS_AND_CLOCKS.md): the trading day was taken from the broker's clock only when a fresh 1-minute
candle could be read; otherwise it silently fell back to THIS COMPUTER's local day. With the computer and the broker in different
timezones that boundary is hours away from the broker's midnight, so yesterday's late losses kept counting after the broker's
new day began (and the boundary could flip between refreshes). The fix: measure the broker clock (cached, cross-checked), optionally
use `[risk] broker_timezone`, and otherwise report the daily loss as UNKNOWN. It never uses the computer's timezone.

SAFE: every adapter here is a read-only stub. Any attempt to place, modify or close an order fails the test. No MT5, no broker,
no account is touched.
Run:  python -m unittest tests.test_daily_loss_reset -v
"""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module
from app import app
from tests.fake_bridge import FakeBridge
from trademate import brokertime
from trademate.broker import create_adapter
from trademate.broker.base import AccountInfo, Candle, ClosedTrade, Position
from trademate.brokertime import (CACHE_MAX_AGE_SECONDS, ServerClockResolver, day_start_from_offset, next_reset_utc, offset_from_candle,
                                  valid_timezone, zone_offset_seconds)
from trademate.config import AppSettings, ConfigError, Mt5Settings, Settings, TradingSettings, load_config
from trademate.errors import BrokerUnavailableError
from trademate.risk import RiskRules, RiskRulesStore, TradeProposal, evaluate_trade, summarize_state
from trademate.risk import scenarios as sc
from trademate.risk.account_state import HISTORY_LIMIT, build_state, gather_state, resolve_server_clock
from trademate.execution_gate import portfolio_breaches

R3 = RiskRules(1.0, 3.0, 3.0, 3)          # 3 % daily loss limit
H = 3600


import contextlib


@contextlib.contextmanager
def local_tz(name):
    """The computer's configured local timezone for the daily-loss day ([ui] timezone)."""
    from trademate.risk import account_state
    account_state.set_local_timezone(name)
    try:
        yield
    finally:
        account_state.set_local_timezone("")


def utc(y, mo, d, h=0, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


def account(balance=10000.0, equity=None):
    return AccountInfo(1, "Headway-Demo", "b", "demo", "USD", 100, balance, balance if equity is None else equity, 0.0, balance, None, 0.0)


def trade(net, time_close, ticket=1, **kw):
    fields = dict(ticket=ticket, symbol="EURUSD", direction="buy", volume=0.1, price_open=1.1, price_close=1.09, stop_loss=None,
                  take_profit=None, profit=net, swap=0.0, commission=0.0, net=net, time_open=None if time_close is None else time_close - 60, time_close=time_close,
                  reason="client")
    fields.update(kw)
    return ClosedTrade(**fields)


class ReadOnlyStub:
    """A broker adapter that can only READ. Anything that could trade fails the test immediately."""

    READS = {"get_positions", "get_account", "get_symbol_spec", "get_symbols", "get_candles", "get_history"}

    def __init__(self, now_utc, *, server_offset=3 * H, balance=10000.0, equity=None, positions=(), history=(), history_error=None,
                 symbols=("EURUSD", "GBPUSD", "XAUUSD"), candle_age=10, candle_override=None):
        self.now, self.server_offset = now_utc, server_offset
        self.balance, self.equity, self.positions_, self.history_ = balance, equity, list(positions), list(history)
        self.history_error, self.symbols_, self.candle_age, self.candle_override = history_error, symbols, candle_age, candle_override
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_") or name in self.READS:
            raise AttributeError(name)

        def refuse(*a, **k):
            raise AssertionError(f"the daily-loss code called {name}(): it must never trade or write")
        return refuse

    def get_positions(self):
        self.calls.append("get_positions")
        return list(self.positions_)

    def get_account(self):
        self.calls.append("get_account")
        return account(self.balance, self.equity)

    def get_symbol_spec(self, name):
        self.calls.append("get_symbol_spec")
        raise BrokerUnavailableError("no tick data in this stub")

    def get_symbols(self):
        self.calls.append("get_symbols")
        return [SimpleNamespace(name=n) for n in self.symbols_]

    def get_candles(self, name, tf, count=300):
        self.calls.append("get_candles")
        if self.server_offset is None:
            raise BrokerUnavailableError("MT5 did not answer.")
        if self.candle_override is not None:
            return [Candle(self.candle_override(name), 1.0, 1.0, 1.0, 1.0)]
        opened = (self.now - self.candle_age + self.server_offset) // 60 * 60
        return [Candle(int(opened), 1.0, 1.0, 1.0, 1.0)]

    def get_history(self, days=30, limit=200):
        self.calls.append("get_history")
        if self.history_error is not None:
            raise self.history_error
        return sorted(self.history_, key=lambda t: t.time_close or 0, reverse=True)[:limit]


def state_for(stub, now, resolver=None, rules=R3):
    resolver = resolver or ServerClockResolver()
    return summarize_state(gather_state(stub, now, resolver), rules)


# =====================================================================================================================
class DayBoundaryTest(unittest.TestCase):
    """Trading-day transitions on the broker's clock."""

    def test_day_starts_at_broker_midnight(self):
        off = 3 * H                                                  # broker is UTC+3
        before = utc(2026, 10, 19, 20, 59, 59)                       # 23:59:59 broker time on 19 Oct
        after = utc(2026, 10, 19, 21, 0, 0)                          # 00:00:00 broker time on 20 Oct
        self.assertEqual(day_start_from_offset(before, off), utc(2026, 10, 19, 0))        # broker midnight of the 19th (an epoch on the broker clock)
        self.assertEqual(day_start_from_offset(after, off), utc(2026, 10, 20, 0))
        self.assertEqual(day_start_from_offset(after, off) - day_start_from_offset(before, off), 86400)

    def test_boundary_is_one_second_exact_for_other_offsets(self):
        for off in (-5 * H, 0, 2 * H, 19800, -3 * H - 1800):         # UTC-5, UTC, UTC+2, UTC+5:30, UTC-3:30
            midnight_utc = utc(2026, 10, 20, 0) - off                # the real instant the broker's 20 Oct starts
            self.assertEqual(day_start_from_offset(midnight_utc - 1, off), utc(2026, 10, 19, 0), off)
            self.assertEqual(day_start_from_offset(midnight_utc, off), utc(2026, 10, 20, 0), off)

    def test_next_reset_is_the_next_broker_midnight_in_utc(self):
        off = 3 * H
        now = utc(2026, 10, 19, 12, 0, 0)
        self.assertEqual(next_reset_utc(now, off), utc(2026, 10, 19, 21, 0, 0))         # 00:00 broker = 21:00 UTC
        self.assertEqual(next_reset_utc(utc(2026, 10, 19, 21, 0, 0), off), utc(2026, 10, 20, 21, 0, 0))

    def test_old_local_day_fallback_was_the_bug(self):
        """THE REPRODUCTION. When the broker's offset could not be measured, the old code measured the day on THIS COMPUTER's clock
        (`day_start_from_offset(now, local_offset)`) and compared it with times that are on the BROKER's clock. Whenever the computer's
        calendar date differs from the broker's date, the counters reset at the computer's midnight instead of the broker's."""
        # A) computer ahead of the broker (UTC+8 vs UTC+3). 20:00 broker time on the 19th = 01:00 on the 20th on the computer.
        now = utc(2026, 10, 19, 17, 0, 0)
        loss_10am = utc(2026, 10, 19, 10, 0, 0)                                        # closed at 10:00 broker time on the 19th = TODAY
        self.assertLess(loss_10am, day_start_from_offset(now, 8 * H))                  # old code: counted as yesterday -> reset 5 h too early
        self.assertGreaterEqual(loss_10am, day_start_from_offset(now, 3 * H))          # correct: still today
        # B) computer behind the broker (UTC-5 vs UTC+3). 00:30 broker time on the 20th = 16:30 on the 19th on the computer.
        now = utc(2026, 10, 19, 21, 30, 0)
        loss_2330 = utc(2026, 10, 19, 23, 30, 0)                                       # closed at 23:30 broker time on the 19th = YESTERDAY
        self.assertGreaterEqual(loss_2330, day_start_from_offset(now, -5 * H))         # old code: still counted after the broker's midnight
        self.assertLess(loss_2330, day_start_from_offset(now, 3 * H))                  # correct: yesterday
        # C) the cutoff the fix uses never depends on the computer at all
        self.assertEqual(day_start_from_offset(now, 3 * H), utc(2026, 10, 20, 0, 0, 0))

    def test_dst_follows_the_zone_not_a_fixed_number(self):
        self.assertEqual(zone_offset_seconds("America/New_York", utc(2026, 1, 15, 12)), -5 * H)
        self.assertEqual(zone_offset_seconds("America/New_York", utc(2026, 7, 15, 12)), -4 * H)
        self.assertEqual(zone_offset_seconds("Europe/Athens", utc(2026, 1, 15, 12)), 2 * H)
        self.assertEqual(zone_offset_seconds("Europe/Athens", utc(2026, 7, 15, 12)), 3 * H)
        # the instant of the US spring-forward: 07:00 UTC on 8 March 2026
        self.assertEqual(zone_offset_seconds("America/New_York", utc(2026, 3, 8, 6, 59, 59)), -5 * H)
        self.assertEqual(zone_offset_seconds("America/New_York", utc(2026, 3, 8, 7, 0, 0)), -4 * H)
        # the broker's new day after a DST change starts at ITS new midnight
        self.assertEqual(day_start_from_offset(utc(2026, 3, 8, 12), zone_offset_seconds("America/New_York", utc(2026, 3, 8, 12))),
                         utc(2026, 3, 8, 0))

    def test_timezone_validation(self):
        self.assertTrue(valid_timezone("Europe/Athens"))
        for bad in ("", "  ", "Not/AZone", "UTC+3", None, 5, "x" * 100):
            self.assertFalse(valid_timezone(bad), repr(bad))
        with self.assertRaises(ValueError):
            zone_offset_seconds("Not/AZone", 0)


# =====================================================================================================================
class OffsetMeasurementTest(unittest.TestCase):
    """The broker's offset is measured from a FRESH 1-minute candle and never guessed from a stale one."""

    NOW = utc(2026, 10, 19, 12, 0, 30)

    def test_fresh_candle_gives_the_offset(self):
        for off in (3 * H, 2 * H, -5 * H, 0, 19800):
            for age in (0, 5, 30, 59):
                opened = (self.NOW - age + off) // 60 * 60
                self.assertEqual(offset_from_candle(opened, self.NOW), off, (off, age))

    def test_stale_or_odd_candles_are_not_a_measurement(self):
        off = 3 * H
        for age in (150, 300, 600, 5000):                            # a closed market / quiet symbol
            self.assertIsNone(offset_from_candle(self.NOW - age + off, self.NOW), age)
        self.assertIsNone(offset_from_candle(self.NOW + off + 120, self.NOW))      # candle from the future
        self.assertIsNone(offset_from_candle(self.NOW + 20 * H, self.NOW))         # impossible offset
        for junk in (None, "x", float("nan")):
            self.assertIsNone(offset_from_candle(junk, self.NOW))

    def test_two_symbols_agree(self):
        stub = ReadOnlyStub(self.NOW, server_offset=3 * H)
        off, why = ServerClockResolver.probe(stub, ["EURUSD", "GBPUSD", "XAUUSD"], self.NOW)
        self.assertEqual(off, 3 * H)
        self.assertEqual(stub.calls.count("get_candles"), 2, "stops as soon as two symbols agree")

    def test_symbols_that_disagree_are_not_trusted(self):
        stub = ReadOnlyStub(self.NOW, candle_override=lambda n: (self.NOW + (3 * H if n == "EURUSD" else 2 * H)) // 60 * 60)
        off, why = ServerClockResolver.probe(stub, ["EURUSD", "GBPUSD"], self.NOW)
        self.assertIsNone(off)
        self.assertIn("disagree", why)

    def test_one_fresh_symbol_among_stale_ones_is_enough(self):
        stub = ReadOnlyStub(self.NOW, candle_override=lambda n: (self.NOW + 3 * H) // 60 * 60 if n == "XAUUSD" else self.NOW - 86400 * 2)
        self.assertEqual(ServerClockResolver.probe(stub, ["EURUSD", "GBPUSD", "XAUUSD"], self.NOW)[0], 3 * H)

    def test_bridge_not_answering_gives_up_quickly(self):
        stub = ReadOnlyStub(self.NOW, server_offset=None)
        off, why = ServerClockResolver.probe(stub, ["A", "B", "C", "D"], self.NOW)
        self.assertIsNone(off)
        self.assertLessEqual(stub.calls.count("get_candles"), 2, "must not wait for every symbol when MT5 is not answering")


# =====================================================================================================================
class ResolverTest(unittest.TestCase):
    NOW = utc(2026, 10, 19, 12, 0, 0)

    def test_live_measurement_is_reused_for_a_minute_then_remeasured(self):
        stub, res = ReadOnlyStub(self.NOW), ServerClockResolver()
        first = res.resolve(stub, ["EURUSD", "GBPUSD"], self.NOW)
        self.assertEqual((first.offset_seconds, first.source), (3 * H, "live"))
        n = stub.calls.count("get_candles")
        res.resolve(stub, ["EURUSD", "GBPUSD"], self.NOW + 30)
        self.assertEqual(stub.calls.count("get_candles"), n, "no new bridge round-trip inside the reuse window")
        stub.now = self.NOW + 90
        res.resolve(stub, ["EURUSD", "GBPUSD"], self.NOW + 90)
        self.assertGreater(stub.calls.count("get_candles"), n, "re-measured after the reuse window")

    def test_last_measurement_survives_a_failed_probe_for_a_limited_time(self):
        stub, res = ReadOnlyStub(self.NOW), ServerClockResolver()
        res.resolve(stub, ["EURUSD", "GBPUSD"], self.NOW)
        stub.server_offset = None                                    # MT5 stops answering
        later = res.resolve(stub, ["EURUSD", "GBPUSD"], self.NOW + 2 * H)
        self.assertEqual((later.offset_seconds, later.source), (3 * H, "cached"))
        self.assertIn("last live measurement", later.detail)
        expired = res.resolve(stub, ["EURUSD", "GBPUSD"], self.NOW + CACHE_MAX_AGE_SECONDS + 60)
        self.assertEqual((expired.offset_seconds, expired.source), (None, "unknown"))

    def test_configured_zone_is_the_fallback_and_follows_dst(self):
        stub = ReadOnlyStub(self.NOW, server_offset=None)
        res = ServerClockResolver("Europe/Athens")
        summer = res.resolve(stub, ["EURUSD"], utc(2026, 7, 15, 12))
        winter = res.resolve(stub, ["EURUSD"], utc(2026, 1, 15, 12))
        self.assertEqual((summer.offset_seconds, summer.source), (3 * H, "configured"))
        self.assertEqual((winter.offset_seconds, winter.source), (2 * H, "configured"))

    def test_unknown_when_nothing_is_available_and_it_says_how_to_fix_it(self):
        clock = ServerClockResolver().resolve(ReadOnlyStub(self.NOW, server_offset=None), ["EURUSD"], self.NOW)
        self.assertFalse(clock.known)
        self.assertEqual(clock.source, "unknown")
        self.assertIn("broker_timezone", clock.detail)

    def test_live_measurement_wins_over_the_configured_zone_and_the_conflict_is_reported(self):
        res = ServerClockResolver("America/New_York")                # says UTC-4 in October
        clock = res.resolve(ReadOnlyStub(self.NOW), ["EURUSD", "GBPUSD"], self.NOW)   # the broker really is UTC+3
        self.assertEqual((clock.offset_seconds, clock.source), (3 * H, "live"))
        self.assertIn("broker_timezone", clock.detail)

    def test_a_different_broker_connection_never_reuses_the_cache(self):
        res = ServerClockResolver()
        res.resolve(ReadOnlyStub(self.NOW, server_offset=3 * H), ["EURUSD", "GBPUSD"], self.NOW)
        other = ReadOnlyStub(self.NOW, server_offset=None)
        self.assertEqual(res.resolve(other, ["EURUSD"], self.NOW + 5).source, "unknown")

    def test_the_computers_timezone_is_never_consulted(self):
        def forbidden(*a, **k):
            raise AssertionError("the computer's local timezone must not be used for the trading day")
        with mock.patch("time.localtime", forbidden), mock.patch("time.timezone", 99999), mock.patch("time.altzone", 99999):
            clock = ServerClockResolver().resolve(ReadOnlyStub(self.NOW, server_offset=None), ["EURUSD"], self.NOW)
            self.assertFalse(clock.known)
            stub = ReadOnlyStub(self.NOW)
            self.assertEqual(state_for(stub, self.NOW)["todays_loss"], 0.0)


# =====================================================================================================================
class DailyLossCounterTest(unittest.TestCase):
    """End to end through gather_state + summarize_state with simulated broker data."""

    def test_counters_reset_at_local_midnight(self):
        """The day is the computer's LOCAL day (here Asia/Manila, UTC+8), NOT the broker's (UTC+3). Broker timestamps are converted before comparing."""
        with local_tz("Asia/Manila"):
            close = utc(2026, 10, 19, 15, 50, 0) + 3 * H                 # a 300 loss closed at 23:50 Manila = 15:50 UTC (broker clock = UTC+3)
            history = [trade(-300.0, close)]
            now_before = utc(2026, 10, 19, 15, 55, 0)                    # 23:55 Manila: the loss counts and trading is locked (3 % of 10,000 = 300)
            before = state_for(ReadOnlyStub(now_before, balance=9700.0, history=history), now_before)
            self.assertAlmostEqual(before["start_of_day_balance"], 10000.0)
            self.assertAlmostEqual(before["todays_loss"], 300.0)
            self.assertTrue(before["daily_locked"])
            now_after = utc(2026, 10, 19, 16, 5, 0)                      # 00:05 Manila: a NEW day, although the broker clock still reads 19:05 on the 19th
            after = state_for(ReadOnlyStub(now_after, balance=9700.0, history=history), now_after)
            self.assertEqual(after["todays_closed_pl"], 0.0)
            self.assertEqual(after["todays_loss"], 0.0)
            self.assertAlmostEqual(after["start_of_day_balance"], 9700.0)
            self.assertAlmostEqual(after["daily_remaining"], 291.0)       # 3 % of the new 9,700 start-of-day balance
            self.assertFalse(after["daily_locked"])
            self.assertEqual(portfolio_breaches(after, R3), [])
            self.assertEqual(after["next_reset_utc"], utc(2026, 10, 20, 16, 0, 0))      # the next LOCAL midnight (21 Oct 00:00 Manila)
            self.assertIn("local midnight", after["day_start_source"])

    def test_the_brokers_midnight_is_not_a_boundary(self):
        """Broker midnight (21:00 UTC here) falls inside the local day: a loss before it and one after it both count."""
        with local_tz("Asia/Manila"):
            now = utc(2026, 10, 19, 21, 30, 0)                           # 05:30 Manila on the 20th, 00:30 broker
            history = [trade(-30.0, utc(2026, 10, 19, 20, 50, 0) + 3 * H), trade(-20.0, utc(2026, 10, 19, 21, 10, 0) + 3 * H, ticket=2)]
            self.assertAlmostEqual(state_for(ReadOnlyStub(now, balance=9950.0, history=history), now)["todays_loss"], 50.0)

    def test_loss_closed_exactly_at_local_midnight_belongs_to_the_new_day(self):
        with local_tz("Asia/Manila"):
            now = utc(2026, 10, 19, 16, 5, 0)                            # 00:05 Manila
            midnight = utc(2026, 10, 19, 16, 0, 0) + 3 * H               # 00:00:00 Manila on the broker's clock
            for t_close, expect in ((midnight - 1, 0.0), (midnight, 40.0)):
                s = state_for(ReadOnlyStub(now, balance=9960.0, history=[trade(-40.0, t_close)]), now)
                self.assertAlmostEqual(s["todays_loss"], expect, msg=str(t_close))

    def test_the_brokers_offset_cannot_move_the_local_boundary(self):
        """Same local day, same instant: the result is identical whatever the BROKER's offset is; only the configured local timezone decides the day."""
        now = utc(2026, 10, 19, 21, 30, 0)
        trades_utc = [(-250.0, utc(2026, 10, 19, 15, 30, 0)), (-20.0, utc(2026, 10, 19, 16, 10, 0))]      # 23:30 and 00:10 Manila
        with local_tz("Asia/Manila"):
            results = []
            for off in (3 * H, 0, -5 * H, 2 * H):
                history = [trade(n, t + off, ticket=i + 1) for i, (n, t) in enumerate(trades_utc)]
                results.append(state_for(ReadOnlyStub(now, server_offset=off, balance=9730.0, history=history), now))
            for r in results[1:]:
                self.assertEqual(r["todays_loss"], results[0]["todays_loss"])
                self.assertEqual(r["start_of_day_balance"], results[0]["start_of_day_balance"])
            self.assertAlmostEqual(results[0]["todays_loss"], 20.0)       # only the 00:10 Manila loss belongs to the new local day
        with local_tz("America/Los_Angeles"):                              # a different LOCAL timezone is a different day
            history = [trade(n, t + 3 * H, ticket=i + 1) for i, (n, t) in enumerate(trades_utc)]
            self.assertAlmostEqual(state_for(ReadOnlyStub(now, balance=9730.0, history=history), now)["todays_loss"], 270.0)

    def test_restart_does_not_reset_the_counters(self):
        """Counters come from the broker's data, not from anything held in this process: a brand-new resolver (= a restart)
        gives exactly the same numbers."""
        now = utc(2026, 10, 19, 15, 0, 0)
        history = [trade(-75.5, utc(2026, 10, 19, 14, 0, 0)), trade(+20.0, utc(2026, 10, 19, 15, 59, 0) - 3600, ticket=2)]
        a = state_for(ReadOnlyStub(now, balance=9944.5, history=history), now, ServerClockResolver())
        b = state_for(ReadOnlyStub(now, balance=9944.5, history=history), now, ServerClockResolver())     # "restarted" application
        for key in ("todays_loss", "daily_remaining", "start_of_day_balance", "todays_closed_pl", "daily_locked"):
            self.assertEqual(a[key], b[key], key)
        self.assertAlmostEqual(a["todays_loss"], 55.5)

    def test_remaining_daily_loss_against_the_configured_limit(self):
        now = utc(2026, 10, 19, 12, 0, 0)
        history = [trade(-120.0, utc(2026, 10, 19, 9, 0, 0))]
        pos = [Position(5, "EURUSD", "buy", 0.1, 1.1, 1.09, 1.08, None, -30.0, 0.0)]
        s = state_for(ReadOnlyStub(now, balance=9880.0, equity=9850.0, positions=pos, history=history), now)
        self.assertAlmostEqual(s["start_of_day_balance"], 10000.0)
        self.assertAlmostEqual(s["daily_limit_amount"], 300.0)        # 3 % of 10,000
        self.assertAlmostEqual(s["todays_loss"], 150.0)               # 120 closed + 30 floating
        self.assertAlmostEqual(s["daily_remaining"], 150.0)
        self.assertAlmostEqual(s["daily_remaining_percent"], 1.5)
        self.assertFalse(s["daily_locked"])
        for pct, remaining in ((5.0, 350.0), (1.0, 0.0)):             # another configured limit: 5 % -> 500 - 150; 1 % -> 100 < 150 -> 0
            s2 = summarize_state(gather_state(ReadOnlyStub(now, balance=9880.0, equity=9850.0, positions=pos, history=history), now,
                                              ServerClockResolver()), RiskRules(1.0, pct, 3.0, 3))
            self.assertAlmostEqual(s2["daily_remaining"], remaining, msg=str(pct))
        self.assertTrue(s2["daily_locked"])                            # 1 % limit already exceeded

    def test_net_includes_swap_and_commission_and_profits_offset_losses(self):
        now = utc(2026, 10, 19, 12, 0, 0)
        history = [trade(-100.0, utc(2026, 10, 19, 5, 0, 0), ticket=1), trade(+60.0, utc(2026, 10, 19, 6, 0, 0), ticket=2),
                   trade(-7.25, utc(2026, 10, 19, 7, 0, 0), ticket=3, profit=-5.0, swap=-1.25, commission=-1.0)]
        s = state_for(ReadOnlyStub(now, balance=9952.75, history=history), now)
        self.assertAlmostEqual(s["todays_closed_pl"], -47.25)
        self.assertAlmostEqual(s["todays_loss"], 47.25)
        self.assertAlmostEqual(s["start_of_day_balance"], 10000.0)

    def test_a_profitable_day_is_zero_loss_with_the_full_allowance(self):
        now = utc(2026, 10, 19, 12, 0, 0)
        s = state_for(ReadOnlyStub(now, balance=10500.0, history=[trade(+500.0, utc(2026, 10, 19, 8, 0, 0))]), now)
        self.assertEqual(s["todays_loss"], 0.0)
        self.assertAlmostEqual(s["daily_remaining"], 300.0)

    def test_open_position_loss_counts_as_designed_and_a_floating_gain_does_not_hide_a_realised_loss_of_zero(self):
        now = utc(2026, 10, 19, 12, 0, 0)
        losing = [Position(5, "EURUSD", "buy", 0.1, 1.1, 1.09, 1.08, None, -45.0, -1.0)]
        s = state_for(ReadOnlyStub(now, balance=10000.0, equity=9954.0, positions=losing), now)
        self.assertAlmostEqual(s["floating_pl"], -46.0)               # profit + swap
        self.assertAlmostEqual(s["todays_loss"], 46.0)
        winning = [Position(5, "EURUSD", "buy", 0.1, 1.1, 1.12, 1.08, None, 80.0, 0.0)]
        self.assertEqual(state_for(ReadOnlyStub(now, balance=10000.0, equity=10080.0, positions=winning), now)["todays_loss"], 0.0)

    def test_yesterdays_trades_are_not_counted(self):
        with local_tz("Asia/Manila"):
            now = utc(2026, 10, 19, 12, 0, 0)                            # 20:00 Manila on the 19th; the local day began at 18 Oct 16:00 UTC
            history = [trade(-999.0, utc(2026, 10, 18, 10, 0, 0) + 3 * H), trade(-10.0, utc(2026, 10, 19, 1, 0, 0) + 3 * H, ticket=2)]
            self.assertAlmostEqual(state_for(ReadOnlyStub(now, balance=9990.0, history=history), now)["todays_loss"], 10.0)


# =====================================================================================================================
class MissingDataIsNeverZeroTest(unittest.TestCase):
    NOW = utc(2026, 10, 19, 12, 0, 0)

    def assert_unknown(self, s, *words):
        self.assertIsNone(s["todays_loss"])
        self.assertIsNone(s["daily_remaining"])
        self.assertFalse(s["daily_locked"], "unknown is not 'locked' either; the trade check fails separately")
        for w in words:
            self.assertIn(w, s["history_error"])

    def test_history_unavailable(self):
        for exc in (BrokerUnavailableError("MT5 did not send the trade history in time."), NotImplementedError()):
            s = state_for(ReadOnlyStub(self.NOW, history_error=exc), self.NOW)
            self.assertIsNone(s["todays_closed_pl"])
            self.assert_unknown(s)
            self.assertTrue(s["history_error"].strip())

    def test_old_bridge_message_is_passed_through(self):
        s = state_for(ReadOnlyStub(self.NOW, history_error=BrokerUnavailableError("The TradeMateBridge EA in MT5 is older than v0.4")), self.NOW)
        self.assert_unknown(s, "older than v0.4")

    def test_broker_clock_unknown_means_unknown_not_the_local_day(self):
        s = state_for(ReadOnlyStub(self.NOW, server_offset=None, history=[trade(-50.0, self.NOW - 100)]), self.NOW)
        self.assert_unknown(s, "Broker server time could not be determined")
        self.assertEqual(s["day_start_source"], "unknown")
        self.assertIsNone(s["day_start"])

    def test_the_trade_check_refuses_while_the_daily_loss_is_unknown(self):
        stub = ReadOnlyStub(self.NOW, server_offset=None)
        state = gather_state(stub, self.NOW, ServerClockResolver())
        d = evaluate_trade(state, sc.proposal(0.5), sc.GOLD, R3)
        self.assertEqual(d.decision, "REJECTED")
        self.assertIn("cannot be verified", d.rejection_reason)
        self.assertIn("Broker server time could not be determined", d.rejection_reason)
        self.assertFalse(d.order_sent)

    def test_trade_without_a_result_today(self):
        s = state_for(ReadOnlyStub(self.NOW, history=[trade(None, self.NOW - 600, ticket=77, profit=None)]), self.NOW)
        self.assert_unknown(s, "#77")

    def test_trade_without_a_close_time(self):
        s = state_for(ReadOnlyStub(self.NOW, history=[trade(-5.0, None, ticket=78, time_open=None)]), self.NOW)
        self.assert_unknown(s, "no close time")

    def test_old_trade_without_a_result_does_not_matter(self):
        old = trade(None, utc(2026, 10, 16, 12), ticket=79, profit=None)
        self.assertEqual(state_for(ReadOnlyStub(self.NOW, history=[old]), self.NOW)["todays_loss"], 0.0)

    def test_open_position_without_a_profit_figure(self):
        pos = [Position(9, "EURUSD", "buy", 0.1, 1.1, 1.09, 1.08, None, None, 0.0)]
        s = state_for(ReadOnlyStub(self.NOW, positions=pos), self.NOW)
        self.assert_unknown(s, "#9")
        self.assertIsNone(s["floating_pl"])

    def test_history_cut_short_by_the_bridge_limit(self):
        # 500 closed trades, all newer than the start of the day: older ones may have been dropped -> cannot be complete
        flood = [trade(-1.0, self.NOW - 60 - i, ticket=1000 + i) for i in range(HISTORY_LIMIT)]
        s = state_for(ReadOnlyStub(self.NOW, balance=9500.0, history=flood), self.NOW)
        self.assert_unknown(s, "limit")
        # 500 trades that DO reach back before the day started are complete
        reach = [trade(-1.0, utc(2026, 10, 19, 11, 0, 0) + i, ticket=2000 + i) for i in range(HISTORY_LIMIT - 1)]
        reach.append(trade(-1.0, utc(2026, 10, 18, 12, 0, 0), ticket=3000))
        s2 = state_for(ReadOnlyStub(self.NOW, balance=9500.0, history=reach), self.NOW)
        self.assertAlmostEqual(s2["todays_loss"], HISTORY_LIMIT - 1)

    def test_build_state_never_turns_none_into_zero(self):
        st = build_state(account(), [], {}, [trade(None, 1_800_000_100, profit=None)], 1_800_000_000, "t")
        self.assertIsNone(st.todays_closed_pl)
        self.assertIsNone(st.start_of_day_balance)
        self.assertIsNone(summarize_state(st, R3)["todays_loss"])

    def test_configured_broker_timezone_rescues_a_closed_market(self):
        stub = ReadOnlyStub(utc(2026, 7, 15, 12), server_offset=None, balance=9900.0,
                            history=[trade(-100.0, utc(2026, 7, 15, 9, 0, 0))])      # Athens is UTC+3 in July -> broker day started 21:00 UTC
        s = state_for(stub, utc(2026, 7, 15, 12), ServerClockResolver("Europe/Athens"))
        self.assertAlmostEqual(s["todays_loss"], 100.0)
        self.assertIn("configured", s["day_start_source"])


# =====================================================================================================================
class NoOrderIsEverSentTest(unittest.TestCase):
    def test_only_read_calls_are_made(self):
        now = utc(2026, 10, 19, 12, 0, 0)
        stub = ReadOnlyStub(now, history=[trade(-10.0, now - 600)])
        state_for(stub, now)
        self.assertTrue(set(stub.calls) <= ReadOnlyStub.READS, stub.calls)
        with self.assertRaises(AssertionError):
            stub.place_order("EURUSD", "buy", 0.1)                    # the guard itself works
        with self.assertRaises(AssertionError):
            stub.close_position(1)


# =====================================================================================================================
class ConfigTest(unittest.TestCase):
    def load(self, text, env=None):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.toml"
            p.write_text(text, encoding="utf-8")
            with mock.patch.dict(os.environ, env or {}):
                if not env:
                    os.environ.pop("TRADEMATE_TIMEZONE", None)
                return load_config(p)

    def test_defaults_are_empty_so_nothing_is_guessed(self):
        s = self.load("")
        self.assertEqual((s.risk.broker_timezone, s.ui.timezone), ("", ""))

    def test_valid_zones_are_accepted(self):
        s = self.load('[risk]\nbroker_timezone = "Europe/Athens"\n[ui]\ntimezone = "Asia/Manila"\n')
        self.assertEqual((s.risk.broker_timezone, s.ui.timezone), ("Europe/Athens", "Asia/Manila"))

    def test_invalid_zones_fail_loudly(self):
        with self.assertRaises(ConfigError) as cm:
            self.load('[risk]\nbroker_timezone = "Mars/Olympus"\n')
        self.assertIn("broker_timezone", str(cm.exception))
        with self.assertRaises(ConfigError) as cm:
            self.load('[ui]\ntimezone = "Nowhere"\n')
        self.assertIn("[ui] timezone", str(cm.exception))

    def test_environment_overrides_the_ui_timezone(self):
        s = self.load('[ui]\ntimezone = "Asia/Manila"\n', env={"TRADEMATE_TIMEZONE": "America/Chicago"})
        self.assertEqual(s.ui.timezone, "America/Chicago")

    def test_shipped_settings_file_loads_and_keeps_the_risk_limits(self):
        from trademate.config import DEFAULT_CONFIG_PATH
        s = load_config(DEFAULT_CONFIG_PATH)
        self.assertEqual((s.risk.max_daily_loss_percent, s.risk.max_risk_percent, s.risk.max_positions), (3.0, 1.0, 3))


# =====================================================================================================================
class RoutesWithFakeBridgeTest(unittest.TestCase):
    """The real adapter + Flask routes, against the FIXTURE bridge only (it never reaches MT5)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bridge = FakeBridge(Path(tmp.name)).start()
        self.addCleanup(self.bridge.stop)
        settings = Settings(app=AppSettings(data_dir=Path(tmp.name)), mt5=Mt5Settings(data_dir=Path(tmp.name)),
                            trading=TradingSettings(command_timeout_seconds=2.0, confirm_timeout_seconds=1.5))
        self.adapter = create_adapter(settings)
        self.store = RiskRulesStore(Path(tmp.name) / "risk_rules.json", RiskRules(1.0, 3.0, 3.0, 3))
        brokertime.DEFAULT_RESOLVER.reset()
        brokertime.DEFAULT_RESOLVER.configure("")
        for patcher in (mock.patch.object(app_module, "adapter", self.adapter), mock.patch.object(app_module, "risk_store", self.store)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(brokertime.DEFAULT_RESOLVER.reset)
        self.client = app.test_client()

    def test_market_time_reports_the_broker_offset(self):
        for off in (3 * H, -5 * H, 0):
            brokertime.DEFAULT_RESOLVER.reset()
            self.bridge.server_offset = off
            res = self.client.get("/api/market/time").get_json()
            self.assertTrue(res["ok"], res)
            self.assertEqual((res["server_offset_seconds"], res["source"]), (off, "live"))
            self.assertAlmostEqual(res["utc_epoch"], time.time(), delta=5)

    def test_market_time_says_unknown_instead_of_guessing(self):
        self.bridge.server_offset = 0
        with mock.patch.object(self.adapter, "get_candles", side_effect=BrokerUnavailableError("MT5 did not answer.")):
            res = self.client.get("/api/market/time").get_json()
        self.assertFalse(res["ok"])
        self.assertIsNone(res["server_offset_seconds"])
        self.assertEqual(res["source"], "unknown")
        self.assertIn("broker", res["detail"].lower())

    def test_risk_state_counts_a_loss_closed_today_converted_from_the_broker_clock(self):
        self.bridge.server_offset = 3 * H                              # broker is UTC+3: fixture history and M1 candles use that clock
        self.assertTrue(self.adapter.place_order("EURUSD", "buy", 1.0, 1.0, 1.2).confirmed)             # FIXTURE only
        ticket = self.adapter.get_positions()[0].ticket
        self.bridge.bid = 1.0990
        self.assertTrue(self.adapter.close_position(ticket).confirmed)
        s = self.client.get("/api/risk/state").get_json()["state"]
        self.assertLess(s["todays_closed_pl"], 0)
        self.assertGreater(s["todays_loss"], 0)
        self.assertEqual(s["server_offset_seconds"], 3 * H)
        self.assertIn("local midnight", s["day_start_source"])
        self.assertGreater(s["next_reset_utc"], time.time())
        self.assertLessEqual(s["next_reset_utc"], time.time() + 90000 + 5)           # the next local midnight is at most 25 hours away (a DST day)

    def test_risk_state_reports_unknown_when_the_broker_clock_is_unreadable(self):
        with mock.patch.object(self.adapter, "get_candles", side_effect=BrokerUnavailableError("MT5 did not answer.")):
            s = self.client.get("/api/risk/state").get_json()["state"]
        self.assertIsNone(s["todays_loss"])
        self.assertIsNone(s["daily_remaining"])
        self.assertIn("Broker server time could not be determined", s["history_error"])

    def test_risk_state_and_evaluate_never_send_a_command_that_trades(self):
        self.bridge.commands_seen.clear()
        self.client.get("/api/risk/state")
        self.client.get("/api/market/time")
        self.client.post("/api/risk/evaluate", json={"symbol": "EURUSD", "direction": "buy", "entry": 1.1, "stop_loss": 1.095, "risk_percent": 1.0})
        sent = {c["cmd"] for c in self.bridge.commands_seen}
        self.assertTrue(sent <= {"candles", "history", "symbol_info", "symbols_search"}, sent)


if __name__ == "__main__":
    unittest.main()
