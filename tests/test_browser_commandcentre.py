"""The redesigned Command Centre in a real browser (headless Chromium), driven against the FAKE MT5 bridge. No real MT5, no real order.

The strategy results are canned (a qualified BUY/SELL, WATCH, WAIT, bad data) so each UI state can be driven on demand; the Risk Engine,
the execution gate, the adapter and the journal are the real ones talking to the fake bridge. Skipped (not passed) without Playwright/Chromium.

    python3 -m unittest tests.test_browser_commandcentre -v
"""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.browser_support import PLAYWRIGHT, BrowserBase
from tests.fake_bridge import FakeBridge
from tests.test_pipeline import STAMP, sell_payload, strategy_payload
from trademate.broker import create_adapter
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.risk import RiskRules, RiskRulesStore

if PLAYWRIGHT:
    _STACK = {}

    def _stack():
        tmp = tempfile.TemporaryDirectory()
        bridge = FakeBridge(Path(tmp.name)).start()
        settings = Settings(app=AppSettings(data_dir=Path(tmp.name)), mt5=Mt5Settings(data_dir=Path(tmp.name)),
                            trading=TradingSettings(command_timeout_seconds=3.0, confirm_timeout_seconds=2.0))
        adapter = create_adapter(settings)
        _STACK.update(tmp=tmp, bridge=bridge, settings=settings)
        return adapter


def ui_payload(base):
    """Add what the page also reads (per-timeframe analysis, per-stage states) to a canned strategy result."""
    r = base["result"]
    buy = r["decision"] in ("BUY", "SELL")
    r["timeframes"] = {tf: {"analyzed": True, "bias": "bullish", "strength": "established", "bias_basis": f"{tf} structure (canned)", "events": [],
                            "invalidation": {"level": 1.099}} for tf in ("H1", "M5")}
    r["data"] = {tf: {**d, "lag_seconds": 12.0} for tf, d in r["data"].items()}
    state = "met" if buy else "not_met"
    r["stages"] = {"context": {"state": "met", "detail": "H1 bullish"}, "setup_candidate": {"state": state, "detail": "d"},
                   "confirmation": {"state": state if buy else "not_evaluated", "detail": "d"}, "trade_levels": {"state": state if buy else "not_evaluated", "detail": "d"}}
    r["context"] = {"state": "h1_bullish", "label": "H1 bullish", "summary": "H1 shows confirmed higher highs and higher lows.", "direction": "bullish", "relation": "aligned"}
    r["clock"] = {"known": True, "detail": "Broker clock measured.", "now_server": 1.0}
    r["gates"] = [{"id": "data", "passed": True, "detail": "H1 and M5 data are fresh."}, {"id": "trade_levels", "passed": buy, "detail": "levels"}]
    r["symbol"] = "EURUSD"
    r["disclaimer"] = "A strategy proposes; it is a hypothesis."
    return base


def watch_payload():
    p = ui_payload(strategy_payload("WATCH"))
    p["result"].update(decision="WATCH", direction="bullish", setup_status="developing", reasons=["Pullback in progress; no closed M5 candle above 1.1010 yet."],
                       missing_evidence=["A closed M5 candle above 1.1010"])
    return p


def wait_payload():
    p = ui_payload(strategy_payload("WAIT"))
    p["result"].update(decision="WAIT", direction=None, setup_status="inconclusive", reasons=["No confirmed M5 CHoCH is available."])
    return p


def stale_payload():
    p = wait_payload()
    p["result"]["context"] = {"state": "data_unavailable", "label": "Data not reliable", "summary": "M5 data is stale", "direction": None, "relation": None}
    p["result"]["reasons"] = ["M5 data is stale: newest closed candle is 9000 s old."]
    return p


class CommandCentreBase(BrowserBase):
    broker_factory = staticmethod(lambda: _stack())
    extra_patches = (lambda cls: [mock.patch.object(app_module, "risk_store", RiskRulesStore(
        Path(_STACK["tmp"].name) / "risk.json", RiskRules(max_risk_per_trade_percent=1.0, max_daily_loss_percent=3.0, max_total_active_risk_percent=5.0,
                                                          max_positions=3))),
        mock.patch.object(app_module, "settings", _STACK["settings"]),
        mock.patch.object(app_module, "pipeline_store", __import__("trademate.pipeline", fromlist=["x"]).ApprovalStore(Path(_STACK["tmp"].name) / "p.jsonl"))],)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if _STACK.get("bridge"):
            _STACK["bridge"].stop()
            _STACK["tmp"].cleanup()
            _STACK.clear()

    def setUp(self):
        super().setUp()
        self.bridge = _STACK["bridge"]
        self.bridge.orders.clear()
        self.bridge.positions.clear()
        self.bridge.commands_seen.clear()
        self.bridge.bid, self.bridge.ask = 1.10000, 1.10012
        self.bridge.write_snapshot()
        app_module.strategy_store.select("choch")
        self.payload = strategy_payload("BUY")
        self.current = ui_payload(self.payload)
        p = mock.patch.object(app_module, "evaluate_selected", lambda adapter, sid, symbol, settings, now_utc=None, **_kw: copy.deepcopy(self.current))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(app_module.strategy_store.select, "choch")

    def writes(self):
        return [c for c in self.bridge.commands_seen if c["cmd"] in ("open", "modify", "close")]

    def open_cc(self, width=1280, height=900):
        self.page.set_viewport_size({"width": width, "height": height})
        self.page.goto(self.base + "/#strategylab")
        self.until("document.querySelector('#view-strategylab [data-cap=broker]').getAttribute('data-state') !== 'loading'")
        self.until("document.querySelector('#cc-strategy').options.length >= 2")

    def analyse(self, symbol="EURUSD"):
        self.page.locator("#lab-market").fill(symbol)
        self.page.locator("#lab-analyse").click()
        self.until("document.querySelector('#lab-decision-body .lab-decision-badge') !== null || document.querySelector('#lab-decision-body .lab-empty-title')?.textContent === 'No result'")


@unittest.skipUnless(PLAYWRIGHT, "SKIPPED: Playwright is not installed.")
class StrategySelectorUiTest(CommandCentreBase):
    def test_dropdown_lists_exactly_the_registered_strategies_and_shows_the_selection(self):
        self.open_cc()
        names = self.page.evaluate("() => Array.from(document.querySelector('#cc-strategy').options).map(o => [o.value, o.textContent])")
        self.assertEqual(names, [["candle_momentum", "Candle Momentum"], ["choch", "CHoCH Trading"], ["market_structure", "Market Structure Trading"], ["bull_bear", "Bull/Bear Trading"], ["trendline", "Trendline Trading"]])
        self.assertEqual(self.page.locator("#cc-strategy").input_value(), "choch")
        self.assertFalse(self.page.is_disabled("#cc-strategy"))
        self.assertIn("CHoCH Trading", self.page.inner_text("#cc-chip-strategy"))
        self.assertTrue(self.page.is_visible("#cc-strategy-tf"))
        tf = self.page.inner_text("#cc-strategy-tf")
        self.assertIn("H1", tf)
        self.assertIn("M5", tf)
        for sel in ("#cc-strategy-tf", "#cc-strategy-desc", "#cc-chip-strategy"):                       # the strategy's own information never mentions timeframes it does not read
            self.assertNotIn("M15", self.page.inner_text(sel))                                           # (the execution-timeframe SELECTOR now legitimately lists every timeframe)
            self.assertNotIn("H4", self.page.inner_text(sel))
        opts = self.page.evaluate("() => Array.from(document.querySelector('[data-scope=\"cc\"] [data-tc=\"timeframe\"]').options).map(o => o.value)")
        self.assertEqual(opts, ["M1", "M2", "M5", "M15", "M30", "H1", "H4"])
        self.assertIn("change of character", self.page.inner_text("#cc-strategy-desc").lower())

    def test_changing_the_dropdown_changes_the_real_backend_selection(self):
        self.open_cc()
        with self.page.expect_response(lambda r: "/api/strategy/select" in r.url and r.status == 200):
            self.page.select_option("#cc-strategy", "market_structure")
        self.until("document.querySelector('#cc-chip-strategy').textContent.includes('Market Structure Trading')")
        self.assertEqual(app_module.strategy_store.selected_id(), "market_structure")
        self.assertIn("higher highs", self.page.inner_text("#cc-strategy-desc").lower())
        self.assertEqual(self.page.locator("#cc-strategy").input_value(), "market_structure")

    def test_analysis_uses_the_strategy_selected_in_the_dropdown(self):
        seen = []
        original = lambda adapter, sid, symbol, settings, now_utc=None, **_kw: (seen.append(sid), copy.deepcopy(self.current))[1]
        with mock.patch.object(app_module, "evaluate_selected", original):
            self.open_cc()
            self.analyse()
            self.page.select_option("#cc-strategy", "market_structure")
            self.until("document.querySelector('#cc-chip-strategy').textContent.includes('Market Structure')")
            self.analyse()
        self.assertEqual(seen, ["choch", "market_structure"])

    def test_switching_strategy_clears_the_analysis_and_any_pending_review(self):
        self.open_cc()
        self.analyse()
        self.page.locator("#cc-review-btn").click()
        self.until("document.querySelector('#cc-execute-btn') && !document.querySelector('#cc-execute').hidden")
        self.page.select_option("#cc-strategy", "market_structure")
        self.until("document.querySelector('#cc-chip-strategy').textContent.includes('Market Structure')")
        self.until("document.querySelector('#cc-execute').hidden")
        self.assertTrue(self.page.is_hidden("#cc-review"))
        self.assertEqual(self.page.locator("#lab-decision-body .lab-decision-badge").count(), 0)
        self.assertIn("strategy changed", self.page.inner_text("#lab-decision-body").lower())
        self.assertEqual(self.writes(), [])

    def test_a_failed_selection_reverts_the_dropdown(self):
        self.open_cc()
        with mock.patch.object(app_module.strategy_store, "select", return_value=(False, "The strategy selection could not be saved.", "choch")):
            self.page.select_option("#cc-strategy", "market_structure")
            self.until("document.querySelector('#lab-status-line').textContent.includes('could not be saved')")
        self.assertEqual(self.page.locator("#cc-strategy").input_value(), "choch")

    def test_the_selection_is_recovered_after_a_reload(self):
        self.open_cc()
        self.page.select_option("#cc-strategy", "market_structure")
        self.until("document.querySelector('#cc-chip-strategy').textContent.includes('Market Structure')")
        self.page.reload()
        self.until("document.querySelector('#cc-strategy').options.length >= 2 && document.querySelector('#cc-strategy').value === 'market_structure'")


@unittest.skipUnless(PLAYWRIGHT, "SKIPPED: Playwright is not installed.")
class DecisionCardUiTest(CommandCentreBase):
    def test_buy_shows_the_real_levels_stages_and_enables_review_but_never_execute(self):
        self.open_cc()
        self.analyse()
        self.assertEqual(self.page.inner_text("#lab-decision-body .lab-decision-badge"), "BUY")
        text = self.page.inner_text("#lab-decision-body")
        self.assertIn("qualifying bullish setup", text)
        for stage in ("market context", "setup candidate", "confirmation", "trade levels"):
            self.assertIn(stage, text.lower())                            # the page upper-cases these labels with CSS
        setup = self.page.inner_text("#lab-setup-body")
        for value in ("1.10012", "1.099", "1.1035"):
            self.assertIn(value, setup)
        self.assertIn("3.0", setup)
        self.assertTrue(self.page.is_visible("#cc-review"))
        self.assertFalse(self.page.is_disabled("#cc-review-btn"))
        self.assertFalse(self.page.is_visible("#cc-execute-btn"))
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.page.locator("#lab-mtf-body .lab-timeframe-card").count(), 2)

    def test_sell_is_labelled_sell(self):
        self.current = ui_payload(sell_payload())
        self.open_cc()
        self.analyse()
        self.assertEqual(self.page.get_attribute("#lab-decision-body .lab-decision-badge", "data-decision"), "SELL")
        self.assertIn("bearish", self.page.inner_text("#lab-decision-body"))

    def test_watch_explains_what_is_missing_and_offers_no_review(self):
        self.current = watch_payload()
        self.open_cc()
        self.analyse()
        self.assertEqual(self.page.inner_text("#lab-decision-body .lab-decision-badge"), "WATCH")
        self.assertIn("required condition is still outstanding", self.page.inner_text("#lab-decision-body"))
        self.assertIn("Pullback in progress", self.page.inner_text("#lab-decision-body"))
        self.assertTrue(self.page.is_hidden("#cc-review"))
        self.assertIn("no levels to review", self.page.inner_text("#lab-setup-body"))

    def test_wait_uses_the_backend_reason(self):
        self.current = wait_payload()
        self.open_cc()
        self.analyse()
        self.assertEqual(self.page.inner_text("#lab-decision-body .lab-decision-badge"), "WAIT")
        self.assertIn("No confirmed M5 CHoCH", self.page.inner_text("#lab-decision-body"))
        self.assertTrue(self.page.is_hidden("#cc-review"))

    def test_stale_data_is_explained_and_never_shown_as_a_direction(self):
        self.current = stale_payload()
        self.open_cc()
        self.analyse()
        text = self.page.inner_text("#lab-decision-body")
        self.assertEqual(self.page.inner_text("#lab-decision-body .lab-decision-badge"), "WAIT")
        self.assertIn("Data problem", text)
        self.assertIn("stale", text)
        self.assertNotIn("bullish setup", text)
        self.assertTrue(self.page.is_hidden("#cc-review"))

    def test_evidence_is_expandable(self):
        self.open_cc()
        self.analyse()
        self.assertFalse(self.page.locator("#lab-decision-body details.lab-intelligence").first.evaluate("e => e.open"))
        self.page.locator("#lab-decision-body details.lab-intelligence summary").first.click()
        self.assertTrue(self.page.locator("#lab-decision-body details.lab-intelligence").first.evaluate("e => e.open"))
        self.assertIn("Checks", self.page.inner_text("#lab-decision-body"))

    def test_a_backend_error_shows_no_decision(self):
        with mock.patch.object(app_module, "evaluate_selected", side_effect=RuntimeError("boom")):
            self.open_cc()
            self.analyse()
        self.assertEqual(self.page.locator("#lab-decision-body .lab-decision-badge").count(), 0)
        self.assertIn("No result", self.page.inner_text("#lab-decision-body"))
        self.assertTrue(self.page.is_hidden("#cc-review"))

    def test_a_duplicate_click_while_analysing_sends_one_request(self):
        calls = []
        original = lambda adapter, sid, symbol, settings, now_utc=None, **_kw: (calls.append(1), __import__("time").sleep(0.6), copy.deepcopy(self.current))[2]
        with mock.patch.object(app_module, "evaluate_selected", original):
            self.open_cc()
            self.page.locator("#lab-market").fill("EURUSD")
            self.page.locator("#lab-analyse").click()
            self.until("document.querySelector('#lab-analyse').disabled")
            self.assertEqual(self.page.inner_text("#lab-analyse-text"), "ANALYSING…")
            self.page.locator("#lab-analyse").click(force=True)
            self.until("document.querySelector('#lab-decision-body .lab-decision-badge') !== null")
        self.assertEqual(len(calls), 1)


@unittest.skipUnless(PLAYWRIGHT, "SKIPPED: Playwright is not installed.")
class TradeReviewUiTest(CommandCentreBase):
    def review(self):
        self.open_cc()
        self.analyse()
        self.page.locator("#cc-review-btn").click()
        self.until("document.querySelector('#cc-review-result .cc-verdict') !== null")

    def test_review_shows_real_risk_figures_and_sends_nothing(self):
        self.review()
        verdict = self.page.inner_text("#cc-review-result")
        self.assertIn("Approved by the Risk Engine", verdict)
        for expected in ("CHoCH Trading v1.0.0", "EURUSD", "BUY", "1.10012", "1.099", "1.1035", "0.08 lots", "USD"):
            self.assertIn(expected, verdict)
        self.assertTrue(self.page.is_visible("#cc-execute-btn"))
        self.assertEqual(self.writes(), [])

    def test_risk_rejection_is_shown_with_reasons_and_no_execute_control(self):
        self.bridge.positions[7] = {"symbol": "EURUSD", "type": "buy", "volume": 0.01, "price_open": 1.1, "sl": 1.09, "tp": 1.12}
        self.bridge.write_snapshot()
        app_module.risk_store.save(RiskRules(1.0, 3.0, 5.0, 1))
        self.addCleanup(app_module.risk_store.save, RiskRules(1.0, 3.0, 5.0, 3))
        self.review()
        verdict = self.page.inner_text("#cc-review-result")
        self.assertIn("Rejected by the Risk Engine", verdict)
        self.assertIn("simultaneous position", verdict.lower())
        self.assertFalse(self.page.is_visible("#cc-execute-btn"))
        self.assertEqual(self.writes(), [])

    def test_execution_needs_two_deliberate_clicks_and_then_confirms_the_position(self):
        self.review()
        self.page.locator("#cc-execute-btn").click()
        self.assertEqual(self.page.get_attribute("#cc-execute-btn", "data-armed"), "true")
        self.assertIn("SEND NOW", self.page.inner_text("#cc-execute-btn"))
        self.assertEqual(self.writes(), [], "the first click only arms the button")
        self.page.locator("#cc-execute-btn").click()
        self.until("document.querySelector('#cc-review-result').textContent.includes('Executed and confirmed')")
        self.assertEqual(len(self.writes()), 1)
        pos = list(self.bridge.positions.values())
        self.assertEqual((len(pos), pos[0]["type"], pos[0]["volume"], pos[0]["sl"], pos[0]["tp"]), (1, "buy", 0.08, 1.0990, 1.1035))
        self.assertTrue(self.page.is_hidden("#cc-execute"))

    def test_cancel_disarms_without_sending(self):
        self.review()
        self.page.locator("#cc-execute-btn").click()
        self.page.locator("#cc-cancel-btn").click()
        self.assertIsNone(self.page.get_attribute("#cc-execute-btn", "data-armed"))
        self.assertEqual(self.writes(), [])

    def test_a_broker_rejection_is_reported_not_hidden(self):
        self.review()
        self.bridge.reject_with = (10019, "No money")
        self.page.locator("#cc-execute-btn").click()
        self.page.locator("#cc-execute-btn").click()
        self.until("document.querySelector('#cc-review-result').textContent.includes('Not executed')")
        text = self.page.inner_text("#cc-review-result")
        self.assertIn("broker_rejected", text)
        self.assertEqual(len(self.bridge.positions), 0)

    def test_an_approval_cannot_be_used_after_the_account_changed(self):
        self.review()
        self.bridge.positions[9] = {"symbol": "EURUSD", "type": "sell", "volume": 0.01, "price_open": 1.1, "sl": 1.11, "tp": 1.09}
        self.bridge.write_snapshot()
        self.page.locator("#cc-execute-btn").click()
        self.page.locator("#cc-execute-btn").click()
        self.until("document.querySelector('#cc-review-result').textContent.includes('Not executed')")
        self.assertIn("changed", self.page.inner_text("#cc-review-result"))
        self.assertEqual(self.writes(), [])

    def test_a_stale_approval_from_the_old_strategy_is_refused_by_the_server(self):
        self.review()
        approval_id = list(app_module.pipeline_store._items)[-1]
        app_module.strategy_store.select("market_structure")                       # the strategy changes behind the page's back
        out = self.page.evaluate("""async (id) => (await fetch('/api/pipeline/execute', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({approval_id: id, confirm: true})})).json()""", approval_id)
        self.assertFalse(out["order_submitted"])
        self.assertEqual(self.writes(), [])


@unittest.skipUnless(PLAYWRIGHT, "SKIPPED: Playwright is not installed.")
class ControlsAndLayoutUiTest(CommandCentreBase):
    def test_autopilot_controls_are_present_off_and_validate_before_starting(self):
        self.open_cc()
        self.until("document.querySelector('#view-strategylab [data-tc=ap-state]').textContent.length > 0")
        self.assertEqual(self.page.locator("#view-strategylab [data-tc=ap-state]").inner_text(), "Off")
        self.assertEqual(self.page.locator("#view-strategylab #lab-autopilot").count(), 0)      # the old dead placeholder is gone
        self.page.locator("#view-strategylab [data-tc=ap-start]").click()
        self.until("document.querySelector('#view-strategylab [data-tc=ap-msg]').textContent.length > 0")
        self.assertIn("stop condition", self.page.inner_text("#view-strategylab [data-tc=ap-msg]").lower())
        self.assertEqual(self.writes(), [])

    def test_watchdog_toggle_starts_real_server_side_monitoring_of_the_selected_strategy(self):
        self.current["result"]["strategy"] = dict(STAMP, id="market_structure", name="Market Structure Trading", rule_id="MARKET-STRUCTURE-v1")      # the strategy that will be answering
        self.open_cc()
        self.page.select_option("#cc-strategy", "market_structure")
        self.until("document.querySelector('#cc-chip-strategy').textContent.includes('Market Structure')")
        self.page.locator("#lab-market").fill("EURUSD")
        with self.page.expect_response(lambda r: "/api/strategylab/watchdog/start" in r.url and r.status == 202):
            self.page.locator("#lab-watchdog").check()
        self.assertTrue(app_module.watchdog.status()["active"])
        self.assertEqual(app_module.watchdog.status()["strategy_id"], "market_structure")
        self.until("document.querySelector('#cc-wd-state').textContent.includes('Market Structure Trading')")
        with self.page.expect_response(lambda r: "/api/strategylab/watchdog/stop" in r.url):
            self.page.locator("#lab-watchdog").uncheck()
        self.until("!document.querySelector('#cc-wd-state').textContent.includes('Active')")
        self.assertFalse(app_module.watchdog.status()["active"])

    def test_switching_strategy_stops_the_watchdog_on_the_server_and_in_the_label(self):
        self.open_cc()
        self.page.locator("#lab-market").fill("EURUSD")
        self.page.locator("#lab-watchdog").check()
        self.until("document.querySelector('#cc-wd-state').textContent.includes('CHoCH Trading')")
        self.page.select_option("#cc-strategy", "market_structure")
        self.until("document.querySelector('#cc-wd-state').getAttribute('data-wd') === 'off'")
        for _ in range(100):
            if not app_module.watchdog.status()["active"]:
                break
            self.page.wait_for_timeout(50)
        self.assertFalse(app_module.watchdog.status()["active"])

    def test_header_shows_only_real_account_values(self):
        self.open_cc()
        self.until("!document.querySelector('#cc-chip-account').hidden")
        self.assertIn("DEMO", self.page.inner_text("#cc-chip-account"))
        self.assertIn("MT5 connected", self.page.inner_text("#cc-chip-conn"))
        self.assertTrue(self.page.is_visible("#cc-account-kv"))
        kv = self.page.inner_text("#cc-account-kv")
        self.assertIn("Balance", kv)
        self.assertIn("Equity", kv)

    def test_disconnected_broker_is_shown_and_analysis_explains_the_blocker(self):
        from trademate.broker.base import ConnectionState, ConnectionStatus
        with mock.patch.object(self.broker, "get_connection_status", return_value=ConnectionStatus(ConnectionState.DISCONNECTED, "MT5 is not running.")):
            self.page.goto(self.base + "/#strategylab")
            self.until("document.querySelector('#view-strategylab [data-cap=broker]').getAttribute('data-state') === 'Disconnected'")
            self.until("document.querySelector('#cc-chip-conn').textContent.includes('disconnected')")
            self.assertEqual(self.page.get_attribute("#cc-chip-conn", "data-tone"), "bad")
            self.assertTrue(self.page.is_hidden("#cc-chip-account"))

    def test_phone_layout_has_no_overflow_and_large_touch_targets(self):
        for width in (390, 320):
            with self.subTest(width=width):
                self.open_cc(width, 800)
                self.analyse()
                overflow = self.page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
                self.assertLessEqual(overflow, 1)
                for sel in ("#lab-analyse", "#cc-strategy", "#lab-market"):
                    box = self.page.locator(sel).bounding_box()
                    self.assertGreaterEqual(box["height"], 44, sel)
                    self.assertLessEqual(box["x"] + box["width"], width + 1, sel)
                badge = self.page.locator("#lab-decision-body .lab-decision-badge").bounding_box()
                self.assertLessEqual(badge["x"] + badge["width"], width + 1)

    def test_light_theme_is_readable(self):
        self.page.goto(self.base + "/")
        self.page.evaluate("() => document.documentElement.setAttribute('data-theme', 'light')")
        self.open_cc()
        self.analyse()
        ratio = self.page.evaluate("""() => {
          const lum = (c) => { const m = c.match(/[\\d.]+/g).map(Number); const f = (v) => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); };
            return .2126 * f(m[0]) + .7152 * f(m[1]) + .0722 * f(m[2]); };
          const fg = getComputedStyle(document.querySelector('.cc-decision-text')).color;
          let el = document.querySelector('.cc-decision-text'), bg = 'rgb(255,255,255)';
          while (el) { const b = getComputedStyle(el).backgroundColor; const a = b.match(/[\\d.]+/g); if (a && a.length >= 3 && (a.length === 3 || Number(a[3]) > .8)) { bg = b; break; } el = el.parentElement; }
          const l1 = lum(fg), l2 = lum(bg); return (Math.max(l1, l2) + .05) / (Math.min(l1, l2) + .05); }""")
        self.assertGreaterEqual(ratio, 4.5, f"decision text contrast {ratio:.2f}")

    def test_keyboard_reaches_the_strategy_dropdown_and_analyse(self):
        self.open_cc()
        self.page.locator("#cc-strategy").focus()
        self.assertEqual(self.page.evaluate("() => document.activeElement.id"), "cc-strategy")
        self.assertTrue(self.page.evaluate("() => !!document.querySelector('label[for=cc-strategy]')"))
        self.page.keyboard.press("Tab")
        self.assertEqual(self.page.evaluate("() => document.activeElement.id"), "lab-analyse")

    def test_recent_activity_lists_recorded_trades_only(self):
        self.open_cc()
        self.assertIn("No recorded activity", self.page.inner_text("#cc-activity"))
        self.analyse()
        self.page.locator("#cc-review-btn").click()
        self.until("document.querySelector('#cc-execute-btn') && !document.querySelector('#cc-execute').hidden")
        self.page.locator("#cc-execute-btn").click()
        self.page.locator("#cc-execute-btn").click()
        self.until("document.querySelector('#cc-activity li[data-kind=trade]') !== null")
        self.assertIn("EURUSD", self.page.inner_text("#cc-activity"))


if __name__ == "__main__":
    unittest.main()
