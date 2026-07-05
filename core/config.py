"""
core/config.py
────────────────────────────────────────────────────────────────────────
Stage 6: Added ReconciliationSettings dataclass.
Controls the LedgerReconciler (grace period, tolerance).
Loaded from the `reconciliation:` YAML block.
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


@dataclass
class StrategySettingsBase:
    strategy_id:           str
    primary_bar:           str
    htf_bar:               str
    trade_size:            float
    sl_atr:                float
    tp1_atr:               float
    tp2_atr:               float
    max_open_trades:       int
    daily_loss_limit_usdt: float
    min_free_margin_usdt:  float
    enabled:               bool
    htf_filter:            bool
    htf_period:            int
    trailing_tp2:          bool
    trail_atr_mult:        float
    breakeven_sl:          bool
    enable_exit_signal:    bool
    enable_sl:             bool
    warmup_bars:           int
    htf_warmup_bars:       int

    def build_config(self, strategy_id, instrument_id, state_dir, mode, primary_bar, htf_bar):
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement build_config(). "
            "See MsSettings or FvgSettings for the pattern."
        )


# ── Stage 6 ───────────────────────────────────────────────────────────────
@dataclass
class ReconciliationSettings:
    """
    Controls the LedgerReconciler. All strategies share one reconciler
    since under NETTING there is one blended exchange position.

    grace_secs:    seconds after a ledger mutation before the next check
                   runs. Prevents false positives during in-flight orders.
    tolerance_btc: differences smaller than this (rounding/fees) are OK.
    enabled:       false = reconciler disabled entirely (dry_run always
                   skips regardless of this flag).
    """
    enabled:       bool  = True
    grace_secs:    float = 15.0
    tolerance_btc: float = 0.0001


@dataclass
class FuturesSettings:
    """
    Leverage + margin type applied to the instrument symbol on Binance
    Futures.  Leverage can be changed at any time (safe with open
    positions).  Margin type can ONLY be changed when the position
    for that symbol is ZERO — Binance rejects the call with error
    -4046 if a position exists.

    If ``margin_type`` is None (omitted from YAML), no margin-type
    API call is made during startup; Nautilus uses whatever the
    exchange account currently has configured.
    """
    leverage:    int
    margin_type: str | None = None


@dataclass
class InstrumentSettings:
    symbol:       str
    nt_id:        str
    venue:        str
    account_type: str


@dataclass
class MarketLotSizeSettings:
    min_qty:  float = 0.001
    step_size: float = 0.001


@dataclass
class MinNotionalSettings:
    notional: float = 50.0


@dataclass
class SymbolFilterSettings:
    market_lot_size: MarketLotSizeSettings
    min_notional:    MinNotionalSettings


@dataclass
class ExchangeFiltersSettings:
    """Per-symbol exchange filter fallbacks.
    Populated from the ``exchange_filters:`` YAML block.
    The API (GET /fapi/v1/exchangeInfo) is always tried first;
    these values are used only if the API call fails.
    """
    symbols: dict[str, SymbolFilterSettings]  # keyed by symbol e.g. "BTCUSDT"


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


@dataclass
class Settings:
    mode:              str
    instrument:        InstrumentSettings
    futures:           FuturesSettings
    strategies:        Dict[str, StrategySettingsBase]
    redis:             RedisSettings
    logging:           LoggingSettings
    telegram:          TelegramSettings
    trader_id:         str
    reconciliation:    ReconciliationSettings = None  # Stage 6; default below
    exchange_filters:  ExchangeFiltersSettings | None = None
    binance_api_key:            str = ""
    binance_api_secret:         str = ""
    binance_testnet_api_key:    str = ""
    binance_testnet_api_secret: str = ""

    def __post_init__(self):
        if self.reconciliation is None:
            self.reconciliation = ReconciliationSettings()

    @property
    def is_live(self) -> bool: return self.mode == "live"

    @property
    def is_paper(self) -> bool: return self.mode == "paper"

    @property
    def is_dry_run(self) -> bool: return self.mode == "dry_run"

    def symbol_filters(self, symbol: str) -> SymbolFilterSettings | None:
        """Return exchange filter fallback for *symbol*, or None."""
        if self.exchange_filters is None:
            return None
        return self.exchange_filters.symbols.get(symbol)

    @property
    def active_api_key(self) -> str:
        return self.binance_testnet_api_key if self.is_paper else self.binance_api_key

    @property
    def active_api_secret(self) -> str:
        return self.binance_testnet_api_secret if self.is_paper else self.binance_api_secret

    @property
    def enabled_strategies(self) -> Dict[str, StrategySettingsBase]:
        return {k: v for k, v in self.strategies.items() if v.enabled}


def load_settings(config_dir=None, settings_file="settings.yaml") -> Settings:
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

    from strategies import REGISTRY
    strategies: Dict[str, StrategySettingsBase] = {}
    for name, s in raw.get("strategies", {}).items():
        entry = REGISTRY.get(name)
        if entry is None:
            raise ValueError(
                f"Unknown strategy '{name}'. Available: {list(REGISTRY)}. "
                "Add a REGISTRY entry in strategies/__init__.py"
            )
        settings_cls = entry["settings"]
        known = {f.name for f in fields(settings_cls)}
        extra = [k for k in s if k not in known]
        if extra:
            log.warning("strategies.%s: unknown key(s) %s — ignored.", name, extra)
        strategies[name] = settings_cls(**{k: v for k, v in s.items() if k in known})

    # ── Futures leverage / margin type ─────────────────────────────────────
    futures_raw = raw.get("futures", {})
    futures = FuturesSettings(
        leverage    = futures_raw["leverage"],
        margin_type = futures_raw.get("margin_type"),  # None → no API call
    )

    # ── Reconciliation block (optional — defaults used if absent) ─────────
    rec_raw = raw.get("reconciliation", {})
    reconciliation = ReconciliationSettings(
        enabled       = rec_raw.get("enabled",       True),
        grace_secs    = rec_raw.get("grace_secs",    15.0),
        tolerance_btc = rec_raw.get("tolerance_btc", 0.0001),
    )

    # ── Exchange filters fallback ──────────────────────────────────────────
    ef_raw = raw.get("exchange_filters", {})
    exchange_filters = None
    if ef_raw:
        parsed_symbols = {}
        for sym, sym_raw in ef_raw.items():
            mls_raw  = sym_raw.get("market_lot_size", {})
            mn_raw   = sym_raw.get("min_notional", {})
            parsed_symbols[sym.upper()] = SymbolFilterSettings(
                market_lot_size = MarketLotSizeSettings(
                    min_qty   = mls_raw.get("min_qty", 0.001),
                    step_size = mls_raw.get("step_size", 0.001),
                ),
                min_notional = MinNotionalSettings(
                    notional = mn_raw.get("notional", 50.0),
                ),
            )
        exchange_filters = ExchangeFiltersSettings(symbols=parsed_symbols)

    inst_raw  = raw["instrument"]
    redis_raw = raw["redis"]
    log_raw   = raw["logging"]
    tg_raw    = raw["telegram"]

    settings = Settings(
        mode           = mode,
        trader_id      = raw["trader_id"],
        futures        = futures,
        reconciliation = reconciliation,
        exchange_filters = exchange_filters,
        instrument = InstrumentSettings(
            **{k: inst_raw[k] for k in ["symbol", "nt_id", "venue", "account_type"]}
        ),
        strategies = strategies,
        redis = RedisSettings(
            host         = os.getenv("REDIS_HOST", redis_raw["host"]),
            port         = int(os.getenv("REDIS_PORT", redis_raw["port"])),
            timeout_secs = redis_raw["timeout_secs"],
        ),
        logging = LoggingSettings(
            level         = log_raw["level"],
            level_file    = log_raw["level_file"],
            log_dir       = log_raw["log_dir"],
            log_file_name = log_raw["log_file_name"],
            rotate_mb     = log_raw["rotate_mb"],
            keep_backups  = log_raw["keep_backups"],
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
            errors.append(f"{prefix}: strategy_id is required")
        if st.tp1_atr <= st.sl_atr:
            errors.append(f"{prefix}: tp1_atr ({st.tp1_atr}) must be > sl_atr ({st.sl_atr})")
        if st.trade_size <= 0:
            errors.append(f"{prefix}: trade_size must be > 0")
        if st.max_open_trades < 1:
            errors.append(f"{prefix}: max_open_trades must be >= 1")
        if st.daily_loss_limit_usdt <= 0:
            errors.append(f"{prefix}: daily_loss_limit_usdt must be > 0")
        if st.trailing_tp2 and st.trail_atr_mult <= 0:
            errors.append(f"{prefix}: trail_atr_mult must be > 0 when trailing_tp2=true")
        if st.min_free_margin_usdt < 0:
            errors.append(f"{prefix}: min_free_margin_usdt must be >= 0 (use 0.0 to disable)")
    if s.futures.leverage < 1:
        errors.append("futures.leverage must be >= 1")
    if s.futures.margin_type is not None and s.futures.margin_type not in {"CROSSED", "ISOLATED"}:
        errors.append("futures.margin_type must be 'CROSSED' or 'ISOLATED' when present")
    if s.reconciliation.grace_secs < 0:
        errors.append("reconciliation.grace_secs must be >= 0")
    if s.reconciliation.tolerance_btc < 0:
        errors.append("reconciliation.tolerance_btc must be >= 0")
    if errors:
        msg = "\n".join(f"  • {e}" for e in errors)
        raise ValueError(f"Configuration errors:\n{msg}")
