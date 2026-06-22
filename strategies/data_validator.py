"""
strategies/data_validator.py
──────────────────────────────────────────────────────────────────────
Stage 2 validation strategy.

Subscribes to all configured timeframes and logs every incoming bar
with wall-clock timing so we can answer:

  1. Are bars arriving promptly after each Binance candle closes?
  2. Does price data match what is in our ParquetDataCatalog?
  3. Is the 1H bar always delivered BEFORE the 15m bar that closes at
     the same hour boundary? (critical for HTF bias correctness)

All received bars are appended to:
    state/live_bars_{YYYYMMDD}.csv

Run compare_bars.py after ≥24 hours to diff against catalog data.
"""

import csv
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AggregationSource, PriceType, BarAggregation
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

logger = logging.getLogger(__name__)

# CSV columns written for every received bar
_CSV_COLUMNS = [
    "received_at_utc",     # wall-clock time this process received the bar
    "bar_type",            # e.g. BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL
    "bar_open_ts_ns",      # bar.ts_init (bar open time, nanoseconds)
    "open", "high", "low", "close", "volume",
    "delay_ms",            # wall-clock delay after expected bar close (ms)
]


# ── Config ─────────────────────────────────────────────────────────────
class DataFeedValidatorConfig(StrategyConfig, frozen=True):
    instrument_id:     InstrumentId
    primary_bar_type:  BarType   # e.g. 15-MINUTE
    htf_bar_type:      BarType   # e.g. 1-HOUR
    aux_bar_type:      BarType   # e.g. 4-HOUR  (logged but not used for signals)
    state_dir:         str = "state"


# ── Helper: build bar types from settings ─────────────────────────────
_TF_MAP: dict[str, tuple[int, BarAggregation]] = {
    "1m":  (1,  BarAggregation.MINUTE),
    "5m":  (5,  BarAggregation.MINUTE),
    "15m": (15, BarAggregation.MINUTE),
    "1h":  (1,  BarAggregation.HOUR),
    "4h":  (4,  BarAggregation.HOUR),
    "1d":  (1,  BarAggregation.DAY),
}

def make_bar_type(instrument_id: InstrumentId, timeframe: str) -> BarType:
    """Convert a timeframe string ('15m', '1h', '4h') to a NautilusTrader BarType."""
    tf = timeframe.lower()
    if tf not in _TF_MAP:
        raise ValueError(f"Unknown timeframe '{timeframe}'. Supported: {list(_TF_MAP)}")
    step, aggregation = _TF_MAP[tf]
    from nautilus_trader.model.data import BarSpecification
    return BarType(
        instrument_id      = instrument_id,
        bar_spec           = BarSpecification(
            step           = step,
            aggregation    = aggregation,
            price_type     = PriceType.LAST,
        ),
        aggregation_source = AggregationSource.EXTERNAL,
    )


# ── Strategy ───────────────────────────────────────────────────────────
class DataFeedValidator(Strategy):
    """
    Subscribes to all configured bar types and logs every arrival.
    No signals, no orders — pure data validation for Stage 2.
    """

    def __init__(self, config: DataFeedValidatorConfig) -> None:
        super().__init__(config)

        self._state_dir = Path(config.state_dir)
        self._csv_path: Path | None = None
        self._csv_file = None
        self._csv_writer = None

        # Track bar counts per type for progress reporting
        self._bar_counts: dict[str, int] = {}

        # Track last bar arrival for the 1H/15m ordering check
        self._last_1h_ts: int = 0
        self._last_15m_ts: int = 0
        self._ordering_violations: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────
    def on_start(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)

        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        self._csv_path = self._state_dir / f"live_bars_{today}.csv"

        # Open CSV in append mode so restarts don't lose data
        is_new = not self._csv_path.exists()
        self._csv_file   = open(self._csv_path, "a", newline="", buffering=1)
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=_CSV_COLUMNS)
        if is_new:
            self._csv_writer.writeheader()

        # Subscribe to all three timeframes
        for bt in [
            self.config.primary_bar_type,
            self.config.htf_bar_type,
            self.config.aux_bar_type,
        ]:
            self.subscribe_bars(bt)
            logger.info("Subscribed  %s", bt)

        logger.info(
            "DataFeedValidator started — logging bars to %s",
            self._csv_path,
        )

    def on_stop(self) -> None:
        if self._csv_file:
            self._csv_file.flush()
            self._csv_file.close()
        self._print_session_summary()

    # ── Bar handler ───────────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> None:
        received_ns = time.time_ns()
        bt_str      = str(bar.bar_type)

        step_ns   = self._bar_step_ns(bt_str)
        open_ns   = bar.ts_event - step_ns   # bar open time from Binance authoritative close timestamp
        open_ts   = (open_ns + 500_000_000) // 1_000_000_000 * 1_000_000_000  # round to nearest second
        delay_ms  = (received_ns - bar.ts_event) / 1_000_000

        received_utc = datetime.fromtimestamp(
            received_ns / 1e9, tz=timezone.utc
        ).isoformat()

        # ── Write to CSV ───────────────────────────────────────────────
        self._csv_writer.writerow({
            "received_at_utc": received_utc,
            "bar_type":        bt_str,
            "bar_open_ts_ns":  open_ts,
            "open":   bar.open.as_double(),
            "high":   bar.high.as_double(),
            "low":    bar.low.as_double(),
            "close":  bar.close.as_double(),
            "volume": bar.volume.as_double(),
            "delay_ms": round(delay_ms, 1),
        })

        # ── Console log ────────────────────────────────────────────────
        tf_label  = self._tf_label(bt_str)
        self._bar_counts[tf_label] = self._bar_counts.get(tf_label, 0) + 1

        logger.info(
            "BAR %-4s  O=%-12s H=%-12s L=%-12s C=%-12s V=%-10s  "
            "delay=%+.0fms  [%s #%d]",
            tf_label,
            bar.open, bar.high, bar.low, bar.close, bar.volume,
            delay_ms,
            tf_label,
            self._bar_counts[tf_label],
        )

        # ── 1H → 15m ordering check at the hour boundary ──────────────
        htf_str     = str(self.config.htf_bar_type)
        primary_str = str(self.config.primary_bar_type)

        if bt_str == htf_str:
            self._last_1h_ts = received_ns
        elif bt_str == primary_str:
            # Is this a 15m bar closing at an hour boundary?
            bar_close_min = (bar.ts_event // 60_000_000_000) % 60
            if bar_close_min == 0:   # closes on the hour
                if self._last_15m_ts > 0 and self._last_1h_ts < received_ns:
                    logger.info(
                        "✓  HOUR BOUNDARY  1H arrived %.0fms before 15m",
                        (received_ns - self._last_1h_ts) / 1_000_000,
                    )
                elif self._last_1h_ts < self._last_15m_ts:
                    self._ordering_violations += 1
                    logger.warning(
                        "✗  ORDERING VIOLATION #%d  15m arrived before 1H at %s",
                        self._ordering_violations,
                        received_utc,
                    )
            self._last_15m_ts = received_ns

    # ── Helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _bar_step_ns(bt_str: str) -> int:
        """
        Return the bar duration in nanoseconds by parsing the bar type string.

        NautilusTrader 1.228.0 BinanceBar does not expose .bar_spec on
        the bar_type object, so we parse the string representation instead.
        Format: BTCUSDT-PERP.BINANCE-{STEP}-{AGGREGATION}-LAST-EXTERNAL
        Example: BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL
        """
        parts = bt_str.split("-")
        for i, part in enumerate(parts):
            if part in ("MINUTE", "HOUR", "DAY") and i > 0:
                try:
                    step = int(parts[i - 1])
                    if part == "MINUTE":
                        return step * 60 * 1_000_000_000
                    if part == "HOUR":
                        return step * 3600 * 1_000_000_000
                    if part == "DAY":
                        return step * 86400 * 1_000_000_000
                except ValueError:
                    pass
        return 60 * 1_000_000_000   # fallback: 1 minute

    @staticmethod
    def _tf_label(bt_str: str) -> str:
        """Convert a bar type string to a short label like '15m', '1H', '4H'."""
        parts = bt_str.split("-")
        for i, part in enumerate(parts):
            if part in ("MINUTE", "HOUR", "DAY") and i > 0:
                try:
                    step = int(parts[i - 1])
                    if part == "MINUTE":
                        return f"{step}m"
                    if part == "HOUR":
                        return f"{step}H"
                    if part == "DAY":
                        return f"{step}D"
                except ValueError:
                    pass
        return "??"

    def _print_session_summary(self) -> None:
        logger.info("─" * 60)
        logger.info("DataFeedValidator — session summary")
        for label, count in sorted(self._bar_counts.items()):
            logger.info("  %-8s  %d bars received", label, count)
        if self._ordering_violations:
            logger.warning(
                "  ⚠  %d hour-boundary ordering violations detected",
                self._ordering_violations,
            )
        else:
            logger.info("  ✓  No hour-boundary ordering violations")
        if self._csv_path:
            logger.info("  Bars saved to: %s", self._csv_path)
        logger.info("─" * 60)