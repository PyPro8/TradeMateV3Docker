"""Full Test runner service: runs TradeMate's own automated tests for the Full Test dashboard and reports them honestly.

WHAT IT DOES
  * Reuses the repository's `unittest` tests through tests/json_runner.py (structured results). Node checks run as `node file`.
  * Test groups are an ALLOWLIST defined below. The browser can only pick group ids from it. There is no way to pass a
    command, a path or an argument from the browser, so this is not remote command execution.
  * Runs in a background thread and subprocesses, never on a request thread. One run at a time; a second request while a run
    is active is refused with a clear message.
  * The child process gets a scrubbed environment: the real-order opt-in variable is REMOVED, so a test run can never place an
    MT5 order even if it is set in your shell. Tests that can place a real demo order are NOT in any group.

HOW RESULTS ARE LABELLED (nothing is rounded up to PASS)
  group  PASS     every test passed and none was skipped
         PARTIAL  nothing failed, but some tests were skipped (they did NOT run)
         FAIL     at least one failed/errored, or the process crashed / timed out
         BLOCKED  the group could not start (e.g. Node.js is not installed)
         NOT RUN  not requested
  overall FAIL > PARTIAL (anything skipped / blocked / not run) > PASS. PASS needs every group run, none skipped or blocked.
  The opt-in real-MT5 tests are listed but never run here; passing this suite does NOT prove real broker execution.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("trademate.testrunner")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GROUP_TIMEOUT_SECONDS = 900
MAX_LOG_CHARS = 20000

# kind: automated | browser | integration. `simulated_broker`: the fake bridge / stubs stand in for MT5 (never a real one).
GROUPS: list[dict] = [
    {"id": "structure", "title": "Market Structure Engine", "kind": "automated", "simulated_broker": False,
     "modules": ["tests.test_structure_engine"], "info": "Swing detection, classification, BOS/CHoCH, forming candles, data handling."},
    {"id": "structure_regressions", "title": "BOS/CHoCH and lookahead-bias regressions", "kind": "automated", "simulated_broker": False,
     "modules": ["tests.test_structure_regressions"], "info": "Ranging markets, superseded levels, prefix invariants, duplicates, isolation."},
    {"id": "chart", "title": "Chart integration and chart settings", "kind": "automated", "simulated_broker": False,
     "modules": ["tests.test_chart_settings", "tests.test_chart_style_js"], "node": ["tests/chart_style_test.js"],
     "info": "Candle colours, presets, persistence, theme, overlay data."},
    {"id": "analysis", "title": "Analysis sidebar", "kind": "automated", "simulated_broker": True,
     "modules": ["tests.test_analysis", "tests.test_structure_api"], "info": "Analysis view model, status states, /api/analysis, structure API."},
    {"id": "mtf_strategy", "title": "Multi-timeframe Strategy Engine", "kind": "automated", "simulated_broker": True,
     "modules": ["tests.test_mtf_timeframes", "tests.test_mtf_strategy", "tests.test_mtf_api"],
     "info": "M5/M15/H1/H4: closed vs forming candles, data quality, native H4, conflict rules, qualification gates, /api/strategy/mtf, [strategy] settings."},
    {"id": "strategy_lab", "title": "Command Centre", "kind": "automated", "simulated_broker": True,
     "modules": ["tests.test_strategylab", "tests.test_watchdog", "tests.test_labgreeting_js"], "node": ["tests/labgreeting_test.js"],
     "info": "Command Centre capability registry, broker-backed analysis, read-only Watchdog and setup deduplication, safe rendering, browser alerts, and greeting preferences."},
    {"id": "strategies", "title": "Strategies: CHoCH Trading and Trend Continuation", "kind": "unit", "simulated_broker": True,
     "modules": ["tests.test_strategies"],
     "info": "Registry, validated persisted selection, H1/M5-only qualification of both strategies, independence, strategy switching with pending proposals, Watchdog binding, Autopilot honesty. Candles only: never a real order."},
    {"id": "pipeline", "title": "Strategy -> Risk -> Execution pipeline", "kind": "integration", "simulated_broker": True,
     "modules": ["tests.test_pipeline"],
     "info": "Qualification contract, Risk approval sized from the live price, execution eligibility, single-use bound approvals, revalidation, broker outcomes, duplicates. Simulated bridge only: never a real order."},
    {"id": "risk", "title": "Risk Engine", "kind": "automated", "simulated_broker": False,
     "modules": ["tests.test_risk_limits", "tests.test_risk_sizing", "tests.test_phase5_complete"],
     "info": "Position sizing, limits, daily loss, exposure, approvals."},
    {"id": "daily_loss", "title": "Daily loss reset and broker clock", "kind": "automated", "simulated_broker": True,
     "modules": ["tests.test_daily_loss_reset"], "info": "Broker trading-day boundary, Today's Loss / Remaining Daily Loss, unknown data, no local-timezone fallback."},
    {"id": "clocks", "title": "Clocks and candle countdown", "kind": "automated", "simulated_broker": False,
     "modules": ["tests.test_clock_js"], "node": ["tests/clock_test.js"], "info": "Eastern Time EST/EDT, configured zone, countdown maths for every timeframe, page markup."},
    {"id": "sl_risk_first", "title": "Risk-first Stop Loss calculation", "kind": "automated", "simulated_broker": False,
     "modules": ["tests.test_sl_risk_first"], "info": "Risk amount to SL price; never exceeds the requested risk."},
    {"id": "sl_price_first", "title": "Price-first Stop Loss calculation", "kind": "automated", "simulated_broker": False,
     "modules": ["tests.test_sl_price_first"], "info": "SL price to risk amount; tick grid, direction, limits."},
    {"id": "sl_instruments", "title": "BUY/SELL and instrument-specific calculations", "kind": "automated", "simulated_broker": True,
     "modules": ["tests.test_sl_instruments", "tests.test_slcalc_js"], "node": ["tests/slcalc_test.js"],
     "info": "FX, JPY, gold, index, crypto, cross pairs; /api/risk/sl; stop-loss panel logic."},
    {"id": "execution", "title": "Execution safety and approval gates", "kind": "integration", "simulated_broker": True,
     "modules": ["tests.test_execution", "tests.test_execution_safety", "tests.test_phase6", "tests.test_outcomes",
                 "tests.test_command_expiry", "tests.test_live_runner", "tests.test_real_demo_script"],
     "node": ["tests/outcome_test.js"], "info": "Risk gate before MT5, unknown outcomes, no blind retries. FAKE bridge only."},
    {"id": "server", "title": "Headway-Demo server validation", "kind": "automated", "simulated_broker": True,
     "modules": ["tests.test_server_validation"], "info": "Expected server, fail-closed order guard, configuration precedence."},
    {"id": "history", "title": "Trade journal/history and persistence", "kind": "automated", "simulated_broker": True,
     "modules": ["tests.test_history"], "info": "Order journal, history view, local deletion sends nothing to MT5."},
    {"id": "routes", "title": "Flask routes and API validation", "kind": "automated", "simulated_broker": True,
     "modules": ["tests.test_web", "tests.test_startup", "tests.test_project_integrity", "tests.test_broker", "tests.test_testrunner"],
     "info": "Page shell, routes, startup, project integrity, adapter reads, test-runner safety."},
    {"id": "browser", "title": "Browser/UI tests", "kind": "browser", "simulated_broker": True,
     "modules": ["tests.test_browser_phase7a", "tests.test_browser_phase7a1", "tests.test_browser_clocks", "tests.test_browser_mtf", "tests.test_browser_strategylab", "tests.test_browser_commandcentre"], "needs": ["playwright"],
     "info": "Real headless Chromium. SKIPPED (not passed) when Playwright/Chromium is missing."},
]
GENERAL = {"id": "general", "title": "General regression suite", "kind": "automated", "simulated_broker": True,
           "info": "Every other test module, plus the P/L Node check."}
GENERAL_NODE = ["tests/pl_test.js"]

# Never run from the browser. Shown so nobody mistakes the simulated results for real-broker proof.
OPT_IN = [
    {"id": "real_demo", "title": "Real MT5 DEMO order (one order, left open)", "kind": "opt-in demo execution",
     "command": "TRADEMATE_REAL_DEMO_ORDER=YES_PLACE_ONE_DEMO_ORDER python -m trademate.broker.real_demo_execution_test EURUSD --place-real-demo-order",
     "why": "Places a real order on your connected MT5 DEMO account. Needs MT5 open, the EA running, and explicit opt-in. Terminal only."},
    {"id": "live_cycle", "title": "Real MT5 DEMO lifecycle (BUY/SELL, modify, close)", "kind": "opt-in demo execution",
     "command": "python -m trademate.broker.live_phase6_test EURUSD",
     "why": "Opens, modifies and closes real DEMO positions. Asks you to type YES. Terminal only."},
]

_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def discover_test_modules(tests_dir: Path | None = None) -> list[str]:
    tests_dir = tests_dir or PROJECT_ROOT / "tests"
    return sorted(f"tests.{p.stem}" for p in tests_dir.glob("test_*.py"))


def build_groups(registry: list[dict] | None = None, discover: list[str] | None = None, general: dict | None = GENERAL) -> list[dict]:
    """The allowlist, with the General group computed as every discovered module no other group claims."""
    groups = [dict(g) for g in (registry if registry is not None else GROUPS)]
    if general is not None:
        claimed = {m for g in groups for m in g.get("modules", [])}
        claimed_node = {n for g in groups for n in g.get("node", [])}
        rest = [m for m in (discover if discover is not None else discover_test_modules()) if m not in claimed]
        node_rest = [n for n in GENERAL_NODE if n not in claimed_node]
        groups.append({**general, "modules": rest, "node": node_rest})
    return groups


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tail(text: str) -> str:
    return text if len(text) <= MAX_LOG_CHARS else "... (earlier output omitted) ...\n" + text[-MAX_LOG_CHARS:]


def classify_group(summary: dict, exit_code: int | None, timed_out: bool, blocked: str | None) -> str:
    if blocked:
        return "BLOCKED"
    if timed_out or summary.get("failed") or summary.get("error"):
        return "FAIL"
    if exit_code not in (0, None) or (summary.get("passed", 0) + summary.get("skipped", 0) == 0):
        return "FAIL"                     # crashed, or ran nothing: never a pass
    if summary.get("skipped"):
        return "PARTIAL"
    return "PASS"


def overall_status(groups: list[dict], total_requested: int, total_available: int) -> tuple[str, str]:
    st = [g["status"] for g in groups]
    if "FAIL" in st:
        return "FAIL", "At least one test failed."
    ran = [s for s in st if s in ("PASS", "PARTIAL")]
    if not ran:
        return "BLOCKED", "No test group could run."
    reasons = []
    if "PARTIAL" in st:
        reasons.append("some tests were skipped (skipped tests did not run)")
    if "BLOCKED" in st:
        reasons.append("some groups could not run")
    if total_requested < total_available:
        reasons.append("only part of the suite was selected")
    if reasons:
        return "PARTIAL", "Not a complete pass: " + "; ".join(reasons) + "."
    return "PASS", "Every automated group ran and passed. This does not prove real MT5 order execution."


class RunManager:
    """One run at a time. State is held in memory and the last finished run is persisted (atomically) to disk."""

    def __init__(self, data_dir: Path, groups: list[dict] | None = None, python: str | None = None, cwd: Path | None = None,
                 timeout: int = GROUP_TIMEOUT_SECONDS):
        self.dir = Path(data_dir) / "test_runs"
        self.groups = groups if groups is not None else build_groups()
        self.by_id = {g["id"]: g for g in self.groups}
        self.python = python or sys.executable
        self.cwd = Path(cwd or PROJECT_ROOT)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._state: dict = {"running": False, "run_id": None}
        self._proc: subprocess.Popen | None = None
        self._cancel = False

    # ---------- what the page shows before anything runs ----------
    def catalog(self) -> dict:
        node = shutil.which("node") is not None
        pw = _has_playwright()
        out = []
        for g in self.groups:
            needs = []
            if g.get("node") and not node:
                needs.append("Node.js (`node`) is not installed: the Node checks of this group cannot run.")
            if "playwright" in g.get("needs", []) and not pw:
                needs.append("Playwright is not installed: `pip install playwright && playwright install chromium`. "
                             "These tests will be SKIPPED, not passed.")
            out.append({"id": g["id"], "title": g["title"], "kind": g["kind"], "info": g.get("info", ""),
                        "simulated_broker": g.get("simulated_broker", True), "modules": len(g.get("modules", [])),
                        "node_checks": len(g.get("node", [])), "warnings": needs})
        return {"groups": out, "opt_in": OPT_IN, "node_available": node, "playwright_available": pw,
                "disclaimer": "These tests use simulated brokers. Passing them does not prove that real MT5 order execution works. "
                              "Real demo-order tests are separate, opt-in and run from the terminal."}

    # ---------- state ----------
    def status(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state, default=str))

    def last(self) -> dict | None:
        path = self.dir / "last_run.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def start(self, ids) -> tuple[bool, str]:
        if ids == "all" or ids is None:
            ids = [g["id"] for g in self.groups]
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and _ID_RE.match(i) for i in ids):
            return False, "Choose test groups from the list."
        unknown = [i for i in ids if i not in self.by_id]
        if unknown:
            return False, "Unknown test group."                         # never echo browser input back
        ids = list(dict.fromkeys(ids))
        with self._lock:
            if self._state.get("running"):
                return False, "A test run is already in progress. Wait for it to finish."
            run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            self._cancel = False
            self._state = {"running": True, "run_id": run_id, "started": _now(), "finished": None, "overall": None,
                           "overall_note": "", "current": None, "complete_selection": len(ids) == len(self.groups),
                           "groups": [self._blank(self.by_id[i]) for i in ids]}
        threading.Thread(target=self._run, args=(run_id, ids), name="trademate-test-run", daemon=True).start()
        return True, run_id

    def cancel(self) -> bool:
        with self._lock:
            if not self._state.get("running"):
                return False
            self._cancel = True
            proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
        return True

    @staticmethod
    def _blank(g: dict) -> dict:
        return {"id": g["id"], "title": g["title"], "kind": g["kind"], "simulated_broker": g.get("simulated_broker", True),
                "status": "NOT RUN", "passed": 0, "failed": 0, "error": 0, "skipped": 0, "seconds": None, "tests": [],
                "checks": [], "log": "", "note": ""}

    def _set(self, gid: str, **fields) -> None:
        with self._lock:
            for g in self._state["groups"]:
                if g["id"] == gid:
                    g.update(fields)

    # ---------- the run ----------
    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items()}
        for name in ("TRADEMATE_REAL_DEMO_ORDER",):                 # a test run can never be authorised to place a real order
            env.pop(name, None)
        env.update(TRADEMATE_TEST_RUN="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
        return env

    def _run(self, run_id: str, ids: list[str]) -> None:
        try:
            for gid in ids:
                if self._cancel:
                    self._set(gid, status="NOT RUN", note="Cancelled before it started.")
                    continue
                with self._lock:
                    self._state["current"] = gid
                self._run_group(self.by_id[gid])
        except Exception:                                              # noqa: BLE001
            logger.exception("Test run crashed")
        finally:
            with self._lock:
                groups = self._state["groups"]
                overall, note = overall_status([g for g in groups if g["status"] != "NOT RUN"] or groups,
                                               len(groups), len(self.groups))
                if self._cancel:
                    overall, note = "PARTIAL", "The run was cancelled."
                self._state.update(running=False, current=None, finished=_now(), overall=overall, overall_note=note,
                                   total_groups=len(self.groups))
                snapshot = json.loads(json.dumps(self._state, default=str))
            self._persist(snapshot)

    def _run_group(self, g: dict) -> None:
        started = time.perf_counter()
        mods, nodes = g.get("modules", []), g.get("node", [])
        summary = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
        tests, checks, log, notes = [], [], [], []
        blocked, timed_out, exit_codes = None, False, []

        if mods:
            code, out, result, to = self._run_unittest(g["id"], mods)
            exit_codes.append(code)
            timed_out = timed_out or to
            log.append(out)
            if result:
                for k in summary:
                    summary[k] += result["summary"].get(k, 0)
                tests = result["tests"]
            elif not to:
                notes.append("The test process ended without a result file.")
        for script in nodes:
            if shutil.which("node") is None:
                blocked = blocked or "Node.js is not installed, so the JavaScript checks could not run."
                checks.append({"name": script, "status": "BLOCKED", "detail": "node not found"})
                continue
            code, out, to = self._run_node(script)
            timed_out = timed_out or to
            ok = code == 0 and not to
            summary["passed" if ok else "failed"] += 1
            checks.append({"name": script, "status": "PASS" if ok else "FAIL", "detail": out})
            log.append(f"$ node {script}\n{out}")
        if not mods and not nodes:
            notes.append("This group has no tests.")
        has_run = summary["passed"] + summary["skipped"] + summary["failed"] + summary["error"] > 0
        status = classify_group(summary, max([c for c in exit_codes if c is not None], default=0), timed_out,
                                blocked if not has_run else None)
        if blocked and has_run and status == "PASS":
            status = "PARTIAL"
            notes.append(blocked)
        if timed_out:
            notes.append(f"Timed out after {self.timeout} seconds.")
        if status == "PARTIAL" and summary["skipped"]:
            reasons = sorted({t["detail"] for t in tests if t["status"] == "skipped" and t["detail"]})
            notes.append(f"{summary['skipped']} test(s) skipped and NOT run: " + "; ".join(reasons[:3]))
        self._set(g["id"], status=status, tests=tests, checks=checks, log=_tail("\n".join(log)),
                  seconds=round(time.perf_counter() - started, 2), note=" ".join(notes), **summary)

    def _spawn(self, argv: list[str]) -> tuple[int | None, str, bool]:
        """Run argv with a timeout. Returns (exit code, combined output, timed_out). Output only: stdin is closed."""
        try:
            proc = subprocess.Popen(argv, cwd=self.cwd, env=self._env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, errors="replace")
        except OSError as exc:
            return None, f"Could not start the process: {exc}", False
        self._proc = proc
        timer = threading.Timer(self.timeout, proc.kill)
        timer.start()
        try:
            out, _ = proc.communicate()
        finally:
            timer.cancel()
            self._proc = None
        timed_out = proc.returncode in (-9, -15) and not self._cancel
        return proc.returncode, out or "", timed_out

    def _run_unittest(self, gid: str, mods: list[str]):
        fd, path = tempfile.mkstemp(prefix="trademate-test-", suffix=".json")
        os.close(fd)
        try:
            code, out, timed_out = self._spawn([self.python, "-m", "tests.json_runner", "--out", path, *mods])
            result = None
            try:
                with open(path, encoding="utf-8") as fh:
                    result = json.load(fh)
            except (OSError, ValueError):
                pass
            lines = [ln for ln in out.splitlines() if not ln.startswith("@@TEST ")]
            return code, "\n".join(lines), result, timed_out
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _run_node(self, script: str):
        code, out, timed_out = self._spawn(["node", script])
        return code, out.strip(), timed_out

    def _persist(self, snapshot: dict) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / "last_run.json.tmp"
            tmp.write_text(json.dumps(snapshot), encoding="utf-8")
            os.replace(tmp, self.dir / "last_run.json")
        except OSError:
            logger.exception("Could not save the last test run")


def _has_playwright() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False
