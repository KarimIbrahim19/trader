"""
backtest_layer1.py  —  Layer 1: Market Structure only
──────────────────────────────────────────────────────────────────────
The raw signal with no filters at all.
Entry:  ms.momentum_long / ms.momentum_short
Exit:   opposite momentum signal  OR  SL / TP hit
SL/TP:  ATR-based, checked against bar high/low each bar
        TP1 closes 50% at 2.0×ATR, TP2 closes rest at 3.5×ATR

This is the control group — every later layer is compared to this.
"""

import argparse
from decimal import Decimal
from pathlib import Path

import pandas as pd

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.trading.strategy import Strategy

from market_structure import MarketStructure


# ══════════════════════════════════════════════════════════════════════
#  CLI + DATE FILTERING  (matches backtest_fvg_signal.py for fair comparison)
# ══════════════════════════════════════════════════════════════════════
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest Layer 1 — Market Structure only")
    p.add_argument("--catalog",  default="./catalog",
                    help="Path to ParquetDataCatalog directory (default: ./catalog)")
    p.add_argument("--start",    default=None,
                    help="Filter bars from this date YYYY-MM-DD (inclusive)")
    p.add_argument("--end",      default=None,
                    help="Filter bars until this date YYYY-MM-DD (inclusive)")
    p.add_argument("--bar-type", default="BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL")
    p.add_argument("--instrument", default="BTCUSDT-PERP.BINANCE")
    return p.parse_args()


def filter_bars_by_date(bars: list, start: str | None, end: str | None) -> list:
    """Slice a bar list to [start, end] inclusive, by ts_init (nanoseconds)."""
    if not start and not end:
        return bars
    start_ns = int(pd.Timestamp(start, tz="UTC").timestamp() * 1e9) if start else 0
    end_ns   = (int(pd.Timestamp(end, tz="UTC").timestamp() * 1e9) + 86_399_000_000_000
                if end else 2**63 - 1)
    return [b for b in bars if start_ns <= b.ts_init <= end_ns]


# ══════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════
class Layer1Config(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type:      BarType

    # Position size
    trade_size: Decimal = Decimal("0.01")   # BTC per trade

    # Market structure
    swing_len: int   = 10    # pivot lookback (bars each side)
    atr_dist:  float = 0.5   # min ATR distance between opposite pivots
    atr_len:   int   = 14    # ATR period inside the MS engine

    # Risk
    sl_atr:  float = 1.5    # stop loss distance  = sl_atr  × ATR
    tp1_atr: float = 2.0    # TP1 (close 50%)     = tp1_atr × ATR
    tp2_atr: float = 3.5    # TP2 (close rest)    = tp2_atr × ATR


# ══════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════
class Layer1Strategy(Strategy):
    """
    Layer 1 — pure market structure, zero confluence filters.

    Keeps the NautilusTrader boilerplate minimal so the MS logic
    is easy to read and compare to the Pine Script original.
    """

    def __init__(self, config: Layer1Config) -> None:
        super().__init__(config)

        self.ms = MarketStructure(
            swing_len  = config.swing_len,
            atr_dist   = config.atr_dist,
            atr_len    = config.atr_len,
        )

        # Simple position-tracking state
        self._bar_count: int   = 0
        self._in_long:   bool  = False
        self._in_short:  bool  = False
        self._sl:        float = 0.0
        self._tp1:       float = 0.0
        self._tp2:       float = 0.0
        self._tp1_hit:   bool  = False

    # ── Lifecycle ─────────────────────────────────────────────────────
    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        self.subscribe_bars(self.config.bar_type)
        self.log.info("Layer 1 started — Market Structure only, no filters")

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)

    # ── Main bar handler ──────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> None:
        high  = bar.high.as_double()
        low   = bar.low.as_double()
        close = bar.close.as_double()

        # Update market structure engine on every bar
        self.ms.update(high, low, close, self._bar_count)
        self._bar_count += 1

        # If in a trade: manage SL/TP/exit, then return
        if self._in_long or self._in_short:
            self._manage_position(high, low)
            return

        # Entry signals
        if self.ms.momentum_long:
            self._enter(OrderSide.BUY, close)
        elif self.ms.momentum_short:
            self._enter(OrderSide.SELL, close)

    # ── Entry ─────────────────────────────────────────────────────────
    def _enter(self, side: OrderSide, close: float) -> None:
        atr = self.ms.atr
        if atr <= 0:
            return

        is_long = side == OrderSide.BUY

        if is_long:
            self._sl  = close - self.config.sl_atr  * atr
            self._tp1 = close + self.config.tp1_atr * atr
            self._tp2 = close + self.config.tp2_atr * atr
        else:
            self._sl  = close + self.config.sl_atr  * atr
            self._tp1 = close - self.config.tp1_atr * atr
            self._tp2 = close - self.config.tp2_atr * atr

        self._in_long  = is_long
        self._in_short = not is_long
        self._tp1_hit  = False

        order = self.order_factory.market(
            instrument_id = self.config.instrument_id,
            order_side    = side,
            quantity      = self.instrument.make_qty(self.config.trade_size),
            time_in_force = TimeInForce.GTC,
        )
        self.submit_order(order)

        self.log.info(
            f"{'LONG' if is_long else 'SHORT'}  "
            f"entry≈{close:.1f}  sl={self._sl:.1f}  "
            f"tp1={self._tp1:.1f}  tp2={self._tp2:.1f}  atr={atr:.1f}"
        )

    # ── Position management ───────────────────────────────────────────
    def _manage_position(self, high: float, low: float) -> None:
        """
        Check SL / TP / exit signal using the bar's high and low.
        Checked in priority order: SL → TP1 → TP2 → exit signal.
        """
        if self._in_long:
            if low <= self._sl:
                self._close_all("SL")
            elif not self._tp1_hit and high >= self._tp1:
                self._close_half()
                self._tp1_hit = True
            elif self._tp1_hit and high >= self._tp2:
                self._close_all("TP2")
            elif self.ms.momentum_short:
                self._close_all("exit-signal")

        elif self._in_short:
            if high >= self._sl:
                self._close_all("SL")
            elif not self._tp1_hit and low <= self._tp1:
                self._close_half()
                self._tp1_hit = True
            elif self._tp1_hit and low <= self._tp2:
                self._close_all("TP2")
            elif self.ms.momentum_long:
                self._close_all("exit-signal")

    def _close_half(self) -> None:
        """Submit a market order to close 50% of the current position."""
        side = OrderSide.SELL if self._in_long else OrderSide.BUY
        half = self.instrument.make_qty(self.config.trade_size / 2)
        order = self.order_factory.market(
            instrument_id = self.config.instrument_id,
            order_side    = side,
            quantity      = half,
            time_in_force = TimeInForce.GTC,
        )
        self.submit_order(order)
        self.log.info("TP1 — closed 50%")

    def _close_all(self, reason: str) -> None:
        self.close_all_positions(self.config.instrument_id)
        self._in_long  = False
        self._in_short = False
        self._tp1_hit  = False
        self.log.info(f"Closed  reason={reason}")


# ══════════════════════════════════════════════════════════════════════
#  RUNNER
# ══════════════════════════════════════════════════════════════════════
def run() -> None:
    args          = parse_args()
    CATALOG_PATH  = Path(args.catalog)
    BAR_TYPE_STR  = args.bar_type
    INSTRUMENT_ID = args.instrument

    # ── Data ──────────────────────────────────────────────────────────
    catalog    = ParquetDataCatalog(CATALOG_PATH)
    instrument = catalog.instruments(instrument_ids=[INSTRUMENT_ID])[0]
    bars       = catalog.bars(bar_types=[BAR_TYPE_STR])
    bars       = filter_bars_by_date(bars, args.start, args.end)

    if not bars:
        print("No bars in the selected date range — check --start/--end.")
        return

    label = f"{args.start or 'start'} → {args.end or 'end'}"
    print(f"Catalog    : {args.catalog}")
    print(f"Instrument : {instrument.id}")
    print(f"Date range : {label}")
    print(f"Bars       : {len(bars):,} × 15m")
    print(
        f"Actual span: "
        f"{pd.Timestamp(bars[0].ts_init,  unit='ns', tz='UTC').date()} → "
        f"{pd.Timestamp(bars[-1].ts_init, unit='ns', tz='UTC').date()}"
    )
    print()

    # ── Engine ────────────────────────────────────────────────────────
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            logging=LoggingConfig(log_level="WARNING"),
        )
    )

    engine.add_venue(
        venue             = Venue("BINANCE"),
        oms_type          = OmsType.NETTING,
        account_type      = AccountType.MARGIN,
        base_currency     = None,
        starting_balances = [Money(10_000, USDT)],
    )

    engine.add_instrument(instrument)
    engine.add_data(bars)

    # ── Strategy ──────────────────────────────────────────────────────
    strategy = Layer1Strategy(
        config=Layer1Config(
            instrument_id = InstrumentId.from_str(INSTRUMENT_ID),
            bar_type      = BarType.from_str(BAR_TYPE_STR),
            trade_size    = Decimal("0.01"),
            swing_len     = 10,
            atr_dist      = 0.5,
            sl_atr        = 1.5,
            tp1_atr       = 2.0,
            tp2_atr       = 3.5,
        )
    )
    engine.add_strategy(strategy)

    # ── Run ───────────────────────────────────────────────────────────
    print("Running Layer 1 backtest — Market Structure only…\n")
    engine.run()

    # ── Reports ───────────────────────────────────────────────────────
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 150)
    sep = "═" * 72

    pos_df = engine.trader.generate_positions_report()

    print(sep)
    print("  CLOSED POSITIONS")
    print(sep)
    if pos_df.empty:
        print("  No closed positions.")
        print("  The MS engine did not generate any momentum signals.")
        print("  Try reducing swing_len or atr_dist.")
    else:
        keep = [c for c in [
            "instrument_id", "side", "quantity",
            "avg_px_open", "avg_px_close", "realized_pnl",
            "ts_opened", "ts_closed",
        ] if c in pos_df.columns]
        print(pos_df[keep].to_string(index=False))

    print(f"\n{sep}")
    print("  ACCOUNT REPORT")
    print(sep)
    print(engine.trader.generate_account_report(Venue("BINANCE")).to_string())

    # ── Summary ───────────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  LAYER 1 SUMMARY  —  Market Structure only (no filters)   [{label}]")
    print(sep)

    if not pos_df.empty and "realized_pnl" in pos_df.columns:
        pnl     = pos_df["realized_pnl"].str.split(" ").str[0].astype(float)
        total   = pnl.sum()
        n       = len(pnl)
        winners = (pnl > 0).sum()
        losers  = (pnl <= 0).sum()

        avg_win  = pnl[pnl > 0].mean() if winners > 0 else 0.0
        avg_loss = pnl[pnl <= 0].mean() if losers  > 0 else 0.0
        rr_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0

        print(f"  Trades        : {n}")
        print(f"  Winners       : {winners}  |  Losers : {losers}")
        print(f"  Win rate      : {winners / n * 100:.1f}%")
        print(f"  Avg win       : {avg_win:+.2f} USDT")
        print(f"  Avg loss      : {avg_loss:+.2f} USDT")
        print(f"  Avg R:R       : 1 : {rr_ratio:.2f}")
        print(f"  Best trade    : {pnl.max():+.2f} USDT")
        print(f"  Worst trade   : {pnl.min():+.2f} USDT")
        print(f"  Total PnL     : {total:+.2f} USDT")
        print(f"  Final equity  : ~{10_000 + total:,.2f} USDT  (started $10,000)")
        print()
        print("  Next step: add HTF bias filter (Layer 2)")
        print("  Compare that result against this baseline.")

    print(sep)
    engine.dispose()


if __name__ == "__main__":
    run()