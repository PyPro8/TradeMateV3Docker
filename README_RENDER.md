# TradeMate Vision v5.3 — Render Docker deployment

This deployment runs the Flask application, Linux/Wine, MetaTrader 5 and the
TradeMateBridge EA in one Render Docker web service. MT5 and all mutable state
live under `/var/data`, which must be a Render persistent disk.

## Important operating limits

- This template is **DEMO-only**. It forces `TRADEMATE_MODE=demo` and the
  expected broker server `Headway-Demo`; the existing adapter also performs
  its own account/server checks. Do not change this to live mode.
- Render is a general application host, not a dedicated trading VPS. Deploys,
  restarts, host maintenance, resource pressure or network interruptions can
  stop Wine/MT5 and interrupt EA monitoring. Render persistent disks require a
  single instance and prevent zero-downtime deploys. Do not rely on Anti-Twin as
  the only protection for an open position during a service interruption.
- Do not deploy/restart while positions are open. First verify the account is
  flat in MT5/broker history, then perform maintenance.
- The Flask web service opens its Render port first; Wine/MT5 installation and
  EA compilation then run in a supervised background process. The dashboard is
  reachable while MT5 initialises and will report disconnected until a fresh
  bridge snapshot appears. First boot can take several minutes. A failed install
  is reported in `/var/data/logs/mt5-install.log` and supervisor output is in
  `/var/data/logs/mt5-supervisor.log`.
- MT5 account credentials are supplied through Render environment secrets.
  They are written to a mode-0600 temporary startup file, not to Git or the
  Docker image, and removed after terminal startup. MT5 may retain its own
  protected account data in the persistent Wine prefix.
- `MT5_SERVER` must exactly match `TRADEMATE_EXPECTED_SERVER` (default
  `Headway-Demo`). Set `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`,
  `TRADEMATE_WEB_USER`, and `TRADEMATE_WEB_PASSWORD` in Render. Never send
  credentials in chat or commit them to the repository.
- Use a broker-issued demo account. If the broker requires an interactive
  password-change flow, OTP, certificate or additional account setup, this
  unattended first-login path may need broker-specific onboarding.

## Deploy with Render Blueprint

1. Push this project to a private GitHub repository.
2. In Render, choose **New → Blueprint** and select that repository.
3. Review the `trademate-vision` web service. The blueprint uses a paid
   `standard` Docker web service in Frankfurt and a 10 GB persistent disk at
   `/var/data`.
4. Add these environment secrets when prompted:
   - `TRADEMATE_WEB_USER`: a username for dashboard/API access.
   - `TRADEMATE_WEB_PASSWORD`: a unique password of at least 16 characters.
   - `MT5_LOGIN`: numeric Headway demo account login.
   - `MT5_PASSWORD`: that demo account's master/trading password.
5. Keep `TRADEMATE_MODE=demo`, `TRADEMATE_EXPECTED_SERVER=Headway-Demo`, and
   `MT5_SERVER=Headway-Demo` unchanged unless the broker explicitly provides
   a different demo server and the project configuration is deliberately
   updated to match.
6. Deploy and watch the logs. Gunicorn opens the web port first. The MT5
   supervisor then installs MT5 on first boot, compiles TradeMateBridge with
   MetaEditor and starts MT5 with the EA attached in the background.
7. Open the Render URL. The browser prompts for HTTP Basic authentication.
8. Confirm the dashboard reports the correct demo account and a fresh bridge
   snapshot. Verify read-only candle retrieval before testing any order flow.

## Render service settings

- Runtime: Docker (`Dockerfile` at repository root)
- Health check: `/healthz` (does not expose account information)
- Persistent disk: `/var/data`, 10 GB minimum in this template
- One instance only; do not scale horizontally
- Auto-deploy disabled deliberately: an automatic deployment can interrupt
  an active MT5 terminal. Deploy manually only when the account is flat.

## Useful environment variables

| Variable | Default / purpose |
|---|---|
| `PORT` | Render web port, default 10000 |
| `TRADEMATE_MODE` | Must remain `demo` |
| `TRADEMATE_EXPECTED_SERVER` | `Headway-Demo` |
| `MT5_SERVER` | Must match expected server |
| `MT5_LOGIN` / `MT5_PASSWORD` | Render secrets for demo account |
| `TRADEMATE_WEB_USER` / `TRADEMATE_WEB_PASSWORD` | Required dashboard/API authentication |
| `MT5_SETUP_URL` | Optional broker-provided MT5 installer URL; defaults to MetaQuotes installer |
| `TM_CHART_SYMBOL` | Initial chart symbol, default `EURUSD` |
| `TM_CHART_PERIOD` | Initial chart timeframe, default `M5` |
| `MT5_WAIT` | MT5 startup wait, default 120 seconds |
| `MT5_INSTALL_TIMEOUT` | Installer timeout, default 900 seconds |

## Verification checklist

- [ ] Render service is paid and persistent disk is mounted at `/var/data`.
- [ ] First boot completes Wine and MT5 installation.
- [ ] `terminal64.exe` and `MetaEditor64.exe` exist in the persistent prefix.
- [ ] MetaEditor compiles `TradeMateBridge.mq5` with zero errors.
- [ ] MT5 logs into the intended demo server.
- [ ] TradeMateBridge writes a fresh `trademate_snapshot.json` to Common/Files.
- [ ] `/api/broker` reports the expected demo account and connected bridge.
- [ ] Read-only candle request succeeds.
- [ ] Manual order and reversal tests pass on demo only.
- [ ] Restart/recovery is tested with no open positions.

A healthy HTTP response alone does not prove MT5 is connected or that the EA is
executing correctly. Verify both independently.
