"""
main.py
──────────────────────────────────────────────────────────────────────
BTC Trader — entry point.

Stage gate comments show which lines were added per stage.

Current stage: 2 (live data feed validation)

Usage:
    python main.py
    python main.py --config path/to/settings.yaml
    python main.py --check     # run infra check then exit
"""

import argparse
import logging
import signal
import sys
from pathlib import Path

from core.config import load_settings
from core.logging_setup import setup_logging

logger = logging.getLogger("btc_trader.main")

# Shared shutdown flag so signal handlers can stop the loop
_SHUTDOWN = False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BTC SMC Algorithmic Trader")
    p.add_argument(
        "--config", default="config/settings.yaml",
        help="Path to settings.yaml (default: config/settings.yaml)",
    )
    p.add_argument(
        "--check", action="store_true",
        help="Run infrastructure check and exit",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # ── Stage 1+: Load config & logging ──────────────────────────────
    config_path = Path(args.config)
    try:
        settings = load_settings(config_path.parent, config_path.name)
    except (FileNotFoundError, ValueError) as e:
        print(f"\n[ERROR] Configuration failed:\n  {e}\n")
        sys.exit(1)

    setup_logging(settings.logging, project_root=Path(__file__).parent)

    logger.info("═" * 60)
    logger.info("BTC Trader starting  (Stage 2 — live data feed)")
    logger.info("  Mode       : %s", settings.mode)
    logger.info("  Instrument : %s", settings.instrument.nt_id)
    logger.info("  Primary TF : %s   HTF: %s",
                settings.timeframes.primary, settings.timeframes.htf)
    logger.info("  Strategy   : %s  htf_filter=%s",
                settings.strategy.name, settings.strategy.htf_filter)
    logger.info("═" * 60)

    # ── Optional infra check ───────────────────────────────────────────
    if args.check:
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/check_infra.py",
             "--config", str(config_path)],
            cwd=Path(__file__).parent,
        )
        sys.exit(result.returncode)

    # ── Stage 1+: Redis check ──────────────────────────────────────────
    _check_redis(settings)

    # ── Stage 2: Build TradingNode with data client ───────────────────
    from core.node_builder import build_node
    node = build_node(settings)

    # ── Stage 2: Build DataFeedValidator strategy ─────────────────────
    from nautilus_trader.model.identifiers import InstrumentId
    from strategies.data_validator import (
        DataFeedValidator,
        DataFeedValidatorConfig,
        make_bar_type,
    )

    instrument_id = InstrumentId.from_str(settings.instrument.nt_id)

    primary_bar_type = make_bar_type(instrument_id, settings.timeframes.primary)
    htf_bar_type     = make_bar_type(instrument_id, settings.timeframes.htf)
    aux_bar_type     = make_bar_type(instrument_id, "4h")   # always capture 4H too

    strategy = DataFeedValidator(
        config=DataFeedValidatorConfig(
            strategy_id      = "DATA-VALIDATOR-001",
            instrument_id    = instrument_id,
            primary_bar_type = primary_bar_type,
            htf_bar_type     = htf_bar_type,
            aux_bar_type     = aux_bar_type,
            state_dir        = "state",
        )
    )

    # Add strategy to the node's trader BEFORE calling build()
    node.trader.add_strategy(strategy)

    logger.info(
        "Strategy added  DataFeedValidator  "
        "bars: %s + %s + 4H",
        settings.timeframes.primary,
        settings.timeframes.htf,
    )
    logger.info("Bars will be logged to state/live_bars_<date>.csv")
    logger.info(
        "After ≥1 hour, run: python scripts/compare_bars.py "
        "to validate against the catalog"
    )

    # Stage 3: strategy will be added here (replaces DataFeedValidator)
    # Stage 4: TelegramActor added here
    # Stage 5: exec client factories registered in node_builder.py

    # ── Start the node ─────────────────────────────────────────────────
    # node.build() performs:
    #   • instrument loading from Binance REST API
    #   • Redis cache connection
    #   • WebSocket connection to Binance Futures kline streams
    # node.run() blocks until shutdown (SIGINT/SIGTERM)
    logger.info("Starting TradingNode — connecting to Binance Futures...")
    try:
        node.build()
        node.run()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received — shutting down gracefully...")
    finally:
        if not node.is_stopped:
            node.stop()
        node.dispose()
        logger.info("BTC Trader stopped cleanly.")


def _check_redis(settings) -> None:
    """Fail fast if Redis is unreachable."""
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


# ── Graceful shutdown ──────────────────────────────────────────────────
def _handle_shutdown(signum, frame) -> None:
    logger.info("Signal %s received — shutdown initiated", signum)
    # node.run() exits cleanly on SIGINT/SIGTERM via NautilusTrader's
    # own signal handling; this handler provides a fallback.
    sys.exit(0)


signal.signal(signal.SIGINT,  _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


if __name__ == "__main__":
    main()
