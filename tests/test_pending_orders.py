"""Phase 7B pending orders: validation, Risk Engine gate, execution outcomes, management. FAKE bridge only: no real MT5, no real order.

    python3 -m unittest tests.test_pending_orders -v

Fake EURUSD: bid 1.10000, ask 1.10012, tick 0.00001, stops level 10 points (0.0001), volume 0.01..., equity 1000 USD.
"""

import time
import unittest
from unittest import mock

import app as app_module
from tests.test_phase6 import Phase6Base
from trademate.errors import OrderRejectedError
from trademate.pending import UNSUPPORTED_TYPES, validate_pending
from trademate.risk import RiskRules

PEND = {"order_type": "buy_limit", "symbol": "EURUSD", "volume": 0.01, "price": 1.0990, "sl": 1.0950, "tp": 1.1050}
FUTURE = int(time.time()) + 3 * 3600


class PendingBase(Phase6Base):
    def pending(self, **over):
        return self.client.post("/api/pending", json={**PEND, **over}).get_json()

    def review(self, **over):
        return self.client.post("/api/pending/review", json={**PEND, **over}).get_json()

    def pend_cmds(self, name="open_pending"):
        return [c for c in self.bridge.commands_seen if c.get("cmd") == name]

    def orders(self):
        return self.client.get("/api/broker").get_json()["orders"]


class PriceRules(PendingBase):
    def refused(self, **over):
        r = self.pending(**over)
        self.assertEqual((r["outcome"], r["order_submitted"]), ("invalid_request", False), r)
        self.assertEqual(self.pend_cmds(), [])
        return r["message"]

    def test_each_type_is_accepted_on_the_correct_side_of_the_market(self):
        self.set_rules(RiskRules(max_risk_per_trade_percent=1.0, max_daily_loss_percent=3.0, max_total_active_risk_percent=5.0, max_positions=10))
        cases = [("buy_limit", 1.0990, 1.0950, 1.1050), ("sell_limit", 1.1020, 1.1060, 1.0980), ("buy_stop", 1.1030, 1.0990, 1.1090),
                 ("sell_stop", 1.0970, 1.1010, 1.0910)]
        for kind, price, sl, tp in cases:
            r = self.pending(order_type=kind, price=price, sl=sl, tp=tp)
            self.assertEqual((r["state"], r["outcome"]), ("executed", "executed"), (kind, r))
            self.assertEqual(r["result"]["position"]["order_type"], kind)
        self.assertEqual(len(self.orders()), 4)
        self.assertEqual(self.client.get("/api/broker").get_json()["positions"], [])          # none of them is a position

    def test_a_price_on_the_wrong_side_or_too_close_is_refused_never_adjusted(self):
        cases = [("buy_limit", 1.10012, "BELOW the current Ask"), ("buy_limit", 1.1020, "BELOW the current Ask"), ("buy_limit", 1.10005, "at least 10 points"),
                 ("sell_limit", 1.1000, "ABOVE the current Bid"), ("sell_limit", 1.0990, "ABOVE the current Bid"), ("sell_limit", 1.10005, "at least 10 points"),
                 ("buy_stop", 1.10015, "ABOVE the current Ask"), ("buy_stop", 1.0990, "ABOVE the current Ask"),
                 ("sell_stop", 1.09995, "BELOW the current Bid"), ("sell_stop", 1.1010, "BELOW the current Bid")]
        for kind, price, word in cases:
            side_sl = (price - 0.004, price + 0.004) if kind.startswith("buy") else (price + 0.004, price - 0.004)
            msg = self.refused(order_type=kind, price=price, sl=round(side_sl[0], 5), tp=round(side_sl[1], 5))
            self.assertIn(word, msg, (kind, price))
        self.assertEqual(self.orders(), [])

    def test_the_price_sent_to_mt5_is_exactly_the_price_the_user_gave(self):
        self.assertEqual(self.pending(price=1.09873)["outcome"], "executed")
        self.assertEqual(float(self.pend_cmds()[0]["price"]), 1.09873)

    def test_off_grid_price_is_refused_with_the_nearest_valid_one(self):
        msg = self.refused(price=1.098735)
        self.assertIn("steps of 1e-05", msg)
        self.assertIn("nearest valid", msg)

    def test_unsupported_and_unknown_types_are_refused(self):
        for kind in (*UNSUPPORTED_TYPES, "market", "BUY_LIMIT ", None, 5, ""):
            msg = self.refused(order_type=kind)
            self.assertTrue("not supported" in msg or "must be one of" in msg, (kind, msg))

    def test_stops_are_checked_against_the_pending_price_not_the_market(self):
        self.assertIn("stop", self.refused(sl=1.1010).lower())                  # buy limit with SL above its entry
        self.assertIn("take", self.refused(tp=1.0980).lower())                  # TP below the entry
        self.assertIn("point", self.refused(sl=1.09895).lower())                # SL closer than the broker's minimum to the entry
        self.assertEqual(self.pending(sl=1.0950, tp=None)["outcome"], "executed")   # a TP is optional

    def test_stop_loss_is_required(self):
        self.assertIn("stop-loss is required", self.refused(sl=None).lower())

    def test_volume_and_numbers_are_validated_server_side(self):
        for over in ({"volume": 0}, {"volume": -1}, {"volume": "x"}, {"volume": True}, {"volume": 0.015}, {"volume": 500}, {"price": 0}, {"price": -1},
                     {"price": True}, {"price": "1.099"}, {"symbol": ""}, {"symbol": "NOPE"}):
            r = self.pending(**over)
            self.assertNotEqual(r["outcome"], "executed", over)
            self.assertFalse(r.get("order_submitted"), over)
        for raw in ('{"order_type":"buy_limit","symbol":"EURUSD","volume":NaN,"price":1.099,"sl":1.095}',
                    '{"order_type":"buy_limit","symbol":"EURUSD","volume":0.01,"price":Infinity,"sl":1.095}',
                    '{"order_type":"buy_limit","symbol":"EURUSD","volume":0.01,"price":1.099,"sl":1.095,"expiration":NaN}'):
            r = self.client.post("/api/pending", data=raw, content_type="application/json").get_json()
            self.assertEqual(r["outcome"], "invalid_request")
        self.assertEqual(self.pend_cmds(), [])

    def test_expiration_rules(self):
        now = time.time()
        for bad in (now - 100, now + 5, now + 10 ** 9, now + 3600.5, "tomorrow", float("nan")):
            self.assertNotEqual(self.pending(expiration=bad)["outcome"], "executed", bad)
        self.assertEqual(self.pend_cmds(), [])
        r = self.pending(expiration=FUTURE)
        self.assertEqual(r["outcome"], "executed")
        self.assertEqual(int(float(self.pend_cmds()[0]["expiration"])), FUTURE)
        self.assertEqual(self.orders()[0]["expiration"], FUTURE)

    def test_validate_pending_is_usable_directly(self):
        info = self.adapter.get_symbol_spec("EURUSD")
        ok = validate_pending(info, "sell_stop", 0.01, 1.0970, 1.1010, 1.0910, None, None, time.time())      # no volume cap given: the broker's own is used
        self.assertEqual((ok.side, ok.price, ok.expiration), ("sell", 1.0970, None))
        with self.assertRaises(OrderRejectedError):
            validate_pending(info, "sell_stop", 0.01, 1.1000, 1.1010, 1.0910, None, None, time.time())


class RiskGate(PendingBase):
    def test_the_risk_engine_judges_the_order_before_any_command_exists(self):
        seen = []
        real = app_module.gate_pending

        def spy(*a, **k):
            seen.append(len(self.pend_cmds()))
            return real(*a, **k)

        with mock.patch.object(app_module, "gate_pending", spy):
            r = self.pending()
        self.assertEqual(seen, [0])
        self.assertEqual(r["outcome"], "executed")
        self.assertEqual(len(self.pend_cmds()), 1)

    def test_a_risk_rejection_never_reaches_the_bridge_and_carries_the_figures(self):
        self.set_rules(RiskRules(max_risk_per_trade_percent=0.1, max_daily_loss_percent=3.0, max_total_active_risk_percent=3.0, max_positions=3))
        r = self.pending(price=1.0990, sl=1.0900)
        self.assertEqual((r["state"], r["outcome"], r["order_submitted"]), ("rejected_risk", "risk_rejected", False))
        self.assertGreater(r["risk"]["requested_risk_amount"], r["risk"]["max_risk_amount"])
        self.assertEqual(self.pend_cmds(), [])

    def test_risk_is_measured_from_the_pending_price_not_the_market(self):
        rv = self.review(price=1.0950, sl=1.0900, tp=1.1050)
        self.assertTrue(rv["approved"], rv)
        self.assertAlmostEqual(rv["estimates"]["risk_amount"], 5.0, places=2)        # (1.0950 - 1.0900) / 1e-5 x $1 x 0.01 lot
        self.assertEqual(rv["order"]["entry"], 1.0950)
        self.assertFalse(rv["order_sent"])
        self.assertEqual(self.pend_cmds(), [])

    def test_pending_orders_count_toward_the_position_limit(self):
        self.set_rules(RiskRules(max_risk_per_trade_percent=1.0, max_daily_loss_percent=3.0, max_total_active_risk_percent=3.0, max_positions=1))
        self.assertEqual(self.pending()["outcome"], "executed")
        r = self.pending(price=1.0980, sl=1.0940)
        self.assertEqual(r["outcome"], "risk_rejected")
        self.assertEqual(len(self.pend_cmds()), 1)
        m = self.order()                                                             # a MARKET order is limited by the same pending order
        self.assertEqual(m["outcome"], "risk_rejected")

    def test_pending_orders_count_toward_total_active_risk(self):
        self.set_rules(RiskRules(max_risk_per_trade_percent=1.0, max_daily_loss_percent=3.0, max_total_active_risk_percent=1.2, max_positions=5))
        self.assertEqual(self.pending(volume=0.02, price=1.0990, sl=1.0950)["outcome"], "executed")       # about 0.8 % if it fills
        self.assertEqual(self.pending(volume=0.02, price=1.0980, sl=1.0940)["outcome"], "risk_rejected")  # 0.8 + 0.8 > 1.2
        self.assertEqual(len(self.pend_cmds()), 1)

    def test_oversized_volume_is_refused_with_a_resize_suggestion_not_shrunk(self):
        rv = self.review(volume=0.05, price=1.0990, sl=1.0950)
        self.assertFalse(rv["approved"])
        self.assertEqual(rv["stage"], "rejected_risk")
        self.assertIsNotNone(rv["resize"])
        r = self.pending(volume=0.05)
        self.assertEqual(r["outcome"], "risk_rejected")
        self.assertEqual(self.pend_cmds(), [])

    def test_fail_closed_when_mt5_data_or_the_engine_is_unavailable(self):
        with mock.patch("trademate.execution_gate.gather_state", side_effect=app_module.BrokerError("MT5 is not connected.")):
            r = self.pending()
        self.assertEqual((r["outcome"], r["order_submitted"]), ("connection_failed", False))
        with mock.patch("trademate.execution_gate.evaluate_trade", side_effect=RuntimeError("boom")):
            r = self.pending()
        self.assertEqual(r["order_submitted"], False)
        self.assertNotIn("boom", str(r))
        self.assertEqual(self.pend_cmds(), [])

    def test_an_old_ea_without_pending_support_disables_everything_safely(self):
        with mock.patch.object(type(self.bridge), "PENDING_SUPPORTED", False):
            self.bridge.write_snapshot()
            b = self.client.get("/api/broker").get_json()
            self.assertFalse(b["pending_supported"])
            self.assertEqual(b["orders"], [])
            r = self.pending()
            self.assertEqual((r["outcome"], r["order_submitted"]), ("connection_failed", False))
            self.assertIn("updated TradeMateBridge EA", r["message"])
            self.assertEqual(self.client.post("/api/pending/1/cancel", json={}).get_json()["order_submitted"], False)
        self.assertEqual(self.pend_cmds(), [])

    def test_the_server_check_still_guards_pending_orders(self):
        with mock.patch.object(type(self.bridge), "SERVER", "Headway-Real"):
            self.bridge.write_snapshot()
            r = self.pending()
        self.assertEqual(r["outcome"], "execution_refused")
        self.assertIn("REFUSED", r["message"])
        self.assertEqual(self.pend_cmds(), [])


class Execution(PendingBase):
    def test_broker_rejection_is_reported_with_the_retcode_and_no_false_order_appears(self):
        self.bridge.reject_with = (10015, "Invalid price")
        r = self.pending()
        self.assertEqual((r["state"], r["outcome"], r["order_submitted"]), ("failed", "broker_rejected", True))
        self.assertEqual(r["broker"]["retcode"], 10015)
        self.assertEqual(self.orders(), [])

    def test_accepted_but_not_visible_is_not_called_executed(self):
        self.bridge.apply_but_hide = True
        r = self.pending()
        self.assertEqual((r["state"], r["outcome"]), ("unconfirmed", "accepted_unconfirmed"))
        self.assertEqual(self.orders(), [])

    def test_a_lost_answer_is_unknown_and_an_identical_retry_is_blocked(self):
        self.bridge.swallow_commands = True
        first = self.pending()
        self.assertEqual((first["outcome"], first["order_submitted"]), ("unknown", None))
        self.assertEqual(first["automatic_resubmission"], "DISABLED")
        sent = len(self.pend_cmds())
        again = self.pending()
        self.assertNotEqual(again["outcome"], "executed")
        self.assertEqual(len(self.pend_cmds()), sent)                              # the retry never reached the bridge

    def test_unknown_outcome_is_reconciled_against_the_account(self):
        self.bridge.swallow_commands = True
        self.pending()
        self.bridge.swallow_commands = False
        self.bridge.next_ticket += 1                                              # the order DID reach MT5; only the answer was lost
        self.bridge.orders[self.bridge.next_ticket] = {"symbol": "EURUSD", "type": "buy_limit", "volume": 0.01, "price_open": 1.0990,
                                                       "sl": 1.0950, "tp": 1.1050, "expiration": None, "time_setup": 1}
        self.bridge.write_snapshot()
        rec = self.adapter.journal.unresolved()[0]
        res = self.adapter.reconcile(rec["id"])
        self.assertEqual(res["state"], "reconciled_executed")
        self.assertIn("DID get placed", res["message"])

    def test_orders_and_positions_are_separate_and_a_fill_moves_one_to_the_other(self):
        t = self.pending()["result"]["ticket"]
        b = self.client.get("/api/broker").get_json()
        self.assertEqual([o["ticket"] for o in b["orders"]], [t])
        self.assertEqual(b["positions"], [])
        self.assertEqual(b["orders"][0]["status"], "pending")
        self.bridge.fill_order(t)
        b = self.client.get("/api/broker").get_json()
        self.assertEqual(b["orders"], [])
        self.assertEqual([p["ticket"] for p in b["positions"]], [t])

    def test_unsupported_order_types_reported_by_the_broker_are_never_shown(self):
        self.bridge.next_ticket += 1
        self.bridge.orders[self.bridge.next_ticket] = {"symbol": "EURUSD", "type": "buy_stop_limit", "volume": 0.01, "price_open": 1.103,
                                                       "sl": None, "tp": None, "expiration": None, "time_setup": 1}
        self.bridge.write_snapshot()
        self.assertEqual(self.orders(), [])

    def test_repeated_identical_requests_after_success_are_separate_orders_but_never_duplicated_by_retry_logic(self):
        self.assertEqual(self.pending()["outcome"], "executed")
        self.assertEqual(len(self.pend_cmds()), 1)                                # one request: one command, no hidden retry


class Management(PendingBase):
    def test_cancel_removes_only_the_chosen_order(self):
        a = self.pending(price=1.0990)["result"]["ticket"]
        b = self.pending(price=1.0980, sl=1.0940)["result"]["ticket"]
        r = self.client.post(f"/api/pending/{a}/cancel", json={}).get_json()
        self.assertEqual((r["outcome"], r["request"]["ticket"]), ("executed", a))
        self.assertEqual([o["ticket"] for o in self.orders()], [b])
        self.assertEqual(int(self.pend_cmds("cancel_order")[0]["ticket"]), a)

    def test_cancel_refuses_unknown_tickets_and_positions_without_sending_anything(self):
        m = self.order()["result"]["ticket"]                                     # an open POSITION
        for t in (m, 99999):
            r = self.client.post(f"/api/pending/{t}/cancel", json={}).get_json()
            self.assertEqual((r["outcome"], r["order_submitted"]), ("execution_refused", False), t)
        self.assertEqual(self.pend_cmds("cancel_order"), [])
        self.assertEqual(len(self.client.get("/api/broker").get_json()["positions"]), 1)     # the position was not touched

    def test_a_pending_ticket_cannot_be_closed_as_a_position(self):
        t = self.pending()["result"]["ticket"]
        r = self.client.post(f"/api/position/{t}/close", json={}).get_json()
        self.assertNotEqual(r["outcome"], "executed")
        self.assertEqual(len(self.orders()), 1)                                  # still pending

    def test_cancel_of_an_order_that_filled_first_says_so(self):
        t = self.pending()["result"]["ticket"]
        real = self.adapter._run

        def fill_then_run(fields, **kw):
            if fields.get("cmd") == "cancel_order":
                self.bridge.fill_order(t)                                         # price hit the order a moment before the cancel
                fields = {**fields, "cmd": "cancel_order"}
                return {"ok": True, "retcode": 10009, "message": "ok", "ticket": t}
            return real(fields, **kw)

        with mock.patch.object(self.adapter, "_run", fill_then_run):
            r = self.client.post(f"/api/pending/{t}/cancel", json={}).get_json()
        self.assertEqual(r["outcome"], "accepted_unconfirmed")
        self.assertIn("FILLED", r["message"])

    def test_modify_updates_price_sl_tp_and_shows_the_brokers_values(self):
        t = self.pending()["result"]["ticket"]
        r = self.client.post(f"/api/pending/{t}/modify", json={"price": 1.0985, "sl": 1.0945, "tp": 1.1040}).get_json()
        self.assertEqual((r["outcome"], r["request"]["action"]), ("executed", "modify_pending"))
        o = self.orders()[0]
        self.assertEqual((o["price"], o["stop_loss"], o["take_profit"]), (1.0985, 1.0945, 1.1040))
        self.assertEqual(o["volume"], 0.01)                                      # volume untouched

    def test_modify_touches_only_the_chosen_order_and_never_adjusts_a_bad_price(self):
        a = self.pending(price=1.0990)["result"]["ticket"]
        b = self.pending(price=1.0980, sl=1.0940)["result"]["ticket"]
        bad = self.client.post(f"/api/pending/{a}/modify", json={"price": 1.1050}).get_json()      # a Buy Limit above the Ask
        self.assertEqual((bad["state"], bad["order_submitted"]), ("failed", False))
        self.assertIn("BELOW the current Ask", bad["message"])
        by = {o["ticket"]: o for o in self.orders()}
        self.assertEqual((by[a]["price"], by[b]["price"]), (1.0990, 1.0980))
        self.client.post(f"/api/pending/{b}/modify", json={"tp": 1.1040})
        by = {o["ticket"]: o for o in self.orders()}
        self.assertEqual((by[a]["take_profit"], by[b]["take_profit"]), (1.1050, 1.1040))

    def test_modify_validation_and_no_change_cases(self):
        t = self.pending()["result"]["ticket"]
        for body in ({}, {"price": "x"}, {"price": float("nan")}):
            r = self.client.post(f"/api/pending/{t}/modify", json=body).get_json()
            self.assertEqual(r["outcome"], "invalid_request", body)
        r = self.client.post(f"/api/pending/{t}/modify", json={"price": 1.0990}).get_json()
        self.assertEqual(r["outcome"], "executed")
        self.assertIn("No change", r["message"])
        self.assertEqual(self.pend_cmds("modify_order"), [])

    def test_modify_unknown_order_is_refused(self):
        r = self.client.post("/api/pending/424242/modify", json={"price": 1.099}).get_json()
        self.assertEqual((r["outcome"], r["order_submitted"]), ("execution_refused", False))


if __name__ == "__main__":
    unittest.main()
