"""
risk/position_manager.py
────────────────────────────────────────────────────────────────────────
Signal-agnostic position management for the live SMC trading system.

Stage 4 additions:
  • strategy_id param — passed through to every notifier call so Telegram
    messages are labelled [MS-CLTRADER-001] / [FVG-CLTRADER-001].
  • notifier param — optional TelegramNotifier (duck-typed, no import).
    Every call is wrapped in try/except so a notifier error never affects
    the trading loop.
  • _kill_switch_notified flag — prevents spamming the kill-switch message
    on every bar once the daily limit is hit.
  • TP1 notification happens in _manage_open_trades() AFTER tp1_hit=True
    and SL update, so the message shows the correct new SL level.
  • Final close notification happens inside _close_trade() after state
    is finalised.

Crash-safety (dirty flag):
  • _state_dirty flag — set True in _enter() and _close_trade() whenever
    the ledger changes. BaseSmcStrategy.on_bar() calls flush_state() after
    pm.on_bar() completes and saves to disk if dirty. The save happens
    after ALL on_bar() logic for the bar is done, so tp1_hit, new SL,
    best_price etc. are all captured in one consistent write.
  • on_stop() keeps its save as a belt-and-suspenders safety net.

Everything else is unchanged from Stage 3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from risk.trade_ledger import OpenTrade, TradeLedger

SubmitOrderFn = Callable[[str, Decimal], None]


# ── Config ────────────────────────────────────────────────────────────────
@dataclass
class PositionManagerConfig:
    trade_size:            Decimal
    sl_atr:                float
    tp1_atr:               float
    tp2_atr:               float
    trailing_tp2:          bool
    trail_atr_mult:        float
    breakeven_sl:          bool
    enable_exit_signal:    bool
    enable_sl:             bool
    max_open_trades:       int
    daily_loss_limit_usdt: float


# ── Manager ───────────────────────────────────────────────────────────────
class PositionManager:

    def __init__(
        self,
        config:          PositionManagerConfig,
        ledger:          TradeLedger,
        submit_order_fn: SubmitOrderFn,
        log:             logging.Logger,
        strategy_id:     str = "",
        notifier:        Optional[object] = None,   # TelegramNotifier, duck-typed
    ) -> None:
        self.config       = config
        self.ledger       = ledger
        self._submit      = submit_order_fn
        self.log          = log
        self._strategy_id = strategy_id
        self._notifier    = notifier

        self._last_close: float = 0.0
        self._last_ts:    int   = 0

        # Prevent repeated kill-switch messages after daily limit is hit
        self._kill_switch_notified: bool = False

        # Dirty flag — set True whenever ledger state changes.
        # BaseSmcStrategy.on_bar() reads and resets this via flush_state()
        # and saves to StateStore when True.
        self._state_dirty: bool = False

    # ── Main update ───────────────────────────────────────────────────────
    def on_bar(
        self,
        high:         float,
        low:          float,
        close:        float,
        atr:          float,
        ts:           int,
        long_signal:  bool,
        short_signal: bool,
    ) -> None:
        self._last_close = close
        self._last_ts    = ts

        self._manage_open_trades(high, low, close, atr, ts, long_signal, short_signal)

        if long_signal and self._can_enter():
            self._enter("LONG", close, atr, ts)

        if short_signal and self._can_enter():
            self._enter("SHORT", close, atr, ts)

    # ── Entry ─────────────────────────────────────────────────────────────
    def _enter(self, side: str, close: float, atr: float, ts: int) -> None:
        if atr <= 0:
            return

        if side == "LONG":
            sl         = close - self.config.sl_atr  * atr
            tp1        = close + self.config.tp1_atr * atr
            tp2        = close + self.config.tp2_atr * atr
            order_side = "BUY"
        else:
            sl         = close + self.config.sl_atr  * atr
            tp1        = close - self.config.tp1_atr * atr
            tp2        = close - self.config.tp2_atr * atr
            order_side = "SELL"

        trade = OpenTrade(
            trade_id    = self.ledger.next_trade_id(),
            side        = side,
            entry_price = close,
            entry_ts    = ts,
            full_qty    = self.config.trade_size,
            sl=sl, tp1=tp1, tp2=tp2,
        )
        self.ledger.record_open(trade)
        self._submit(order_side, self.config.trade_size)
        self._state_dirty = True   # new trade — persist after this bar

        self.log.info(
            f"OPEN  #{trade.trade_id:05d} {side:<5}  entry≈{close:.1f}  "
            f"sl={sl:.1f}  tp1={tp1:.1f}  tp2={tp2:.1f}  "
            f"atr={atr:.1f}  open={self.ledger.open_count}"
        )

        self._notify("on_trade_opened", trade, self._strategy_id)

    # ── Position management ───────────────────────────────────────────────
    def _manage_open_trades(
        self,
        high: float, low: float, close: float, atr: float, ts: int,
        long_signal: bool, short_signal: bool,
    ) -> None:
        still_open: list[OpenTrade] = []

        for t in self.ledger.open_trades:

            if t.side == "LONG":
                if self.config.enable_sl and low <= t.sl:
                    frac   = 0.5 if t.tp1_hit else 1.0
                    reason = "BE" if (t.tp1_hit and self.config.breakeven_sl) else "SL"
                    self._close_trade(t, t.sl, frac, ts, reason, final=True)
                    continue

                if not t.tp1_hit:
                    if high >= t.tp1:
                        pnl_before  = t.realized_pnl
                        self._close_trade(t, t.tp1, 0.5, ts, "TP1", final=False)
                        tp1_leg_pnl = t.realized_pnl - pnl_before

                        t.tp1_hit = True
                        if self.config.breakeven_sl:
                            t.sl = t.entry_price
                        if self.config.trailing_tp2:
                            t.best_price     = t.entry_price
                            t.trail_distance = self.config.trail_atr_mult * atr
                        still_open.append(t)

                        # Notify AFTER state update so message shows new SL
                        self._notify("on_tp1_hit", t, self._strategy_id, tp1_leg_pnl)

                    elif self.config.enable_exit_signal and short_signal:
                        self._close_trade(t, close, 1.0, ts, "exit-signal", final=True)
                    else:
                        still_open.append(t)
                    continue

                if self.config.trailing_tp2:
                    t.best_price  = max(t.best_price, high)
                    trail_trigger = t.best_price - t.trail_distance
                    if low <= trail_trigger:
                        self._close_trade(t, trail_trigger, 0.5, ts, "TP2-trail", final=True)
                    else:
                        still_open.append(t)
                else:
                    if high >= t.tp2:
                        self._close_trade(t, t.tp2, 0.5, ts, "TP2", final=True)
                    elif self.config.enable_exit_signal and short_signal:
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
                        pnl_before  = t.realized_pnl
                        self._close_trade(t, t.tp1, 0.5, ts, "TP1", final=False)
                        tp1_leg_pnl = t.realized_pnl - pnl_before

                        t.tp1_hit = True
                        if self.config.breakeven_sl:
                            t.sl = t.entry_price
                        if self.config.trailing_tp2:
                            t.best_price     = t.entry_price
                            t.trail_distance = self.config.trail_atr_mult * atr
                        still_open.append(t)

                        self._notify("on_tp1_hit", t, self._strategy_id, tp1_leg_pnl)

                    elif self.config.enable_exit_signal and long_signal:
                        self._close_trade(t, close, 1.0, ts, "exit-signal", final=True)
                    else:
                        still_open.append(t)
                    continue

                if self.config.trailing_tp2:
                    t.best_price  = min(t.best_price, low)
                    trail_trigger = t.best_price + t.trail_distance
                    if high >= trail_trigger:
                        self._close_trade(t, trail_trigger, 0.5, ts, "TP2-trail", final=True)
                    else:
                        still_open.append(t)
                else:
                    if low <= t.tp2:
                        self._close_trade(t, t.tp2, 0.5, ts, "TP2", final=True)
                    elif self.config.enable_exit_signal and long_signal:
                        self._close_trade(t, close, 0.5, ts, "exit-signal", final=True)
                    else:
                        still_open.append(t)

        self.ledger.open_trades = still_open

    # ── Close (partial or final) ──────────────────────────────────────────
    def _close_trade(
        self,
        trade:      OpenTrade,
        exit_price: float,
        qty_frac:   float,
        ts:         int,
        reason:     str,
        final:      bool,
    ) -> None:
        qty_closed = trade.full_qty * Decimal(str(qty_frac))

        if trade.side == "LONG":
            pnl        = (exit_price - trade.entry_price) * float(qty_closed)
            order_side = "SELL"
        else:
            pnl        = (trade.entry_price - exit_price) * float(qty_closed)
            order_side = "BUY"

        trade.realized_pnl += pnl
        self._submit(order_side, qty_closed)

        self.log.info(
            f"{reason:<12} #{trade.trade_id:05d} {trade.side:<5}  "
            f"exit≈{exit_price:.1f}  frac={qty_frac:.2f}  "
            f"leg_pnl={pnl:+.2f}  cum_pnl={trade.realized_pnl:+.2f}"
        )

        if final:
            trade.exit_ts     = ts
            trade.exit_reason = reason
            self.ledger.record_close(trade, final=True)
            # Notify final close (TP1 notified separately in _manage_open_trades)
            duration_secs = (ts - trade.entry_ts) / 1e9
            self._notify("on_trade_closed", trade, self._strategy_id, pnl, duration_secs)
        else:
            self.ledger.record_close(trade, final=False)

        self._state_dirty = True   # any close (partial or final) — persist after this bar

    # ── Dirty-flag flush ──────────────────────────────────────────────────
    def flush_state(self) -> bool:
        """
        Returns True if ledger state changed since the last call (trade
        opened, TP1 hit, or trade closed). Resets the flag.

        Called by BaseSmcStrategy.on_bar() after pm.on_bar() completes.
        Saving AFTER the full bar logic ensures tp1_hit, new SL, best_price,
        and trail_distance are all written in one consistent snapshot —
        never a half-updated state.
        """
        dirty = self._state_dirty
        self._state_dirty = False
        return dirty

    # ── Entry gate ────────────────────────────────────────────────────────
    def _can_enter(self) -> bool:
        if self.ledger.open_count >= self.config.max_open_trades:
            reason = (
                f"max open trades "
                f"({self.ledger.open_count}/{self.config.max_open_trades})"
            )
            self.log.debug(f"Entry blocked — {reason}")
            self._notify("on_entry_blocked", reason, self._strategy_id)
            return False

        if self.ledger.daily_pnl <= -abs(self.config.daily_loss_limit_usdt):
            self.log.warning(
                f"Entry blocked — daily PnL {self.ledger.daily_pnl:.2f} "
                f"hit limit -{self.config.daily_loss_limit_usdt:.2f}"
            )
            if not self._kill_switch_notified:
                self._kill_switch_notified = True
                self._notify(
                    "on_kill_switch",
                    self._strategy_id,
                    self.ledger.daily_pnl,
                    self.config.daily_loss_limit_usdt,
                )
            return False

        return True

    # ── Graceful stop ─────────────────────────────────────────────────────
    def on_stop(self, reason: str = "RESTART") -> None:
        if self.ledger.open_count == 0:
            self.log.info("PositionManager stopped — no open trades.")
            return
        self.log.warning(
            f"PositionManager stopped ({reason}) with "
            f"{self.ledger.open_count} open trade(s). "
            f"Positions remain on exchange — state will be saved."
        )
        for t in self.ledger.open_trades:
            self.log.warning(
                f"  OPEN #{t.trade_id:05d} {t.side:<5}  "
                f"entry={t.entry_price:.1f}  "
                f"pnl_so_far={t.realized_pnl:+.2f}  "
                f"tp1_hit={t.tp1_hit}"
            )

    # ── Notifier helper ───────────────────────────────────────────────────
    def _notify(self, method: str, *args) -> None:
        """
        Call a notifier method by name. Any exception is caught and logged
        so notifier failures can never affect the trading loop.
        """
        if self._notifier is None:
            return
        fn = getattr(self._notifier, method, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            self.log.warning(f"Notifier {method} error: {e}")