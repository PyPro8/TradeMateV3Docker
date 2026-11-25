"""Pip targets, user lot size, one-order-per-setup and the new routes, through the REAL pipeline and the in-memory fake bridge (no MT5, no real orders).

    python3 -m unittest tests.test_fixed_target_execution -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.test_pipeline import PipelineBase, strategy_payload
from trademate import pipeline
from trademate.decision_hub import DecisionHub
from trademate.lotsize import LotStore
from trademate.pips import PipStore
from trademate.selection_store import SelectionStore
from trademate.setups_ledger import SetupLedger


def pip_payload(cid="CAND-1", pips=10.0, **over):
    p = strategy_payload()
    r = p["result"]
    r["setup_candidates"][0]["candidate_id"] = cid
    r["target"] = {"mode": "pips", "pips": pips, "pip_size": 0.0001}
    r["risk_reward"]["min_rr"] = 0.0
    r["execution_timeframe"] = "M5"
    r.update(over)
    return p


class FixedTargetBase(PipelineBase):
    def setUp(self):
        super().setUp()
        tmp = Path(self._tmp.name)
        self.lots = LotStore(tmp / "lots.json")
        self.pips = PipStore(tmp / "pips.json")
        self.ledger = SetupLedger(tmp / "ledger.json")
        self.hub = DecisionHub()
        self.sel = SelectionStore(tmp / "sel.json")
        for name, obj in (("lot_store", self.lots), ("pip_store", self.pips), ("setup_ledger", self.ledger), ("decision_hub", self.hub), ("selection_store", self.sel)):
            p = mock.patch.object(app_module, name, obj)
            p.start()
            self.addCleanup(p.stop)
        self.holder.payload = pip_payload()

    def approval_id(self, r):
        return r["approval"]["id"]

    def live(self):
        info = self.adapter.get_symbol_info("EURUSD")
        return info.bid, info.ask


class FixedTargetTest(FixedTargetBase):
    def test_target_is_measured_from_the_live_entry_not_the_strategy_price(self):
        self.lots.set("EURUSD", 0.05)
        r = self.approved()
        _, ask = self.live()
        self.assertAlmostEqual(r["approval"]["summary"]["take_profit"], ask + 0.0010, places=5)
        self.assertNotAlmostEqual(r["approval"]["summary"]["take_profit"], 1.1035, places=4)         # the strategy's reference TP (a 2R-ish level) is not used
        self.assertEqual(r["approval"]["summary"]["target_pips"], 10.0)
        self.assertEqual(r["approval"]["summary"]["volume"], 0.05)
        self.assertEqual(r["approval"]["summary"]["volume_source"], "user_lot")

    def test_executed_position_has_the_pip_target_and_user_lot(self):
        self.lots.set("EURUSD", 0.05)
        r = self.approved()
        out = self.execute(self.approval_id(r))
        self.assertEqual(out["outcome"], "executed", out)
        w = self.writes()
        self.assertEqual(len(w), 1)
        self.assertAlmostEqual(float(w[0]["volume"]), 0.05)
        _, ask = self.live()
        self.assertAlmostEqual(float(w[0]["tp"]), ask + 0.0010, places=5)

    def test_tampered_or_invalid_saved_lot_never_reaches_the_broker(self):
        for bad in (0.015, 500.0, 0.001):
            with self.subTest(bad=bad):
                self.lots.set("EURUSD", bad)                   # as if the file had been edited by hand
                r = self.propose()
                self.assertFalse(r["approved"])
                self.assertEqual(r["code"], pipeline.VOLUME_INVALID)
        self.assertEqual(self.writes(), [])

    def test_risk_rejected_order_never_reaches_the_broker(self):
        self.lots.set("EURUSD", 50.0)                          # valid for the instrument, far beyond the risk limits
        r = self.propose()
        self.assertFalse(r["approved"])
        self.assertEqual(self.writes(), [])

    def test_same_setup_cannot_create_a_second_order(self):
        self.lots.set("EURUSD", 0.05)
        a = self.approved()
        out = self.execute(self.approval_id(a))
        self.assertEqual(out["outcome"], "executed")
        again = self.propose()
        self.assertFalse(again["approved"])
        self.assertEqual(again["code"], pipeline.DUPLICATE_SETUP)
        self.assertEqual(len(self.writes()), 1)

    def test_two_approvals_of_one_setup_only_one_executes(self):
        self.lots.set("EURUSD", 0.05)
        a = self.approved()
        self.ledger.claim("CAND-1")                            # another path got there first
        out = self.execute(self.approval_id(a))
        self.assertNotEqual(out["outcome"], "executed")
        self.assertEqual(self.writes(), [])

    def test_a_new_candidate_is_a_new_order(self):
        self.lots.set("EURUSD", 0.05)
        self.execute(self.approval_id(self.approved()))
        self.holder.payload = pip_payload(cid="CAND-2")
        r = self.propose()
        self.assertTrue(r["approved"], r)

    def test_unknown_pip_size_is_rejected(self):
        p = pip_payload()
        p["result"]["target"]["pip_size"] = None
        self.holder.payload = p
        r = self.propose()
        self.assertFalse(r["approved"])
        self.assertEqual(self.writes(), [])

    def test_broker_minimum_stop_distance_rejects(self):
        self.bridge_stops = 0
        self.holder.payload = pip_payload(pips=0.5)            # half a pip: closer than the broker's minimum (10 points)
        self.lots.set("EURUSD", 0.05)
        r = self.propose()
        self.assertFalse(r["approved"])
        self.assertEqual(r["code"], pipeline.LEVELS_INVALID)

    def test_realigned_target_helper(self):
        plan = pipeline.ExecutionPlan("a", "EURUSD", "buy", 0.1, 1.09, 1.1010, 1.1000, 0.00001, target_pips=10.0, pip_size=0.0001)
        self.assertAlmostEqual(pipeline.realigned_target(plan, 1.10020, 0.00001), 1.1012, places=5)    # filled 2 pips worse: the target moves with it
        self.assertIsNone(pipeline.realigned_target(plan, 1.1000, 0.00001))
        plan2 = pipeline.ExecutionPlan("a", "EURUSD", "buy", 0.1, 1.09, 1.1010, 1.1000, 0.00001)
        self.assertIsNone(pipeline.realigned_target(plan2, 1.2, 0.00001))


class RoutesTest(FixedTargetBase):
    def test_lot_rejects_invalid_values_and_saves_exact_ones(self):
        bad = {"volume": 0.015}, {"volume": 0.001}, {"volume": 1000}, {"volume": "abc"}, {"volume": None}, {"volume": -1}, {}
        for body in bad:
            r = self.client.post("/api/lot", json={"symbol": "EURUSD", **body})
            self.assertEqual(r.status_code, 400, body)
            self.assertFalse(r.get_json()["ok"])
            self.assertIsNone(self.lots.get("EURUSD"))                       # nothing was saved, nothing rounded
        r = self.client.post("/api/lot", json={"symbol": "EURUSD", "volume": 0.07})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.lots.get("EURUSD"), 0.07)
        self.assertEqual(self.client.get("/api/lot?symbol=EURUSD").get_json()["will_use"], 0.07)

    def test_lot_message_names_the_valid_neighbours(self):
        j = self.client.post("/api/lot", json={"symbol": "EURUSD", "volume": 0.015}).get_json()
        self.assertIn("0.01 and 0.02", j["message"])

    def test_selection_validates_and_persists(self):
        r = self.client.post("/api/selection", json={"symbol": "EURUSD", "strategy": "candle_momentum", "timeframe": "M15"})
        self.assertEqual(r.status_code, 200, r.get_json())
        j = self.client.get("/api/selection").get_json()
        self.assertEqual((j["symbol"], j["strategy_id"], j["timeframe"]), ("EURUSD", "candle_momentum", "M15"))
        self.assertEqual(self.client.post("/api/selection", json={"timeframe": "M7"}).status_code, 400)
        self.assertEqual(self.client.post("/api/selection", json={"strategy": "nope"}).status_code, 400)
        self.assertEqual(self.client.post("/api/selection", json={"symbol": "NOSUCH"}).status_code, 400)
        self.client.post("/api/selection", json={"strategy": "choch", "timeframe": "H1"})
        self.assertEqual(self.sel.timeframe("candle_momentum"), "M15")        # timeframes are independent per strategy
        self.assertEqual(self.sel.timeframe("choch"), "H1")

    def test_decision_current_is_keyed_and_never_leaks_another_selection(self):
        self.hub.publish(self.hub.begin(), "EURUSD", "choch", "M5", state="fresh", result={"decision": "BUY"})
        j = self.client.get("/api/decision/current?symbol=EURUSD&strategy=choch&timeframe=M5").get_json()
        self.assertEqual(j["record"]["decision"], "BUY")
        for q in ("symbol=GBPUSD&strategy=choch&timeframe=M5", "symbol=EURUSD&strategy=trend&timeframe=M5", "symbol=EURUSD&strategy=choch&timeframe=H1"):
            self.assertIsNone(self.client.get("/api/decision/current?" + q).get_json()["record"], q)

    def test_autopilot_start_validation_is_enforced_by_the_backend(self):
        self.client.post("/api/selection", json={"symbol": "EURUSD", "strategy": "candle_momentum", "timeframe": "M5"})
        for body in ({"mode": "runs", "value": 0}, {"mode": "runs", "value": 2.5}, {"mode": "profit", "value": -1}, {"mode": "hours", "value": "x"}, {"mode": "bogus", "value": 1}, {}):
            r = self.client.post("/api/autopilot/start", json=body)
            self.assertEqual(r.status_code, 400, body)
        st = self.client.get("/api/autopilot/status").get_json()
        self.assertFalse(st["armed"])
        self.assertEqual([m["id"] for m in st["modes"]], ["runs", "profit", "hours"])

    def test_pip_card_uses_the_central_engine_for_synthetics_and_indices_without_any_user_input(self):
        from tests.test_instruments import crash100, info
        cases = {"Crash 100 Index": crash100(tv=0.001, tick=0.001, bid=1000.0, ask=1000.2), "US30": info("US30", 1, 0.1, 0.1, 0.1, "Indices", bid=39000.0, ask=39001.0)}
        for name, spec_info in cases.items():
            with mock.patch.object(app_module.adapter, "get_symbol_spec", return_value=spec_info):
                j = self.client.get(f"/api/pip?symbol={name.replace(' ', '%20')}").get_json()
            self.assertTrue(j["defined"], name)                                    # the old resolver refused these as "unknown"
            self.assertEqual((j["source"], j["size"]), ("automatic", 1.0), name)   # nothing was typed by the user
            self.assertEqual(j["status"], "VERIFIED", name)
            self.assertIsNone(self.pips.get(name))
        self.assertAlmostEqual(j["money_per_unit_1lot"], 1.0)                      # US30: tick_value 0.1 / tick 0.1 * unit 1.0 * 1 lot
        with mock.patch.object(app_module.adapter, "get_symbol_spec", return_value=cases["Crash 100 Index"]):
            self.assertAlmostEqual(self.client.get("/api/pip?symbol=Crash%20100%20Index").get_json()["money_per_unit_1lot"], 1.0)   # 1 lot = $1 per 1.00 pip

    def test_autopilot_precheck_blocks_an_unverified_instrument_and_allows_a_verified_one(self):
        from types import SimpleNamespace as NS
        from tests.test_instruments import crash100
        from trademate.broker.base import ConnectionState
        sel = ("Crash 100 Index", "candle_momentum", "M1")
        wd = {"active": True, "symbol": sel[0], "strategy_id": sel[1], "exec_tf": sel[2]}
        patches = [mock.patch.object(app_module.watchdog, "status", return_value=wd),
                   mock.patch.object(app_module, "_current_selection", return_value=sel),
                   mock.patch.object(app_module.adapter, "get_connection_status", return_value=NS(state=ConnectionState.CONNECTED, message="ok")),
                   mock.patch.object(app_module, "settings", NS(trading=NS(enabled=True), pips=NS(overrides={})))]
        for p_ in patches:
            p_.start(); self.addCleanup(p_.stop)
        with mock.patch.object(app_module.adapter, "get_symbol_spec", return_value=crash100(tv=None, bid=1000.0, ask=1000.2)):
            msg = app_module._autopilot_precheck()
        self.assertIn("UNVERIFIED", msg)
        self.assertIn("TICK_VALUE", msg)                                           # names the missing broker field
        with mock.patch.object(app_module.adapter, "get_symbol_spec", return_value=crash100(tv=0.001, bid=1000.0, ask=1000.2)):
            self.assertIsNone(app_module._autopilot_precheck())

    def test_pip_card_reports_unverified_when_broker_metadata_is_missing(self):
        from tests.test_instruments import crash100
        with mock.patch.object(app_module.adapter, "get_symbol_spec", return_value=crash100(tv=None, bid=1000.0, ask=1000.2)):
            j = self.client.get("/api/pip?symbol=Crash%20100%20Index").get_json()
        self.assertEqual(j["status"], "UNVERIFIED")
        self.assertTrue(any("TICK_VALUE" in p for p in j["problems"]))             # says WHICH metadata is missing

    def test_pip_size_can_be_set_in_the_browser_validated_and_cleared(self):
        j = self.client.get("/api/pip?symbol=EURUSD").get_json()
        self.assertEqual((j["defined"], j["source"], round(j["size"], 6)), (True, "automatic", 0.0001))
        self.assertEqual([r["pips"] for r in j["preview"]], [10.0, 10.0, 100.0])
        for bad in ("abc", "-1", "0", "", True, "inf", "nan", 0.000000001):
            r = self.client.post("/api/pip", json={"symbol": "EURUSD", "size": bad})
            self.assertEqual(r.status_code, 400, bad)
        self.assertIsNone(self.pips.get("EURUSD"))
        j = self.client.post("/api/pip", json={"symbol": "EURUSD", "size": "0.001"}).get_json()
        self.assertEqual((j["source"], j["size"]), ("browser", 0.001))
        self.assertAlmostEqual(j["preview"][2]["price_distance"], 0.1)          # 100 pips = 0.1 with the user's pip
        self.assertEqual(self.pips.get("EURUSD"), 0.001)
        j = self.client.post("/api/pip", json={"symbol": "EURUSD", "size": None}).get_json()
        self.assertEqual(j["source"], "automatic")

    def test_browser_pip_wins_over_settings_and_reaches_the_strategy_service(self):
        self.pips.set("EURUSD", 0.5)
        self.assertEqual(app_module._pip_overrides()["EURUSD"], 0.5)
        from trademate.strategies.service import evaluate_selected
        out = evaluate_selected(app_module.adapter, "candle_momentum", "EURUSD", app_module.settings, pip_overrides=app_module._pip_overrides())
        self.assertEqual(out["result"]["pip"]["size"], 0.5)
        self.assertEqual(out["result"]["pip"]["rule"], "override")

    def test_changing_the_pip_cancels_pending_approvals_and_clears_decisions(self):
        self.hub.publish(self.hub.begin(), "EURUSD", "choch", "M5", state="fresh", result={"decision": "BUY"})
        self.client.post("/api/pip", json={"symbol": "EURUSD", "size": 0.001})
        self.assertIsNone(self.hub.get("EURUSD", "choch", "M5"))

    def test_emergency_stop_route_is_safe_when_idle(self):
        self.addCleanup(app_module.safety_lock.unlock, "UNLOCK")                     # the lock is persistent: never leave it set for other tests
        j = self.client.post("/api/autopilot/emergency-stop").get_json()
        self.assertTrue(j["ok"])
        self.assertTrue(j["emergency_locked"])


if __name__ == "__main__":
    unittest.main()
