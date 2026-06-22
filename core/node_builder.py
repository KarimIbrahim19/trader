"""
core/node_builder.py
──────────────────────────────────────────────────────────────────────
Builds the NautilusTrader TradingNode from Settings.

All imports and parameter names below are verified against
NautilusTrader 1.228.0 (the version installed in production).

Key API differences from earlier NautilusTrader versions:
  • BinanceDataClientConfig / BinanceLiveDataClientFactory   (unified, not futures-specific)
  • BinanceAccountType.USDT_FUTURES                         (not USDT_FUTURE)
  • DatabaseConfig uses connection_timeout / response_timeout (not timeout)
  • LoggingConfig uses log_directory / log_file_name         (not log_file_path)
  • LiveRiskEngineConfig required in live environment        (not RiskEngineConfig)

Stage gates:
  Stage 1  Infrastructure stub — node not started
  Stage 2  Binance DATA client added                ← current
  Stage 3  Strategy + persistence added
  Stage 4  TelegramActor added
  Stage 5  Binance EXEC client added
"""

import logging

from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
from nautilus_trader.adapters.binance.config import BinanceDataClientConfig
from nautilus_trader.adapters.binance.factories import BinanceLiveDataClientFactory
from nautilus_trader.config import (
    CacheConfig,
    DatabaseConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId

from core.config import Settings

logger = logging.getLogger(__name__)


def build_node(settings: Settings) -> TradingNode:
    """
    Construct and return a fully configured TradingNode.

    The node is NOT started here — call node.build() then node.run()
    from main.py after strategies have been added.
    """
    logger.info("Building TradingNode  mode=%s", settings.mode)

    # ── Redis cache ────────────────────────────────────────────────────
    cache_cfg = CacheConfig(
        database=DatabaseConfig(
            type               = "redis",
            host               = settings.redis.host,
            port               = settings.redis.port,
            connection_timeout = settings.redis.timeout_secs,
            response_timeout   = settings.redis.timeout_secs,
        )
    )

    # ── NautilusTrader internal logging ───────────────────────────────
    nt_logging = LoggingConfig(
        log_level      = settings.logging.level,
        log_level_file = settings.logging.level_file,
        log_directory  = settings.logging.log_dir + "/",
        log_file_name  = settings.logging.log_file_name,
        log_colors     = True,
    )

    # ── Risk engine ────────────────────────────────────────────────────
    # Must use LiveRiskEngineConfig (not RiskEngineConfig) in live env
    risk_cfg = LiveRiskEngineConfig(bypass=False)

    # ── Binance Futures DATA client ────────────────────────────────────
    # Instrument loading is triggered lazily when the strategy calls
    # self.subscribe_bars() in on_start(). A "No loading configured"
    # warning appears in the log — this is informational only. The
    # subscriptions still succeed and bars arrive correctly.
    #
    # Note: InstrumentProviderConfig(load_ids=...) was tried but triggers
    # authenticated REST calls during _connect, failing with -2015 when
    # API keys are not yet configured. Lazy loading is the correct approach.
    data_client_cfg = BinanceDataClientConfig(
        api_key      = settings.active_api_key or "",
        api_secret   = settings.active_api_secret or "",
        account_type = BinanceAccountType.USDT_FUTURES,
    )

    # ── Node config ────────────────────────────────────────────────────
    node_cfg = TradingNodeConfig(
        trader_id   = TraderId("BTCTRADER-001"),
        cache       = cache_cfg,
        logging     = nt_logging,
        risk_engine = risk_cfg,
        data_clients = {"BINANCE": data_client_cfg},
        exec_clients = {},        # Stage 5: BinanceExecClientConfig added here
        timeout_connection     = 30.0,
        timeout_reconciliation = 10.0,
        timeout_portfolio      = 10.0,
        timeout_disconnection  = 5.0,
    )

    node = TradingNode(config=node_cfg)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    # Stage 5: node.add_exec_client_factory("BINANCE", BinanceLiveExecClientFactory)

    logger.info(
        "TradingNode built  trader_id=BTCTRADER-001  "
        "data_client=Binance(USDT_FUTURES)  redis=%s:%d",
        settings.redis.host,
        settings.redis.port,
    )
    return node