#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/app"
DATA_ROOT="${TRADEMATE_RENDER_DATA_ROOT:-/var/data}"
WINEPREFIX="${WINEPREFIX:-$DATA_ROOT/wineprefix}"
MT5_INSTALL_DIR="${MT5_INSTALL_DIR:-$WINEPREFIX/drive_c/MT5}"
DISPLAY="${DISPLAY:-:99}"
PORT="${PORT:-10000}"

# Render mounts its persistent disk at /var/data. Only these paths are durable.
mkdir -p "$DATA_ROOT" "$DATA_ROOT/home" "$DATA_ROOT/wineprefix" "$DATA_ROOT/trademate" "$DATA_ROOT/logs"
chown trademate:trademate "$DATA_ROOT" "$DATA_ROOT/home" "$DATA_ROOT/wineprefix" "$DATA_ROOT/trademate" "$DATA_ROOT/logs"

# Seed user data from the archive once, without ever overwriting persistent data.
if [[ ! -e "$DATA_ROOT/trademate/.render_seeded" ]]; then
  if [[ -d "$APP_DIR/data" ]]; then
    cp -an "$APP_DIR/data/." "$DATA_ROOT/trademate/" || true
  fi
  chown -R trademate:trademate "$DATA_ROOT/trademate"
  touch "$DATA_ROOT/trademate/.render_seeded"
fi

exec gosu trademate:trademate env \
  HOME="$DATA_ROOT/home" \
  WINEPREFIX="$WINEPREFIX" \
  MT5_INSTALL_DIR="$MT5_INSTALL_DIR" \
  DISPLAY="$DISPLAY" \
  PORT="$PORT" \
  TRADEMATE_HOST="0.0.0.0" \
  TRADEMATE_MODE="${TRADEMATE_MODE:-demo}" \
  TRADEMATE_DATA_DIR="$DATA_ROOT/trademate" \
  TRADEMATE_LOG_DIR="$DATA_ROOT/logs" \
  TRADEMATE_LAUNCHER_LOG_DIR="$DATA_ROOT/logs/launcher" \
  TRADEMATE_EXPECTED_SERVER="${TRADEMATE_EXPECTED_SERVER:-Headway-Demo}" \
  PYTHONUNBUFFERED=1 \
  /app/deploy/run-as-trademate.sh
