#!/usr/bin/env bash
set -Eeuo pipefail
cd /app

export WINEPREFIX DISPLAY WINEDEBUG="${WINEDEBUG:-fixme-all}"
export TRADEMATE_MODE="${TRADEMATE_MODE:-demo}"
export TRADEMATE_HOST="0.0.0.0"
export PORT="${PORT:-10000}"
export TRADEMATE_DATA_DIR="${TRADEMATE_DATA_DIR:-/var/data/trademate}"
export TRADEMATE_LOG_DIR="${TRADEMATE_LOG_DIR:-/var/data/logs}"
export TRADEMATE_LAUNCHER_LOG_DIR="${TRADEMATE_LAUNCHER_LOG_DIR:-/var/data/logs/launcher}"
export TRADEMATE_EXPECTED_SERVER="${TRADEMATE_EXPECTED_SERVER:-Headway-Demo}"
export MT5_INSTALL_DIR="${MT5_INSTALL_DIR:-$WINEPREFIX/drive_c/MT5}"

if [[ "$TRADEMATE_MODE" != "demo" ]]; then
  echo "FATAL: Render deployment requires TRADEMATE_MODE=demo. Refusing to start." >&2
  exit 1
fi
for key in MT5_LOGIN MT5_PASSWORD MT5_SERVER TRADEMATE_WEB_USER TRADEMATE_WEB_PASSWORD; do
  if [[ -z ${!key:-} ]]; then
    echo "FATAL: required Render environment variable $key is missing." >&2
    exit 1
  fi
done
if [[ ! "$MT5_LOGIN" =~ ^[0-9]+$ ]]; then
  echo "FATAL: MT5_LOGIN must be a numeric demo-account login." >&2
  exit 1
fi
if [[ "${MT5_SERVER,,}" != "${TRADEMATE_EXPECTED_SERVER,,}" ]]; then
  echo "FATAL: MT5_SERVER must match TRADEMATE_EXPECTED_SERVER. Refusing to start." >&2
  exit 1
fi
if [[ ${#TRADEMATE_WEB_PASSWORD} -lt 16 ]]; then
  echo "FATAL: TRADEMATE_WEB_PASSWORD must be at least 16 characters." >&2
  exit 1
fi

# Keep MT5 credentials in a one-use private handoff file. The supervisor reads
# them into unexported shell variables and writes per-launch mode-0600 INI files.
umask 077
MT5_CREDENTIALS_FILE="$(mktemp /tmp/trademate-mt5-credentials.XXXXXX)"
printf '%s\0%s\0%s\0' "$MT5_LOGIN" "$MT5_PASSWORD" "$MT5_SERVER" > "$MT5_CREDENTIALS_FILE"
chmod 600 "$MT5_CREDENTIALS_FILE"
export TRADEMATE_MT5_CREDENTIALS_FILE="$MT5_CREDENTIALS_FILE"
unset MT5_LOGIN MT5_PASSWORD MT5_SERVER

# The web credentials are passed only to Gunicorn, never to Wine/MT5.
WEB_AUTH_USER_VALUE="$TRADEMATE_WEB_USER"
WEB_AUTH_PASSWORD_VALUE="$TRADEMATE_WEB_PASSWORD"
export -n TRADEMATE_WEB_USER TRADEMATE_WEB_PASSWORD

mkdir -p "$TRADEMATE_DATA_DIR" "$TRADEMATE_LOG_DIR"

# Start the HTTP service immediately so Render can detect its port while MT5
# installs/initialises in the background. One worker is deliberate: the app
# owns in-process engines, locks and execution state.
env -u TRADEMATE_MT5_CREDENTIALS_FILE \
  TRADEMATE_WEB_USER="$WEB_AUTH_USER_VALUE" \
  TRADEMATE_WEB_PASSWORD="$WEB_AUTH_PASSWORD_VALUE" \
  .venv/bin/gunicorn \
    --chdir /app \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --graceful-timeout 30 \
    --bind "0.0.0.0:${PORT}" \
    --access-logfile - \
    --error-logfile - \
    deploy.wsgi:application &
WEB_PID=$!

# Wait for the protected app's public health endpoint locally before starting
# the slower Wine/MT5 first-boot work.
ready=0
for _ in $(seq 1 60); do
  if ! kill -0 "$WEB_PID" 2>/dev/null; then
    echo "FATAL: Gunicorn exited before becoming ready." >&2
    wait "$WEB_PID" || true
    exit 1
  fi
  if curl --fail --silent --max-time 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
if [[ $ready != 1 ]]; then
  echo "FATAL: TradeMate web service did not become ready on port ${PORT}." >&2
  kill -TERM "$WEB_PID" 2>/dev/null || true
  wait "$WEB_PID" || true
  exit 1
fi

# MT5 startup runs as a supervised background process. The dashboard remains
# reachable during the initial installer download/EA compilation and displays
# the actual disconnected/connected bridge state.
./deploy/mt5-supervisor.sh >>"$TRADEMATE_LOG_DIR/mt5-supervisor.log" 2>&1 &
MT5_SUPERVISOR_PID=$!
echo "[TradeMate] Web service ready on 0.0.0.0:${PORT}; MT5 supervisor pid=${MT5_SUPERVISOR_PID}."

wait "$WEB_PID"
