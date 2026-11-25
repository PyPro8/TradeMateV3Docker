"""The Trading Journal: the user's personal notepad. It documents trades (including Rapid Fire's) and never overwrites the user's own words.

    python3 -m unittest tests.test_trade_journal -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trademate.trade_journal import TradeJournal


def rf_row(ticket, net=3.0, role="ORIGINAL", status="CLOSED", t=1_800_000_000):
    return {"ticket": ticket, "execution_status": status, "role_label": role, "side": "buy", "volume": 0.02, "entry_price": 1.1, "exit_price": 1.1003, "gross": net, "commission": 0.0,
            "swap": 0.0, "net": net, "closed_by": "take_profit", "twin_of_ticket": None, "symbol": "EURUSD", "entry_time": t, "target_points": 30.0}


class JournalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "j.json"
        self.j = TradeJournal(self.path)

    FULL = dict(date="2026-11-20", time="09:30", instrument="XAUUSD", strategy="Rapid Fire", result="win", profit_loss=12.5, emotion="anxious", notes="Hesitated.")

    def test_an_entry_keeps_every_documentation_field(self):
        e, problems = self.j.create(self.FULL)
        self.assertEqual(problems, [])
        for k, v in self.FULL.items():
            self.assertEqual(e[k], v, k)
        self.assertEqual(TradeJournal(self.path).list()[0]["notes"], "Hesitated.")              # persisted

    def test_personal_notes_and_emotions_stay_editable(self):
        e, _ = self.j.create(self.FULL)
        e2, problems = self.j.update(e["id"], {"emotion": "calm", "notes": "Better after review.", "profit_loss": -3})
        self.assertEqual(problems, [])
        self.assertEqual((e2["emotion"], e2["notes"], e2["profit_loss"], e2["instrument"]), ("calm", "Better after review.", -3.0, "XAUUSD"))
        self.assertTrue(self.j.delete(e["id"]))
        self.assertFalse(self.j.delete(e["id"]))

    def test_validation(self):
        bad = [dict(self.FULL, date="20/11/2026"), dict(self.FULL, time="25:00"), dict(self.FULL, result="great"), dict(self.FULL, profit_loss="abc"), dict(self.FULL, notes="x" * 5001),
               dict(self.FULL, nope=1), {}, dict(self.FULL, instrument=5)]
        for b in bad:
            e, problems = self.j.create(b)
            self.assertIsNone(e, b)
            self.assertTrue(problems, b)
        self.assertEqual(self.j.list(), [])
        e, _ = self.j.create(self.FULL)
        self.assertIsNone(self.j.update(e["id"], {"result": "great"})[0])
        self.assertIsNone(self.j.update("nope", {"notes": "x"})[0])

    def test_a_corrupt_file_reads_as_empty_not_a_crash(self):
        self.path.write_text("{not json")
        self.assertEqual(self.j.list(), [])
        e, problems = self.j.create(self.FULL)
        self.assertEqual(problems, [])

    # ---- Rapid Fire trades are documented, never executed, and never overwrite the user's words
    def test_rapid_fire_import_creates_entries_with_empty_emotion_and_notes(self):
        res = self.j.import_rapid_fire([rf_row(1, 3.0), rf_row(2, -1.0, role="ANTI-TWIN"), rf_row(3, status="OPEN")])
        self.assertEqual(res, {"created": 2, "refreshed": 0})
        by = {e["auto"]["ticket"]: e for e in self.j.list()}
        self.assertEqual((by[1]["result"], by[1]["profit_loss"], by[1]["instrument"], by[1]["emotion"], by[1]["notes"]), ("win", 3.0, "EURUSD", "", ""))
        self.assertEqual((by[2]["result"], by[2]["strategy"]), ("loss", "Rapid Fire (Anti-Twin)"))
        self.assertNotIn(3, by)                                                                  # an open trade is not a finished experience yet

    def test_a_refresh_never_touches_the_users_notes_and_emotions(self):
        self.j.import_rapid_fire([rf_row(1, 3.0)])
        e = self.j.list()[0]
        self.j.update(e["id"], {"emotion": "greedy", "notes": "I should have waited.", "result": "loss", "profit_loss": 99})
        res = self.j.import_rapid_fire([rf_row(1, 5.0)])                                         # MT5's numbers changed
        self.assertEqual(res, {"created": 0, "refreshed": 1})
        e2 = self.j.list()[0]
        self.assertEqual((e2["emotion"], e2["notes"], e2["result"], e2["profit_loss"]), ("greedy", "I should have waited.", "loss", 99.0))   # the user's own words stand
        self.assertEqual(e2["auto"]["net"], 5.0)                                                 # only the automatic facts were refreshed
        self.assertEqual(len(self.j.list()), 1)

    def test_the_auto_block_cannot_be_edited_through_the_user_api(self):
        self.j.import_rapid_fire([rf_row(1)])
        e = self.j.list()[0]
        self.assertIsNone(self.j.update(e["id"], {"auto": {"ticket": 9}})[0])


class JournalApiTest(unittest.TestCase):
    def setUp(self):
        import app as A
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.j = TradeJournal(Path(self._tmp.name) / "j.json")
        p = mock.patch.object(A, "trade_journal", self.j)
        p.start()
        self.addCleanup(p.stop)
        self.A, self.c = A, A.app.test_client()

    def test_crud_through_the_api(self):
        r = self.c.post("/api/trade-journal", json=JournalTest.FULL)
        self.assertTrue(r.get_json()["ok"])
        eid = r.get_json()["entry"]["id"]
        self.assertEqual(self.c.post("/api/trade-journal", json={"date": "bad"}).status_code, 400)
        self.assertEqual(self.c.post("/api/trade-journal", data="x").status_code, 400)
        r = self.c.put(f"/api/trade-journal/{eid}", json={"emotion": "calm", "notes": "edited"})
        self.assertEqual((r.get_json()["entry"]["emotion"], r.get_json()["entry"]["notes"]), ("calm", "edited"))
        self.assertEqual(self.c.put("/api/trade-journal/nope", json={"notes": "x"}).status_code, 404)
        got = self.c.get("/api/trade-journal").get_json()
        self.assertEqual(len(got["entries"]), 1)
        self.assertIn("anxious", got["emotions"])
        self.assertEqual(self.c.delete(f"/api/trade-journal/{eid}").status_code, 200)
        self.assertEqual(self.c.delete(f"/api/trade-journal/{eid}").status_code, 404)

    def test_import_from_the_rapid_fire_engine_does_not_execute_anything(self):
        fake = mock.Mock(trade_rows=mock.Mock(return_value=[rf_row(11), rf_row(12)]))
        with mock.patch.object(self.A, "rapid_engine", fake):
            j = self.c.post("/api/trade-journal/import-rapid-fire").get_json()
        self.assertEqual((j["created"], j["refreshed"]), (2, 0))
        self.assertEqual([c[0] for c in fake.method_calls], ["trade_rows"])                              # a read only: nothing is started, stopped or sent
        self.assertEqual(len(self.c.get("/api/trade-journal").get_json()["entries"]), 2)

    def test_the_journal_survives_a_hard_reset(self):
        from trademate.hard_reset import KEPT, RESET_FILES
        self.assertIn("trade_journal.json", KEPT)
        self.assertNotIn("trade_journal.json", RESET_FILES)


if __name__ == "__main__":
    unittest.main()
