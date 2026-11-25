"""Full Test runner: allowlist, honest status labels, no overlapping runs, scrubbed environment, guarded routes.

These tests drive the real RunManager with tiny FIXTURE test modules (tests/runner_fixtures), never with the real
groups, so they cannot recurse into themselves and cannot place an order.

    python3 -m unittest tests.test_testrunner -v
"""

import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import app
from trademate import testrunner as tr

ROOT = Path(__file__).resolve().parent.parent


def grp(gid, modules=(), node=(), title=None, kind="automated"):
    return {"id": gid, "title": title or gid, "kind": kind, "simulated_broker": True, "modules": list(modules), "node": list(node),
            "info": ""}


FX = "tests.runner_fixtures."


class Registry(unittest.TestCase):
    def test_every_test_module_belongs_to_exactly_one_group(self):
        groups = tr.build_groups()
        seen = {}
        for g in groups:
            for m in g.get("modules", []):
                self.assertNotIn(m, seen, f"{m} is in both {seen.get(m)} and {g['id']}")
                seen[m] = g["id"]
        self.assertEqual(sorted(seen), tr.discover_test_modules())                 # nothing falls off the dashboard

    def test_every_listed_module_and_script_exists(self):
        for g in tr.GROUPS:
            for m in g.get("modules", []):
                self.assertTrue((ROOT / (m.replace(".", "/") + ".py")).exists(), m)
            for n in g.get("node", []):
                self.assertTrue((ROOT / n).exists(), n)
        for n in tr.GENERAL_NODE:
            self.assertTrue((ROOT / n).exists(), n)

    def test_group_ids_are_safe_and_unique(self):
        ids = [g["id"] for g in tr.build_groups()]
        self.assertEqual(len(ids), len(set(ids)))
        for i in ids:
            self.assertRegex(i, tr._ID_RE.pattern)

    def test_no_real_order_script_is_in_any_group(self):
        text = repr(tr.build_groups())
        for banned in ("real_demo_execution_test", "live_phase6_test", "live_test"):
            self.assertNotIn(banned + "'", text.replace("tests.test_" + banned, ""), banned)
        self.assertTrue(all(o["kind"] == "opt-in demo execution" for o in tr.OPT_IN))

    def test_only_groups_that_exist_are_listed(self):
        titles = " ".join(g["title"] for g in tr.GROUPS)
        for needed in ("Market Structure", "Analysis", "Risk Engine", "Risk-first", "Price-first", "Headway-Demo", "Browser"):
            self.assertIn(needed, titles)


class Labels(unittest.TestCase):
    def test_group_classification(self):
        c = tr.classify_group
        self.assertEqual(c({"passed": 5, "skipped": 0, "failed": 0, "error": 0}, 0, False, None), "PASS")
        self.assertEqual(c({"passed": 5, "skipped": 2, "failed": 0, "error": 0}, 0, False, None), "PARTIAL")
        self.assertEqual(c({"passed": 0, "skipped": 4, "failed": 0, "error": 0}, 0, False, None), "PARTIAL")
        self.assertEqual(c({"passed": 5, "skipped": 0, "failed": 1, "error": 0}, 1, False, None), "FAIL")
        self.assertEqual(c({"passed": 5, "skipped": 0, "failed": 0, "error": 1}, 1, False, None), "FAIL")
        self.assertEqual(c({"passed": 5, "skipped": 0, "failed": 0, "error": 0}, 0, True, None), "FAIL")      # timed out
        self.assertEqual(c({"passed": 0, "skipped": 0, "failed": 0, "error": 0}, 0, False, None), "FAIL")      # ran nothing
        self.assertEqual(c({"passed": 3, "skipped": 0, "failed": 0, "error": 0}, 139, False, None), "FAIL")    # crashed
        self.assertEqual(c({}, None, False, "no node"), "BLOCKED")

    def test_overall(self):
        o = tr.overall_status
        self.assertEqual(o([{"status": "PASS"}, {"status": "PASS"}], 2, 2)[0], "PASS")
        self.assertEqual(o([{"status": "PASS"}, {"status": "PARTIAL"}], 2, 2)[0], "PARTIAL")
        self.assertEqual(o([{"status": "PASS"}, {"status": "BLOCKED"}], 2, 2)[0], "PARTIAL")
        self.assertEqual(o([{"status": "PASS"}], 1, 5)[0], "PARTIAL")                   # a subset is never a complete PASS
        self.assertEqual(o([{"status": "PASS"}, {"status": "FAIL"}], 2, 2)[0], "FAIL")
        self.assertEqual(o([{"status": "BLOCKED"}], 1, 1)[0], "BLOCKED")
        self.assertIn("does not prove real MT5", o([{"status": "PASS"}], 1, 1)[1])


class Running(unittest.TestCase):
    def manager(self, groups, timeout=60):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tr.RunManager(Path(tmp.name), groups=groups, cwd=ROOT, timeout=timeout)

    def wait(self, mgr, limit=60):
        end = time.time() + limit
        while time.time() < end:
            if not mgr.status().get("running"):
                return mgr.status()
            time.sleep(0.05)
        self.fail("run did not finish")

    def group(self, st, gid):
        return next(g for g in st["groups"] if g["id"] == gid)

    def test_pass_fail_skip_are_reported_separately_and_never_blended(self):
        m = self.manager([grp("ok", [FX + "ok_mod"]), grp("mixed", [FX + "mixed_mod"]), grp("skipped", [FX + "skip_mod"])])
        self.assertTrue(m.start("all")[0])
        st = self.wait(m)
        ok, mixed, skipped = (self.group(st, i) for i in ("ok", "mixed", "skipped"))
        self.assertEqual((ok["status"], ok["passed"], ok["failed"], ok["skipped"]), ("PASS", 2, 0, 0))
        self.assertEqual((mixed["status"], mixed["passed"], mixed["failed"], mixed["skipped"]), ("FAIL", 1, 1, 1))
        self.assertEqual((skipped["status"], skipped["passed"], skipped["skipped"]), ("PARTIAL", 0, 1))
        self.assertIn("NOT run", skipped["note"])
        self.assertIn("dependency missing", skipped["note"])
        self.assertEqual(st["overall"], "FAIL")
        failed = next(t for t in mixed["tests"] if t["status"] == "failed")
        self.assertIn("Mixed.test_fails", failed["id"])
        self.assertIn("expected 1 got 2", failed["detail"])
        self.assertIsNotNone(ok["seconds"])

    def test_skips_make_the_overall_result_partial_not_pass(self):
        m = self.manager([grp("ok", [FX + "ok_mod"]), grp("skipped", [FX + "skip_mod"])])
        m.start("all")
        st = self.wait(m)
        self.assertEqual(st["overall"], "PARTIAL")
        self.assertIn("skipped tests did not run", st["overall_note"])

    def test_a_complete_clean_run_is_pass_and_says_what_it_does_not_prove(self):
        m = self.manager([grp("a", [FX + "ok_mod"]), grp("b", [], [FX.replace(".", "/") + "node_ok.js"])])
        m.start("all")
        st = self.wait(m)
        self.assertEqual(st["overall"], "PASS", st)
        self.assertIn("does not prove real MT5", st["overall_note"])
        self.assertEqual(self.group(st, "b")["checks"][0]["status"], "PASS")

    def test_a_subset_run_is_never_a_complete_pass(self):
        m = self.manager([grp("a", [FX + "ok_mod"]), grp("b", [FX + "ok_mod"])])
        m.start(["a"])
        self.assertEqual(self.wait(m)["overall"], "PARTIAL")

    def test_node_failure_is_a_failure_with_its_output(self):
        m = self.manager([grp("n", [], ["tests/runner_fixtures/node_fail.js"])])
        m.start("all")
        g = self.group(self.wait(m), "n")
        self.assertEqual(g["status"], "FAIL")
        self.assertIn("node fixture failed", g["checks"][0]["detail"])

    def test_missing_node_is_blocked_not_passed(self):
        m = self.manager([grp("n", [], ["tests/runner_fixtures/node_ok.js"])])
        with mock.patch.object(tr.shutil, "which", return_value=None):
            m.start("all")
            st = self.wait(m)
        self.assertEqual(self.group(st, "n")["status"], "BLOCKED")
        self.assertEqual(st["overall"], "BLOCKED")

    def test_a_hung_group_is_killed_and_reported_as_failed(self):
        m = self.manager([grp("h", [FX + "hang_mod"])], timeout=2)
        started = time.time()
        m.start("all")
        g = self.group(self.wait(m), "h")
        self.assertEqual(g["status"], "FAIL")
        self.assertIn("Timed out", g["note"])
        self.assertLess(time.time() - started, 20)

    def test_a_module_that_cannot_be_imported_is_a_failure(self):
        m = self.manager([grp("bad", ["tests.runner_fixtures.does_not_exist"])])
        m.start("all")
        self.assertEqual(self.group(self.wait(m), "bad")["status"], "FAIL")

    def test_only_one_run_at_a_time(self):
        m = self.manager([grp("slow", [FX + "slow_mod"])])
        self.assertTrue(m.start("all")[0])
        ok, msg = m.start("all")
        self.assertFalse(ok)
        self.assertIn("already in progress", msg)
        self.wait(m)
        self.assertTrue(m.start("all")[0])                                    # free again afterwards
        self.wait(m)

    def test_start_returns_immediately_and_never_blocks_the_caller(self):
        m = self.manager([grp("slow", [FX + "slow_mod"])])
        t0 = time.time()
        m.start("all")
        self.assertLess(time.time() - t0, 1.0)
        self.assertTrue(m.status()["running"])
        self.wait(m)

    def test_last_run_is_persisted_with_a_timestamp_and_survives_a_new_manager(self):
        m = self.manager([grp("ok", [FX + "ok_mod"])])
        self.assertIsNone(m.last())
        m.start("all")
        self.wait(m)
        last = m.last()
        self.assertRegex(last["finished"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(last["overall"], "PASS")
        again = tr.RunManager(m.dir.parent, groups=m.groups, cwd=ROOT)
        self.assertEqual(again.last()["run_id"], last["run_id"])

    def test_cancel_stops_a_running_group(self):
        m = self.manager([grp("h", [FX + "hang_mod"]), grp("ok", [FX + "ok_mod"])], timeout=60)
        m.start("all")
        time.sleep(1.0)
        self.assertTrue(m.cancel())
        st = self.wait(m, 30)
        self.assertEqual(st["overall"], "PARTIAL")
        self.assertIn("cancelled", st["overall_note"])
        self.assertFalse(m.cancel())

    def test_child_environment_cannot_carry_the_real_order_opt_in(self):
        m = self.manager([grp("env", [FX + "env_mod"])])
        with mock.patch.dict(os.environ, {"TRADEMATE_REAL_DEMO_ORDER": "YES_PLACE_ONE_DEMO_ORDER"}):
            self.assertNotIn("TRADEMATE_REAL_DEMO_ORDER", m._env())
            m.start("all")
            g = self.group(self.wait(m), "env")
        self.assertEqual(g["status"], "PASS", g)


class Allowlist(unittest.TestCase):
    def test_only_known_group_ids_are_accepted_and_input_is_not_echoed(self):
        m = tr.RunManager(Path(tempfile.mkdtemp()), groups=[grp("ok", [FX + "ok_mod"])], cwd=ROOT)
        self.addCleanup(shutil.rmtree, str(m.dir.parent), True)
        for bad in (["../../etc/passwd"], ["ok; rm -rf /"], ["$(id)"], ["OK"], ["x" * 100], [1], [None], [], "ok", {"a": 1}, 5,
                    ["ok", "nope"], ["python -c print(1)"]):
            ok, msg = m.start(bad)
            self.assertFalse(ok, bad)
            self.assertNotIn("rm -rf", msg)
            self.assertNotIn("passwd", msg)
        self.assertFalse(m.status().get("running"))


class Routes(unittest.TestCase):
    H = {"X-TradeMate-Test": "1"}

    def use(self, mode="development", host="127.0.0.1"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mgr = tr.RunManager(Path(tmp.name), groups=[grp("ok", [FX + "ok_mod"]), grp("slow", [FX + "slow_mod"])], cwd=ROOT)
        fake = mock.Mock()
        fake.mode.value = mode
        fake.ui.host = host
        fake.app = app_module.settings.app
        for target, value in (("test_runner", mgr), ("settings", mock.Mock(mode=mock.Mock(value=mode), ui=mock.Mock(host=host),
                                                                         app=app_module.settings.app, risk=app_module.settings.risk))):
            p = mock.patch.object(app_module, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: mgr.cancel())
        return app.test_client(), mgr

    def test_a_normal_local_request_works(self):
        c, mgr = self.use()
        cat = c.get("/api/fulltest/catalog").get_json()
        self.assertTrue(cat["ok"])
        self.assertIn("does not prove that real MT5", cat["disclaimer"])
        self.assertEqual({o["id"] for o in cat["opt_in"]}, {"real_demo", "live_cycle"})
        r = c.post("/api/fulltest/run", json={"groups": ["ok"]}, headers=self.H)
        self.assertEqual(r.status_code, 202)
        for _ in range(100):
            st = c.get("/api/fulltest/status").get_json()
            if not st["state"]["running"]:
                break
            time.sleep(0.1)
        self.assertEqual(st["state"]["groups"][0]["status"], "PASS")
        self.assertEqual(st["last"]["run_id"], st["state"]["run_id"])

    def test_live_mode_is_refused_everywhere(self):
        c, _ = self.use(mode="live")
        for method, url in (("get", "/api/fulltest/catalog"), ("get", "/api/fulltest/status"), ("post", "/api/fulltest/run"),
                            ("post", "/api/fulltest/cancel")):
            r = getattr(c, method)(url, headers=self.H)
            self.assertEqual(r.status_code, 403, url)

    def test_server_reachable_from_other_computers_is_refused(self):
        c, _ = self.use(host="0.0.0.0")
        self.assertEqual(c.get("/api/fulltest/catalog").status_code, 403)
        self.assertEqual(c.post("/api/fulltest/run", json={}, headers=self.H).status_code, 403)

    def test_requests_from_another_machine_are_refused(self):
        c, mgr = self.use()
        r = c.post("/api/fulltest/run", json={"groups": ["ok"]}, headers=self.H, environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(mgr.status().get("running"))
        self.assertEqual(c.get("/api/fulltest/status", environ_overrides={"REMOTE_ADDR": "10.0.0.2"}).status_code, 403)

    def test_post_needs_the_custom_header_and_a_same_origin_origin(self):
        c, mgr = self.use()
        self.assertEqual(c.post("/api/fulltest/run", json={"groups": ["ok"]}).status_code, 403)
        evil = {**self.H, "Origin": "http://evil.example"}
        self.assertEqual(c.post("/api/fulltest/run", json={"groups": ["ok"]}, headers=evil).status_code, 403)
        same = {**self.H, "Origin": "http://localhost"}
        self.assertEqual(c.post("/api/fulltest/run", json={"groups": ["ok"]}, headers=same).status_code, 202)
        self.assertFalse(mgr.status().get("run_id") is None)

    def test_bad_group_ids_get_400_and_nothing_runs(self):
        c, mgr = self.use()
        for body in ({"groups": ["ok; reboot"]}, {"groups": ["nope"]}, {"groups": []}, {"groups": "ok"}):
            r = c.post("/api/fulltest/run", json=body, headers=self.H)
            self.assertEqual(r.status_code, 400, body)
            self.assertNotIn("reboot", r.get_data(as_text=True))
        self.assertIsNone(mgr.status().get("run_id"))

    def test_overlapping_run_gets_409(self):
        c, mgr = self.use()
        self.assertEqual(c.post("/api/fulltest/run", json={"groups": ["slow"]}, headers=self.H).status_code, 202)
        r = c.post("/api/fulltest/run", json={"groups": ["ok"]}, headers=self.H)
        self.assertEqual(r.status_code, 409)
        self.assertIn("already in progress", r.get_json()["message"])
        c.post("/api/fulltest/cancel", headers=self.H)

    def test_the_page_is_served_by_flask_at_both_urls(self):
        c, _ = self.use()
        for url in ("/full-test", "/Full%20Test.html"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn("Full Test", r.get_data(as_text=True))
            r.close()


class PageIsSafe(unittest.TestCase):
    def test_dashboard_script_never_uses_html_injection_apis(self):
        js = (ROOT / "static" / "js" / "fulltest.js").read_text(encoding="utf-8")
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, js, banned)
        html = (ROOT / "Full Test.html").read_text(encoding="utf-8")
        self.assertNotRegex(html, r"\son[a-z]+\s*=")                               # no inline event handlers
        self.assertNotIn("<script>", html.replace('<script src', ''))               # no inline script bodies

    def test_no_route_accepts_a_command(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        block = src[src.index("def api_fulltest_run"):src.index("def api_fulltest_cancel")]
        for banned in ("subprocess", "os.system", "shell=True", "eval(", "exec("):
            self.assertNotIn(banned, block)


if __name__ == "__main__":
    unittest.main()
