#!/usr/bin/env bash
set -Eeuo pipefail
cd /app

export WINEPREFIX DISPLAY WINEDEBUG="${WINEDEBUG:-fixme-all}"
export MT5_INSTALL_DIR="${MT5_INSTALL_DIR:-$WINEPREFIX/drive_c/MT5}"
export TRADEMATE_LAUNCHER_LOG_DIR="${TRADEMATE_LAUNCHER_LOG_DIR:-/var/data/logs/launcher}"

# Load the one-use credential handoff into shell memory, not the environment.
[[ -r ${TRADEMATE_MT5_CREDENTIALS_FILE:-} ]] || { echo "FATAL: MT5 credential handoff file unavailable."; exit 1; }
mapfile -d '' -t _creds < "$TRADEMATE_MT5_CREDENTIALS_FILE"
[[ ${#_creds[@]} -eq 3 ]] || { echo "FATAL: MT5 credential handoff file malformed."; exit 1; }
MT5_LOGIN_VALUE="${_creds[0]}"
MT5_PASSWORD_VALUE="${_creds[1]}"
MT5_SERVER_VALUE="${_creds[2]}"
unset _creds
rm -f "$TRADEMATE_MT5_CREDENTIALS_FILE"
unset TRADEMATE_MT5_CREDENTIALS_FILE

# Wine's GUI requirements are satisfied by a private virtual display. No VNC
# or remote desktop port is exposed publicly.
if ! pgrep -f 'Xvfb :99' >/dev/null 2>&1; then
  Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp -ac \
    >"${TRADEMATE_LOG_DIR:-/var/data/logs}/xvfb.log" 2>&1 &
  XVFB_PID=$!
  for _ in $(seq 1 30); do
    kill -0 "$XVFB_PID" 2>/dev/null || { echo "FATAL: Xvfb exited."; exit 1; }
    [[ -S /tmp/.X11-unix/X99 ]] && break
    sleep 1
  done
fi

mkdir -p "$WINEPREFIX"
TERMINAL="$MT5_INSTALL_DIR/terminal64.exe"
if [[ ! -s "$TERMINAL" ]]; then
  echo "[MT5] Initialising persistent Wine prefix..."
  wineboot --init
  installer="/tmp/mt5setup.exe"
  setup_url="${MT5_SETUP_URL:-https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe}"
  echo "[MT5] Downloading MT5 installer..."
  curl --fail --location --retry 3 --retry-delay 2 "$setup_url" -o "$installer"
  echo "[MT5] Installing terminal unattended into persistent storage..."
  set +e
  timeout "${MT5_INSTALL_TIMEOUT:-900}" env -u MT5_LOGIN -u MT5_PASSWORD -u MT5_SERVER \
    wine "$installer" /auto '/path:C:\MT5' \
    >"${TRADEMATE_LOG_DIR:-/var/data/logs}/mt5-install.log" 2>&1
  install_rc=$?
  set -e
  rm -f "$installer"
  if [[ ! -s "$TERMINAL" ]]; then
    echo "FATAL: MT5 installer did not create terminal64.exe (exit $install_rc)."
    tail -80 "${TRADEMATE_LOG_DIR:-/var/data/logs}/mt5-install.log" || true
    exit 1
  fi
  # A fresh prefix has no account or positions. Stop any installer-launched
  # terminal so the bridge helper can attach the EA with the secret-backed INI.
  wineserver -k || true
  wineserver -w || true
  sleep 2
  echo "[MT5] Installer completed."
fi

# Initialise the portable data tree once, ensuring the helper has a durable
# MQL5/Experts directory. No credentials are present in this first-run process.
if [[ ! -d "$MT5_INSTALL_DIR/MQL5" ]]; then
  echo "[MT5] Initialising portable terminal data tree..."
  ( cd "$MT5_INSTALL_DIR" && nohup wine "$TERMINAL" /portable >>"${TRADEMATE_LOG_DIR:-/var/data/logs}/mt5-initialize.log" 2>&1 & )
  for _ in $(seq 1 30); do
    [[ -d "$MT5_INSTALL_DIR/MQL5" ]] && break
    sleep 1
  done
  wineserver -k || true
  wineserver -w || true
  [[ -d "$MT5_INSTALL_DIR/MQL5" ]] || { echo "FATAL: MT5 did not create its MQL5 data directory."; exit 1; }
fi

backoff=30
while true; do
  # Give each helper attempt a fresh one-use file. The helper consumes it and
  # writes a mode-0600 temporary MT5 startup INI; no Wine process inherits it.
  creds_file="$(mktemp /tmp/trademate-mt5-credentials.XXXXXX)"
  umask 077
  printf '%s\0%s\0%s\0' "$MT5_LOGIN_VALUE" "$MT5_PASSWORD_VALUE" "$MT5_SERVER_VALUE" > "$creds_file"
  chmod 600 "$creds_file"
  export TRADEMATE_MT5_CREDENTIALS_FILE="$creds_file"

  echo "[MT5] Running TradeMateBridge install/compile/start helper..."
  set +e
  ./mt5/wine_mt5_helper.sh --mt5-only --no-browser
  rc=$?
  set -e
  rm -f "$creds_file"
  unset TRADEMATE_MT5_CREDENTIALS_FILE

  if [[ $rc -ne 0 ]]; then
    echo "[MT5] Helper failed with exit $rc; retrying in ${backoff}s. Check persistent launcher logs."
    sleep "$backoff"
    if ((backoff < 300)); then backoff=$((backoff * 2)); fi
    if ((backoff > 300)); then backoff=300; fi
    continue
  fi

  if pgrep -f 'terminal64\.exe' >/dev/null 2>&1; then
    echo "[MT5] Terminal process is running. Monitoring for unexpected exit."
    backoff=30
    while pgrep -f 'terminal64\.exe' >/dev/null 2>&1; do sleep 15; done
    echo "[MT5] Terminal process exited; attempting safe reconnection/restart."
  else
    echo "[MT5] Helper returned but no terminal process was found; retrying in ${backoff}s."
  fi
  sleep "$backoff"
  if ((backoff < 300)); then backoff=$((backoff * 2)); fi
  if ((backoff > 300)); then backoff=300; fi
done
