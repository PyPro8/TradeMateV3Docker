"""Configuration loading.

Precedence (lowest to highest):
    1. Built-in defaults (the dataclasses below)
    2. config/settings.toml
    3. Environment variables (mode/logging, persistent data paths, host and PORT)

The [trading] section sets manual-execution guard rails (DEMO only).
The [mt5] section tells TradeMate where to find the snapshot written by the
TradeMateBridge Expert Advisor. No MT5 password or login is ever needed or
stored: the MT5 terminal is already logged in. Secrets must NOT go in
settings.toml.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from trademate.errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.toml"


class Mode(str, Enum):
    DEVELOPMENT = "development"
    DEMO = "demo"
    LIVE = "live"


@dataclass(frozen=True)
class AppSettings:
    name: str = "TradeMate"
    log_level: str = "INFO"
    log_dir: Path = PROJECT_ROOT / "logs"
    data_dir: Path = PROJECT_ROOT / "data"  # TradeMate's own records (order journal): never MT5's files


@dataclass(frozen=True)
class Mt5Settings:
    """How TradeMate finds the MT5 bridge snapshot (read-only)."""

    data_dir: Path | None = None  # None = auto-detect the MT5 shared files folder
    snapshot_file: str = "trademate_snapshot.json"
    stale_after_seconds: float = 10.0
    # The MT5 server orders may be sent to. Precedence: env TRADEMATE_EXPECTED_SERVER > [mt5] expected_server > this default.
    # Orders are REFUSED when the connected server differs or cannot be verified (see trademate/broker/server_check.py).
    expected_server: str = "Headway-Demo"


@dataclass(frozen=True)
class TradingSettings:
    """Manual DEMO execution guard rails. Executes only when the user clicks."""

    enabled: bool = True
    max_volume: float = 1.0  # refuse any single order above this many lots
    deviation_points: int = 20  # allowed slippage
    magic: int = 20260401  # tags TradeMate orders in MT5
    command_timeout_seconds: float = 15.0  # how long to wait for MT5 to answer
    confirm_timeout_seconds: float = 6.0  # how long to wait for the account to show the change


@dataclass(frozen=True)
class RiskSettings:
    """Phase 5.1 position sizing limits. Only used to CALCULATE a size: nothing here places an order."""

    default_risk_percent: float = 1.0  # pre-filled in the proposed-trade form
    max_risk_percent: float = 1.0  # max risk per trade (% of equity). The user can change every rule in the UI.
    max_daily_loss_percent: float = 3.0  # % of the start-of-day balance
    max_total_active_risk_percent: float = 3.0  # % of equity, all open positions together
    max_positions: int = 3
    # IANA timezone of the BROKER's server clock (e.g. "Europe/Athens"), used for the daily-loss trading day ONLY when the live
    # measurement from MT5 is unavailable (market closed, MT5 disconnected). "" = none: the daily loss is then reported unknown.
    broker_timezone: str = ""


@dataclass(frozen=True)
class StructureSettings:
    """Phase 7A market-structure analysis defaults. Analysis only: nothing here places an order."""

    swing_left: int = 3
    swing_right: int = 3
    break_mode: str = "close"  # "close" or "wick"
    equal_tolerance: float = 0.0


@dataclass(frozen=True)
class StrategySettings:
    """Phase 8 multi-timeframe strategy policy values. Analysis and proposals only: nothing here places an order."""

    min_rr: float = 1.5            # minimum reward-to-risk of TP1
    trigger_lookback: int = 12     # closed M5 candles in which the zone reaction and the M5 trigger must both have happened
    sl_buffer_ticks: int = 0       # ticks added beyond the structural stop (needs the symbol's tick size; 0 = none)
    # Selectable strategies (CHoCH Trading / Trend Continuation; both read only H1 and M5)
    default_strategy: str = "choch"          # used until the user selects one; validated against the registry at load
    counter_min_rr: float = 2.0              # CHoCH AGAINST the H1 direction needs at least this reward-to-risk
    signal_max_age: int = 12                 # closed M5 candles a CHoCH / resumption break stays actionable
    pullback_min_retrace: float = 0.25       # a pullback must retrace at least this fraction of the M5 impulse leg
    # Candle Momentum and CHoCH-with-fixed-target (distances are in PIPS; what a pip is comes from trademate.pips, never from here)
    candle_trigger_pips: float = 10.0        # the candle must move this far from its open before an entry qualifies
    candle_target_pips: float = 20.0         # take-profit: this many pips further from the ACTUAL fill
    candle_sl_buffer_pips: float = 50.0      # protective stop distance beyond the candle open
    choch_target_pips: float = 100.0         # CHoCH Trading take-profit, from the ACTUAL entry
    market_structure_target_pips: float = 25.0   # Market Structure Trading take-profit, from the ACTUAL entry
    bull_bear_target_pips: float = 25.0      # Bull/Bear Trading take-profit, from the ACTUAL entry
    choch_max_extension_fraction: float = 0.5  # a CHoCH is stale once price has moved this fraction of the target beyond its level
    default_exec_timeframe: str = "M5"       # used until the user chooses an execution timeframe
    # EMA 20/50 + ATR strategy
    ema_fast_period: int = 20
    ema_slow_period: int = 50
    atr_period: int = 14
    atr_sl_multiplier: float = 2.0           # stop-loss distance = this x ATR
    atr_tp_multiplier: float = 4.0           # take-profit distance = this x ATR


@dataclass(frozen=True)
class CandleMomentumSettings:
    """Candle Momentum engine ([candle_momentum] in settings.toml). Timing and validity controls: none of them loosens the Risk Engine or a broker check.
    Defaults are POLICY values, not tuned against data (see docs/CANDLE_MOMENTUM.md)."""

    poll_seconds: float = 0.25                 # how often the engine reads the quote: the tick-monitoring cadence (the browser refresh rate is unrelated)
    candle_verify_seconds: float = 5.0         # the forming candle is re-read from the broker at least this often (and at every expected candle boundary)
    spec_refresh_seconds: float = 60.0         # the instrument specification is re-read this often (a spec older than 300 s blocks execution)
    quote_max_age_seconds: float = 3.0         # a quote older than this is never used to judge an entry
    signal_expiry_seconds: float = 15.0        # a reached trigger that has not become an order within this time is EXPIRED
    max_slippage_units: float = 0.0            # execution slippage tolerance in strategy units; 0 = not set (the pipeline's drift protection applies)
    max_entry_attempts: int = 1                # 1 = no retry; a retry only follows an order that provably was not sent
    retry_delay_seconds: float = 2.0


@dataclass(frozen=True)
class PipSettings:
    """What one pip is, per instrument, where the automatic rule (forex / metals) does not apply. Price units per pip."""

    overrides: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AutopilotSettings:
    """Autopilot runtime. It never loosens the Risk Engine; it only decides WHEN a risk-approved setup is submitted."""

    poll_seconds: float = 5.0               # how often the loop looks at the current decision
    max_decision_age_seconds: float = 30.0  # a decision older than this is stale and is never executed
    max_runs_limit: int = 1000
    max_hours_limit: float = 720.0


@dataclass(frozen=True)
class PipelineSettings:
    """Strategy -> Risk -> Execution pipeline (controlled, manual-confirmation mode). These are EXECUTION-ELIGIBILITY values, not strategy rules."""

    approval_ttl_seconds: float = 60.0         # a risk approval expires after this long; a new proposal is then required
    max_entry_drift_stop_fraction: float = 0.25  # live price may differ from the strategy's reference entry by at most this fraction of the stop distance
    max_spread_stop_fraction: float = 0.0      # OPTIONAL user limit: spread <= this fraction of the stop. 0 = off (default): the automatic execution-cost policy applies


@dataclass(frozen=True)
class UiSettings:
    host: str = "127.0.0.1"
    port: int = 5000
    # IANA timezone of the "local" clock in the top bar (e.g. "Asia/Manila"). "" = this browser's own timezone.
    timezone: str = ""


@dataclass(frozen=True)
class Settings:
    mode: Mode = Mode.DEVELOPMENT
    app: AppSettings = field(default_factory=AppSettings)
    mt5: Mt5Settings = field(default_factory=Mt5Settings)
    trading: TradingSettings = field(default_factory=TradingSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    structure: StructureSettings = field(default_factory=StructureSettings)
    strategy: StrategySettings = field(default_factory=StrategySettings)
    pipeline: PipelineSettings = field(default_factory=PipelineSettings)
    pips: PipSettings = field(default_factory=PipSettings)
    autopilot: AutopilotSettings = field(default_factory=AutopilotSettings)
    candle_momentum: CandleMomentumSettings = field(default_factory=CandleMomentumSettings)
    ui: UiSettings = field(default_factory=UiSettings)


def _parse_candle_momentum(data) -> CandleMomentumSettings:
    if not isinstance(data, dict):
        raise ConfigError("[candle_momentum] must be a table.")
    out = {}
    if "max_target_consumed_fraction" in data:
        logging.getLogger("trademate.config").warning(
            "settings.toml [candle_momentum] max_target_consumed_fraction is no longer used: percentage-based late-entry (MISSED) rejection was removed. The key is ignored.")
    limits = {"poll_seconds": (0.05, 5), "candle_verify_seconds": (0.5, 600), "spec_refresh_seconds": (5, 3600), "quote_max_age_seconds": (0.2, 600),
              "signal_expiry_seconds": (0.5, 3600), "max_slippage_units": (0, 100000), "retry_delay_seconds": (0.1, 600)}
    for name, (lo, hi) in limits.items():
        v = data.get(name, getattr(CandleMomentumSettings, name))
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi or (lo == 0.0001 and v <= 0):
            raise ConfigError(f"[candle_momentum] {name} must be a number from {lo:g} to {hi:g}.")
        out[name] = float(v)
    n = data.get("max_entry_attempts", CandleMomentumSettings.max_entry_attempts)
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 3:
        raise ConfigError("[candle_momentum] max_entry_attempts must be a whole number from 1 to 3 (1 = no retry).")
    out["max_entry_attempts"] = n
    return CandleMomentumSettings(**out)


def _parse_mode(value: object) -> Mode:
    try:
        return Mode(str(value).strip().lower())
    except ValueError:
        valid = ", ".join(m.value for m in Mode)
        raise ConfigError(f"Invalid mode {value!r}. Valid modes: {valid}.") from None


def _parse_log_level(value: object) -> str:
    level = str(value).strip().upper()
    if not isinstance(logging.getLevelName(level), int):
        raise ConfigError(f"Invalid log level {value!r}.")
    return level


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc


def load_config(path: Path | None = None) -> Settings:
    """Load settings from defaults, the TOML file, and environment variables."""
    if path is None:
        path = Path(os.environ.get("TRADEMATE_CONFIG", DEFAULT_CONFIG_PATH))
    data = _read_toml(path)

    app_data = data.get("app", {})
    mt5_data = data.get("mt5", {})
    ui_data = data.get("ui", {})
    trading_data = data.get("trading", {})
    risk_data = data.get("risk", {})
    structure_data = data.get("structure", {})
    strategy_data = data.get("strategy", {})
    if isinstance(strategy_data, dict) and "candle_late_pips" in strategy_data:
        logging.getLogger("trademate.config").warning(
            "settings.toml [strategy] candle_late_pips is no longer used: the hard-coded late-entry limit was removed in Candle Momentum v2. "
            "See [candle_momentum] and docs/CANDLE_MOMENTUM.md. The key is ignored.")
    pipeline_data = data.get("pipeline", {})
    pips_data = data.get("pips", {})
    autopilot_data = data.get("autopilot", {})
    cm_data = data.get("candle_momentum", {})

    mode = _parse_mode(os.environ.get("TRADEMATE_MODE", data.get("mode", Mode.DEVELOPMENT.value)))
    log_level = _parse_log_level(
        os.environ.get("TRADEMATE_LOG_LEVEL", app_data.get("log_level", AppSettings.log_level))
    )

    log_dir = Path(os.environ.get("TRADEMATE_LOG_DIR", app_data.get("log_dir", "logs")))
    if not log_dir.is_absolute():
        log_dir = PROJECT_ROOT / log_dir

    local_dir = Path(str(os.environ.get("TRADEMATE_DATA_DIR", app_data.get("data_dir", "data"))).strip() or "data")
    if not local_dir.is_absolute():
        local_dir = PROJECT_ROOT / local_dir

    data_dir_text = str(os.environ.get("TRADEMATE_MT5_DATA_DIR", mt5_data.get("data_dir", ""))).strip()
    stale = mt5_data.get("stale_after_seconds", Mt5Settings.stale_after_seconds)
    if isinstance(stale, bool) or not isinstance(stale, (int, float)) or stale <= 0:
        raise ConfigError("[mt5] stale_after_seconds must be a number greater than 0.")
    snapshot_file = str(mt5_data.get("snapshot_file", Mt5Settings.snapshot_file)).strip()
    if not snapshot_file or "/" in snapshot_file or "\\" in snapshot_file:
        raise ConfigError("[mt5] snapshot_file must be a plain file name.")

    expected_server = str(os.environ.get("TRADEMATE_EXPECTED_SERVER",
                                         mt5_data.get("expected_server", Mt5Settings.expected_server))).strip()

    raw_port = os.environ.get("PORT", ui_data.get("port", UiSettings.port))
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        raise ConfigError("PORT / [ui] port must be an integer between 1 and 65535.") from None
    if isinstance(raw_port, bool) or not 1 <= port <= 65535:
        raise ConfigError("PORT / [ui] port must be an integer between 1 and 65535.")
    ui_host = str(os.environ.get("TRADEMATE_HOST", ui_data.get("host", UiSettings.host))).strip()
    if not ui_host:
        raise ConfigError("TRADEMATE_HOST / [ui] host cannot be empty.")

    t_enabled = trading_data.get("enabled", TradingSettings.enabled)
    if not isinstance(t_enabled, bool):
        raise ConfigError("[trading] enabled must be true or false.")
    max_volume = trading_data.get("max_volume", TradingSettings.max_volume)
    if isinstance(max_volume, bool) or not isinstance(max_volume, (int, float)) or max_volume <= 0:
        raise ConfigError("[trading] max_volume must be a number greater than 0.")
    deviation = trading_data.get("deviation_points", TradingSettings.deviation_points)
    if isinstance(deviation, bool) or not isinstance(deviation, int) or deviation < 0:
        raise ConfigError("[trading] deviation_points must be a whole number, 0 or more.")
    magic = trading_data.get("magic", TradingSettings.magic)
    if isinstance(magic, bool) or not isinstance(magic, int) or magic < 0:
        raise ConfigError("[trading] magic must be a whole number, 0 or more.")

    default_risk = risk_data.get("default_risk_percent", RiskSettings.default_risk_percent)
    max_risk = risk_data.get("max_risk_percent", RiskSettings.max_risk_percent)
    max_daily = risk_data.get("max_daily_loss_percent", RiskSettings.max_daily_loss_percent)
    max_active = risk_data.get("max_total_active_risk_percent", RiskSettings.max_total_active_risk_percent)
    for key, val in (("default_risk_percent", default_risk), ("max_risk_percent", max_risk),
                     ("max_daily_loss_percent", max_daily), ("max_total_active_risk_percent", max_active)):
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not 0 < val <= 100:
            raise ConfigError(f"[risk] {key} must be a number greater than 0 and at most 100.")
    max_pos = risk_data.get("max_positions", RiskSettings.max_positions)
    if isinstance(max_pos, bool) or not isinstance(max_pos, int) or not 1 <= max_pos <= 1000:
        raise ConfigError("[risk] max_positions must be a whole number from 1 to 1000.")

    broker_tz = str(risk_data.get("broker_timezone", RiskSettings.broker_timezone)).strip()
    ui_tz = str(os.environ.get("TRADEMATE_TIMEZONE", ui_data.get("timezone", UiSettings.timezone))).strip()
    from trademate.brokertime import valid_timezone
    if broker_tz and not valid_timezone(broker_tz):
        raise ConfigError(f"[risk] broker_timezone {broker_tz!r} is not a timezone name this computer knows "
                          "(use an IANA name such as Europe/Athens; on Windows run: pip install tzdata).")
    if ui_tz and not valid_timezone(ui_tz):
        raise ConfigError(f"[ui] timezone {ui_tz!r} is not a timezone name this computer knows "
                          "(use an IANA name such as Asia/Manila; on Windows run: pip install tzdata).")

    try:
        from trademate.structure import StructureConfig
        s_left = structure_data.get("swing_left", StructureSettings.swing_left)
        s_right = structure_data.get("swing_right", StructureSettings.swing_right)
        s_mode = structure_data.get("break_mode", StructureSettings.break_mode)
        s_tol = structure_data.get("equal_tolerance", StructureSettings.equal_tolerance)
        StructureConfig(swing_left=s_left, swing_right=s_right, break_mode=s_mode, equal_tolerance=s_tol)  # validates
    except ValueError as exc:
        raise ConfigError(f"[structure] {exc}") from exc

    try:
        from trademate.strategy import StrategyConfig
        st_rr = strategy_data.get("min_rr", StrategySettings.min_rr)
        st_look = strategy_data.get("trigger_lookback", StrategySettings.trigger_lookback)
        st_buf = strategy_data.get("sl_buffer_ticks", StrategySettings.sl_buffer_ticks)
        for name, value in (("min_rr", st_rr), ("trigger_lookback", st_look), ("sl_buffer_ticks", st_buf)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number.")
        if isinstance(st_look, float) or isinstance(st_buf, float) or int(st_buf) < 0:
            raise ValueError("trigger_lookback must be a whole number and sl_buffer_ticks a whole number of 0 or more.")
        StrategyConfig(min_rr=float(st_rr), trigger_lookback=int(st_look)).validate()
        st_def = strategy_data.get("default_strategy", StrategySettings.default_strategy)
        st_cmr = strategy_data.get("counter_min_rr", StrategySettings.counter_min_rr)
        st_age = strategy_data.get("signal_max_age", StrategySettings.signal_max_age)
        st_ret = strategy_data.get("pullback_min_retrace", StrategySettings.pullback_min_retrace)
        from trademate.strategies.registry import STRATEGY_IDS
        if st_def not in STRATEGY_IDS:
            raise ValueError(f"default_strategy must be one of {', '.join(STRATEGY_IDS)}.")
        for name, value in (("counter_min_rr", st_cmr), ("signal_max_age", st_age), ("pullback_min_retrace", st_ret)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
                raise ValueError(f"{name} must be a number above 0.")
        if isinstance(st_age, float) or st_age > 500 or not 0 < st_ret < 1 or st_cmr < float(st_rr):
            raise ValueError("signal_max_age must be a whole number (at most 500), pullback_min_retrace between 0 and 1, and counter_min_rr at least min_rr.")
        extra = {}
        for name in ("candle_trigger_pips", "candle_target_pips", "candle_sl_buffer_pips", "choch_target_pips", "market_structure_target_pips", "bull_bear_target_pips", "choch_max_extension_fraction"):
            value = strategy_data.get(name, getattr(StrategySettings, name))
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 100000:
                raise ValueError(f"{name} must be a number above 0.")
            extra[name] = float(value)
        if extra["choch_max_extension_fraction"] > 1:
            raise ValueError("choch_max_extension_fraction must be between 0 and 1.")
        exec_tf = strategy_data.get("default_exec_timeframe", StrategySettings.default_exec_timeframe)
        from trademate.strategies.base import EXEC_TIMEFRAMES
        if exec_tf not in EXEC_TIMEFRAMES:
            raise ValueError(f"default_exec_timeframe must be one of {', '.join(EXEC_TIMEFRAMES)}.")
        extra["default_exec_timeframe"] = exec_tf
        for name in ("ema_fast_period", "ema_slow_period", "atr_period"):
            value = strategy_data.get(name, getattr(StrategySettings, name))
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
                raise ValueError(f"{name} must be a whole number from 1 to 1000.")
            extra[name] = value
        if extra["ema_fast_period"] >= extra["ema_slow_period"]:
            raise ValueError("ema_fast_period must be smaller than ema_slow_period.")
        for name in ("atr_sl_multiplier", "atr_tp_multiplier"):
            value = strategy_data.get(name, getattr(StrategySettings, name))
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 100:
                raise ValueError(f"{name} must be a number above 0 and at most 100.")
            extra[name] = float(value)
    except ValueError as exc:
        raise ConfigError(f"[strategy] {exc}") from exc
    overrides = pips_data.get("overrides", {}) if isinstance(pips_data, dict) else {}
    if not isinstance(overrides, dict) or any(not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0 for k, v in overrides.items()):
        raise ConfigError("[pips.overrides] must map an instrument name to a pip size greater than 0 (price units per pip).")
    ap_vals = {}
    for key, default, upper in (("poll_seconds", AutopilotSettings.poll_seconds, 600), ("max_decision_age_seconds", AutopilotSettings.max_decision_age_seconds, 3600),
                                ("max_runs_limit", AutopilotSettings.max_runs_limit, 100000), ("max_hours_limit", AutopilotSettings.max_hours_limit, 8760)):
        value = autopilot_data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= upper:
            raise ConfigError(f"[autopilot] {key} must be a number above 0 and at most {upper}.")
        ap_vals[key] = int(value) if key == "max_runs_limit" else float(value)

    pipe_vals = {}
    for key, default in (("approval_ttl_seconds", PipelineSettings.approval_ttl_seconds),
                         ("max_entry_drift_stop_fraction", PipelineSettings.max_entry_drift_stop_fraction),
                         ("max_spread_stop_fraction", PipelineSettings.max_spread_stop_fraction)):
        value = pipeline_data.get(key, default)
        upper = 3600 if key == "approval_ttl_seconds" else 10
        low_ok = (value == 0) if key == "max_spread_stop_fraction" else False     # 0 = the optional spread ratio is off
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not (low_ok or 0 < value <= upper):
            raise ConfigError(f"[pipeline] {key} must be a number greater than 0 and at most {upper}" + (" (0 switches the optional spread ratio off)." if key == "max_spread_stop_fraction" else "."))
        pipe_vals[key] = float(value)

    return Settings(
        mode=mode,
        app=AppSettings(
            name=str(app_data.get("name", AppSettings.name)),
            log_level=log_level,
            log_dir=log_dir,
            data_dir=local_dir,
        ),
        mt5=Mt5Settings(
            data_dir=Path(data_dir_text).expanduser() if data_dir_text else None,
            snapshot_file=snapshot_file,
            stale_after_seconds=float(stale),
            expected_server=expected_server,
        ),
        trading=TradingSettings(enabled=t_enabled, max_volume=float(max_volume),
                                deviation_points=deviation, magic=magic),
        risk=RiskSettings(default_risk_percent=float(default_risk), max_risk_percent=float(max_risk),
                          max_daily_loss_percent=float(max_daily), max_total_active_risk_percent=float(max_active),
                          max_positions=max_pos, broker_timezone=broker_tz),
        structure=StructureSettings(swing_left=s_left, swing_right=s_right, break_mode=s_mode,
                                    equal_tolerance=float(s_tol)),
        strategy=StrategySettings(min_rr=float(st_rr), trigger_lookback=int(st_look), sl_buffer_ticks=int(st_buf), default_strategy=st_def,
                                  counter_min_rr=float(st_cmr), signal_max_age=int(st_age), pullback_min_retrace=float(st_ret), **extra),
        pipeline=PipelineSettings(**pipe_vals),
        pips=PipSettings(overrides={k: float(v) for k, v in overrides.items()}),
        autopilot=AutopilotSettings(**ap_vals),
        candle_momentum=_parse_candle_momentum(cm_data),
        ui=UiSettings(host=ui_host, port=port, timezone=ui_tz),
    )
