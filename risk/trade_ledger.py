"""
risk/trade_ledger.py
────────────────────────────────────────────────────────────────────────
Per-trade record keeping, completely independent of the signal source
and the NautilusTrader venue model.

This is a direct port of the OpenTrade ledger shared across both
backtest scripts (backtest_ms_signal.py / backtest_fvg_signal.py),
which are guaranteed to be feature-identical. Every backtesting finding
about trade lifecycle, partial closes, and PnL attribution applies here
without modification.

The TradeLedger is the single source of truth for all PnL reporting.
The venue's netted account balance is used only as a sanity check.

Key design rules (same as backtest):
  • A trade enters open_trades when it is opened.
  • A trade STAYS in open_trades after TP1 (partial close). It is not
    moved to closed_trades until the second leg fully resolves.
  • A trade moves to closed_trades only when final=True is passed to
    record_close().
  • realized_pnl accumulates on the trade across partial closes, so
    the final value always reflects the total net result.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


# ── Per-trade record ──────────────────────────────────────────────────────
@dataclass
class OpenTrade:
    """
    Single trade record. Mirrors the backtest OpenTrade dataclass exactly
    so backtesting output and live output share the same structure.
    """
    trade_id:     int
    side:         str            # "LONG" or "SHORT"
    entry_price:  float
    entry_ts:     int            # bar.ts_init at entry (nanoseconds UTC)
    full_qty:     Decimal
    sl:           float
    tp1:          float
    tp2:          float
    tp1_hit:      bool           = False
    realized_pnl: float          = 0.0    # accumulates across partial closes
    exit_ts:      Optional[int]  = None
    exit_reason:  str            = ""
    # Trailing TP2 state — both None unless trailing_tp2 is enabled
    best_price:     Optional[float] = None   # ratchets favorably only
    trail_distance: Optional[float] = None   # frozen ATR × mult, set at TP1 fire


# ── Utility functions ─────────────────────────────────────────────────────
def summarize_trades(trades: list[OpenTrade]) -> dict | None:
    """
    Compute count / win-rate / avg win / avg loss / R:R / total PnL
    from a list of fully closed trades.
    Returns None if the list is empty (matches backtest output format).
    """
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
    """
    Group closed trades by exit_reason.
    Possible reasons: SL / BE / TP1 / TP2 / TP2-trail / exit-signal / RESTART
    """
    out: dict[str, dict] = {}
    for t in trades:
        bucket = out.setdefault(t.exit_reason, {"count": 0, "total": 0.0})
        bucket["count"] += 1
        bucket["total"] += t.realized_pnl
    return out


# ── Ledger manager ────────────────────────────────────────────────────────
class TradeLedger:
    """
    Manages the full list of open and closed trades for the current session.

    The position manager manipulates open_trades directly (same pattern as
    the backtest scripts — build a still_open list, assign it back after
    the management loop). record_close() only handles the closed_trades
    append; it does not remove from open_trades.

    This keeps the lifecycle crystal-clear:
      open_trades   → set by position manager after each bar's management loop
      closed_trades → appended by record_close() when final=True
    """

    def __init__(self) -> None:
        self._next_id:    int               = 1
        self.open_trades:   list[OpenTrade] = []
        self.closed_trades: list[OpenTrade] = []
        self._peak_open:  int               = 0

    # ── ID generation ─────────────────────────────────────────────────
    def next_trade_id(self) -> int:
        """Return the next sequential trade ID and advance the counter."""
        tid = self._next_id
        self._next_id += 1
        return tid

    # ── Trade lifecycle ───────────────────────────────────────────────
    def record_open(self, trade: OpenTrade) -> None:
        """Register a newly opened trade and update peak-open diagnostic."""
        self.open_trades.append(trade)
        if len(self.open_trades) > self._peak_open:
            self._peak_open = len(self.open_trades)

    def record_close(self, trade: OpenTrade, final: bool) -> None:
        """
        Called on every partial or final close.

        When final=False (TP1 partial): just accumulates — trade stays in
        open_trades. The position manager keeps it in still_open.

        When final=True: appends to closed_trades. The position manager
        does NOT include it in still_open, so it is naturally excluded
        from the next bar's open_trades assignment.

        Important: this method does NOT remove from open_trades. The
        position manager does that implicitly by not adding the trade to
        its still_open list, then assigning still_open → open_trades.
        """
        if final:
            self.closed_trades.append(trade)

    # ── Diagnostics ───────────────────────────────────────────────────
    @property
    def open_count(self) -> int:
        return len(self.open_trades)

    @property
    def peak_open(self) -> int:
        return self._peak_open

    @property
    def daily_pnl(self) -> float:
        """Gross realized PnL of all fully closed trades this session."""
        return sum(t.realized_pnl for t in self.closed_trades)

    @property
    def next_id(self) -> int:
        """Current value of the ID counter — needed for persistence."""
        return self._next_id

    # ── Summary helpers ───────────────────────────────────────────────
    def summary_all(self) -> dict | None:
        return summarize_trades(self.closed_trades)

    def summary_long(self) -> dict | None:
        longs = [t for t in self.closed_trades if t.side == "LONG"]
        return summarize_trades(longs)

    def summary_short(self) -> dict | None:
        shorts = [t for t in self.closed_trades if t.side == "SHORT"]
        return summarize_trades(shorts)

    def exit_breakdown(self) -> dict[str, dict]:
        return breakdown_by_reason(self.closed_trades)

    def print_summary(self, log) -> None:
        """Log a formatted summary block — called from strategy on_stop()."""
        sep = "═" * 60

        def _fmt(title: str, s: dict | None) -> None:
            log.info(f"  ── {title}")
            if s is None:
                log.info("    No trades.")
                return
            log.info(
                f"    Trades : {s['trades']}  (W {s['winners']} / L {s['losers']})  "
                f"WR {s['wr']:.1f}%"
            )
            log.info(
                f"    Avg win: {s['avg_win']:+.2f}  Avg loss: {s['avg_loss']:+.2f}  "
                f"R:R 1:{s['rr']:.2f}"
            )
            log.info(
                f"    Best/Worst: {s['best']:+.2f} / {s['worst']:+.2f}   "
                f"Total PnL: {s['total']:+.2f} (gross)"
            )

        log.info(sep)
        log.info("  TRADE LEDGER SUMMARY  (session)")
        log.info(sep)
        _fmt("ALL TRADES",  self.summary_all())
        _fmt("LONG ONLY",   self.summary_long())
        _fmt("SHORT ONLY",  self.summary_short())
        log.info(sep)

        reasons = self.exit_breakdown()
        if reasons:
            log.info("  EXIT REASONS")
            for reason, stats in sorted(reasons.items(), key=lambda x: -x[1]["total"]):
                log.info(
                    f"    {reason:<14} : {stats['count']:4d} trades  "
                    f"total pnl {stats['total']:+.2f}"
                )
        log.info(f"  Peak concurrent open trades : {self._peak_open}")
        log.info(sep)

    # ── Persistence support ───────────────────────────────────────────
    def restore_from_persistence(
        self, open_trades: list[OpenTrade], next_id: int
    ) -> None:
        """
        Restore open trade state after a process restart.
        Called by the strategy's on_start() if StateStore finds saved data.
        Closed trades from the previous session are not restored — they
        live in the JSON export history instead.
        """
        self.open_trades = open_trades
        self._next_id    = next_id
        self._peak_open  = len(open_trades)