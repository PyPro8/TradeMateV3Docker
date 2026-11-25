"""Shared harness for the Phase 7B real-browser tests (headless Chromium via Playwright; skipped when it is missing).

The page is served by the real Flask app, with a stub broker. `QuoteStub` can only READ, plus the optional pending-order and
signal tests bolt on a FAKE bridge adapter; no real MT5 and no real order is ever involved.
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    PLAYWRIGHT = False

import app as app_module
from tests.test_browser_phase7a import StubBroker, _walk


def _shift(rows, delta):
    return [[t, o + delta, h + delta, l + delta, c + delta] for t, o, h, l, c in rows]
from trademate.broker.base import Candle, ConnectionState, ConnectionStatus, SymbolInfo, SymbolMatch, SymbolSearch
from trademate.chart_settings import ChartSettingsStore
from trademate.errors import BrokerUnavailableError

SKIP_TEXT = "SKIPPED: Playwright is not installed (pip install playwright && playwright install chromium)."


class QuoteStub(StubBroker):
    """Two streamed symbols whose bid/ask a test can change, plus switches for connection loss and an old snapshot."""

    def __init__(self):
        super().__init__()
        self.quotes = {"EURUSD": (round(self.series["H1"][-1][4], 2), round(self.series["H1"][-1][4], 2) + 0.02),
                       "XAUUSD": (2000.10, 2000.40)}
        self.streamed = ["EURUSD", "XAUUSD"]
        self.connected = True
        self.snapshot_age = 0.1
        self.series_by = {"EURUSD": self.series, "XAUUSD": {"M15": _shift(_walk(300, 8), 1900.0), "H1": _shift(_walk(300, 9), 1900.0)}}

    def get_connection_status(self):
        if not self.connected:
            return ConnectionStatus(ConnectionState.DISCONNECTED, "MT5 is not running.")
        return ConnectionStatus(ConnectionState.CONNECTED, "stub", self.snapshot_age)

    def _info(self, name):
        bid, ask = self.quotes[name]
        point = 0.01
        return SymbolInfo(name, name, 2, point, round((ask - bid) / point), 100.0, 0.01, 100.0, 0.01, "X", "USD", bid, ask, 10, 0, 4,
                          tick_size=0.01, tick_value=1.0)

    def get_symbols(self):
        return [self._info(n) for n in self.streamed]

    def get_candles(self, symbol, timeframe, count=300):
        rows = self.series_by.get(symbol, {}).get(timeframe.upper())
        if rows is None:
            raise BrokerUnavailableError("no candles")
        return [Candle(*c) for c in rows]

    def get_symbol_spec(self, symbol):
        if symbol not in self.quotes:
            raise BrokerUnavailableError("no spec")
        return self._info(symbol)

    def get_symbol_info(self, symbol):
        if symbol not in self.quotes:
            raise BrokerUnavailableError("unknown symbol")
        return self._info(symbol)

    def search_symbols(self, query, limit=40):
        query = query.casefold().strip()
        matches = [SymbolMatch(name, name, "Test", 4, True) for name in self.quotes if query in name.casefold()]
        return SymbolSearch(matches[:limit], len(matches), len(self.quotes))

    def add_to_watchlist(self, symbol):
        if symbol not in self.quotes:
            raise BrokerUnavailableError("unknown symbol")


class BrowserBase(unittest.TestCase):
    broker_factory = QuoteStub
    extra_patches = ()          # callables (cls) -> list of mock patchers

    @classmethod
    def setUpClass(cls):
        if not PLAYWRIGHT:
            raise unittest.SkipTest(SKIP_TEXT)
        from werkzeug.serving import make_server
        cls._tmp = tempfile.TemporaryDirectory()
        cls.broker = cls.broker_factory()
        from trademate.strategies.selection import StrategySelectionStore
        sel = StrategySelectionStore(Path(cls._tmp.name) / "strategy_selection.json", "choch")      # never touch the real data/ folder
        sel.on_change(app_module._on_strategy_change)
        cls.patches = [mock.patch.object(app_module, "adapter", cls.broker),
                       mock.patch.object(app_module, "chart_store", ChartSettingsStore(Path(cls._tmp.name) / "cs.json")),
                       mock.patch.object(app_module, "strategy_store", sel),
                       # the Command Centre's own state (selection, lot sizes, setup ledger, decision hub, Autopilot) is isolated too
                       mock.patch.object(app_module, "selection_store", __import__("trademate.selection_store", fromlist=["x"]).SelectionStore(Path(cls._tmp.name) / "sel.json")),
                       mock.patch.object(app_module, "pip_store", __import__("trademate.pips", fromlist=["x"]).PipStore(Path(cls._tmp.name) / "pips.json")),
                       mock.patch.object(app_module, "lot_store", __import__("trademate.lotsize", fromlist=["x"]).LotStore(Path(cls._tmp.name) / "lots.json")),
                       mock.patch.object(app_module, "setup_ledger", __import__("trademate.setups_ledger", fromlist=["x"]).SetupLedger(Path(cls._tmp.name) / "ledger.json")),
                       mock.patch.object(app_module, "decision_hub", __import__("trademate.decision_hub", fromlist=["x"]).DecisionHub()),
                       mock.patch.object(app_module.autopilot, "path", Path(cls._tmp.name) / "ap.json")]
        for make in cls.extra_patches:
            cls.patches.extend(make(cls))
        for p in cls.patches:
            p.start()
        cls.server = make_server("127.0.0.1", 0, app_module.app, threaded=True)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            cls.pw.stop()
            cls.server.shutdown()
            for p in cls.patches:
                p.stop()
            raise unittest.SkipTest(f"SKIPPED: Chromium could not start ({str(exc)[:80]}).")

    @classmethod
    def tearDownClass(cls):
        if not PLAYWRIGHT or not hasattr(cls, "browser"):
            return
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        for p in cls.patches:
            p.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.ctx = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.ctx.new_page()
        self.errors, self.requests = [], []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append("CSP: " + m.text) if "Content Security Policy" in m.text else None)
        self.page.on("request", lambda r: self.requests.append((r.method, r.url, r.post_data)))
        self.addCleanup(self.ctx.close)
        app_module.watchdog.stop()                         # the Watchdog is a process-wide singleton: no test may inherit another's monitoring
        app_module.decision_hub.clear()                    # ... nor another test's current decision
        self.addCleanup(app_module.watchdog.stop)

    def tearDown(self):
        self.assertEqual(self.errors, [], "JavaScript / CSP errors in the page")

    def until(self, expression, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.page.evaluate(f"() => !!({expression})"):
                return
            self.page.wait_for_timeout(100)
        self.fail(f"Timed out waiting for: {expression}")

    def open_chart(self):
        with self.page.expect_response(lambda r: "/api/candles" in r.url and r.status == 200, timeout=15000):
            self.page.goto(self.base + "/#chart")
        self.until("window.TradeMateApp && window.TradeMateApp.chartQuotes().length")
