"""Phase 5 completion tests (5.1 sizing audit, 5.2 all checks shown, 5.3 execution-ready result, 5.4 live positions).
No order is ever sent by the Risk Engine; the fake bridge is only used to stand in for MT5's open positions."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.fake_bridge import FakeBridge
from trademate.broker import create_adapter
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.risk import RiskRules, RiskRulesStore, SymbolSpec, TradeProposal, evaluate_trade
from trademate.risk import scenarios as sc

ROOT = Path(__file__).resolve().parent.parent


def decide(state=None, risk=1.0, rules=RiskRules(3.0, 3.0, 5.0, 3), spec=sc.GOLD, proposal=None):
    return evaluate_trade(state or sc.make_state(), proposal or sc.proposal(risk), spec, rules)


def status(d, name):
    return next(c for c in d.checks if c["name"] == name)["status"]


from trademate.risk import engine as _risk_engine
def central():
    """The central protections app.py installs (absent in a bare Risk Engine); evaluated when the test runs."""
    return ["Emergency lock", "Consecutive losses"] if _risk_engine._PROTECTIONS["provider"] else []


class RiskLimitTests(unittest.TestCase):
    def test_1_four_percent_vs_three_percent_rejected(self):
        d = decide(risk=4.0)
        self.assertEqual(d.decision, "REJECTED")
        self.assertEqual(status(d, "Risk per trade"), "FAIL")

    def test_2_exactly_three_percent_passes_the_per_trade_check(self):
        d = decide(risk=3.0)
        self.assertEqual(status(d, "Risk per trade"), "PASS")
        self.assertEqual(d.decision, "ACCEPTED")

    def test_3_too_many_positions_rejected(self):
        d = decide(sc.make_state(positions=[sc.pos(i, 100) for i in range(1, 4)]))
        self.assertEqual((d.decision, status(d, "Simultaneous positions")), ("REJECTED", "FAIL"))

    def test_4_total_active_risk_above_5_percent_rejected(self):
        d = decide(sc.make_state(positions=[sc.pos(1, 1300), sc.pos(2, 1300)]), risk=1.0)   # 5% + 1%
        self.assertEqual((d.decision, status(d, "Total active risk")), ("REJECTED", "FAIL"))
        self.assertIn("6.00%", d.rejection_reason)

    def test_all_failures_are_reported_not_just_the_first(self):
        d = decide(sc.make_state(positions=[sc.pos(1, 1300), sc.pos(2, 1300), sc.pos(3, 1300)]), risk=1.0)
        failed = {c["name"] for c in d.checks if c["status"] == "FAIL"}
        self.assertEqual(failed, {"Simultaneous positions", "Total active risk"})
        self.assertEqual(len(d.rejection_reasons), 2)
        self.assertEqual(len(d.checks), 7 + len(central()))                                  # the seven rules, plus the central protections when the application has installed them

    def test_every_check_listed_with_pass_fail(self):
        d = decide()
        self.assertEqual([c["name"] for c in d.checks], ["Trade parameters", "Daily loss limit", "Risk per trade",
                         "Remaining daily risk", "Simultaneous positions", "Total active risk", *central(), "Broker constraints"])
        self.assertTrue(all(c["status"] in ("PASS", "FAIL") for c in d.checks))

    def test_limits_are_independent(self):
        # remaining daily fails while per-trade, positions and total pass
        d = decide(sc.make_state(lost_percent=2.5), risk=1.0, rules=RiskRules(3.0, 3.0, 5.0, 3))
        self.assertEqual((status(d, "Risk per trade"), status(d, "Remaining daily risk"),
                          status(d, "Simultaneous positions"), status(d, "Total active risk")), ("PASS", "FAIL", "PASS", "PASS"))


class SizingTests(unittest.TestCase):
    def test_5_volume_rounded_down_and_risk_matches_rounded_volume(self):
        spec = SymbolSpec("XAUUSD", 0.01, 1.0, 1.0, 0.01, 50.0, 0.1, 4)           # step 0.1, min 0.01 -> grid 0.01, 0.11, ...
        d = decide(spec=spec, proposal=TradeProposal("XAUUSD", "buy", 2650.0, 2640.0, 2670.0, 1.0))   # ideal 0.52
        self.assertAlmostEqual(d.ideal_volume, 0.52)
        self.assertAlmostEqual(d.broker_valid_volume, 0.51)
        self.assertAlmostEqual(d.approved_volume, 0.51)
        self.assertAlmostEqual(d.trade_risk_amount, 510.0)                            # risk of the ROUNDED volume
        self.assertAlmostEqual(d.requested_risk_amount, 520.0)
        self.assertLessEqual(d.trade_risk_amount, d.requested_risk_amount)

    def test_5_sell_and_other_symbols_size_correctly(self):
        d = decide(proposal=sc.proposal(1.0, "sell"))
        self.assertEqual(d.approved_volume, 0.52)
        eur = SymbolSpec("EURUSD", 1e-5, 1.0, 1.0, 0.01, 100.0, 0.01, 4)
        d = decide(spec=eur, proposal=TradeProposal("EURUSD", "sell", 1.1000, 1.1050, 1.0900, 1.0))   # $520 / $500 per lot
        self.assertEqual(d.approved_volume, 1.04)

    def test_6_below_minimum_rejected_never_raised(self):
        spec = SymbolSpec("XAUUSD", 0.01, 1.0, 1.0, 1.0, 50.0, 1.0, 4)               # min 1 lot; ideal 0.52
        d = decide(spec=spec)
        self.assertEqual(d.decision, "REJECTED")
        self.assertEqual(status(d, "Broker constraints"), "FAIL")
        self.assertIsNone(d.approved_volume)
        self.assertAlmostEqual(d.ideal_volume, 0.52)

    def test_sizing_cannot_bypass_a_limit(self):
        d = decide(risk=4.0)                                   # perfectly valid volume, but over the limit
        self.assertIsNotNone(d.broker_valid_volume)
        self.assertIsNone(d.approved_volume)
        self.assertFalse(d.ready_for_execution)

    def test_invalid_inputs_fail_safe(self):
        for p in (TradeProposal("XAUUSD", "buy", 2650.0, 2650.0, None, 1.0),             # zero distance
                  TradeProposal("XAUUSD", "buy", 2650.0, 2640.0, 2640.0, 1.0),           # TP on the wrong side
                  TradeProposal("XAUUSD", "sell", 2650.0, 2640.0, None, 1.0),            # SL on the wrong side
                  TradeProposal("XAUUSD", "buy", None, 2640.0, None, 1.0),
                  TradeProposal("XAUUSD", "buy", 2650.0, 2640.0, None, None)):
            d = decide(proposal=p)
            self.assertEqual(d.decision, "REJECTED", p)
            self.assertIsNone(d.approved_volume)
            self.assertEqual(status(d, "Trade parameters"), "FAIL")
        self.assertEqual(decide(spec=None).decision, "REJECTED")
        self.assertEqual(decide(spec=SymbolSpec("X", None, 1.0, 1.0, 0.01, 1.0, 0.01)).decision, "REJECTED")

    def test_accepted_result_is_execution_ready_and_sends_nothing(self):
        d = decide()
        self.assertTrue(d.ready_for_execution)
        self.assertFalse(d.order_sent)
        t = d.trade
        for key in ("symbol", "direction", "entry", "stop_loss", "take_profit", "risk_percent", "risk_amount",
                    "ideal_volume", "broker_valid_volume", "approved_volume", "account_equity_used", "limits", "checks",
                    "decision", "rejection_reasons"):
            self.assertIn(key, t)
        self.assertEqual((t["decision"], t["approved_volume"], t["take_profit"], t["account_equity_used"]), ("ACCEPTED", 0.52, 2670.0, 52000.0))


class NoOrderGuaranteeTests(unittest.TestCase):
    def test_risk_package_has_no_order_capability(self):
        for f in (ROOT / "trademate" / "risk").glob("*.py"):
            src = re.sub(r'""".*?"""|#.*', "", f.read_text(encoding="utf-8"), flags=re.S)
            self.assertIsNone(re.search(r"place_order|close_position|modify_position|order_send|\.execute", src), f.name)

    def test_risk_routes_never_call_write_methods(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        block = src[src.index('@app.route("/api/risk/size"'):src.index('@app.route("/api/history")')]
        self.assertIsNone(re.search(r"place_order|close_position|modify|_execute|order_send", block))

    def test_ui_has_no_execute_button_for_accepted_trades(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="risk-calc"'):]
        panel = panel[:panel.index("</section>")]
        self.assertNotRegex(panel.lower(), r"place order|execute|send order|>buy<.*button")
        js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        ev = js[js.index("function riskEvaluate"):js.index("function riskSaveRules")]
        self.assertNotIn("/api/order", ev)
        self.assertNotIn("placeOrder", ev)


class LivePositionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bridge = FakeBridge(Path(tmp.name)).start()
        self.addCleanup(self.bridge.stop)
        settings = Settings(app=AppSettings(data_dir=Path(tmp.name)), mt5=Mt5Settings(data_dir=Path(tmp.name)),
                            trading=TradingSettings(command_timeout_seconds=2.0, confirm_timeout_seconds=1.5))
        store = RiskRulesStore(Path(tmp.name) / "r.json", RiskRules(3.0, 3.0, 50.0, 10))
        self.adapter = create_adapter(settings)
        for p in (mock.patch.object(app_module, "adapter", self.adapter), mock.patch.object(app_module, "risk_store", store)):
            p.start()
            self.addCleanup(p.stop)
        self.client = app.test_client()

    def state(self):
        return self.client.get("/api/risk/state").get_json()["state"]

    def test_11_position_opened_in_mt5_is_detected_once(self):
        self.assertEqual(self.state()["open_positions"], 0)
        self.adapter.place_order("EURUSD", "buy", 0.10, 1.0950, 1.1100)     # stands in for a manual MT5 trade
        s = self.state()
        self.assertEqual(s["open_positions"], 1)
        again = self.state()
        self.assertEqual(again["open_positions"], 1)                        # polling twice never duplicates
        p = s["positions"][0]
        self.assertEqual((p["symbol"], p["direction"], p["volume"], p["stop_loss"], p["take_profit"]),
                         ("EURUSD", "buy", 0.10, 1.0950, 1.1100))
        self.assertIsNotNone(p["floating_pl"])
        self.assertIsNotNone(p["risk_amount"])

    def test_floating_pl_and_risk_at_sl_are_different_values(self):
        self.adapter.place_order("EURUSD", "buy", 0.10, 1.0950, 1.1100)
        p = self.state()["positions"][0]
        self.assertNotAlmostEqual(p["floating_pl"], p["risk_amount"])
        self.assertAlmostEqual(p["risk_amount"], 0.10 * 512, places=4)      # 512 ticks x $1 x 0.10 lot (open 1.10012)
        self.assertGreater(p["risk_amount"], 0)

    def test_sl_and_volume_changes_update_risk(self):
        self.adapter.place_order("EURUSD", "buy", 0.10, 1.0950, 1.1100)
        before = self.state()["active_risk"]
        ticket = self.adapter.get_positions()[0].ticket
        self.bridge.positions[ticket]["sl"] = 1.0990                          # SL tightened in MT5
        self.bridge.write_snapshot()
        tighter = self.state()["active_risk"]
        self.assertLess(tighter, before)
        self.bridge.positions[ticket]["volume"] = 0.20                        # volume changed in MT5
        self.bridge.write_snapshot()
        self.assertAlmostEqual(self.state()["active_risk"], tighter * 2, places=4)

    def test_multiple_positions_sum_and_unprotected_one_is_flagged(self):
        self.adapter.place_order("EURUSD", "buy", 0.10, 1.0950, None)
        self.adapter.place_order("EURUSD", "sell", 0.05, 1.1100, None)
        s = self.state()
        self.assertEqual(s["open_positions"], 2)
        self.assertAlmostEqual(s["active_risk"], sum(p["risk_amount"] for p in s["positions"]))
        self.adapter.place_order("EURUSD", "buy", 0.01)                         # no SL
        s = self.state()
        self.assertFalse(s["active_risk_complete"])
        self.assertEqual(len(s["unprotected"]), 1)

    def test_12_closed_position_leaves_the_risk_state(self):
        self.adapter.place_order("EURUSD", "buy", 0.10, 1.0950, 1.1100)
        ticket = self.adapter.get_positions()[0].ticket
        self.assertEqual(self.state()["open_positions"], 1)
        self.adapter.close_position(ticket)
        s = self.state()
        self.assertEqual((s["open_positions"], s["active_risk"], s["positions"]), (0, 0.0, []))

    def test_mt5_disconnected_fails_safe(self):
        from trademate.errors import BrokerUnavailableError
        patcher = mock.patch.object(self.adapter, "get_account", side_effect=BrokerUnavailableError("MT5 is not connected."))
        patcher.start()
        self.addCleanup(patcher.stop)
        res = self.client.post("/api/risk/evaluate", json={"symbol": "EURUSD", "direction": "buy", "entry": 1.1,
                                                          "stop_loss": 1.095, "risk_percent": 1}).get_json()
        self.assertFalse(res["ok"])
        self.assertNotIn("decision", res)
        self.assertIn("not connected", res["message"])


if __name__ == "__main__":
    unittest.main()
