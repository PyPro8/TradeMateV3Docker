"""TradeMate - Flask web application.

Run with:   python app.py
Then open:  http://127.0.0.1:5000

Phase 4: chart trading. Every write goes through the BrokerAdapter and is only
reported as done after the broker's own account state confirms it.
"""

import logging
import sys
import threading
import time
from datetime import datetime, timezone
from dataclasses import replace

from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from trademate import __version__
from trademate.broker import ConnectionState, ConnectionStatus, create_adapter
from trademate.chart_settings import ChartSettingsStore, preset_list
from trademate.config import load_config
from trademate.analysis import SUPPORTED_TIMEFRAMES, build_analysis, unavailable as analysis_unavailable
from trademate.execution_gate import safe_review, gate_order, gate_pending, portfolio_breaches, review_pending
from trademate.errors import (BrokerRejectedError, BrokerError, BrokerUnavailableError, ConfigError, ExecutionUnconfirmedError,
                              OrderRejectedError)
from trademate.logging_setup import setup_logging
from trademate.structure import StructureConfig, analyze_cached
from trademate.testrunner import RunManager
from trademate.risk.sl_calculator import MODES as SL_MODES, judge_limit, risk_from_sl, sl_from_risk
from trademate.risk import (RiskLimits, RiskRules, RiskRulesStore, SymbolSpec, TradeProposal, TradeRequest,
                            calculate_position_size, evaluate_trade, parse_rules, summarize_state)
from trademate import brokertime
from trademate.risk.account_state import gather_state, resolve_server_clock, set_local_timezone, to_spec
from trademate.strategy.service import evaluate_symbol as evaluate_strategy
from trademate.strategy.service import FETCH_COUNT
from trademate.trade_pipeline import review_strategy_proposal
from trademate.strategy.timeframes import TF_SECONDS
from trademate.strategy.watchdog import WatchdogManager
from trademate.strategies import registry as strategy_registry
from trademate.strategies.selection import StrategySelectionStore
from trademate.strategies.service import evaluate_selected
from trademate import strategylab
from trademate import pipeline
from trademate.autopilot import Autopilot, MODE_LABELS, MODES
from trademate.decision_hub import DecisionHub
from trademate.lotsize import LotStore, validate_for_info
from trademate.pips import PipStore, resolve_pip, pips_to_price
from trademate.instruments import build_spec, classify as classify_instrument
from trademate.chart_extras import DrawingStore, IndicatorStore
from trademate import hard_reset, sessions as trading_sessions
from trademate.trade_journal import EMOTIONS as JOURNAL_EMOTIONS, RESULTS as JOURNAL_RESULTS, TradeJournal
from trademate.notifications import NotificationLog
from trademate.selection_store import SelectionStore
from trademate.setups_ledger import SetupLedger
from trademate import lot_adjust
from trademate.risk.trade_calculator import calculate as calc_trade
from trademate.strategies.service import set_trade_settings_provider
from trademate.trade_settings import TradeSettings
from trademate.execution_settings import ExecutionSettings
from trademate.risk.engine import set_protections_provider
from trademate import localday
from trademate.risk.daily_log import DailyRiskLog
from trademate.risk.protections import ConsecutiveLossGuard, SafetyLock, protection_checks
from trademate.strategies.base import EXEC_TIMEFRAMES
from trademate import bull_bear as bb_pkg
from trademate import candle_momentum as cm_pkg
from trademate.bull_bear.diagnostics import EventLog as BBEventLog
from trademate.bull_bear.engine import BullBearEngine
from trademate.bull_bear.feed import AdapterFeed as BBFeed
from trademate.bull_bear.models import BBConfig, validate_config as bb_validate
from trademate.bull_bear.store import ConfigStore as BBConfigStore, StateStore as BBStateStore
from trademate.bull_bear.antitwin import AntiTwin
from trademate.candle_momentum.diagnostics import EventLog as CMEventLog
from trademate.candle_momentum.engine import CandleMomentumEngine
from trademate.candle_momentum.feed import AdapterFeed
from trademate.candle_momentum.models import CMConfig, validate_config as cm_validate
from trademate.candle_momentum.store import ConfigStore as CMConfigStore, StateStore as CMStateStore

# Phase 1 foundation: configuration and logging.
try:
    settings = load_config()
except ConfigError as exc:
    sys.exit(f"Configuration error: {exc}")

setup_logging(settings)
logger = logging.getLogger("trademate.web")
brokertime.DEFAULT_RESOLVER.configure(settings.risk.broker_timezone)   # optional fallback for the broker's trading day

# Broker integration (read-only). The rest of the app only sees the BrokerAdapter interface.
adapter = create_adapter(settings)
# The user's selected strategy (CHoCH Trading / Trend Continuation): validated against the registry, persisted in data/, recovered after a restart.
strategy_store = StrategySelectionStore(settings.app.data_dir / "strategy_selection.json", settings.strategy.default_strategy)


def _selected_strategy_id() -> str:
    return strategy_store.selected_id()


# The Watchdog always analyses with ITS OWN strategy (the one it was started with), never "whatever is selected now".
decision_hub = DecisionHub(stale_after=max(60.0, 6 * 15.0))            # the ONE current decision per (instrument, strategy, timeframe)
selection_store = SelectionStore(settings.app.data_dir / "trade_selection.json", settings.strategy.default_exec_timeframe)
lot_store = LotStore(settings.app.data_dir / "lot_sizes.json")
trade_settings = TradeSettings(settings.app.data_dir / "trade_settings.json")                         # Command Centre settings: global / instrument / strategy scopes
set_trade_settings_provider(lambda symbol, sid: trade_settings.saved_only(symbol, sid))


def _lot_for(symbol):
    """The instrument's saved lot size; the built-in default (0.02) applies ONLY when none was ever saved. The pipeline still validates it against the broker's volume rules."""
    if not symbol:
        return None
    return lot_store.get(symbol) or trade_settings.resolve(symbol, _selected_strategy_id())["values"]["lot"]
pip_store = PipStore(settings.app.data_dir / "pip_sizes.json")


def _pip_overrides() -> dict:
    """settings.toml [pips.overrides] first, then what the user typed in the browser (which wins)."""
    return {**settings.pips.overrides, **pip_store.all()}

setup_ledger = SetupLedger(settings.app.data_dir / "setup_ledger.json")
set_local_timezone(settings.ui.timezone)                  # the daily-loss day is the computer's LOCAL day ([ui] timezone, or the operating system's when empty)
execution_settings = ExecutionSettings(settings.app.data_dir / "execution_settings.json")           # the user's global execution / safety switches
safety_lock = SafetyLock(settings.app.data_dir / "safety_lock.json")                                  # Emergency Stop lock (persists across restarts)
indicator_store = IndicatorStore(settings.app.data_dir / "chart_indicators.json")                     # moving average (display only)
drawing_store = DrawingStore(settings.app.data_dir / "manual_drawings.json")                          # the user's manual lines (never strategy trendlines)
notifications = NotificationLog(settings.app.data_dir / "notifications.json")                           # unique ids, history kept
daily_risk_log = DailyRiskLog(settings.app.data_dir / "risk_daily_log.json")                            # one row per local day; never deleted
loss_guard = ConsecutiveLossGuard(settings.app.data_dir / "consecutive_losses.json", get_config=execution_settings.get_all)
# Candle Momentum: ONE persistent, tick-driven engine (state survives refreshes and restarts). Its executor hand-off is wired after the Autopilot exists, below.
cm_events = CMEventLog(settings.app.data_dir / "candle_momentum_events.jsonl")
cm_config_store = CMConfigStore(settings.app.data_dir / "candle_momentum_config.json", EXEC_TIMEFRAMES)
cm_state_store = CMStateStore(settings.app.data_dir / "candle_momentum_state.json")
candle_engine = CandleMomentumEngine(AdapterFeed(adapter), settings, cm_config_store, cm_state_store, cm_events, hub=decision_hub, pip_overrides=lambda: _pip_overrides(),
                                     async_fetch=True, max_entries=execution_settings.max_entries_per_candle)
cm_pkg.set_engine(candle_engine)
# Bull/Bear: a modified copy of the Candle Momentum engine (structure-gated) with the optional Anti-Twin. Its Anti-Twin executor is attached below, next to Rapid Fire's.
bb_events = BBEventLog(settings.app.data_dir / "bull_bear_events.jsonl")
bb_config_store = BBConfigStore(settings.app.data_dir / "bull_bear_config.json", EXEC_TIMEFRAMES)
bb_state_store = BBStateStore(settings.app.data_dir / "bull_bear_state.json")
bull_bear_engine = BullBearEngine(BBFeed(adapter), settings, bb_config_store, bb_state_store, bb_events, hub=decision_hub, pip_overrides=lambda: _pip_overrides(),
                                  async_fetch=True, max_entries=execution_settings.max_entries_per_candle)
bb_pkg.set_engine(bull_bear_engine)
watchdog = WatchdogManager(lambda symbol, strategy_id=None, exec_tf=None: evaluate_selected(adapter, strategy_id or _selected_strategy_id(), symbol, settings, pip_overrides=_pip_overrides(),
                                                                                            now_utc=time.time(), exec_tf=exec_tf or selection_store.timeframe(strategy_id or _selected_strategy_id())),
                           hub=decision_hub, engine=candle_engine, engines={"bull_bear": bull_bear_engine})

# A previous run may have died with an order in flight: those records become UNKNOWN right away.
if getattr(adapter, "journal", None) is not None:
    try:
        adapter.journal.settle_stale(0.0)
    except Exception:
        logger.exception("Could not check the execution journal at startup")

# Risk rules: defaults from settings.toml, overridden by what the user saved in the UI (data/risk_rules.json).
risk_store = RiskRulesStore(settings.app.data_dir / "risk_rules.json",
                            RiskRules(max_risk_per_trade_percent=settings.risk.max_risk_percent,
                                      max_daily_loss_percent=settings.risk.max_daily_loss_percent,
                                      max_total_active_risk_percent=settings.risk.max_total_active_risk_percent,
                                      max_positions=settings.risk.max_positions))

# Strategy -> Risk -> Execution pipeline (controlled mode). Approvals live in memory only (a restart discards them: fail closed).
pipeline_store = pipeline.ApprovalStore(settings.app.data_dir / "pipeline_log.jsonl")


_retargeting = threading.local()
_retargeting.active = False


class _Retarget:
    def __enter__(self):
        _retargeting.active = True

    def __exit__(self, *a):
        _retargeting.active = False


def _on_strategy_change(old_id, new_id):
    """A real strategy change cancels every pending approval and stops/clears the Watchdog (nothing from the old strategy may survive)."""
    cancelled = pipeline_store.invalidate_all("invalidated", f"The selected strategy changed from {old_id} to {new_id}.")
    if not getattr(_retargeting, "active", False):                                  # the side-nav selection route retargets the Watchdog itself, with no stop/start gap
        watchdog.reset_for_strategy_change()
    logger.info("Strategy changed %s -> %s; %d approval(s) cancelled, Watchdog reset", old_id, new_id, cancelled)


strategy_store.on_change(_on_strategy_change)

# Chart appearance (candle colours, preset, market-structure visibility): data/chart_settings.json.
chart_store = ChartSettingsStore(settings.app.data_dir / "chart_settings.json")

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # always reload CSS/JS while developing


@app.route("/")
def index():
    return render_template("index.html", mode=settings.mode.value, version=__version__, risk=settings.risk,
                           dev_tools=_dev_tools_allowed(), ui_timezone=settings.ui.timezone)


# ---------------------------------------------------------------------------------------------------------------------
# Full Test dashboard (developer tool). It can only start the allowlisted automated test groups in trademate/testrunner.py:
# no command, path or argument comes from the browser. It is available only in development / demo mode, only when the
# server is bound to this computer, only to requests from this computer, and POSTs need a header a foreign web page cannot
# send. A hidden button is not the security control; these checks are.
# ---------------------------------------------------------------------------------------------------------------------
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
test_runner = RunManager(settings.app.data_dir)
FULL_TEST_PAGE = Path(__file__).resolve().parent / "Full Test.html"


def _dev_tools_allowed() -> bool:
    return settings.mode.value != "live" and settings.ui.host in _LOOPBACK


def _test_runner_guard(write: bool = False):
    """None when allowed, else (reason, status) for a JSON refusal."""
    if settings.mode.value == "live":
        return "The Full Test runner is disabled in live mode.", 403
    if settings.ui.host not in _LOOPBACK:
        return "The Full Test runner is disabled because the server is reachable from other computers ([ui] host).", 403
    if request.remote_addr not in _LOOPBACK:
        return "The Full Test runner only accepts requests from this computer.", 403
    if write:
        if request.headers.get("X-TradeMate-Test") != "1":
            return "Missing X-TradeMate-Test header.", 403
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            return "Cross-origin request refused.", 403
    return None


@app.route("/full-test")
@app.route("/Full Test.html")
def full_test_page():
    """The standalone dashboard file, served by Flask so its API calls work (a page opened from file:// cannot reach them)."""
    return send_file(FULL_TEST_PAGE, mimetype="text/html", max_age=0)


@app.route("/api/fulltest/catalog")
def api_fulltest_catalog():
    refused = _test_runner_guard()
    if refused:
        return jsonify({"ok": False, "message": refused[0]}), refused[1]
    return jsonify({"ok": True, **test_runner.catalog()})


@app.route("/api/fulltest/status")
def api_fulltest_status():
    refused = _test_runner_guard()
    if refused:
        return jsonify({"ok": False, "message": refused[0]}), refused[1]
    return jsonify({"ok": True, "state": test_runner.status(), "last": None if test_runner.status().get("running") else test_runner.last()})


@app.route("/api/fulltest/run", methods=["POST"])
def api_fulltest_run():
    refused = _test_runner_guard(write=True)
    if refused:
        return jsonify({"ok": False, "message": refused[0]}), refused[1]
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    ok, info = test_runner.start(data.get("groups", "all"))
    if not ok:
        return jsonify({"ok": False, "message": info}), (409 if "already in progress" in info else 400)
    return jsonify({"ok": True, "run_id": info}), 202


@app.route("/api/fulltest/cancel", methods=["POST"])
def api_fulltest_cancel():
    refused = _test_runner_guard(write=True)
    if refused:
        return jsonify({"ok": False, "message": refused[0]}), refused[1]
    return jsonify({"ok": test_runner.cancel()})


def broker_payload(broker):
    """Everything the UI needs from the broker, as plain data. Never raises."""
    status = broker.get_connection_status()
    payload = {"status": status.to_dict(), "account": None, "positions": [], "symbols": [], "orders": [], "pending_supported": False}
    if status.state is ConnectionState.CONNECTED:
        try:
            payload["account"] = broker.get_account().to_dict()
            payload["positions"] = [p.to_dict() for p in broker.get_positions()]
            try:                                   # pending orders are separate from positions; an old EA simply reports none
                payload["pending_supported"] = bool(broker.supports_pending_orders())
                payload["orders"] = [o.to_dict() for o in broker.get_orders()] if payload["pending_supported"] else []
            except (NotImplementedError, AttributeError, BrokerError):
                payload["pending_supported"], payload["orders"] = False, []
            payload["symbols"] = [sym.to_dict() for sym in broker.get_symbols()]
        except BrokerUnavailableError as exc:  # data disappeared between reads
            payload = {"status": ConnectionStatus(ConnectionState.DISCONNECTED, str(exc)).to_dict(),
                       "account": None, "positions": [], "symbols": []}
    payload["executions_unresolved"] = unresolved_executions()
    return payload


def _exec_view(rec):
    keys = ("id", "action", "symbol", "side", "volume", "ticket", "sl", "tp", "state", "stage", "message", "created_at")
    return {k: rec.get(k) for k in keys}


def unresolved_executions():
    """Writes whose real outcome is still unknown. Read from TradeMate's own journal: sends nothing to MT5."""
    journal = getattr(adapter, "journal", None)
    if journal is None:
        return []
    try:
        journal.settle_stale(120.0)
        return [_exec_view(r) for r in journal.unresolved()]
    except Exception:
        logger.exception("Could not read the execution journal")
        return []


@app.route("/api/broker")
def api_broker():
    """Read-only broker data for the UI."""
    return jsonify(broker_payload(adapter))


def _payload():
    """JSON body of a write request. Must be application/json from our own origin (CSRF guard)."""
    origin = request.headers.get("Origin")
    if origin and origin.split("://", 1)[-1] != request.host:
        raise OrderRejectedError("Blocked: request did not come from the TradeMate page.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise OrderRejectedError("Send a JSON body (Content-Type: application/json).")
    return data


def _optional_price(data, key):
    value = data.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        raise OrderRejectedError(f"{key} must be a finite number.")
    return float(value)


# Machine-readable outcome of a write. The browser and the test scripts branch on `outcome`, never on message text.
#   executed            MT5 confirmed it (account state shows the change)               order_submitted: true
#   risk_rejected       TradeMate's Risk Engine refused it                               order_submitted: false
#   execution_refused   TradeMate's Execution Engine refused it before sending           order_submitted: false
#   invalid_request     malformed / unsupported request, caught locally                  order_submitted: false
#   connection_failed   MT5 / bridge unavailable, nothing was sent                       order_submitted: false
#   broker_rejected     MT5/broker received it and refused it (retcode + reason)         order_submitted: true
#   accepted_unconfirmed MT5 accepted it but the account does not show the result yet    order_submitted: true
#   unknown             sent, answer lost: UNRESOLVED until reconciled                   order_submitted: null
#   not_executed        sent, and reconciliation proved it did NOT happen                order_submitted: true
#   internal_error      a TradeMate bug; see the log                                     order_submitted: false / null
def _reply(state, message, outcome, submitted, context=None, status=200, **extra):
    body = {"state": state, "message": message, "outcome": outcome, "order_submitted": submitted,
            "request": context or {}, **extra}
    return jsonify(body), status


def _unknown_reply(exc_message, execution_id, status=200, context=None):
    """The command may be live at the broker. Say so, try to settle it against the account, never claim failure."""
    state, message, rec, outcome, submitted = "unknown", exc_message, None, "unknown", None
    if execution_id:
        try:
            rec = adapter.reconcile(execution_id)           # read-only check of the real account
        except BrokerError as why:
            logger.warning("Could not reconcile %s right now: %s", execution_id, why)
        except Exception:
            logger.exception("Reconciliation of %s crashed", execution_id)
        if rec and rec["state"] == "reconciled_executed":
            state, message, outcome, submitted = "executed", rec["message"], "executed", True
        elif rec and rec["state"] == "reconciled_not_executed":
            state, message, outcome, submitted = "failed", rec["message"], "not_executed", True
        elif rec:
            message = f"{exc_message} {rec['message']}"
    extra = {"execution_id": execution_id}
    if outcome == "unknown":
        extra.update(automatic_resubmission="DISABLED", next_action="Reconcile against MT5 positions and deal history "
                     "before any retry. A blind retry is not permitted.")
    return _reply(state, message, outcome, submitted, context, status, **extra)


def _execute(action, context=None):
    """Run a write and turn the outcome into an honest JSON answer.

    Never says 'executed' unless the account confirms it, and never says 'failed' for something that may be live.
    """
    try:
        result = action()
    except BrokerRejectedError as exc:                      # MT5/the broker saw it and refused it
        return _reply("failed", str(exc), "broker_rejected", True, context, execution_id=None,
                      broker={"retcode": exc.retcode, "message": exc.broker_message})
    except OrderRejectedError as exc:                       # refused by TradeMate's execution layer before sending
        return _reply("failed", str(exc), "execution_refused", False, context)
    except ExecutionUnconfirmedError as exc:
        return _unknown_reply(str(exc), exc.execution_id, context=context)
    except BrokerUnavailableError as exc:
        return _reply("failed", str(exc), "connection_failed", False, context)
    except BrokerError as exc:
        return _reply("failed", str(exc), "execution_refused", False, context)
    except Exception:
        # A bug. The full traceback goes to the log, and the journal tells us whether a command was in flight.
        logger.exception("Unhandled error while executing a trade request")
        settled = adapter.settle_in_flight()
        if settled:
            return _unknown_reply("TradeMate hit an internal error while an order was in flight. "
                                  "The outcome is UNKNOWN.", settled[-1]["id"], status=500, context=context)
        return _reply("failed", "TradeMate hit an internal error before anything was sent to MT5. "
                      "No order was sent. See logs/trademate.log.", "internal_error", False, context, status=500,
                      execution_id=None)
    if result.confirmed:
        return _reply("executed", result.message, "executed", True, context, result=result.to_dict(),
                      execution_id=result.execution_id)
    return _reply("unconfirmed", result.message, "accepted_unconfirmed", True, context, result=result.to_dict(),
                  execution_id=result.execution_id)


def _target_risk():
    """Preferred risk per trade (settings.toml default_risk_percent), never above the user's saved maximum."""
    return min(settings.risk.default_risk_percent, risk_store.load().max_risk_per_trade_percent)


def _order_fields(data):
    """Raw order fields from the request body. Strict parsing happens in execution_gate.clean_request."""
    return (data.get("symbol"), data.get("side"), data.get("volume"),
            _optional_price(data, "sl"), _optional_price(data, "tp"))


@app.route("/api/order/review", methods=["POST"])
def api_order_review():
    """Phase 6: review a proposed order. The Risk Engine judges it; NOTHING is sent to MT5."""
    try:
        review = safe_review(adapter, risk_store.load(), _max_order_volume(), *_order_fields(_payload()),
                              target_risk_percent=_target_risk())
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "stage": "invalid", "approved": False, "message": str(exc), "order_sent": False}), 200
    return jsonify({"ok": True, **review.to_dict()})


def _risk_details(review):
    d = review.decision or {}
    return {"requested_risk_amount": d.get("trade_risk_amount"), "requested_risk_percent": d.get("trade_risk_percent"),
            "max_risk_amount": d.get("effective_max_risk_amount"), "max_risk_percent": d.get("effective_max_risk_percent"),
            "limiting_rule": d.get("limiting_rule"), "reasons": d.get("rejection_reasons") or [],
            "currency": review.currency}


@app.route("/api/order", methods=["POST"])
def api_order():
    """Place a market order on the DEMO account, but ONLY after the Risk Engine approves these exact parameters.

    The review is repeated here on every submission (the browser's earlier review is never trusted), so a changed
    volume, SL, TP, price or account state is always judged again before anything reaches the bridge.
    """
    try:
        fields = _order_fields(_payload())
    except OrderRejectedError as exc:
        return _reply("failed", str(exc), "invalid_request", False)
    symbol, side, volume, sl, tp = fields
    ctx = {"symbol": symbol, "side": side, "volume": volume, "sl": sl, "tp": tp}
    review = gate_order(adapter, risk_store.load(), _max_order_volume(), *fields, target_risk_percent=_target_risk())
    if not review.approved:
        logger.info("Order refused before execution (%s): %s", review.stage, review.message)
        if review.stage == "rejected_risk":
            return _reply("rejected_risk", review.message, "risk_rejected", False, ctx, review=review.to_dict(),
                          risk=_risk_details(review), order_sent=False)
        outcome = ("invalid_request" if review.stage == "invalid" else
                   "connection_failed" if review.cause == "connection" else "internal_error")
        return _reply("failed", review.message, outcome, False, ctx, review=review.to_dict(), order_sent=False)
    order = review.order
    ctx = {**ctx, "volume": review.approved_volume, "entry_reference": order.get("entry")}
    return _execute(lambda: adapter.place_order(order["symbol"], order["side"], review.approved_volume,
                                                order["sl"], order["tp"]), ctx)


# ---------------------------------------------------------------------------------------------------------------------
# Strategy -> Risk -> Execution pipeline (controlled, manual-confirmation mode). Two explicit steps: PROPOSE (nothing is sent), then
# EXECUTE (only with an unexpired, unused approval and confirm: true). Orders are placed ONLY through adapter.place_order, exactly like
# /api/order; the Risk gate (gate_order) is re-run inside prepare_execution().
# ---------------------------------------------------------------------------------------------------------------------
def _strategy_for_pipeline(symbol):
    """The SELECTED strategy's evaluation (the only strategy context the pipeline ever uses)."""
    sid = _selected_strategy_id()
    return evaluate_selected(adapter, sid, symbol, settings, now_utc=time.time(), exec_tf=selection_store.timeframe(sid), pip_overrides=_pip_overrides())


@app.route("/api/pipeline/propose", methods=["POST"])
def api_pipeline_propose():
    """Strategy -> Risk Engine -> execution eligibility -> approval. NOTHING is sent to MT5."""
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "approved": False, "message": str(exc), "order_sent": False}), 400
    symbol = data.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 32 or not symbol.strip().isprintable():
        return jsonify({"ok": False, "approved": False, "message": "A valid symbol is required.", "order_sent": False}), 400
    pct = data.get("risk_percent")
    if pct is not None and (isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 < pct <= 100):
        return jsonify({"ok": False, "approved": False, "message": "risk_percent must be a number above 0 and at most 100.", "order_sent": False}), 400
    try:
        report = pipeline.propose(adapter, settings, risk_store.load(), _max_order_volume(), symbol.strip(), pipeline_store,
                                  evaluate=_strategy_for_pipeline, risk_percent=pct, strategy_id=_selected_strategy_id(),
                                  fixed_volume=lot_store.get(symbol.strip()), ledger=setup_ledger, max_entries=execution_settings.max_entries_per_candle())
    except Exception:  # noqa: BLE001 - fail closed: an exception is never an approval
        logger.exception("Pipeline proposal crashed for %s", symbol)
        return jsonify({"ok": False, "approved": False, "order_sent": False, "code": "internal_error",
                        "message": "The pipeline could not evaluate this symbol, so nothing was approved and nothing was sent. See logs/trademate.log."}), 500
    body = report.to_dict()
    body["approved"] = body.pop("ok")
    return jsonify({"ok": True, **body})


@app.route("/api/pipeline/approvals/<approval_id>")
def api_pipeline_approval(approval_id):
    a = pipeline_store.get(approval_id)
    if a is None:
        return jsonify({"ok": False, "message": "No such approval."}), 404
    return jsonify({"ok": True, "approval": a.public()})


@app.route("/api/pipeline/execute", methods=["POST"])
def api_pipeline_execute():
    """Execute a previously APPROVED proposal after explicit confirmation. Everything is re-validated first; see pipeline.prepare_execution()."""
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return _reply("failed", str(exc), "invalid_request", False)
    approval_id = data.get("approval_id")
    try:
        plan, refusal = pipeline.prepare_execution(adapter, settings, risk_store.load(), _max_order_volume(), pipeline_store, approval_id,
                                                   data.get("confirm"), evaluate=_strategy_for_pipeline, strategy_id=_selected_strategy_id(), ledger=setup_ledger, max_entries=execution_settings.max_entries_per_candle())
    except Exception:  # noqa: BLE001 - fail closed; nothing was sent
        logger.exception("Pipeline execution check crashed")
        return _reply("failed", "The pipeline could not re-validate this approval, so nothing was sent. See logs/trademate.log.",
                      "internal_error", False, {"approval_id": approval_id}, status=500, order_sent=False)
    if refusal is not None:
        logger.info("Pipeline execution refused (%s): %s", refusal.code, refusal.message)
        return _reply("failed", refusal.message, "execution_refused", False, {"approval_id": approval_id}, refusal=refusal.to_dict(),
                      stages={**pipeline.stage_states("execution_refused")}, order_sent=False)
    ctx = {"approval_id": plan.approval_id, "symbol": plan.symbol, "side": plan.side, "volume": plan.volume, "sl": plan.stop_loss,
           "tp": plan.take_profit, "entry_reference": plan.entry_live}
    box = {}

    def action():
        box["result"] = adapter.place_order(plan.symbol, plan.side, plan.volume, plan.stop_loss, plan.take_profit)
        return box["result"]

    response = _execute(action, ctx)
    _realign_after_fill(plan, box.get("result"))
    return _with_pipeline_report(response, plan, box.get("result"))


def _realign_after_fill(plan, result) -> None:
    """A pip target is defined from the ACTUAL entry. If the fill differs from the quote the order was priced on, move the take-profit to the right place."""
    try:
        if result is None or not result.confirmed or result.position is None:
            return
        new_tp = pipeline.realigned_target(plan, result.position.price_open, plan.tick_size)
        if new_tp is not None:
            adapter.modify_position(result.position.ticket, plan.stop_loss, new_tp, True, True)
            logger.info("Take-profit of %s realigned to %s (%s pips from the actual entry %s)", result.position.ticket, new_tp, plan.target_pips, result.position.price_open)
            plan.take_profit = new_tp
    except Exception:                                    # noqa: BLE001 - the order stands with its original SL/TP; the placement check reports any difference
        logger.exception("Could not realign the take-profit to the actual entry")


def _with_pipeline_report(response, plan, result):
    """Attach the six-stage states and the placement check to the write's answer, and record the outcome. Reports only."""
    resp, status = (response if isinstance(response, tuple) else (response, 200))
    try:
        body = resp.get_json()
        outcome = body.get("outcome")
        body["stages"] = pipeline.stage_states(outcome)
        if result is not None and result.confirmed:
            body["placement_check"] = pipeline.verify_placement(plan, result.position)
            if body["placement_check"]["matches"] is False:
                body["warning"] = ("The position exists but differs from the approved order: " + "; ".join(body["placement_check"]["discrepancies"])
                                   + ". Check it in MT5 now.")
        elif outcome in ("accepted_unconfirmed", "unknown"):
            body["next_action"] = body.get("next_action") or ("Reconcile with MT5 (POST /api/executions/<id>/reconcile or 'Check MT5 again') before any "
                                                              "retry. This approval is used up: a new order needs a new proposal.")
        pipeline_store.record_outcome(plan.approval_id, {"outcome": outcome, "state": body.get("state"), "execution_id": body.get("execution_id"),
                                                         "stages": body["stages"], "placement_check": body.get("placement_check")})
        return jsonify(body), status
    except Exception:
        logger.exception("Could not build the pipeline report")
        return resp, status


# ---------------------------------------------------------------------------------------------------------------------
# Pending orders (Phase 7B). Same gate, same outcome contract as market orders. A pending order is NOT a position.
# ---------------------------------------------------------------------------------------------------------------------
def _pending_fields(data):
    expiration = data.get("expiration")
    if expiration is not None and (isinstance(expiration, bool) or not isinstance(expiration, (int, float)) or expiration != expiration
                                   or expiration in (float("inf"), float("-inf"))):
        raise OrderRejectedError("expiration must be a finite number (epoch seconds) or empty.")
    price = data.get("price")
    if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or price != price or price in (float("inf"), float("-inf"))):
        raise OrderRejectedError("price must be a finite number.")
    return (data.get("order_type"), data.get("symbol"), data.get("volume"), price,
            _optional_price(data, "sl"), _optional_price(data, "tp"), expiration)


@app.route("/api/pending/review", methods=["POST"])
def api_pending_review():
    """Review a proposed pending order: validation + Risk Engine. NOTHING is sent."""
    try:
        review = review_pending(adapter, risk_store.load(), _max_order_volume(), *_pending_fields(_payload()), target_risk_percent=_target_risk())
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "stage": "invalid", "approved": False, "message": str(exc), "order_sent": False})
    except Exception:  # noqa: BLE001 - fail closed
        logger.exception("Pending review crashed")
        return jsonify({"ok": False, "stage": "unavailable", "approved": False, "order_sent": False,
                        "message": "The pending order could not be reviewed, so it was not approved. Nothing was sent."})
    return jsonify({"ok": True, **review.to_dict()})


@app.route("/api/pending", methods=["POST"])
def api_pending_place():
    """Place a pending order, but ONLY after validation and the Risk Engine approved these exact parameters."""
    try:
        fields = _pending_fields(_payload())
    except OrderRejectedError as exc:
        return _reply("failed", str(exc), "invalid_request", False)
    order_type, symbol, volume, price, sl, tp, expiration = fields
    ctx = {"symbol": symbol, "order_type": order_type, "volume": volume, "price": price, "sl": sl, "tp": tp, "expiration": expiration}
    review = gate_pending(adapter, risk_store.load(), _max_order_volume(), *fields, target_risk_percent=_target_risk())
    if not review.approved:
        logger.info("Pending order refused before execution (%s): %s", review.stage, review.message)
        if review.stage == "rejected_risk":
            return _reply("rejected_risk", review.message, "risk_rejected", False, ctx, review=review.to_dict(), risk=_risk_details(review), order_sent=False)
        outcome = ("invalid_request" if review.stage == "invalid" else "connection_failed" if review.cause == "connection" else "internal_error")
        return _reply("failed", review.message, outcome, False, ctx, review=review.to_dict(), order_sent=False)
    o = review.order
    ctx = {**ctx, "volume": review.approved_volume}
    return _execute(lambda: adapter.place_pending_order(o["symbol"], o["order_type"], review.approved_volume, o["price"], o["sl"], o["tp"], o.get("expiration")), ctx)


@app.route("/api/pending/<int:ticket>/cancel", methods=["POST"])
def api_pending_cancel(ticket):
    return _execute(lambda: adapter.cancel_pending_order(ticket), {"ticket": ticket, "action": "cancel_pending"})


@app.route("/api/pending/<int:ticket>/modify", methods=["POST"])
def api_pending_modify(ticket):
    """Change a pending order's entry price, SL, TP and/or expiry. Include only the keys to change; the price is never adjusted for you."""
    try:
        data = _payload()
        change = {k for k in ("price", "sl", "tp", "expiration") if k in data}
        if not change:
            raise OrderRejectedError("Provide price, sl, tp and/or expiration.")
        _o, _s, _v, price, sl, tp, expiration = _pending_fields({k: data.get(k) for k in change})
    except OrderRejectedError as exc:
        return _reply("failed", str(exc), "invalid_request", False, {"ticket": ticket})
    return _execute(lambda: adapter.modify_pending_order(ticket, price, sl, tp, expiration, change),
                    {"ticket": ticket, "action": "modify_pending", "price": price, "sl": sl, "tp": tp})


def _with_risk_report(response):
    """After a modification, let the Risk Engine re-evaluate the account and ATTACH what it finds. Reports only."""
    resp, status = (response if isinstance(response, tuple) else (response, 200))
    try:
        body = resp.get_json()
        if body.get("state") != "executed":
            return resp, status
        rules = risk_store.load()
        summary = summarize_state(gather_state(adapter), rules)
        body["risk_report"] = {"breaches": portfolio_breaches(summary, rules), "active_risk": summary["active_risk"],
                               "active_risk_percent": summary["active_risk_percent"], "currency": summary["currency"]}
        return jsonify(body), status
    except Exception:
        logger.exception("Could not build the post-modification risk report")
        return resp, status


@app.route("/api/position/<int:ticket>/modify", methods=["POST"])
def api_modify(ticket):
    """Change SL and/or TP. Include only the keys to change; null removes that level."""
    try:
        data = _payload()
        change_sl, change_tp = "sl" in data, "tp" in data
        if not (change_sl or change_tp):
            raise OrderRejectedError("Provide sl and/or tp.")
        sl, tp = _optional_price(data, "sl"), _optional_price(data, "tp")
    except OrderRejectedError as exc:
        return _reply("failed", str(exc), "invalid_request", False, {"ticket": ticket})
    response = _execute(lambda: adapter.modify_position(ticket, sl, tp, change_sl, change_tp),
                        {"ticket": ticket, "action": "modify", "sl": sl, "tp": tp})
    return _with_risk_report(response)


@app.route("/api/position/<int:ticket>/close", methods=["POST"])
def api_close(ticket):
    try:
        _payload()
    except OrderRejectedError as exc:
        return _reply("failed", str(exc), "invalid_request", False, {"ticket": ticket})
    return _execute(lambda: adapter.close_position(ticket), {"ticket": ticket, "action": "close"})


def _structure_config(args) -> StructureConfig:
    """settings.toml [structure] defaults, optionally overridden by validated query parameters.
    The newest candle from the broker is still forming, so it is never treated as closed."""
    st = settings.structure

    def whole(name, default):
        raw = args.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{name} must be a whole number.") from None

    return StructureConfig(swing_left=whole("swing_left", st.swing_left), swing_right=whole("swing_right", st.swing_right),
                           break_mode=args.get("break_mode", st.break_mode), equal_tolerance=st.equal_tolerance,
                           last_candle_closed=False)


@app.route("/api/candles")
def api_candles():
    """Candles for the chart, straight from MT5. With structure=1 the market-structure analysis of the SAME
    candles is added (analysis only: it never creates an order)."""
    symbol = request.args.get("symbol", "")
    timeframe = request.args.get("timeframe", "H1")
    want_structure = request.args.get("structure") == "1"
    try:
        structure_cfg = _structure_config(request.args) if want_structure else None
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc), "candles": []}), 400
    try:
        count = int(request.args.get("count", "300"))
        candles = adapter.get_candles(symbol, timeframe, count)
    except (ValueError, BrokerError) as exc:
        return jsonify({"ok": False, "message": str(exc), "candles": []})
    rows = [c.to_list() for c in candles]
    payload = {"ok": True, "symbol": symbol, "timeframe": timeframe.upper(), "candles": rows}
    structure = None
    if structure_cfg is not None:
        structure = analyze_cached(rows, structure_cfg)
        payload["structure"] = structure.to_dict()
        if request.args.get("liquidity") == "1":
            payload["liquidity"] = _liquidity_for_chart(symbol, rows, structure)
    if request.args.get("fvg") == "1":
        # Independent of structure=1 and of the saved fvg_visible setting: whether the zones are SHOWN is decided in the browser,
        # the analysis itself is never switched off by hiding them.
        payload["fvg"] = _fvg_for_chart(symbol, timeframe, rows)
    if request.args.get("order_blocks") == "1":
        # Same rule: independent of the saved order_blocks_visible setting. Order blocks are built on the structure result, so
        # when the caller did not ask for structure=1 the SAME structure configuration is run here without adding it to the reply.
        payload["order_blocks"] = _order_blocks_for_chart(symbol, timeframe, rows, structure if structure_cfg is not None else None)
    return jsonify(payload)


def _tick_size(symbol: str):
    try:
        return adapter.get_symbol_spec(symbol).tick_size
    except (BrokerError, NotImplementedError, AttributeError):
        return None


def _liquidity_for_chart(symbol, rows, structure):
    """Pools and sweeps for the chart overlay. Analysis only; a failure is reported, never raised, and never touches an order."""
    from trademate.liquidity import LiquidityConfig, analyze_liquidity, ticks_to_price
    st = chart_store.load()
    tick = _tick_size(symbol)
    if not (isinstance(tick, (int, float)) and tick > 0):
        return {"available": False, "reason": "The symbol's tick size is not available from MT5, so liquidity was not analysed."}
    try:
        cfg = LiquidityConfig(tolerance=ticks_to_price(st.liquidity_tolerance_ticks, tick), sweep_mode=st.liquidity_sweep_mode,
                              max_pools_per_side=st.liquidity_max_pools, last_candle_closed=structure.config.last_candle_closed)
        return {"available": True, **analyze_liquidity(rows, structure, cfg).to_dict()}
    except ValueError as exc:
        return {"available": False, "reason": f"Liquidity could not be analysed: {exc}"}
    except Exception:                                  # noqa: BLE001 - a bug here must never break the chart
        logger.exception("Liquidity analysis crashed")
        return {"available": False, "reason": "Liquidity could not be analysed because of an internal error (see the log)."}


def _order_blocks_for_chart(symbol, timeframe, rows, structure):
    """Order blocks for the chart overlay: the engine's own result for exactly these candles and this structure analysis (never
    recalculated in the browser). Analysis only: a failure is reported, never raised, and nothing here can touch an order.
    `available` is False (and no block is returned) when the engine reports insufficient / invalid / inconsistent data."""
    from trademate.orderblocks import DATA_OK, OrderBlockConfig, analyze_order_blocks
    try:
        if structure is None:
            structure = analyze_cached(rows, _structure_config(request.args))
        res = analyze_order_blocks(rows, structure, OrderBlockConfig(), symbol=symbol or None, timeframe=timeframe.upper())
        return {"available": res.data_status == DATA_OK, **res.to_dict()}
    except ValueError as exc:
        return {"available": False, "blocks": [], "reason": f"Order blocks could not be analysed: {exc}"}
    except Exception:                                  # noqa: BLE001 - a bug here must never break the chart
        logger.exception("Order block analysis crashed")
        return {"available": False, "blocks": [], "reason": "Order blocks could not be analysed because of an internal error (see the log)."}


def _fvg_for_chart(symbol, timeframe, rows):
    """Fair value gaps for the chart overlay: the engine's own result for exactly these candles (never recalculated in the browser).
    Analysis only: a failure is reported, never raised, and nothing here can touch an order. `available` is False (and no gap is
    returned) when the engine reports insufficient or invalid candles, so the chart never draws a guessed gap. The newest candle is
    still forming, so a gap completed by it comes back unconfirmed."""
    from trademate.fvg import DATA_OK, FVGConfig, analyze_fvg
    try:
        res = analyze_fvg(rows, FVGConfig(last_candle_closed=False), symbol=symbol or None, timeframe=timeframe.upper())
        return {"available": res.data_status == DATA_OK, **res.to_dict()}
    except ValueError as exc:
        return {"available": False, "gaps": [], "reason": f"Fair value gaps could not be analysed: {exc}"}
    except Exception:                                  # noqa: BLE001 - a bug here must never break the chart
        logger.exception("FVG analysis crashed")
        return {"available": False, "gaps": [], "reason": "Fair value gaps could not be analysed because of an internal error (see the log)."}


@app.route("/api/analysis")
def api_analysis():
    """What the Market Structure Engine has determined for one symbol / timeframe, with the evidence behind it.

    Read-only. Analysis only: no order, no entry, no stop, no target, no setup. Every field comes from the live candles of
    THIS request; a field that cannot be substantiated is reported as unavailable, never invented.
    """
    symbol = request.args.get("symbol", "").strip()
    timeframe = request.args.get("timeframe", "H1").strip().upper()
    if not symbol or len(symbol) > 32 or not symbol.isprintable():
        return jsonify({"ok": False, "message": "A symbol is required."}), 400
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return jsonify({"ok": False, "message": f"Unsupported timeframe. Use one of: {', '.join(SUPPORTED_TIMEFRAMES)}."}), 400
    try:
        cfg = _structure_config(request.args)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    status = adapter.get_connection_status()
    if status.state is not ConnectionState.CONNECTED:
        return jsonify(analysis_unavailable(symbol, timeframe, cfg, status.message, stale=status.age_seconds is not None))
    try:
        candles = adapter.get_candles(symbol, timeframe, FETCH_COUNT.get(timeframe, 500 if timeframe in ("H1", "H4") else 300))
    except BrokerError as exc:
        return jsonify(analysis_unavailable(symbol, timeframe, cfg, str(exc)))
    try:
        digits = adapter.get_symbol_spec(symbol).digits
    except (BrokerError, NotImplementedError, AttributeError):
        digits = None
    now_utc = time.time()
    try:
        server = resolve_server_clock(adapter, now_utc, first=(symbol,))
    except Exception:  # noqa: BLE001 - clock failure means the newest candle must remain provisional
        logger.exception("Could not resolve the broker clock for Analysis")
        server = None
    now_server = now_utc + server.offset_seconds if server is not None and server.known else None
    rows = [c.to_list() for c in candles]
    last_time = max((r[0] for r in rows if isinstance(r[0], (int, float))), default=None)
    closed = bool(now_server is not None and last_time is not None
                  and now_server >= last_time + TF_SECONDS.get(timeframe, 0))
    cfg = replace(cfg, last_candle_closed=closed)
    candle_clock = {"known": now_server is not None,
                    "note": (f"Broker clock verified ({server.source}); newest candle is "
                             f"{'closed' if closed else 'forming'}."
                             if now_server is not None else
                             "Broker clock could not be verified; newest candle is conservatively treated as forming.")}
    liq_ctx = {"settings": chart_store.load(), "tick_size": _tick_size(symbol)}
    return jsonify(build_analysis(symbol, timeframe, rows, cfg, digits, status.warnings, liq_ctx, candle_clock))


@app.route("/api/strategylab/status")
def api_strategylab_status():
    """What the Command Centre can really do now, including read-only monitoring state."""
    try:
        after_event_id = int(request.args.get("after_event_id", "0"))
    except ValueError:
        return jsonify({"ok": False, "message": "after_event_id must be an integer."}), 400
    if after_event_id < 0:
        return jsonify({"ok": False, "message": "after_event_id cannot be negative."}), 400
    status = adapter.get_connection_status()
    connected = status.state is ConnectionState.CONNECTED
    monitor = watchdog.status(after_event_id)
    return jsonify({"ok": True, "part": strategylab.IMPLEMENTED_PART, "broker": {"state": status.state.value, "connected": connected,
                                                                                "message": status.message},
                    "watchdog": monitor, "states": list(strategylab.STATES),
                    "capabilities": strategylab.capabilities(connected, monitor["state"] if monitor["active"] else None)})


@app.route("/api/strategylab/watchdog/start", methods=["POST"])
def api_strategylab_watchdog_start():
    """Start one read-only background analysis loop after validating the broker symbol."""
    try:
        data = _payload()
        symbol = data.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise OrderRejectedError("A broker-supported symbol is required.")
        status = adapter.get_connection_status()
        if status.state is not ConnectionState.CONNECTED:
            return jsonify({"ok": False, "message": "MT5 is not connected; monitoring was not started."}), 409
        info = adapter.get_symbol_info(symbol.strip())
        selected = strategy_registry.get_strategy(_selected_strategy_id())
        if selected is None:
            return jsonify({"ok": False, "message": "No valid strategy is selected; monitoring was not started."}), 409
        selection_store.save(symbol=info.name)
        ok, message = watchdog.start(info.name, selected.id, selection_store.timeframe(selected.id))
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except (BrokerError, NotImplementedError) as exc:
        return jsonify({"ok": False, "message": f"Could not validate the broker instrument: {exc}"}), 400
    if not ok:
        return jsonify({"ok": False, "message": message, "watchdog": watchdog.status()}), 409
    return jsonify({"ok": True, "message": message, "watchdog": watchdog.status()}), 202


@app.route("/api/strategylab/watchdog/stop", methods=["POST"])
def api_strategylab_watchdog_stop():
    """Stop the background monitor; this endpoint never places or changes an order."""
    _, message = watchdog.stop()
    return jsonify({"ok": True, "message": message, "watchdog": watchdog.status()})


def _rf_public() -> dict:
    """Rapid Fire is NOT one of the five selectable strategies: it is an independent engine with its own Start/Stop (Strategy page)."""
    from trademate.rapid_fire.models import PHILOSOPHY, STRATEGY_ID, STRATEGY_NAME, TIMEFRAME, VERSION
    return {"id": STRATEGY_ID, "name": STRATEGY_NAME, "version": VERSION, "independent": True, "timeframe": TIMEFRAME, "philosophy": PHILOSOPHY,
            "summary": "Very small, repeated M1 price moves: enter on a short move from the candle open, take profit quickly, at most two shots per candle."}


def _selection_state() -> dict:
    sel = strategy_store.load()
    defn = strategy_registry.get_strategy(sel["id"])
    return {"strategies": strategy_registry.public_catalogue(), "independent": [_rf_public()], "selected": defn.public() if defn else None, "selected_id": sel["id"] if defn else None,
            "stored": sel["stored"], "problem": sel["problem"], "epoch": strategy_store.epoch,
            "timeframes": list(strategy_registry.CATALOGUE[sel["id"]].public()["timeframes"]) if defn else []}


@app.route("/api/strategy/catalogue")
def api_strategy_catalogue():
    """The registered strategies and the current selection (recovered from disk after a restart)."""
    return jsonify({"ok": True, **_selection_state()})


@app.route("/api/strategy/select", methods=["POST"])
def api_strategy_select():
    """Select exactly one registered strategy. Unknown identifiers are rejected. A real change cancels pending approvals and resets the Watchdog."""
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    ok, message, previous = strategy_store.select(data.get("strategy"))
    status = 200 if ok else 400
    return jsonify({"ok": ok, "message": message, "previous_id": previous, **_selection_state()}), status


@app.route("/api/strategy/analyse")
def api_strategy_analyse():
    """Analyse one instrument with the SELECTED strategy (H1 context + M5 setup). Read-only: it proposes, never sizes or sends."""
    symbol = request.args.get("symbol", "").strip()
    if not symbol or len(symbol) > 32 or not symbol.isprintable():
        return jsonify({"ok": False, "message": "A symbol is required."}), 400
    epoch = strategy_store.epoch
    sid = _selected_strategy_id()
    if strategy_registry.get_strategy(sid) is None:
        return jsonify({"ok": False, "message": "No valid strategy is selected. Choose Candle Momentum, CHoCH Trading or Trend Continuation."}), 409
    tf = request.args.get("timeframe") or selection_store.timeframe(sid)
    if tf not in EXEC_TIMEFRAMES:
        return jsonify({"ok": False, "message": f"Unknown timeframe. Choose one of: {', '.join(EXEC_TIMEFRAMES)}."}), 400
    selection_store.save(symbol=symbol)
    ticket = decision_hub.begin()
    try:
        payload = evaluate_selected(adapter, sid, symbol, settings, now_utc=time.time(), exec_tf=tf, pip_overrides=_pip_overrides())
    except Exception:  # noqa: BLE001 - never leak internals; the log has them
        logger.exception("Strategy analysis failed for %s (%s)", symbol, sid)
        return jsonify({"ok": False, "message": "The analysis failed (see logs/trademate.log). No decision is shown."}), 500
    if strategy_store.epoch != epoch or _selected_strategy_id() != sid:      # the user switched strategy while this ran: the answer is stale
        return jsonify({"ok": False, "stale": True, "message": "The selected strategy changed while this analysis ran, so its result was discarded."}), 409
    if payload.get("ok") and payload.get("result") is not None:
        res = payload["result"]
        decision_hub.publish(ticket, symbol, sid, payload.get("exec_tf") or tf, state="fresh" if payload.get("connection") == "CONNECTED" else "disconnected",
                             message="Manual analysis.", result=res if payload.get("connection") == "CONNECTED" else None, source="manual")
    return jsonify({**payload, "epoch": epoch})


# ---------------------------------------------------------------------------------------------------------------------
# Command Centre: authoritative decision, selection (instrument / strategy / timeframe), lot size, Autopilot.
# ---------------------------------------------------------------------------------------------------------------------
def _valid_symbol(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 32 and value.strip().isprintable()


def _current_selection():
    sid = _selected_strategy_id()
    return selection_store.symbol() or trade_settings.main_instrument(), sid, selection_store.timeframe(sid)


@app.route("/api/decision/current")
def api_decision_current():
    """THE current decision for one exact (instrument, strategy, timeframe). The Command Centre panel renders this and nothing else, so a Watchdog
    result, a manual Analyse and the notification text can never disagree. Never returns another selection's decision."""
    symbol = request.args.get("symbol", "").strip()
    sid = request.args.get("strategy") or _selected_strategy_id()
    tf = request.args.get("timeframe") or selection_store.timeframe(sid)
    if not _valid_symbol(symbol):
        return jsonify({"ok": False, "message": "A symbol is required."}), 400
    rec = decision_hub.get(symbol, sid, tf)
    mon = watchdog.status()
    monitoring = bool(mon.get("active")) and mon.get("symbol") == symbol and mon.get("strategy_id") == sid and mon.get("exec_tf") == tf
    return jsonify({"ok": True, "symbol": symbol, "strategy_id": sid, "timeframe": tf, "version": decision_hub.version(), "monitoring": monitoring,
                    "monitor_state": mon.get("state") if monitoring else None, "record": rec, "server_time": time.time()})


@app.route("/api/selection", methods=["GET", "POST"])
def api_selection():
    """Instrument, strategy and execution timeframe from the side navigation. A change cancels pending approvals and re-points an active Watchdog
    (no stale result of the old selection can be shown: the decision is looked up by the NEW key)."""
    if request.method == "GET":
        sid = _selected_strategy_id()
        return jsonify({"ok": True, "symbol": selection_store.symbol() or trade_settings.main_instrument(), "strategy_id": sid, "timeframe": selection_store.timeframe(sid),
                        **_selection_state(), "timeframes": list(EXEC_TIMEFRAMES)})   # AFTER the state: the selector must offer every execution timeframe
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    old = _current_selection()
    symbol = data.get("symbol", old[0])
    sid = data.get("strategy", old[1])
    tf = data.get("timeframe")
    if autopilot.status()["armed"]:
        return jsonify({"ok": False, "message": "Stop Autopilot before changing the instrument, strategy or timeframe."}), 409
    if symbol is not None:
        if not _valid_symbol(symbol):
            return jsonify({"ok": False, "message": "Select a valid broker instrument."}), 400
        try:
            symbol = adapter.get_symbol_info(symbol.strip()).name
        except (BrokerError, NotImplementedError) as exc:
            return jsonify({"ok": False, "message": f"Could not validate the instrument: {exc}"}), 400
    if strategy_registry.get_strategy(sid) is None:
        return jsonify({"ok": False, "message": "Unknown strategy."}), 400
    if tf is not None and tf not in EXEC_TIMEFRAMES:
        return jsonify({"ok": False, "message": f"Unknown timeframe. Choose one of: {', '.join(EXEC_TIMEFRAMES)}."}), 400
    was_active = watchdog.status().get("active")
    with _Retarget():
        ok, message, _ = strategy_store.select(sid)
    if not ok:
        return jsonify({"ok": False, "message": message}), 400
    selection_store.save(symbol, sid, tf)
    new = _current_selection()
    if new != old:
        pipeline_store.invalidate_all("invalidated", "The instrument, strategy or timeframe changed.")
        if was_active and new[0]:
            watchdog.retarget(new[0], new[1], new[2])
    return jsonify({"ok": True, "symbol": new[0], "strategy_id": new[1], "timeframe": new[2], "changed": new != old, **_selection_state(), "timeframes": list(EXEC_TIMEFRAMES)})


def _lot_view(symbol):
    info = None
    try:
        info = adapter.get_symbol_spec(symbol)
    except (BrokerError, NotImplementedError):
        pass
    saved = lot_store.get(symbol)
    chk = validate_for_info(saved, info, _max_order_volume()) if (saved is not None and info is not None) else None
    return {"symbol": symbol, "saved": saved, "saved_valid": bool(chk and chk.ok), "volume_min": getattr(info, "volume_min", None),
            "volume_max": getattr(info, "volume_max", None), "volume_step": getattr(info, "volume_step", None), "cap": _max_order_volume(),
            "will_use": saved if (chk and chk.ok) else None,
            "message": (f"{saved:g} lots will be used for new entries." if chk and chk.ok else
                        chk.message if chk else "No lot size is saved: manual proposals use risk-based sizing and Autopilot will not start.")}


def _pip_view(symbol):
    """What one strategy pip/point is for an instrument, from the SAME central engine every strategy and the pipeline use
    (trademate.instruments.build_spec): detected automatically from the broker's contract; a saved override is optional and says so."""
    try:
        info = adapter.get_symbol_spec(symbol)
    except (BrokerError, NotImplementedError) as exc:
        return {"symbol": symbol, "ok": False, "message": f"The instrument's specification could not be read ({exc})."}
    spec = build_spec(info, _pip_overrides(), time.time(), symbol)
    pip = spec.to_pip_definition()
    source = ("browser" if symbol in pip_store.all() else "settings" if symbol in settings.pips.overrides else None) if spec.unit_source == "override" else \
             "automatic" if pip.ok else None
    st = settings.strategy
    preview = None
    if pip.ok:
        digits = int(getattr(info, "digits", 5) or 5)
        preview = [{"label": "Candle Momentum trigger", "pips": st.candle_trigger_pips}, {"label": "Candle Momentum target", "pips": st.candle_target_pips},
                   {"label": "CHoCH target", "pips": st.choch_target_pips}]
        for row in preview:
            row["price_distance"] = round(spec.units_to_price(row["pips"]), digits)
    try:
        per_lot = spec.money_per_lot()
    except ValueError:
        per_lot = None
    return {"symbol": symbol, "ok": True, "defined": pip.ok, "size": pip.size, "rule": spec.unit_source, "source": source, "message": pip.reason,
            "tick_size": getattr(info, "tick_size", None), "digits": getattr(info, "digits", None), "preview": preview,
            "status": spec.status, "unit_name": spec.unit_name, "asset_class": spec.asset_class, "problems": list(spec.problems),
            "notes": list(spec.notes), "money_per_unit_1lot": per_lot, "currency": spec.currency_profit}


@app.route("/api/pip", methods=["GET", "POST"])
def api_pip():
    """What one pip is for an instrument. The browser value is validated here (positive, finite, not finer than the instrument's tick) and wins over settings.toml."""
    if request.method == "GET":
        symbol = request.args.get("symbol", "").strip()
        if not _valid_symbol(symbol):
            return jsonify({"ok": False, "message": "A symbol is required."}), 400
        return jsonify(_pip_view(symbol))
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    symbol = data.get("symbol")
    if not _valid_symbol(symbol):
        return jsonify({"ok": False, "message": "A symbol is required."}), 400
    symbol = symbol.strip()
    if autopilot.status()["armed"]:
        return jsonify({"ok": False, "message": "Stop Autopilot before changing the pip size."}), 409
    if data.get("size") is None:
        pip_store.clear(symbol)
    else:
        raw = data.get("size")
        try:
            size = float(str(raw).strip()) if not isinstance(raw, bool) else float("nan")
        except ValueError:
            size = float("nan")
        if not (size == size and 0 < size < 1e6 and size not in (float("inf"),)):
            return jsonify({"ok": False, "message": "The pip size must be a number greater than zero (price units per pip, for example 0.0001 or 1)."}), 400
        try:
            tick = adapter.get_symbol_spec(symbol).tick_size
        except (BrokerError, NotImplementedError) as exc:
            return jsonify({"ok": False, "message": f"The instrument's specification could not be read ({exc}), so the pip size was not saved."}), 400
        if tick and size < tick - 1e-12:
            return jsonify({"ok": False, "message": f"A pip of {size:g} is smaller than this instrument's smallest price step ({tick:g}). Check the value."}), 400
        pip_store.set(symbol, size)
    pipeline_store.invalidate_all("invalidated", "The pip size changed.")
    decision_hub.clear()                                  # earlier analyses used the old pip size
    return jsonify(_pip_view(symbol))


@app.route("/api/lot", methods=["GET", "POST"])
def api_lot():
    """The lot size for FUTURE entries on one instrument. The browser's number is only a request: it is validated here, exactly, against the
    instrument's real min / max / step. Nothing is rounded and open positions are never touched."""
    if request.method == "GET":
        symbol = request.args.get("symbol", "").strip()
        if not _valid_symbol(symbol):
            return jsonify({"ok": False, "message": "A symbol is required."}), 400
        return jsonify({"ok": True, **_lot_view(symbol)})
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    symbol = data.get("symbol")
    if not _valid_symbol(symbol):
        return jsonify({"ok": False, "message": "A symbol is required."}), 400
    symbol = symbol.strip()
    try:
        info = adapter.get_symbol_spec(symbol)
    except (BrokerError, NotImplementedError) as exc:
        return jsonify({"ok": False, "message": f"The instrument's volume rules could not be read ({exc}), so no lot size was saved."}), 400
    chk = validate_for_info(data.get("volume"), info, _max_order_volume())
    if not chk.ok:
        return jsonify({"ok": False, "message": chk.message, "code": chk.code, **{k: v for k, v in _lot_view(symbol).items() if k.startswith("volume_")}}), 400
    lot_store.set(symbol, chk.volume)
    return jsonify({"ok": True, **_lot_view(symbol)})


# ---- Autopilot ---------------------------------------------------------------------------------------------------------
def _autopilot_propose(symbol, sid, tf, volume):
    rep = pipeline.propose(adapter, settings, risk_store.load(), _max_order_volume(), symbol, pipeline_store, evaluate=_strategy_for_pipeline,
                           strategy_id=sid, fixed_volume=volume, ledger=setup_ledger, max_entries=execution_settings.max_entries_per_candle())
    body = rep.to_dict()
    return {"approved": bool(body.pop("ok", False)), "approval_id": (body.get("approval") or {}).get("id"),
            "message": body.get("message"), "code": body.get("code")}


def _autopilot_execute_once(approval_id):
    """The SAME path as the manual confirm button: prepare_execution (re-validation + Risk gate + ledger) -> journal-backed place_order -> placement check."""
    with app.app_context():
        plan, refusal = pipeline.prepare_execution(adapter, settings, risk_store.load(), _max_order_volume(), pipeline_store, approval_id, True,
                                                   evaluate=_strategy_for_pipeline, strategy_id=_selected_strategy_id(), ledger=setup_ledger, max_entries=execution_settings.max_entries_per_candle())
        if refusal is not None:
            return {"outcome": "execution_refused", "ticket": None, "message": refusal.message, "order_sent": False, "code": refusal.code}
        box = {}

        def action():
            box["result"] = adapter.place_order(plan.symbol, plan.side, plan.volume, plan.stop_loss, plan.take_profit)
            return box["result"]

        response = _execute(action, {"approval_id": plan.approval_id, "symbol": plan.symbol, "side": plan.side, "volume": plan.volume,
                                     "sl": plan.stop_loss, "tp": plan.take_profit, "entry_reference": plan.entry_live, "source": "autopilot"})
        _realign_after_fill(plan, box.get("result"))
        resp, _status = response if isinstance(response, tuple) else (response, 200)
        body = resp.get_json() or {}
        pos = getattr(box.get("result"), "position", None)
        try:
            pipeline_store.record_outcome(plan.approval_id, {"outcome": body.get("outcome"), "state": body.get("state"), "execution_id": body.get("execution_id"), "source": "autopilot"})
        except Exception:                                  # noqa: BLE001
            logger.exception("Could not record the Autopilot outcome")
        return {"outcome": body.get("outcome"), "ticket": getattr(pos, "ticket", None), "message": body.get("message"), "order_sent": bool(body.get("submitted", body.get("order_sent"))),
                "fill_price": getattr(pos, "price_open", None), "broker": body.get("broker"), "candidate_id": plan.candidate_id, "volume": plan.volume}


def _margin_per_lot(symbol, side, price):
    """Margin required for 1 lot, or None. The MT5 bridge does not expose OrderCalcMargin, so this is None unless an adapter provides it: it is never guessed."""
    fn = getattr(adapter, "margin_per_lot", None)
    if fn is None:
        return None
    try:
        v = fn(symbol, side, price)
        return float(v) if v else None
    except Exception:                                                              # noqa: BLE001
        return None


def _autopilot_execute(approval_id):
    """Execute; if the broker refuses for insufficient funds, reduce the lot intelligently (lot_adjust.py), re-run EVERY risk check on the smaller size, and retry
    a bounded number of times. The same rejected order is never re-sent, the saved lot size is not changed, and the original/adjusted lots are reported."""
    res = _autopilot_execute_once(approval_id)
    broker = res.get("broker") or {}
    if res.get("outcome") != "broker_rejected" or not lot_adjust.is_insufficient_funds(broker.get("retcode"), broker.get("message") or res.get("message")):
        return res
    a = pipeline_store.get(approval_id)
    if a is None:
        return res
    if not execution_settings.get("auto_lot_adjust"):
        res["message"] = (res.get("message") or "") + " Automatic lot adjustment is switched off, so the order was not retried."
        return res
    original, sid, symbol = a.volume, a.core.get("strategy_id"), a.symbol
    rep_log, failed, current = [], [], res
    for attempt in range(1, execution_settings.get("lot_adjust_max_attempts") + 1):
        if current.get("candidate_id"):
            setup_ledger.release(current["candidate_id"])                          # the broker proved nothing was opened: free the entry slot for the reduced order
        failed.append(a.volume if attempt == 1 else rep_log[-1]["adjusted_lot"])
        try:
            info = adapter.get_symbol_spec(symbol)
            acct = adapter.get_account()                                           # 1-2: current balance, equity and free margin
            rules = risk_store.load()
            dist = abs(a.entry_live - a.stop_loss)
            plan = lot_adjust.plan_reduced_lot(
                original_lot=original, volume_min=info.volume_min, volume_max=min(info.volume_max or 0, _max_order_volume() or info.volume_max or 0) or info.volume_max,
                volume_step=info.volume_step, stop_distance=dist, tick_size=info.tick_size, tick_value_loss=info.tick_value_loss or info.tick_value,
                max_risk_amount=(acct.equity or 0) * rules.max_risk_per_trade_percent / 100.0, free_margin=acct.free_margin,
                margin_per_lot=_margin_per_lot(symbol, a.direction, a.entry_live), failed_lots=tuple(failed))
        except (BrokerError, NotImplementedError) as exc:
            rep_log.append({"attempt": attempt, "adjusted_lot": None, "reason": f"Account or instrument data could not be re-read ({exc}); no retry."})
            break
        if not plan.ok:
            rep_log.append({"attempt": attempt, "adjusted_lot": None, "reason": plan.reason})
            break
        tf = selection_store.timeframe(sid)
        prop = _autopilot_propose(symbol, sid, tf, plan.adjusted_lot)             # 8: the reduced size passes the WHOLE Risk Engine again
        if not prop.get("approved"):
            rep_log.append({"attempt": attempt, "adjusted_lot": plan.adjusted_lot, "reason": f"The Risk Engine did not approve the reduced lot {plan.adjusted_lot:g}: {prop.get('message')}"})
            break
        rep_log.append({"attempt": attempt, "adjusted_lot": plan.adjusted_lot, "reason": plan.reason})
        current = _autopilot_execute_once(prop["approval_id"])
        a = pipeline_store.get(prop["approval_id"]) or a
        b2 = current.get("broker") or {}
        if current.get("outcome") != "broker_rejected" or not lot_adjust.is_insufficient_funds(b2.get("retcode"), b2.get("message") or current.get("message")):
            break
    last = next((r for r in reversed(rep_log) if r["adjusted_lot"]), None)
    current["lot_adjustment"] = {"original_lot": original, "adjusted_lot": last["adjusted_lot"] if (last and current.get("outcome") == "executed") else None,
                                 "reason": rep_log[-1]["reason"] if rep_log else "", "attempts": rep_log}
    if current.get("outcome") != "executed":
        current["message"] = f"Not enough money for {original:g} lots. " + (rep_log[-1]["reason"] if rep_log else "") + " No further order was sent."
    else:
        current["message"] = f"Lot reduced from {original:g} to {current['lot_adjustment']['adjusted_lot']:g} (insufficient funds). " + (current.get("message") or "")
    return current


def _autopilot_precheck():
    if safety_lock.locked:
        return "EMERGENCY LOCKED: " + (safety_lock.status()["reason"] or "Emergency Stop was pressed") + ". Lift the emergency lock in Settings before starting Autopilot."
    if not settings.trading.enabled:
        return "Trading is disabled in settings ([trading] enabled)."
    status = adapter.get_connection_status()
    if status.state is not ConnectionState.CONNECTED:
        return f"MT5 is not connected ({status.message})."
    if not watchdog.status().get("active"):
        return "Start the Watchdog first: Autopilot acts on the setups it monitors."
    sym, sid, tf = _current_selection()
    mon = watchdog.status()
    if (mon.get("symbol"), mon.get("strategy_id"), mon.get("exec_tf")) != (sym, sid, tf):
        return "The Watchdog is monitoring a different instrument, strategy or timeframe than the one selected."
    try:                                                    # the same central conversion engine every strategy uses: an UNVERIFIED instrument must not trade automatically
        spec = build_spec(adapter.get_symbol_spec(sym), _pip_overrides(), time.time(), sym)
    except (BrokerError, NotImplementedError) as exc:
        return f"The specification of {sym} could not be read from MT5 ({exc}), so its pip/point conversion is UNVERIFIED and Autopilot is blocked."
    if not spec.verified:
        return f"The pip/point conversion for {sym} is UNVERIFIED, so Autopilot is blocked: " + " ".join(spec.problems)
    return None


def _autopilot_history():
    return [{"ticket": t.ticket, "net": t.net, "time_close": t.time_close} for t in adapter.get_history(30, 500)]


def _currency():
    try:
        return adapter.get_account().currency
    except BrokerError:
        return None


autopilot = Autopilot(settings.app.data_dir / "autopilot_state.json", selection=_current_selection, hub_get=decision_hub.get, lot_for=_lot_for,
                      propose=_autopilot_propose, execute=_autopilot_execute, positions=lambda: {p.ticket for p in adapter.get_positions()}, history=_autopilot_history,
                      precheck=_autopilot_precheck, currency=_currency, poll_seconds=settings.autopilot.poll_seconds,
                      max_decision_age=settings.autopilot.max_decision_age_seconds, max_runs=settings.autopilot.max_runs_limit, max_hours=settings.autopilot.max_hours_limit,
                      observer=lambda ev: (candle_engine.report_execution(ev) if ev.get("strategy_id") == "candle_momentum" else
                                           bull_bear_engine.report_execution(ev) if ev.get("strategy_id") == "bull_bear" else None),
                      max_entries=execution_settings.max_entries_per_candle)

# Event-driven hand-off: a validated Candle Momentum signal wakes the Autopilot immediately (no waiting for its poll), and a bounded retry may free ONE setup.
candle_engine.on_signal = lambda symbol, tf, candle_id: autopilot.wake()
bull_bear_engine.on_signal = lambda symbol, tf, candle_id: autopilot.wake()
bull_bear_engine.release_hook = lambda candle_id: (setup_ledger.release(candle_id), autopilot.forget_attempt(candle_id))
candle_engine.release_hook = lambda candle_id: (setup_ledger.release(candle_id), autopilot.forget_attempt(candle_id))


@app.route("/api/autopilot/status")
def api_autopilot_status():
    try:
        after = int(request.args.get("after_event_id", "0"))
    except ValueError:
        after = 0
    return jsonify({**autopilot.status(after), "modes": [{"id": m, "label": MODE_LABELS[m]} for m in MODES], "available": True})


@app.route("/api/autopilot/start", methods=["POST"])
def api_autopilot_start():
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    ok, message = autopilot.start(data.get("mode"), data.get("value"), resume=data.get("resume") is True)
    return jsonify({"ok": ok, "message": message, **autopilot.status()}), (200 if ok else 400)


@app.route("/api/autopilot/stop", methods=["POST"])
def api_autopilot_stop():
    ok, message = autopilot.stop("Stopped by the user.")
    return jsonify({"ok": ok, "message": message, **autopilot.status()})


@app.route("/api/autopilot/pause", methods=["POST"])
def api_autopilot_pause():
    """PAUSE: no new trades; the session configuration is kept and open trades are still tracked. Resume continues the same session."""
    ok, message = autopilot.pause()
    return jsonify({"ok": ok, "message": message, **autopilot.status()}), (200 if ok else 400)


@app.route("/api/autopilot/resume-pause", methods=["POST"])
def api_autopilot_resume_pause():
    ok, message = autopilot.unpause()
    if ok and safety_lock.locked:
        autopilot.pause()
        return jsonify({"ok": False, "message": "EMERGENCY LOCKED: lift the lock first.", **autopilot.status()}), 400
    return jsonify({"ok": ok, "message": message, **autopilot.status()}), (200 if ok else 400)


@app.route("/api/autopilot/emergency-stop", methods=["POST"])
def api_autopilot_emergency_stop():
    """EMERGENCY STOP: no new orders from ANY route (the Risk Engine refuses while the lock is set), pending approvals are cancelled, the Autopilot session ends.
    Open positions are NOT closed unless the user switched on `emergency_close_positions` in Settings (documented in docs/AUTOPILOT.md); then every open position is closed."""
    reason = "Emergency stop pressed: no new trades will be opened."
    safety_lock.lock(reason)
    autopilot.stop(reason, emergency=True)
    if "rapid_engine" in globals():
        rapid_engine.stop()                                                      # Rapid Fire: no new entries (open positions stay managed unless the policy below closes them)
    pipeline_store.invalidate_all("invalidated", "Emergency stop.")
    closed, failed = [], []
    if execution_settings.get("emergency_close_positions"):
        try:
            for p in adapter.get_positions():
                try:
                    adapter.close_position(p.ticket)
                    closed.append(p.ticket)
                except BrokerError as exc:
                    failed.append({"ticket": p.ticket, "error": str(exc)})
        except BrokerError as exc:
            failed.append({"error": str(exc)})
    msg = "Emergency stop done. EMERGENCY LOCKED: no new trades will be opened." + (f" Closed positions: {closed}." if closed else " Open positions were not closed.") + (f" Could not close: {failed}." if failed else "")
    return jsonify({"ok": True, "message": msg, "emergency_locked": True, "closed_positions": closed, "close_failures": failed, "positions_closed_by_policy": bool(execution_settings.get("emergency_close_positions")),
                    **autopilot.status()})


@app.route("/api/trade-settings", methods=["GET", "POST"])
def api_trade_settings():
    """GET ?symbol=&strategy= -> effective values + where each came from. POST {scope, symbol?, strategy?, values:{...}} saves; {scope, ..., reset:true} clears that scope's saved values."""
    if request.method == "GET":
        sym, sid = request.args.get("symbol") or None, request.args.get("strategy") or None
        r = trade_settings.resolve(sym, sid)
        return jsonify({"ok": True, **r, "builtin_defaults": __import__("trademate.trade_settings", fromlist=["BUILTIN"]).BUILTIN, "strategy_defaults": {"choch": settings.strategy.choch_target_pips, "market_structure": settings.strategy.market_structure_target_pips, "bull_bear": settings.strategy.bull_bear_target_pips}, "scopes": ["strategy_instrument", "instrument", "strategy", "global"]})
    data = _payload()
    try:
        if data.get("reset") is True:
            r = trade_settings.clear(data.get("scope", ""), data.get("symbol"), data.get("strategy"), data.get("keys"))
        else:
            r = trade_settings.save(data.get("scope", ""), data.get("values") or {}, data.get("symbol"), data.get("strategy"))
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, **r})


@app.route("/api/calc/trade", methods=["POST"])
def api_calc_trade():
    """Money preview for a target / protective-stop distance (strategy units) and the reverse lot calculation. Uses the broker's own tick data; untrusted -> no numbers."""
    data = _payload()
    symbol = (data.get("symbol") or selection_store.symbol() or trade_settings.main_instrument()).strip()
    num = lambda k: (float(data[k]) if isinstance(data.get(k), (int, float)) and not isinstance(data.get(k), bool) else (float(data[k]) if isinstance(data.get(k), str) and data[k].strip().replace(".", "", 1).isdigit() else None))
    try:
        info = adapter.get_symbol_spec(symbol)
        acct = adapter.get_account()
    except (BrokerError, NotImplementedError) as exc:
        return jsonify({"ok": True, "trusted": False, "reasons": [str(exc) or "MT5 data is not available."], "message": "This calculation cannot be trusted: broker data is unavailable. " + str(exc)})
    spec = build_spec(info, _pip_overrides(), time.time(), symbol)
    res = calc_trade(volume=num("volume"), target_units=num("target"), stop_units=num("stop"), unit_size=spec.unit, tick_size=getattr(info, "tick_size", None),
                     tick_value_profit=getattr(info, "tick_value_profit", None) or getattr(info, "tick_value", None), tick_value_loss=getattr(info, "tick_value_loss", None) or getattr(info, "tick_value", None),
                     balance=acct.balance, equity=acct.equity, currency=acct.currency or "", volume_min=info.volume_min, volume_max=info.volume_max, volume_step=info.volume_step,
                     verified=spec.verified, problems=spec.problems, max_risk_amount=num("max_risk"))
    return jsonify({"ok": True, "symbol": symbol, **res})


_notif_sync = {"at": 0.0}


def _sync_notifications(force=False):
    """Create notifications for newly completed trades (once per ticket) and newly reached locks. A broker/history failure creates nothing and changes nothing."""
    now = time.time()
    if not force and now - _notif_sync["at"] < 3.0:
        return
    _notif_sync["at"] = now
    try:
        notifications.sync_closed_trades(adapter.get_history(3, 500))
    except (BrokerError, NotImplementedError):
        pass
    g = loss_guard.status()
    if g["locked"]:
        notifications.add(f"trading_locked:{g['resets']}:{g['count']}", "trading_locked", "TRADING LOCKED", g["reason"] or "", "loss")
    if safety_lock.locked:
        notifications.add(f"emergency_lock:{safety_lock.status()['since']}", "emergency", "EMERGENCY LOCKED", safety_lock.status()["reason"] or "", "loss")


@app.route("/api/notifications")
def api_notifications():
    """?after=<seq>: items newer than seq. ?baseline=1: only the latest seq (a fresh page must not replay or play sounds for old items) plus recent history for display."""
    _sync_notifications()
    if request.args.get("baseline") == "1":
        return jsonify({"ok": True, "latest_seq": notifications.latest_seq, "items": [], "history": notifications.recent(50), "sound_enabled": execution_settings.get("sound_enabled")})
    try:
        after = max(0, int(request.args.get("after", "0")))
    except ValueError:
        after = 0
    return jsonify({"ok": True, "latest_seq": notifications.latest_seq, "items": notifications.since(after), "sound_enabled": execution_settings.get("sound_enabled")})


@app.route("/api/sessions")
def api_sessions():
    symbols, selected = [], request.args.get("symbol") or selection_store.symbol() or trade_settings.main_instrument()
    try:
        symbols = [s.name for s in adapter.get_symbols()]
    except (BrokerError, NotImplementedError):
        pass
    cls = lambda name: classify_instrument(name)
    return jsonify({"ok": True, **trading_sessions.snapshot(datetime.now(timezone.utc), symbols, selected, cls), "symbols_known": bool(symbols)})


def _autopilot_running() -> bool:
    return bool(autopilot.status().get("armed"))


@app.route("/api/hard-reset", methods=["GET", "POST"])
def api_hard_reset():
    """GET: exactly what would be reset and what is kept. POST {confirm:"HARD RESET"}: back up, then reset SETTINGS only (never history, risk records or code)."""
    if request.method == "GET":
        return jsonify({"ok": True, **hard_reset.plan(settings.app.data_dir), "backups": hard_reset.backups(settings.app.data_dir)})
    ok, message, detail = hard_reset.perform(settings.app.data_dir, str(_payload().get("confirm", "")), autopilot_running=_autopilot_running(), locked=safety_lock.locked)
    return jsonify({"ok": ok, "message": message, **detail}), (200 if ok else 400)


@app.route("/api/hard-reset/restore", methods=["POST"])
def api_hard_reset_restore():
    ok, message = hard_reset.restore(settings.app.data_dir, str(_payload().get("name", "")), autopilot_running=_autopilot_running())
    return jsonify({"ok": ok, "message": message}), (200 if ok else 400)


@app.route("/api/chart/indicators", methods=["GET", "POST"])
def api_chart_indicators():
    if request.method == "GET":
        return jsonify({"ok": True, "ma": indicator_store.get_ma()})
    try:
        return jsonify({"ok": True, "ma": indicator_store.set_ma(_payload().get("ma") or {})})
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc), "ma": indicator_store.get_ma()}), 400


@app.route("/api/chart/drawings", methods=["GET", "POST"])
def api_chart_drawings():
    """The user's MANUAL lines for one symbol. Automatic strategy trendlines are never stored here."""
    if request.method == "GET":
        sym = request.args.get("symbol", "").strip()
        return jsonify({"ok": True, "symbol": sym, "lines": drawing_store.get(sym) if sym else []})
    data = _payload()
    try:
        lines = drawing_store.set(str(data.get("symbol", "")).strip(), data.get("lines"))
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "lines": lines})


@app.route("/api/safety/status")
def api_safety_status():
    return jsonify({"ok": True, "emergency": safety_lock.status(), "consecutive_losses": loss_guard.status(), "settings": execution_settings.get_all()})


@app.route("/api/safety/unlock", methods=["POST"])
def api_safety_unlock():
    ok, message = safety_lock.unlock(str(_payload().get("confirm", "")))
    return jsonify({"ok": ok, "message": message, "emergency": safety_lock.status()}), (200 if ok else 400)


@app.route("/api/risk/consecutive", methods=["GET"])
def api_consecutive_status():
    _sync_loss_guard(force=True)
    return jsonify({"ok": True, **loss_guard.status()})


@app.route("/api/risk/consecutive/reset", methods=["POST"])
def api_consecutive_reset():
    ok, message = loss_guard.reset(str(_payload().get("confirm", "")))
    return jsonify({"ok": ok, "message": message, **loss_guard.status()}), (200 if ok else 400)


@app.route("/api/execution-settings", methods=["GET", "POST"])
def api_execution_settings():
    if request.method == "GET":
        return jsonify({"ok": True, "settings": execution_settings.get_all()})
    try:
        saved = execution_settings.update(_payload())
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc), "settings": execution_settings.get_all()}), 400
    return jsonify({"ok": True, "settings": saved, "consecutive_losses": loss_guard.status()})


_guard_sync = {"at": 0.0}


def _sync_loss_guard(force=False):
    """Fold newly completed trades into the consecutive-loss counter (each ticket once). A history read failure keeps the stored count (it never unlocks)."""
    now = time.time()
    if not force and now - _guard_sync["at"] < 5.0:
        return
    _guard_sync["at"] = now
    try:
        loss_guard.sync(adapter.get_history(3, 500))
    except (BrokerError, NotImplementedError) as exc:
        logger.warning("Consecutive-loss counter not refreshed: %s", exc)


def _central_protections():
    _sync_loss_guard()
    return protection_checks(safety_lock, loss_guard)


set_protections_provider(_central_protections)           # EVERY evaluate_trade() call now includes the emergency lock and the consecutive-loss limit


def _reversal_whatif(closing_at_loss: bool) -> list:
    """Anti-Twin PRE-FLIGHT: the loss protections that the reversal's OWN closing loss could trip. Closing at a loss adds one consecutive loss; if that reaches the limit,
    the opposite order would be refused AFTER the original was closed. Judged before anything is closed, so the original is simply left alone. Read-only."""
    _sync_loss_guard()
    g = loss_guard.status()
    if closing_at_loss and g["enabled"] and not g["locked"] and g["count"] + 1 >= g["threshold"]:
        return [{"name": "Consecutive losses", "passed": False,
                 "detail": (f"Closing this position at a loss would make {g['count'] + 1} consecutive losses, which reaches the limit of {g['threshold']}: the Risk Engine would then refuse "
                            "the opposite order. The original is left open.")}]
    return []


# ---- RAPID FIRE (independent M1 strategy; see docs/RAPID_FIRE.md) -------------------------------------------------------------------
# Uses the SAME central pieces as every other order: the Risk gate (gate_order), the journaled adapter, the emergency lock and loss protections.
from trademate import rapid_fire as rf_pkg
from trademate.rapid_fire import metrics as rf_metrics
from trademate.rapid_fire.engine import RapidFireEngine
from trademate.rapid_fire.executor import Executor as RFExecutor
from trademate.rapid_fire.store import ConfigStore as RFConfigStore, Ledger as RFLedger
from trademate.risk.account_state import set_software_stops_provider


class _LiveAdapter:
    """Always the CURRENT `adapter` of this module (so a replaced adapter is never held by a stale reference)."""
    def __getattr__(self, name):
        return getattr(adapter, name)


def _rf_guard():
    if not settings.trading.enabled:
        return "Trading is disabled in settings.toml ([trading] enabled = false)."
    if settings.mode.value == "live":
        return "Rapid Fire does not run in LIVE mode: unattended live trading is not enabled."
    if safety_lock.status().get("locked"):
        return "EMERGENCY LOCKED: lift the emergency lock in Settings first."
    return None


def _rf_server_offset():
    try:
        return resolve_server_clock(adapter, time.time()).offset_seconds
    except Exception:                                    # noqa: BLE001
        return None


rf_config_store = RFConfigStore(settings.app.data_dir / "rapid_fire_config.json")
rf_ledger = RFLedger(settings.app.data_dir / "rapid_fire_state.json")
rapid_engine = RapidFireEngine(_LiveAdapter(), rf_config_store, rf_ledger,
                               RFExecutor(_LiveAdapter(), lambda: risk_store.load(), lambda: _max_order_volume(), _reversal_whatif),
                               guard=_rf_guard, server_offset=_rf_server_offset, event_path=settings.app.data_dir / "rapid_fire_events.jsonl")
rf_pkg.set_engine(rapid_engine)
# Bull/Bear Anti-Twin: the same central Risk gate and journaled adapter as every other order; the Emergency Stop lock has priority over any reversal.
bull_bear_engine.anti_twin = AntiTwin(RFExecutor(_LiveAdapter(), lambda: risk_store.load(), lambda: _max_order_volume(), _reversal_whatif), settings.app.data_dir / "bull_bear_antitwin.json",
                                      bb_events, locked=lambda: bool(safety_lock.status().get("locked")))
# Candle Momentum Anti-Twin: the same rule, its OWN ledger file and instance.
candle_engine.anti_twin = AntiTwin(RFExecutor(_LiveAdapter(), lambda: risk_store.load(), lambda: _max_order_volume(), _reversal_whatif), settings.app.data_dir / "candle_momentum_antitwin.json",
                                      bb_events, locked=lambda: bool(safety_lock.status().get("locked")))
set_software_stops_provider(rapid_engine.software_stops)   # Rapid Fire positions have no broker stop: their emergency level is their risk distance
if rf_ledger.has_live():                                    # a restart never leaves an open Rapid Fire position unmanaged (entries stay OFF until Start)
    rapid_engine.ensure_thread()


@app.route("/api/rapid-fire/status")
def api_rf_status():
    return jsonify(rapid_engine.status())


@app.route("/api/rapid-fire/config", methods=["GET", "POST"])
def api_rf_config():
    if request.method == "GET":
        return jsonify({"ok": True, "config": rapid_engine.config.to_dict()})
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc), "problems": [str(exc)]}), 400
    if rapid_engine.running and any(k in data for k in ("symbol",)):
        return jsonify({"ok": False, "message": "Stop Rapid Fire before changing the instrument.", "problems": ["Stop Rapid Fire before changing the instrument."]}), 409
    cfg, problems = rapid_engine.save_config(data)
    if cfg is None:
        return jsonify({"ok": False, "message": problems[0], "problems": problems}), 400
    return jsonify({"ok": True, "config": cfg.to_dict(), "note": "Open positions keep the levels they were opened with."})


@app.route("/api/rapid-fire/start", methods=["POST"])
def api_rf_start():
    try:
        _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    ok, message = rapid_engine.start()
    return jsonify({"ok": ok, "message": message, **{"status": rapid_engine.status()}}), (200 if ok else 409)


@app.route("/api/rapid-fire/stop", methods=["POST"])
def api_rf_stop():
    try:
        _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "message": rapid_engine.stop(), "status": rapid_engine.status()})


# ---------------------------------------------------------------------------------------------------- Trading Journal (the user's own notepad)
trade_journal = TradeJournal(settings.app.data_dir / "trade_journal.json")


@app.route("/api/trade-journal", methods=["GET", "POST"])
def api_trade_journal():
    if request.method == "GET":
        return jsonify({"ok": True, "entries": trade_journal.list(), "results": list(JOURNAL_RESULTS), "emotions": list(JOURNAL_EMOTIONS)})
    data = request.get_json(silent=True)
    entry, problems = trade_journal.create(data if isinstance(data, dict) else {})
    if problems:
        return jsonify({"ok": False, "problems": problems, "message": " ".join(problems)}), 400
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/trade-journal/<eid>", methods=["PUT", "DELETE"])
def api_trade_journal_entry(eid):
    if request.method == "DELETE":
        return (jsonify({"ok": True}) if trade_journal.delete(eid) else (jsonify({"ok": False, "message": "That journal entry does not exist."}), 404))
    data = request.get_json(silent=True)
    entry, problems = trade_journal.update(eid, data if isinstance(data, dict) else {})
    if problems:
        return jsonify({"ok": False, "problems": problems, "message": " ".join(problems)}), (404 if problems == ["That journal entry does not exist."] else 400)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/trade-journal/import-rapid-fire", methods=["POST"])
def api_trade_journal_import_rf():
    """Document the closed Rapid Fire trades: new entries start with empty emotion and notes; existing entries keep every word the user wrote."""
    res = trade_journal.import_rapid_fire(rapid_engine.trade_rows(limit=1000))
    return jsonify({"ok": True, **res, "message": f"{res['created']} trade(s) added to the journal, {res['refreshed']} refreshed. Your notes and emotions are never changed by this."})


@app.route("/api/rapid-fire/chart-levels")
def api_rf_chart_levels():
    """Entry-linked exit levels of the open Rapid Fire positions (software-managed TP and emergency exit), for the chart. Read-only."""
    return jsonify(rapid_engine.chart_levels())


@app.route("/api/rapid-fire/trades")
def api_rf_trades():
    return jsonify({"ok": True, "trades": rapid_engine.trade_rows(), "currency": rapid_engine.status()["instrument"]["currency"]})


@app.route("/api/rapid-fire/trades/<tid>/resolve", methods=["POST"])
def api_rf_resolve(tid):
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    ticket = data.get("ticket")
    ok, message = rapid_engine.resolve_entry(tid, str(data.get("outcome") or ""), ticket if isinstance(ticket, int) and not isinstance(ticket, bool) else None)
    return jsonify({"ok": ok, "message": message}), (200 if ok else 400)


@app.route("/api/rapid-fire/lot-suggestion")
def api_rf_lot_suggestion():
    """The largest valid lot whose ESTIMATED gross profit at the take-profit distance does not exceed the target (account currency). An estimate from broker tick data."""
    try:
        profit = float(request.args.get("profit", ""))
    except ValueError:
        return jsonify({"ok": False, "message": "Enter the target profit as a number."}), 400
    cfg = rapid_engine.config
    try:
        info = adapter.get_symbol_spec(cfg.symbol)
        currency = adapter.get_account().currency
    except (BrokerError, NotImplementedError) as exc:
        return jsonify({"ok": False, "message": str(exc) or "MT5 data is not available."})
    res = rf_metrics.suggest_lot(info, profit, cfg.target_points)
    return jsonify({"ok": res["lot"] is not None, "currency": currency, "symbol": info.name, "target_points": cfg.target_points, **res,
                    "note": "Account currency only: TradeMate does not convert to other currencies (for example naira) because it has no exchange rate. Estimate, before spread and costs."})


# ---- Candle Momentum engine ----------------------------------------------------------------------------------------------
def _cm_key_args():
    symbol = request.args.get("symbol", "").strip() or (selection_store.symbol() or "")
    tf = request.args.get("timeframe") or selection_store.timeframe("candle_momentum")
    return symbol, tf


@app.route("/api/candle-momentum/state")
def api_cm_state():
    """The engine's live view of one (instrument, timeframe) PLUS the Watchdog and Autopilot status. The lifecycle inside is the same record the decision hub holds."""
    symbol, tf = _cm_key_args()
    if not _valid_symbol(symbol) or tf not in EXEC_TIMEFRAMES:
        return jsonify({"ok": False, "message": "A valid instrument and timeframe are required."}), 400
    snap = candle_engine.snapshot(symbol, tf)
    mon = watchdog.status()
    ap = autopilot.status()
    rec = decision_hub.get(symbol, "candle_momentum", tf)
    return jsonify({"ok": True, **snap, "watchdog": {"active": bool(mon.get("active")) and mon.get("symbol") == symbol and mon.get("exec_tf") == tf and mon.get("strategy_id") == "candle_momentum",
                                                      "state": mon.get("state"), "message": mon.get("message"), "mode": mon.get("mode")},
                    "autopilot": {"armed": ap.get("armed"), "state": ap.get("state"), "message": ap.get("message")},
                    "decision": {"state": rec.get("state"), "decision": rec.get("decision"), "age_seconds": rec.get("age_seconds"), "source": rec.get("source")} if rec else None,
                    "lot": lot_store.get(symbol)})


@app.route("/api/candle-momentum/config", methods=["GET", "POST"])
def api_cm_config():
    """Per-(instrument, timeframe) settings. Validated on write; distances are strategy units converted by the central instrument engine."""
    if request.method == "GET":
        symbol, tf = _cm_key_args()
        if not _valid_symbol(symbol) or tf not in EXEC_TIMEFRAMES:
            return jsonify({"ok": False, "message": "A valid instrument and timeframe are required."}), 400
        cfg, custom = candle_engine.effective_config(symbol, tf)
        return jsonify({"ok": True, "config": cfg.to_dict(), "customised": custom, "timeframes": list(EXEC_TIMEFRAMES)})
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    symbol, tf = data.get("symbol"), data.get("timeframe")
    if not _valid_symbol(symbol) or tf not in EXEC_TIMEFRAMES:
        return jsonify({"ok": False, "message": "A valid instrument and timeframe are required."}), 400
    if autopilot.status()["armed"]:
        return jsonify({"ok": False, "message": "Stop Autopilot before changing Candle Momentum settings."}), 409
    base, _ = candle_engine.effective_config(symbol.strip(), tf)
    try:
        cfg = CMConfig.from_dict({k: v for k, v in data.items() if k not in ("symbol", "timeframe")}, base)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "The settings could not be read."}), 400
    if data.get("reset") is True:
        cm_config_store.reset(symbol.strip(), tf)
        candle_engine.reload_config(symbol.strip(), tf)
        cfg, _ = candle_engine.effective_config(symbol.strip(), tf)
        return jsonify({"ok": True, "config": cfg.to_dict(), "message": "Settings reset to the defaults."})
    problems = cm_validate(cfg, EXEC_TIMEFRAMES)
    if problems:
        return jsonify({"ok": False, "message": " ".join(problems)}), 400
    problems = cm_config_store.save(cfg)
    if problems:
        return jsonify({"ok": False, "message": " ".join(problems)}), 400
    candle_engine.reload_config(symbol.strip(), tf)
    pipeline_store.invalidate_all("invalidated", "Candle Momentum settings changed.")
    return jsonify({"ok": True, "config": cfg.to_dict(), "message": "Candle Momentum settings saved."})


@app.route("/api/candle-momentum/events")
def api_cm_events():
    """Structured diagnostics (every event carries its candle id). Filter by candle_id / symbol / timeframe / after_id."""
    try:
        after, limit = int(request.args.get("after_id", "0")), int(request.args.get("limit", "200"))
    except ValueError:
        return jsonify({"ok": False, "message": "after_id and limit must be integers."}), 400
    return jsonify({"ok": True, "events": cm_events.tail(after, request.args.get("candle_id"), request.args.get("symbol"), request.args.get("timeframe"), limit),
                    "latest_id": cm_events.latest_id()})


# ---- Bull/Bear engine (structure-gated Candle Momentum copy + Anti-Twin) -------------------------------------------------------
def _bb_key_args():
    symbol = request.args.get("symbol", "").strip() or (selection_store.symbol() or "")
    tf = request.args.get("timeframe") or selection_store.timeframe("bull_bear")
    return symbol, tf


@app.route("/api/bull-bear/state")
def api_bb_state():
    symbol, tf = _bb_key_args()
    if not _valid_symbol(symbol) or tf not in EXEC_TIMEFRAMES:
        return jsonify({"ok": False, "message": "A valid instrument and timeframe are required."}), 400
    snap = bull_bear_engine.snapshot(symbol, tf)
    mon, ap = watchdog.status(), autopilot.status()
    rec = decision_hub.get(symbol, "bull_bear", tf)
    if snap.get("machine") is None and not snap.get("structure"):
        snap["structure"] = None
    return jsonify({"ok": True, **snap, "watchdog": {"active": bool(mon.get("active")) and mon.get("symbol") == symbol and mon.get("exec_tf") == tf and mon.get("strategy_id") == "bull_bear",
                                                      "state": mon.get("state"), "message": mon.get("message"), "mode": mon.get("mode")},
                    "autopilot": {"armed": ap.get("armed"), "state": ap.get("state"), "message": ap.get("message")},
                    "decision": {"state": rec.get("state"), "decision": rec.get("decision"), "age_seconds": rec.get("age_seconds"), "source": rec.get("source")} if rec else None,
                    "emergency_locked": bool(safety_lock.status().get("locked"))})


@app.route("/api/bull-bear/config", methods=["GET", "POST"])
def api_bb_config():
    """Per-(instrument, timeframe) Bull/Bear settings: the Candle Momentum-compatible ones plus the independent Anti-Twin ones. Validated on write."""
    if request.method == "GET":
        symbol, tf = _bb_key_args()
        if not _valid_symbol(symbol) or tf not in EXEC_TIMEFRAMES:
            return jsonify({"ok": False, "message": "A valid instrument and timeframe are required."}), 400
        cfg, custom = bull_bear_engine.effective_config(symbol, tf)
        return jsonify({"ok": True, "config": cfg.to_dict(), "customised": custom, "timeframes": list(EXEC_TIMEFRAMES)})
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    symbol, tf = data.get("symbol"), data.get("timeframe")
    if not _valid_symbol(symbol) or tf not in EXEC_TIMEFRAMES:
        return jsonify({"ok": False, "message": "A valid instrument and timeframe are required."}), 400
    if autopilot.status()["armed"] and data.get("reset") is not True and not set(data) <= {"symbol", "timeframe"} | {k for k in data if k.startswith("at_") or k == "anti_twin_enabled"}:
        return jsonify({"ok": False, "message": "Stop Autopilot before changing Bull/Bear entry settings."}), 409
    base, _ = bull_bear_engine.effective_config(symbol.strip(), tf)
    if data.get("reset") is True:
        bb_config_store.reset(symbol.strip(), tf)
        bull_bear_engine.reload_config(symbol.strip(), tf)
        cfg, _ = bull_bear_engine.effective_config(symbol.strip(), tf)
        return jsonify({"ok": True, "config": cfg.to_dict(), "message": "Settings reset to the defaults."})
    try:
        cfg = BBConfig.from_dict({k: v for k, v in data.items() if k not in ("symbol", "timeframe")}, base)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "The settings could not be read."}), 400
    problems = bb_validate(cfg, EXEC_TIMEFRAMES) or bb_config_store.save(cfg)
    if problems:
        return jsonify({"ok": False, "message": " ".join(problems), "problems": problems}), 400
    bull_bear_engine.reload_config(symbol.strip(), tf)
    pipeline_store.invalidate_all("invalidated", "Bull/Bear settings changed.")
    return jsonify({"ok": True, "config": cfg.to_dict(), "message": "Bull/Bear settings saved."})


@app.route("/api/bull-bear/events")
def api_bb_events():
    try:
        after, limit = int(request.args.get("after_id", "0")), int(request.args.get("limit", "200"))
    except ValueError:
        return jsonify({"ok": False, "message": "after_id and limit must be integers."}), 400
    return jsonify({"ok": True, "events": bb_events.tail(after, request.args.get("candle_id"), request.args.get("symbol"), request.args.get("timeframe"), limit),
                    "latest_id": bb_events.latest_id()})


@app.route("/api/strategy/mtf")
def api_strategy_mtf():
    """Multi-timeframe strategy evaluation (M5, M15, H1, H4) for one symbol. Read-only: it proposes a setup (BUY / SELL / WAIT / WATCH) with
    structural levels, and it never sizes a position, runs a risk check or sends an order."""
    symbol = request.args.get("symbol", "").strip()
    if not symbol or len(symbol) > 32 or not symbol.isprintable():
        return jsonify({"ok": False, "message": "A symbol is required."}), 400
    try:
        return jsonify(evaluate_strategy(adapter, symbol, settings, now_utc=time.time()))
    except Exception:                                                     # noqa: BLE001 - never leak internals; the log has them
        logger.exception("Multi-timeframe evaluation failed for %s", symbol)
        return jsonify({"ok": False, "message": "The multi-timeframe evaluation failed (see logs/trademate.log)."}), 500


@app.route("/api/strategy/mtf/trade-review")
def api_strategy_trade_review():
    """Run fresh MTF analysis, then pass only a qualified BUY/SELL to the existing Risk Engine.

    This endpoint is preview-only. It never sends an order or stores an approval. /api/order must
    independently re-read market/account data and re-run the risk gate at execution time.
    """
    symbol = request.args.get("symbol", "").strip()
    if not symbol or len(symbol) > 32 or not symbol.isprintable():
        return jsonify({"ok": False, "stage": "invalid_request", "approved": False,
                        "message": "A valid symbol is required.", "order_sent": False}), 400
    try:
        strategy = evaluate_selected(adapter, _selected_strategy_id(), symbol, settings, now_utc=time.time(), pip_overrides=_pip_overrides())
        review = review_strategy_proposal(adapter, strategy, risk_store.load(), _max_order_volume(), _target_risk())
        return jsonify(review)
    except Exception:  # noqa: BLE001 - fail closed; details stay in server logs
        logger.exception("Strategy-to-Risk review failed for %s", symbol)
        return jsonify({"ok": False, "stage": "unavailable", "approved": False,
                        "message": "The strategy proposal could not be reviewed safely. No order was sent; see logs/trademate.log.",
                        "order_sent": False}), 503


@app.route("/api/chart/settings", methods=["GET", "POST"])
def api_chart_settings():
    """Candle colours, colour preset, theme, chart theme and overlay visibility. Local file only; nothing goes to MT5."""
    if request.method == "GET":
        return jsonify({"ok": True, "settings": chart_store.load().to_dict(), "presets": preset_list(),
                        "stored": chart_store.path.exists()})
    try:
        new = chart_store.update(_payload())
    except (OrderRejectedError, ValueError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except OSError as exc:
        return jsonify({"ok": False, "message": f"Could not save the chart settings: {exc}"}), 500
    return jsonify({"ok": True, "settings": new.to_dict(), "presets": preset_list()})


@app.route("/api/symbols/search")
def api_symbols_search():
    """Search the broker's whole instrument catalogue (not just Market Watch)."""
    try:
        limit = int(request.args.get("limit", "40"))
        result = adapter.search_symbols(request.args.get("q", ""), limit)
    except (ValueError, BrokerError) as exc:
        return jsonify({"ok": False, "message": str(exc), "matches": []})
    return jsonify({"ok": True, **result.to_dict()})


@app.route("/api/symbols/add", methods=["POST"])
def api_symbols_add():
    """Select a broker symbol on demand, return its real info, and keep streaming its price."""
    try:
        data = _payload()
        name = data.get("symbol")
        if not isinstance(name, str):
            raise OrderRejectedError("symbol is required.")
        info = adapter.get_symbol_info(name)
        adapter.add_to_watchlist(info.name)
    except (OrderRejectedError, BrokerError) as exc:
        return jsonify({"ok": False, "message": str(exc)})
    return jsonify({"ok": True, "symbol": info.to_dict()})


@app.route("/api/watch", methods=["POST"])
def api_watch():
    """The browser's watchlist, so the bridge keeps exporting those symbols."""
    try:
        data = _payload()
        symbols = data.get("symbols")
        if not isinstance(symbols, list):
            raise OrderRejectedError("symbols must be a list.")
        adapter.set_watchlist(symbols)
    except (OrderRejectedError, BrokerError) as exc:
        return jsonify({"ok": False, "message": str(exc)})
    return jsonify({"ok": True})


@app.route("/api/symbols/spec")
def api_symbol_spec():
    """Contract/tick data for one symbol. The chart uses it to show TP/SL in account currency."""
    try:
        info = adapter.get_symbol_spec(request.args.get("symbol", ""))
    except (BrokerError, NotImplementedError) as exc:
        return jsonify({"ok": False, "message": str(exc) or "Not supported by this broker adapter."})
    keys = ("name", "digits", "point", "contract_size", "tick_size", "tick_value", "tick_value_profit",
            "tick_value_loss", "currency_profit", "volume_min", "volume_max", "volume_step")
    data = info.to_dict()
    account_currency = None
    try:
        account_currency = adapter.get_account().currency
    except BrokerError:
        pass
    return jsonify({"ok": True, "spec": {k: data.get(k) for k in keys}, "account_currency": account_currency})


def _risk_number(value):
    """A number from JSON (or a numeric string from a form field). Anything else is None, which the engine rejects."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@app.route("/api/risk/size", methods=["POST"])
def api_risk_size():
    """Phase 5.1: how big may this trade be? CALCULATION ONLY - sends nothing to MT5 except read-only data requests."""
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    symbol = str(data.get("symbol") or "").strip()
    trade = TradeRequest(symbol=symbol, direction=str(data.get("direction") or ""),
                         entry_price=_risk_number(data.get("entry")), stop_loss=_risk_number(data.get("stop_loss")),
                         risk_percent=_risk_number(data.get("risk_percent")))
    limits = RiskLimits(max_risk_percent=risk_store.load().max_risk_per_trade_percent,
                        max_volume=settings.trading.max_volume if settings.trading.enabled else None)
    equity = currency = spec = None
    problem = None
    try:
        account = adapter.get_account()
        equity, currency = account.equity, account.currency
        if not symbol:
            problem = "Choose a symbol."
        else:
            spec = to_spec(adapter.get_symbol_spec(symbol))
    except (BrokerError, NotImplementedError) as exc:
        problem = str(exc) or "Not supported by this broker adapter."
    result = calculate_position_size(equity, trade, spec, limits)
    out = result.to_dict()
    if problem and result.validation_status == "REJECTED" and spec is None:
        # Input mistakes are reported first by the engine; broker trouble only when the inputs themselves were fine.
        if result.rejection_reason and ("specification" in result.rejection_reason or "equity" in result.rejection_reason):
            out["rejection_reason"] = problem
    return jsonify({"ok": True, "result": out, "account_currency": currency})


def _max_order_volume():
    return settings.trading.max_volume if settings.trading.enabled else None


@app.route("/api/risk/sl", methods=["POST"])
def api_risk_sl():
    """Two-way stop-loss PREVIEW. mode "risk": risk amount -> SL price. mode "price": SL price -> risk amount.

    Calculation only: sends nothing to MT5 and authorises nothing. Money comes from the Risk Engine's own tick maths, in the
    account currency. /api/order re-reads price and symbol data and the Risk Engine re-judges the order before execution.
    """
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    mode = data.get("mode")
    symbol = str(data.get("symbol") or "").strip()
    if mode not in SL_MODES:
        return jsonify({"ok": False, "message": f"mode must be one of: {', '.join(SL_MODES)}."}), 400
    if not symbol or len(symbol) > 32 or not symbol.isprintable():
        return jsonify({"ok": False, "message": "A symbol is required."}), 400
    rid = data.get("request_id")
    try:
        info = adapter.get_symbol_spec(symbol)
        account = adapter.get_account()
    except (BrokerError, NotImplementedError) as exc:
        return jsonify({"ok": True, "request_id": rid, "result": {"mode": mode, "valid": False,
                        "message": (str(exc) or "MT5 data is not available.") + " No calculation was made."}, "limit": None})
    direction = str(data.get("direction") or "").strip().lower()
    entry = _risk_number(data.get("entry"))
    source = "provided"
    if entry is None and data.get("entry") in (None, ""):
        entry = info.ask if direction == "buy" else info.bid if direction == "sell" else None   # a market order fills here
        source = "live"
    volume = _risk_number(data.get("volume"))
    spec = to_spec(info)
    kw = dict(currency=account.currency or "", digits=info.digits, stops_level=info.stops_level, point=info.point)
    if mode == "risk":
        result = sl_from_risk(direction, entry, volume, _risk_number(data.get("risk_amount")), spec, **kw)
    else:
        result = risk_from_sl(direction, entry, volume, _risk_number(data.get("stop_loss")), spec, **kw)
    limit = judge_limit(result, account.equity, risk_store.load().max_risk_per_trade_percent)
    return jsonify({"ok": True, "request_id": rid, "entry_source": source, "symbol": info.name, "digits": info.digits,
                    "result": result.to_dict(), "limit": limit,
                    "preview_only": True, "note": "Preview only. Nothing was sent. The Risk Engine re-checks every order before execution."})


@app.route("/api/market/time")
def api_market_time():
    """The broker's clock for the candle countdown: UTC offset of the MT5 server (measured from live candles, never guessed).
    `server_offset_seconds` is null when it cannot be trusted; the page then shows no countdown instead of a wrong one."""
    now = time.time()
    try:
        clock = resolve_server_clock(adapter, now)
    except Exception:                                    # noqa: BLE001 - a clock lookup must never break the page
        logger.exception("Could not resolve the broker clock")
        clock = brokertime.ServerClock(None, "unknown", "The broker clock could not be read (see the log).")
    return jsonify({"ok": clock.known, "utc_epoch": now, "server_offset_seconds": clock.offset_seconds, "source": clock.source,
                    "detail": clock.detail, "verified_at": clock.verified_at})


@app.route("/api/risk/state")
def api_risk_state():
    """Phase 5.2: the user's rules plus how much of them is used right now. Read-only."""
    rules = risk_store.load()
    try:
        state = gather_state(adapter)
    except (BrokerError, NotImplementedError) as exc:
        return jsonify({"ok": False, "message": str(exc) or "MT5 data is not available.", "rules": rules.to_dict(),
                        "default_risk_percent": min(settings.risk.default_risk_percent, rules.max_risk_per_trade_percent)})
    summary = summarize_state(state, rules)
    summary["breaches"] = portfolio_breaches(summary, rules)
    try:
        daily_risk_log.record(summary, localday.resolve_tz(settings.ui.timezone))
    except Exception:                                                              # noqa: BLE001 - the history log must never break the risk display
        logger.exception("Could not record the daily risk history")
    return jsonify({"ok": True, "state": summary, "rules": rules.to_dict(), "consecutive_losses": loss_guard.status(), "emergency": safety_lock.status(),
                    "history_days": len(daily_risk_log.days),
                    "default_risk_percent": min(settings.risk.default_risk_percent, rules.max_risk_per_trade_percent)})


@app.route("/api/risk/daily-history")
def api_risk_daily_history():
    return jsonify({"ok": True, "days": daily_risk_log.history()})


@app.route("/api/risk/rules", methods=["POST"])
def api_risk_rules():
    """Save the user's risk rules (any value above 0 and at most 100 %). Local file only; nothing goes to MT5."""
    try:
        data = _payload()
        rules = parse_rules(data, risk_store.load())
        risk_store.save(rules)
    except (OrderRejectedError, ValueError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except OSError as exc:
        return jsonify({"ok": False, "message": f"Could not save the risk rules: {exc}"}), 500
    return jsonify({"ok": True, "rules": rules.to_dict()})


@app.route("/api/risk/evaluate", methods=["POST"])
def api_risk_evaluate():
    """Phase 5.2: ONE central decision (APPROVED / REJECTED) for a proposed trade. Decides only; places nothing.

    The proposal (symbol, direction, entry, stop_loss, take_profit, risk_percent) stands in for what the future
    Strategy Engine will produce.
    """
    try:
        data = _payload()
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    symbol = str(data.get("symbol") or "").strip()
    proposal = TradeProposal(symbol=symbol, direction=str(data.get("direction") or ""),
                             entry=_risk_number(data.get("entry")), stop_loss=_risk_number(data.get("stop_loss")),
                             take_profit=_risk_number(data.get("take_profit")),
                             strategy_risk_percent=_risk_number(data.get("risk_percent")),
                             volume=_risk_number(data.get("volume")))
    rules = risk_store.load()
    try:
        state = gather_state(adapter)
    except (BrokerError, NotImplementedError) as exc:
        return jsonify({"ok": False, "message": str(exc) or "MT5 data is not available."})
    spec, spec_problem = None, None
    if not symbol:
        spec_problem = "Choose a symbol."
    else:
        try:
            spec = to_spec(adapter.get_symbol_spec(symbol))
        except (BrokerError, NotImplementedError) as exc:
            spec_problem = str(exc) or "Not supported by this broker adapter."
    decision = evaluate_trade(state, proposal, spec, rules, _max_order_volume()).to_dict()
    if spec is None and decision["decision"] == "REJECTED":
        for c in decision["checks"]:
            if c["name"] == "Broker constraints" and not c["passed"]:
                c["detail"] = spec_problem
        if decision["rejection_reason"] and "specification" in decision["rejection_reason"]:
            decision["rejection_reason"] = spec_problem
    return jsonify({"ok": True, "decision": decision, "account_currency": state.currency})


@app.route("/api/history")
def api_history():
    """Closed trades from the broker's own deal history (read-only)."""
    try:
        days = int(request.args.get("days", "30"))
        limit = int(request.args.get("limit", "200"))
        trades = adapter.get_history(days, limit)
    except (ValueError, BrokerError, NotImplementedError) as exc:
        return jsonify({"ok": False, "message": str(exc) or "Not supported by this broker adapter.", "trades": []})
    seen_tickets, unique = set(), []
    for t in trades:                                                             # one row per closed position, whatever the bridge repeats (reconnect, overlapping windows)
        key = (t.ticket, t.time_close)
        if key not in seen_tickets:
            seen_tickets.add(key)
            unique.append(t)
    trades = unique
    # "Delete Trading History" never touches MT5. It only remembers, locally, which closed trades the user already
    # cleared from this view; the broker's history is unchanged and ?all=1 shows everything again.
    journal = getattr(adapter, "journal", None)
    hidden_before = int(journal.get_meta("history_hidden_before", "0") or 0) if journal else 0
    hidden = 0
    if hidden_before and request.args.get("all") != "1":
        kept = [t for t in trades if (t.time_close or 0) > hidden_before]
        hidden, trades = len(trades) - len(kept), kept
    currency = None
    try:
        currency = adapter.get_account().currency
    except BrokerError:
        pass
    rows = []
    for t in trades:
        row = t.to_dict()
        tag = rapid_engine.history_tag(t.ticket) if "rapid_engine" in globals() else None
        if tag:
            row["rapid_fire"] = tag                                              # additive, optional: every existing field is unchanged
        rows.append(row)
    return jsonify({"ok": True, "currency": currency, "trades": rows,
                    "hidden_before": hidden_before, "hidden_count": hidden})


@app.route("/api/executions")
def api_executions():
    """TradeMate's own record of every order attempt (local journal)."""
    journal = getattr(adapter, "journal", None)
    if journal is None:
        return jsonify({"ok": False, "message": "No execution journal is configured.", "executions": []})
    return jsonify({"ok": True, "executions": [_exec_view(r) for r in journal.recent(150)],
                    "unresolved": unresolved_executions()})


@app.route("/api/executions/<exec_id>/reconcile", methods=["POST"])
def api_reconcile(exec_id):
    """Compare an unresolved order with the real account. Read-only: it cannot place, change or close anything."""
    try:
        _payload()
        return jsonify({"ok": True, "execution": _exec_view(adapter.reconcile(exec_id))})
    except (OrderRejectedError, BrokerError, NotImplementedError) as exc:
        return jsonify({"ok": False, "message": str(exc)})


@app.route("/api/executions/<exec_id>/resolve", methods=["POST"])
def api_resolve(exec_id):
    """The user checked MT5 and says what happened to an unresolved order."""
    try:
        outcome = _payload().get("outcome")
        return jsonify({"ok": True, "execution": _exec_view(adapter.resolve(exec_id, outcome))})
    except (OrderRejectedError, BrokerError, NotImplementedError) as exc:
        return jsonify({"ok": False, "message": str(exc)})


@app.route("/api/local-history/clear", methods=["POST"])
def api_clear_local_history():
    """Delete TradeMate's LOCAL trading history (its order journal).

    Local only. This sends nothing to MT5, closes nothing, and does not touch the broker's account history.
    """
    try:
        data = _payload()
        if data.get("confirm") != "DELETE":
            raise OrderRejectedError('Confirmation missing: send {"confirm": "DELETE"}.')
        journal = getattr(adapter, "journal", None)
        if journal is None:
            raise OrderRejectedError("No local history exists to delete.")
        forgotten = len(journal.unresolved())
        deleted = journal.clear()
        hide = data.get("hide_mt5_view_before")
        if isinstance(hide, int) and not isinstance(hide, bool) and hide > 0:
            journal.set_meta("history_hidden_before", str(hide))
    except OrderRejectedError as exc:
        return jsonify({"ok": False, "message": str(exc)})
    except Exception as exc:  # journal I/O: report, log, never pretend
        logger.exception("Deleting the local history failed")
        return jsonify({"ok": False, "message": f"Could not delete the local history: {exc}"}), 500
    return jsonify({"ok": True, "deleted": deleted, "unresolved_forgotten": forgotten})


@app.route("/api/trading")
def api_trading():
    """What execution guard rails are active (for the UI)."""
    t = settings.trading
    return jsonify({"enabled": t.enabled and settings.mode.value != "live", "max_volume": t.max_volume,
                    "mode": settings.mode.value})


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'"
    )
    return response


@app.errorhandler(Exception)
def handle_error(exc):
    if isinstance(exc, HTTPException):  # 404 and friends keep Flask's normal pages
        return exc
    logger.exception("Unhandled error")
    if request.path.startswith("/api/"):
        if request.method == "POST" and request.path.startswith(("/api/order", "/api/position")):
            settled = adapter.settle_in_flight()       # a write may have been in flight: never report 'failed' for it
            if settled:
                return jsonify({"state": "unknown", "outcome": "unknown", "order_submitted": None,
                                "execution_id": settled[-1]["id"], "automatic_resubmission": "DISABLED",
                                "message": "TradeMate hit an internal error while an order was in flight. "
                                           "The outcome is UNKNOWN."}), 500
        return jsonify({"ok": False, "state": "error", "message": "Internal error. See logs/trademate.log."}), 500
    return "Something went wrong. See logs/trademate.log for details.", 500


if __name__ == "__main__":
    logger.info("TradeMate %s starting in %s mode", __version__, settings.mode.value)
    try:
        app.run(host=settings.ui.host, port=settings.ui.port, debug=False)
    except OSError as exc:
        sys.exit(f"Could not start TradeMate on {settings.ui.host}:{settings.ui.port}: {exc}")
