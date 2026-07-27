"""
core/config.py
────────────────────────────────────────────────────────────────────────
Multi-exchange refactor:
  • Removed the single global `instrument:`/`futures:` blocks. Replaced
    with a `venues:` block (connection-level: account type; credentials
    resolved by env-var convention -- see VenueCredentials) and a
    `symbols:` block keyed by "venue:SYMBOL" (exchange-account-level:
    nt_id, leverage, margin type, exchange filter fallbacks).
  • Every strategy now declares its own `venue:` and `symbol:` in its
    YAML block. Multiple strategies may share a (venue, symbol) pair
    (as MS + FVG currently share BTCUSDT on Binance) -- leverage/margin
    are defined once per (venue, symbol), not per strategy, since
    they're properties of the exchange account, not the strategy.
  • ReconciliationSettings unchanged from Stage 6 -- grouping by
    (venue, instrument) now happens in risk/reconciler.py.

Env var convention for venue credentials (see config/.env.example):
    {VENUE}_API_KEY / {VENUE}_API_SECRET                   (live)
    {VENUE}_TESTNET_API_KEY / {VENUE}_TESTNET_API_SECRET   (paper)
  e.g. venue "binance" -> BINANCE_API_KEY, BINANCE_TESTNET_API_KEY, ...
  Adding a new venue to YAML automatically looks for its own env vars --
  no code changes needed here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, Tuple

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)


@dataclass
class StrategySettingsBase:
    strategy_id:           str
    venue:                 str    # e.g. "binance" -- must match a key under `venues:`
    symbol:                str    # e.g. "BTCUSDT" -- must match a "venue:SYMBOL" key under `symbols:`
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

    def build_config(self, strategy_id, instrument_id, venue, position_mode, state_dir, mode, primary_bar, htf_bar):
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement build_config(). "
            "See MsSettings or FvgSettings for the pattern."
        )


# ── Stage 6 ───────────────────────────────────────────────────────────────
@dataclass
class ReconciliationSettings:
    """
    Controls the LedgerReconciler. Grouped by (venue, instrument) as of
    the multi-exchange refactor -- strategies trading different symbols
    or different venues no longer share one exposure check.

    grace_secs:    seconds after a ledger mutation before the next check
                   runs. Prevents false positives during in-flight orders.
    tolerance_btc: differences smaller than this (rounding/fees) are OK.
                   Named `_btc` for historical reasons -- applies to the
                   base-asset quantity of whichever instrument a group
                   is tracking, not literally BTC.
    enabled:       false = reconciler disabled entirely (dry_run always
                   skips regardless of this flag).
    """
    enabled:       bool  = True
    grace_secs:    float = 15.0
    tolerance_btc: float = 0.0001


@dataclass
class VenueSettings:
    """
    Connection-level settings for one exchange venue.
    Keyed by venue name (lowercase) under the `venues:` YAML block.

    position_mode: "netting" (default) or "hedge". Binance's position
    mode is account-wide (applies to every symbol on that account, per
    POST /fapi/v1/positionSide/dual) -- so this lives here, per venue,
    not per symbol or per strategy. Switching an existing live venue
    from netting to hedge (or back) requires flattening ALL positions
    and canceling ALL open orders on that account first -- Binance
    rejects the mode-change call otherwise. This system never switches
    it automatically; it only verifies the account's actual mode
    matches this setting at startup and refuses to start if not (see
    core/exchanges/binance.py's verify_position_mode()).
    """
    account_type:  str
    position_mode: str = "netting"


@dataclass
class VenueCredentials:
    """
    API credentials for one venue, resolved from environment variables
    by naming convention: {VENUE}_API_KEY / {VENUE}_API_SECRET (live),
    {VENUE}_TESTNET_API_KEY / {VENUE}_TESTNET_API_SECRET (paper).
    """
    api_key:            str = ""
    api_secret:         str = ""
    testnet_api_key:    str = ""
    testnet_api_secret: str = ""

    def active(self, is_paper: bool) -> Tuple[str, str]:
        if is_paper:
            return self.testnet_api_key, self.testnet_api_secret
        return self.api_key, self.api_secret


@dataclass
class MarketLotSizeSettings:
    min_qty:  float = 0.001
    step_size: float = 0.001


@dataclass
class MinNotionalSettings:
    notional: float = 50.0


@dataclass
class SymbolSettings:
    """
    Exchange-account-level settings for one (venue, symbol) pair.
    Keyed by "venue:SYMBOL" (e.g. "binance:BTCUSDT") under the
    `symbols:` YAML block. Shared by every strategy trading that
    symbol on that venue -- leverage/margin type are properties of
    the exchange account+symbol, not of any one strategy.
    """
    nt_id:           str
    leverage:        int
    margin_type:     str | None = None
    market_lot_size: MarketLotSizeSettings = field(default_factory=MarketLotSizeSettings)
    min_notional:    MinNotionalSettings   = field(default_factory=MinNotionalSettings)


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
    venues:            Dict[str, VenueSettings]
    symbols:           Dict[Tuple[str, str], SymbolSettings]   # key: (venue_lower, SYMBOL_upper)
    strategies:        Dict[str, StrategySettingsBase]
    redis:             RedisSettings
    logging:           LoggingSettings
    telegram:          TelegramSettings
    trader_id:         str
    reconciliation:    ReconciliationSettings = None  # Stage 6; default below
    venue_credentials: Dict[str, VenueCredentials] = field(default_factory=dict)

    def __post_init__(self):
        if self.reconciliation is None:
            self.reconciliation = ReconciliationSettings()

    @property
    def is_live(self) -> bool: return self.mode == "live"

    @property
    def is_paper(self) -> bool: return self.mode == "paper"

    @property
    def is_dry_run(self) -> bool: return self.mode == "dry_run"

    def symbol_settings(self, venue: str, symbol: str) -> SymbolSettings:
        """
        Look up the (venue, symbol) exchange-account settings. Raises if
        missing -- every strategy's (venue, symbol) pair must have a
        corresponding entry under `symbols:` in settings.yaml.
        """
        key = (venue.lower(), symbol.upper())
        settings = self.symbols.get(key)
        if settings is None:
            raise ValueError(
                f"No 'symbols' entry for '{venue.lower()}:{symbol.upper()}'. "
                "Add one under the 'symbols:' block in settings.yaml."
            )
        return settings

    def credentials_for(self, venue: str) -> Tuple[str, str]:
        """Return (api_key, api_secret) active for the current mode."""
        creds = self.venue_credentials.get(venue.lower())
        if creds is None:
            return "", ""
        return creds.active(self.is_paper)

    def position_mode_for(self, venue: str) -> str:
        """Return "netting" or "hedge" for the given venue."""
        v = self.venues.get(venue.lower())
        return v.position_mode if v is not None else "netting"

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

    # ── Venues ──────────────────────────────────────────────────────────────
    venues_raw = raw.get("venues", {})
    venues: Dict[str, VenueSettings] = {}
    venue_credentials: Dict[str, VenueCredentials] = {}
    for vname, v in venues_raw.items():
        vkey = vname.lower()
        venues[vkey] = VenueSettings(
            account_type  = v["account_type"],
            position_mode = v.get("position_mode", "netting"),
        )
        env_prefix = vname.upper()
        venue_credentials[vkey] = VenueCredentials(
            api_key            = os.getenv(f"{env_prefix}_API_KEY", ""),
            api_secret         = os.getenv(f"{env_prefix}_API_SECRET", ""),
            testnet_api_key    = os.getenv(f"{env_prefix}_TESTNET_API_KEY", ""),
            testnet_api_secret = os.getenv(f"{env_prefix}_TESTNET_API_SECRET", ""),
        )

    # ── Symbols (per venue+symbol exchange-account settings) ────────────────
    symbols_raw = raw.get("symbols", {})
    symbols: Dict[Tuple[str, str], SymbolSettings] = {}
    for key, s in symbols_raw.items():
        if ":" not in key:
            raise ValueError(
                f"'symbols' key '{key}' must be formatted 'venue:SYMBOL' "
                "(e.g. 'binance:BTCUSDT')."
            )
        vname, sym = key.split(":", 1)
        mls_raw = s.get("market_lot_size", {})
        mn_raw  = s.get("min_notional", {})
        symbols[(vname.lower(), sym.upper())] = SymbolSettings(
            nt_id       = s["nt_id"],
            leverage    = s["leverage"],
            margin_type = s.get("margin_type"),
            market_lot_size = MarketLotSizeSettings(
                min_qty   = mls_raw.get("min_qty", 0.001),
                step_size = mls_raw.get("step_size", 0.001),
            ),
            min_notional = MinNotionalSettings(
                notional = mn_raw.get("notional", 50.0),
            ),
        )

    # ── Strategies ────────────────────────────────────────────────────────
    # The YAML key (e.g. "ms", "ms_eth") is the strategy *instance* name --
    # used for logging, Telegram, state-file naming, and reconciler
    # bookkeeping. It no longer has to match a REGISTRY key: an optional
    # `type:` field selects which strategy class/settings to use, so the
    # same strategy type can run multiple instances (e.g. MS on both
    # BTCUSDT and ETHUSDT). If `type:` is omitted, the instance name
    # itself is used as the type (backward compatible with existing
    # configs where e.g. the "ms" block implicitly means type "ms").
    from strategies import REGISTRY
    strategies: Dict[str, StrategySettingsBase] = {}
    for name, s in raw.get("strategies", {}).items():
        strategy_type = s.get("type", name)
        entry = REGISTRY.get(strategy_type)
        if entry is None:
            raise ValueError(
                f"strategies.{name}: unknown type '{strategy_type}'. "
                f"Available: {list(REGISTRY)}. Add a REGISTRY entry in "
                "strategies/__init__.py, or fix the 'type:' field."
            )
        settings_cls = entry["settings"]
        known = {f.name for f in fields(settings_cls)}
        extra = [k for k in s if k not in known and k != "type"]
        if extra:
            log.warning("strategies.%s: unknown key(s) %s — ignored.", name, extra)
        strategies[name] = settings_cls(**{k: v for k, v in s.items() if k in known})

    # ── Reconciliation block (optional — defaults used if absent) ─────────
    rec_raw = raw.get("reconciliation", {})
    reconciliation = ReconciliationSettings(
        enabled       = rec_raw.get("enabled",       True),
        grace_secs    = rec_raw.get("grace_secs",    15.0),
        tolerance_btc = rec_raw.get("tolerance_btc", 0.0001),
    )

    redis_raw = raw["redis"]
    log_raw   = raw["logging"]
    tg_raw    = raw["telegram"]

    settings = Settings(
        mode              = mode,
        trader_id         = raw["trader_id"],
        venues            = venues,
        symbols           = symbols,
        venue_credentials = venue_credentials,
        reconciliation    = reconciliation,
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
    )
    _validate(settings)
    return settings


def _validate(s: Settings) -> None:
    errors = []

    if not s.venues:
        errors.append("No venues defined in settings.yaml — add a 'venues:' block.")
    if not s.strategies:
        errors.append("No strategies defined in settings.yaml")

    for vkey, v in s.venues.items():
        if v.position_mode not in {"netting", "hedge"}:
            errors.append(
                f"venues.{vkey}: position_mode must be 'netting' or 'hedge' "
                f"(got '{v.position_mode}')"
            )

    if s.telegram.enabled:
        if not s.telegram.bot_token:
            errors.append("telegram.enabled=true requires TELEGRAM_BOT_TOKEN in config/.env")
        if not s.telegram.chat_id:
            errors.append("telegram.enabled=true requires TELEGRAM_CHAT_ID in config/.env")

    for name, st in s.strategies.items():
        if not st.enabled:
            continue
        prefix = f"strategies.{name}"
        if not st.strategy_id:
            errors.append(f"{prefix}: strategy_id is required")

        if not st.venue:
            errors.append(f"{prefix}: venue is required")
        elif st.venue.lower() not in s.venues:
            errors.append(
                f"{prefix}: venue '{st.venue}' not defined under 'venues:' in settings.yaml"
            )

        if not st.symbol:
            errors.append(f"{prefix}: symbol is required")
        elif st.venue and (st.venue.lower(), st.symbol.upper()) not in s.symbols:
            errors.append(
                f"{prefix}: no 'symbols' entry for "
                f"'{st.venue.lower()}:{st.symbol.upper()}' — "
                "add one under the 'symbols:' block in settings.yaml"
            )

        if st.venue and st.venue.lower() in s.venues and not s.is_dry_run:
            creds = s.venue_credentials.get(st.venue.lower())
            key, secret = creds.active(s.is_paper) if creds else ("", "")
            if not key or not secret:
                env_kind = "TESTNET_" if s.is_paper else ""
                env_prefix = st.venue.upper()
                errors.append(
                    f"{prefix}: mode={s.mode} requires "
                    f"{env_prefix}_{env_kind}API_KEY / {env_prefix}_{env_kind}API_SECRET "
                    "in config/.env"
                )

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

    venue_symbol_strats: dict[tuple[str, str], list[str]] = {}
    for name, st in s.strategies.items():
        if not st.enabled:
            continue
        key = (st.venue.lower(), st.symbol.upper())
        venue_symbol_strats.setdefault(key, []).append(name)

    for (venue, symbol), strat_names in venue_symbol_strats.items():
        if len(strat_names) < 2:
            continue
        v = s.venues.get(venue)
        if v is not None and v.position_mode == "netting":
            errors.append(
                f"venues.{venue}: position_mode='netting' but "
                f"multiple strategies share symbol '{symbol}': "
                f"{', '.join(strat_names)}. "
                f"Set position_mode='hedge' or keep one strategy per symbol."
            )

    for (vkey, sym), sym_cfg in s.symbols.items():
        sprefix = f"symbols.{vkey}:{sym}"
        if sym_cfg.leverage < 1:
            errors.append(f"{sprefix}: leverage must be >= 1")
        if sym_cfg.margin_type is not None and sym_cfg.margin_type not in {"CROSSED", "ISOLATED"}:
            errors.append(f"{sprefix}: margin_type must be 'CROSSED' or 'ISOLATED' when present")

    if s.reconciliation.grace_secs < 0:
        errors.append("reconciliation.grace_secs must be >= 0")
    if s.reconciliation.tolerance_btc < 0:
        errors.append("reconciliation.tolerance_btc must be >= 0")

    if errors:
        msg = "\n".join(f"  • {e}" for e in errors)
        raise ValueError(f"Configuration errors:\n{msg}")
