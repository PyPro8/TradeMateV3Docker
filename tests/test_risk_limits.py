"""Phase 5 / Risk Engine v5.2 tests: limits and protection. No order is ever placed.

Part 1: the 7 controlled scenarios (trademate.risk.scenarios) + edge cases on the pure engine.
Part 2: how MT5 data becomes risk state (daily loss, active risk, server day).
Part 3: rules (any value allowed, saved locally) and the Flask routes against tests/fake_bridge.py.
Run:  python -m unittest tests.test_risk_limits -v
"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.fake_bridge import FakeBridge
from trademate.broker import create_adapter
from trademate.broker.base import AccountInfo, ClosedTrade, Position
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.risk import (RiskRules, RiskRulesStore, TradeProposal, evaluate_trade, parse_rules, position_risk)
from trademate.risk import scenarios as sc
from trademate.risk.account_state import build_state, day_start_from_offset

R1 = RiskRules(1.0, 3.0, 3.0, 3)


def decide(state=None, risk=1.0, rules=R1, **kw):
    return evaluate_trade(state or sc.make_state(), kw.pop("proposal", sc.proposal(risk)), sc.GOLD, rules, **kw)


class ScenarioTest(unittest.TestCase):
    def test_all_seven_controlled_scenarios(self):
        results = sc.run()
        self.assertEqual(len(results), 8)
        for scenario, decision, ok in results:
            self.assertTrue(ok, f"{scenario.name}: {decision.decision} / {decision.rejection_reason}")

    def test_1_approved_has_volume_and_exact_risk(self):
        d = decide()                                           # 1% of $52,000 = $520 on a 10.00 stop = 0.52 lots
        self.assertEqual((d.decision, d.approved_volume), ("ACCEPTED", 0.52))
        self.assertAlmostEqual(d.trade_risk_amount, 520.0)
        self.assertFalse(d.trading_locked)
        self.assertTrue(all(c["passed"] for c in d.checks))

    def test_2_over_per_trade_limit(self):
        d = decide(risk=1.5)
        self.assertEqual(d.decision, "REJECTED")
        self.assertTrue(d.rejection_reason.startswith("Trade exceeds maximum risk per trade."))
        self.assertIsNone(d.approved_volume)

    def test_3_daily_limit_reached_locks_trading(self):
        d = decide(sc.make_state(lost_percent=3.0), risk=0.5)
        self.assertEqual(d.decision, "REJECTED")
        self.assertTrue(d.trading_locked)
        self.assertTrue(d.rejection_reason.startswith("Daily loss limit reached."))

    def test_daily_limit_exceeded_also_locks(self):
        self.assertTrue(decide(sc.make_state(lost_percent=4.5), risk=0.1).trading_locked)

    def test_4_remaining_daily_is_the_stricter_limit(self):
        rules = RiskRules(2.0, 3.0, 10.0, 3)                   # per-trade allows 2%, but only 1% of the day is left
        d = decide(sc.make_state(lost_percent=2.0), risk=1.5, rules=rules)
        self.assertEqual(d.decision, "REJECTED")
        self.assertIn("remaining daily", d.rejection_reason.lower())
        self.assertEqual(d.limiting_rule, "Remaining daily loss")
        self.assertAlmostEqual(d.effective_max_risk_amount, 520.0)       # 3% - 2% of 52,000
        self.assertFalse(d.trading_locked)

    def test_4_smaller_trade_still_eligible_with_partial_allowance(self):
        self.assertEqual(decide(sc.make_state(lost_percent=2.0), risk=0.5).decision, "ACCEPTED")

    def test_effective_max_is_the_strictest_of_all_rules(self):
        d = decide(sc.make_state(positions=[sc.pos(1, 1040)]), risk=0.5)   # 2% active, limit 3% -> 1% left; per-trade 1%
        self.assertAlmostEqual(d.effective_max_risk_percent, 1.0)
        d = decide(sc.make_state(positions=[sc.pos(1, 1300)]), risk=0.5)   # 2.5% active -> 0.5% left < per-trade 1%
        self.assertEqual(d.limiting_rule, "Remaining active risk")
        self.assertAlmostEqual(d.effective_max_risk_percent, 0.5)
        self.assertEqual(d.decision, "ACCEPTED")                          # exactly at the limit is allowed

    def test_5_position_limit_reason_has_counts(self):
        d = decide(sc.make_state(positions=[sc.pos(i, 100) for i in (1, 2, 3)]))
        self.assertIn("Current: 3", d.rejection_reason)
        self.assertIn("Maximum: 3", d.rejection_reason)
        self.assertEqual(decide(sc.make_state(positions=[sc.pos(1, 100), sc.pos(2, 100)])).decision, "ACCEPTED")

    def test_6_total_active_risk(self):
        d = decide(sc.make_state(positions=[sc.pos(1, 650), sc.pos(2, 650)]))
        self.assertEqual(d.decision, "REJECTED")
        self.assertIn("2.50%", d.rejection_reason)
        self.assertIn("3.50%", d.rejection_reason)

    def test_raising_the_limits_changes_the_outcome(self):
        state = sc.make_state(positions=[sc.pos(1, 650), sc.pos(2, 650)])
        self.assertEqual(decide(state).decision, "REJECTED")
        self.assertEqual(decide(state, rules=RiskRules(1.0, 3.0, 5.0, 3)).decision, "ACCEPTED")

    def test_position_without_stop_loss_blocks_with_a_clear_reason(self):
        d = decide(sc.make_state(positions=[sc.pos(7, 300, sl=None)]))
        self.assertEqual(d.decision, "REJECTED")
        self.assertIn("#7", d.rejection_reason)
        self.assertIn("no stop-loss", d.rejection_reason)

    def test_unknown_daily_loss_rejects(self):
        d = decide(sc.make_state(history=False))
        self.assertEqual(d.decision, "REJECTED")
        self.assertIn("cannot be verified", d.rejection_reason)

    def test_broker_constraint_is_the_last_check(self):
        d = decide(risk=0.0001)                                # below the broker minimum volume
        self.assertEqual(d.decision, "REJECTED")
        self.assertIn("below the broker minimum", d.rejection_reason)
        self.assertEqual([c["name"] for c in d.checks][-1], "Broker constraints")

    def test_invalid_buy_sl_is_rejected_by_the_decision(self):
        p = TradeProposal("XAUUSD", "buy", 2650.0, 2660.0, None, 1.0)
        self.assertIn("BELOW the entry", decide(proposal=p).rejection_reason)

    def test_missing_sl_or_risk_rejected(self):
        self.assertEqual(decide(proposal=TradeProposal("XAUUSD", "buy", 2650.0, None, None, 1.0)).decision, "REJECTED")
        self.assertEqual(decide(proposal=TradeProposal("XAUUSD", "buy", 2650.0, 2640.0, None, None)).decision, "REJECTED")

    def test_trade_risk_from_a_given_volume_uses_mt5_tick_data(self):
        p = TradeProposal("XAUUSD", "buy", 2650.0, 2640.0, None, None, volume=0.52)   # $1000/lot -> $520
        d = decide(proposal=p)
        self.assertEqual(d.decision, "ACCEPTED")
        self.assertAlmostEqual(d.trade_risk_amount, 520.0)
        d = decide(proposal=replace(p, volume=0.80))                                    # $800 = 1.54% > 1%
        self.assertIn("maximum risk per trade", d.rejection_reason)
        d = decide(proposal=replace(p, volume=0.525), rules=RiskRules(5.0, 3.0, 9.0, 3))
        self.assertIn("volume step", d.rejection_reason)

    def test_no_hardcoded_instrument_values(self):            # a different tick value gives a different size
        from trademate.risk import SymbolSpec
        cheap = SymbolSpec("IDX", 0.1, 0.1, 0.1, 0.1, 100.0, 0.1, 4)     # $0.1 per 0.1 tick -> 10.00 = $10/lot
        d = evaluate_trade(sc.make_state(), TradeProposal("IDX", "buy", 100.0, 90.0, None, 1.0), cheap, R1)
        self.assertEqual((d.decision, d.approved_volume), ("ACCEPTED", 52.0))   # $520 / ($10 per lot), not gold's $1000

    def test_decision_is_json_ready(self):
        json.dumps(decide().to_dict())


class PositionRiskTest(unittest.TestCase):
    def test_risk_is_measured_from_open_price_to_stop(self):
        self.assertAlmostEqual(position_risk(1, "XAUUSD", "buy", 0.5, 2650.0, 2640.0, sc.GOLD).risk_amount, 500.0)
        self.assertAlmostEqual(position_risk(1, "XAUUSD", "sell", 2.0, 2650.0, 2655.0, sc.GOLD).risk_amount, 1000.0)

    def test_stop_at_or_beyond_break_even_is_zero_risk(self):
        self.assertEqual(position_risk(1, "XAUUSD", "buy", 1.0, 2650.0, 2650.0, sc.GOLD).risk_amount, 0.0)
        self.assertEqual(position_risk(1, "XAUUSD", "buy", 1.0, 2650.0, 2660.0, sc.GOLD).risk_amount, 0.0)

    def test_positions_differ(self):                          # not assumed identical
        a = position_risk(1, "XAUUSD", "buy", 1.0, 2650.0, 2640.0, sc.GOLD).risk_amount
        b = position_risk(2, "XAUUSD", "buy", 0.1, 2650.0, 2640.0, sc.GOLD).risk_amount
        self.assertNotEqual(a, b)

    def test_cannot_calculate_without_stop_or_tick_data(self):
        self.assertIsNone(position_risk(1, "XAUUSD", "buy", 1.0, 2650.0, None, sc.GOLD).risk_amount)
        self.assertIsNone(position_risk(1, "XAUUSD", "buy", 1.0, 2650.0, 2640.0, None).risk_amount)


def account(balance=10000.0, equity=10000.0):
    return AccountInfo(1, "srv", "b", "demo", "USD", 100, balance, equity, 0.0, equity, None, 0.0)


def closed(net, time_close):
    return ClosedTrade(1, "EURUSD", "buy", 0.1, 1.1, 1.09, None, None, net, 0.0, 0.0, net, time_close - 60, time_close, "client")


class StateTest(unittest.TestCase):
    DAY = 1_800_000_000 - 1_800_000_000 % 86400                # an arbitrary day boundary

    def test_daily_loss_from_closed_trades_and_floating(self):
        # balance now 9,700 after closing -300 today; floating -100 -> equity 9,600; day started at 10,000
        pos = [Position(5, "EURUSD", "buy", 0.1, 1.1, 1.09, 1.08, None, -100.0, 0.0)]
        st = build_state(account(9700.0, 9600.0), pos, {}, [closed(-300.0, self.DAY + 3600), closed(+999.0, self.DAY - 3600)],
                         self.DAY, "test")
        self.assertAlmostEqual(st.start_of_day_balance, 10000.0)
        self.assertAlmostEqual(st.todays_closed_pl, -300.0)    # yesterday's trade is not counted
        s = __import__("trademate.risk", fromlist=["summarize_state"]).summarize_state(st, R1)
        self.assertAlmostEqual(s["todays_loss"], 400.0)
        self.assertAlmostEqual(s["todays_loss_percent"], 4.0)
        self.assertTrue(s["daily_locked"])                    # 4% >= 3%
        self.assertAlmostEqual(s["daily_remaining"], 0.0)

    def test_profit_today_means_no_loss(self):
        st = build_state(account(10500.0, 10500.0), [], {}, [closed(+500.0, self.DAY + 10)], self.DAY, "test")
        from trademate.risk import summarize_state
        s = summarize_state(st, R1)
        self.assertEqual(s["todays_loss"], 0.0)
        self.assertAlmostEqual(s["daily_remaining"], 300.0)   # 3% of the 10,000 start-of-day balance
        self.assertFalse(s["daily_locked"])

    def test_history_unavailable_means_unknown_not_zero(self):
        st = build_state(account(), [], {}, None, self.DAY, "test", "no history")
        self.assertIsNone(st.todays_closed_pl)
        from trademate.risk import summarize_state
        self.assertIsNone(summarize_state(st, R1)["todays_loss"])

    def test_server_day_start(self):
        now = 1_800_000_000                                    # a UTC instant
        utc_start = now - now % 86400
        self.assertEqual(day_start_from_offset(now, 0), utc_start)
        # a server at UTC+3 started its day 3h before UTC midnight (when UTC time of day is >= 21:00 it is already next day)
        off = 3 * 3600
        self.assertEqual(day_start_from_offset(now, off) % 86400, 0)          # a day boundary on the server's clock
        self.assertLessEqual(day_start_from_offset(now, off), now + off)
        self.assertGreater(day_start_from_offset(now, off) + 86400, now + off)


class RulesTest(unittest.TestCase):
    def test_any_positive_value_up_to_100_is_allowed(self):
        r = parse_rules({"max_risk_per_trade_percent": "7.5", "max_daily_loss_percent": 50, "max_total_active_risk_percent": 100,
                         "max_positions": 25}, R1)
        self.assertEqual((r.max_risk_per_trade_percent, r.max_daily_loss_percent, r.max_total_active_risk_percent, r.max_positions),
                         (7.5, 50.0, 100.0, 25))

    def test_invalid_values_rejected_with_a_message(self):
        for bad in ({"max_risk_per_trade_percent": 0}, {"max_risk_per_trade_percent": -1}, {"max_daily_loss_percent": 101},
                    {"max_total_active_risk_percent": "abc"}, {"max_positions": 0}, {"max_positions": 2.5},
                    {"max_positions": True}, {"max_risk_per_trade_percent": float("nan")}):
            with self.assertRaises(ValueError, msg=str(bad)):
                parse_rules(bad, R1)

    def test_store_round_trip_and_bad_file_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RiskRulesStore(Path(tmp) / "r.json", R1)
            self.assertEqual(store.load(), R1)
            store.save(RiskRules(5.0, 10.0, 20.0, 8))
            self.assertEqual(store.load(), RiskRules(5.0, 10.0, 20.0, 8))
            (Path(tmp) / "r.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(store.load(), R1)

    def test_higher_than_the_old_2_percent_cap_now_works(self):
        d = decide(risk=4.0, rules=RiskRules(5.0, 10.0, 20.0, 3))
        self.assertEqual(d.decision, "ACCEPTED")


class RiskRoutesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bridge = FakeBridge(Path(tmp.name)).start()
        self.addCleanup(self.bridge.stop)
        settings = Settings(app=AppSettings(data_dir=Path(tmp.name)), mt5=Mt5Settings(data_dir=Path(tmp.name)),
                            trading=TradingSettings(command_timeout_seconds=2.0, confirm_timeout_seconds=1.5))
        self.store = RiskRulesStore(Path(tmp.name) / "risk_rules.json", RiskRules(1.0, 3.0, 3.0, 3))
        for patcher in (mock.patch.object(app_module, "adapter", create_adapter(settings)),
                        mock.patch.object(app_module, "risk_store", self.store)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = app.test_client()

    def evaluate(self, **kw):
        body = {"symbol": "EURUSD", "direction": "buy", "entry": 1.1000, "stop_loss": 1.0950, "risk_percent": 1.0, **kw}
        res = self.client.post("/api/risk/evaluate", json=body).get_json()
        self.assertTrue(res["ok"], res)
        return res["decision"]

    def test_state_reports_rules_and_live_account(self):
        res = self.client.get("/api/risk/state").get_json()
        self.assertTrue(res["ok"])
        s = res["state"]
        self.assertEqual((s["equity"], s["balance"], s["open_positions"]), (1000.0, 1000.0, 0))
        self.assertEqual(s["rules"]["max_positions"], 3)
        self.assertEqual(s["todays_loss"], 0.0)               # nothing closed today, nothing floating
        self.assertAlmostEqual(s["daily_remaining"], 30.0)    # 3% of 1000

    def test_approved_through_the_route(self):
        d = self.evaluate()                                    # $10 risk on 50 pips = $500/lot -> 0.02 lots
        self.assertEqual((d["decision"], d["approved_volume"]), ("ACCEPTED", 0.02))

    def test_rejected_over_per_trade_limit(self):
        d = self.evaluate(risk_percent=1.5)
        self.assertEqual(d["decision"], "REJECTED")
        self.assertIn("maximum risk per trade", d["rejection_reason"])

    def test_open_positions_come_from_mt5_and_count_toward_the_limit(self):
        adapter = app_module.adapter
        for _ in range(3):
            self.assertTrue(adapter.place_order("EURUSD", "buy", 0.01, 1.0950, 1.1050).confirmed)   # via the FIXTURE bridge only
        s = self.client.get("/api/risk/state").get_json()["state"]
        self.assertEqual(s["open_positions"], 3)
        self.assertAlmostEqual(s["active_risk"], 3 * 5.12, places=4)   # buy fills at the ask 1.10012; SL 1.0950 = 512 ticks x $1 x 0.01 lot
        d = self.evaluate()
        self.assertEqual(d["decision"], "REJECTED")
        self.assertIn("Maximum simultaneous position limit reached", d["rejection_reason"])
        self.assertIn("Current: 3", d["rejection_reason"])

    def test_position_without_sl_blocks_new_trades(self):
        self.assertTrue(app_module.adapter.place_order("EURUSD", "buy", 0.01).confirmed)
        d = self.evaluate()
        self.assertEqual(d["decision"], "REJECTED")
        self.assertIn("no stop-loss", d["rejection_reason"])

    def test_closed_loss_today_is_counted_and_locks(self):
        adapter = app_module.adapter
        self.assertTrue(adapter.place_order("EURUSD", "buy", 1.0, 1.0, 1.2).confirmed)           # fixture only
        ticket = adapter.get_positions()[0].ticket
        self.bridge.bid = 1.0990                                                                  # price falls: a real loss
        self.assertTrue(adapter.close_position(ticket).confirmed)
        s = self.client.get("/api/risk/state").get_json()["state"]
        self.assertLess(s["todays_closed_pl"], 0)
        self.assertIsNotNone(s["todays_loss"])

    def test_user_can_raise_limits_to_any_value(self):
        res = self.client.post("/api/risk/rules", json={"max_risk_per_trade_percent": 10, "max_daily_loss_percent": 40,
                                                        "max_total_active_risk_percent": 60, "max_positions": 12}).get_json()
        self.assertTrue(res["ok"])
        self.assertEqual(self.store.load(), RiskRules(10.0, 40.0, 60.0, 12))
        self.assertEqual(self.evaluate(risk_percent=8.0)["decision"], "ACCEPTED")                # far above the old 2% cap

    def test_invalid_rules_rejected_and_not_saved(self):
        r = self.client.post("/api/risk/rules", json={"max_daily_loss_percent": 0})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(self.store.load().max_daily_loss_percent, 3.0)

    def test_rules_endpoint_blocks_cross_origin(self):
        self.assertEqual(self.client.post("/api/risk/rules", json={}, headers={"Origin": "http://evil.example"}).status_code, 400)
        self.assertEqual(self.client.post("/api/risk/evaluate", json={}, headers={"Origin": "http://evil.example"}).status_code, 400)

    def test_unknown_symbol_gives_a_clear_reason(self):
        d = self.evaluate(symbol="NOPE")
        self.assertEqual(d["decision"], "REJECTED")
        self.assertIn("NOPE", d["rejection_reason"])

    def test_evaluating_never_sends_a_trade_command(self):
        before = len(self.bridge.commands_seen)
        self.evaluate(); self.client.get("/api/risk/state")
        sent = [c["cmd"] for c in self.bridge.commands_seen[before:]]
        self.assertTrue(set(sent) <= {"symbol_info", "candles", "history"}, sent)


if __name__ == "__main__":
    unittest.main()
