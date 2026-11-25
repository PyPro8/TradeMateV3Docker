#!/usr/bin/env bash
# =============================================================================
# TradeMate launcher. The ONE documented way to start the application:
#
#     ./launch.sh
#
#   ./launch.sh --check        run every check (Python, virtual environment, dependencies, configuration, port) and stop
#   ./launch.sh --no-browser   do not open the web page
#   ./launch.sh --mt5          Linux + Wine only: also discover MT5, install/compile the EA and start MT5 first (uses mt5/wine_mt5_helper.sh)
#   ./launch.sh --help
#
# What it does: finds the project root from its own location, uses the project's virtual environment (venv/ or .venv/; it creates .venv/ only when none exists),
# checks the dependencies and config/settings.toml, checks the port is free, then runs `python app.py` in the foreground (Ctrl+C stops TradeMate).
# It never edits configuration, never deletes or overwrites data, never trades and never closes a position.
# Overrides: PYTHON=/path/to/python3 chooses the interpreter used to create .venv/. The host and port are [ui] host / port in config/settings.toml (docs/CONFIGURATION.md).
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || { echo "ERROR: cannot enter the project folder $ROOT" >&2; exit 1; }

CHECK_ONLY=0; OPEN_BROWSER=1; USE_MT5=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --no-browser) OPEN_BROWSER=0 ;;
    --mt5) USE_MT5=1 ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "ERROR: unknown option '$arg'. Run ./launch.sh --help" >&2; exit 2 ;;
  esac
done

ok()   { printf '  [ OK ] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
fail() { printf '\n  [FAIL] %s\n' "$1" >&2; shift; for line in "$@"; do printf '         %s\n' "$line" >&2; done; exit 1; }

echo "TradeMate launcher (project: $ROOT)"

# ---- 1. Python ---------------------------------------------------------------
BASE_PY="${PYTHON:-}"
if [[ -z $BASE_PY ]]; then
  for c in python3 python; do command -v "$c" >/dev/null 2>&1 && { BASE_PY="$c"; break; }; done
fi
[[ -n $BASE_PY ]] || fail "Python 3.11 or newer was not found." "Install it (for example: sudo apt install python3 python3-venv) and run ./launch.sh again."
"$BASE_PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null \
  || fail "Python 3.11 or newer is required, but $("$BASE_PY" --version 2>&1) was found ($BASE_PY)." "Install a newer Python or point PYTHON=/path/to/python3.12 at one."
ok "Python: $("$BASE_PY" --version 2>&1) ($BASE_PY)"

# ---- 2. Virtual environment --------------------------------------------------
PY=""
for v in venv .venv; do
  if [[ -x $ROOT/$v/bin/python ]]; then PY="$ROOT/$v/bin/python"; ok "virtual environment: $v/"; break; fi
done
if [[ -z $PY ]]; then
  if ((CHECK_ONLY)); then
    warn "no virtual environment (venv/ or .venv/) yet: a normal start would create .venv/ and install requirements.txt (needs internet)"
    PY="$BASE_PY"
  else
    echo "  ... no virtual environment found: creating .venv/ (nothing existing is touched)"
    "$BASE_PY" -m venv "$ROOT/.venv" || fail "Could not create .venv/." "On Debian/Ubuntu install the venv module: sudo apt install python3-venv"
    PY="$ROOT/.venv/bin/python"
    ok "virtual environment created: .venv/"
  fi
fi

# ---- 3. Dependencies ---------------------------------------------------------
missing=$("$PY" - <<'PYEOF' 2>/dev/null
import importlib.util, re, sys
from pathlib import Path
need = []
for line in Path("requirements.txt").read_text().splitlines():
    line = line.split("#")[0].strip()
    if not line or "sys_platform" in line:
        continue
    name = re.split(r"[<>=!~\[; ]", line)[0]
    mod = {"Flask": "flask"}.get(name, name.replace("-", "_").lower())
    if importlib.util.find_spec(mod) is None:
        need.append(name)
print(" ".join(need))
PYEOF
)
if [[ -n $missing ]]; then
  if ((CHECK_ONLY)); then
    warn "missing Python packages: $missing  (install with: $PY -m pip install -r requirements.txt)"
  else
    echo "  ... installing the missing packages from requirements.txt ($missing)"
    "$PY" -m pip install -r requirements.txt >/dev/null 2>&1 \
      || fail "Could not install the dependencies ($missing)." "Run it yourself to see the error:  $PY -m pip install -r requirements.txt" "(an internet connection is needed the first time)"
    ok "dependencies installed"
  fi
else
  ok "dependencies: all present"
fi

# ---- 4. Configuration (read only) --------------------------------------------
[[ -f $ROOT/config/settings.toml ]] || fail "config/settings.toml is missing." "Restore it from your backup or from the original download. The launcher never creates or edits it."
[[ -f $ROOT/app.py ]] || fail "app.py is missing from $ROOT." "This does not look like a complete TradeMate folder."
cfg=$("$PY" - <<'PYEOF' 2>&1
import sys
sys.path.insert(0, ".")
try:
    from trademate.config import load_config
    s = load_config()
    print(f"{s.ui.host} {s.ui.port} {s.app.data_dir}")
except Exception as exc:                       # noqa: BLE001
    print("ERROR " + str(exc))
    sys.exit(3)
PYEOF
) || fail "The configuration could not be loaded:" "${cfg#ERROR }" "Fix config/settings.toml (or the TRADEMATE_* environment variable named above) and run ./launch.sh again."
read -r HOST PORT DATA_DIR <<<"$cfg"
ok "configuration loaded: config/settings.toml (web interface on http://$HOST:$PORT)"
mkdir -p "$DATA_DIR" 2>/dev/null
[[ -w $DATA_DIR ]] && ok "data folder writable: $DATA_DIR (existing data is never deleted)" || fail "The data folder is not writable: $DATA_DIR" "Fix its permissions or set [app] data_dir in config/settings.toml."

# ---- 5. Port ----------------------------------------------------------------
if "$PY" - "$HOST" "$PORT" <<'PYEOF' 2>/dev/null
import socket, sys
s = socket.socket()
s.settimeout(1)
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
except OSError:
    sys.exit(1)
sys.exit(0)
PYEOF
then
  fail "Something is already listening on $HOST:$PORT." "If it is TradeMate, it is already running: open http://$HOST:$PORT" "Otherwise choose another port: set [ui] port in config/settings.toml and run ./launch.sh again."
fi
ok "port $PORT is free"

((CHECK_ONLY)) && { echo; echo "All checks done. Start TradeMate with: ./launch.sh"; exit 0; }

# ---- 6. Start ----------------------------------------------------------------
if ((USE_MT5)); then
  [[ -x $ROOT/mt5/wine_mt5_helper.sh ]] || fail "mt5/wine_mt5_helper.sh is missing or not executable."
  echo "  ... handing over to the MT5 helper (it starts MT5 and TradeMate)"
  exec "$ROOT/mt5/wine_mt5_helper.sh" $( ((OPEN_BROWSER)) || echo --no-browser )
fi
URL="http://$HOST:$PORT"
if ((OPEN_BROWSER)) && command -v xdg-open >/dev/null 2>&1 && [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]]; then
  ( sleep 3; xdg-open "$URL" >/dev/null 2>&1 ) &
fi
echo
echo "Starting TradeMate at $URL   (Ctrl+C to stop)"
exec "$PY" app.py
