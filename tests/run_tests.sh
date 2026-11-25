#!/usr/bin/env bash
# TradeMate master test runner.
#
#   bash tests/run_tests.sh              full automated regression suite (about 15 minutes with the browser tests; prints passed / failed / skipped separately)
#   bash tests/run_tests.sh --phase7a    only the Phase 7A tests (market structure, candle colours, chart wiring)
#   bash tests/run_tests.sh --phase7a1   only the Phase 7A.1 tests (analysis, stop-loss calculator, server check, Full Test)
#   bash tests/run_tests.sh --no-browser skip the real-browser tests (they also skip themselves if Chromium is missing)
#
# Safe: the automated suite uses a fake bridge and stub brokers. It NEVER sends an order to MT5. The real-MT5
# scripts (trademate.broker.live_phase6_test, real_demo_execution_test) are NOT run here; they need explicit opt-in.
#
# Python: $PYTHON if set, else the project's virtual environment (venv/ or .venv/, or the one that is activated), else python3.
# Node.js is optional: without it the JavaScript checks are reported as NOT RUN (never as passed, and not as a failure).
set -u
cd "$(dirname "$0")/.." || exit 2

pick_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  for cand in "${VIRTUAL_ENV:-/nonexistent}/bin/python" "venv/bin/python" ".venv/bin/python" "venv/Scripts/python.exe" ".venv/Scripts/python.exe"; do
    [ -x "$cand" ] && { echo "$cand"; return; }
  done
  command -v python3 || command -v python
}
PY="$(pick_python)"
if [ -z "$PY" ]; then echo "No Python found. Activate the project's virtual environment first." >&2; exit 2; fi
if ! "$PY" -c "import flask" >/dev/null 2>&1; then
  echo "The Python at '$PY' cannot import Flask. Activate the project's virtual environment (or set PYTHON=...) and run again." >&2
  exit 2
fi

MODE=full; BROWSER=1
for arg in "$@"; do
  case "$arg" in
    --phase7a) MODE=phase7a ;;
    --phase7a1) MODE=phase7a1 ;;
    --no-browser) BROWSER=0 ;;
    -h|--help) sed -n 2,13p "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

FAILED=0
NODE_RAN=1
step() { echo; echo "=== $1"; }
echo "Using Python: $PY ($("$PY" --version 2>&1))"

run_node_checks() {
  if ! command -v node >/dev/null 2>&1; then
    echo "NOT RUN: Node.js is not installed, so the JavaScript checks did not run (install Node.js to run them)."
    NODE_RAN=0
    return
  fi
  for f in "$@"; do
    [ -f "$f" ] || { echo "MISSING: $f"; FAILED=1; continue; }
    node "$f" || FAILED=1
  done
}

step "Compile check"
"$PY" -m compileall -q . >/dev/null || { echo "compileall FAILED"; FAILED=1; }

PHASE7A_MODULES="tests.test_structure_engine tests.test_structure_api tests.test_chart_settings tests.test_chart_style_js"
PHASE7A1_MODULES="tests.test_structure_regressions tests.test_analysis tests.test_sl_risk_first tests.test_sl_price_first tests.test_sl_instruments tests.test_server_validation tests.test_testrunner tests.test_slcalc_js tests.test_command_expiry"
if [ "$BROWSER" = 1 ]; then
  PHASE7A_MODULES="$PHASE7A_MODULES tests.test_browser_phase7a"
  PHASE7A1_MODULES="$PHASE7A1_MODULES tests.test_browser_phase7a1"
fi

if [ "$MODE" = phase7a ]; then
  step "Phase 7A unit + integration tests"
  # shellcheck disable=SC2086
  "$PY" -m unittest $PHASE7A_MODULES -v || FAILED=1
  step "Phase 7A JavaScript checks (Node)"
  run_node_checks tests/chart_style_test.js
elif [ "$MODE" = phase7a1 ]; then
  step "Phase 7A.1 unit + integration tests"
  # shellcheck disable=SC2086
  "$PY" -m unittest $PHASE7A1_MODULES -v || FAILED=1
  step "Phase 7A.1 JavaScript checks (Node)"
  run_node_checks tests/slcalc_test.js tests/outcome_test.js
else
  step "Python test suite: every test module in its OWN process (the application keeps persistent safety state, so one module can never affect another). About 15 minutes with the browser tests."
  TOTAL=0; BAD=0; SKIP=0; FAILED_MODULES=""
  for f in tests/test_*.py; do
    mod="$(echo "${f%.py}" | tr / .)"
    if [ "$BROWSER" = 0 ]; then case "$mod" in tests.test_browser_*) continue ;; esac; fi
    out="$("$PY" -m unittest "$mod" 2>&1 | grep -av 'HTTP/1.1')"
    ran="$(echo "$out" | grep -aoE '^Ran [0-9]+' | tail -1 | grep -oE '[0-9]+')"; ran="${ran:-0}"
    res="$(echo "$out" | grep -aE '^(OK|FAILED)' | tail -1)"
    nbad=0; for k in failures errors; do v="$(echo "$res" | grep -oE "$k=[0-9]+" | grep -oE '[0-9]+')"; nbad=$((nbad + ${v:-0})); done
    nskip="$(echo "$res" | grep -oE 'skipped=[0-9]+' | grep -oE '[0-9]+')"; nskip="${nskip:-0}"
    TOTAL=$((TOTAL + ran)); BAD=$((BAD + nbad)); SKIP=$((SKIP + nskip))
    if [ -z "$res" ] || [ "$nbad" != 0 ]; then
      FAILED=1; FAILED_MODULES="$FAILED_MODULES $mod"
      echo "  FAILED  $mod  (${res:-no result})"; echo "$out" | grep -aE '^(FAIL|ERROR): ' | sed 's/ (.*//; s/^/            /'
    else
      echo "  ok      $mod  ($ran tests${nskip:+, $nskip skipped})"
    fi
  done
  echo
  echo "PYTHON TESTS: ran $TOTAL | passed $((TOTAL - BAD - SKIP)) | failed $BAD | skipped $SKIP (skipped tests did NOT run)"
  [ -n "$FAILED_MODULES" ] && echo "Modules with failures:$FAILED_MODULES"
  step "JavaScript checks (Node)"
  run_node_checks tests/pl_test.js tests/outcome_test.js tests/chart_style_test.js tests/slcalc_test.js tests/clock_test.js tests/labgreeting_test.js tests/candlemomentum_test.js
fi

echo
if [ "$FAILED" != 0 ]; then echo "SOME CHECKS FAILED."
elif [ "$NODE_RAN" = 0 ]; then echo "AUTOMATED CHECKS PASSED, but the Node.js checks did NOT run (see above). Skipped tests did NOT run either."
else echo "ALL AUTOMATED CHECKS PASSED (see 'skipped' counts above: skipped tests did NOT run)."; fi
exit "$FAILED"
