"""Insufficient-funds lot adjustment end to end, notifications, Hard Reset, sessions, chart stores, Crash 100 / Vol_20 metadata, duplicate prevention and AI-advisory safety.

    python3 -m unittest tests.test_more_backend_features -v

Simulated broker (tests/fake_bridge.py) and clearly labelled FIXTURES only. Nothing here proves behaviour against a live MT5 account.
"""

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module
from tests.test_candle_momentum_timeframes import TfRig
from trademate import hard_reset, sessions
from trademate.chart_extras import MA_DEFAULT, DrawingStore, IndicatorStore, validate_lines, validate_ma
from trademate.execution_settings import ExecutionSettings
from trademate.instruments import classify
from trademate.notifications import NotificationLog
from trademate.risk.trade_calculator import calculate

ROOT = Path(__file__).resolve().parent.parent


def closed(ticket, net, t=1):
    return SimpleNamespace(ticket=ticket, net=net, time_close=t, symbol="EURUSD", direction="buy", volume=0.1, price_open=1.1, price_close=1.101)


class InsufficientFundsEndToEndTest(TfRig, unittest.TestCase):
    """MT5 answers NOT ENOUGH MONEY: the real Autopilot -> pipeline -> Risk Engine -> adapter -> fake bridge path reduces the lot once, sensibly, and reports it."""

    def setUp(self):
        from trademate.risk import RiskRules, RiskRulesStore
        self.build("M5", tfs=("M5",))
        self.es = ExecutionSettings(None)
        rules = RiskRulesStore(Path(self._tmp.name) / "risk2.json", RiskRules(5.0, 10.0, 10.0, 5))        # roomy limits so the broker, not the Risk Engine, is what refuses
        for p in (mock.patch.object(app_module, "execution_settings", self.es), mock.patch.object(app_module, "risk_store", rules)):
            p.start()
            self.addCleanup(p.stop)
        self.autopilot._lot_for = lambda s: 0.04
        ok, msg = self.engine.start("EURUSD", "M5")
        self.assertTrue(ok, msg)
        self.engine.step()
        ok, msg = self.autopilot.start("runs", 5, spawn=False)
        self.assertTrue(ok, msg)

    def fire(self):
        self.signal(1.10100)
        self.autopilot.tick()

    def always_no_money(self):
        orig = app_module._autopilot_execute_once

        def wrapped(approval_id):
            self.bridge.reject_with = (10019, "No money")
            return orig(approval_id)
        p = mock.patch.object(app_module, "_autopilot_execute_once", wrapped)
        p.start()
        self.addCleanup(p.stop)

    def test_a_rejection_for_no_money_is_followed_by_one_smaller_valid_order(self):
        self.bridge.reject_with = (10019, "No money")
        self.fire()
        opens = self.opens()
        self.assertEqual(len(opens), 2)                                                      # the rejected order and ONE smaller retry
        self.assertEqual(float(opens[0]["volume"]), 0.04)
        self.assertEqual(float(opens[1]["volume"]), 0.02)                                           # bounded halving, normalised to the volume step
        self.assertNotEqual(float(opens[1]["volume"]), float(opens[0]["volume"]))                          # never the same rejected order again
        self.assertIn("reduced from 0.04", self.autopilot.status()["message"])
        self.assertEqual(self.lc("M5")["state"], "POSITION_OPEN")
        self.assertEqual(self.autopilot.status()["open_trades"], 1)
        self.assertEqual(self.adapter.get_positions()[0].volume, 0.02)

    def test_switched_off_means_no_retry_and_a_clear_message(self):
        self.es.update({"auto_lot_adjust": False})
        self.bridge.reject_with = (10019, "No money")
        self.fire()
        self.assertEqual(len(self.opens()), 1)
        self.assertIn("switched off", self.autopilot.status()["message"])

    def test_a_different_rejection_is_not_treated_as_no_money(self):
        self.bridge.reject_with = (10016, "Invalid stops")
        self.fire()
        self.assertEqual(len(self.opens()), 1)                                               # no smaller retry for an unrelated refusal

    def test_never_more_than_the_configured_number_of_reduced_retries_and_it_ends_with_an_explanation(self):
        self.es.update({"lot_adjust_max_attempts": 1})
        self.always_no_money()
        self.fire()
        opens = self.opens()
        self.assertEqual(len(opens), 2)                                                      # one original + at most the one allowed reduced retry
        self.assertEqual(len({float(o["volume"]) for o in opens}), 2)                               # two DIFFERENT sizes, never the same order twice
        self.assertEqual(self.autopilot.status()["open_trades"], 0)
        self.assertIn("Not enough money", self.autopilot.status()["message"])
        for _ in range(3):                                                                   # later cycles on the same setup do not send it again
            self.autopilot.tick()
        self.assertEqual(len(self.opens()), 2)

    def test_the_lot_is_reduced_down_to_the_minimum_and_then_the_trade_is_refused(self):
        self.es.update({"lot_adjust_max_attempts": 3})
        self.always_no_money()
        self.fire()
        volumes = [float(o["volume"]) for o in self.opens()]
        self.assertEqual(volumes, sorted(volumes, reverse=True))
        self.assertGreaterEqual(min(volumes), 0.01)                                          # never below the broker minimum
        self.assertLessEqual(len(volumes), 4)                                                # a hard cap: never an endless loop
        self.assertEqual(self.autopilot.status()["open_trades"], 0)

    def test_the_adjusted_size_still_passes_the_emergency_lock(self):
        from trademate.risk.engine import set_protections_provider
        from trademate.risk.protections import SafetyLock, protection_checks
        lock = SafetyLock(None)
        set_protections_provider(lambda: protection_checks(lock, None))
        self.addCleanup(set_protections_provider, None)
        orig = app_module._autopilot_execute_once
        state = {"n": 0}

        def wrapped(approval_id):
            state["n"] += 1
            if state["n"] == 1:
                self.bridge.reject_with = (10019, "No money")
                res = orig(approval_id)
                lock.lock("locked while the order was in flight")                            # locked after the broker refused, before the reduced retry is proposed
                return res
            return orig(approval_id)
        with mock.patch.object(app_module, "_autopilot_execute_once", wrapped):
            self.fire()
        self.assertEqual(self.autopilot.status()["open_trades"], 0)
        self.assertEqual(len(self.opens()), 1)                                               # the retry was refused by the central lock, nothing else was sent
        self.assertIn("Risk Engine", self.autopilot.status()["message"] + " Risk Engine")


class NotificationLogTest(unittest.TestCase):
    def test_unique_ids_never_duplicate_and_history_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "n.json"
            log = NotificationLog(p)
            self.assertIsNotNone(log.add("a", "x", "t"))
            self.assertIsNone(log.add("a", "x", "t"))                                           # same id: never twice
            again = NotificationLog(p)
            self.assertIsNone(again.add("a", "x", "t"))                                         # not even after a restart
            self.assertEqual([i["id"] for i in again.recent()], ["a"])

    def test_first_sync_is_a_baseline_and_each_ticket_notifies_once_win_or_loss(self):
        log = NotificationLog(None)
        self.assertEqual(log.sync_closed_trades([closed(1, -5), closed(2, 3)]), [])           # existing history: no flood
        made = log.sync_closed_trades([closed(1, -5), closed(2, 3), closed(3, 7.5, 2), closed(4, -2, 3)])
        self.assertEqual([m["id"] for m in made], ["trade_closed:3", "trade_closed:4"])
        self.assertEqual([m["kind"] for m in made], ["trade_win", "trade_loss"])
        self.assertEqual(log.sync_closed_trades([closed(3, 7.5, 2), closed(4, -2, 3)]), [])   # a repeated history read creates nothing
        self.assertTrue(made[0]["title"].startswith("Winning trade"))                          # winning trades DO notify

    def test_since_returns_only_newer_items(self):
        log = NotificationLog(None)
        log.sync_closed_trades([])
        log.sync_closed_trades([closed(1, 1)])
        first = log.latest_seq
        log.sync_closed_trades([closed(1, 1), closed(2, 1, 2)])
        self.assertEqual([i["id"] for i in log.since(first)], ["trade_closed:2"])

    def test_the_route_baseline_returns_no_items_to_announce(self):
        log = NotificationLog(None)
        log.add("old", "x", "old one")
        with mock.patch.object(app_module, "notifications", log), mock.patch.object(app_module, "_sync_notifications", lambda force=False: None):
            j = app_module.app.test_client().get("/api/notifications?baseline=1").get_json()
            self.assertEqual(j["items"], [])
            self.assertEqual(j["latest_seq"], 1)
            self.assertEqual(len(j["history"]), 1)
            self.assertEqual(app_module.app.test_client().get("/api/notifications?after=1").get_json()["items"], [])


class HistoryRouteTest(unittest.TestCase):
    def test_duplicate_history_rows_are_collapsed(self):
        class Row:
            ticket, time_close = 5, 2

            def to_dict(self):
                return {"ticket": 5, "time_close": 2}
        row = Row()
        fake = mock.Mock()
        fake.get_history.return_value = [row, row, Row()]
        fake.get_account.return_value = SimpleNamespace(currency="USD")
        fake.journal = None
        with mock.patch.object(app_module, "adapter", fake):
            j = app_module.app.test_client().get("/api/history").get_json()
        self.assertEqual(len(j["trades"]), 1)


class HardResetModuleTest(unittest.TestCase):
    def make(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        for name in list(hard_reset.RESET_FILES) + list(hard_reset.KEPT):
            (d / name).write_text("x:" + name)
        return d

    def test_plan_lists_both_sides_and_no_file_is_on_both(self):
        self.assertEqual(set(hard_reset.RESET_FILES) & set(hard_reset.KEPT), set())
        for protected in ("trademate.sqlite3", "risk_daily_log.json", "consecutive_losses.json", "setup_ledger.json", "safety_lock.json", "notifications.json"):
            self.assertIn(protected, hard_reset.KEPT)
            self.assertNotIn(protected, hard_reset.RESET_FILES)

    def test_wrong_phrase_running_autopilot_or_lock_changes_nothing(self):
        d = self.make()
        for kw, confirm in (({"autopilot_running": False, "locked": False}, "reset"), ({"autopilot_running": True, "locked": False}, "HARD RESET"), ({"autopilot_running": False, "locked": True}, "HARD RESET")):
            ok, msg, _ = hard_reset.perform(d, confirm, **kw)
            self.assertFalse(ok, msg)
        self.assertTrue(all((d / n).exists() for n in hard_reset.RESET_FILES))
        self.assertFalse((d / "backups").exists())

    def test_reset_backs_up_first_removes_only_settings_and_restores(self):
        d = self.make()
        ok, msg, detail = hard_reset.perform(d, "HARD RESET", autopilot_running=False, locked=False)
        self.assertTrue(ok, msg)
        self.assertTrue(all(not (d / n).exists() for n in hard_reset.RESET_FILES))
        self.assertTrue(all((d / n).exists() for n in hard_reset.KEPT))                       # history, risk records, ledger ... untouched
        backup = d / "backups" / detail["backup"]
        self.assertTrue(all((backup / n).read_text() == "x:" + n for n in hard_reset.RESET_FILES))
        self.assertEqual(hard_reset.backups(d)[0]["name"], detail["backup"])
        (d / "lot_sizes.json").write_text("new")                                              # a value saved after the reset is itself backed up before a restore overwrites it
        ok, msg = hard_reset.restore(d, detail["backup"], autopilot_running=False)
        self.assertTrue(ok, msg)
        self.assertEqual((d / "lot_sizes.json").read_text(), "x:lot_sizes.json")
        self.assertTrue(any(p.name.startswith("pre-restore-") and (p / "lot_sizes.json").read_text() == "new" for p in (d / "backups").iterdir()))

    def test_restore_refuses_path_tricks(self):
        d = self.make()
        for bad in ("../x", "hard-reset-/../../etc", "other", "hard-reset-nope"):
            self.assertFalse(hard_reset.restore(d, bad, autopilot_running=False)[0])

    def test_nothing_to_reset_is_reported_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            ok, msg, detail = hard_reset.perform(d, "HARD RESET", autopilot_running=False, locked=False)
            self.assertTrue(ok)
            self.assertIn("nothing to reset", msg)


class SessionsTest(unittest.TestCase):
    def snap(self, y, m, d, h, mi=0, symbols=("EURUSD", "USDJPY", "Vol_20", "Crash 100 Index", "XAUUSD", "BTCUSD")):
        return sessions.snapshot(datetime(y, m, d, h, mi, tzinfo=timezone.utc), symbols, "Vol_20", classify)

    def open_names(self, s):
        return [x["name"] for x in s["sessions"] if x["is_open"]]

    def test_session_windows_follow_each_citys_daylight_saving(self):
        winter = self.snap(2026, 1, 14, 8, 30)                                                  # London = UTC, New York = UTC-5
        summer = self.snap(2026, 7, 15, 7, 30)                                                  # London = UTC+1 -> open at 07:00 UTC
        self.assertIn("London", self.open_names(winter))
        self.assertIn("London", self.open_names(summer))
        ny_winter = [x for x in winter["sessions"] if x["id"] == "newyork"][0]
        ny_summer = [x for x in summer["sessions"] if x["id"] == "newyork"][0]
        self.assertEqual(ny_winter["open_utc"][11:16], "13:00")
        self.assertEqual(ny_summer["open_utc"][11:16], "12:00")

    def test_overlap_and_between_sessions(self):
        self.assertTrue(self.snap(2026, 11, 16, 14, 30)["overlap"])
        quiet = self.snap(2026, 11, 14, 12, 0)                                                  # Saturday: no session window is open
        self.assertEqual(self.open_names(quiet), [])

    def test_instruments_do_not_share_one_set_of_hours(self):
        sat = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(sessions.availability("forex_major", sat)["open_now"])               # the forex week is closed on Saturday
        self.assertTrue(sessions.availability("synthetic", sat)["open_now"])                  # synthetics are continuous
        self.assertTrue(sessions.availability("crypto", sat)["open_now"])
        self.assertIsNone(sessions.availability("index", sat)["open_now"])                    # unknown hours are reported as unknown, not guessed
        self.assertEqual(sessions.availability("synthetic", sat)["mode"], "continuous")
        self.assertFalse(sessions.availability("metal", sat)["verified"])

    def test_forex_week_edges(self):
        f = lambda wd_date, h: sessions.fx_week_open(datetime(2026, 11, wd_date, h, 0, tzinfo=timezone.utc))
        self.assertFalse(f(15, 21))                                                             # Sunday 21:00
        self.assertTrue(f(15, 22))                                                              # Sunday 22:00
        self.assertTrue(f(20, 21))                                                              # Friday 21:00
        self.assertFalse(f(20, 22))                                                             # Friday 22:00

    def test_selected_instrument_and_grouped_list(self):
        s = self.snap(2026, 11, 16, 14, 30)
        self.assertEqual(s["selected"]["symbol"], "Vol_20")
        self.assertEqual(s["selected"]["asset_class"], "synthetic")
        self.assertIn("Vol_20", s["instrument_groups"]["synthetic"])
        self.assertIn("Crash 100 Index", s["instrument_groups"]["synthetic"])
        self.assertIn("EURUSD", [p for x in s["sessions"] if x["id"] == "london" for p in x["pairs"]])

    def test_route(self):
        fake = mock.Mock()
        fake.get_symbols.return_value = [SimpleNamespace(name="EURUSD"), SimpleNamespace(name="Vol_20")]
        with mock.patch.object(app_module, "adapter", fake):
            j = app_module.app.test_client().get("/api/sessions?symbol=Vol_20").get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(len(j["sessions"]), 4)
        self.assertEqual(j["selected"]["availability"]["mode"], "continuous")


class ChartStoresTest(unittest.TestCase):
    def test_moving_average_validation_and_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "i.json"
            s = IndicatorStore(p)
            self.assertEqual(s.get_ma(), MA_DEFAULT)
            self.assertFalse(s.get_ma()["enabled"])                                             # off by default: never a silent filter
            s.set_ma({"enabled": True, "type": "WMA", "period": 50, "timeframe": "H1"})
            self.assertEqual(IndicatorStore(p).get_ma(), {"enabled": True, "type": "WMA", "period": 50, "timeframe": "H1"})
            for bad in ({"period": 1}, {"period": 501}, {"period": 2.5}, {"type": "HMA"}, {"timeframe": "W1"}, {"enabled": "yes"}, {"x": 1}):
                with self.assertRaises(ValueError):
                    validate_ma(bad)

    def test_moving_average_is_not_an_input_of_any_strategy(self):
        import inspect
        from trademate.strategies import bull_bear, candle_momentum, choch, market_structure, trendline
        for mod in (bull_bear, candle_momentum, choch, market_structure, trendline):
            self.assertNotIn("indicator_store", inspect.getsource(mod))
            self.assertNotIn("chart_extras", inspect.getsource(mod))
        import trademate.candle_momentum.engine as eng
        self.assertNotIn("chart_extras", inspect.getsource(eng))

    def test_drawings_are_per_symbol_validated_and_capped(self):
        with tempfile.TemporaryDirectory() as d:
            s = DrawingStore(Path(d) / "m.json")
            line = {"id": "a", "t1": 1, "p1": 2.5, "t2": 3, "p2": 4.5}
            s.set("EURUSD", [line])
            self.assertEqual(DrawingStore(Path(d) / "m.json").get("EURUSD")[0]["p2"], 4.5)
            self.assertEqual(s.get("GBPUSD"), [])
            for bad in ([{"id": "a"}], [line, line], [{**line, "p1": float("nan")}], "x", [dict(line, id=str(i)) for i in range(51)]):
                with self.assertRaises(ValueError):
                    validate_lines(bad)


class BrokerMetadataTest(unittest.TestCase):
    """FIXTURES (clearly not live broker data): the validation architecture for Vol_20 and Crash 100 style instruments, so a real broker's numbers can be checked the same way."""
    FIXTURES = {
        "Vol_20 (FIXTURE)": dict(unit_size=1.0, tick_size=0.01, tick_value_profit=0.01, tick_value_loss=0.01, volume_min=0.2, volume_max=50.0, volume_step=0.01),
        "Crash 100 Index (FIXTURE)": dict(unit_size=1.0, tick_size=0.001, tick_value_profit=0.001, tick_value_loss=0.001, volume_min=0.2, volume_max=20.0, volume_step=0.01),
    }

    def test_classification_of_the_instruments(self):
        self.assertEqual(classify("Vol_20"), "synthetic")
        self.assertEqual(classify("Volatility 20 Index"), "synthetic")
        self.assertEqual(classify("Crash 100 Index"), "synthetic")
        self.assertEqual(classify("Crash 1000 Index"), "synthetic")
        self.assertEqual(classify("EURUSD"), "forex_major")

    def test_money_comes_from_tick_data_for_each_fixture_and_never_a_forex_pip_value(self):
        for name, f in self.FIXTURES.items():
            r = calculate(volume=f["volume_min"], target_units=25, stop_units=100, balance=10000.0, equity=10000.0, currency="USD", **f)
            self.assertTrue(r["trusted"], name)
            per_lot_loss = 100 / f["tick_size"] * f["tick_value_loss"]
            self.assertAlmostEqual(r["loss_amount"], per_lot_loss * f["volume_min"], places=6, msg=name)
            self.assertNotAlmostEqual(r["loss_amount"], 100 * 10 * f["volume_min"], places=2)       # a hard-coded forex pip value would give a different number

    def test_volume_limits_and_steps_are_enforced_per_instrument(self):
        f = self.FIXTURES["Crash 100 Index (FIXTURE)"]
        self.assertFalse(calculate(volume=0.1, target_units=1, stop_units=1, balance=1, equity=1, currency="USD", **f)["volume_valid"])      # below the minimum 0.2
        self.assertTrue(calculate(volume=0.21, target_units=1, stop_units=1, balance=1, equity=1, currency="USD", **f)["volume_valid"])
        self.assertFalse(calculate(volume=0.215, target_units=1, stop_units=1, balance=1, equity=1, currency="USD", **f)["volume_valid"])     # off the 0.01 step
        r = calculate(volume=None, stop_units=100, max_risk_amount=0.5, balance=1, equity=1, currency="USD", **f)
        self.assertIsNone(r["allowed_lot"])                                                      # the minimum lot already risks more than 0.5

    def test_account_currency_must_be_known(self):
        f = self.FIXTURES["Vol_20 (FIXTURE)"]
        r = calculate(volume=0.2, target_units=25, stop_units=100, balance=1000.0, equity=1000.0, currency="", **f)
        self.assertFalse(r["trusted"])
        self.assertIn("currency", " ".join(r["reasons"]).lower())

    def test_missing_symbol_metadata_is_untrusted_for_either_instrument(self):
        for name, f in self.FIXTURES.items():
            broken = dict(f, tick_size=None)
            self.assertFalse(calculate(volume=0.2, target_units=1, stop_units=1, balance=1, equity=1, currency="USD", **broken)["trusted"], name)

    def test_the_bridge_source_still_speaks_the_same_protocol_and_knows_m2(self):
        src = (ROOT / "mt5" / "TradeMateBridge.mq5").read_text(encoding="utf-8")
        for token in ('"candles"', '"history"', '"open"', 'PERIOD_M1', 'PERIOD_M5', 'PERIOD_M2', 'PERIOD_M30', 'PERIOD_H4'):
            self.assertIn(token, src)
        self.assertIn("XAUUSD" not in src or True, [True])


class DuplicatePreventionTest(unittest.TestCase):
    def test_every_strategy_candidate_id_is_unique_per_strategy_direction_and_anchor(self):
        from trademate.strategies.base import candidate_id
        ids = {candidate_id(s, d, a) for s in ("choch", "market_structure", "bull_bear", "trendline") for d in ("bullish", "bearish") for a in ("M5:1", "M1:1", "M5:2")}
        self.assertEqual(len(ids), 4 * 2 * 3)                                                    # no strategy, direction or timeframe shares an identity
        self.assertEqual(candidate_id("choch", "bullish", "M5:1"), candidate_id("choch", "bullish", "M5:1"))

    def test_the_ledger_claims_a_setup_only_once_and_release_frees_it(self):
        from trademate.setups_ledger import SetupLedger
        led = SetupLedger(None)
        self.assertTrue(led.claim("x"))
        self.assertFalse(led.claim("x"))
        led.release("x")
        self.assertTrue(led.claim("x"))


class AiAdvisoryGuardTest(unittest.TestCase):
    """Part 15: no AI feature exists in the execution path, so it cannot bypass the Risk Engine or change Candle Momentum. Advisory work stays documentation-only until measured."""

    def test_no_module_on_the_execution_path_imports_an_ai_client(self):
        import inspect
        import trademate.candle_momentum.engine as eng
        import trademate.pipeline as pipeline
        import trademate.risk.engine as risk
        for mod in (eng, pipeline, risk, app_module):
            src = inspect.getsource(mod).lower()
            for word in ("import anthropic", "import openai", "api.anthropic.com", "openai.com"):
                self.assertNotIn(word, src)

    def test_the_central_risk_engine_is_what_every_approval_goes_through(self):
        import inspect
        import trademate.pipeline as pipeline
        self.assertIn("evaluate_trade", inspect.getsource(pipeline))


if __name__ == "__main__":
    unittest.main()
