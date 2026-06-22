"""
backtest_layer3.py  —  Layer 3: MS + HTF Bias + FVG Proximity
──────────────────────────────────────────────────────────────────────
Adds FVG confluence on top of Layer 2.
Uses fvg.long_filter / fvg.short_filter (filter mode).

The FVG module also exposes fvg.bull_signal / fvg.bear_signal
(signal mode) which can be used as standalone entries in other strategies.

Layer baselines:
  Layer 1 — Trades 464 | WR 39.2% | PnL -111.66 USDT
  Layer 2 — Trades 304 | WR 40.8% | PnL  -14.81 USDT
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
from fvg_zones import FVGZones


# ══════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════
class Layer3Config(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type_15m:  BarType
    bar_type_1h:   BarType

    trade_size: Decimal = Decimal("0.01")

    # Market structure
    swing_len: int   = 10
    atr_dist:  float = 0.5
    atr_len:   int   = 14

    # Risk
    sl_atr:  float = 1.5
    tp1_atr: float = 2.0
    tp2_atr: float = 3.5

    # HTF
    htf_period: int = 21

    # FVG (filter mode)
    fvg_atr_mult:     float = 0.25
    fvg_max_zones:    int   = 10
    fvg_sig_lookback: int   = 3
    fvg_ifvg_enable:  bool  = True


# ══════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════
class Layer3Strategy(Strategy):
    """
    Layer 3 — MS + HTF bias + FVG proximity/recency filter.

    FVG module is used in FILTER MODE here:
        fvg.long_filter  = bull zone within 1 ATR  AND  bounce ≤ sig_lookback bars ago
        fvg.short_filter = bear zone within 1 ATR  AND  bounce ≤ sig_lookback bars ago

    To switch to SIGNAL MODE instead, replace fvg.long_filter with fvg.bull_signal
    (fires only on the exact bounce bar — much stricter, far fewer trades).
    """

    def __init__(self, config: Layer3Config) -> None:
        super().__init__(config)

        self.ms  = MarketStructure(
            swing_len = config.swing_len,
            atr_dist  = config.atr_dist,
            atr_len   = config.atr_len,
        )
        self.htf = HTFBias(period=config.htf_period)
        self.fvg = FVGZones(
            atr_mult     = config.fvg_atr_mult,
            max_zones    = config.fvg_max_zones,
            sig_lookback = config.fvg_sig_lookback,
            ifvg_enable  = config.fvg_ifvg_enable,
        )

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
        self.subscribe_bars(self.config.bar_type_1h)
        self.log.info("Layer 3 started — MS + HTF + FVG (filter mode)")

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)

    # ── Bar routing ───────────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> None:

        # 1H bars → update HTF bias only
        if bar.bar_type == self.config.bar_type_1h:
            self.htf.update(bar.close.as_double())
            return

        # 15m bars → update all modules
        high  = bar.high.as_double()
        low   = bar.low.as_double()
        close = bar.close.as_double()

        self.ms.update(high, low, close, self._bar_count)
        self.fvg.update(high, low, close, self.ms.atr)
        self._bar_count += 1

        # Manage any open position first
        if self._in_long or self._in_short:
            self._manage_position(high, low)
            return

        # Wait for HTF warmup
        if not self.htf.initialized:
            return

        # ── Entry: all three conditions must align ─────────────────────
        #
        #   FILTER MODE  →  fvg.long_filter  (proximity + recency)
        #   SIGNAL MODE  →  fvg.bull_signal  (exact bounce bar only)
        #
        if (self.ms.momentum_long
                and self.htf.bull
                and self.fvg.long_filter):
            self._enter(OrderSide.BUY, close)

        elif (self.ms.momentum_short
                and self.htf.bear
                and self.fvg.short_filter):
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

        bull_z, bear_z = self.fvg.zone_counts()
        self.log.info(
            f"{'LONG' if is_long else 'SHORT'}  entry≈{close:.1f}  "
            f"sl={self._sl:.1f}  tp1={self._tp1:.1f}  tp2={self._tp2:.1f}  "
            f"atr={atr:.1f}  fvg(b={bull_z} br={bear_z})"
        )

    # ── Position management (unchanged from Layers 1 & 2) ────────────
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

    catalog    = ParquetDataCatalog(CATALOG_PATH)
    instrument = catalog.instruments(instrument_ids=[INSTRUMENT_ID])[0]
    bars_15m   = catalog.bars(bar_types=[BAR_15M])
    bars_1h    = catalog.bars(bar_types=[BAR_1H])

    print(f"Instrument : {instrument.id}")
    print(f"15m bars   : {len(bars_15m):,}")
    print(f"1H  bars   : {len(bars_1h):,}")
    print()

    engine = BacktestEngine(
        config=BacktestEngineConfig(logging=LoggingConfig(log_level="WARNING"))
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
    engine.add_data(bars_1h)

    strategy = Layer3Strategy(
        config=Layer3Config(
            instrument_id     = InstrumentId.from_str(INSTRUMENT_ID),
            bar_type_15m      = BarType.from_str(BAR_15M),
            bar_type_1h       = BarType.from_str(BAR_1H),
            trade_size        = Decimal("0.01"),
            swing_len         = 10,
            atr_dist          = 0.5,
            sl_atr            = 1.5,
            tp1_atr           = 2.0,
            tp2_atr           = 3.5,
            htf_period        = 21,
            fvg_atr_mult      = 0.25,
            fvg_max_zones     = 10,
            fvg_sig_lookback  = 3,
            fvg_ifvg_enable   = True,
        )
    )
    engine.add_strategy(strategy)

    print("Running Layer 3 backtest — MS + HTF + FVG…\n")
    engine.run()

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 150)
    sep = "═" * 72

    pos_df = engine.trader.generate_positions_report()

    print(sep)
    print("  CLOSED POSITIONS")
    print(sep)
    if pos_df.empty:
        print("  No closed positions — FVG filter may be too strict.")
        print("  Try: fvg_sig_lookback=5  or  fvg_atr_mult=0.1")
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

    # ── Summary ───────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("  LAYER 3 SUMMARY  —  MS + HTF Bias + FVG Proximity (filter mode)")
    print(sep)

    L1 = dict(trades=464, wr=39.2, pnl=-111.66)
    L2 = dict(trades=304, wr=40.8, pnl= -14.81)

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

        print(f"  {'Metric':<22}  {'Layer 1':>10}  {'Layer 2':>10}  {'Layer 3':>10}")
        print(f"  {'-'*60}")
        print(f"  {'Trades':<22}  {L1['trades']:>10}  {L2['trades']:>10}  {n:>10}")
        print(f"  {'Win rate':<22}  {L1['wr']:>9.1f}%  {L2['wr']:>9.1f}%  {wr:>9.1f}%")
        print(f"  {'Total PnL (USDT)':<22}  {L1['pnl']:>+10.2f}  {L2['pnl']:>+10.2f}  {total:>+10.2f}")
        print(f"  {'-'*60}")
        print(f"  {'Avg win':<22}  {'':>10}  {'':>10}  {avg_win:>+10.2f}")
        print(f"  {'Avg loss':<22}  {'':>10}  {'':>10}  {avg_loss:>+10.2f}")
        print(f"  {'Avg R:R':<22}  {'':>10}  {'':>10}  {'1:'+f'{rr:.2f}':>10}")
        print(f"  {'Best trade':<22}  {'':>10}  {'':>10}  {pnl.max():>+10.2f}")
        print(f"  {'Worst trade':<22}  {'':>10}  {'':>10}  {pnl.min():>+10.2f}")
        print(f"  {'Final equity':<22}  {'':>10}  {'':>10}  {10_000+total:>10,.2f}")
        print(f"  {'vs Layer 2':<22}  {'':>10}  {'':>10}  {total-L2['pnl']:>+10.2f}")
        print()
        if n < 80:
            print("  ⚠  Trade count is low — results have limited statistical weight.")
            print("     Consider fvg_sig_lookback=5 or fvg_atr_mult=0.1 for more signals.")
        print("  Next step → Layer 4: add MA filter (HMA trend confirmation)")

    print(sep)
    engine.dispose()


if __name__ == "__main__":
    run()