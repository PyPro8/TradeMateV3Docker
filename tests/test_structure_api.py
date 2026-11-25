"""Phase 7A: market-structure output on /api/candles and its separation from trading.

    python3 -m unittest tests.test_structure_api -v
"""

import unittest
from unittest import mock

import app as app_module
from app import app
from trademate.broker.base import Candle
from trademate.errors import BrokerUnavailableError
from tests.test_structure_engine import DOWN, UP, UP_THEN_BREAK_LOW, series


class CandleOnlyAdapter:
    """Supplies candles and NOTHING else. Any attempt to trade or touch the account raises AttributeError."""

    def __init__(self, data):
        self.data = data            # {(symbol, timeframe): [[t,o,h,l,c], ...]}
        self.calls = []

    def get_candles(self, symbol, timeframe, count=300):
        self.calls.append((symbol, timeframe.upper(), count))
        try:
            return [Candle(*c) for c in self.data[(symbol, timeframe.upper())]]
        except KeyError:
            raise BrokerUnavailableError(f"No candles for {symbol} {timeframe}.") from None


class StructureApiTest(unittest.TestCase):
    def setUp(self):
        self.adapter = CandleOnlyAdapter({
            ("EURUSD", "H1"): series(UP_THEN_BREAK_LOW),
            ("EURUSD", "M15"): series(UP),
            ("XAUUSD", "H1"): series(DOWN),
        })
        patcher = mock.patch.object(app_module, "adapter", self.adapter)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.test_client()

    def candles(self, symbol="EURUSD", tf="H1", extra=""):
        return self.client.get(f"/api/candles?symbol={symbol}&timeframe={tf}&structure=1&swing_left=2&swing_right=2{extra}")

    def test_structure_is_returned_with_the_candles(self):
        data = self.candles().get_json()
        self.assertTrue(data["ok"])
        st = data["structure"]
        self.assertEqual([e["type"] + ":" + e["direction"] for e in st["events"]], ["BOS:bullish", "BOS:bullish", "CHOCH:bearish"])
        self.assertEqual([s["classification"] for s in st["swings"] if s["kind"] == "high"], [None, "HH", "HH"])
        self.assertEqual(st["trend"], "bearish")
        self.assertEqual(st["config"]["break_mode"], "close")

    def test_every_returned_time_and_price_comes_from_the_candles(self):
        data = self.candles().get_json()
        by_time = {c[0]: c for c in data["candles"]}
        for s in data["structure"]["swings"]:
            self.assertIn(s["pivot_time"], by_time)
            self.assertEqual(s["price"], by_time[s["pivot_time"]][2 if s["kind"] == "high" else 3])
        for e in data["structure"]["events"]:
            self.assertIn(e["candle_time"], by_time)
            self.assertIn(e["reference_pivot_time"], by_time)

    def test_the_last_broker_candle_is_treated_as_still_forming(self):
        events = self.candles().get_json()["structure"]["events"]
        self.assertFalse(events[-1]["confirmed"])           # the break happened on the newest (forming) candle
        self.assertIsNone(events[-1]["confirmed_time"])
        self.assertTrue(events[0]["confirmed"])

    def test_plain_candle_requests_are_unchanged(self):
        data = self.client.get("/api/candles?symbol=EURUSD&timeframe=H1").get_json()
        self.assertNotIn("structure", data)
        self.assertEqual(data["candles"][0], series(UP_THEN_BREAK_LOW)[0])

    def test_other_symbol_and_timeframe_get_their_own_structure_never_stale_markers(self):
        eur_h1 = self.candles("EURUSD", "H1").get_json()
        eur_m15 = self.candles("EURUSD", "M15").get_json()
        xau = self.candles("XAUUSD", "H1").get_json()
        self.assertEqual(eur_h1["symbol"], "EURUSD")
        self.assertEqual(xau["symbol"], "XAUUSD")
        for payload in (eur_h1, eur_m15, xau):
            times = {c[0] for c in payload["candles"]}
            for s in payload["structure"]["swings"]:
                self.assertIn(s["pivot_time"], times)
        self.assertNotEqual(eur_h1["structure"], eur_m15["structure"])
        self.assertEqual({e["direction"] for e in xau["structure"]["events"]}, {"bearish"})

    def test_refreshing_does_not_duplicate_anything(self):
        a = self.candles().get_json()
        b = self.candles().get_json()
        self.assertEqual(a, b)
        ids = [e["id"] for e in b["structure"]["events"]]
        self.assertEqual(len(ids), len(set(ids)))
        sw = [s["id"] for s in b["structure"]["swings"]]
        self.assertEqual(len(sw), len(set(sw)))

    def test_new_candle_keeps_earlier_structure(self):
        short = series(UP)
        self.adapter.data[("EURUSD", "H1")] = short
        before = self.candles().get_json()["structure"]
        self.adapter.data[("EURUSD", "H1")] = series(UP + [14.5, 14.2])
        after = self.candles().get_json()["structure"]
        known = {s["id"] for s in before["swings"] if s["confirmed"]}
        self.assertTrue(known <= {s["id"] for s in after["swings"]})

    def test_overrides_are_validated(self):
        for extra in ("&swing_left=0", "&swing_right=abc", "&swing_left=999", "&break_mode=touch", "&swing_right=2.5"):
            r = self.client.get("/api/candles?symbol=EURUSD&timeframe=H1&structure=1" + extra)
            self.assertEqual(r.status_code, 400, extra)
            self.assertFalse(r.get_json()["ok"])
        ok = self.client.get("/api/candles?symbol=EURUSD&timeframe=H1&structure=1&swing_left=2&swing_right=2&break_mode=wick").get_json()
        self.assertEqual(ok["structure"]["config"]["break_mode"], "wick")

    def test_insufficient_data_is_reported_not_faked(self):
        self.adapter.data[("EURUSD", "H1")] = series(UP[:4])
        st = self.candles().get_json()["structure"]
        self.assertTrue(st["diagnostics"]["insufficient_data"])
        self.assertEqual((st["swings"], st["events"]), ([], []))

    def test_broker_without_candles_gives_a_clear_message(self):
        data = self.candles("NOPE", "H1").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["candles"], [])
        self.assertNotIn("structure", data)

    def test_structure_endpoint_never_trades(self):
        order_adapter = mock.MagicMock()
        order_adapter.get_candles.side_effect = self.adapter.get_candles
        with mock.patch.object(app_module, "adapter", order_adapter):
            self.candles()
            self.candles("XAUUSD")
        called = {c[0] for c in order_adapter.method_calls}
        self.assertEqual(called, {"get_candles"})              # no place_order, modify, close, account, positions...

    def test_structure_levels_are_not_trade_fields(self):
        text = repr(self.candles().get_json()["structure"]).lower()
        for word in ("stop_loss", "take_profit", "volume", "ticket", "lots"):
            self.assertNotIn(word, text)

    def test_no_new_write_route_exists_for_structure(self):
        rules = {r.rule: r.methods for r in app.url_map.iter_rules()}
        self.assertFalse([r for r in rules if "structure" in r])
        self.assertEqual(rules["/api/candles"] - {"HEAD", "OPTIONS"}, {"GET"})

    def test_result_is_deterministic_across_requests_and_cache(self):
        first = self.candles().get_json()
        for _ in range(5):
            self.assertEqual(self.candles().get_json(), first)


class StructureOverlayScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        with open(os.path.join(app.static_folder, "js", "app.js"), encoding="utf-8") as f:
            cls.js = f.read()

    def test_overlay_is_drawn_only_when_visible_and_matching_the_loaded_candles(self):
        body = self.js[self.js.index("function drawStructure"):self.js.index("this.draw = function")]
        self.assertIn("!state.structureVisible", body)
        self.assertIn("st.key !== state.candleKey", body)           # data of another symbol / timeframe is never drawn
        self.assertNotIn("slColor", body)
        self.assertNotIn("pending", body)                           # never touches the draggable trade lines

    def test_structure_is_replaced_not_appended_on_every_refresh(self):
        self.assertIn("state.structure = res.structure ? { key: key,", self.js)
        self.assertNotIn("state.structure.push", self.js)
        self.assertNotIn("items.events.push", self.js)

    def test_overlay_does_not_register_hit_targets_or_orders(self):
        body = self.js[self.js.index("function drawStructure"):self.js.index("this.draw = function")]
        for word in ("hitTargets", "targets.push", "fetch(", "/api/order", "sendOrder"):
            self.assertNotIn(word, body)

    def test_toggle_hides_without_stopping_analysis(self):
        # candles are always requested with structure=1, so the data is ready the moment the user turns it on
        self.assertIn("&structure=1'", self.js)


if __name__ == "__main__":
    unittest.main()
