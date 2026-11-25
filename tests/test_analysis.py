"""Phase 7A.1: the Analysis view model and /api/analysis.

Everything is built from the candles passed in. These tests prove that (shifting every price shifts every output), that data
problems are never dressed up as READY, and that facts, rule-based reading, conditions to watch and setups stay separate.

    python3 -m unittest tests.test_analysis -v
"""

import json
import unittest
from dataclasses import replace
from unittest import mock

import app as app_module
from app import app
from tests.test_structure_engine import CFG, DOWN, UP, UP_THEN_BREAK_LOW, series
from trademate import analysis
from trademate.broker.base import Candle, ConnectionState, ConnectionStatus
from trademate.errors import BrokerUnavailableError

FORMING = replace(CFG, last_candle_closed=False)
ALLOWED = {"READY", "WATCH", "STALE DATA", "UNAVAILABLE", "INVALID DATA", "INSUFFICIENT DATA"}


def text_of(a):
    return json.dumps(a["interpretation"]).lower()


class StatusTest(unittest.TestCase):
    def test_healthy_data_without_a_developing_break_is_ready_and_not_a_signal(self):
        a = analysis.build_analysis("EURUSD", "H1", series(UP), FORMING, 2)
        self.assertEqual(a["status"]["code"], "READY")
        self.assertFalse(a["status"]["is_trade_signal"])
        self.assertIn("not a trade signal", a["status"]["meaning"].lower())

    def test_a_break_on_the_forming_candle_is_watch_not_ready(self):
        a = analysis.build_analysis("EURUSD", "H1", series(UP_THEN_BREAK_LOW), FORMING, 2)
        self.assertEqual(a["status"]["code"], "WATCH")
        st = a["structure"]
        self.assertEqual(st["developing_event"]["state"], "developing")
        self.assertEqual(st["latest_confirmed_event"]["state"], "confirmed")
        self.assertEqual(st["trend"]["confirmed"], "bullish")             # the developing CHoCH is NOT counted as settled
        self.assertEqual(st["trend"]["including_developing"], "bearish")
        self.assertIn("reads bullish", " ".join(a["interpretation"]["rule_based"]))
        self.assertIn("unconfirmed", " ".join(a["interpretation"]["observed_facts"]))

    def test_the_same_break_on_a_closed_candle_is_ready_and_confirmed(self):
        a = analysis.build_analysis("EURUSD", "H1", series(UP_THEN_BREAK_LOW), replace(CFG, last_candle_closed=True), 2)
        self.assertEqual(a["status"]["code"], "READY")
        self.assertEqual(a["structure"]["latest_event"]["state"], "confirmed")
        self.assertEqual(a["structure"]["trend"]["confirmed"], "bearish")

    def test_no_candles_is_unavailable_never_ready(self):
        a = analysis.build_analysis("EURUSD", "H1", [], FORMING)
        self.assertEqual(a["status"]["code"], "UNAVAILABLE")
        self.assertIsNone(a["structure"])
        self.assertIsNone(a["interpretation"])

    def test_too_few_candles_is_insufficient(self):
        a = analysis.build_analysis("EURUSD", "H1", series(UP)[:3], FORMING)
        self.assertEqual(a["status"]["code"], "INSUFFICIENT DATA")
        self.assertIsNone(a["interpretation"])

    def test_flat_market_with_no_swings_is_insufficient_not_ready(self):
        flat = [[1_000_000 + i * 3600, 10, 10.5, 9.5, 10] for i in range(20)]
        a = analysis.build_analysis("EURUSD", "H1", flat, FORMING)
        self.assertEqual(a["status"]["code"], "INSUFFICIENT DATA")
        self.assertIn("swing", a["status"]["reason"])

    def test_invalid_candle_makes_the_analysis_invalid_not_a_guess(self):
        rows = [list(r) for r in series(UP)]
        rows[4][2] = float("nan")
        a = analysis.build_analysis("EURUSD", "H1", rows, FORMING)
        self.assertEqual(a["status"]["code"], "INVALID DATA")
        self.assertEqual(a["context"]["data"]["completeness"], "invalid")
        self.assertIsNone(a["structure"])

    def test_conflicting_duplicate_candles_are_invalid(self):
        rows = [list(r) for r in series(UP)]
        dup = list(rows[5])
        dup[3] -= 0.2
        a = analysis.build_analysis("EURUSD", "H1", rows + [dup], FORMING)
        self.assertEqual(a["status"]["code"], "INVALID DATA")

    def test_only_substantiated_states_are_ever_emitted(self):
        for rows in (series(UP), series(DOWN), series(UP_THEN_BREAK_LOW), [], series(UP)[:2]):
            code = analysis.build_analysis("X", "H1", rows, FORMING)["status"]["code"]
            self.assertIn(code, ALLOWED)
            self.assertNotIn(code, ("NO SETUP", "ANALYSING"))      # reserved / browser-only

    def test_feed_problems_map_to_unavailable_or_stale(self):
        self.assertEqual(analysis.unavailable("X", "H1", FORMING, "no MT5")["status"]["code"], "UNAVAILABLE")
        stale = analysis.unavailable("X", "H1", FORMING, "MT5 stopped updating (60s ago)", stale=True)
        self.assertEqual(stale["status"]["code"], "STALE DATA")
        self.assertEqual(stale["context"]["data"]["freshness"], "stale_feed")
        self.assertIsNone(stale["structure"])


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.a = analysis.build_analysis("EURUSD", "H1", series(UP_THEN_BREAK_LOW), replace(CFG, last_candle_closed=True), 2)
        self.ev = self.a["structure"]["latest_event"]

    def test_choch_explanation_names_the_previous_direction_level_candle_and_method(self):
        lines = " ".join(self.ev["explanation"]["lines"])
        self.assertEqual(self.ev["type"], "CHOCH")
        self.assertIn("previous break was bullish", lines)
        self.assertIn(self.ev["level_text"], lines)
        self.assertIn(self.ev["candle"]["label"], lines)
        self.assertIn("CLOSED below", lines)
        self.assertIn("candle close", lines)
        self.assertIn("not proof that the market has reversed", lines)

    def test_first_break_explanation_says_why_it_is_a_bos(self):
        a = analysis.build_analysis("EURUSD", "H1", series(UP), replace(CFG, last_candle_closed=True), 2)
        first = a["structure"]["recent_events"][-1]
        self.assertEqual(first["type"], "BOS")
        self.assertIn("no earlier break", " ".join(first["explanation"]["lines"]).lower())

    def test_wick_mode_explanation_says_wick(self):
        cfg = replace(CFG, break_mode="wick", last_candle_closed=True)
        a = analysis.build_analysis("EURUSD", "H1", series(UP_THEN_BREAK_LOW), cfg, 2)
        self.assertIn("wick", " ".join(a["structure"]["latest_event"]["explanation"]["lines"]))
        self.assertEqual(a["structure"]["latest_event"]["method"], "wick")

    def test_event_fields_are_the_engines_own(self):
        for key in ("level", "candle_time", "break_mode", "confirmed", "reference_pivot_time", "trend_before"):
            self.assertIn(key, self.ev)

    def test_nothing_is_hard_coded_shifting_every_price_shifts_every_output(self):
        base = series(UP_THEN_BREAK_LOW)
        shifted = [[t, o + 1000, h + 1000, l + 1000, c + 1000] for t, o, h, l, c in base]
        cfg = replace(CFG, last_candle_closed=True)
        a, b = analysis.build_analysis("X", "H1", base, cfg, 2), analysis.build_analysis("X", "H1", shifted, cfg, 2)
        self.assertEqual([s["price"] + 1000 for s in a["structure"]["swings"]], [s["price"] for s in b["structure"]["swings"]])
        self.assertAlmostEqual(a["structure"]["latest_event"]["level"] + 1000, b["structure"]["latest_event"]["level"])
        self.assertEqual(a["status"]["code"], b["status"]["code"])

    def test_prices_are_formatted_with_the_symbol_digits(self):
        self.assertRegex(self.ev["level_text"], r"^\d+\.\d{2}$")
        a5 = analysis.build_analysis("EURUSD", "H1", series(UP_THEN_BREAK_LOW), replace(CFG, last_candle_closed=True), 5)
        self.assertRegex(a5["structure"]["latest_event"]["level_text"], r"^\d+\.\d{5}$")


class InterpretationHonestyTest(unittest.TestCase):
    def setUp(self):
        self.a = analysis.build_analysis("EURUSD", "H1", series(UP_THEN_BREAK_LOW), FORMING, 2)
        self.i = self.a["interpretation"]

    def test_four_kinds_of_statement_are_kept_apart(self):
        self.assertEqual(set(self.i), {"observed_facts", "rule_based", "watch", "setups"})
        self.assertTrue(self.i["observed_facts"] and self.i["rule_based"] and self.i["watch"])

    def test_setups_are_not_available_and_nothing_claims_a_trade(self):
        s = self.i["setups"]
        self.assertFalse(s["supported"])
        self.assertEqual(s["status"], "NOT AVAILABLE")
        body = text_of(self.a)
        for word in ("buy ", "sell ", "entry", "take profit", "stop loss", "go long", "go short"):
            self.assertNotIn(word, body)

    def test_watch_items_are_conditional_not_predictions(self):
        for line in self.i["watch"][:-1]:
            self.assertTrue(line.startswith("If ") or line.startswith("The forming candle must"), line)
        self.assertIn("not trade signals", self.i["watch"][-1])

    def test_choch_is_never_called_a_reversal_or_bos_high_probability(self):
        a = analysis.build_analysis("EURUSD", "H1", series(UP_THEN_BREAK_LOW), replace(CFG, last_candle_closed=True), 2)
        body = json.dumps(a["interpretation"]).lower() + " ".join(a["caveats"]).lower()
        self.assertIn("does not confirm a reversal", body)
        self.assertIn("does not by itself confirm", body)
        self.assertNotIn("confirmed reversal", body.replace("does not confirm a reversal", ""))
        self.assertIn("high-probability", body)                            # only inside the 'does not mean' caveat
        self.assertIn("not forecasts, signals or guarantees", body)

    def test_data_quality_and_clock_limits_are_stated_honestly(self):
        d = self.a["context"]["data"]
        self.assertEqual(d["candle_clock"], "not_verifiable")
        self.assertIn("does not export", d["candle_clock_note"])
        self.assertEqual(self.a["context"]["latest_candle"]["state"], "forming")
        self.assertIn("server time", self.a["context"]["time_note"])

    def test_modules_that_do_not_exist_are_marked_not_available_and_engines_report_what_ran(self):
        """Updated in the Phase 7C integration repair: FVG and Order Blocks are real engines now, so their status is decided per response
        from what actually ran (this fixture has valid candles but no liquidity context). Modules that do not exist stay `not_available`."""
        by_id = {m["id"]: m for m in self.a["modules"]}
        self.assertEqual(by_id["structure"]["status"], "available")
        for later in ("multi_timeframe", "setups"):
            self.assertEqual(by_id[later]["status"], "not_available")
        self.assertEqual((by_id["fvg"]["status"], by_id["order_blocks"]["status"]), ("available", "available"))
        self.assertEqual(by_id["liquidity"]["status"], "unavailable")              # no liquidity context was supplied: honest, with a reason
        self.assertTrue(by_id["liquidity"]["reason"])


class Feed:
    """Connected feed with candles + symbol spec only. No order method exists, so analysis cannot trade."""

    def __init__(self, data, status=None):
        self.data = data
        self.calls = []
        self.status = status or ConnectionStatus(ConnectionState.CONNECTED, "ok", 0.2)

    def get_connection_status(self):
        return self.status

    def get_candles(self, symbol, timeframe, count=300):
        self.calls.append((symbol, timeframe.upper(), count))
        try:
            return [Candle(*c) for c in self.data[(symbol, timeframe.upper())]]
        except KeyError:
            raise BrokerUnavailableError(f"No candles for {symbol} {timeframe}.") from None

    def get_symbol_spec(self, symbol):
        return mock.Mock(digits=2)


class AnalysisApiTest(unittest.TestCase):
    def use(self, feed):
        p = mock.patch.object(app_module, "adapter", feed)
        p.start()
        self.addCleanup(p.stop)
        return app.test_client()

    def get(self, client, symbol="EURUSD", tf="H1", extra="&swing_left=2&swing_right=2"):
        return client.get(f"/api/analysis?symbol={symbol}&timeframe={tf}{extra}")

    def test_returns_the_view_model_for_the_requested_symbol_and_timeframe(self):
        c = self.use(Feed({("EURUSD", "H1"): series(UP_THEN_BREAK_LOW), ("XAUUSD", "M15"): series(DOWN)}))
        a = self.get(c).get_json()
        self.assertTrue(a["ok"])
        self.assertEqual((a["symbol"], a["timeframe"]), ("EURUSD", "H1"))
        self.assertEqual(a["status"]["code"], "WATCH")                     # API treats the newest candle as forming
        b = self.get(c, "XAUUSD", "M15").get_json()
        self.assertEqual((b["symbol"], b["timeframe"]), ("XAUUSD", "M15"))
        self.assertNotEqual(a["structure"]["swings"], b["structure"]["swings"])   # independent per symbol/timeframe

    def test_disconnected_is_unavailable_and_old_snapshot_is_stale(self):
        down = ConnectionStatus(ConnectionState.DISCONNECTED, "MT5 is not running.")
        a = self.get(self.use(Feed({}, down))).get_json()
        self.assertEqual(a["status"]["code"], "UNAVAILABLE")
        old = ConnectionStatus(ConnectionState.DISCONNECTED, "MT5 stopped updating TradeMate (last update 60s ago).", 60.0)
        b = self.get(self.use(Feed({}, old))).get_json()
        self.assertEqual(b["status"]["code"], "STALE DATA")

    def test_missing_candles_are_unavailable_with_the_reason(self):
        a = self.get(self.use(Feed({}))).get_json()
        self.assertEqual(a["status"]["code"], "UNAVAILABLE")
        self.assertIn("No candles", a["status"]["reason"])

    def test_bad_requests_are_refused_by_the_server(self):
        c = self.use(Feed({("EURUSD", "H1"): series(UP)}))
        for url in ("/api/analysis", "/api/analysis?symbol=", "/api/analysis?symbol=EURUSD&timeframe=W9",
                    "/api/analysis?symbol=EURUSD&timeframe=H1&swing_left=0", "/api/analysis?symbol=EURUSD&timeframe=H1&swing_left=x",
                    "/api/analysis?symbol=" + "X" * 40):
            r = c.get(url)
            self.assertEqual(r.status_code, 400, url)
            self.assertFalse(r.get_json()["ok"])

    def test_the_route_is_read_only(self):
        feed = Feed({("EURUSD", "H1"): series(UP)})
        c = self.use(feed)
        self.assertEqual(c.post("/api/analysis").status_code, 405)
        self.assertFalse(hasattr(feed, "place_order"))                    # the analysis path could not trade even if it tried
        self.assertTrue(self.get(c).get_json()["ok"])

    def test_analysis_uses_strategy_history_limit_and_verified_candle_close(self):
        from types import SimpleNamespace

        rows = series(UP_THEN_BREAK_LOW)
        feed = Feed({("EURUSD", "H1"): rows})
        client = self.use(feed)
        latest = rows[-1][0]
        clock = SimpleNamespace(known=True, offset_seconds=0, source="test")
        with mock.patch.object(app_module, "time", SimpleNamespace(time=lambda: latest + 3600)), \
             mock.patch.object(app_module, "resolve_server_clock", return_value=clock):
            a = self.get(client).get_json()
        self.assertEqual(feed.calls[-1], ("EURUSD", "H1", 500))
        self.assertEqual(a["context"]["structure_input"]["last_candle_closed"], True)
        self.assertEqual(a["context"]["data"]["candle_clock"], "verified")
        self.assertEqual(a["context"]["latest_candle"]["state"], "closed")
        self.assertEqual(a["structure"]["trend"]["confirmed"], "bearish")
        self.assertTrue(a["structure"]["latest_event"]["confirmed"])


if __name__ == "__main__":
    unittest.main()
