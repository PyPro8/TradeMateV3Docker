"""The stale-WAIT fix at the backend: whatever the Watchdog sees is what the decision hub (and so the Command Centre panel) holds, keyed by selection.

    python3 -m unittest tests.test_watchdog_hub -v
"""

import threading
import time
import unittest

from tests.test_watchdog import confirmed_result
from trademate.decision_hub import DecisionHub
from trademate.strategy.watchdog import WatchdogManager


def until(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return fn()


class Feed:
    def __init__(self):
        self.decision = "WAIT"
        self.calls = []
        self.gate = None

    def __call__(self, symbol, strategy_id=None, exec_tf=None):
        self.calls.append((symbol, strategy_id, exec_tf))
        if self.gate:
            self.gate.wait(2)
        if self.decision in ("BUY", "SELL"):
            r = dict(confirmed_result(), decision=self.decision)
        else:
            r = {"decision": self.decision, "setup_status": "inconclusive", "context": {"summary": "Nothing yet."}}
        r["strategy"] = {"id": strategy_id}
        r["data"] = {"M5": {"status": "ok"}}
        return {"ok": True, "connection": "CONNECTED", "result": r}


class WatchdogHubTest(unittest.TestCase):
    def setUp(self):
        self.hub = DecisionHub(stale_after=60)
        self.feed = Feed()
        self.wd = WatchdogManager(self.feed, interval=0.05, hub=self.hub)
        self.addCleanup(self.wd.stop)

    def decision(self, sym="EURUSD", sid="candle_momentum", tf="M5"):
        rec = self.hub.get(sym, sid, tf)
        return rec and rec["decision"]

    def test_wait_becomes_buy_then_sell_then_wait_with_no_manual_analyse(self):
        self.assertTrue(self.wd.start("EURUSD", "candle_momentum", "M5")[0])
        self.assertTrue(until(lambda: self.decision() == "WAIT"))
        self.feed.decision = "BUY"
        self.assertTrue(until(lambda: self.decision() == "BUY"), "the hub never saw the Watchdog's BUY")
        self.feed.decision = "SELL"
        self.assertTrue(until(lambda: self.decision() == "SELL"))
        self.feed.decision = "WAIT"
        self.assertTrue(until(lambda: self.decision() == "WAIT"))

    def test_retarget_follows_selection_and_old_results_cannot_leak(self):
        self.wd.start("EURUSD", "candle_momentum", "M5")
        self.feed.decision = "BUY"
        self.assertTrue(until(lambda: self.decision() == "BUY"))
        self.feed.decision = "SELL"
        self.assertTrue(self.wd.retarget("GBPUSD", "choch", "H1")[0])
        self.assertTrue(until(lambda: self.decision("GBPUSD", "choch", "H1") == "SELL"))
        self.assertEqual(self.decision("EURUSD", "candle_momentum", "M5"), "BUY")       # the old key was never overwritten by the new selection
        self.assertTrue(any(c == ("GBPUSD", "choch", "H1") for c in self.feed.calls))
        self.assertEqual(self.wd.status()["exec_tf"], "H1")

    def test_in_flight_result_of_the_old_target_is_discarded_after_retarget(self):
        self.feed.gate = threading.Event()
        self.wd.start("EURUSD", "candle_momentum", "M5")
        self.assertTrue(until(lambda: len(self.feed.calls) >= 1))
        self.feed.decision = "BUY"
        self.wd.retarget("GBPUSD", "candle_momentum", "M5")
        self.feed.gate.set()
        time.sleep(0.3)
        self.assertIsNone(self.hub.get("EURUSD", "candle_momentum", "M5") and self.hub.get("EURUSD", "candle_momentum", "M5")["decision"] == "BUY" or None)

    def test_broker_error_leaves_no_decision(self):
        def boom(symbol, strategy_id=None, exec_tf=None):
            raise RuntimeError("feed down")
        wd = WatchdogManager(boom, interval=0.05, hub=self.hub)
        self.addCleanup(wd.stop)
        wd.start("EURUSD", "candle_momentum", "M5")
        self.assertTrue(until(lambda: (self.hub.get("EURUSD", "candle_momentum", "M5") or {}).get("state") == "error"))
        self.assertIsNone(self.decision())


if __name__ == "__main__":
    unittest.main()
