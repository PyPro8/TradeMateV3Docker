"""Stop-loss calculator: instrument-specific cases and the API route (/api/risk/sl). Group: BUY/SELL and instrument cases.

SIMULATED (stub feed): the route can only read symbol data and the account, it has no way to send an order.

    python3 -m unittest tests.test_sl_instruments -v
"""

import unittest
from unittest import mock

import app as app_module
from app import app
from tests.sl_fixtures import INSTRUMENTS
from trademate.broker.base import AccountInfo, SymbolInfo
from trademate.errors import BrokerUnavailableError
from trademate.risk import RiskRules, RiskRulesStore
from trademate.risk.sl_calculator import risk_from_sl, sl_from_risk


class EveryInstrumentBothDirections(unittest.TestCase):
    def test_risk_first_then_price_first_agree_for_every_instrument_and_side(self):
        for name, (sp, digits, entry) in INSTRUMENTS.items():
            for side in ("buy", "sell"):
                a = sl_from_risk(side, entry, 0.10, 100.0, sp, digits=digits, currency="USD")
                self.assertTrue(a.valid, (name, side, a.message))
                b = risk_from_sl(side, entry, 0.10, a.stop_loss, sp, digits=digits, currency="USD")
                self.assertTrue(b.valid, (name, side, b.message))
                self.assertAlmostEqual(a.risk_amount, b.risk_amount, places=9)
                self.assertLessEqual(b.risk_amount, 100.0 + 1e-9)

    def test_money_is_not_a_pip_value_shortcut(self):
        """EURUSD and USDJPY have different tick sizes and tick values: the same price distance is NOT the same money."""
        eur, ed, ee = INSTRUMENTS["EURUSD"]
        jpy, jd, je = INSTRUMENTS["USDJPY"]
        a = risk_from_sl("buy", ee, 1.0, round(ee - 0.0100, 5), eur, digits=ed).risk_amount      # 0.01 price units
        b = risk_from_sl("buy", je, 1.0, round(je - 0.0100, 3), jpy, digits=jd).risk_amount
        self.assertAlmostEqual(a, 1000.0, places=4)
        self.assertAlmostEqual(b, 6.66667, places=3)
        self.assertGreater(a / b, 100)


def info(name="EURUSD", **over):
    sp, digits, entry = INSTRUMENTS[name]
    base = dict(name=name, description=name, digits=digits, point=sp.tick_size, spread_points=12, contract_size=100000.0,
                volume_min=sp.volume_min, volume_max=sp.volume_max, volume_step=sp.volume_step, currency_base="EUR",
                currency_profit="USD", bid=entry, ask=round(entry + 12 * sp.tick_size, digits), stops_level=0, freeze_level=0,
                trade_mode=4, tick_size=sp.tick_size, tick_value=sp.tick_value, tick_value_loss=sp.tick_value_loss)
    base.update(over)
    return SymbolInfo(**base)


class Feed:
    """Symbol spec + account only: no order method exists, so the SL route cannot trade."""

    def __init__(self, symbols=None, equity=10_000.0, currency="USD", fail=None):
        self.symbols = symbols if symbols is not None else {"EURUSD": info("EURUSD"), "XAUUSD": info("XAUUSD")}
        self.equity, self.currency, self.fail = equity, currency, fail
        self.spec_calls = 0

    def get_symbol_spec(self, symbol):
        self.spec_calls += 1
        if self.fail:
            raise self.fail
        if symbol not in self.symbols:
            raise BrokerUnavailableError(f"Unknown symbol {symbol}.")
        return self.symbols[symbol]

    def get_account(self):
        if self.fail:
            raise self.fail
        return AccountInfo(1, "Headway-Demo", "Test", "demo", self.currency, 100, self.equity, self.equity, 0.0, self.equity, None, 0.0)


class SlRoute(unittest.TestCase):
    def use(self, feed, max_pct=1.0):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = RiskRulesStore(Path(tmp.name) / "r.json", RiskRules(max_risk_per_trade_percent=max_pct))
        for target, value in (("adapter", feed), ("risk_store", store)):
            p = mock.patch.object(app_module, target, value)
            p.start()
            self.addCleanup(p.stop)
        return app.test_client()

    def post(self, c, **body):
        return c.post("/api/risk/sl", json=body).get_json()

    def test_risk_mode_uses_the_live_entry_for_each_direction(self):
        c = self.use(Feed())
        buy = self.post(c, mode="risk", symbol="EURUSD", direction="buy", volume=0.10, risk_amount=50)
        sell = self.post(c, mode="risk", symbol="EURUSD", direction="sell", volume=0.10, risk_amount=50)
        self.assertTrue(buy["ok"] and buy["result"]["valid"], buy)
        self.assertEqual(buy["entry_source"], "live")
        self.assertAlmostEqual(buy["result"]["entry"], 1.10024, places=5)    # BUY enters at the ask (bid 1.10012 + 12 points)
        self.assertAlmostEqual(buy["result"]["stop_loss"], 1.09524, places=5)
        self.assertAlmostEqual(sell["result"]["entry"], 1.10012, places=5)   # SELL enters at the bid
        self.assertAlmostEqual(sell["result"]["stop_loss"], 1.10512, places=5)
        self.assertEqual(buy["result"]["currency"], "USD")
        self.assertTrue(buy["preview_only"])

    def test_price_mode_returns_the_money_and_the_limit_judgement(self):
        c = self.use(Feed(), max_pct=1.0)                                     # limit 100 on 10 000
        ok = self.post(c, mode="price", symbol="EURUSD", direction="buy", volume=0.10, entry=1.10012, stop_loss=1.09512)
        self.assertAlmostEqual(ok["result"]["risk_amount"], 50.0, places=6)
        self.assertEqual(ok["entry_source"], "provided")
        self.assertTrue(ok["limit"]["ok"])
        over = self.post(c, mode="price", symbol="EURUSD", direction="buy", volume=0.10, entry=1.10012, stop_loss=1.08012)
        self.assertAlmostEqual(over["result"]["risk_amount"], 200.0, places=6)
        self.assertFalse(over["limit"]["ok"])
        self.assertIn("Risk Engine would reject", over["limit"]["message"])

    def test_currency_comes_from_the_account_not_an_assumption(self):
        c = self.use(Feed(currency="NGN"))
        r = self.post(c, mode="risk", symbol="EURUSD", direction="buy", volume=0.10, risk_amount=50)
        self.assertEqual(r["result"]["currency"], "NGN")
        self.assertEqual(r["limit"]["message"].count("NGN") >= 1, True)

    def test_request_id_is_echoed_so_the_browser_can_drop_stale_answers(self):
        c = self.use(Feed())
        for rid in ("a1", "a2"):
            self.assertEqual(self.post(c, mode="risk", symbol="EURUSD", direction="buy", volume=0.1, risk_amount=50, request_id=rid)["request_id"], rid)

    def test_mt5_unavailable_gives_a_reason_and_no_numbers(self):
        c = self.use(Feed(fail=BrokerUnavailableError("MT5 is not connected.")))
        r = self.post(c, mode="risk", symbol="EURUSD", direction="buy", volume=0.1, risk_amount=50)
        self.assertFalse(r["result"]["valid"])
        self.assertIn("MT5 is not connected", r["result"]["message"])
        self.assertIsNone(r["limit"])

    def test_unknown_symbol_and_missing_fields_are_reported_cleanly(self):
        c = self.use(Feed())
        self.assertFalse(self.post(c, mode="risk", symbol="NOPE", direction="buy", volume=0.1, risk_amount=50)["result"]["valid"])
        self.assertFalse(self.post(c, mode="risk", symbol="EURUSD", direction="buy", volume=0.1)["result"]["valid"])
        self.assertFalse(self.post(c, mode="price", symbol="EURUSD", direction="buy", volume=0.1)["result"]["valid"])
        self.assertFalse(self.post(c, mode="risk", symbol="EURUSD", direction="", volume=0.1, risk_amount=50)["result"]["valid"])

    def test_bad_requests_are_refused_by_the_server(self):
        c = self.use(Feed())
        for body in ({"mode": "x", "symbol": "EURUSD"}, {"mode": "risk"}, {"mode": "risk", "symbol": ""}, {"mode": "risk", "symbol": "E" * 40}):
            r = c.post("/api/risk/sl", json=body)
            self.assertEqual(r.status_code, 400, body)
        self.assertEqual(c.post("/api/risk/sl", data="not json", content_type="application/json").status_code, 400)

    def test_the_route_cannot_trade(self):
        feed = Feed()
        c = self.use(feed)
        self.assertFalse(hasattr(feed, "place_order"))
        self.assertTrue(self.post(c, mode="risk", symbol="XAUUSD", direction="buy", volume=0.01, risk_amount=5)["result"]["valid"])
        self.assertEqual(c.get("/api/risk/sl").status_code, 405)

    def test_nan_infinity_in_the_json_body_is_not_a_number(self):
        c = self.use(Feed())
        for raw in ('{"mode":"risk","symbol":"EURUSD","direction":"buy","volume":NaN,"risk_amount":50}',
                    '{"mode":"risk","symbol":"EURUSD","direction":"buy","volume":0.1,"risk_amount":Infinity}'):
            r = c.post("/api/risk/sl", data=raw, content_type="application/json")
            body = r.get_json()
            self.assertFalse(body["result"]["valid"] if r.status_code == 200 else False)


if __name__ == "__main__":
    unittest.main()
