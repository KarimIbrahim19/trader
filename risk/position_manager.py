"""
risk/position_manager.py
────────────────────────────────────────────────────────────────────────
Hedge mode support:
  • PositionManagerConfig.position_mode: "netting" (default) or "hedge".
  • Netting (unchanged): one blended exchange position per instrument.
    A new opposite-direction signal while a trade is open triggers
    _execute_netted_flip() (close opposing + open new, one consolidated
    order). Pending orders are buffered in a single bucket per bar.
  • Hedge: LONG and SHORT are independent exchange slots (Binance
    positionSide=LONG/SHORT), so a strategy can hold both directions at
    once with no conflict. _is_flip_scenario()/_execute_netted_flip()
    are never invoked in hedge mode -- on_bar() just opens/manages each
    side independently. Pending orders are buffered in a SEPARATE
    bucket per position_side ("LONG"/"SHORT"), each flushed as its own
    order tagged with that side, since a LONG-side close and a
    SHORT-side open can never be netted into one order (they're
    different exchange positions). reduce_only is never used in hedge
    mode -- Binance rejects it combined with positionSide.
  • SubmitOrderFn gained a 4th parameter, position_side: Optional[str]
    ("LONG"/"SHORT" in hedge mode, None in netting mode). The strategy
    layer (BaseSmcStrategy._make_submit_fn) uses it to build NT's
    position_id for the order; netting mode ignores it entirely.

Stage 5 additions:
  • SubmitOrderFn now returns Optional[str] — the NT client_order_id for
    entry orders, None for dry_run. Close orders ignore the return value.
  • on_order_submitted callback — called by _flush_pending() with (list[trade_id],
    client_order_id) so BaseSmcStrategy can build the rejection lookup map for
    the consolidated order.
  • balance_check_fn — optional Callable[[], float] returning free USDT.
    Provided by strategy in paper/live mode, None in dry_run. When None
    or min_free_margin_usdt is 0.0, the check is skipped entirely.
  • min_free_margin_usdt in PositionManagerConfig — balance gate threshold.
  • _state_dirty flag (Stage 4 crash-safety) — unchanged.
  • _kill_switch_notified flag — unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from risk.trade_ledger import OpenTrade, TradeLedger

# Entry orders return the NT client_order_id (str) or None in dry_run.
# Close orders are submitted with the same fn but the return is ignored.
# position_side: "LONG"/"SHORT" in hedge mode, None in netting mode.
SubmitOrderFn       = Callable[[str, Decimal, bool, Optional[str]], Optional[str]]
OnOrderSubmittedFn  = Optional[Callable[[list[int], str], None]]
BalanceCheckFn      = Optional[Callable[[], float]]

# Pending-buffer bucket key for netting mode (single blended position).
_NETTING_BUCKET = "BOTH"


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
    min_free_margin_usdt:  float   # Stage 5: 0.0 = disabled
    min_qty:               Decimal = Decimal("0.001")    # LOT_SIZE / MARKET_LOT_SIZE
    min_notional:          float = 50.0                  # MIN_NOTIONAL (USDT)
    position_mode:         str = "netting"                # "netting" | "hedge"


class PositionManager:

    def __init__(
        self,
        config:              PositionManagerConfig,
        ledger:              TradeLedger,
        submit_order_fn:     SubmitOrderFn,
        log:                 logging.Logger,
        strategy_id:         str = "",
        symbol:              str = "",
        notifier:            Optional[object] = None,
        on_order_submitted:  OnOrderSubmittedFn = None,
        balance_check_fn:    BalanceCheckFn = None,
    ) -> None:
        self.config              = config
        self.ledger              = ledger
        self._submit             = submit_order_fn
        self.log                 = log
        self._strategy_id        = strategy_id
        self._symbol             = symbol   # e.g. "BTCUSDT" -- for log/notify labeling only
        self._notifier           = notifier
        self._on_order_submitted = on_order_submitted
        self._balance_check_fn   = balance_check_fn

        self._last_close: float = 0.0
        self._last_ts:    int   = 0
        self._kill_switch_notified: bool = False
        self._state_dirty:          bool = False

        # Pending order buffer — deferred submission for consolidation.
        # Keyed by bucket: "BOTH" for netting mode (single blended
        # position, current behavior unchanged), or "LONG"/"SHORT" for
        # hedge mode (independent exchange slots — never netted against
        # each other). Each bucket separates close vs entry qty so
        # _flush_pending() can split when the consolidated net would
        # fall below MIN_NOTIONAL, same as before.
        self._pending: dict[str, dict] = {}
        self._reset_pending()

    # ── Main update ───────────────────────────────────────────────────────
    def on_bar(
        self, high: float, low: float, close: float,
        atr: float, ts: int, long_signal: bool, short_signal: bool,
    ) -> None:
        self._last_close = close
        self._last_ts    = ts
        self._reset_pending()

        self._manage_open_trades(high, low, close, atr, ts, long_signal, short_signal)

        if self.config.position_mode == "hedge":
            # LONG and SHORT are independent exchange slots under hedge
            # mode -- a strategy can hold both at once with no conflict,
            # so there is no "flip" scenario to detect or execute. Any
            # opposing trade left open by _manage_open_trades (e.g.
            # because enable_exit_signal=False) just keeps running
            # toward its own SL/TP; a new opposite-direction signal
            # opens its own independent trade rather than closing it.
            if long_signal and self._can_enter(close):
                self._enter("LONG", close, atr, ts)
            if short_signal and self._can_enter(close):
                self._enter("SHORT", close, atr, ts)
        elif self._is_flip_scenario(long_signal, short_signal):
            self._execute_netted_flip(high, low, close, atr, ts, long_signal, short_signal)
        else:
            if long_signal and self._can_enter(close):
                self._enter("LONG", close, atr, ts)
            if short_signal and self._can_enter(close):
                self._enter("SHORT", close, atr, ts)

        self._flush_pending()

    def on_price(self, price: float, ts: int) -> None:
        """Sub-bar price check called from on_mark_price().  Checks SL/TP
        for all open trades using current mark price.  No new entries."""
        self._manage_open_trades(price, price, price, 0.0, ts, False, False)
        self._flush_pending()

    # ── Flip detection ────────────────────────────────────────────────────
    def _is_flip_scenario(self, long_signal: bool, short_signal: bool) -> bool:
        if not self.ledger.open_trades:
            return False
        if short_signal:
            return any(t.side == "LONG" for t in self.ledger.open_trades)
        if long_signal:
            return any(t.side == "SHORT" for t in self.ledger.open_trades)
        return False

    # ── Netted flip ────────────────────────────────────────────────────────
    def _execute_netted_flip(
        self, high: float, low: float, close: float, atr: float, ts: int,
        long_signal: bool, short_signal: bool,
    ) -> None:
        # Direction
        if short_signal:
            net_side      = "SELL"
            target_dir    = "SHORT"
            opposing_dir  = "LONG"
        else:
            net_side      = "BUY"
            target_dir    = "LONG"
            opposing_dir  = "SHORT"

        # Gather opposing trades and sum remaining qty
        opposing_trades = [t for t in self.ledger.open_trades if t.side == opposing_dir]
        sum_opposing = Decimal("0")
        for t in opposing_trades:
            remaining = t.full_qty * (Decimal("0.5") if t.tp1_hit else Decimal("1.0"))
            sum_opposing += remaining

        opposing_count = len(opposing_trades)
        if opposing_count == 0:
            return

        # Entry gate — adjusted for soon-to-be-closed opposing trades
        after_flip_count = self.ledger.open_count - opposing_count + 1
        can_open_new = after_flip_count <= self.config.max_open_trades

        if self.ledger.daily_pnl <= -abs(self.config.daily_loss_limit_usdt):
            can_open_new = False

        if (
            self._balance_check_fn is not None
            and self.config.min_free_margin_usdt > 0.0
        ):
            free_usdt = self._balance_check_fn()
            if free_usdt < self.config.min_free_margin_usdt:
                can_open_new = False

        new_qty = self.config.trade_size if can_open_new else Decimal("0")
        net_qty = sum_opposing + new_qty

        if net_qty <= 0:
            return

        # Calculate ATR levels for potential new trade
        if can_open_new:
            if target_dir == "LONG":
                new_sl  = close - self.config.sl_atr  * atr
                new_tp1 = close + self.config.tp1_atr * atr
                new_tp2 = close + self.config.tp2_atr * atr
            else:
                new_sl  = close + self.config.sl_atr  * atr
                new_tp1 = close - self.config.tp1_atr * atr
                new_tp2 = close - self.config.tp2_atr * atr

        # Close opposing trades in ledger (no individual order submission)
        for t in opposing_trades:
            remaining = t.full_qty * (Decimal("0.5") if t.tp1_hit else Decimal("1.0"))
            if t.side == "LONG":
                leg_pnl = (close - t.entry_price) * float(remaining)
            else:
                leg_pnl = (t.entry_price - close) * float(remaining)

            t.realized_pnl += leg_pnl
            t.exit_ts     = ts

            # Determine real exit reason — same logic as _manage_open_trades
            if t.side == "LONG":
                if low <= t.sl:
                    t.exit_reason = "BE" if (t.tp1_hit and self.config.breakeven_sl) else "SL"
                elif t.tp1_hit and not self.config.trailing_tp2 and high >= t.tp2:
                    t.exit_reason = "TP2"
                elif not t.tp1_hit and high >= t.tp1:
                    t.exit_reason = "TP1"
                else:
                    t.exit_reason = "FLIP"
            else:  # SHORT
                if high >= t.sl:
                    t.exit_reason = "BE" if (t.tp1_hit and self.config.breakeven_sl) else "SL"
                elif t.tp1_hit and not self.config.trailing_tp2 and low <= t.tp2:
                    t.exit_reason = "TP2"
                elif not t.tp1_hit and low <= t.tp1:
                    t.exit_reason = "TP1"
                else:
                    t.exit_reason = "FLIP"

            self.ledger.record_close(t, final=True)
            duration_secs = (ts - t.entry_ts) / 1e9

            self.log.info(
                f"FLIP CLOSE #{t.trade_id:05d} {t.side:<5}  "
                f"exit≈{close:.1f}  reason={t.exit_reason}  "
                f"leg_pnl={leg_pnl:+.2f}  cum_pnl={t.realized_pnl:+.2f}"
            )
            self._notify("on_trade_closed", t, self._strategy_id, leg_pnl, duration_secs, symbol=self._symbol)

        # Purge closed opposing trades from open_trades
        self.ledger.open_trades = [t for t in self.ledger.open_trades if t.exit_ts is None]

        # Open new trade in ledger if allowed
        new_trade = None
        if can_open_new:
            new_trade = OpenTrade(
                trade_id    = self.ledger.next_trade_id(),
                side        = target_dir,
                entry_price = close,
                entry_ts    = ts,
                full_qty    = self.config.trade_size,
                sl = new_sl, tp1 = new_tp1, tp2 = new_tp2,
            )
            self.ledger.record_open(new_trade)

            self.log.info(
                f"FLIP OPEN #{new_trade.trade_id:05d} {target_dir:<5}  "
                f"entry≈{close:.1f}  sl={new_sl:.1f}  tp1={new_tp1:.1f}  "
                f"tp2={new_tp2:.1f}  atr={atr:.1f}  open={self.ledger.open_count}"
            )
            self._notify("on_trade_opened", new_trade, self._strategy_id, symbol=self._symbol)

        # Enqueue close and entry separately so the MIN_NOTIONAL split
        # logic in _flush_pending can distinguish them, avoiding the bug
        # where close (BUY) and entry (SELL) cancel into a sub-threshold net.
        # Netting mode only -- always the single "BOTH" bucket.
        opposing_ids = [t.trade_id for t in opposing_trades]
        if sum_opposing > 0:
            self._enqueue(net_side, sum_opposing, opposing_ids, "close", _NETTING_BUCKET)
        if new_qty > 0 and new_trade is not None:
            self._enqueue(net_side, new_qty, [new_trade.trade_id], "entry", _NETTING_BUCKET)

        # Summary log line
        new_part = f"  + open {new_qty}" if can_open_new else ""
        self.log.info(
            f"FLIP {net_side:<4} {net_qty} {self._symbol or 'units'}  |  "
            f"closed {opposing_count} opposing ({sum_opposing}){new_part}  "
            f"ts={ts}"
        )

        # Telegram notification — pass formatted qty strings for readability
        self._notify(
            "on_netted_flip",
            self._strategy_id,
            net_side,
            str(net_qty),
            str(sum_opposing),
            opposing_count,
            str(new_qty),
            symbol=self._symbol,
        )

    # ── Entry ─────────────────────────────────────────────────────────────
    def _enter(self, side: str, close: float, atr: float, ts: int) -> None:
        if atr <= 0:
            return

        if side == "LONG":
            sl, tp1, tp2 = (
                close - self.config.sl_atr  * atr,
                close + self.config.tp1_atr * atr,
                close + self.config.tp2_atr * atr,
            )
            order_side = "BUY"
        else:
            sl, tp1, tp2 = (
                close + self.config.sl_atr  * atr,
                close - self.config.tp1_atr * atr,
                close - self.config.tp2_atr * atr,
            )
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
        bucket = side if self.config.position_mode == "hedge" else _NETTING_BUCKET
        self._enqueue(order_side, self.config.trade_size, [trade.trade_id], "entry", bucket)
        self.log.info(
            f"OPEN  #{trade.trade_id:05d} {side:<5}  entry≈{close:.1f}  "
            f"sl={sl:.1f}  tp1={tp1:.1f}  tp2={tp2:.1f}  "
            f"atr={atr:.1f}  open={self.ledger.open_count}"
        )
        self._notify("on_trade_opened", trade, self._strategy_id, symbol=self._symbol)

    # ── Pending buffer ─────────────────────────────────────────────────────
    def _reset_pending(self) -> None:
        self._pending = {}

    def _new_bucket(self) -> dict:
        return {
            "buy_close":  Decimal("0"),
            "buy_entry":  Decimal("0"),
            "sell_close": Decimal("0"),
            "sell_entry": Decimal("0"),
            "ids_close":  [],
            "ids_entry":  [],
        }

    def _enqueue(
        self, side: str, qty: Decimal, trade_ids: list[int], op_type: str,
        bucket_key: str = _NETTING_BUCKET,
    ) -> None:
        if qty <= 0:
            return
        bucket = self._pending.setdefault(bucket_key, self._new_bucket())
        if op_type == "close":
            if side == "BUY":
                bucket["buy_close"] += qty
            else:
                bucket["sell_close"] += qty
            bucket["ids_close"].extend(trade_ids)
        else:
            if side == "BUY":
                bucket["buy_entry"] += qty
            else:
                bucket["sell_entry"] += qty
            bucket["ids_entry"].extend(trade_ids)

    def _flush_pending(self) -> None:
        # Each bucket ("BOTH" for netting, "LONG"/"SHORT" for hedge) is
        # flushed independently — a LONG-side close and a SHORT-side
        # open are two different exchange positions and can never be
        # netted into one order, unlike close+entry within the same
        # bucket, which still nets exactly as before.
        for bucket_key, bucket in list(self._pending.items()):
            self._flush_bucket(bucket_key, bucket)
        self._reset_pending()

    def _flush_bucket(self, bucket_key: str, bucket: dict) -> None:
        buy_close  = bucket["buy_close"]
        sell_close = bucket["sell_close"]
        buy_entry  = bucket["buy_entry"]
        sell_entry = bucket["sell_entry"]

        if buy_close == 0 and sell_close == 0 and buy_entry == 0 and sell_entry == 0:
            return

        total_buy  = buy_close + buy_entry
        total_sell = sell_close + sell_entry
        hedge = self.config.position_mode == "hedge"
        # position_side passed to the submit fn: None in netting mode
        # (NT/Binance resolve BOTH automatically), the bucket's own
        # LONG/SHORT tag in hedge mode.
        position_side = bucket_key if hedge else None

        # Compute net side and qty for the consolidated view
        if total_buy > 0 and total_sell > 0:
            net_side     = "BUY" if total_buy >= total_sell else "SELL"
            net_qty      = abs(total_buy - total_sell)
            reduce_only  = False
        elif total_buy > 0:
            net_side     = "BUY"
            net_qty      = total_buy
            reduce_only  = (buy_entry == 0) and not hedge  # only close ops → True (netting only; hedge never uses reduce_only)
        else:
            net_side     = "SELL"
            net_qty      = total_sell
            reduce_only  = (sell_entry == 0) and not hedge

        if net_qty <= 0:
            return

        # MIN_NOTIONAL guard: only split when close AND entry coexist AND net is below threshold
        has_close = buy_close > 0 or sell_close > 0
        has_entry = buy_entry > 0 or sell_entry > 0
        notional  = float(net_qty) * self._last_close

        if has_close and has_entry and not reduce_only and notional < self.config.min_notional:
            self._submit_split(bucket, position_side)
        else:
            client_order_id = self._submit(net_side, net_qty, reduce_only, position_side)
            self._state_dirty = True
            if self._on_order_submitted is not None and client_order_id is not None:
                all_ids = bucket["ids_close"] + bucket["ids_entry"]
                try:
                    self._on_order_submitted(all_ids, client_order_id)
                except Exception as e:
                    self.log.warning(f"on_order_submitted callback error: {e}")

    # ── MIN_NOTIONAL split ──────────────────────────────────────────────────
    def _must_split(self, net_qty: Decimal, reduce_only: bool) -> bool:
        """True when a consolidated order would violate MIN_NOTIONAL."""
        if reduce_only:
            return False
        return float(net_qty) * self._last_close < self.config.min_notional

    def _submit_split(self, bucket: dict, position_side: Optional[str] = None) -> None:
        """Submit close (reduce_only=True, exempt) and entry portions separately."""
        hedge = self.config.position_mode == "hedge"
        close_side, close_qty = self._net_side_qty(
            bucket["buy_close"], bucket["sell_close"],
        )
        entry_side, entry_qty = self._net_side_qty(
            bucket["buy_entry"], bucket["sell_entry"],
        )

        # Submit close first (reduce_only=True → exempt from MIN_NOTIONAL;
        # hedge mode never sends reduce_only, positionSide already scopes it)
        close_coid = None
        if close_qty > 0:
            if close_qty < self.config.min_qty:
                self.log.info(
                    f"MIN_NOTIONAL split: close qty {close_qty} "
                    f"< LOT_SIZE min {self.config.min_qty} "
                    f"— submitting as reduce_only (may bypass LOT_SIZE)"
                )
            close_coid = self._submit(close_side, close_qty, not hedge, position_side)
            self._state_dirty = True

        # Submit entry second (LOT_SIZE check applies)
        entry_coid = None
        if entry_qty > 0:
            if entry_qty < self.config.min_qty:
                self.log.warning(
                    f"MIN_NOTIONAL split: entry qty {entry_qty} "
                    f"< LOT_SIZE min {self.config.min_qty} — skipping entry"
                )
            else:
                entry_notional = float(entry_qty) * self._last_close
                if entry_notional < self.config.min_notional:
                    self.log.warning(
                        f"MIN_NOTIONAL split: entry notional ${entry_notional:.2f} "
                        f"< min ${self.config.min_notional:.2f} — skipping entry"
                    )
                else:
                    entry_coid = self._submit(entry_side, entry_qty, False, position_side)
                    self._state_dirty = True

        # Callbacks — each split portion gets its own _order_to_trade entry
        if self._on_order_submitted is not None:
            if close_coid is not None and bucket["ids_close"]:
                try:
                    self._on_order_submitted(bucket["ids_close"], close_coid)
                except Exception as e:
                    self.log.warning(f"on_order_submitted close callback error: {e}")
            if entry_coid is not None and bucket["ids_entry"]:
                try:
                    self._on_order_submitted(bucket["ids_entry"], entry_coid)
                except Exception as e:
                    self.log.warning(f"on_order_submitted entry callback error: {e}")

    @staticmethod
    def _net_side_qty(buy: Decimal, sell: Decimal) -> tuple[str, Decimal]:
        """Return (side, net_qty) for a set of BUY/SELL orders."""
        if buy > 0 and sell > 0:
            side = "BUY" if buy >= sell else "SELL"
            qty  = abs(buy - sell)
        elif buy > 0:
            side = "BUY"
            qty  = buy
        elif sell > 0:
            side = "SELL"
            qty  = sell
        else:
            side = "BUY"
            qty  = Decimal("0")
        return side, qty

    # ── Position management ───────────────────────────────────────────────
    def _manage_open_trades(
        self, high: float, low: float, close: float, atr: float, ts: int,
        long_signal: bool, short_signal: bool,
    ) -> None:
        still_open: list[OpenTrade] = []

        for t in self.ledger.open_trades:
            if t.exit_ts is not None:
                continue  # already closed (e.g., by netted flip)

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
                        t._pre_tp1_sl = t.sl       # save SL before TP1 state change
                        t.tp1_hit = True
                        if self.config.breakeven_sl:
                            t.sl = t.entry_price
                        if self.config.trailing_tp2:
                            t.best_price     = t.entry_price
                            t.trail_distance = self.config.trail_atr_mult * atr
                        still_open.append(t)
                        self._notify("on_tp1_hit", t, self._strategy_id, tp1_leg_pnl, symbol=self._symbol)
                    elif self.config.enable_exit_signal and short_signal:
                        self._close_trade(t, close, 1.0, ts, "exit-signal", final=True, reduce_only=False)
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
                        self._close_trade(t, close, 0.5, ts, "exit-signal", final=True, reduce_only=False)
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
                        t._pre_tp1_sl = t.sl       # save SL before TP1 state change
                        t.tp1_hit = True
                        if self.config.breakeven_sl:
                            t.sl = t.entry_price
                        if self.config.trailing_tp2:
                            t.best_price     = t.entry_price
                            t.trail_distance = self.config.trail_atr_mult * atr
                        still_open.append(t)
                        self._notify("on_tp1_hit", t, self._strategy_id, tp1_leg_pnl, symbol=self._symbol)
                    elif self.config.enable_exit_signal and long_signal:
                        self._close_trade(t, close, 1.0, ts, "exit-signal", final=True, reduce_only=False)
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
                        self._close_trade(t, close, 0.5, ts, "exit-signal", final=True, reduce_only=False)
                    else:
                        still_open.append(t)

        self.ledger.open_trades = still_open

    # ── Close ─────────────────────────────────────────────────────────────
    def _close_trade(
        self, trade: OpenTrade, exit_price: float, qty_frac: float,
        ts: int, reason: str, final: bool, reduce_only: bool = True,
    ) -> None:
        qty_closed = trade.full_qty * Decimal(str(qty_frac))

        if trade.side == "LONG":
            pnl        = (exit_price - trade.entry_price) * float(qty_closed)
            order_side = "SELL"
        else:
            pnl        = (trade.entry_price - exit_price) * float(qty_closed)
            order_side = "BUY"

        trade.realized_pnl += pnl
        trade._pending_close_pnl = pnl
        if self.config.position_mode == "hedge":
            # Binance rejects reduceOnly combined with positionSide in
            # hedge mode -- selling/buying against trade.side's own slot
            # is unambiguous (SELL always reduces the LONG slot, BUY
            # always reduces the SHORT slot), so reduce_only is moot.
            self._enqueue(order_side, qty_closed, [trade.trade_id], "close", trade.side)
        else:
            self._enqueue(order_side, qty_closed, [trade.trade_id], "close", _NETTING_BUCKET)
        self.log.info(
            f"{reason:<12} #{trade.trade_id:05d} {trade.side:<5}  "
            f"exit≈{exit_price:.1f}  frac={qty_frac:.2f}  "
            f"leg_pnl={pnl:+.2f}  cum_pnl={trade.realized_pnl:+.2f}"
        )

        if final:
            trade.exit_ts     = ts
            trade.exit_reason = reason
            self.ledger.record_close(trade, final=True)
            duration_secs = (ts - trade.entry_ts) / 1e9
            self._notify("on_trade_closed", trade, self._strategy_id, pnl, duration_secs, symbol=self._symbol)
        else:
            self.ledger.record_close(trade, final=False)

    # ── Entry gate ────────────────────────────────────────────────────────
    def _can_enter(self, close: float = 0.0) -> bool:
        """
        Returns True only when ALL entry conditions are met:
          1. open_count < max_open_trades
          2. daily_pnl > -daily_loss_limit  (kill switch)
          3. free_margin >= min_free_margin  (paper/live only)
          4. entry notional >= MIN_NOTIONAL exchange filter
        """
        # Gate 1: open trade cap
        if self.ledger.open_count >= self.config.max_open_trades:
            reason = (
                f"max open trades "
                f"({self.ledger.open_count}/{self.config.max_open_trades})"
            )
            self.log.debug(f"Entry blocked — {reason}")
            self._notify("on_entry_blocked", reason, self._strategy_id)
            return False

        # Gate 2: daily loss limit (kill switch)
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

        # Gate 3: margin balance (paper/live only — dry_run has no balance_check_fn)
        if (
            self._balance_check_fn is not None
            and self.config.min_free_margin_usdt > 0.0
        ):
            free_usdt = self._balance_check_fn()
            if free_usdt < self.config.min_free_margin_usdt:
                reason = (
                    f"insufficient margin "
                    f"(free={free_usdt:.2f} USDT, "
                    f"min={self.config.min_free_margin_usdt:.2f} USDT)"
                )
                self.log.warning(f"Entry blocked — {reason}")
                self._notify("on_entry_blocked", reason, self._strategy_id)
                return False

        # Gate 4: trade size vs LOT_SIZE
        if self.config.trade_size < self.config.min_qty:
            reason = (
                f"trade_size {self.config.trade_size} "
                f"< LOT_SIZE min {self.config.min_qty}"
            )
            self.log.warning(f"Entry blocked — {reason}")
            self._notify("on_entry_blocked", reason, self._strategy_id)
            return False

        # Gate 5: entry notional vs MIN_NOTIONAL
        entry_notional = float(self.config.trade_size) * close
        if entry_notional < self.config.min_notional:
            reason = (
                f"entry notional ${entry_notional:.2f} "
                f"< min ${self.config.min_notional:.2f} "
                f"(qty={self.config.trade_size} × price={close:.2f})"
            )
            self.log.warning(f"Entry blocked — {reason}")
            self._notify("on_entry_blocked", reason, self._strategy_id)
            return False

        return True

    # ── Dirty flag ────────────────────────────────────────────────────────
    def flush_state(self) -> bool:
        dirty = self._state_dirty
        self._state_dirty = False
        return dirty

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
                f"pnl_so_far={t.realized_pnl:+.2f}  tp1_hit={t.tp1_hit}"
            )

    # ── Notifier helper ───────────────────────────────────────────────────
    def _notify(self, method: str, *args, **kwargs) -> None:
        if self._notifier is None:
            return
        fn = getattr(self._notifier, method, None)
        if fn is None:
            return
        try:
            fn(*args, **kwargs)
        except Exception as e:
            self.log.warning(f"Notifier {method} error: {e}")