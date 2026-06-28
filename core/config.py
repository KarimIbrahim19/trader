"""
core/config.py
────────────────────────────────────────────────────────────────────────
Loads and validates the full application configuration.

Sources (in priority order for secrets):
  1. config/.env          ← secrets (API keys, bot token)
  2. config/settings.yaml ← all other settings

Stage 3 change: the single `strategy` + `risk` block is replaced by a
`strategies` dict. Each entry uses a per-strategy Settings dataclass
(registered in `strategies/REGISTRY`) with its own signal params, bar
types, and risk limits. Strategies are enabled/disabled with a single
`enabled` flag — no code changes needed.

Usage:
    from core.config import load_settings
    settings = load_settings()
    for name, s in settings.strategies.items():
        if s.enabled:
            print(name, s.primary_bar, s.trade_size)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)


# ── Per-strategy settings base ────────────────────────────────────────────
@dataclass
class StrategySettingsBase:
    """
    Common fields present in every strategy's YAML block.
    Per-strategy subclasses add their own specific fields.
    """
    strategy_id:           str
    primary_bar:           str
    htf_bar:               str
    trade_size:            float
    sl_atr:                float
    tp1_atr:               float
    tp2_atr:               float
    max_open_trades:       int
    daily_loss_limit_usdt: float
    enabled:               bool
    htf_filter:            bool
    htf_period:            int
    trailing_tp2:          bool
    trail_atr_mult:        float
    breakeven_sl:          bool
    enable_exit_signal:    bool
    enable_sl:             bool

    def build_config(
        self, strategy_id: str, instrument_id, state_dir: str,
        mode: str, primary_bar, htf_bar,
    ):
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement build_config(). "
            "See MsSettings or FvgSettings for the pattern."
        )


# ── Global sub-configs (unchanged from Stage 1/2) ─────────────────────────
@dataclass
class InstrumentSettings:
    symbol:       str
    nt_id:        str
    venue:        str
    account_type: str


@dataclass
class RedisSettings:
    host:         str
    port:         int
    timeout_secs: int


@dataclass
class LoggingSettings:
    level:         str
    level_file:    str
    log_dir:       str
    log_file_name: str
    rotate_mb:     int
    keep_backups:  int


@dataclass
class TelegramSettings:
    enabled:                 bool
    notify_signals:          bool
    notify_entries:          bool
    notify_exits:            bool
    notify_daily_summary:    bool
    daily_summary_utc:       str
    heartbeat_interval_mins: int
    bot_token: str = ""
    chat_id:   str = ""


# ── Root settings ─────────────────────────────────────────────────────────
@dataclass
class Settings:
    mode:       str                               # dry_run | paper | live
    instrument: InstrumentSettings
    strategies: Dict[str, StrategySettingsBase]
    redis:      RedisSettings
    logging:    LoggingSettings
    telegram:   TelegramSettings
    trader_id:  str

    # Binance credentials
    binance_api_key:            str = ""
    binance_api_secret:         str = ""
    binance_testnet_api_key:    str = ""
    binance_testnet_api_secret: str = ""

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
        return self.binance_testnet_api_key if self.is_paper else self.binance_api_key

    @property
    def active_api_secret(self) -> str:
        return self.binance_testnet_api_secret if self.is_paper else self.binance_api_secret

    @property
    def enabled_strategies(self) -> Dict[str, StrategySettingsBase]:
        """Convenience: only the strategies that are enabled."""
        return {k: v for k, v in self.strategies.items() if v.enabled}


# ── Loader ────────────────────────────────────────────────────────────────
def load_settings(
    config_dir:    str | Path | None = None,
    settings_file: str = "settings.yaml",
) -> Settings:
    if config_dir is None:
        config_dir = Path(__file__).parent.parent / "config"
    config_dir = Path(config_dir)

    env_file = config_dir / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)

    yaml_path = config_dir / settings_file
    if not yaml_path.exists():
        raise FileNotFoundError(f"Settings file not found: {yaml_path}")

    with open(yaml_path) as f:
        raw = yaml.safe_load(f)

    mode = raw.get("mode", "dry_run")
    if mode not in {"dry_run", "paper", "live"}:
        raise ValueError(f"Invalid mode '{mode}'. Must be: dry_run | paper | live")

    # ── Parse per-strategy blocks via registry ──────────────────────────
    # Lazy import avoids circular dependency (strategies modules import
    # StrategySettingsBase from this file).
    from strategies import REGISTRY

    strategies: Dict[str, StrategySettingsBase] = {}
    for name, s in raw.get("strategies", {}).items():
        entry = REGISTRY.get(name)
        if entry is None:
            raise ValueError(
                f"Unknown strategy '{name}'. "
                f"Available: {list(REGISTRY)}. "
                "Add a REGISTRY entry in strategies/__init__.py"
            )

        settings_cls = entry["settings"]
        known = {f.name for f in fields(settings_cls)}
        extra = [k for k in s if k not in known]
        if extra:
            log.warning(
                "strategies.%s: unknown key(s) %s — ignored.  "
                "Valid keys: %s", name, extra, sorted(known),
            )

        strategies[name] = settings_cls(
            **{k: v for k, v in s.items() if k in known}
        )

    inst_raw  = raw["instrument"]
    redis_raw = raw["redis"]
    log_raw   = raw["logging"]
    tg_raw    = raw["telegram"]

    settings = Settings(
        mode       = mode,
        trader_id  = raw["trader_id"],
        instrument = InstrumentSettings(
            symbol       = inst_raw["symbol"],
            nt_id        = inst_raw["nt_id"],
            venue        = inst_raw["venue"],
            account_type = inst_raw["account_type"],
        ),
        strategies = strategies,
        redis      = RedisSettings(
            host         = os.getenv("REDIS_HOST", redis_raw["host"]),
            port         = int(os.getenv("REDIS_PORT", redis_raw["port"])),
            timeout_secs = redis_raw["timeout_secs"],
        ),
        logging    = LoggingSettings(
            level         = log_raw["level"],
            level_file    = log_raw["level_file"],
            log_dir       = log_raw["log_dir"],
            log_file_name = log_raw["log_file_name"],
            rotate_mb     = log_raw["rotate_mb"],
            keep_backups  = log_raw["keep_backups"],
        ),
        telegram   = TelegramSettings(
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


# ── Validation ────────────────────────────────────────────────────────────
def _validate(s: Settings) -> None:
    errors = []

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

    if s.telegram.enabled:
        if not s.telegram.bot_token:
            errors.append("telegram.enabled=true requires TELEGRAM_BOT_TOKEN in config/.env")
        if not s.telegram.chat_id:
            errors.append("telegram.enabled=true requires TELEGRAM_CHAT_ID in config/.env")

    if not s.strategies:
        errors.append("No strategies defined in settings.yaml")

    for name, st in s.strategies.items():
        if not st.enabled:
            continue
        prefix = f"strategies.{name}"
        if not st.strategy_id:
            errors.append(
                f"{prefix}: strategy_id is required (set a unique ID in settings.yaml)"
            )
        if st.tp1_atr <= st.sl_atr:
            errors.append(
                f"{prefix}: tp1_atr ({st.tp1_atr}) must be > sl_atr ({st.sl_atr}) "
                "to guarantee TP1-reached trades are net winners"
            )
        if st.trade_size <= 0:
            errors.append(f"{prefix}: trade_size must be > 0")
        if st.max_open_trades < 1:
            errors.append(f"{prefix}: max_open_trades must be >= 1")
        if st.daily_loss_limit_usdt <= 0:
            errors.append(f"{prefix}: daily_loss_limit_usdt must be > 0")
        if st.trailing_tp2 and st.trail_atr_mult <= 0:
            errors.append(
                f"{prefix}: trail_atr_mult must be > 0 when trailing_tp2=true"
            )

    if errors:
        msg = "\n".join(f"  • {e}" for e in errors)
        raise ValueError(f"Configuration errors:\n{msg}")
