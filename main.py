"""
main.py
────────────────────────────────────────────────────────────────────────
BTC Trader — entry point.

Stage 4 additions:
  • Creates one shared TelegramNotifier from settings.telegram.
  • Calls strategy.set_notifier(notifier) for each strategy before
    node.build() so the notifier is available in on_start().
  • After node.build() (all strategies running, ledgers registered):
      notifier.on_system_start() — sends startup message to Telegram
      notifier.start_timers()    — begins heartbeat + daily summary
  • In finally block:
      notifier.stop_timers()     — cancels daemon timer threads
      notifier.on_system_stop()  — sends shutdown + session summary

Usage:
    python main.py
    python main.py --config path/to/settings.yaml
    python main.py --check
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

logger = logging.getLogger("btc_trader.main")


# ── Bar type helper ───────────────────────────────────────────────────────
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
        raise ValueError(
            f"Unknown timeframe '{timeframe}'. Supported: {list(_TF_MAP)}"
        )
    step, aggregation = _TF_MAP[tf]
    return BarType(
        instrument_id      = instrument_id,
        bar_spec           = BarSpecification(
            step           = step,
            aggregation    = aggregation,
            price_type     = PriceType.LAST,
        ),
        aggregation_source = AggregationSource.EXTERNAL,
    )


# ── Strategy factory ──────────────────────────────────────────────────────
def _build_strategy(
    name:          str,
    strat_settings: StrategySettingsBase,
    settings:      Settings,
    instrument_id: InstrumentId,
):
    """
    Instantiate the correct strategy class via the REGISTRY.
    Adding a new strategy = add a REGISTRY entry in strategies/__init__.py
    + a YAML block — no changes to main.py or config.py needed.
    """
    from strategies import REGISTRY

    entry = REGISTRY.get(name)
    if entry is None:
        raise ValueError(
            f"Unknown strategy '{name}'. "
            f"Available: {list(REGISTRY)}."
        )

    primary_bar  = _make_bar_type(instrument_id, strat_settings.primary_bar)
    htf_bar      = _make_bar_type(instrument_id, strat_settings.htf_bar)
    strategy_id  = strat_settings.strategy_id

    config = strat_settings.build_config(
        strategy_id   = strategy_id,
        instrument_id = instrument_id,
        state_dir     = "state",
        mode          = settings.mode,
        primary_bar   = primary_bar,
        htf_bar       = htf_bar,
    )
    return entry["strategy"](config=config)


# ── Entry point ───────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BTC SMC Algorithmic Trader")
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

    # ── Startup banner ─────────────────────────────────────────────────
    logger.info("═" * 60)
    logger.info("BTC Trader starting  (Stage 4 — Telegram notifications)")
    logger.info("  Trader ID  : %s", settings.trader_id)
    logger.info("  Mode       : %s", settings.mode)
    logger.info("  Instrument : %s", settings.instrument.nt_id)
    enabled = settings.enabled_strategies
    if enabled:
        for name, s in enabled.items():
            logger.info(
                "  Strategy   : %-6s  primary=%s  htf=%s  "
                "htf_filter=%s  size=%s BTC  max_open=%d",
                name.upper(), s.primary_bar, s.htf_bar,
                s.htf_filter, s.trade_size, s.max_open_trades,
            )
    else:
        logger.warning("  No strategies enabled — check settings.yaml")
    logger.info("═" * 60)

    # ── Infra check shortcut ───────────────────────────────────────────
    if args.check:
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/check_infra.py",
             "--config", str(config_path)],
            cwd=Path(__file__).parent,
        )
        sys.exit(result.returncode)

    # ── Redis check ────────────────────────────────────────────────────
    _check_redis(settings)

    # ── Create shared TelegramNotifier ────────────────────────────────
    # One instance shared by all strategies so heartbeat covers them all.
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

    # ── Build TradingNode ──────────────────────────────────────────────
    from core.node_builder import build_node
    node = build_node(settings)

    instrument_id = InstrumentId.from_str(settings.instrument.nt_id)

    if not enabled:
        logger.error(
            "No strategies enabled in settings.yaml. "
            "Set at least one strategy's 'enabled: true' and restart."
        )
        sys.exit(1)

    # ── Build strategies, wire notifier, add to trader ────────────────
    for name, strat_cfg in enabled.items():
        try:
            strategy = _build_strategy(name, strat_cfg, settings, instrument_id)
            strategy.set_notifier(notifier, strategy_name=name)       # Stage 4: wire before build()
            node.trader.add_strategy(strategy)
            logger.info("Added strategy: %s (notifier wired)", name.upper())
        except Exception as e:
            logger.error("Failed to build strategy '%s': %s", name, e)
            sys.exit(1)

    # ── Start ──────────────────────────────────────────────────────────
    logger.info("Starting TradingNode — connecting to Binance Futures...")
    if settings.is_dry_run:
        logger.info("DRY_RUN: signals logged, no orders placed.")

    try:
        node.build()
        # All strategies are now running (on_start() has been called).
        # Ledgers are registered — safe to start timers and send system start.
        notifier.on_system_start(
            trader_id      = settings.trader_id,
            mode           = settings.mode,
            enabled_strats = enabled,
        )
        notifier.start_timers(
            heartbeat_mins    = tg.heartbeat_interval_mins,
            daily_summary_utc = tg.daily_summary_utc,
        )
        node.run()   # blocks until NT handles SIGINT/SIGTERM internally

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down gracefully...")

    finally:
        # Send shutdown message BEFORE stop_timers() so the executor
        # is still accepting submissions.
        notifier.on_system_stop(settings.trader_id)
        # Then cancel timer threads and shut down the HTTP executor.
        notifier.stop_timers()
        node.dispose()
        logger.info("BTC Trader stopped cleanly.")


def _check_redis(settings: Settings) -> None:
    try:
        import redis
        r = redis.Redis(
            host           = settings.redis.host,
            port           = settings.redis.port,
            socket_timeout = settings.redis.timeout_secs,
        )
        r.ping()
        logger.info(
            "Redis connected  (%s:%d)",
            settings.redis.host, settings.redis.port,
        )
    except ImportError:
        logger.error("redis package not installed — run: pip install redis")
        sys.exit(1)
    except Exception as e:
        logger.error(
            "Redis connection failed (%s:%d): %s",
            settings.redis.host, settings.redis.port, e,
        )
        logger.error("Start Redis with: redis-server")
        sys.exit(1)


if __name__ == "__main__":
    main()