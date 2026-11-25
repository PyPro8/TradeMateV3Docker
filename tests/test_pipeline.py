"""Strategy -> Risk -> Execution pipeline (controlled mode).

SAFETY: every order in this file goes to tests/fake_bridge.py (an in-memory stand-in for the MT5 Expert Advisor) through the real
adapter. No MT5, no network, no real order is possible. Simulated tests do NOT prove live broker behaviour; see docs/STRATEGY_TO_EXECUTION_PIPELINE.md.

    python3 -m unittest tests.test_pipeline -v
"""

import copy
import dataclasses
import inspect
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from tests.fake_bridge import FakeBridge
from tests.mtf_fixtures import NOW, ReadOnlyFeed, buy_rows, sell_rows
from trademate import pipeline
from trademate.broker import create_adapter
from trademate.broker.base import ConnectionState, ConnectionStatus, Position
from trademate.config import AppSettings, Mt5Settings, PipelineSettings, Settings, StrategySettings, TradingSettings
from trademate.risk import RiskRules, RiskRulesStore
from trademate.risk.engine import ACCEPTED, REJECTED, TradeProposal, evaluate_trade
from trademate.strategy import service

TFS = ("H1", "M5")                       # the only timeframes either selectable strategy reads
STAMP = {"id": "choch", "name": "CHoCH Trading", "version": "1.0.0", "rule_id": "CHOCH-H1M5-v1", "timeframes": ["H1", "M5"],
         "emits_choch_alerts": True, "choch_alert_timeframes": ["M5"]}


def strategy_payload(decision="BUY", entry=1.10012, sl=1.0990, tp=1.1035, symbol="EURUSD", **over):
    """What Strategy Engine's `evaluate_symbol` returns for a qualified setup (same keys as the real engine)."""
    side = {"BUY": "bullish", "SELL": "bearish"}.get(decision, "bullish")
    rr = abs(tp - entry) / abs(entry - sl)
    result = {
        "symbol": symbol, "decision": decision, "direction": side, "setup_status": "confirmed" if decision in ("BUY", "SELL") else "developing",
        "selected_setup_family": "choch" if decision in ("BUY", "SELL") else None, "rule_id": "CHOCH-H1M5-v1", "strategy": dict(STAMP),
        "entry": {"price": entry, "basis": "test", "zone": {"lower": entry - 0.0005, "upper": entry + 0.0005, "source": "M15 fvg"}},
        "stop_loss": {"price": sl, "basis": "test"}, "take_profit_1": {"price": tp, "basis": "test", "rr": rr},
        "risk_reward": {"risk": abs(entry - sl), "reward_tp1": abs(tp - entry), "rr_tp1": rr, "min_rr": 1.5},
        "setup_invalidation": {"level": sl, "timeframe": "M5", "basis": "a closed M5 candle beyond the protected swing"},
        "gates": [{"name": "data", "passed": True, "evaluated": True, "detail": "ok"}, {"name": "target", "passed": True, "evaluated": True, "detail": "ok"}],
        "setup_candidates": [{"setup_family": "choch", "direction": side, "execution_eligible": True, "status": "qualified"}],
        "data": {tf: {"status": "ok", "reason": "", "newest_closed_time": 1_700_000_000 + i * 300} for i, tf in enumerate(TFS)},
        "clock": {"known": True, "now_server": 1_700_001_000.0}, "confirmation_evidence": ["test evidence"], "reasons": ["WATCH: waiting"],
    }
    if decision not in ("BUY", "SELL"):
        result.update(entry=None, stop_loss=None, take_profit_1=None, risk_reward=None, setup_invalidation=None)
    result.update(over)
    return {"ok": True, "result": result, "connection": "connected"}


def sell_payload(**over):
    return strategy_payload("SELL", entry=1.10000, sl=1.1012, tp=1.0966, **over)


class Holder:
    """The strategy 'feed' the routes see; tests change .payload between propose and execute."""

    def __init__(self, payload):
        self.payload, self.calls = payload, 0

    def __call__(self, symbol):
        self.calls += 1
        return copy.deepcopy(self.payload)


class PipelineBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.bridge = FakeBridge(tmp).start()
        self.addCleanup(self.bridge.stop)
        trading = TradingSettings(command_timeout_seconds=1.0, confirm_timeout_seconds=0.6)
        self.settings = Settings(app=AppSettings(data_dir=tmp), mt5=Mt5Settings(data_dir=tmp), trading=trading)
        self.adapter = create_adapter(self.settings)
        self.store = pipeline.ApprovalStore(tmp / "pipeline_log.jsonl")
        self.rules = RiskRulesStore(tmp / "risk_rules.json", RiskRules(1.0, 3.0, 3.0, 3))
        self.holder = Holder(strategy_payload())
        from trademate.strategies.selection import StrategySelectionStore
        self.selection = StrategySelectionStore(tmp / "strategy_selection.json", "choch")
        self.selection.on_change(app_module._on_strategy_change)
        for p in (mock.patch.object(app_module, "adapter", self.adapter), mock.patch.object(app_module, "settings", self.settings),
                  mock.patch.object(app_module, "pipeline_store", self.store), mock.patch.object(app_module, "risk_store", self.rules),
                  mock.patch.object(app_module, "_strategy_for_pipeline", self.holder), mock.patch.object(app_module, "strategy_store", self.selection)):
            p.start()
            self.addCleanup(p.stop)
        self.client = app.test_client()

    # ---- helpers ----
    def with_settings(self, **pipeline_over):
        new = dataclasses.replace(self.settings, pipeline=dataclasses.replace(self.settings.pipeline, **pipeline_over))
        p = mock.patch.object(app_module, "settings", new)
        p.start()
        self.addCleanup(p.stop)

    def propose(self, symbol="EURUSD", **extra):
        resp = self.client.post("/api/pipeline/propose", json={"symbol": symbol, **extra})
        return resp.get_json()

    def execute(self, approval_id, confirm=True, client=None):
        body = {"approval_id": approval_id}
        if confirm is not None:
            body["confirm"] = confirm
        return (client or self.client).post("/api/pipeline/execute", json=body).get_json()

    def approved(self, **extra):
        r = self.propose(**extra)
        self.assertTrue(r["approved"], r)
        return r

    def writes(self):
        """WRITE commands (open/modify/close) that reached the bridge. Read requests do not count."""
        return [c for c in self.bridge.commands_seen if c["cmd"] in ("open", "modify", "close")]

    def set_price(self, bid, ask):
        self.bridge.bid, self.bridge.ask = bid, ask
        self.bridge.write_snapshot()


# =====================================================================================================================
class QualificationContractTest(unittest.TestCase):
    """The strategy's JSON must be complete and consistent before anything else looks at it."""

    def test_valid_buy_qualifies_with_every_mandatory_field(self):
        q = pipeline.qualify(strategy_payload())
        self.assertTrue(q.qualified, q.reasons)
        p = q.proposal
        self.assertEqual((p.symbol, p.direction, p.setup_family), ("EURUSD", "buy", "choch"))
        for field in ("entry_reference", "stop_loss", "take_profit", "rr_tp1", "min_rr", "invalidation_level", "evaluated_at_server"):
            self.assertIsNotNone(getattr(p, field), field)
        self.assertEqual(set(p.candle_times), set(TFS))
        self.assertLess(p.stop_loss, p.entry_reference)
        self.assertLess(p.entry_reference, p.take_profit)

    def test_valid_sell_qualifies(self):
        q = pipeline.qualify(sell_payload())
        self.assertTrue(q.qualified, q.reasons)
        self.assertEqual(q.proposal.direction, "sell")
        self.assertGreater(q.proposal.stop_loss, q.proposal.entry_reference)

    def test_watch_and_wait_are_never_executable(self):
        for decision in ("WATCH", "WAIT"):
            with self.subTest(decision=decision):
                q = pipeline.qualify(strategy_payload(decision))
                self.assertFalse(q.qualified)
                self.assertEqual((q.code, q.decision), (pipeline.NOT_QUALIFIED, decision))
                self.assertIsNone(q.proposal)

    def test_a_buy_label_alone_is_not_enough(self):
        """Even a BUY with the mandatory evidence removed is refused with a specific reason."""
        cases = {"entry reference": lambda r: r.update(entry=None), "stop-loss": lambda r: r.update(stop_loss=None),
                 "take-profit": lambda r: r.update(take_profit_1=None), "risk-to-reward information": lambda r: r.update(risk_reward=None),
                 "invalidation condition": lambda r: r.update(setup_invalidation=None), "setup family": lambda r: r.update(selected_setup_family=None)}
        for missing, mutate in cases.items():
            with self.subTest(missing=missing):
                payload = strategy_payload()
                mutate(payload["result"])
                q = pipeline.qualify(payload)
                self.assertFalse(q.qualified)
                self.assertEqual(q.code, pipeline.PROPOSAL_INCOMPLETE)
                self.assertIn(missing, q.reasons[0])

    def test_non_finite_or_non_positive_prices_are_incomplete(self):
        for bad in (float("nan"), float("inf"), 0.0, -1.0, "1.1", None, True):
            with self.subTest(bad=bad):
                payload = strategy_payload()
                payload["result"]["stop_loss"]["price"] = bad
                self.assertEqual(pipeline.qualify(payload).code, pipeline.PROPOSAL_INCOMPLETE)

    def test_stale_or_unavailable_data_blocks_qualification(self):
        for status in ("stale", "unavailable", "insufficient", "defective"):
            with self.subTest(status=status):
                payload = strategy_payload()
                payload["result"]["data"]["H1"]["status"] = status
                payload["result"]["data"]["H1"]["reason"] = f"H1 is {status}"
                q = pipeline.qualify(payload)
                self.assertEqual(q.code, pipeline.DATA_UNUSABLE)
                self.assertIn("H1", q.reasons[0])

    def test_unknown_broker_clock_blocks_qualification(self):
        payload = strategy_payload()
        payload["result"]["clock"] = {"known": False, "now_server": None}
        self.assertEqual(pipeline.qualify(payload).code, pipeline.DATA_UNUSABLE)

    def test_missing_candle_timestamp_is_incomplete(self):
        payload = strategy_payload()
        payload["result"]["data"]["M5"]["newest_closed_time"] = None
        self.assertEqual(pipeline.qualify(payload).code, pipeline.PROPOSAL_INCOMPLETE)

    def test_levels_that_contradict_the_direction_are_inconsistent(self):
        self.assertEqual(pipeline.qualify(strategy_payload(sl=1.1010)).code, pipeline.PROPOSAL_INCONSISTENT)                  # BUY with the stop above entry
        self.assertEqual(pipeline.qualify(strategy_payload(tp=1.0950)).code, pipeline.PROPOSAL_INCONSISTENT)                  # BUY with the target below entry
        self.assertEqual(pipeline.qualify(strategy_payload("SELL", entry=1.1, sl=1.0990, tp=1.0966)).code, pipeline.PROPOSAL_INCONSISTENT)

    def test_reward_to_risk_below_the_strategys_own_minimum_is_inconsistent(self):
        payload = strategy_payload(tp=1.1015)         # RR ~1.26 but the strategy's own minimum is 1.5
        self.assertEqual(pipeline.qualify(payload).code, pipeline.PROPOSAL_INCONSISTENT)

    def test_direction_decision_mismatch_and_unconfirmed_status_are_refused(self):
        payload = strategy_payload()
        payload["result"]["direction"] = "bearish"
        self.assertEqual(pipeline.qualify(payload).code, pipeline.PROPOSAL_INCONSISTENT)
        payload = strategy_payload()
        payload["result"]["setup_status"] = "developing"
        self.assertEqual(pipeline.qualify(payload).code, pipeline.NOT_QUALIFIED)

    def test_a_failed_gate_or_a_non_eligible_candidate_is_refused(self):
        payload = strategy_payload()
        payload["result"]["gates"][1]["passed"] = False
        self.assertEqual(pipeline.qualify(payload).code, pipeline.NOT_QUALIFIED)
        payload = strategy_payload()
        payload["result"]["setup_candidates"][0]["execution_eligible"] = False
        self.assertEqual(pipeline.qualify(payload).code, pipeline.NOT_QUALIFIED)

    def test_garbage_input_is_not_qualified(self):
        for bad in (None, {}, {"ok": False}, {"ok": True, "result": None}, {"ok": True, "result": []}):
            self.assertFalse(pipeline.qualify(bad).qualified)


# =====================================================================================================================
class RealStrategyEngineContractTest(unittest.TestCase):
    """The ACTUAL Strategy Engine output, not a hand-made payload, must traverse qualification and the Risk Engine.

    Defect found in this task: StrategyResult.to_trade_proposal() carried no risk percentage and no size, and nothing in the application
    called it, so a qualified strategy setup could never be approved by the Risk Engine (it always said 'Strategy risk percentage is missing')."""

    class Broker(ReadOnlyFeed):
        """Read-only feed + the account/spec reads the pipeline needs. It has NO order method (touching one fails the test)."""

        def __init__(self, rows, bid, ask):
            super().__init__(rows)
            self._bid, self._ask = bid, ask

        def get_symbol_spec(self, symbol):
            from trademate.broker.base import SymbolInfo
            return SymbolInfo(name=symbol, description="", digits=2, point=0.01, spread_points=10, contract_size=1.0, volume_min=0.01, volume_max=100.0,
                              volume_step=0.01, currency_base="X", currency_profit="USD", bid=self._bid, ask=self._ask, trade_mode=4, tick_size=0.01,
                              tick_value=0.01, tick_value_profit=0.01, tick_value_loss=0.01)

        def get_account(self):
            from trademate.broker.base import AccountInfo
            return AccountInfo(1, "srv", "b", "demo", "USD", 100, 10000.0, 10000.0, 0.0, 10000.0, None, 0.0)

        def get_positions(self):
            return []

        def get_orders(self):
            return []

        def supports_pending_orders(self):
            return False

        def get_history(self, days=3, limit=500):
            return []

    def setUp(self):
        self.settings = dataclasses.replace(Settings(), strategy=StrategySettings(trigger_lookback=24))
        self.rules = RiskRules(1.0, 3.0, 3.0, 3)

    def run_pipeline(self, rows, bid, ask, **kw):
        feed = self.Broker(rows, bid, ask)
        evaluate = lambda s: service.evaluate_symbol(feed, s, self.settings, now_utc=NOW)
        store = pipeline.ApprovalStore()
        return pipeline.propose(feed, self.settings, self.rules, 100.0, "EURUSD", store, evaluate=evaluate, now=NOW, **kw), store

    def test_the_old_proposal_object_alone_could_never_be_approved(self):
        feed = self.Broker(buy_rows(), 140.9, 141.0)
        result = service.evaluate_symbol(feed, "EURUSD", self.settings, now_utc=NOW)["result"]
        self.assertEqual(result["decision"], "BUY")
        from trademate.risk.account_state import gather_state, to_spec
        proposal = TradeProposal("EURUSD", "buy", result["entry"]["price"], result["stop_loss"]["price"], result["take_profit_1"]["price"])   # what to_trade_proposal() produced
        decision = evaluate_trade(gather_state(feed, NOW), proposal, to_spec(feed.get_symbol_spec("EURUSD")), self.rules, 100.0)
        self.assertEqual(decision.decision, REJECTED)
        self.assertIn("Strategy risk percentage is missing", " ".join(decision.rejection_reasons))

    def test_real_buy_is_qualified_risk_approved_and_eligible(self):
        report, store = self.run_pipeline(buy_rows(), 140.95, 141.0)
        self.assertTrue(report.ok, (report.code, report.message, report.reasons))
        self.assertEqual(report.stages, {"strategy_qualified": True, "risk_approved": True, "execution_eligible": True, "order_submitted": None,
                                         "broker_accepted": None, "position_confirmed": None})
        a = report.approval
        self.assertEqual((a["direction"], a["stop_loss"], a["take_profit"]), ("buy", 134.8, 151.05))
        self.assertAlmostEqual(a["summary"]["risk_percent"], 1.0, delta=0.05)       # sized for 1% of equity, rounded DOWN
        self.assertLessEqual(a["summary"]["risk_percent"], 1.0 + 1e-9)

    def test_real_sell_is_qualified_risk_approved_and_eligible(self):
        report, _ = self.run_pipeline(sell_rows(), 159.0, 159.05)
        self.assertTrue(report.ok, (report.code, report.message, report.reasons))
        self.assertEqual((report.approval["direction"], report.approval["stop_loss"]), ("sell", 165.2))

    def test_real_buy_is_not_approved_when_the_price_has_run_away(self):
        report, _ = self.run_pipeline(buy_rows(), 143.0, 143.05)       # ~2 vs a 6.2 stop: beyond 25% of the stop distance
        self.assertFalse(report.ok)
        self.assertEqual(report.code, pipeline.ENTRY_DRIFTED)
        self.assertEqual((report.stages["strategy_qualified"], report.stages["risk_approved"], report.stages["execution_eligible"]), (True, True, False))


# =====================================================================================================================
class EndToEndBuySellTest(PipelineBase):
    def test_valid_buy_is_approved_confirmed_and_reaches_the_execution_boundary(self):
        r = self.approved()
        self.assertEqual(self.writes(), [], "proposing must not send anything")
        self.assertFalse(r["order_sent"])
        self.assertEqual((r["approval"]["direction"], r["approval"]["volume"]), ("buy", 0.08))
        self.assertLessEqual(r["approval"]["summary"]["risk_percent"], 1.0)
        with mock.patch.object(self.adapter, "place_order", wraps=self.adapter.place_order) as spy, \
                mock.patch.object(pipeline, "gate_order", wraps=pipeline.gate_order) as gate:
            out = self.execute(r["approval"]["id"])
        gate.assert_called_once()                                                    # the existing gate re-judged the exact order
        spy.assert_called_once_with("EURUSD", "buy", 0.08, 1.0990, 1.1035)           # the existing MT5 pathway, exact approved values
        self.assertEqual((out["state"], out["outcome"], out["order_submitted"]), ("executed", "executed", True))
        self.assertEqual(out["stages"], {"order_submitted": True, "broker_accepted": True, "position_confirmed": True})
        self.assertTrue(out["placement_check"]["matches"], out["placement_check"])
        self.assertEqual(len(self.writes()), 1)
        pos = list(self.bridge.positions.values())
        self.assertEqual((len(pos), pos[0]["type"], pos[0]["volume"], pos[0]["sl"], pos[0]["tp"]), (1, "buy", 0.08, 1.0990, 1.1035))
        self.assertEqual(self.store.get(r["approval"]["id"]).status, "consumed")
        self.assertEqual(self.store.get(r["approval"]["id"]).outcome["outcome"], "executed")

    def test_valid_sell_is_approved_and_executed(self):
        self.holder.payload = sell_payload()
        r = self.approved()
        self.assertEqual((r["approval"]["direction"], r["approval"]["volume"]), ("sell", 0.08))
        out = self.execute(r["approval"]["id"])
        self.assertEqual(out["outcome"], "executed")
        pos = list(self.bridge.positions.values())[0]
        self.assertEqual((pos["type"], pos["sl"], pos["tp"]), ("sell", 1.1012, 1.0966))

    def test_requires_explicit_confirmation(self):
        r = self.approved()
        for confirm in (None, False, "true", 1, "yes"):
            with self.subTest(confirm=confirm):
                out = self.execute(r["approval"]["id"], confirm=confirm)
                self.assertEqual(out["refusal"]["code"], pipeline.CONFIRMATION_REQUIRED)
                self.assertFalse(out["order_submitted"])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.store.get(r["approval"]["id"]).status, "issued")          # still usable once actually confirmed

    def test_unknown_approval_is_refused(self):
        out = self.execute("does-not-exist")
        self.assertEqual(out["refusal"]["code"], pipeline.APPROVAL_UNKNOWN)
        self.assertEqual(self.writes(), [])


class NotExecutableTest(PipelineBase):
    def test_watch_and_wait_never_reach_risk_or_execution(self):
        for decision in ("WATCH", "WAIT"):
            with self.subTest(decision=decision):
                self.holder.payload = strategy_payload(decision)
                with mock.patch.object(pipeline, "evaluate_trade") as risk:
                    r = self.propose()
                risk.assert_not_called()
                self.assertFalse(r["approved"])
                self.assertEqual(r["code"], pipeline.NOT_QUALIFIED)
                self.assertIsNone(r["approval"])
                self.assertEqual(r["stages"]["strategy_qualified"], False)
                self.assertIsNone(r["stages"]["risk_approved"])
        self.assertEqual(self.writes(), [])

    def test_incomplete_proposal_is_rejected_with_the_correct_reason(self):
        self.holder.payload["result"]["stop_loss"] = None
        r = self.propose()
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.PROPOSAL_INCOMPLETE))
        self.assertIn("stop-loss", r["message"])

    def test_stale_market_data_is_rejected(self):
        self.holder.payload["result"]["data"]["M5"].update(status="stale", reason="M5 is stale (test)")
        r = self.propose()
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.DATA_UNUSABLE))
        self.assertIn("M5", r["reasons"][0])

    def test_invalid_stops_from_the_strategy_are_rejected(self):
        self.holder.payload = strategy_payload(sl=1.1020)            # BUY with the stop ABOVE the entry
        r = self.propose()
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.PROPOSAL_INCONSISTENT))

    def test_a_stop_the_broker_considers_too_close_is_refused_before_anything_is_sent(self):
        self.holder.payload = strategy_payload(sl=1.10005, tp=1.1035)        # 7 points from the ask: inside the symbol's stops level
        r = self.propose()
        if not r["approved"]:                                                # the Risk Engine or eligibility may already refuse it: also correct
            self.assertEqual(self.writes(), [])
            return
        out = self.execute(r["approval"]["id"])
        self.assertEqual(out["outcome"], "execution_refused")
        self.assertFalse(out["order_submitted"])
        self.assertEqual(out["stages"], {"order_submitted": False, "broker_accepted": False, "position_confirmed": False})
        self.assertEqual(self.writes(), [])


class RiskPolicyTest(PipelineBase):
    def test_a_correctly_sized_smaller_position_is_used_when_the_size_exceeds_the_order_maximum(self):
        with mock.patch.object(app_module, "_max_order_volume", return_value=0.05):
            r = self.propose()
        self.assertTrue(r["approved"], r)
        self.assertEqual(r["approval"]["volume"], 0.05)
        self.assertTrue(r["risk"]["volume_capped"])
        self.assertLess(r["approval"]["summary"]["risk_percent"], 1.0)          # LESS than the 1% requested, never more
        self.assertTrue(any("capped" in w for w in r["warnings"]))

    def test_a_size_below_the_broker_minimum_is_rejected_never_rounded_up(self):
        self.holder.payload = strategy_payload(sl=1.0500, tp=1.2000, entry=1.10012)       # huge stop: allowed size 0.002 lots < 0.01 minimum
        r = self.propose()
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.RISK_REJECTED))
        self.assertIn("minimum", " ".join(r["reasons"]))
        self.assertEqual(r["stages"]["risk_approved"], False)

    def test_a_requested_risk_above_the_rules_is_rejected_not_clamped(self):
        r = self.propose(risk_percent=2.0)
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.RISK_REJECTED))
        self.assertIn("maximum risk per trade", " ".join(r["reasons"]))

    def test_the_daily_loss_limit_blocks_the_pipeline(self):
        from trademate import execution_gate
        original = execution_gate.gather_state

        def losing(adapter, *a, **k):
            return dataclasses.replace(original(adapter, *a, **k), todays_closed_pl=-40.0, start_of_day_balance=1000.0)     # 4% >= the 3% limit

        with mock.patch.object(execution_gate, "gather_state", losing):
            r = self.propose()
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.RISK_REJECTED))
        self.assertIn("Daily loss limit reached", " ".join(r["reasons"]))
        self.assertTrue(r["risk"]["trading_locked"])

    def test_unknown_daily_loss_fails_closed(self):
        from trademate import execution_gate
        original = execution_gate.gather_state
        with mock.patch.object(execution_gate, "gather_state", lambda a, *x, **k: dataclasses.replace(original(a, *x, **k), todays_closed_pl=None)):
            r = self.propose()
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.RISK_REJECTED))
        self.assertIn("cannot be determined", " ".join(r["reasons"]))

    def test_position_limit_is_enforced(self):
        self.bridge.positions[7] = {"symbol": "EURUSD", "type": "buy", "volume": 0.01, "price_open": 1.1, "sl": 1.09, "tp": 1.12}
        self.bridge.write_snapshot()
        self.rules.save(RiskRules(1.0, 3.0, 3.0, 1))
        r = self.propose()
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.RISK_REJECTED))
        self.assertIn("simultaneous position", " ".join(r["reasons"]).lower())

    def test_invalid_symbol_specifications_are_rejected(self):
        self.holder.payload = strategy_payload(entry=1.1, sl=1.09, tp=1.13, symbol="CLOSED1")     # trade_mode 0: trading disabled by the broker
        r = self.propose("CLOSED1")
        self.assertFalse(r["approved"])
        self.assertEqual(r["code"], pipeline.RISK_REJECTED)
        self.assertIn("disabled", " ".join(r["reasons"]))
        self.holder.payload = strategy_payload(symbol="NOPE")
        r = self.propose("NOPE")
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.MARKET_UNAVAILABLE))
        self.assertEqual(self.writes(), [])


class EligibilityTest(PipelineBase):
    def test_price_drift_beyond_the_limit_blocks_execution_eligibility(self):
        self.set_price(1.10040, 1.10052)                      # ask +40 points vs the 1.10012 reference; limit = 25% of 112 points = 28
        r = self.propose()
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.ENTRY_DRIFTED))
        self.assertEqual((r["stages"]["risk_approved"], r["stages"]["execution_eligible"]), (True, False))

    def test_a_spread_as_large_as_the_stop_blocks_execution_eligibility(self):
        self.set_price(1.09890, 1.10012)                      # 122-point spread >= the ~112-point stop: the stop is touched at fill
        r = self.propose()
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.SPREAD_TOO_WIDE))
        self.assertEqual((r["stages"]["risk_approved"], r["stages"]["execution_eligible"]), (True, False))
        self.assertIn("execution-cost policy", r["message"])
        self.assertNotIn("rejected by the Risk Engine", r["message"].lower())

    def test_a_62_point_spread_vs_a_112_point_stop_is_no_longer_blocked_by_a_universal_ratio(self):
        self.set_price(1.09950, 1.10012)                      # was rejected by the old 0.25 x stop rule; now workable and merely warned about
        r = self.propose()
        self.assertNotEqual(r.get("code"), pipeline.SPREAD_TOO_WIDE)

    def test_trading_disabled_blocks_approval(self):
        new = dataclasses.replace(self.settings, trading=dataclasses.replace(self.settings.trading, enabled=False))
        with mock.patch.object(app_module, "settings", new):
            r = self.propose()
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.TRADING_DISABLED))

    def test_disconnected_terminal_means_no_approval_and_no_execution(self):
        down = ConnectionStatus(ConnectionState.DISCONNECTED, "MT5 is not running")
        with mock.patch.object(self.adapter, "get_connection_status", return_value=down):
            r = self.propose()
        self.assertEqual((r["approved"], r["code"]), (False, pipeline.MARKET_UNAVAILABLE))
        r = self.approved()
        with mock.patch.object(self.adapter, "get_connection_status", return_value=down):
            out = self.execute(r["approval"]["id"])
        self.assertEqual(out["refusal"]["code"], pipeline.MARKET_UNAVAILABLE)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.store.get(r["approval"]["id"]).status, "issued")            # nothing changed in the market: still valid until expiry

    def test_candle_momentum_is_not_blocked_by_generic_fraction_of_stop_drift(self):
        cfg = PipelineSettings(max_entry_drift_stop_fraction=0.25)
        core = {"direction": "buy", "stop_loss": 99.0, "take_profit": 130.0}
        # Drift is 9 against a 13-unit stop (the screenshot class of rejection). CM gets a
        # fresh quote/levels and remains subject to geometry, spread and RR checks.
        problems = pipeline.eligibility_problems(core, 100.0, 1.0, 109.0, 108.0, 109.0, cfg,
                                                 strategy_id="candle_momentum")
        self.assertEqual(problems, [])

    def test_other_strategies_keep_the_generic_entry_drift_guard(self):
        cfg = PipelineSettings(max_entry_drift_stop_fraction=0.25)
        core = {"direction": "buy", "stop_loss": 99.0, "take_profit": 130.0}
        problems = pipeline.eligibility_problems(core, 100.0, 1.0, 109.0, 108.0, 109.0, cfg,
                                                 strategy_id="choch")
        self.assertEqual(problems[0][0], pipeline.ENTRY_DRIFTED)

    def test_eligibility_helper_is_a_pure_function_of_levels_and_prices(self):
        cfg = PipelineSettings()
        core = {"direction": "buy", "stop_loss": 99.0, "take_profit": 104.0}
        self.assertEqual(pipeline.eligibility_problems(core, 100.0, 1.5, 100.1, 100.0, 100.1, cfg), [])
        self.assertEqual(pipeline.eligibility_problems(core, 100.0, 1.5, 98.9, 98.8, 98.9, cfg)[0][0], pipeline.ENTRY_DRIFTED)     # through the stop
        self.assertEqual(pipeline.eligibility_problems(core, 100.0, 1.5, 104.1, 104.0, 104.1, cfg)[0][0], pipeline.ENTRY_DRIFTED)  # through the target
        self.assertEqual(pipeline.eligibility_problems(core, 100.0, 1.5, 100.2, 100.1, 100.2, cfg), [])
        self.assertEqual(pipeline.eligibility_problems(core, 100.0, 4.5, 100.1, 100.0, 100.1, cfg)[0][0], pipeline.RR_DEGRADED)


class StaleApprovalTest(PipelineBase):
    def assert_refused(self, out, code):
        self.assertEqual(out["refusal"]["code"], code, out)
        self.assertFalse(out["order_submitted"])
        self.assertEqual(self.writes(), [])

    def test_an_expired_approval_is_refused(self):
        self.with_settings(approval_ttl_seconds=0.05)
        r = self.approved()
        time.sleep(0.12)
        self.assert_refused(self.execute(r["approval"]["id"]), pipeline.APPROVAL_EXPIRED)
        self.assertEqual(self.store.get(r["approval"]["id"]).status, "expired")

    def test_a_new_position_in_the_account_invalidates_the_approval(self):
        r = self.approved()
        self.bridge.positions[9] = {"symbol": "EURUSD", "type": "sell", "volume": 0.01, "price_open": 1.1, "sl": 1.11, "tp": 1.09}
        self.bridge.write_snapshot()
        self.assert_refused(self.execute(r["approval"]["id"]), pipeline.STATE_CHANGED)
        self.assertEqual(self.store.get(r["approval"]["id"]).status, "invalidated")
        self.assert_refused(self.execute(r["approval"]["id"]), pipeline.APPROVAL_INVALIDATED)          # and it stays dead

    def test_a_changed_stop_on_an_existing_position_invalidates_the_approval(self):
        self.bridge.positions[9] = {"symbol": "EURUSD", "type": "sell", "volume": 0.01, "price_open": 1.1, "sl": 1.11, "tp": 1.09}
        self.bridge.write_snapshot()
        r = self.approved()
        self.bridge.positions[9]["sl"] = 1.12
        self.bridge.write_snapshot()
        self.assert_refused(self.execute(r["approval"]["id"]), pipeline.STATE_CHANGED)

    def test_a_pending_order_appearing_invalidates_the_approval(self):
        r = self.approved()
        self.bridge.orders[55] = {"symbol": "EURUSD", "type": "buy_limit", "volume": 0.01, "price_open": 1.09, "sl": 1.08, "tp": 1.12}
        self.bridge.write_snapshot()
        self.assert_refused(self.execute(r["approval"]["id"]), pipeline.STATE_CHANGED)

    def test_a_different_account_invalidates_the_approval(self):
        r = self.approved()
        other = {"login": 22222222, "server": "Headway-Demo", "currency": "USD"}
        with mock.patch.object(pipeline, "_account_identity", return_value=other):
            self.assert_refused(self.execute(r["approval"]["id"]), pipeline.ACCOUNT_CHANGED)

    def test_changed_risk_rules_invalidate_the_approval(self):
        r = self.approved()
        self.rules.save(RiskRules(0.5, 3.0, 3.0, 3))
        self.assert_refused(self.execute(r["approval"]["id"]), pipeline.RULES_CHANGED)

    def test_a_material_change_to_the_proposal_invalidates_the_approval(self):
        for label, mutate in {"stop moved": lambda p: p["result"]["stop_loss"].update(price=1.0985),
                              "target moved": lambda p: p["result"]["take_profit_1"].update(price=1.1040),
                              "setup family changed": lambda p: p["result"].update(selected_setup_family="trend_continuation")}.items():
            with self.subTest(label):
                self.holder.payload = strategy_payload()
                r = self.approved()
                self.holder.payload = strategy_payload()
                mutate(self.holder.payload)
                for c in self.holder.payload["result"]["setup_candidates"]:
                    c["setup_family"] = self.holder.payload["result"]["selected_setup_family"]
                self.assert_refused(self.execute(r["approval"]["id"]), pipeline.PROPOSAL_CHANGED)

    def test_a_new_reference_entry_alone_is_not_a_material_change(self):
        r = self.approved()
        self.holder.payload = strategy_payload(entry=1.10020)         # the next M5 close moved the reference; thesis (stop, target, family) is the same
        out = self.execute(r["approval"]["id"])
        self.assertEqual(out["outcome"], "executed", out)

    def test_a_setup_that_stopped_qualifying_is_refused(self):
        r = self.approved()
        self.holder.payload = strategy_payload("WATCH")
        self.assert_refused(self.execute(r["approval"]["id"]), pipeline.NO_LONGER_QUALIFIED)

    def test_data_going_stale_between_approval_and_execution_is_refused(self):
        r = self.approved()
        self.holder.payload["result"]["data"]["H1"]["status"] = "stale"
        self.assert_refused(self.execute(r["approval"]["id"]), pipeline.NO_LONGER_QUALIFIED)

    def test_price_drift_after_approval_is_refused(self):
        r = self.approved()
        self.set_price(1.10060, 1.10072)
        self.assert_refused(self.execute(r["approval"]["id"]), pipeline.ENTRY_DRIFTED)

    def test_a_moderate_drift_that_pushes_the_fixed_volume_over_the_risk_limit_is_refused_then_resized_by_a_new_proposal(self):
        r = self.approved()
        self.assertEqual(r["approval"]["volume"], 0.08)
        self.set_price(1.10020, 1.10032)                         # +20 points: inside the drift limit (28) but 0.08 lots now risks ~1.06%
        self.assert_refused(self.execute(r["approval"]["id"]), pipeline.RISK_REVALIDATION_FAILED)
        again = self.approved()
        self.assertEqual(again["approval"]["volume"], 0.07)       # sized again from the new live price, rounded DOWN
        self.assertLessEqual(again["approval"]["summary"]["risk_percent"], 1.0)

    def test_the_risk_gate_independently_refuses_when_the_account_can_no_longer_afford_it(self):
        r = self.approved()
        from trademate import execution_gate
        original = execution_gate.gather_state
        with mock.patch.object(execution_gate, "gather_state", lambda a, *x, **k: dataclasses.replace(original(a, *x, **k), todays_closed_pl=-40.0,
                                                                                                    start_of_day_balance=1000.0)):
            self.assert_refused(self.execute(r["approval"]["id"]), pipeline.RISK_REVALIDATION_FAILED)


class BrokerOutcomeTest(PipelineBase):
    def lose_the_answer_of_the_next_order(self, flag):
        """Switch a bridge fault on only when the ORDER is sent, so the pre-send reads (spec, account, history) still work."""
        real = self.adapter.place_order

        def faulty(*a, **k):
            setattr(self.bridge, flag, True)
            return real(*a, **k)

        p = mock.patch.object(self.adapter, "place_order", faulty)
        p.start()
        self.addCleanup(p.stop)

    def test_the_terminal_dying_before_the_send_keeps_the_approval_and_sends_nothing(self):
        r = self.approved()
        self.bridge.ignore_commands = True                       # every read now fails too
        out = self.execute(r["approval"]["id"])
        self.assertFalse(out["order_submitted"])
        self.assertEqual(out["refusal"]["code"], pipeline.MARKET_UNAVAILABLE)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.store.get(r["approval"]["id"]).status, "issued")

    def test_insufficient_margin_is_reported_exactly_as_the_broker_said_it(self):
        r = self.approved()
        self.bridge.reject_with = (10019, "No money")
        out = self.execute(r["approval"]["id"])
        self.assertEqual((out["state"], out["outcome"], out["order_submitted"]), ("failed", "broker_rejected", True))
        self.assertEqual(out["broker"]["retcode"], 10019)
        self.assertIn("No money", out["broker"]["message"])
        self.assertEqual(out["stages"], {"order_submitted": True, "broker_accepted": False, "position_confirmed": False})
        self.assertEqual(len(self.bridge.positions), 0)

    def test_invalid_stops_rejection_from_the_broker_is_reported_accurately(self):
        r = self.approved()
        self.bridge.reject_with = (10016, "Invalid stops")
        out = self.execute(r["approval"]["id"])
        self.assertEqual((out["outcome"], out["broker"]["retcode"]), ("broker_rejected", 10016))
        self.assertIn("Invalid stops", out["broker"]["message"])

    def test_a_lost_answer_is_reconciled_with_the_account_and_never_resent(self):
        r = self.approved()
        self.lose_the_answer_of_the_next_order("silent")         # MT5 executes, the answer never arrives
        out = self.execute(r["approval"]["id"])
        self.assertEqual(len(self.bridge.positions), 1)
        self.assertEqual(out["outcome"], "executed", out)       # settled by reading the real account, not by guessing
        self.assertEqual(out["stages"]["position_confirmed"], True)
        self.assertEqual(len(self.writes()), 1)
        again = self.execute(r["approval"]["id"])                # the approval is used up: no second order
        self.assertEqual(again["refusal"]["code"], pipeline.APPROVAL_USED)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(len(self.bridge.positions), 1)

    def test_a_command_that_was_taken_but_not_executed_is_reported_as_not_executed(self):
        r = self.approved()
        self.lose_the_answer_of_the_next_order("swallow_commands")
        out = self.execute(r["approval"]["id"])
        self.assertIn(out["outcome"], ("not_executed", "unknown"), out)
        self.assertEqual(len(self.bridge.positions), 0)
        if out["outcome"] == "unknown":
            self.assertEqual(out["stages"], {"order_submitted": None, "broker_accepted": None, "position_confirmed": None})
            self.assertEqual(out["automatic_resubmission"], "DISABLED")
        self.assertEqual(self.store.get(r["approval"]["id"]).status, "consumed")

    def test_an_unreachable_terminal_sends_nothing_and_is_not_called_a_failure_of_the_broker(self):
        r = self.approved()
        self.lose_the_answer_of_the_next_order("ignore_commands")
        out = self.execute(r["approval"]["id"])
        self.assertEqual(out["outcome"], "connection_failed")
        self.assertEqual(out["stages"]["position_confirmed"], False)
        self.assertEqual(len(self.bridge.positions), 0)

    def test_an_unresolved_order_blocks_an_identical_new_one(self):
        """MT5 executed the order but the answer was lost AND the account cannot be read to settle it: outcome UNKNOWN, no blind retry."""
        from trademate.errors import BrokerUnavailableError
        r = self.approved()
        self.lose_the_answer_of_the_next_order("silent")
        p = mock.patch.object(self.adapter, "reconcile", side_effect=BrokerUnavailableError("MT5 is not answering"))
        p.start()
        self.addCleanup(p.stop)
        first = self.execute(r["approval"]["id"])
        self.assertEqual((first["outcome"], first["order_submitted"]), ("unknown", None), first)
        self.assertEqual(first["stages"], {"order_submitted": None, "broker_accepted": None, "position_confirmed": None})
        self.assertEqual(first["automatic_resubmission"], "DISABLED")
        self.assertEqual(len(self.writes()), 1)
        self.bridge.silent = False
        r2 = self.propose()                                       # a fresh proposal for the same trade IS approved by the Risk Engine ...
        self.assertTrue(r2["approved"], r2)
        second = self.execute(r2["approval"]["id"])               # ... but the execution layer refuses to send an identical order while one is unresolved
        self.assertFalse(second["order_submitted"], second)
        self.assertEqual(len(self.writes()), 1)                   # exactly one order ever reached MT5
        self.assertEqual(len(self.bridge.positions), 1)


class DuplicateExecutionTest(PipelineBase):
    def test_the_same_approval_cannot_be_executed_twice(self):
        r = self.approved()
        first = self.execute(r["approval"]["id"])
        second = self.execute(r["approval"]["id"])
        self.assertEqual(first["outcome"], "executed")
        self.assertEqual(second["refusal"]["code"], pipeline.APPROVAL_USED)
        self.assertEqual((len(self.writes()), len(self.bridge.positions)), (1, 1))

    def test_concurrent_attempts_with_one_approval_place_exactly_one_order(self):
        r = self.approved()
        results, barrier = [], threading.Barrier(4)

        def attempt():
            client = app.test_client()
            barrier.wait()
            results.append(self.execute(r["approval"]["id"], client=client))

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(len(results), 4)
        self.assertEqual(sum(1 for x in results if x.get("outcome") == "executed"), 1, results)
        self.assertEqual((len(self.writes()), len(self.bridge.positions)), (1, 1))

    def test_store_consume_is_single_use(self):
        a = self.store.issue(created_at=0, expires_at=1e12, symbol="X", direction="buy", volume=0.01, stop_loss=1, take_profit=3, entry_live=2,
                             core={}, account={}, rules={}, state_signature="", summary={})
        self.assertTrue(self.store.consume(a.id))
        self.assertFalse(self.store.consume(a.id))
        self.assertFalse(self.store.consume("nope"))
        self.assertFalse(self.store.consume(None))


class StageAndPlacementTest(unittest.TestCase):
    def test_six_stages_are_distinct_states(self):
        self.assertEqual(len(set(pipeline.STAGES)), 6)
        self.assertEqual(pipeline.STAGES[0], "strategy_qualified")
        self.assertEqual(pipeline.STAGES[-1], "position_confirmed")

    def test_an_accepted_order_is_not_proof_of_a_position(self):
        self.assertEqual(pipeline.stage_states("accepted_unconfirmed"), {"order_submitted": True, "broker_accepted": True, "position_confirmed": None})
        self.assertIsNone(pipeline.stage_states("unknown")["order_submitted"])

    def test_every_outcome_maps_to_stage_states(self):
        for outcome in ("executed", "accepted_unconfirmed", "broker_rejected", "not_executed", "unknown", "execution_refused", "connection_failed",
                        "invalid_request", "risk_rejected"):
            self.assertEqual(set(pipeline.stage_states(outcome)), {"order_submitted", "broker_accepted", "position_confirmed"}, outcome)

    def plan(self):
        return pipeline.ExecutionPlan("a", "EURUSD", "buy", 0.08, 1.0990, 1.1035, 1.10012, 1e-5)

    def pos(self, **over):
        base = dict(ticket=1, symbol="EURUSD", direction="buy", volume=0.08, price_open=1.1, price_current=1.1, stop_loss=1.0990, take_profit=1.1035,
                    profit=0.0, swap=0.0)
        return Position(**{**base, **over})

    def test_matching_position_passes_the_placement_check(self):
        self.assertTrue(pipeline.verify_placement(self.plan(), self.pos())["matches"])

    def test_a_broker_that_placed_something_different_is_reported(self):
        for label, over in {"volume": dict(volume=0.1), "stop-loss": dict(stop_loss=1.0980), "take-profit": dict(take_profit=None),
                            "direction": dict(direction="sell"), "symbol": dict(symbol="GBPUSD")}.items():
            with self.subTest(label):
                check = pipeline.verify_placement(self.plan(), self.pos(**over))
                self.assertFalse(check["matches"])
                self.assertIn(label, " ".join(check["discrepancies"]))
        self.assertIsNone(pipeline.verify_placement(self.plan(), None)["matches"])


class NoParallelExecutionPathTest(unittest.TestCase):
    def test_the_pipeline_module_cannot_send_orders(self):
        import ast
        calls = set()
        for node in ast.walk(ast.parse(inspect.getsource(pipeline))):
            if isinstance(node, ast.Call):
                f = node.func
                calls.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
        for forbidden in ("place_order", "place_pending_order", "cancel_pending_order", "close_position", "modify_position", "modify_pending_order",
                          "send", "_run", "_write", "reconcile"):
            self.assertNotIn(forbidden, calls, forbidden)

    def test_app_routes_use_the_existing_adapter_pathway_only(self):
        src = inspect.getsource(app_module.api_pipeline_execute)
        self.assertIn("adapter.place_order(", src)
        self.assertIn("_execute(", src)
        self.assertIn("prepare_execution(", src)


class ManualTradingStillWorksTest(PipelineBase):
    def test_manual_order_endpoint_is_unchanged(self):
        review = self.client.post("/api/order/review", json={"symbol": "EURUSD", "side": "buy", "volume": 0.05, "sl": 1.0990, "tp": 1.1035}).get_json()
        self.assertTrue(review["approved"], review)
        out = self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 0.05, "sl": 1.0990, "tp": 1.1035}).get_json()
        self.assertEqual(out["outcome"], "executed", out)
        self.assertEqual(len(self.bridge.positions), 1)

    def test_manual_risk_rejection_is_unchanged(self):
        out = self.client.post("/api/order", json={"symbol": "EURUSD", "side": "buy", "volume": 5.0, "sl": 1.0990, "tp": 1.1035}).get_json()
        self.assertEqual(out["outcome"], "risk_rejected")
        self.assertEqual(self.writes(), [])


class ApprovalLogTest(PipelineBase):
    def test_every_stage_of_an_approval_is_logged(self):
        r = self.approved()
        self.execute(r["approval"]["id"])
        lines = (Path(self._tmp.name) / "pipeline_log.jsonl").read_text().splitlines()
        events = [__import__("json").loads(x)["event"] for x in lines]
        self.assertEqual(events, ["approval_issued", "execution_outcome"])

    def test_a_logging_failure_never_blocks_a_trade(self):
        r = self.approved()
        blocker = Path(self._tmp.name) / "a_file"
        blocker.write_text("x")
        self.store._log_path = blocker / "pipeline_log.jsonl"          # its parent is a FILE: every write raises OSError
        out = self.execute(r["approval"]["id"])
        self.assertEqual(out["outcome"], "executed")


class RouteValidationTest(PipelineBase):
    def test_bad_requests_are_rejected_without_touching_mt5(self):
        for body in ({}, {"symbol": ""}, {"symbol": 5}, {"symbol": "X" * 40}, {"symbol": "EURUSD", "risk_percent": 0}, {"symbol": "EURUSD", "risk_percent": "1"},
                     {"symbol": "EURUSD", "risk_percent": True}, {"symbol": "EURUSD", "risk_percent": 101}):
            with self.subTest(body=body):
                resp = self.client.post("/api/pipeline/propose", json=body)
                self.assertEqual(resp.status_code, 400)
                self.assertFalse(resp.get_json()["approved"])
        self.assertEqual(self.holder.calls, 0)

    def test_a_crash_inside_the_pipeline_is_never_an_approval(self):
        with mock.patch.object(pipeline, "evaluate_trade", side_effect=RuntimeError("boom")):
            resp = self.client.post("/api/pipeline/propose", json={"symbol": "EURUSD"})
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.get_json()["approved"])
        self.assertFalse(resp.get_json()["order_sent"])

    def test_cross_origin_posts_are_refused(self):
        resp = self.client.post("/api/pipeline/execute", json={"approval_id": "x", "confirm": True}, headers={"Origin": "http://evil.example"})
        self.assertFalse(resp.get_json()["order_submitted"])
        self.assertEqual(resp.get_json()["outcome"], "invalid_request")

    def test_config_section_is_validated(self):
        from trademate.config import ConfigError, load_config
        path = Path(self._tmp.name) / "s.toml"
        path.write_text("[pipeline]\napproval_ttl_seconds = 0\n")
        with self.assertRaises(ConfigError):
            load_config(path)
        path.write_text("[pipeline]\nmax_entry_drift_stop_fraction = 0.1\n")
        self.assertEqual(load_config(path).pipeline.max_entry_drift_stop_fraction, 0.1)


if __name__ == "__main__":
    unittest.main()
