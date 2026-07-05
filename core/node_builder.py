"""
core/node_builder.py
────────────────────────────────────────────────────────────────────────
Stage 6A: Enable NT native position + open-order reconciliation.
  • Added LiveExecEngineConfig with:
      position_check_interval_secs=60.0  (was None — disabled)
      open_check_interval_secs=60.0      (was None — disabled)
  This makes NT periodically verify its own internal portfolio state
  against the Binance exchange position. Keeps NT's view self-healing
  independent of our custom LedgerReconciler.

All Stage 5 changes retained (three testnet URL params, InstrumentProviderConfig
load_all=True on both clients, exec client for paper/live).
"""

import logging

from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
from nautilus_trader.adapters.binance.common.symbol import BinanceSymbol
from nautilus_trader.adapters.binance.config import BinanceDataClientConfig
from nautilus_trader.adapters.binance.factories import BinanceLiveDataClientFactory
from nautilus_trader.adapters.binance.futures.enums import BinanceFuturesMarginType
from nautilus_trader.config import (
    CacheConfig,
    DatabaseConfig,
    InstrumentProviderConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId

from core.config import Settings

logger = logging.getLogger(__name__)

# ── Monkey-patch: Binance Futures leverage init crash (NT v1.228) ──────
#
# BinanceFuturesExecClient._update_account_state() calls
# query_futures_symbol_config() with no symbol filter, then iterates ALL
# returned symbols calling account.set_leverage().  If any symbol has
# leverage=0 (common on testnet for unused symbols), set_leverage() raises
# ValueError("leverage was not >= 1").  The except KeyError handler at line
# 241 is dead code ( _get_cached_instrument_id() never raises ), so the
# ValueError propagates and kills _connect().
#
# Fixed upstream in NT v1.229.0 (PR #4289).  Remove this block after upgrade.
import nautilus_trader.adapters.binance.futures.execution as _futures_exec

_orig_update_account_state = _futures_exec.BinanceFuturesExecutionClient._update_account_state

async def _patched_update_account_state(self):
    try:
        await _orig_update_account_state(self)
    except ValueError as e:
        if "leverage was not >= 1" in str(e):
            logger.warning(
                "Ignored invalid leverage during connect (testnet symbol config quirk): %s", e,
            )
        else:
            raise

_futures_exec.BinanceFuturesExecutionClient._update_account_state = _patched_update_account_state

_TESTNET_HTTP       = "https://testnet.binancefuture.com"
_TESTNET_WS_MARKET  = "wss://fstream.binancefuture.com/market"
_TESTNET_WS_PRIVATE = "wss://fstream.binancefuture.com/private"
_TESTNET_WS_API     = "wss://testnet.binancefuture.com/ws-fapi/v1"


def build_node(settings: Settings) -> TradingNode:
    logger.info(
        "Building TradingNode  mode=%s  trader_id=%s",
        settings.mode, settings.trader_id,
    )

    cache_cfg = CacheConfig(
        database=DatabaseConfig(
            type               = "redis",
            host               = settings.redis.host,
            port               = settings.redis.port,
            connection_timeout = settings.redis.timeout_secs,
            response_timeout   = settings.redis.timeout_secs,
        )
    )

    nt_logging = LoggingConfig(
        log_level      = settings.logging.level,
        log_level_file = settings.logging.level_file,
        log_directory  = settings.logging.log_dir + "/",
        log_file_name  = settings.logging.log_file_name,
        log_colors     = True,
    )

    risk_cfg = LiveRiskEngineConfig(bypass=False)

    # ── Stage 6A: NT native reconciliation ───────────────────────────────
    # position_check and open_check keep NT's own portfolio state
    # consistent with the exchange independently of our LedgerReconciler.
    # Previously both were None (disabled), so NT never verified its own
    # position state after startup.
    exec_engine_cfg = LiveExecEngineConfig(
        reconciliation               = True,   # already default, explicit for clarity
        position_check_interval_secs = 60.0,   # was None
        open_check_interval_secs     = 60.0,   # was None
    )

    # ── Binance DATA client ───────────────────────────────────────────────
    data_client_cfg = BinanceDataClientConfig(
        api_key             = settings.active_api_key or "",
        api_secret          = settings.active_api_secret or "",
        account_type        = BinanceAccountType.USDT_FUTURES,
        base_url_http       = _TESTNET_HTTP if settings.is_paper else None,
        base_url_ws         = _TESTNET_WS_MARKET  if settings.is_paper else None,
        instrument_provider = InstrumentProviderConfig(load_all=True),
    )

    # ── Binance EXEC client (paper/live only) ─────────────────────────────
    exec_clients: dict = {}

    if not settings.is_dry_run:
        from nautilus_trader.adapters.binance.config import BinanceExecClientConfig
        from nautilus_trader.adapters.binance.factories import BinanceLiveExecClientFactory

        http_url    = _TESTNET_HTTP    if settings.is_paper else None
        ws_market   = _TESTNET_WS_MARKET  if settings.is_paper else None
        ws_private  = _TESTNET_WS_PRIVATE if settings.is_paper else None
        ws_api      = _TESTNET_WS_API     if settings.is_paper else None

        symbol = BinanceSymbol(settings.instrument.symbol)

        exec_leverages = {symbol: settings.futures.leverage}
        exec_margin_types = None
        if settings.futures.margin_type is not None:
            exec_margin_types = {
                symbol: BinanceFuturesMarginType(settings.futures.margin_type),
            }

        exec_client_cfg = BinanceExecClientConfig(
            api_key             = settings.active_api_key,
            api_secret          = settings.active_api_secret,
            account_type        = BinanceAccountType.USDT_FUTURES,
            base_url_http       = http_url,
            base_url_ws         = ws_api,       # WS API (session.logon) — not used in HMAC mode
            base_url_ws_stream  = ws_private,   # user data stream (ACCOUNT_UPDATE, etc.)
            instrument_provider = InstrumentProviderConfig(load_all=True),
            futures_leverages    = exec_leverages,
            futures_margin_types = exec_margin_types,
        )
        exec_clients = {"BINANCE": exec_client_cfg}

        logger.info(
            "Exec client configured  mode=%s  http=%s  ws=%s  ws_stream=%s",
            settings.mode,
            http_url or "production",
            ws_api   or "production",
            ws_private or "production",
        )
    else:
        logger.info("Exec client disabled  (dry_run)")

    node_cfg = TradingNodeConfig(
        trader_id              = TraderId(settings.trader_id),
        cache                  = cache_cfg,
        logging                = nt_logging,
        risk_engine            = risk_cfg,
        exec_engine            = exec_engine_cfg,           # Stage 6A
        data_clients           = {"BINANCE": data_client_cfg},
        exec_clients           = exec_clients,
        timeout_connection     = 30.0,
        timeout_reconciliation = 10.0,
        timeout_portfolio      = 10.0,
        timeout_disconnection  = 5.0,
    )

    node = TradingNode(config=node_cfg)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)

    if not settings.is_dry_run:
        node.add_exec_client_factory("BINANCE", BinanceLiveExecClientFactory)
        logger.info("Exec client factory registered — BINANCE")

    logger.info(
        "Data client configured  ws_market=%s",
        _TESTNET_WS_MARKET if settings.is_paper else "production",
    )

    logger.info(
        "TradingNode built  trader_id=%s  redis=%s:%d",
        settings.trader_id,
        settings.redis.host,
        settings.redis.port,
    )
    return node
