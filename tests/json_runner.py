"""Structured test runner for the Full Test dashboard. A thin layer on the standard `unittest`, not a new framework.

    python -m tests.json_runner --out result.json tests.test_structure_engine tests.test_analysis

Prints one line per finished test (`@@TEST <status> <id>`) so a parent process can show live progress, and writes the
complete per-test result (status, duration, failure text, skip reason) to --out as JSON when it ends. Exit status 0 only if
nothing failed or errored. Skipped tests are reported as skipped; they are never counted as passed.
"""

import argparse
import json
import os
import sys
import time
import traceback
import unittest

MAX_TEXT = 6000


class Recorder(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.rows = []
        self._t0 = {}

    def startTest(self, test):
        super().startTest(test)
        self._t0[test.id()] = time.perf_counter()

    def _add(self, test, status, text=""):
        took = time.perf_counter() - self._t0.pop(test.id(), time.perf_counter())
        self.rows.append({"id": test.id(), "status": status, "seconds": round(took, 4), "detail": text[:MAX_TEXT]})
        print(f"@@TEST {status} {test.id()}", flush=True)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._add(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._add(test, "failed", "".join(traceback.format_exception(*err)))

    def addError(self, test, err):
        super().addError(test, err)
        self._add(test, "error", "".join(traceback.format_exception(*err)))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._add(test, "skipped", str(reason))

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._add(test, "passed", "expected failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._add(test, "failed", "unexpected success")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("modules", nargs="+")
    args = ap.parse_args(argv)
    sys.path.insert(0, os.getcwd())
    suite, load_errors = unittest.TestSuite(), []
    for name in args.modules:
        try:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
        except Exception:  # noqa: BLE001 - an unloadable module is a failure to report, not a crash
            load_errors.append({"id": name, "status": "error", "seconds": 0, "detail": traceback.format_exc()[:MAX_TEXT]})
    rec = Recorder()
    started = time.perf_counter()
    suite.run(rec)
    rows = load_errors + rec.rows
    # a module that failed to IMPORT becomes a loader "_FailedTest": it is already recorded by the Recorder as an error
    summary = {s: sum(1 for r in rows if r["status"] == s) for s in ("passed", "failed", "error", "skipped")}
    result = {"modules": args.modules, "seconds": round(time.perf_counter() - started, 3), "summary": summary, "tests": rows}
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    os.replace(tmp, args.out)
    return 0 if not (summary["failed"] or summary["error"]) else 1


if __name__ == "__main__":
    sys.exit(main())
