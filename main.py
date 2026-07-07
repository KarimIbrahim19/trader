"""
main.py
────────────────────────────────────────────────────────────────────────
Multi-exchange refactor:
  • Each strategy now resolves its own instrument_id from its own
    (venue, symbol) pair via settings.symbol_settings() -- there is no
    longer one global instrument shared by every strategy.
  • Exchange filter fallbacks are read per-strategy from that same
    SymbolSettings entry instead of one global exchange_filters block.
  • The reconciler is still one shared singleton (Stage 6), but it now
    groups internally by (venue, instrument_id) -- see risk/reconciler.py.
    register_strategy()/set_portfolio_fn() calls happen inside each
    strategy's on_start(), keyed by that strategy's own venue+instrument.

All Stage 4/5 wiring unchanged.
"""

import argparse
import logging
import sys
from pathlib import Path

from nautilus_trader.model.data import BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId

from core.config import Settings, StrategySettingsBase, load_settings
from core.logging_setup import setup_logging

logger = logging.getLogger("live_trader.main")

_TF_MAP: dict[str, tuple[int, BarAggregation]] = {
    "1m":  (1,  BarAggregation.MINUTE),
    "5m":  (5,  BarAggregation.MINUTE),
    "15m": (15, BarAggregation.MINUTE),
    "1h":  (1,  BarAggregation.HOUR),
    "4h":  (4,  BarAggregation.HOUR),
    "1d":  (1,  BarAggregation.DAY),
}

def _make_bar_type(instrument_id: InstrumentId, timeframe: str) -> BarType:
    tf = timeframe.lower()
    if tf not in _TF_MAP:
        raise ValueError(f"Unknown timeframe '{timeframe}'. Supported: {list(_TF_MAP)}")
    step, aggregation = _TF_MAP[tf]
    return BarType(
        instrument_id      = instrument_id,
        bar_spec           = BarSpecification(
            step        = step,
            aggregation = aggregation,
            price_type  = PriceType.LAST,
        ),
        aggregation_source = AggregationSource.EXTERNAL,
    )


def _build_strategy(
    name:          str,
    strat_settings: StrategySettingsBase,
    settings:      Settings,
    instrument_id: InstrumentId,
):
    # `name` is the arbitrary instance name (e.g. "ms_eth") -- it no
    # longer has to be a REGISTRY key. Resolve the strategy class by
    # matching strat_settings' concrete type instead (load_settings()
    # already picked that type via the strategy's `type:` field).
    from strategies import REGISTRY
    entry = next(
        (e for e in REGISTRY.values() if isinstance(strat_settings, e["settings"])),
        None,
    )
    if entry is None:
        raise ValueError(
            f"strategies.{name}: no REGISTRY entry matches settings type "
            f"{type(strat_settings).__name__}. Available: {list(REGISTRY)}."
        )
    primary_bar = _make_bar_type(instrument_id, strat_settings.primary_bar)
    htf_bar     = _make_bar_type(instrument_id, strat_settings.htf_bar)
    config = strat_settings.build_config(
        strategy_id   = strat_settings.strategy_id,
        instrument_id = instrument_id,
        venue         = strat_settings.venue,
        state_dir     = "state",
        mode          = settings.mode,
        primary_bar   = primary_bar,
        htf_bar       = htf_bar,
    )
    return entry["strategy"](config=config)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live SMC Algorithmic Trader")
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--check", action="store_true",
                   help="Run infrastructure check and exit")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    config_path = Path(args.config)
    try:
        settings = load_settings(config_path.parent, config_path.name)
    except (FileNotFoundError, ValueError) as e:
        print(f"\n[ERROR] Configuration failed:\n  {e}\n")
        sys.exit(1)

    setup_logging(settings.logging, project_root=Path(__file__).parent)

    logger.info("═" * 60)
    logger.info("  Live Trader starting")
    logger.info("  Trader ID  : %s", settings.trader_id)
    logger.info("  Mode       : %s", settings.mode)
    logger.info("  Venues     : %s", ", ".join(settings.venues) or "(none)")
    enabled = settings.enabled_strategies
    if enabled:
        for name, s in enabled.items():
            logger.info(
                "  Strategy   : %-6s  venue=%s  symbol=%s  primary=%s  htf=%s  "
                "htf_filter=%s  size=%s  max_open=%d",
                name.upper(), s.venue, s.symbol, s.primary_bar, s.htf_bar,
                s.htf_filter, s.trade_size, s.max_open_trades,
            )
    else:
        logger.warning("  No strategies enabled — check settings.yaml")

    rec = settings.reconciliation
    if settings.is_dry_run:
        logger.info(
            "  Reconciler : disabled (dry_run — no portfolio available)"
        )
    else:
        logger.info(
            "  Reconciler : %s  grace=%.0fs  tolerance=%.4f  (per-instrument base units)",
            "enabled" if rec.enabled else "disabled",
            rec.grace_secs, rec.tolerance_btc,
        )
    logger.info("═" * 60)

    if args.check:
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/check_infra.py",
             "--config", str(config_path)],
            cwd=Path(__file__).parent,
        )
        sys.exit(result.returncode)

    _check_redis(settings)

    # ── Telegram notifier ─────────────────────────────────────────────────
    from actors.telegram_actor import TelegramNotifier
    tg = settings.telegram
    notifier = TelegramNotifier(
        bot_token            = tg.bot_token,
        chat_id              = tg.chat_id,
        enabled              = tg.enabled,
        notify_signals       = tg.notify_signals,
        notify_entries       = tg.notify_entries,
        notify_exits         = tg.notify_exits,
        notify_daily_summary = tg.notify_daily_summary,
    )

    # ── Stage 6: reconciler ───────────────────────────────────────────────
    # Disabled in dry_run: no exec client → no portfolio → nothing to check.
    # Created before strategies so it can be injected via set_reconciler().
    reconciler = None
    if not settings.is_dry_run and rec.enabled:
        from risk.reconciler import LedgerReconciler
        reconciler = LedgerReconciler(
            grace_secs    = rec.grace_secs,
            tolerance_btc = rec.tolerance_btc,
            notifier      = notifier,
        )
        logger.info(
            "LedgerReconciler created  grace=%.0fs  tolerance=%.4f  (per-instrument base units)",
            rec.grace_secs, rec.tolerance_btc,
        )

    # ── Build TradingNode ─────────────────────────────────────────────────
    from core.node_builder import build_node
    node = build_node(settings)

    if not enabled:
        logger.error(
            "No strategies enabled in settings.yaml. "
            "Set at least one strategy's 'enabled: true' and restart."
        )
        sys.exit(1)

    # ── Build strategies, wire notifier + reconciler + exchange filters ───
    # Each strategy resolves its own instrument_id and exchange-filter
    # fallback from its own (venue, symbol) pair -- no shared global.
    for name, strat_cfg in enabled.items():
        try:
            sym_cfg       = settings.symbol_settings(strat_cfg.venue, strat_cfg.symbol)
            instrument_id = InstrumentId.from_str(sym_cfg.nt_id)

            strategy = _build_strategy(name, strat_cfg, settings, instrument_id)
            strategy.set_notifier(notifier, strategy_name=name)
            strategy.set_exchange_filters({
                "market_lot_size": {
                    "min_qty":   sym_cfg.market_lot_size.min_qty,
                    "step_size": sym_cfg.market_lot_size.step_size,
                },
                "min_notional": {
                    "notional": sym_cfg.min_notional.notional,
                },
            })
            if reconciler is not None:
                strategy.set_reconciler(reconciler)
            node.trader.add_strategy(strategy)
            logger.info(
                "Added strategy: %s  venue=%s  symbol=%s  instrument=%s  "
                "(notifier=%s  reconciler=%s)",
                name.upper(), strat_cfg.venue, strat_cfg.symbol, instrument_id,
                "on" if tg.enabled else "off",
                "on" if reconciler else "off",
            )
        except Exception as e:
            logger.error("Failed to build strategy '%s': %s", name, e)
            sys.exit(1)

    logger.info("Starting TradingNode — connecting to Binance Futures...")
    if settings.is_dry_run:
        logger.info("DRY_RUN: signals logged, no orders placed.")

    try:
        node.build()
        notifier.on_system_start(
            trader_id      = settings.trader_id,
            mode           = settings.mode,
            enabled_strats = enabled,
        )
        notifier.start_timers(
            heartbeat_mins    = tg.heartbeat_interval_mins,
            daily_summary_utc = tg.daily_summary_utc,
        )
        node.run()

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down gracefully...")

    finally:
        notifier.on_system_stop(settings.trader_id)
        notifier.stop_timers()
        node.dispose()
        logger.info("Live Trader stopped cleanly.")


def _check_redis(settings: Settings) -> None:
    try:
        import redis
        r = redis.Redis(
            host           = settings.redis.host,
            port           = settings.redis.port,
            socket_timeout = settings.redis.timeout_secs,
        )
        r.ping()
        logger.info("Redis connected  (%s:%d)", settings.redis.host, settings.redis.port)
    except ImportError:
        logger.error("redis package not installed — run: pip install redis")
        sys.exit(1)
    except Exception as e:
        logger.error("Redis connection failed (%s:%d): %s",
                     settings.redis.host, settings.redis.port, e)
        logger.error("Start Redis with: redis-server")
        sys.exit(1)


if __name__ == "__main__":
    main()
