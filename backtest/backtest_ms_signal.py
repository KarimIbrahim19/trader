"""
backtest_ms_signal.py  —  Market Structure used as a STANDALONE entry signal
──────────────────────────────────────────────────────────────────────
Direct mirror of backtest_fvg_signal.py's architecture and feature set —
same multi-position trade ledger, same CLI structure, same reporting
format, same risk-management toggles, same HTF filter, same JSON export
— so MS and FVG can be compared on fully equal footing. Only the signal
source and MS-specific parameters (swing_len, atr_dist) differ.

No filters — pure BOS/CHoCH momentum signal only (unless --htf-filter).

Entry:
    ms.momentum_long   → open a new LONG trade   (can stack)
    ms.momentum_short  → open a new SHORT trade  (can stack)

Exit (checked per-trade, independently):
    SL  → close remaining qty                                  (toggle: --no-sl)
    TP1 → close 50%, leave the rest running                    (always)
    TP2 → close the remaining 50% — two modes:
        FIXED    (default): a fixed price level set at entry.
        TRAILING (--trailing-tp2): once TP1 fires, a reference price
                 starts at the entry price and ratchets in the favorable
                 direction only as new bars arrive. The remaining 50%
                 closes when price pulls back by --trail-atr × ATR from
                 that ratcheted peak. Opposite-signal exit is disabled
                 once trailing is active. SL still applies throughout
                 (unless --no-sl).
    Opposite MS momentum signal → close remaining qty
        (fixed mode / pre-TP1 only; toggle: --no-exit-signal)

Breakeven SL (--breakeven-sl): once TP1 fires, SL moves to entry price.
Independent of TP2 mode — combinable with either fixed or trailing TP2.

HTF filter (--htf-filter): requires the 1H HMA bias to agree with the
signal direction before an entry is allowed — only loads/subscribes to
1H bars when this flag is set, so the default raw-signal run pays no
extra cost. Affects entries only, never exits.

Export (--export <file>): writes params + date range + all three summary
blocks + exit-reason breakdown + the complete per-trade ledger to a JSON
file for later analysis/comparison. No console trade-table dump anymore —
use --export if you need per-trade detail.

Reported PnL is GROSS (entry/exit price difference × qty), excluding
trading fees — the engine's own account balance is printed separately
for a fee-inclusive sanity check.

Usage:
    python backtest_ms_signal.py
    python backtest_ms_signal.py --catalog ./catalog_24 --start 2024-01-01 --end 2024-12-31
    python backtest_ms_signal.py --swing-len 7 --atr-dist 0.3
    python backtest_ms_signal.py --trailing-tp2 --trail-atr 2.0
    python backtest_ms_signal.py --breakeven-sl
    python backtest_ms_signal.py --no-exit-signal
    python backtest_ms_signal.py --no-sl
    python backtest_ms_signal.py --htf-filter
    python backtest_ms_signal.py --export results/ms_default.json
"""

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
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
#  TRADE LEDGER  —  identical structure to backtest_fvg_signal.py
# ══════════════════════════════════════════════════════════════════════
@dataclass
class OpenTrade:
    trade_id:    int
    side:        str        # "LONG" or "SHORT"
    entry_price: float
    entry_ts:    int
    full_qty:    Decimal
    sl:          float
    tp1:         float
    tp2:         float
    tp1_hit:     bool  = False
    realized_pnl: float = 0.0
    exit_ts:     int   = None
    exit_reason: str   = ""
    # Trailing TP2 state — unused (stays None) unless trailing_tp2 is enabled
    best_price:     float = None
    trail_distance: float = None


def summarize_trades(trades: list[OpenTrade]) -> dict | None:
    """Compute count / win-rate / PnL stats from a list of closed trades."""
    if not trades:
        return None
    pnl     = [t.realized_pnl for t in trades]
    n       = len(pnl)
    wins    = [p for p in pnl if p > 0]
    losses  = [p for p in pnl if p <= 0]
    winners = len(wins)
    losers  = len(losses)
    avg_win  = sum(wins)   / winners if winners else 0.0
    avg_loss = sum(losses) / losers  if losers  else 0.0
    return dict(
        trades=n, winners=winners, losers=losers,
        wr=winners / n * 100,
        avg_win=avg_win, avg_loss=avg_loss,
        rr=abs(avg_win / avg_loss) if avg_loss else 0.0,
        total=sum(pnl), best=max(pnl), worst=min(pnl),
    )


def breakdown_by_reason(trades: list[OpenTrade]) -> dict[str, dict]:
    """Group closed trades by exit_reason (SL / BE / TP2 / TP2-trail / exit-signal / EOD)."""
    out: dict[str, dict] = {}
    for t in trades:
        bucket = out.setdefault(t.exit_reason, {"count": 0, "total": 0.0})
        bucket["count"] += 1
        bucket["total"] += t.realized_pnl
    return out


def print_summary(title: str, s: dict | None) -> None:
    print(f"  ── {title} " + "─" * max(1, 50 - len(title)))
    if s is None:
        print("    No trades.")
        return
    print(f"    Trades    : {s['trades']:>5}   (W {s['winners']} / L {s['losers']})")
    print(f"    Win rate  : {s['wr']:.1f}%")
    print(f"    Avg win   : {s['avg_win']:+.2f}    Avg loss: {s['avg_loss']:+.2f}    R:R 1:{s['rr']:.2f}")
    print(f"    Best/Worst: {s['best']:+.2f}  /  {s['worst']:+.2f}")
    print(f"    Total PnL : {s['total']:+.2f}  (gross, excludes fees)")


def export_results(
    path: str, args: argparse.Namespace, label: str, bars_count: int,
    trail_atr_mult: float, closed: list[OpenTrade],
    summary_all: dict | None, summary_long: dict | None, summary_short: dict | None,
    reasons: dict, max_open: int, final_balance: float | None,
) -> None:
    """
    Write the full backtest result — params, date range, all three summary
    blocks, exit-reason breakdown, and the complete per-trade ledger — to
    a single JSON file. Same structure as backtest_fvg_signal.py's export,
    so files from both scripts can be loaded and compared identically.
    """
    def trade_to_dict(t: OpenTrade) -> dict:
        return dict(
            id=t.trade_id,
            side=t.side,
            entry_price=round(t.entry_price, 2),
            entry_time=pd.Timestamp(t.entry_ts, unit="ns", tz="UTC").isoformat(),
            exit_time=(
                pd.Timestamp(t.exit_ts, unit="ns", tz="UTC").isoformat()
                if t.exit_ts is not None else None
            ),
            exit_reason=t.exit_reason,
            realized_pnl=round(t.realized_pnl, 4),
        )

    export = {
        "meta": {
            "script": "backtest_ms_signal.py",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "catalog": args.catalog,
            "instrument": args.instrument,
            "bar_type": args.bar_type,
            "requested_start": args.start,
            "requested_end": args.end,
            "label": label,
            "bars": bars_count,
        },
        "params": {
            "swing_len": args.swing_len,
            "atr_dist": args.atr_dist,
            "atr_len": args.atr_len,
            "sl_atr": args.sl_atr,
            "tp1_atr": args.tp1_atr,
            "tp2_atr": args.tp2_atr,
            "trailing_tp2": args.trailing_tp2,
            "trail_atr_mult": trail_atr_mult,
            "breakeven_sl": args.breakeven_sl,
            "enable_exit_signal": not args.no_exit_signal,
            "enable_sl": not args.no_sl,
            "htf_filter": args.htf_filter,
            "htf_period": args.htf_period,
            "bar_type_1h": args.bar_type_1h if args.htf_filter else None,
        },
        "summary": {
            "all":   summary_all,
            "long":  summary_long,
            "short": summary_short,
            "exit_reasons": reasons,
        },
        "max_open_trades": max_open,
        "engine_ending_balance": final_balance,
        "trades": [trade_to_dict(t) for t in closed],
    }

    with open(path, "w") as f:
        json.dump(export, f, indent=2)

    print(f"  Exported full results → {path}  ({len(closed)} trades)")


# ══════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════
class MsSignalConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type:      BarType
    bar_type_1h:   BarType   # always concrete; only subscribed/loaded if htf_filter=True

    trade_size: Decimal = Decimal("0.01")

    # Market structure
    swing_len: int   = 10
    atr_dist:  float = 0.5
    atr_len:   int   = 14

    # Risk
    sl_atr:  float = 1.5
    tp1_atr: float = 2.0
    tp2_atr: float = 3.5

    # Trailing TP2 — resolved to a concrete value in run() before this
    # config is constructed, so this field is always a plain float here.
    trailing_tp2:   bool  = False
    trail_atr_mult: float = 3.5

    # Move SL to entry price once TP1 has fired — independent of TP2 mode.
    breakeven_sl: bool = False

    # Opposite-signal exit toggle — default True preserves current behavior.
    enable_exit_signal: bool = True

    # Stop-loss toggle — default True preserves current behavior.
    enable_sl: bool = True

    # HTF bias filter — gates ENTRIES only, never exits. Off by default.
    htf_filter: bool = False
    htf_period: int  = 21


# ══════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════
class MsSignalStrategy(Strategy):
    """
    Multi-position Market Structure strategy. Every momentum signal opens
    a NEW trade regardless of how many are already open. Architecture is
    a direct mirror of FvgSignalStrategy for apples-to-apples comparison.
    """

    def __init__(self, config: MsSignalConfig) -> None:
        super().__init__(config)

        self.ms = MarketStructure(
            swing_len = config.swing_len,
            atr_dist  = config.atr_dist,
            atr_len   = config.atr_len,
        )
        self.htf = HTFBias(period=config.htf_period)

        self._bar_count: int = 0
        self._next_id:   int = 1
        self.open_trades:   list[OpenTrade] = []
        self.closed_trades: list[OpenTrade] = []
        self.max_open_trades: int = 0   # peak concurrent open trades, for diagnostics

        self._last_close: float = 0.0
        self._last_ts:    int   = 0

    # ── Lifecycle ─────────────────────────────────────────────────────
    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        self.subscribe_bars(self.config.bar_type)
        if self.config.htf_filter:
            self.subscribe_bars(self.config.bar_type_1h)
        self.log.info("MS signal-mode (multi-position) strategy started")

    def on_stop(self) -> None:
        for t in list(self.open_trades):
            frac = 0.5 if t.tp1_hit else 1.0
            self._close_trade(t, self._last_close, frac, self._last_ts, "EOD", final=True)
        self.open_trades = []

        # Safety net only — guarded so it doesn't fire (and log a
        # spurious rejection) once everything is already flat.
        if not self.portfolio.is_flat(self.config.instrument_id):
            self.close_all_positions(self.config.instrument_id)

    # ── Main bar handler ──────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> None:
        # 1H bars (only ever arrive if htf_filter subscribed to them) →
        # update HTF bias only, never touch the 15m trade logic.
        if bar.bar_type == self.config.bar_type_1h:
            self.htf.update(bar.close.as_double())
            return

        high  = bar.high.as_double()
        low   = bar.low.as_double()
        close = bar.close.as_double()
        ts    = bar.ts_init

        self.ms.update(high, low, close, self._bar_count)
        self._bar_count += 1
        self._last_close, self._last_ts = close, ts

        # 1) Manage every currently open trade
        self._manage_open_trades(high, low, close, ts)

        # 2) New entries — allowed even while trades are already open.
        #    HTF gate: `not htf_filter` short-circuits when the filter is
        #    off, so the raw signal is completely unaffected by default.
        if self.ms.momentum_long and (not self.config.htf_filter or self.htf.bull):
            self._enter("LONG", close, ts)
        if self.ms.momentum_short and (not self.config.htf_filter or self.htf.bear):
            self._enter("SHORT", close, ts)

        if len(self.open_trades) > self.max_open_trades:
            self.max_open_trades = len(self.open_trades)

    # ── Entry ─────────────────────────────────────────────────────────
    def _enter(self, side: str, close: float, ts: int) -> None:
        atr = self.ms.atr
        if atr <= 0:
            return

        if side == "LONG":
            sl, tp1, tp2 = (
                close - self.config.sl_atr  * atr,
                close + self.config.tp1_atr * atr,
                close + self.config.tp2_atr * atr,
            )
            order_side = OrderSide.BUY
        else:
            sl, tp1, tp2 = (
                close + self.config.sl_atr  * atr,
                close - self.config.tp1_atr * atr,
                close - self.config.tp2_atr * atr,
            )
            order_side = OrderSide.SELL

        trade = OpenTrade(
            trade_id    = self._next_id,
            side        = side,
            entry_price = close,
            entry_ts    = ts,
            full_qty    = self.config.trade_size,
            sl=sl, tp1=tp1, tp2=tp2,
        )
        self._next_id += 1
        self.open_trades.append(trade)

        order = self.order_factory.market(
            instrument_id = self.config.instrument_id,
            order_side    = order_side,
            quantity      = self.instrument.make_qty(trade.full_qty),
            time_in_force = TimeInForce.GTC,
        )
        self.submit_order(order)

        self.log.info(
            f"#{trade.trade_id:05d} OPEN {side:<5} entry≈{close:.1f}  "
            f"sl={sl:.1f} tp1={tp1:.1f} tp2={tp2:.1f}  "
            f"(open={len(self.open_trades)})"
        )

    # ── Position management — checked independently per trade ─────────
    def _manage_open_trades(self, high: float, low: float, close: float, ts: int) -> None:
        still_open: list[OpenTrade] = []
        atr = self.ms.atr   # live ATR, used to size the trail distance at activation

        for t in self.open_trades:
            if t.side == "LONG":
                if self.config.enable_sl and low <= t.sl:
                    frac   = 0.5 if t.tp1_hit else 1.0
                    reason = "BE" if (t.tp1_hit and self.config.breakeven_sl) else "SL"
                    self._close_trade(t, t.sl, frac, ts, reason, final=True)
                    continue

                if not t.tp1_hit:
                    if high >= t.tp1:
                        self._close_trade(t, t.tp1, 0.5, ts, "TP1", final=False)
                        t.tp1_hit = True
                        if self.config.breakeven_sl:
                            t.sl = t.entry_price
                        if self.config.trailing_tp2:
                            t.best_price     = t.entry_price
                            t.trail_distance = self.config.trail_atr_mult * atr
                        still_open.append(t)
                    elif self.config.enable_exit_signal and self.ms.momentum_short:
                        self._close_trade(t, close, 1.0, ts, "exit-signal", final=True)
                    else:
                        still_open.append(t)
                    continue

                if self.config.trailing_tp2:
                    t.best_price = max(t.best_price, high)
                    trail_trigger = t.best_price - t.trail_distance
                    if low <= trail_trigger:
                        self._close_trade(t, trail_trigger, 0.5, ts, "TP2-trail", final=True)
                    else:
                        still_open.append(t)
                else:
                    if high >= t.tp2:
                        self._close_trade(t, t.tp2, 0.5, ts, "TP2", final=True)
                    elif self.config.enable_exit_signal and self.ms.momentum_short:
                        self._close_trade(t, close, 0.5, ts, "exit-signal", final=True)
                    else:
                        still_open.append(t)

            else:  # SHORT
                if self.config.enable_sl and high >= t.sl:
                    frac   = 0.5 if t.tp1_hit else 1.0
                    reason = "BE" if (t.tp1_hit and self.config.breakeven_sl) else "SL"
                    self._close_trade(t, t.sl, frac, ts, reason, final=True)
                    continue

                if not t.tp1_hit:
                    if low <= t.tp1:
                        self._close_trade(t, t.tp1, 0.5, ts, "TP1", final=False)
                        t.tp1_hit = True
                        if self.config.breakeven_sl:
                            t.sl = t.entry_price
                        if self.config.trailing_tp2:
                            t.best_price     = t.entry_price
                            t.trail_distance = self.config.trail_atr_mult * atr
                        still_open.append(t)
                    elif self.config.enable_exit_signal and self.ms.momentum_long:
                        self._close_trade(t, close, 1.0, ts, "exit-signal", final=True)
                    else:
                        still_open.append(t)
                    continue

                if self.config.trailing_tp2:
                    t.best_price = min(t.best_price, low)
                    trail_trigger = t.best_price + t.trail_distance
                    if high >= trail_trigger:
                        self._close_trade(t, trail_trigger, 0.5, ts, "TP2-trail", final=True)
                    else:
                        still_open.append(t)
                else:
                    if low <= t.tp2:
                        self._close_trade(t, t.tp2, 0.5, ts, "TP2", final=True)
                    elif self.config.enable_exit_signal and self.ms.momentum_long:
                        self._close_trade(t, close, 0.5, ts, "exit-signal", final=True)
                    else:
                        still_open.append(t)

        self.open_trades = still_open

    # ── Close (partial or final) ────────────────────────────────────────
    def _close_trade(
        self, trade: OpenTrade, exit_price: float, qty_frac: float,
        ts: int, reason: str, final: bool,
    ) -> None:
        qty_closed = trade.full_qty * Decimal(str(qty_frac))

        if trade.side == "LONG":
            pnl = (exit_price - trade.entry_price) * float(qty_closed)
            order_side = OrderSide.SELL
        else:
            pnl = (trade.entry_price - exit_price) * float(qty_closed)
            order_side = OrderSide.BUY

        trade.realized_pnl += pnl

        order = self.order_factory.market(
            instrument_id = self.config.instrument_id,
            order_side    = order_side,
            quantity      = self.instrument.make_qty(qty_closed),
            time_in_force = TimeInForce.GTC,
        )
        self.submit_order(order)

        self.log.info(
            f"#{trade.trade_id:05d} {reason:<11} {trade.side:<5} "
            f"exit≈{exit_price:.1f}  frac={qty_frac:.2f}  "
            f"leg_pnl={pnl:+.2f}  cum_pnl={trade.realized_pnl:+.2f}"
        )

        if final:
            trade.exit_ts     = ts
            trade.exit_reason = reason
            self.closed_trades.append(trade)


# ══════════════════════════════════════════════════════════════════════
#  CLI + DATE FILTERING
# ══════════════════════════════════════════════════════════════════════
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest Market Structure as a standalone, multi-position entry signal"
    )
    p.add_argument("--catalog",  default="./catalog")
    p.add_argument("--start",    default=None)
    p.add_argument("--end",      default=None)
    p.add_argument("--bar-type", default="BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL")
    p.add_argument("--instrument", default="BTCUSDT-PERP.BINANCE")

    p.add_argument("--swing-len", type=int,   default=10,
                    help="Pivot lookback bars each side (default: 10)")
    p.add_argument("--atr-dist",  type=float, default=0.5,
                    help="Min ATR distance between opposite pivots (default: 0.5)")
    p.add_argument("--atr-len",   type=int,   default=14,
                    help="ATR period used inside the MS engine (default: 14)")

    p.add_argument("--sl-atr",  type=float, default=1.5)
    p.add_argument("--tp1-atr", type=float, default=2.0)
    p.add_argument("--tp2-atr", type=float, default=3.5)

    # ── TP2 mode: fixed (default) or trailing ───────────────────────────
    p.add_argument("--trailing-tp2", action="store_true",
                    help="Use a trailing TP2 instead of a fixed price level (default: fixed)")
    p.add_argument("--trail-atr", type=float, default=None,
                    help="Trailing callback distance = ATR × this value. "
                         "Defaults to --tp2-atr if not set.")

    p.add_argument("--breakeven-sl", action="store_true",
                    help="Move SL to entry price once TP1 has fired (default: SL stays fixed)")

    p.add_argument("--no-exit-signal", action="store_true",
                    help="Disable the opposite-signal exit entirely; rely only on "
                         "SL/TP1/TP2 (default: exit-signal enabled)")

    p.add_argument("--no-sl", action="store_true",
                    help="Disable the stop loss entirely; trades only close via "
                         "TP1/TP2(-trail)/exit-signal/EOD (default: SL enabled)")

    p.add_argument("--htf-filter", action="store_true",
                    help="Require 1H HTF HMA bias to agree with the signal direction "
                         "before entry (default: off, raw signal only)")
    p.add_argument("--htf-period", type=int, default=21,
                    help="HTF HMA period (default: 21)")
    p.add_argument("--bar-type-1h", default="BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
                    help="1H bar type for HTF bias (only loaded if --htf-filter is set)")

    p.add_argument("--export", type=str, default=None,
                    help="Export full trade ledger + params + summary to this JSON "
                         "file path (default: no export, nothing written)")

    return p.parse_args()


def filter_bars_by_date(bars: list, start: str | None, end: str | None) -> list:
    if not start and not end:
        return bars
    start_ns = int(pd.Timestamp(start, tz="UTC").timestamp() * 1e9) if start else 0
    end_ns   = (int(pd.Timestamp(end, tz="UTC").timestamp() * 1e9) + 86_399_000_000_000
                if end else 2**63 - 1)
    return [b for b in bars if start_ns <= b.ts_init <= end_ns]


# ══════════════════════════════════════════════════════════════════════
#  RUNNER
# ══════════════════════════════════════════════════════════════════════
def run() -> None:
    args = parse_args()

    catalog    = ParquetDataCatalog(Path(args.catalog))
    instrument = catalog.instruments(instrument_ids=[args.instrument])[0]
    bars       = catalog.bars(bar_types=[args.bar_type])
    bars       = filter_bars_by_date(bars, args.start, args.end)

    if not bars:
        print("No bars in the selected date range — check --start/--end.")
        return

    # Resolve trailing distance: explicit --trail-atr, else fall back to --tp2-atr
    trail_atr_mult = args.trail_atr if args.trail_atr is not None else args.tp2_atr
    tp2_desc = (
        f"TRAILING {trail_atr_mult}×ATR" if args.trailing_tp2
        else f"{args.tp2_atr}×ATR (fixed)"
    )

    # Only load 1H bars if the HTF filter is actually requested.
    bars_1h = []
    if args.htf_filter:
        bars_1h = catalog.bars(bar_types=[args.bar_type_1h])
        bars_1h = filter_bars_by_date(bars_1h, args.start, args.end)
        if not bars_1h:
            print(
                f"ERROR: --htf-filter was set but no 1H bars were found for "
                f"{args.bar_type_1h} in this date range. Aborting — without this "
                f"data the filter would silently block every entry, producing a "
                f"misleading zero-trade result."
            )
            return

    label = f"{args.start or 'start'} → {args.end or 'end'}"
    print(f"Catalog    : {args.catalog}")
    print(f"Instrument : {instrument.id}")
    print(f"Date range : {label}")
    print(f"Bars       : {len(bars):,}" + (f"  (+ {len(bars_1h):,} 1H bars)" if bars_1h else ""))
    print(
        f"Params     : swing_len={args.swing_len}  atr_dist={args.atr_dist}  atr_len={args.atr_len}  "
        f"sl={'OFF' if args.no_sl else f'{args.sl_atr}×ATR'}"
        f"{' →BE after TP1' if (args.breakeven_sl and not args.no_sl) else ''}  "
        f"tp1={args.tp1_atr}×ATR  tp2={tp2_desc}  "
        f"exit-signal={'off' if args.no_exit_signal else 'on'}  "
        f"htf-filter={'on (period=' + str(args.htf_period) + ')' if args.htf_filter else 'off'}"
    )
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
    engine.add_data(bars)
    if bars_1h:
        engine.add_data(bars_1h)

    strategy = MsSignalStrategy(
        config=MsSignalConfig(
            instrument_id      = InstrumentId.from_str(args.instrument),
            bar_type           = BarType.from_str(args.bar_type),
            bar_type_1h        = BarType.from_str(args.bar_type_1h),
            trade_size         = Decimal("0.01"),
            swing_len          = args.swing_len,
            atr_dist           = args.atr_dist,
            atr_len            = args.atr_len,
            sl_atr             = args.sl_atr,
            tp1_atr            = args.tp1_atr,
            tp2_atr            = args.tp2_atr,
            trailing_tp2       = args.trailing_tp2,
            trail_atr_mult     = trail_atr_mult,
            breakeven_sl       = args.breakeven_sl,
            enable_exit_signal = not args.no_exit_signal,
            enable_sl          = not args.no_sl,
            htf_filter         = args.htf_filter,
            htf_period         = args.htf_period,
        )
    )
    engine.add_strategy(strategy)

    print("Running MS multi-position backtest…\n")
    engine.run()

    closed = strategy.closed_trades
    longs  = [t for t in closed if t.side == "LONG"]
    shorts = [t for t in closed if t.side == "SHORT"]

    sep = "═" * 72
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 150)

    if not closed:
        print(sep)
        print("  No trades — MS momentum signal never fired in this date range.")
        print(sep)

    # ── Summary by direction ─────────────────────────────────────────────
    summary_all   = summarize_trades(closed)
    summary_long  = summarize_trades(longs)
    summary_short = summarize_trades(shorts)

    print(sep)
    print(f"  TRADE LEDGER SUMMARY   [{label}]")
    print(sep)
    print_summary("ALL TRADES", summary_all)
    print()
    print_summary("LONG ONLY", summary_long)
    print()
    print_summary("SHORT ONLY", summary_short)
    print(sep)
    print("  (Full per-trade ledger not printed to console — use --export <file>")
    print("   to save every trade plus this summary to a JSON file for analysis.)")

    # ── Exit-reason breakdown ────────────────────────────────────────────
    reasons: dict = {}
    if closed:
        reasons = breakdown_by_reason(closed)
        print(f"\n  ── EXIT REASONS ──" + "─" * 38)
        for reason, stats in sorted(reasons.items(), key=lambda x: -x[1]["total"]):
            print(f"    {reason:<12}: {stats['count']:>5} trades   total pnl {stats['total']:+.2f}")
        eod = reasons.get("EOD", {}).get("count", 0)
        if eod:
            print(f"\n    Note: {eod} trade(s) were forced closed at the test's last")
            print(f"    bar (no real SL/TP/signal exit) — their PnL is an artifact")
            print(f"    of the test boundary, not a genuine trading outcome.")
        print(sep)

    print(f"  Max concurrent open trades: {strategy.max_open_trades}")
    if args.no_sl and strategy.max_open_trades > 50:
        print(f"  (High open-trade count is expected with --no-sl — without a")
        print(f"   floor, losers can stay open a long time before anything closes them.)")

    # ── Sanity check against the venue's own netted account ─────────────
    account_report = engine.trader.generate_account_report(Venue("BINANCE"))
    final_balance = None
    if not account_report.empty:
        final_balance = float(account_report.iloc[-1]["total"])
        print(
            f"  Engine ending balance (incl. fees): {final_balance:,.2f} USDT  "
            f"(net change: {final_balance - 10_000:+.2f})"
        )
        print("  ^ This is the venue's real netted account result — it will differ")
        print("    slightly from the gross ledger total above because it includes")
        print("    trading fees and reflects NETTING-blended fills, not per-trade IDs.")

    # ── Export (only if --export was passed) ────────────────────────────
    if args.export:
        export_results(
            args.export, args, label, len(bars), trail_atr_mult, closed,
            summary_all, summary_long, summary_short, reasons,
            strategy.max_open_trades, final_balance,
        )

    engine.dispose()


if __name__ == "__main__":
    run()