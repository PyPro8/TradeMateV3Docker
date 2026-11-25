"""Dry run of the LIVE Phase 6 runner against the SIMULATED bridge.

This proves the runner's own logic (DEMO check, lock, gate, cleanup, abort-on-unknown). It proves NOTHING about the
real broker: that needs `python -m trademate.broker.live_phase6_test` on your machine with MT5 open.
"""
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.test_phase6 import Phase6Base
from trademate.broker import live_phase6_test as live


class LiveRunnerDryRun(Phase6Base):
    def setUp(self):
        super().setUp()
        self.data = Path(self._tmp.name)

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = live.main(list(argv) + ["--yes"], app_module=app_module, input_fn=lambda *_: "", data_dir=self.data)
        return code, out.getvalue()

    def test_full_run_passes_cleans_up_and_logs(self):
        # a pre-existing position must survive untouched
        self.bridge.positions[900] = {"symbol": "EURUSD", "type": "buy", "volume": 0.01, "price_open": 1.1, "sl": 1.09,
                                      "tp": 1.12, "time": 0, "magic": 0, "comment": "manual"}
        self.bridge.write_snapshot()
        code, out = self.run_main("EURUSD")
        self.assertEqual(code, 0, out)
        self.assertIn("No test-created positions remain open.", out)
        self.assertIn("pre-existing positions untouched", out)
        self.assertEqual(list(self.client.get("/api/broker").get_json()["positions"])[0]["ticket"], 900)
        self.assertEqual(len(self.client.get("/api/broker").get_json()["positions"]), 1)
        self.assertEqual([c for c in self.bridge.commands_seen if c.get("cmd") == "close" and int(c["ticket"]) == 900], [])
        logs = list((self.data / "live_tests").glob("phase6-*.jsonl"))
        self.assertEqual(len(logs), 1)
        self.assertFalse((self.data / "live_phase6.lock").exists())      # lock released

    def test_second_run_is_refused_while_the_lock_is_held(self):
        (self.data / "live_phase6.lock").write_text("held")
        code, out = self.run_main("EURUSD")
        self.assertEqual(code, 1)
        self.assertIn("another live test run", out)
        self.assertEqual([c for c in self.bridge.commands_seen if c.get("cmd") == "open"], [])

    def test_non_demo_account_sends_no_order(self):
        real = self.adapter.get_account()
        fake = real.__class__(**{**real.to_dict(), "environment": "real"})
        with mock.patch.object(self.adapter, "get_account", return_value=fake):
            code, out = self.run_main("EURUSD")
        self.assertEqual(code, 1)
        self.assertEqual([c for c in self.bridge.commands_seen if c.get("cmd") == "open"], [])

    def test_unknown_outcome_aborts_without_retry_and_reports(self):
        self.bridge.swallow_commands = True
        code, out = self.run_main("EURUSD")
        self.assertEqual(code, 1)
        self.assertIn("RUN ABORTED", out)
        self.assertEqual(len([c for c in self.bridge.commands_seen if c.get("cmd") == "open"]), 1)   # exactly one, never retried

    def test_risk_engine_refusal_skips_instead_of_bypassing(self):
        from trademate.risk import RiskRules
        self.set_rules(RiskRules(max_risk_per_trade_percent=0.0001, max_daily_loss_percent=3.0,
                                 max_total_active_risk_percent=3.0, max_positions=3))
        code, out = self.run_main("EURUSD")
        self.assertEqual([c for c in self.bridge.commands_seen if c.get("cmd") == "open"], [])
        logs = "".join(p.read_text(encoding="utf-8") for p in (self.data / "live_tests").glob("*.jsonl"))
        self.assertIn("NOT bypassed", out + logs)

    def test_runner_refuses_to_touch_tickets_it_did_not_create(self):
        run = live.LiveRun(app_module, live.Recorder(self.data / "x.jsonl"), "EURUSD")
        run.close(12345)
        self.assertEqual([c for c in self.bridge.commands_seen if c.get("cmd") == "close"], [])
        self.assertEqual(run.rec.count(live.FAIL), 1)


if __name__ == "__main__":
    unittest.main()
