"""
core/node_builder.py
────────────────────────────────────────────────────────────────────────
Multi-exchange refactor: builds one data client + (for non-dry_run
modes) one exec client per configured venue, using the adapter registry
in core/exchanges/. All venue-specific wiring (Binance testnet URLs,
the leverage monkey-patch, etc.) now lives in core/exchanges/binance.py
-- this module no longer imports anything Binance-specific.

Adding a new exchange requires zero changes here -- only a new adapter
module + registry entry in core/exchanges/__init__.py, plus a `venues:`
entry in settings.yaml.

Stage 6A (NT native position/open-order reconciliation) unchanged.
"""

from __future__ import annotations

import logging

from nautilus_trader.config import (
    CacheConfig,
    DatabaseConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId

from core.config import Settings
from core.exchanges import get_adapter

logger = logging.getLogger(__name__)


def build_node(settings: Settings) -> TradingNode:
    logger.info(
        "Building TradingNode  mode=%s  trader_id=%s  venues=%s",
        settings.mode, settings.trader_id, list(settings.venues),
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
    # Keeps NT's own portfolio state consistent with the exchange,
    # independent of our LedgerReconciler (risk/reconciler.py).
    exec_engine_cfg = LiveExecEngineConfig(
        reconciliation               = True,
        position_check_interval_secs = 60.0,
        open_check_interval_secs     = 60.0,
    )

    # ── Group symbol settings by venue, so each adapter can build its
    #    exec client with leverage/margin for every symbol traded there
    #    (multiple strategies may share a symbol on the same venue) ──────
    symbols_by_venue: dict[str, dict] = {}
    for (venue_key, symbol), sym_cfg in settings.symbols.items():
        symbols_by_venue.setdefault(venue_key, {})[symbol] = sym_cfg

    data_clients: dict = {}
    exec_clients: dict = {}
    data_factories: list = []
    exec_factories: list = []

    for venue_key in settings.venues:
        adapter  = get_adapter(venue_key)
        nt_venue = adapter.venue_name
        creds    = settings.credentials_for(venue_key)

        data_clients[nt_venue] = adapter.build_data_client_cfg(creds, settings.is_paper)
        data_factories.append((nt_venue, adapter.data_client_factory()))

        if not settings.is_dry_run:
            venue_symbols = symbols_by_venue.get(venue_key, {})
            exec_clients[nt_venue] = adapter.build_exec_client_cfg(
                creds, settings.is_paper, venue_symbols,
                position_mode=settings.position_mode_for(venue_key),
            )
            exec_factories.append((nt_venue, adapter.exec_client_factory()))
            logger.info(
                "Exec client configured  venue=%s  mode=%s  symbols=%s  position_mode=%s",
                nt_venue, settings.mode, list(venue_symbols),
                settings.position_mode_for(venue_key),
            )
        else:
            logger.info("Exec client disabled  venue=%s  (dry_run)", nt_venue)

    node_cfg = TradingNodeConfig(
        trader_id              = TraderId(settings.trader_id),
        cache                  = cache_cfg,
        logging                = nt_logging,
        risk_engine            = risk_cfg,
        exec_engine            = exec_engine_cfg,
        data_clients           = data_clients,
        exec_clients           = exec_clients,
        timeout_connection     = 30.0,
        timeout_reconciliation = 10.0,
        timeout_portfolio      = 10.0,
        timeout_disconnection  = 5.0,
    )

    node = TradingNode(config=node_cfg)
    for nt_venue, factory in data_factories:
        node.add_data_client_factory(nt_venue, factory)
    for nt_venue, factory in exec_factories:
        node.add_exec_client_factory(nt_venue, factory)
        logger.info("Exec client factory registered  venue=%s", nt_venue)

    logger.info(
        "TradingNode built  trader_id=%s  redis=%s:%d  venues=%s",
        settings.trader_id, settings.redis.host, settings.redis.port,
        list(data_clients),
    )
    return node
