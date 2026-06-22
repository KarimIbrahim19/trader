"""
backtest_layer4.py  —  Layer 4: MS + HTF Bias + LSTM filter (CPU‑only Keras)
──────────────────────────────────────────────────────────────────────
Explicitly tells TensorFlow to use zero GPU devices → no CUDA probe,
no hang, instant start. Uses the original .h5 model.
"""

import torch
import math
from collections import deque
from decimal import Decimal
from pathlib import Path

# ═══ Force TensorFlow CPU‑only BEFORE any TF import ═══
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"

import numpy as np
import pandas as pd
import joblib

import tensorflow as tf

# ⚡ Explicitly set visible devices to empty list — no GPU probe ever
tf.config.set_visible_devices([], 'GPU')

# Now load Keras model (no GPU interaction)
from tensorflow.keras.models import load_model

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
from cvd_filter import CVDFilter


# ── Suppress TF warning/error noise ─────────────────────────────────
tf.get_logger().setLevel('ERROR')


# ══════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════
class Layer4Config(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type_15m:  BarType
    bar_type_1h:   BarType
    cvd_path:      Path

    model_path:    Path          # .h5 model
    scaler_path:   Path
    lstm_threshold: float = 0.55
    seq_len:       int = 30

    trade_size: Decimal = Decimal("0.01")

    swing_len: int = 10
    atr_dist:  float = 0.5
    atr_len:   int = 14

    sl_atr:  float = 1.5
    tp1_atr: float = 2.0
    tp2_atr: float = 3.5

    htf_period: int = 21


# ══════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════
class Layer4Strategy(Strategy):
    FEATURE_COLS = [
        "close", "volume", "atr", "high_low_range",
        "cvd_delta",
        "trend", "pending", "momentum_long", "momentum_short",
        "bull_bos", "bear_bos", "bull_choch", "bear_choch",
        "swing_high_count", "swing_low_count",
        "watch_high_dist", "watch_low_dist",
        "htf_bull", "htf_bear", "htf_initialized",
    ]

    def __init__(self, config: Layer4Config) -> None:
        super().__init__(config)
        self.ms  = MarketStructure(swing_len=config.swing_len, atr_dist=config.atr_dist, atr_len=config.atr_len)
        self.htf = HTFBias(period=config.htf_period)
        self.cvd = CVDFilter(config.cvd_path)

        self.model  = None
        self.scaler = None
        self._feat_window = deque(maxlen=config.seq_len)

        self._bar_count = 0
        self._in_long = False
        self._in_short = False
        self._sl = self._tp1 = self._tp2 = 0.0
        self._tp1_hit = False

    # ── Lifecycle ────────────────────────────────────────────────────
    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        self.subscribe_bars(self.config.bar_type_15m)
        self.subscribe_bars(self.config.bar_type_1h)

        self.model = torch.jit.load(self.config.model_path)
        self.model.eval()
        self.scaler = joblib.load(self.config.scaler_path)
        self.log.info("Layer 4 started — MS + HTF + LSTM (PyTorch TorchScript)")

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)
        self.log.info("Layer 4 stopped")

    # ── Bar routing ──────────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> None:
        if bar.bar_type == self.config.bar_type_1h:
            self.htf.update(bar.close.as_double())
            return

        high  = bar.high.as_double()
        low   = bar.low.as_double()
        close = bar.close.as_double()

        self.ms.update(high, low, close, self._bar_count)
        self._bar_count += 1

        if self._in_long or self._in_short:
            self._manage_position(high, low)
            return

        if not self.htf.initialized:
            return

        ts = pd.Timestamp(bar.ts_event, tz="UTC", unit="ns")
        cvd_delta = self.cvd.delta(ts)

        features = self._build_features(high, low, close, bar.volume.as_double(), cvd_delta)
        self._feat_window.append(features)

        if len(self._feat_window) < self.config.seq_len:
            return

        prob_up = self._predict_prob_up()

        if self.ms.momentum_long and self.htf.bull and prob_up > self.config.lstm_threshold:
            self._enter(OrderSide.BUY, close, prob_up)
        elif self.ms.momentum_short and self.htf.bear and prob_up < (1.0 - self.config.lstm_threshold):
            self._enter(OrderSide.SELL, close, prob_up)

    # ── Feature builder ──────────────────────────────────────────────
    def _build_features(self, high, low, close, volume, cvd_delta) -> list[float]:
        def safe_log_dist(a, b):
            if b <= 0 or a <= 0:
                return 0.0
            return max(-2.0, min(2.0, math.log(a / b)))

        return [
            close,
            volume,
            self.ms.atr,
            high - low,
            cvd_delta,
            float(self.ms.trend),
            float(self.ms.pending),
            float(self.ms.momentum_long),
            float(self.ms.momentum_short),
            float(self.ms.bull_bos),
            float(self.ms.bear_bos),
            float(self.ms.bull_choch),
            float(self.ms.bear_choch),
            float(len(self.ms.swing_highs)),
            float(len(self.ms.swing_lows)),
            safe_log_dist(close, self.ms.watch_high) if self.ms.watch_high else 0.0,
            safe_log_dist(self.ms.watch_low, close) if self.ms.watch_low else 0.0,
            float(self.htf.bull),
            float(self.htf.bear),
            float(self.htf.initialized),
        ]

    # ── Prediction ──────────────────────────────────────────────────
    def _predict_prob_up(self) -> float:
        seq = np.array([list(self._feat_window)], dtype=np.float32)
        seq_scaled = self.scaler.transform(seq.reshape(-1, seq.shape[-1])).reshape(seq.shape)
        # Convert to torch tensor (no GPU)
        X = torch.from_numpy(seq_scaled).float()
        with torch.no_grad():
            prob = self.model(X).item()
        return prob

    # ── Entry (unchanged) ────────────────────────────────────────────
    def _enter(self, side: OrderSide, close: float, prob: float) -> None:
        atr = self.ms.atr
        if atr <= 0:
            return
        is_long = side == OrderSide.BUY
        if is_long:
            self._sl  = close - self.config.sl_atr * atr
            self._tp1 = close + self.config.tp1_atr * atr
            self._tp2 = close + self.config.tp2_atr * atr
        else:
            self._sl  = close + self.config.sl_atr * atr
            self._tp1 = close - self.config.tp1_atr * atr
            self._tp2 = close - self.config.tp2_atr * atr

        self._in_long  = is_long
        self._in_short = not is_long
        self._tp1_hit  = False

        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=self.instrument.make_qty(self.config.trade_size),
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)
        self.log.info(
            f"{'LONG' if is_long else 'SHORT'}  "
            f"entry≈{close:.1f}  sl={self._sl:.1f}  "
            f"tp1={self._tp1:.1f}  tp2={self._tp2:.1f}  prob={prob:.3f}"
        )

    # ── Position management (unchanged) ──────────────────────────────
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
        else:
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
        self.submit_order(self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=self.instrument.make_qty(self.config.trade_size / 2),
            time_in_force=TimeInForce.GTC,
        ))
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
    CVD_PATH      = CATALOG_PATH / "cvd" / "15m.parquet"
    MODEL_PATH    = Path("./lstm_model.pt")
    SCALER_PATH   = Path("./lstm_scaler.pkl")

    catalog    = ParquetDataCatalog(CATALOG_PATH)
    instrument = catalog.instruments(instrument_ids=[INSTRUMENT_ID])[0]
    bars_15m   = catalog.bars(bar_types=[BAR_15M])
    bars_1h    = catalog.bars(bar_types=[BAR_1H])

    print(f"Instrument : {instrument.id}")
    print(f"15m bars   : {len(bars_15m):,}")
    print(f"1H  bars   : {len(bars_1h):,}\n")

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(log_level="WARNING")))
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=None,
        starting_balances=[Money(10_000, USDT)],
    )
    engine.add_instrument(instrument)
    engine.add_data(bars_15m)
    engine.add_data(bars_1h)

    strategy = Layer4Strategy(
        config=Layer4Config(
            instrument_id=InstrumentId.from_str(INSTRUMENT_ID),
            bar_type_15m=BarType.from_str(BAR_15M),
            bar_type_1h=BarType.from_str(BAR_1H),
            cvd_path=CVD_PATH,
            model_path=MODEL_PATH,
            scaler_path=SCALER_PATH,
            lstm_threshold=0.55,
            seq_len=30,
            trade_size=Decimal("0.01"),
            swing_len=10,
            atr_dist=0.5,
            sl_atr=1.5,
            tp1_atr=2.0,
            tp2_atr=3.5,
            htf_period=21,
        )
    )
    engine.add_strategy(strategy)

    print("Running Layer 4 backtest — MS + HTF + LSTM (CPU Keras)…\n")
    engine.run()

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
        keep = [c for c in ["instrument_id", "side", "avg_px_open", "avg_px_close", "realized_pnl", "ts_opened", "ts_closed"] if c in pos_df.columns]
        print(pos_df[keep].to_string(index=False))

    print(f"\n{sep}")
    print("  ACCOUNT REPORT")
    print(sep)
    print(engine.trader.generate_account_report(Venue("BINANCE")).to_string())

    print(f"\n{sep}")
    print("  LAYER 4 SUMMARY  —  MS + HTF + LSTM (CPU Keras)")
    print(sep)

    L1 = dict(trades=464, wr=39.2, pnl=-111.66)
    L2 = dict(trades=304, wr=40.8, pnl=-14.81)
    L3 = dict(trades=278, wr=38.8, pnl=-27.81)

    if not pos_df.empty and "realized_pnl" in pos_df.columns:
        pnl   = pos_df["realized_pnl"].str.split(" ").str[0].astype(float)
        total = pnl.sum()
        n     = len(pnl)
        winners = (pnl > 0).sum()
        losers  = (pnl <= 0).sum()
        wr      = winners / n * 100
        avg_win  = pnl[pnl > 0].mean() if winners > 0 else 0.0
        avg_loss = pnl[pnl <= 0].mean() if losers  > 0 else 0.0
        rr       = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0

        print(f"  {'Metric':<20}  {'Layer 1':>12}  {'Layer 2':>12}  {'Layer 3':>12}  {'Layer 4':>12}")
        print(f"  {'-'*76}")
        print(f"  {'Trades':<20}  {L1['trades']:>12}  {L2['trades']:>12}  {L3['trades']:>12}  {n:>12}")
        print(f"  {'Win rate':<20}  {L1['wr']:>11.1f}%  {L2['wr']:>11.1f}%  {L3['wr']:>11.1f}%  {wr:>11.1f}%")
        print(f"  {'Total PnL (USDT)':<20}  {L1['pnl']:>+12.2f}  {L2['pnl']:>+12.2f}  {L3['pnl']:>+12.2f}  {total:>+12.2f}")
        print(f"  {'-'*76}")
        print(f"  {'Avg win':<20}  {'':>12}  {'':>12}  {'':>12}  {avg_win:>+12.2f}")
        print(f"  {'Avg loss':<20}  {'':>12}  {'':>12}  {'':>12}  {avg_loss:>+12.2f}")
        print(f"  {'Avg R:R':<20}  {'':>12}  {'':>12}  {'':>12}  {'1 : ' + f'{rr:.2f}':>12}")
        print(f"  {'Best trade':<20}  {'':>12}  {'':>12}  {'':>12}  {pnl.max():>+12.2f}")
        print(f"  {'Worst trade':<20}  {'':>12}  {'':>12}  {'':>12}  {pnl.min():>+12.2f}")
        print(f"  {'Final equity':<20}  {'':>12}  {'':>12}  {'':>12}  {10_000 + total:>12,.2f}")
        print()

    print(sep)
    engine.dispose()


if __name__ == "__main__":
    run()