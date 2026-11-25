FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/trademate \
    WINEPREFIX=/var/data/wineprefix \
    WINEARCH=win64 \
    DISPLAY=:99 \
    WINEDEBUG=fixme-all

# MT5 is a 64-bit Windows GUI application. Wine's i386 libraries are retained
# because Wine/MT5 dependencies may require 32-bit compatibility components.
RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl wget gnupg \
      python3 python3-venv python3-pip build-essential \
      wine wine64 wine32 winbind \
      xvfb xauth x11-utils \
      fonts-liberation fonts-wine cabextract unzip \
      procps psmisc util-linux gosu tini \
      libgl1 libx11-6 libasound2 libfreetype6 libfontconfig1 \
      libxrender1 libxext6 libxfixes3 libxi6 libxcursor1 libxrandr2 \
      libxinerama1 libxcomposite1 libxdamage1 libxss1 libxtst6 \
      libnss3 libdbus-1-3 libgtk-3-0 libgbm1 libxkbcommon0 \
      libxshmfence1 libpulse0 \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 10001 --create-home --shell /bin/bash trademate \
 && mkdir -p /app /var/data \
 && chown trademate:trademate /app /var/data

WORKDIR /app
COPY --chown=trademate:trademate . /app

RUN python3 -m venv /app/.venv \
 && /app/.venv/bin/pip install --upgrade pip setuptools wheel \
 && /app/.venv/bin/pip install -r requirements.txt \
 && chmod +x /app/deploy/entrypoint.sh /app/deploy/run-as-trademate.sh /app/mt5/wine_mt5_helper.sh

EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
  CMD curl --fail --silent http://127.0.0.1:${PORT:-10000}/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/deploy/entrypoint.sh"]
