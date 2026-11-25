"""Phase 7B pending orders in the real UI (headless Chromium), driven against the FAKE bridge. No real MT5, no real order.

    python3 -m unittest tests.test_browser_phase7b_pending -v        (SKIPPED, not passed, when Playwright/Chromium is missing)
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from tests.browser_support import BrowserBase, PLAYWRIGHT
from tests.fake_bridge import FakeBridge
from tests.test_browser_phase7a import AUDIT_JS
from trademate.broker import create_adapter
from trademate.config import AppSettings, Mt5Settings, Settings, TradingSettings
from trademate.risk import RiskRules, RiskRulesStore

if PLAYWRIGHT:
    _STACK = {}

    def _stack():
        tmp = tempfile.TemporaryDirectory()
        bridge = FakeBridge(Path(tmp.name)).start()
        adapter = create_adapter(Settings(app=AppSettings(data_dir=Path(tmp.name)), mt5=Mt5Settings(data_dir=Path(tmp.name)),
                                          trading=TradingSettings(command_timeout_seconds=3.0, confirm_timeout_seconds=2.0)))
        _STACK.update(tmp=tmp, bridge=bridge)
        return adapter


class PendingUiTest(BrowserBase):
    broker_factory = staticmethod(lambda: _stack())
    extra_patches = (lambda cls: [mock.patch.object(app_module, "risk_store", RiskRulesStore(
        Path(_STACK["tmp"].name) / "risk.json", RiskRules(max_risk_per_trade_percent=1.0, max_daily_loss_percent=3.0,
                                                         max_total_active_risk_percent=5.0, max_positions=10)))],)

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
        self.bridge.reject_with = None
        self.bridge.write_snapshot()

    def cmds(self, name):
        return [c for c in self.bridge.commands_seen if c.get("cmd") == name]

    def role(self, name):
        return f"#view-chart [data-role={name}]"

    def open_panel(self):
        with self.page.expect_response(lambda r: "/api/candles" in r.url and r.status == 200, timeout=20000):
            self.page.goto(self.base + "/#chart")
        self.until("document.querySelector('#view-chart [data-role=order-type]')")
        self.until("window.TradeMateApp.selection().symbol")
        self.page.evaluate("() => window.TradeMateApp.openChart('EURUSD', 'H1', {})")
        self.until("window.TradeMateApp.selection().symbol === 'EURUSD'")
        self.until("!document.querySelector('#view-chart [data-role=order-type] option[value=buy_limit]').disabled", timeout=15)

    def fill(self, order_type, price, sl, tp="", lot="0.01"):
        self.page.select_option(self.role("order-type"), order_type)
        self.page.fill(self.role("lot"), lot)
        self.page.fill(self.role("pend-price"), str(price))
        self.page.fill(self.role("sl"), str(sl))
        self.page.fill(self.role("tp"), str(tp))

    def place(self):
        self.page.click(self.role("pend-place"))
        self.until("document.querySelector('#order-review')")

    def open_view(self, name):
        """Go to a page through the sidebar link (as a user does) and wait until that page is actually shown.

        The pending-orders table exists only on the Dashboard and Trades pages (one copy each, both hidden while the
        Chart page is open), so a test must be ON the page it drives and must scope its selectors to that page.
        """
        self.page.click(f"a.nav-link[data-view={name}]")
        self.until(f"!document.getElementById('view-{name}').hidden")

    # ------------------------------------------------------------------------------------------
    def test_types_are_offered_and_the_fields_appear_only_for_a_pending_type(self):
        self.open_panel()
        opts = self.page.evaluate("() => Array.from(document.querySelectorAll('#view-chart [data-role=order-type] option')).map(o => [o.value, o.disabled])")
        self.assertEqual([v for v, d in opts], ["market", "buy_limit", "sell_limit", "buy_stop", "sell_stop"])   # stop-limit types are not offered
        self.assertFalse(any(d for v, d in opts))
        self.assertTrue(self.page.evaluate("() => document.querySelector('#view-chart [data-role=pend-fields]').hidden"))
        self.page.select_option(self.role("order-type"), "sell_stop")
        self.assertFalse(self.page.evaluate("() => document.querySelector('#view-chart [data-role=pend-fields]').hidden"))
        self.page.select_option(self.role("order-type"), "market")
        self.assertTrue(self.page.evaluate("() => document.querySelector('#view-chart [data-role=pend-fields]').hidden"))

    def test_placing_a_buy_limit_reviews_confirms_and_shows_a_pending_row_not_a_position(self):
        self.open_panel()
        self.fill("buy_limit", 1.0990, 1.0950, 1.1050)
        self.place()
        text = self.page.evaluate("() => document.getElementById('order-review').innerText")
        for needed in ("approved by Risk Engine", "BUY LIMIT", "Entry price", "1.099", "waits for this price", "Est. risk at SL"):
            self.assertIn(needed, text)
        self.assertEqual(self.cmds("open_pending"), [])                       # nothing sent before the user confirms
        self.page.click("#order-review button:has-text('Place Buy Limit')")
        self.until("document.querySelector('#view-chart [data-role=exec]').innerText.indexOf('EXECUTED') >= 0")
        self.assertEqual(len(self.cmds("open_pending")), 1)
        self.until("document.querySelector('[data-role=pending-body] tr[data-order]')")
        row = self.page.evaluate("() => document.querySelector('[data-role=pending-body] tr[data-order]').innerText")
        self.assertIn("Buy Limit", row)
        self.assertEqual(self.page.evaluate("() => document.querySelectorAll('[data-role=trades-body] tr[data-ticket]').length"), 0)   # not a position
        self.assertEqual(len(self.bridge.orders), 1)
        self.assertEqual(self.bridge.positions, {})

    def test_a_price_on_the_wrong_side_is_explained_and_nothing_is_sent(self):
        self.open_panel()
        self.fill("buy_limit", 1.1050, 1.1000)                                  # above the Ask
        self.place()
        text = self.page.evaluate("() => document.getElementById('order-review').innerText")
        self.assertIn("not approved", text.lower())
        self.assertIn("BELOW the current Ask", text)
        self.assertEqual(self.page.evaluate("() => document.querySelectorAll('#order-review button').length"), 1)   # only Cancel: no way to confirm
        self.page.click("#order-review button:has-text('Cancel')")
        self.assertEqual(self.cmds("open_pending"), [])
        self.assertEqual(self.page.input_value(self.role("pend-price")), "1.105")             # the user's price was not changed

    def test_a_risk_rejection_never_reaches_the_bridge(self):
        self.open_panel()
        self.fill("buy_limit", 1.0990, 1.0000, lot="0.01")                       # about $99 risk on a $1000 account
        self.place()
        text = self.page.evaluate("() => document.getElementById('order-review').innerText")
        self.assertIn("not approved", text.lower())
        self.assertEqual(self.page.evaluate("() => Array.from(document.querySelectorAll('#order-review button')).filter(b => /^Place|^Confirm/.test(b.textContent)).length"), 0)
        self.page.click("#order-review button:has-text('Cancel')")
        self.assertEqual(self.cmds("open_pending"), [])

    def test_stop_loss_calculator_follows_the_pending_entry_and_side(self):
        self.open_panel()
        self.page.select_option(self.role("order-type"), "sell_limit")
        self.page.fill(self.role("lot"), "0.10")
        self.page.fill(self.role("pend-price"), "1.1050")
        self.page.click("#view-chart [data-sl-mode=risk]")
        self.page.fill(self.role("sl-risk"), "50")
        self.until("document.querySelector('#view-chart [data-role=sl-out]').innerText.indexOf('Final risk') >= 0")
        self.assertAlmostEqual(float(self.page.input_value(self.role("sl"))), 1.1100, places=4)   # SELL: 500 ticks ABOVE the pending entry, not the market
        self.assertIn("as entered", self.page.evaluate("() => document.querySelector('#view-chart [data-role=sl-out]').innerText"))
        self.assertTrue(self.page.evaluate("() => document.querySelector('#view-chart [data-sl-dir=buy]').disabled"))   # direction follows the order type

    def test_editing_a_pending_order_shows_the_brokers_values_and_cancel_needs_two_clicks(self):
        self.open_panel()
        self.fill("buy_limit", 1.0990, 1.0950, 1.1050)
        self.place()
        self.page.click("#order-review button:has-text('Place Buy Limit')")
        self.until("document.querySelector('#view-trades [data-role=pending-body] tr[data-order]')")
        self.open_view("trades")                                                 # the row is only usable on the page that shows it
        row = "#view-trades [data-role=pending-body] tr[data-order]"
        entry, sl, tp = (f"{row} td:nth-child({n}) input" for n in (4, 5, 6))   # Entry, SL, TP columns
        self.assertEqual(self.page.locator(row).count(), 1)                      # exactly one row: the selectors below are unambiguous
        self.assertEqual([self.page.input_value(i) for i in (entry, sl, tp)], ["1.09900", "1.09500", "1.10500"])   # the broker's values
        self.page.fill(entry, "1.0985")
        self.page.click(f"{row} button:has-text('APPLY')")
        self.until("Array.from(document.querySelectorAll('#view-trades [data-role=pending-body] tr[data-order] input')).every(i => !i.classList.contains('is-dirty'))")
        self.page.wait_for_timeout(1500)
        self.assertEqual(list(self.bridge.orders.values())[0]["price_open"], 1.0985)
        self.assertEqual(len(self.cmds("modify_order")), 1)
        self.assertEqual(self.page.input_value(entry), "1.09850")                # the field shows what the broker now holds
        self.page.click(f"{row} button:has-text('CANCEL ORDER')")
        self.assertEqual(self.cmds("cancel_order"), [])                         # the first click only arms it
        self.assertIn("CONFIRM?", self.page.evaluate(f"() => document.querySelector('{row} .btn-danger').textContent"))
        self.page.click(f"{row} .btn-danger")
        self.until("document.querySelector('#view-trades [data-role=pending-body] .empty-row')")
        self.assertEqual(self.bridge.orders, {})
        self.assertEqual(len(self.cmds("cancel_order")), 1)

    def test_the_pending_row_is_on_trades_and_dashboard_only_and_is_operable_there(self):
        """Regression for the Phase 7B browser-test timeout: a pending row is hidden (not fillable) while the Chart page is open.

        Pins the page structure the edit test relies on: one row per page, on Dashboard and Trades, none on Chart, and on
        either page the row's inputs and buttons are visible and enabled.
        """
        self.open_panel()
        self.fill("buy_limit", 1.0990, 1.0950)
        self.place()
        self.page.click("#order-review button:has-text('Place Buy Limit')")
        self.until("document.querySelector('[data-role=pending-body] tr[data-order]')")
        self.assertEqual(self.page.locator("#view-chart [data-role=pending-body]").count(), 0)     # no pending table in the Chart page
        self.assertEqual(self.page.locator("[data-role=pending-body] tr[data-order]").count(), 2)  # one per page that has the table
        self.assertFalse(self.page.locator("#view-trades [data-role=pending-body] tr[data-order] input").first.is_visible())   # hidden while on Chart
        for view in ("trades", "dashboard"):
            self.open_view(view)
            row = f"#view-{view} [data-role=pending-body] tr[data-order]"
            self.assertEqual(self.page.locator(row).count(), 1, view)
            for inp in self.page.locator(f"{row} input").all():
                self.assertTrue(inp.is_visible() and inp.is_enabled() and inp.is_editable(), view)
            for name in ("APPLY", "CANCEL ORDER"):
                btn = self.page.locator(f"{row} button:has-text('{name}')")
                self.assertTrue(btn.is_visible() and btn.is_enabled(), f"{view} {name}")
        self.assertEqual(self.cmds("modify_order"), [])                         # looking at the row sends nothing
        self.assertEqual(self.cmds("cancel_order"), [])

    def test_a_filled_order_leaves_the_pending_table_and_appears_as_a_position(self):
        self.open_panel()
        self.fill("buy_limit", 1.0990, 1.0950)
        self.place()
        self.page.click("#order-review button:has-text('Place Buy Limit')")
        self.until("document.querySelector('[data-role=pending-body] tr[data-order]')")
        self.bridge.fill_order(next(iter(self.bridge.orders)))
        self.until("document.querySelector('[data-role=pending-body] .empty-row')", timeout=15)
        self.until("document.querySelector('[data-role=trades-body] tr[data-ticket]')", timeout=15)

    def test_market_orders_still_work_next_to_pending_orders(self):
        self.open_panel()
        self.page.select_option(self.role("order-type"), "market")
        self.page.fill(self.role("lot"), "0.01")
        self.page.fill(self.role("sl"), "1.0950")
        self.page.click(self.role("panel-buy"))
        self.until("document.querySelector('#order-review')")
        self.page.click("#order-review button:has-text('Confirm BUY')")
        self.until("document.querySelector('[data-role=trades-body] tr[data-ticket]')", timeout=20)
        self.assertEqual(len(self.cmds("open")), 1)
        self.assertEqual(self.cmds("open_pending"), [])

    def test_an_old_ea_disables_pending_types_and_says_why(self):
        with mock.patch.object(FakeBridge, "PENDING_SUPPORTED", False):
            self.bridge.write_snapshot()
            with self.page.expect_response(lambda r: "/api/candles" in r.url and r.status == 200, timeout=20000):
                self.page.goto(self.base + "/#chart")
            self.until("document.querySelector('#view-chart [data-role=order-type]')")
            self.until("document.querySelector('#view-chart [data-role=order-type] option[value=buy_limit]').disabled", timeout=15)
            self.assertIn("updated TradeMateBridge EA", self.page.evaluate("() => document.querySelector('#view-chart [data-role=pend-note]').innerText"))
            self.assertIn("EA", self.page.evaluate("() => document.querySelector('[data-role=pending-note]').innerText"))
            self.assertTrue(self.page.evaluate("() => document.querySelector('#view-chart [data-role=pend-place]').disabled"))
        self.bridge.write_snapshot()
        self.assertEqual(self.cmds("open_pending"), [])

    def test_contrast_of_the_new_controls_in_both_themes(self):
        self.open_panel()
        self.page.select_option(self.role("order-type"), "buy_stop")
        for theme in ("dark", "light"):
            if self.page.evaluate("() => document.documentElement.getAttribute('data-theme')") != theme:
                self.page.click("#theme-toggle")                                # the app's own switch (it also restyles the chart overlay text)
                self.until(f"document.documentElement.getAttribute('data-theme') === '{theme}'")
            self.page.wait_for_timeout(300)
            bad = [x for x in self.page.evaluate(AUDIT_JS) if not x["disabled"] and "btn-primary" not in x["cls"]]
            self.assertEqual(bad, [], theme)
        if self.page.evaluate("() => document.documentElement.getAttribute('data-theme')") != "dark":
            self.page.click("#theme-toggle")
            self.page.wait_for_timeout(500)                                      # let the saved theme settle for the next test


if __name__ == "__main__":
    unittest.main()
