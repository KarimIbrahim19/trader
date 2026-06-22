"""
core/config.py
──────────────────────────────────────────────────────────────────────
Loads and validates the full application configuration.

Sources (in priority order for secrets — env vars override .env file):
  1. config/.env          ← secrets (API keys, bot token)
  2. config/settings.yaml ← all other settings

Usage:
    from core.config import load_settings
    settings = load_settings()
    print(settings.mode)
    print(settings.risk.trade_size)
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


# ── Sub-configs ────────────────────────────────────────────────────────
@dataclass
class InstrumentSettings:
    symbol:       str   # raw Binance symbol, e.g. "BTCUSDT"
    nt_id:        str   # NautilusTrader ID, e.g. "BTCUSDT-PERP.BINANCE"
    venue:        str   # e.g. "BINANCE"
    account_type: str   # e.g. "USDT_FUTURES"


@dataclass
class TimeframeSettings:
    primary: str   # e.g. "15m"
    htf:     str   # e.g. "1h"


@dataclass
class StrategySettings:
    name:       str
    swing_len:  int
    atr_dist:   float
    atr_len:    int
    htf_filter: bool
    htf_period: int


@dataclass
class RiskSettings:
    trade_size:            float
    sl_atr:                float
    tp1_atr:               float
    tp2_atr:               float
    trailing_tp2:          bool
    trail_atr_mult:        float
    breakeven_sl:          bool
    enable_exit_signal:    bool
    max_open_trades:       int
    daily_loss_limit_usdt: float


@dataclass
class RedisSettings:
    host:         str
    port:         int
    timeout_secs: int


@dataclass
class LoggingSettings:
    level:          str
    level_file:     str
    log_dir:        str
    log_file_name:  str   # added for NautilusTrader 1.228.0 LoggingConfig
    rotate_mb:      int
    keep_backups:   int


@dataclass
class TelegramSettings:
    enabled:                bool
    notify_signals:         bool
    notify_entries:         bool
    notify_exits:           bool
    notify_daily_summary:   bool
    daily_summary_utc:      str
    heartbeat_interval_mins: int
    # Loaded from .env
    bot_token: str = ""
    chat_id:   str = ""


# ── Root config ───────────────────────────────────────────────────────
@dataclass
class Settings:
    mode:       str   # dry_run | paper | live
    instrument: InstrumentSettings
    timeframes: TimeframeSettings
    strategy:   StrategySettings
    risk:       RiskSettings
    redis:      RedisSettings
    logging:    LoggingSettings
    telegram:   TelegramSettings

    # Binance credentials — loaded from .env
    binance_api_key:            str = ""
    binance_api_secret:         str = ""
    binance_testnet_api_key:    str = ""
    binance_testnet_api_secret: str = ""

    # Derived helpers
    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    @property
    def is_dry_run(self) -> bool:
        return self.mode == "dry_run"

    @property
    def active_api_key(self) -> str:
        """Returns testnet key when in paper mode, real key otherwise."""
        if self.is_paper:
            return self.binance_testnet_api_key
        return self.binance_api_key

    @property
    def active_api_secret(self) -> str:
        if self.is_paper:
            return self.binance_testnet_api_secret
        return self.binance_api_secret


# ── Loader ────────────────────────────────────────────────────────────
def load_settings(
    config_dir: str | Path | None = None,
    settings_file: str = "settings.yaml",
) -> Settings:
    """
    Load and validate configuration from settings.yaml + .env.

    Args:
        config_dir: Directory containing settings.yaml and .env.
                    Defaults to <project_root>/config.
        settings_file: YAML filename inside config_dir.
    """
    if config_dir is None:
        # Resolve relative to this file's location: core/ → parent → config/
        config_dir = Path(__file__).parent.parent / "config"
    config_dir = Path(config_dir)

    # ── Load .env file (secrets) ──────────────────────────────────────
    env_file = config_dir / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)  # os env takes priority
    else:
        # .env is optional — keys may be set as real env vars in production
        pass

    # ── Load YAML ──────────────────────────────────────────────────────
    yaml_path = config_dir / settings_file
    if not yaml_path.exists():
        raise FileNotFoundError(f"Settings file not found: {yaml_path}")

    with open(yaml_path) as f:
        raw = yaml.safe_load(f)

    # ── Validate mode ──────────────────────────────────────────────────
    mode = raw.get("mode", "dry_run")
    if mode not in {"dry_run", "paper", "live"}:
        raise ValueError(f"Invalid mode '{mode}'. Must be: dry_run | paper | live")

    # ── Build sub-configs ──────────────────────────────────────────────
    inst_raw  = raw["instrument"]
    tf_raw    = raw["timeframes"]
    strat_raw = raw["strategy"]
    risk_raw  = raw["risk"]
    redis_raw = raw["redis"]
    log_raw   = raw["logging"]
    tg_raw    = raw["telegram"]

    settings = Settings(
        mode = mode,

        instrument = InstrumentSettings(
            symbol       = inst_raw["symbol"],
            nt_id        = inst_raw["nt_id"],
            venue        = inst_raw["venue"],
            account_type = inst_raw["account_type"],
        ),

        timeframes = TimeframeSettings(
            primary = tf_raw["primary"],
            htf     = tf_raw["htf"],
        ),

        strategy = StrategySettings(
            name       = strat_raw["name"],
            swing_len  = strat_raw["swing_len"],
            atr_dist   = strat_raw["atr_dist"],
            atr_len    = strat_raw["atr_len"],
            htf_filter = strat_raw["htf_filter"],
            htf_period = strat_raw["htf_period"],
        ),

        risk = RiskSettings(
            trade_size            = risk_raw["trade_size"],
            sl_atr                = risk_raw["sl_atr"],
            tp1_atr               = risk_raw["tp1_atr"],
            tp2_atr               = risk_raw["tp2_atr"],
            trailing_tp2          = risk_raw["trailing_tp2"],
            trail_atr_mult        = risk_raw["trail_atr_mult"],
            breakeven_sl          = risk_raw["breakeven_sl"],
            enable_exit_signal    = risk_raw["enable_exit_signal"],
            max_open_trades       = risk_raw["max_open_trades"],
            daily_loss_limit_usdt = risk_raw["daily_loss_limit_usdt"],
        ),

        redis = RedisSettings(
            host         = os.getenv("REDIS_HOST", redis_raw["host"]),
            port         = int(os.getenv("REDIS_PORT", redis_raw["port"])),
            timeout_secs = redis_raw["timeout_secs"],
        ),

        logging = LoggingSettings(
            level        = log_raw["level"],
            level_file   = log_raw["level_file"],
            log_dir      = log_raw["log_dir"],
            log_file_name = log_raw["log_file_name"],
            rotate_mb    = log_raw["rotate_mb"],
            keep_backups = log_raw["keep_backups"],
        ),

        telegram = TelegramSettings(
            enabled                 = tg_raw["enabled"],
            notify_signals          = tg_raw["notify_signals"],
            notify_entries          = tg_raw["notify_entries"],
            notify_exits            = tg_raw["notify_exits"],
            notify_daily_summary    = tg_raw["notify_daily_summary"],
            daily_summary_utc       = tg_raw["daily_summary_utc"],
            heartbeat_interval_mins = tg_raw["heartbeat_interval_mins"],
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_id   = os.getenv("TELEGRAM_CHAT_ID", ""),
        ),

        binance_api_key            = os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret         = os.getenv("BINANCE_API_SECRET", ""),
        binance_testnet_api_key    = os.getenv("BINANCE_TESTNET_API_KEY", ""),
        binance_testnet_api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", ""),
    )

    _validate(settings)
    return settings


def _validate(s: Settings) -> None:
    """Raise ValueError for any configuration that would cause a silent failure."""
    errors = []

    # API keys required for paper and live modes
    if s.is_paper:
        if not s.binance_testnet_api_key:
            errors.append("mode=paper requires BINANCE_TESTNET_API_KEY in config/.env")
        if not s.binance_testnet_api_secret:
            errors.append("mode=paper requires BINANCE_TESTNET_API_SECRET in config/.env")

    if s.is_live:
        if not s.binance_api_key:
            errors.append("mode=live requires BINANCE_API_KEY in config/.env")
        if not s.binance_api_secret:
            errors.append("mode=live requires BINANCE_API_SECRET in config/.env")

    # Telegram bot credentials required if telegram is enabled
    if s.telegram.enabled:
        if not s.telegram.bot_token:
            errors.append("telegram.enabled=true requires TELEGRAM_BOT_TOKEN in config/.env")
        if not s.telegram.chat_id:
            errors.append("telegram.enabled=true requires TELEGRAM_CHAT_ID in config/.env")

    # Risk sanity checks
    if s.risk.tp1_atr <= s.risk.sl_atr:
        errors.append(
            f"risk.tp1_atr ({s.risk.tp1_atr}) must be > risk.sl_atr ({s.risk.sl_atr}) "
            "to guarantee TP1-reached trades are always net winners"
        )
    if s.risk.trade_size <= 0:
        errors.append("risk.trade_size must be > 0")
    if s.risk.max_open_trades < 1:
        errors.append("risk.max_open_trades must be >= 1")

    if errors:
        msg = "\n".join(f"  • {e}" for e in errors)
        raise ValueError(f"Configuration errors found:\n{msg}")
