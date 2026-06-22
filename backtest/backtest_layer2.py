"""
backtest_layer2.py  —  Layer 2: Market Structure + HTF Bias
──────────────────────────────────────────────────────────────────────
Adds one filter on top of Layer 1:
  • Only LONG  when 1H HMA is rising  (htf.bull)
  • Only SHORT when 1H HMA is falling (htf.bear)

Everything else — MS engine, SL/TP, position management — is identical
to Layer 1 so the comparison is clean.

Layer 1 baseline:
  Trades 464 | WR 39.2% | PnL -111.66 USDT
"""

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
from htf_bias import HTFBias


# ══════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════
class Layer2Config(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type_15m:  BarType          # primary signal timeframe
    bar_type_1h:   BarType          # HTF bias timeframe  ← NEW

    trade_size: Decimal = Decimal("0.01")

    # Market structure (same defaults as Layer 1)
    swing_len: int   = 10
    atr_dist:  float = 0.5
    atr_len:   int   = 14

    # Risk (same as Layer 1)
    sl_atr:  float = 1.5
    tp1_atr: float = 2.0
    tp2_atr: float = 3.5

    # HTF HMA period (matches i_ma_len in Pine Script)
    htf_period: int = 21


# ══════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════
class Layer2Strategy(Strategy):
    """
    Layer 2 — Market Structure filtered by 1H HTF bias.

    Diff from Layer 1 (marked NEW):
      • Subscribe to 1H bars
      • Route 1H bars to HTFBias.update()
      • Gate entries: long only when htf.bull, short only when htf.bear
    """

    def __init__(self, config: Layer2Config) -> None:
        super().__init__(config)

        self.ms  = MarketStructure(
            swing_len  = config.swing_len,
            atr_dist   = config.atr_dist,
            atr_len    = config.atr_len,
        )
        self.htf = HTFBias(period=config.htf_period)   # NEW

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
        self.subscribe_bars(self.config.bar_type_15m)
        self.subscribe_bars(self.config.bar_type_1h)   # NEW
        self.log.info("Layer 2 started — MS + HTF HMA bias")

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)

    # ── Bar routing ───────────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> None:
        # NEW: route 1H bars to the HTF bias engine
        if bar.bar_type == self.config.bar_type_1h:
            self.htf.update(bar.close.as_double())
            return

        # ── 15m logic (same as Layer 1) ───────────────────────────────
        high  = bar.high.as_double()
        low   = bar.low.as_double()
        close = bar.close.as_double()

        self.ms.update(high, low, close, self._bar_count)
        self._bar_count += 1

        if self._in_long or self._in_short:
            self._manage_position(high, low)
            return

        # NEW: HTF filter gates every entry
        # Skip if HTF not yet initialized (warmup period)
        if not self.htf.initialized:
            return

        if self.ms.momentum_long  and self.htf.bull:
            self._enter(OrderSide.BUY, close)
        elif self.ms.momentum_short and self.htf.bear:
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
            f"tp1={self._tp1:.1f}  tp2={self._tp2:.1f}  "
            f"htf={'bull' if self.htf.bull else 'bear'}"
        )

    # ── Position management (unchanged from Layer 1) ──────────────────
    def _manage_position(self, high: float, low: float) -> None:
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
        side = OrderSide.SELL if self._in_long else OrderSide.BUY
        order = self.order_factory.market(
            instrument_id = self.config.instrument_id,
            order_side    = side,
            quantity      = self.instrument.make_qty(self.config.trade_size / 2),
            time_in_force = TimeInForce.GTC,
        )
        self.submit_order(order)
        self.log.info("TP1 — closed 50%")

    def _close_all(self, reason: str) -> None:
        self.close_all_positions(self.config.instrument_id)
        self._in_long = self._in_short = self._tp1_hit = False
        self.log.info(f"Closed  reason={reason}")


# ══════════════════════════════════════════════════════════════════════
#  RUNNER
# ══════════════════════════════════════════════════════════════════════
def run() -> None:
    CATALOG_PATH  = Path("./catalog")
    BAR_15M       = "BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL"
    BAR_1H        = "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"
    INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"

    # ── Data ──────────────────────────────────────────────────────────
    catalog    = ParquetDataCatalog(CATALOG_PATH)
    instrument = catalog.instruments(instrument_ids=[INSTRUMENT_ID])[0]
    bars_15m   = catalog.bars(bar_types=[BAR_15M])
    bars_1h    = catalog.bars(bar_types=[BAR_1H])

    print(f"Instrument : {instrument.id}")
    print(f"15m bars   : {len(bars_15m):,}")
    print(f"1H  bars   : {len(bars_1h):,}")
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
    engine.add_data(bars_15m)
    engine.add_data(bars_1h)    # NEW: 1H bars fed to engine

    # ── Strategy ──────────────────────────────────────────────────────
    strategy = Layer2Strategy(
        config=Layer2Config(
            instrument_id = InstrumentId.from_str(INSTRUMENT_ID),
            bar_type_15m  = BarType.from_str(BAR_15M),
            bar_type_1h   = BarType.from_str(BAR_1H),
            trade_size    = Decimal("0.01"),
            swing_len     = 10,
            atr_dist      = 0.5,
            sl_atr        = 1.5,
            tp1_atr       = 2.0,
            tp2_atr       = 3.5,
            htf_period    = 21,
        )
    )
    engine.add_strategy(strategy)

    # ── Run ───────────────────────────────────────────────────────────
    print("Running Layer 2 backtest — MS + HTF bias…\n")
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
    else:
        keep = [c for c in [
            "instrument_id", "side", "avg_px_open",
            "avg_px_close", "realized_pnl", "ts_opened", "ts_closed",
        ] if c in pos_df.columns]
        print(pos_df[keep].to_string(index=False))

    print(f"\n{sep}")
    print("  ACCOUNT REPORT")
    print(sep)
    print(engine.trader.generate_account_report(Venue("BINANCE")).to_string())

    # ── Summary with Layer 1 comparison ──────────────────────────────
    print(f"\n{sep}")
    print("  LAYER 2 SUMMARY  —  MS + HTF Bias")
    print(sep)

    # Layer 1 baseline (hardcoded from previous run)
    L1 = dict(trades=464, wr=39.2, pnl=-111.66)

    if not pos_df.empty and "realized_pnl" in pos_df.columns:
        pnl      = pos_df["realized_pnl"].str.split(" ").str[0].astype(float)
        total    = pnl.sum()
        n        = len(pnl)
        winners  = (pnl > 0).sum()
        losers   = (pnl <= 0).sum()
        wr       = winners / n * 100
        avg_win  = pnl[pnl > 0].mean() if winners > 0 else 0.0
        avg_loss = pnl[pnl <= 0].mean() if losers  > 0 else 0.0
        rr       = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0

        filtered = L1["trades"] - n

        print(f"  {'Metric':<20}  {'Layer 1':>12}  {'Layer 2':>12}  {'Delta':>10}")
        print(f"  {'-'*56}")
        print(f"  {'Trades':<20}  {L1['trades']:>12}  {n:>12}  {-filtered:>+10}")
        print(f"  {'Win rate':<20}  {L1['wr']:>11.1f}%  {wr:>11.1f}%  {wr - L1['wr']:>+9.1f}%")
        print(f"  {'Total PnL (USDT)':<20}  {L1['pnl']:>+12.2f}  {total:>+12.2f}  {total - L1['pnl']:>+10.2f}")
        print(f"  {'-'*56}")
        print(f"  {'Avg win':<20}  {'':>12}  {avg_win:>+12.2f}")
        print(f"  {'Avg loss':<20}  {'':>12}  {avg_loss:>+12.2f}")
        print(f"  {'Avg R:R':<20}  {'':>12}  {'1 : ' + f'{rr:.2f}':>12}")
        print(f"  {'Best trade':<20}  {'':>12}  {pnl.max():>+12.2f}")
        print(f"  {'Worst trade':<20}  {'':>12}  {pnl.min():>+12.2f}")
        print(f"  {'Final equity':<20}  {'':>12}  {10_000 + total:>12,.2f}")
        print(f"  {'Signals filtered':<20}  {'':>12}  {filtered:>12}  trades blocked by HTF")
        print()
        print("  Next step → Layer 3: add VWAP filter")

    print(sep)
    engine.dispose()


if __name__ == "__main__":
    run()