"""Read-only background-monitor tests. The evaluator is a deterministic fake; no broker or order API is involved."""

import time
import unittest

from trademate.strategy.watchdog import WatchdogManager


def confirmed_result(entry=1.1):
    return {
        "decision": "BUY",
        "setup_status": "confirmed",
        "entry": {"price": entry},
        "stop_loss": {"price": 1.0},
        "take_profit_1": {"price": 1.3},
        "context": {"summary": "Qualified test setup."},
    }


def developing_candidate(support=1, missing=1):
    return {
        "candidate_id": "pullback-eur",
        "setup_family": "pullback_continuation",
        "direction": "bullish",
        "status": "developing",
        "premise": "Bullish context with an observed pullback.",
        "supporting_evidence": ["support"] * support,
        "conflicting_evidence": [],
        "missing_evidence": ["confirmation"] * missing,
        "invalidation_conditions": ["closed break below the setup level"],
    }


class WatchdogManagerTests(unittest.TestCase):
    def wait_for(self, predicate, timeout=1):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Timed out waiting for the watchdog state.")

    def test_start_stop_and_same_setup_alert_deduplication(self):
        manager = WatchdogManager(lambda symbol: {"ok": True, "connection": "CONNECTED", "result": confirmed_result()}, 0.01)
        ok, _ = manager.start("EURUSD")
        self.assertTrue(ok)
        self.wait_for(lambda: manager.status()["last_checked"] is not None)
        time.sleep(0.04)
        status = manager.status()
        self.assertTrue(status["active"])
        self.assertEqual(status["state"], "Monitoring")
        self.assertEqual(status["symbol"], "EURUSD")
        self.assertEqual(len(status["events"]), 1)
        self.assertEqual(status["events"][0]["decision"], "BUY")
        self.assertTrue(manager.start("EURUSD")[0])  # idempotent; no second worker
        self.assertFalse(manager.start("XAUUSD")[0])
        manager.stop()
        self.wait_for(lambda: not manager.status()["active"])
        self.assertEqual(manager.status()["state"], "Stopped")

    def test_changed_setup_is_a_new_deduplicated_event(self):
        value = [confirmed_result()]
        manager = WatchdogManager(lambda symbol: {"ok": True, "connection": "CONNECTED", "result": value[0]}, 0.01)
        manager.start("EURUSD")
        self.wait_for(lambda: len(manager.status()["events"]) == 1)
        value[0] = confirmed_result(1.2)
        self.wait_for(lambda: len(manager.status()["events"]) == 2)
        events = manager.status()["events"]
        self.assertNotEqual(events[0]["event_key"], events[1]["event_key"])
        manager.stop()
        self.wait_for(lambda: not manager.status()["active"])

    def test_unqualified_or_incomplete_result_never_creates_alert(self):
        result = confirmed_result()
        result["setup_status"] = "developing"
        manager = WatchdogManager(lambda symbol: {"ok": True, "connection": "CONNECTED", "result": result}, 0.01)
        manager.start("EURUSD")
        self.wait_for(lambda: manager.status()["last_checked"] is not None)
        self.assertEqual(manager.status()["events"], [])
        manager.stop()
        self.wait_for(lambda: not manager.status()["active"])

    def test_candidate_transitions_are_tracked_once_and_do_not_create_trade_alerts(self):
        value = {
            "decision": "WATCH",
            "setup_status": "developing",
            "data": {tf: {"status": "ok"} for tf in ("M5", "M15", "H1", "H4")},
            "setup_candidates": [developing_candidate()],
        }
        manager = WatchdogManager(lambda symbol: {"ok": True, "connection": "CONNECTED", "result": value}, 0.01)
        manager.start("EURUSD")
        self.wait_for(lambda: len(manager.status()["candidate_events"]) == 1)
        time.sleep(0.04)
        self.assertEqual(len(manager.status()["candidate_events"]), 1)
        self.assertEqual(manager.status()["candidate_states"][0]["state"], "awaiting_event")
        self.assertEqual(manager.status()["events"], [])

        value["setup_candidates"] = [developing_candidate(support=2, missing=0)]
        self.wait_for(lambda: any(event["event_type"] == "candidate_strengthening"
                                  for event in manager.status()["candidate_events"]))
        self.assertEqual(manager.status()["candidate_states"][0]["state"], "strengthening")
        value["setup_candidates"] = []
        self.wait_for(lambda: manager.status()["candidate_states"][0]["state"] == "no_longer_observed")
        manager.stop()
        self.wait_for(lambda: not manager.status()["active"])

    def test_stale_market_data_suspends_cached_candidate_state(self):
        value = {
            "decision": "WATCH",
            "setup_status": "developing",
            "data": {tf: {"status": "ok"} for tf in ("M5", "M15", "H1", "H4")},
            "setup_candidates": [developing_candidate()],
        }
        manager = WatchdogManager(lambda symbol: {"ok": True, "connection": "CONNECTED", "result": value}, 0.01)
        manager.start("EURUSD")
        self.wait_for(lambda: manager.status()["candidate_states"])
        value["data"]["M5"]["status"] = "stale"
        self.wait_for(lambda: manager.status()["state"] == "Stale Data")
        self.assertEqual(manager.status()["candidate_states"][0]["state"], "stale_data")
        self.assertIn("stale", manager.status()["message"].lower())
        manager.stop()
        self.wait_for(lambda: not manager.status()["active"])

    def test_disconnection_is_visible_and_monitor_stops_cleanly(self):
        manager = WatchdogManager(lambda symbol: {"ok": True, "connection": "DISCONNECTED", "result": {}}, 0.01)
        manager.start("EURUSD")
        self.wait_for(lambda: manager.status()["state"] == "Disconnected")
        manager.stop()
        self.wait_for(lambda: not manager.status()["active"])

    def test_confirmed_choch_creates_one_alert_event_per_timeframe_candle(self):
        event = {
            "type": "CHOCH", "direction": "bearish", "level": 3205.88,
            "candle_time": 1_800_000_000.0, "confirmed": True, "same_candle_conflict": False,
        }
        value = {
            "decision": "WATCH", "setup_status": "developing",
            "data": {tf: {"status": "ok"} for tf in ("M5", "M15", "H1", "H4")},
            "timeframes": {
                "H1": {"data_status": "ok", "events": [event]},
                "H4": {"data_status": "ok", "events": []},
            },
            "setup_candidates": [],
        }
        manager = WatchdogManager(lambda symbol: {"ok": True, "connection": "CONNECTED", "result": value}, 0.01)
        manager.start("CRASH_100")
        self.wait_for(lambda: len([e for e in manager.status()["events"] if e.get("event_type") == "choch"]) == 1)
        time.sleep(0.04)
        events = [e for e in manager.status()["events"] if e.get("event_type") == "choch"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["symbol"], "CRASH_100")
        self.assertEqual(events[0]["timeframe"], "H1")
        self.assertEqual(events[0]["direction"], "bearish")
        self.assertEqual(events[0]["level"], 3205.88)
        manager.stop()
        self.wait_for(lambda: not manager.status()["active"])

    def test_watchdog_choch_alert_ignores_provisional_conflicted_and_stale_events(self):
        events = [
            {"type": "CHOCH", "direction": "bullish", "level": 1.2, "candle_time": 1, "confirmed": False},
            {"type": "CHOCH", "direction": "bullish", "level": 1.3, "candle_time": 2, "confirmed": True, "same_candle_conflict": True},
            {"type": "CHOCH", "direction": "bullish", "level": 1.4, "candle_time": 3, "confirmed": True, "same_candle_conflict": False},
        ]
        value = {
            "decision": "WATCH", "setup_status": "developing",
            "data": {"M5": {"status": "ok"}, "M15": {"status": "stale"}},
            "timeframes": {
                "M5": {"data_status": "ok", "events": events},
                "M15": {"data_status": "ok", "events": [dict(events[2], candle_time=4)]},
            },
            "setup_candidates": [],
        }
        manager = WatchdogManager(lambda symbol: {"ok": True, "connection": "CONNECTED", "result": value}, 0.01)
        manager.start("EURUSD")
        self.wait_for(lambda: manager.status()["last_checked"] is not None)
        time.sleep(0.03)
        self.assertEqual([e for e in manager.status()["events"] if e.get("event_type") == "choch"], [])
        manager.stop()
        self.wait_for(lambda: not manager.status()["active"])


    def test_unreliable_data_never_creates_setup_alert_even_if_cached_result_says_confirmed(self):
        result = confirmed_result()
        result["data"] = {"M5": {"status": "stale"}, "M15": {"status": "ok"}}
        manager = WatchdogManager(lambda symbol: {"ok": True, "connection": "CONNECTED", "result": result}, 0.01)
        manager.start("EURUSD")
        self.wait_for(lambda: manager.status()["last_checked"] is not None)
        time.sleep(0.03)
        self.assertEqual(manager.status()["events"], [])
        manager.stop()
        self.wait_for(lambda: not manager.status()["active"])

    def test_invalid_trade_geometry_never_creates_setup_alert(self):
        for decision, entry, stop, target in (
            ("BUY", 1.1, 1.2, 1.3),
            ("BUY", 1.1, 1.0, 1.05),
            ("SELL", 1.1, 1.0, 1.2),
        ):
            with self.subTest(decision=decision, entry=entry, stop=stop, target=target):
                result = confirmed_result(entry)
                result["decision"] = decision
                result["stop_loss"] = {"price": stop}
                result["take_profit_1"] = {"price": target}
                manager = WatchdogManager(lambda symbol, r=result: {"ok": True, "connection": "CONNECTED", "result": r}, 0.01)
                manager.start("EURUSD")
                self.wait_for(lambda: manager.status()["last_checked"] is not None)
                time.sleep(0.02)
                self.assertEqual(manager.status()["events"], [])
                manager.stop()
                self.wait_for(lambda: not manager.status()["active"])

    def test_setup_event_contains_family_rr_and_supporting_evidence(self):
        result = confirmed_result()
        result.update({"selected_setup_family": "pullback_continuation", "risk_reward": 2.0,
                       "confirmation_evidence": ["confirmed M5 BOS", "live M15 OB"],
                       "setup_candidates": [{"setup_family": "pullback_continuation", "direction": "bullish",
                                             "invalidation_conditions": ["M5 close below swing low"]}]})
        manager = WatchdogManager(lambda symbol: {"ok": True, "connection": "CONNECTED", "result": result}, 0.01)
        manager.start("EURUSD")
        self.wait_for(lambda: len(manager.status()["events"]) == 1)
        event = manager.status()["events"][0]
        self.assertEqual(event["setup_family"], "pullback_continuation")
        self.assertEqual(event["risk_reward"], 2.0)
        self.assertIn("confirmed M5 BOS", event["supporting_evidence"])
        self.assertIn("M5 close below swing low", event["invalidation_conditions"])
        manager.stop()
        self.wait_for(lambda: not manager.status()["active"])

    def test_invalid_poll_interval_is_rejected(self):
        for interval in (0, -1, float("nan"), float("inf")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                WatchdogManager(lambda symbol: {}, interval)


if __name__ == "__main__":
    unittest.main()
