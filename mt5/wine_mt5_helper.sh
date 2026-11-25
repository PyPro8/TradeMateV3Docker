#!/usr/bin/env bash
# =============================================================================
# OPTIONAL Wine/MT5 helper (Linux + Wine): MT5 discovery -> install + compile the EA -> start MT5 -> start TradeMate.
# Normally you do NOT run this file yourself: the one documented command is  ./launch.sh  (add --mt5 to use this helper).
#
#   ./launch.sh --mt5                  full start through this helper
#   mt5/wine_mt5_helper.sh --compile-only   discovery + install + compile, then stop
#                                    (use this to prove/diagnose the EA build)
#   mt5/wine_mt5_helper.sh --restart-mt5    also restart a running MT5, but ONLY if the
#                                    bridge proves there are no open positions
#   mt5/wine_mt5_helper.sh --no-browser     do not open Firefox
#   mt5/wine_mt5_helper.sh --mt5-only       start/verify MT5 + EA, leave web server to container
#
# This script is automation only. It never submits or closes a trade. It only
# restarts MT5 when --restart-mt5 is given AND zero open positions are proven.
# Everything is logged to logs/launcher/ and nothing is deleted.
#
# Overrides (only if auto-detection picks the wrong thing):
#   MT5_INSTALL_DIR=/path/to/"MetaTrader 5"   WINEPREFIX=/path/to/prefix
# =============================================================================
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

# Keep account secrets in shell memory only; do not pass them into Wine,
# MetaEditor, the terminal process environment or log helper processes.
MT5_LOGIN_VALUE="${MT5_LOGIN:-}"
MT5_PASSWORD_VALUE="${MT5_PASSWORD:-}"
MT5_SERVER_VALUE="${MT5_SERVER:-}"
export -n MT5_LOGIN MT5_PASSWORD MT5_SERVER 2>/dev/null || true

COMPILE_ONLY=0; RESTART_MT5=0; OPEN_BROWSER=1; MT5_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --compile-only) COMPILE_ONLY=1 ;;
    --restart-mt5)  RESTART_MT5=1 ;;
    --no-browser)   OPEN_BROWSER=0 ;;
    --mt5-only)     MT5_ONLY=1; OPEN_BROWSER=0 ;;
    -h|--help)      sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown option: $arg (see --help)"; exit 2 ;;
  esac
done

EA_NAME="TradeMateBridge"
EA_SRC="$PROJECT_DIR/mt5/$EA_NAME.mq5"
LOG_DIR="${TRADEMATE_LAUNCHER_LOG_DIR:-$PROJECT_DIR/logs/launcher}"
STAMP="$(date +%Y%m%d-%H%M%S)"
RUN_LOG="$LOG_DIR/launcher-$STAMP.log"
PID_FILE="$LOG_DIR/trademate.pid"
FLASK_LOG="$LOG_DIR/flask.log"
MT5_WINE_LOG="$LOG_DIR/mt5-wine.log"
STAGE_DIR="/tmp/trademate-build-$(id -u)"          # deliberately has no spaces

mkdir -p "$LOG_DIR"
exec > >(tee -a "$RUN_LOG") 2>&1
ln -sf "$RUN_LOG" "$LOG_DIR/latest.log"

# ----------------------------------------------------------------- helpers ---
say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
ok()   { printf '   [ OK ] %s\n' "$*"; }
warn() { printf '   [WARN] %s\n' "$*"; SUMMARY_WARN+=("$*"); }
die()  { printf '\n   [FAIL] %s\n\n   Full log: %s\n' "$*" "$RUN_LOG"; exit 1; }
SUMMARY_WARN=()

if [[ -n ${TRADEMATE_MT5_CREDENTIALS_FILE:-} ]]; then
  [[ -r $TRADEMATE_MT5_CREDENTIALS_FILE ]] || die "The temporary MT5 credential handoff file is missing or unreadable."
  mapfile -d '' -t _mt5_credentials < "$TRADEMATE_MT5_CREDENTIALS_FILE"
  [[ ${#_mt5_credentials[@]} -eq 3 ]] || die "The temporary MT5 credential handoff file is malformed."
  MT5_LOGIN_VALUE="${_mt5_credentials[0]}"
  MT5_PASSWORD_VALUE="${_mt5_credentials[1]}"
  MT5_SERVER_VALUE="${_mt5_credentials[2]}"
  unset _mt5_credentials
  rm -f "$TRADEMATE_MT5_CREDENTIALS_FILE"
  unset TRADEMATE_MT5_CREDENTIALS_FILE
fi

# MT5/MetaEditor write UTF-16 text. Print any such file as UTF-8.
decode_text() {
  local f=$1 h
  h=$(head -c2 "$f" | od -An -tx1 | tr -d ' \n')
  case "$h" in
    fffe|feff) iconv -f UTF-16 -t UTF-8 "$f" ;;
    ??00)      iconv -f UTF-16LE -t UTF-8 "$f" ;;
    *)         cat "$f" ;;
  esac
}

# ------------------------------------------------------------ TradeMate PID ---
stop_trademate() {
  step "1. Stop previous TradeMate (only if this launcher started it)"
  [[ -f $PID_FILE ]] || { ok "no pid file: nothing to stop"; return; }
  local pid; pid=$(<"$PID_FILE")
  if [[ $pid =~ ^[0-9]+$ ]] && [[ -d /proc/$pid ]] \
     && tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q 'app\.py' \
     && [[ $(readlink "/proc/$pid/cwd") == "$PROJECT_DIR" ]]; then
    kill -TERM "$pid"
    for _ in $(seq 1 20); do [[ -d /proc/$pid ]] || break; sleep 0.5; done
    [[ -d /proc/$pid ]] && kill -KILL "$pid"
    ok "stopped TradeMate pid $pid"
  else
    ok "pid file is stale (process gone or not TradeMate)"
  fi
  rm -f "$PID_FILE"
}

# ------------------------------------------------------------- dependencies ---
detect_dependencies() {
  step "2. Dependencies"
  local missing=()
  for c in wine winepath curl iconv timeout pgrep od find; do
    command -v "$c" >/dev/null 2>&1 || missing+=("$c")
  done
  ((${#missing[@]})) && die "Missing commands: ${missing[*]}"
  ok "wine: $(wine --version 2>/dev/null)  ($(command -v wine))"
  [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] || warn "No DISPLAY/WAYLAND_DISPLAY: MetaEditor and MT5 are GUI programs and may not start"

  PY=""
  for cand in "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/.venv/bin/python"; do
    [[ -x $cand ]] && { PY=$cand; break; }
  done
  if ((COMPILE_ONLY)); then
    [[ -n $PY ]] && ok "python: $PY" || ok "python venv not needed for --compile-only"
    return
  fi
  [[ -n $PY ]] || die "No virtual environment found (looked for venv/bin/python and .venv/bin/python)."
  "$PY" -c 'import flask' 2>/dev/null || die "Flask is not installed in $PY. Run: $PY -m pip install -r requirements.txt"
  ok "python: $PY"
  [[ -f $EA_SRC ]] || die "EA source not found: $EA_SRC"
  command -v firefox >/dev/null 2>&1 || { ((OPEN_BROWSER)) && warn "firefox not found; browser will not be opened"; OPEN_BROWSER=0; }
}

# ---------------------------------------------------------------- MT5 / Wine ---
# Sets MT5_PID, RUN_PREFIX, RUN_PORTABLE if a terminal64.exe process exists.
find_running_mt5() {
  MT5_PID=""; RUN_PREFIX=""; RUN_PORTABLE=0
  local pid comm cmd
  for pid in $(pgrep -f 'terminal64\.exe' 2>/dev/null); do
    [[ $pid == $$ ]] && continue
    comm=$(cat "/proc/$pid/comm" 2>/dev/null) || continue
    cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null) || continue
    # Wine names the real terminal process "main" and gives it a Windows command line
    # (C:\Program Files\MetaTrader 5\terminal64.exe /portable). "start.exe /exec <unix path>" is only
    # the helper that launched it, and bash/pgrep/tee lines merely mention the name, so skip those.
    [[ $cmd =~ ^[A-Za-z]:\\.*terminal64\.exe || $comm == terminal64* ]] || continue
    MT5_PID=$pid
    RUN_PREFIX=$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null | sed -n 's/^WINEPREFIX=//p' | head -1)
    [[ $cmd == *"/portable"* ]] && RUN_PORTABLE=1
    return 0
  done
  return 1
}

detect_mt5() {
  step "3. MT5 + MetaEditor (installation directory)"
  find_running_mt5 && ok "MT5 is already running (pid $MT5_PID${RUN_PREFIX:+, WINEPREFIX=$RUN_PREFIX}$( ((RUN_PORTABLE)) && echo ', /portable'))"

  local prefixes=() p exe
  [[ -n ${RUN_PREFIX:-} ]] && prefixes+=("$RUN_PREFIX")
  [[ -n ${WINEPREFIX:-} ]] && prefixes+=("$WINEPREFIX")
  prefixes+=("$HOME/.wine" "$HOME/.mt5")

  INSTALL_DIR=""
  if [[ -n ${MT5_INSTALL_DIR:-} ]]; then
    [[ -f $MT5_INSTALL_DIR/terminal64.exe ]] || die "MT5_INSTALL_DIR has no terminal64.exe: $MT5_INSTALL_DIR"
    INSTALL_DIR=${MT5_INSTALL_DIR%/}
    WINEPREFIX_DIR=${WINEPREFIX:-${INSTALL_DIR%%/drive_c/*}}
  else
    for p in "${prefixes[@]}"; do
      for exe in "$p"/drive_c/Program\ Files*/*MetaTrader*/terminal64.exe; do
        [[ -f $exe && -f $(dirname "$exe")/MetaEditor64.exe ]] || continue
        INSTALL_DIR=$(dirname "$exe"); WINEPREFIX_DIR=$p; break 2
      done
    done
  fi
  [[ -n $INSTALL_DIR ]] || die "Could not find terminal64.exe + MetaEditor64.exe in: ${prefixes[*]}"
  export WINEPREFIX="$WINEPREFIX_DIR"
  export WINEDEBUG="${WINEDEBUG:-fixme-all}"      # hides only Wine 'fixme' noise; errors stay visible
  TERMINAL_EXE="$INSTALL_DIR/terminal64.exe"
  METAEDITOR_EXE="$INSTALL_DIR/MetaEditor64.exe"
  ok "Wine prefix        : $WINEPREFIX_DIR"
  ok "MT5 install dir    : $INSTALL_DIR"
  ok "terminal64.exe     : present"
  ok "MetaEditor64.exe   : present"
}

# Candidate data dirs: (a) hashed AppData folders whose origin.txt names THIS
# install; (b) the install dir itself when it holds an MQL5 tree (portable mode).
find_mt5_data_dir() {
  step "4. MT5 data directory (not the same thing as the install dir)"
  local install_win origin d kind
  install_win=$(winepath -w "$INSTALL_DIR" 2>/dev/null) || die "winepath failed for $INSTALL_DIR"
  say "   install dir (Windows path): $install_win"

  local -a cand_dir=() cand_kind=()
  for d in "$WINEPREFIX_DIR"/drive_c/users/*/AppData/Roaming/MetaQuotes/Terminal/*/; do
    d=${d%/}; [[ -d $d && $(basename "$d") != Common ]] || continue
    if [[ -f $d/origin.txt ]]; then
      origin=$(decode_text "$d/origin.txt" | tr -d '\r\n\357\273\277')
      if [[ ${origin,,} == "${install_win,,}" ]]; then
        cand_dir+=("$d"); cand_kind+=("hashed"); say "   candidate (origin.txt matches install): $d"
      else
        say "   ignored   (origin.txt points to '$origin'): $d"
      fi
    else
      say "   ignored   (no origin.txt): $d"
    fi
  done
  if [[ -d $INSTALL_DIR/MQL5/Experts || -d $INSTALL_DIR/MQL5/Include ]]; then
    cand_dir+=("$INSTALL_DIR"); cand_kind+=("portable"); say "   candidate (install dir holds an MQL5 tree): $INSTALL_DIR"
  fi

  if ((${#cand_dir[@]} == 0)); then
    say "   MQL5 folders that exist anywhere in this prefix:"
    find "$WINEPREFIX_DIR/drive_c" -maxdepth 9 -type d -name MQL5 2>/dev/null | sed 's/^/     /'
    die "No MT5 data directory found for $INSTALL_DIR. Start MT5 once (so it creates its data folder), then re-run."
  fi

  local pick=-1 i best=0 t
  if [[ -n $MT5_PID ]]; then
    for i in "${!cand_dir[@]}"; do
      if { ((RUN_PORTABLE)) && [[ ${cand_kind[i]} == portable ]]; } || { ((!RUN_PORTABLE)) && [[ ${cand_kind[i]} == hashed ]]; }; then pick=$i; fi
    done
    ((pick >= 0)) && say "   chosen because the running terminal is $( ((RUN_PORTABLE)) && echo portable || echo 'non-portable (hashed folder)')"
  elif ((${#cand_dir[@]} == 1)); then
    pick=0
  else
    for i in "${!cand_dir[@]}"; do
      t=$(last_log_epoch "${cand_dir[i]}"); say "   last terminal log write: ${cand_dir[i]} -> $( ((t)) && date -d "@$t" '+%F %T' || echo none)"
      ((t > best)) && { best=$t; pick=$i; }
    done
    ((pick >= 0)) && say "   chosen: the one whose terminal log was written most recently"
  fi
  if ((pick < 0)) && [[ -n $MT5_PID ]]; then
    die "MT5 is running as $( ((RUN_PORTABLE)) && echo 'portable (/portable)' || echo 'non-portable') but no matching data directory was found among the candidates above."
  fi
  ((pick >= 0)) || die "Several MT5 data directories match and I cannot tell which one you use. Start MT5 the way you normally do, then re-run (a running terminal identifies its own data dir)."

  DATA_DIR=${cand_dir[pick]}
  PORTABLE=0; [[ ${cand_kind[pick]} == portable ]] && PORTABLE=1
  MQL5_DIR="$DATA_DIR/MQL5"
  EXPERTS_DIR="$MQL5_DIR/Experts"
  [[ -d $MQL5_DIR ]] || die "MQL5 folder missing in $DATA_DIR"
  mkdir -p "$EXPERTS_DIR"
  [[ -f $MQL5_DIR/Include/Trade/Trade.mqh ]] || die "$MQL5_DIR/Include/Trade/Trade.mqh is missing. The EA includes <Trade\\Trade.mqh>, so MetaEditor cannot compile until MT5 has initialised this data folder."

  ok "data directory     : $DATA_DIR  (${cand_kind[pick]})"
  ok "MQL5 directory     : $MQL5_DIR"
  ok "Experts directory  : $EXPERTS_DIR"
  if [[ -n $MT5_PID ]]; then
    t=$(last_log_epoch "$DATA_DIR")
    if ((t && $(date +%s) - t < 900)); then ok "running terminal is writing logs here (last write $(date -d "@$t" +%T))"
    else warn "MT5 is running but $DATA_DIR/logs has no recent writes: this may not be the folder it uses"; fi
  fi
}

last_log_epoch() {  # newest mtime of <dir>/logs/*.log, or 0
  local newest=0 f t
  for f in "$1"/logs/*.log; do
    [[ -f $f ]] || continue
    t=$(stat -c %Y "$f"); ((t > newest)) && newest=$t
  done
  echo "$newest"
}

# -------------------------------------------------------------- positions ---
# Reads the bridge snapshot with the project's own code. Prints key=value lines.
snapshot_info() {
  "$PY" - <<'PYEOF' 2>&1
import json, sys, time
from pathlib import Path
sys.path.insert(0, ".")
from trademate.config import load_config
from trademate.broker.mt5_adapter import find_common_files_dir
s = load_config()
d, _ = find_common_files_dir(s.mt5.data_dir)
if d is None:
    print("state=nofolder"); sys.exit(0)
p = Path(d) / s.mt5.snapshot_file
print(f"file={p}")
try:
    snap = json.loads(p.read_text(encoding="utf-8"))
    age = time.time() - p.stat().st_mtime
except Exception as exc:
    print(f"state=unreadable\nerror={exc}"); sys.exit(0)
print(f"state={'fresh' if age <= s.mt5.stale_after_seconds else 'stale'}")
print(f"age={age:.1f}")
b = snap.get("bridge", {}) if isinstance(snap, dict) else {}
print(f"bridge={b.get('name','?')}\nversion={b.get('version','?')}")
pos = snap.get("positions") or []
print(f"positions={len(pos)}")
for q in pos:
    print(f"pos=#{q.get('ticket')} {q.get('symbol')} {q.get('type')} {q.get('volume')} lots, P/L {q.get('profit')}")
PYEOF
}

check_positions() {
  step "6. Open positions (checked BEFORE anything is restarted)"
  POS_STATE=unknown; POS_COUNT=0
  if [[ -z $MT5_PID ]]; then
    say "   MT5 is not running, so positions cannot be read. This launcher will not close or change any."
    return
  fi
  local info; info=$(snapshot_info)
  local state; state=$(sed -n 's/^state=//p' <<<"$info")
  if [[ $state == fresh ]]; then
    POS_COUNT=$(sed -n 's/^positions=//p' <<<"$info")
    if ((POS_COUNT > 0)); then
      POS_STATE=open
      printf '\n   !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n'
      printf '   !!  %s OPEN POSITION(S) EXIST ON THE ACCOUNT\n' "$POS_COUNT"
      sed -n 's/^pos=/   !!    /p' <<<"$info"
      printf '   !!  The launcher will NOT close them and will NOT restart MT5.\n'
      printf '   !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n\n'
      SUMMARY_WARN+=("$POS_COUNT open position(s) exist; MT5 was left untouched")
    else
      POS_STATE=none; ok "bridge snapshot is fresh: 0 open positions"
    fi
  else
    warn "MT5 is running but the bridge snapshot is '$state': open positions are UNKNOWN. MT5 will not be restarted."
  fi
}

# ----------------------------------------------------------- EA install/compile ---
install_ea() {
  step "8. Install EA source into the active Experts directory"
  cp -f "$EA_SRC" "$EXPERTS_DIR/$EA_NAME.mq5" || die "Could not copy to $EXPERTS_DIR"
  cmp -s "$EA_SRC" "$EXPERTS_DIR/$EA_NAME.mq5" || die "Copy verification failed for $EXPERTS_DIR/$EA_NAME.mq5"
  ok "$EXPERTS_DIR/$EA_NAME.mq5 ($(stat -c %s "$EA_SRC") bytes) matches mt5/$EA_NAME.mq5"
}

# One MetaEditor run. Paths given to MetaEditor must be space-free, because the
# argument has to reach MetaEditor as  /compile:PATH  without quotes.
compile_attempt() {
  local label=$1 win_src=$2 win_log=$3 ex5=$4 unix_log=$5
  local wine_out="$LOG_DIR/compile-$STAMP-$label.wine.txt"
  local txt="$LOG_DIR/compile-$STAMP-$label.log"
  say "   -- attempt $label"
  say "      source : $win_src"
  say "      log    : $win_log"
  if [[ $win_src == *" "* || $win_log == *" "* ]]; then
    NOTES+=("attempt $label: skipped, Windows path contains a space"); return 1
  fi
  local marker; marker=$(mktemp "$LOG_DIR/.marker.XXXXXX")
  rm -f "$unix_log"
  local -a args=("/compile:$win_src" "/log:$win_log")
  ((PORTABLE)) && args=(/portable "${args[@]}")
  say "      command: (cd \"$INSTALL_DIR\" && wine MetaEditor64.exe ${args[*]})"
  say "      ---- MetaEditor/Wine output ----"
  ( cd "$INSTALL_DIR" && timeout 240 wine "$METAEDITOR_EXE" "${args[@]}" ) 2>&1 | tee "$wine_out"
  local rc=${PIPESTATUS[0]}
  say "      ---- exit code $rc ----"
  if ((rc == 124)); then pkill -f 'MetaEditor64\.exe' 2>/dev/null; fi   # only MetaEditor, never MT5

  # MetaEditor may finish writing a moment after the wine call returns.
  for _ in $(seq 1 30); do [[ -f $ex5 && $ex5 -nt $marker ]] && break; sleep 1; done

  local errors="" fresh=0
  [[ -f $ex5 && $ex5 -nt $marker ]] && fresh=1
  if [[ -f $unix_log ]]; then
    decode_text "$unix_log" >"$txt"
    say "      ---- compiler log ($txt) ----"; sed 's/^/      /' "$txt"
    errors=$(grep -Eio 'Result: *[0-9]+ errors?' "$txt" | grep -Eo '[0-9]+' | tail -1)
  fi

  if ((fresh)) && [[ -z $errors || $errors == 0 ]]; then
    ok "attempt $label produced a fresh $(basename "$ex5") ($(stat -c %s "$ex5") bytes)"
    rm -f "$marker"; return 0
  fi

  # ---- explain exactly why there is no .ex5 ----
  local why
  if ((rc == 124)); then
    why="MetaEditor did not exit within 240 s (it was killed). It probably treated the arguments as 'open the IDE' instead of 'compile'."
  elif [[ ! -f $unix_log ]]; then
    why="MetaEditor exited (code $rc) WITHOUT writing a compiler log and WITHOUT an .ex5: it never reached the compiler."
    local errs; errs=$(grep -E '^[0-9a-f]{4}:err:' "$wine_out" | grep -v 'start_rpcss' | head -5)
    [[ -n $errs ]] && why+=" Wine errors: $errs"
  elif [[ -n $errors && $errors != 0 ]]; then
    why="the compiler ran and reported $errors error(s):"$'\n'"$(grep -Ei 'error' "$txt" | grep -vi 'Result:' | head -15 | sed 's/^/        /')"
    grep -qiE 'cannot open|Trade\.mqh' "$txt" && why+=$'\n'"        -> an include file was not found: MetaEditor is using a different data folder than $DATA_DIR (portable vs non-portable mismatch)."
  else
    why="the compiler log shows no errors but no fresh .ex5 exists at $ex5."
    local found; found=$(find "$WINEPREFIX_DIR/drive_c" "$STAGE_DIR" -name "$EA_NAME.ex5" -newer "$marker" 2>/dev/null | head -3)
    [[ -n $found ]] && why+=" A fresh .ex5 WAS written elsewhere: $found"
  fi
  NOTES+=("attempt $label: $why")
  rm -f "$marker"; return 1
}

compile_ea() {
  step "9. Compile $EA_NAME.mq5 with MetaEditor (output is shown and logged)"
  NOTES=()
  rm -rf "$STAGE_DIR"; mkdir -p "$STAGE_DIR"
  cp -f "$EA_SRC" "$STAGE_DIR/$EA_NAME.mq5"
  local src_win log_win
  src_win=$(winepath -w "$STAGE_DIR/$EA_NAME.mq5"); log_win=$(winepath -w "$STAGE_DIR/compile.log")

  # Attempt A: build in a space-free scratch folder, then install the result.
  if compile_attempt A "$src_win" "$log_win" "$STAGE_DIR/$EA_NAME.ex5" "$STAGE_DIR/compile.log"; then
    cp -f "$STAGE_DIR/$EA_NAME.ex5" "$EXPERTS_DIR/$EA_NAME.ex5" || die "Could not copy .ex5 into $EXPERTS_DIR"
    EX5_HOW="compiled in scratch dir, installed to Experts"
  else
    # Attempt B: compile in place; 8.3 short names avoid the space in 'Program Files'.
    local short; short=$(winepath -s "$EXPERTS_DIR" 2>/dev/null)
    if [[ -n $short ]] && compile_attempt B "$short\\$EA_NAME.mq5" "$log_win" "$EXPERTS_DIR/$EA_NAME.ex5" "$STAGE_DIR/compile.log"; then
      EX5_HOW="compiled in place (8.3 path)"
    else
      step "COMPILATION FAILED: why there is no $EA_NAME.ex5"
      printf '   %s\n' "${NOTES[@]}"
      say ""
      say "   Raw outputs are in $LOG_DIR (compile-$STAMP-*.wine.txt / *.log)."
      die "MetaEditor did not produce $EA_NAME.ex5. Nothing was started."
    fi
  fi

  step "10. Verify the .ex5 in the active Experts directory"
  [[ -s $EXPERTS_DIR/$EA_NAME.ex5 ]] || die "$EXPERTS_DIR/$EA_NAME.ex5 is missing or empty"
  ok "$EXPERTS_DIR/$EA_NAME.ex5  ($(stat -c %s "$EXPERTS_DIR/$EA_NAME.ex5") bytes, $EX5_HOW)"
  ok "log kept: $LOG_DIR/compile-$STAMP-*.log"
}

# ----------------------------------------------------------------- MT5 start ---
# Is the EA already attached to a chart in the saved chart profile? (MT5 restores those on start.)
ea_in_saved_profile() {
  local f
  while IFS= read -r -d '' f; do
    decode_text "$f" 2>/dev/null | grep -q "^name=$EA_NAME" && return 0
  done < <(find "$DATA_DIR/Profiles/Charts" -name '*.chr' -print0 2>/dev/null)
  return 1
}

# MT5 startup config: opens one chart and attaches the EA. Optional account secrets are
# read from a one-use secret handoff, written to a mode-0600 temporary file, and redacted from logs.
write_startup_ini() {
  local attach_ea="$1"
  local dir="/tmp/trademate-mt5-$(id -u)"            # space-free on purpose
  mkdir -p "$dir"
  STARTUP_UNIX="$dir/startup.ini"
  umask 077
  {
    if [[ -n $MT5_LOGIN_VALUE || -n $MT5_PASSWORD_VALUE || -n $MT5_SERVER_VALUE ]]; then
      [[ $MT5_LOGIN_VALUE =~ ^[0-9]+$ ]] || die "MT5_LOGIN must contain only the numeric demo-account login."
      [[ -n $MT5_PASSWORD_VALUE && -n $MT5_SERVER_VALUE ]] || die "MT5_LOGIN, MT5_PASSWORD and MT5_SERVER must all be set together."
      [[ $MT5_PASSWORD_VALUE != *$'\n'* && $MT5_PASSWORD_VALUE != *$'\r'* && $MT5_SERVER_VALUE != *$'\n'* && $MT5_SERVER_VALUE != *$'\r'* ]] || die "MT5 credentials contain an invalid line break."
      printf '[Common]\nLogin=%s\nPassword=%s\nServer=%s\nKeepPrivate=1\n' "$MT5_LOGIN_VALUE" "$MT5_PASSWORD_VALUE" "$MT5_SERVER_VALUE"
      unset MT5_LOGIN_VALUE MT5_PASSWORD_VALUE MT5_SERVER_VALUE
    fi
    if [[ $attach_ea == 1 ]]; then
      printf '[StartUp]\nExpert=%s\nSymbol=%s\nPeriod=%s\n' "$EA_NAME" "${TM_CHART_SYMBOL:-EURUSD}" "${TM_CHART_PERIOD:-H1}"
    fi
    printf '[Experts]\nAllowLiveTrading=1\nAllowDllImport=0\nEnabled=1\n'
  } | sed 's/$/\r/' >"$STARTUP_UNIX"
  chmod 600 "$STARTUP_UNIX"
  STARTUP_WIN=$(winepath -w "$STARTUP_UNIX")
  say "   startup config generated (credentials redacted); EA=${EA_NAME}, chart=${TM_CHART_SYMBOL:-EURUSD}/${TM_CHART_PERIOD:-H1}"
}

# Print the evidence for "MT5 did not stay up": Wine's own output, what is running, MT5's log.
mt5_failure_report() {
  say ""
  say "   ---- last 40 lines of $MT5_WINE_LOG (what Wine printed for terminal64.exe) ----"
  tail -40 "$MT5_WINE_LOG" 2>/dev/null | sed 's/^/   /'
  say "   ---- Wine/MT5 processes right now ----"
  ps -eo pid,etime,comm,args 2>/dev/null | grep -Ei 'wine|terminal64|metaeditor|services\.exe|explorer\.exe' | grep -v grep | cut -c1-200 | sed 's/^/   /'
  local f; f=$(ls -t "$DATA_DIR"/logs/*.log 2>/dev/null | head -1)
  if [[ -n $f ]]; then
    say "   ---- last 15 lines of MT5's own log: $f ----"
    decode_text "$f" | tail -15 | sed 's/^/   /'
  fi
}

start_mt5() {
  step "12. MT5 terminal"
  if [[ -n $MT5_PID ]]; then
    if ((RESTART_MT5)); then
      [[ $POS_STATE == none ]] || die "Refusing --restart-mt5: open positions exist or their state is unknown (state=$POS_STATE). Close MT5 yourself when you are ready."
      say "   restarting MT5 (0 open positions proven)"
      kill -TERM "$MT5_PID"
      for _ in $(seq 1 30); do [[ -d /proc/$MT5_PID ]] || break; sleep 1; done
      [[ -d /proc/$MT5_PID ]] && die "MT5 did not exit after SIGTERM; not forcing it. Close it manually."
      MT5_PID=""
    else
      ok "MT5 already running (pid $MT5_PID): left untouched"
      say "   Note: a running MT5 keeps the EA it already loaded and no chart is opened for it. To get the new EA attached automatically, close MT5 when no positions are open and re-run, or use --restart-mt5."
      return
    fi
  fi
  local -a args=(); ((PORTABLE)) && args=(/portable)
  if ea_in_saved_profile; then
    ok "$EA_NAME is already attached in the saved chart profile: MT5 will restore it (no second copy added)"
    write_startup_ini 0 && args+=("/config:$STARTUP_WIN")
  else
    say "   $EA_NAME is not in the saved chart profile: opening a chart with it attached"
    write_startup_ini 1 && args+=("/config:$STARTUP_WIN")
  fi
  # Credentials were unexported before any Wine process was launched and are
  # consumed into the mode-0600 startup INI above.
  ( cd "$INSTALL_DIR" && nohup wine "$TERMINAL_EXE" "${args[@]}" >>"$MT5_WINE_LOG" 2>&1 & )
  step "13. Wait for MT5 to be running"
  for _ in $(seq 1 "${MT5_WAIT:-60}"); do find_running_mt5 && break; sleep 1; done
  [[ -n $MT5_PID ]] || { mt5_failure_report; die "MT5 did not appear as a process within ${MT5_WAIT:-60} s."; }
  sleep 5
  find_running_mt5 || { mt5_failure_report; die "MT5 started and then exited within 5 s of launching."; }
  ok "MT5 running (pid $MT5_PID)"
  # The terminal reads /config during startup. Remove the temporary plaintext
  # credential file after the process is established; MT5 keeps its own protected account store.
  if [[ -n ${STARTUP_UNIX:-} && -f ${STARTUP_UNIX:-} ]]; then
    rm -f "$STARTUP_UNIX"
    ok "temporary startup configuration removed"
  fi
}

verify_bridge() {
  step "14. Bridge (TradeMateBridge EA inside MT5)"
  local want have info state
  want=$(sed -n 's/^#define BRIDGE_VERSION *"\(.*\)"/\1/p' "$EA_SRC")
  for _ in $(seq 1 60); do
    info=$(snapshot_info); state=$(sed -n 's/^state=//p' <<<"$info")
    [[ $state == fresh ]] && break
    sleep 2
  done
  if [[ $state == fresh ]]; then
    have=$(sed -n 's/^version=//p' <<<"$info")
    ok "bridge is writing snapshots (age $(sed -n 's/^age=//p' <<<"$info")s, EA version $have)"
    [[ $have == "$want" ]] || warn "Running EA reports version $have but the compiled source is $want: MT5 is still using the old EA. Re-attach it (or use --restart-mt5 with no open positions)."
    BRIDGE_OK=1
  else
    warn "No fresh bridge snapshot after 2 min (state=$state). MT5 is up but the EA is not writing: the EA did not attach. Check the Experts tab in MT5 for the reason, or attach TradeMateBridge to one chart by hand (README, step 3)."
    BRIDGE_OK=0
  fi
}

# --------------------------------------------------------------- Flask / UI ---
start_trademate() {
  step "15. Start TradeMate (Flask)"
  read -r HOST PORT < <("$PY" -c 'from trademate.config import load_config as l; s=l(); print(s.ui.host, s.ui.port)') \
    || die "Could not read [ui] host/port from config/settings.toml"
  URL="http://$HOST:$PORT"
  if curl -s -o /dev/null --max-time 2 "$URL/"; then
    die "Something other than this launcher's TradeMate already answers on $URL. Stop it first."
  fi
  nohup "$PY" app.py >>"$FLASK_LOG" 2>&1 &
  FLASK_PID=$!; disown "$FLASK_PID"
  echo "$FLASK_PID" >"$PID_FILE"
  for _ in $(seq 1 30); do
    [[ -d /proc/$FLASK_PID ]] || { tail -20 "$FLASK_LOG"; die "TradeMate exited during startup (log above: $FLASK_LOG)"; }
    curl -s -o /dev/null --max-time 2 "$URL/" && break
    sleep 1
  done
  ok "pid $FLASK_PID, log $FLASK_LOG"
}

verify_trademate() {
  step "16-17. TradeMate responds, and TradeMate <-> bridge <-> MT5 works"
  curl -fsS --max-time 5 "$URL/" >/dev/null || die "TradeMate does not answer on $URL"
  ok "web UI answers on $URL"
  local out; out=$(curl -fsS --max-time 10 "$URL/api/broker" | "$PY" -c '
import json, sys
d = json.load(sys.stdin); st = d["status"]; a = d.get("account") or {}
print("state=" + st["state"]); print("message=" + str(st.get("message", "")))
print("account=%s %s %s" % (a.get("login"), a.get("server"), a.get("environment")))
print("positions=%d" % len(d.get("positions", [])))
syms = d.get("symbols", [])
print("symbol=" + (syms[0]["name"] if syms else ""))') || { warn "/api/broker did not return valid data"; return; }
  local st; st=$(sed -n 's/^state=//p' <<<"$out")
  if [[ $st != connected && $st != CONNECTED ]]; then
    warn "TradeMate reports bridge state '$st': $(sed -n 's/^message=//p' <<<"$out")"; return
  fi
  ok "read path: CONNECTED, account $(sed -n 's/^account=//p' <<<"$out"), $(sed -n 's/^positions=//p' <<<"$out") open position(s)"
  local sym; sym=$(sed -n 's/^symbol=//p' <<<"$out")
  [[ -n $sym ]] || { warn "no symbol in snapshot; command channel not tested"; return; }
  local n; n=$(curl -fsS --max-time 20 "$URL/api/candles?symbol=$sym&timeframe=H1&count=10" | "$PY" -c '
import json, sys
d = json.load(sys.stdin); print(len(d["candles"]) if d.get("ok") else -1)')
  if [[ $n =~ ^[0-9]+$ ]] && ((n > 0)); then ok "command path: TradeMate -> EA -> MT5 -> broker answered ($n $sym candles; read-only request, no trade)"
  else warn "command channel test failed for $sym (candles request got no answer from the EA)"; fi
}

open_browser() {
  step "18. Browser"
  ((OPEN_BROWSER)) || { say "   skipped"; return; }
  nohup firefox "$URL" >/dev/null 2>&1 &
  disown
  ok "Firefox opened to $URL"
}

# --------------------------------------------------------------------- main ---
say "TradeMate launcher  $(date '+%F %T')   log: $RUN_LOG"
BRIDGE_OK=0; MT5_PID=""; POS_STATE=unknown

((COMPILE_ONLY)) || stop_trademate
detect_dependencies
detect_mt5
find_mt5_data_dir
check_positions
install_ea
compile_ea
if ((COMPILE_ONLY)); then
  step "DONE (--compile-only): compile succeeded. MT5 and TradeMate were not touched."
  exit 0
fi
start_mt5
verify_bridge
if ((!MT5_ONLY)); then
  start_trademate
  verify_trademate
  open_browser
else
  step "Docker mode: MT5 only; production WSGI server is started by the container entrypoint"
fi

step "SUMMARY"
if ((MT5_ONLY)); then
  if ((${#SUMMARY_WARN[@]} == 0)); then say "   MT5 + TradeMateBridge verified. The container entrypoint will now start the production web server."
  else say "   MT5 started with warnings:"; printf '   - %s\n' "${SUMMARY_WARN[@]}"; fi
else
  if ((${#SUMMARY_WARN[@]} == 0)); then say "   Everything verified. TradeMate: $URL"
  else say "   Started, with warnings:"; printf '   - %s\n' "${SUMMARY_WARN[@]}"; fi
fi
say "   Logs: $LOG_DIR"
((${#SUMMARY_WARN[@]} == 0))