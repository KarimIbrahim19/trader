# Close-order Rejection Handling

## Current Approach (Option A — Optimistic + revert)

Implemented 2026-07-03.

**Design:**
- `_close_trade` updates the ledger immediately on submission (sets `exit_ts`,
  calls `record_close`, increments `realized_pnl`).
- The `client_order_id` is registered in `_order_to_trade` via the existing
  `_on_order_submitted` callback (same pattern as entry orders).
- If the order is subsequently rejected (async), `on_order_rejected` finds the
  trade in `closed_trades` and **reverts** the ledger:
  1. Remove from `closed_trades`
  2. Clear `exit_ts` and `exit_reason`
  3. Subtract `_pending_close_pnl` from `realized_pnl`
  4. Re-add to `open_trades`
- On the next bar, `_manage_open_trades` sees the trade is still open and
  retries the close. If the SL/TP condition still holds, the close resubmits.
  If the exchange net position has changed (e.g., opposing strategy's position
  was resolved), the `reduce_only=True` close may now succeed.

**Trade-data field added:**
- `OpenTrade._pending_close_pnl: float` — stores the PnL delta from the last
  close submission. Zeroed on revert or after confirming the close succeeded.

**Files changed:**
- `risk/trade_ledger.py` — added `_pending_close_pnl` field
- `risk/position_manager.py` — `_close_trade` registers close orders
- `strategies/base_smc_strategy.py` — `on_order_rejected` reverts close rejections
- `actors/telegram_actor.py` — added `on_close_reverted` notifier

**Notification paths:**
| Event | Log | Telegram |
|-------|-----|----------|
| Entry rejected | `ENTRY REJECTED #XXXXX` | `on_order_rejected` |
| Close rejected + reverted | `CLOSE ORDER REVERTED #XXXXX` | `on_close_reverted` |
| Untracked close rejected (legacy fallback) | `CLOSE ORDER REJECTED!` | `on_close_order_rejected` |

The `on_close_order_rejected` path is theoretically unreachable after the fix
(every close order is registered in `_order_to_trade`), but kept as a safety
net in case of edge cases or future code paths that submit close orders
without registration.

**Limitations:**
- Trade is re-added to the end of `open_trades`, so iteration order changes.
- `realized_pnl` may jitter if a close is repeatedly rejected on every bar
  (each rejection reverts, each bar retries). In practice the market moves
  past SL/TP within 1-2 bars, so the retry succeeds quickly.

## Future Approach (Option B — Defer to fill)

Consider if Option A's jitter becomes a problem.

**Design:**
- `_close_trade` submits the order but does NOT update the ledger (no `exit_ts`,
  no `record_close`, no `realized_pnl` change).
- A new `_pending_closes: dict[str, PendingClose]` tracks submitted close orders
  by `client_order_id`.
- `_manage_open_trades` skips trades that have pending closes.
- `on_order_filled` for close fills: apply the ledger changes (set `exit_ts`,
  call `record_close`, update `realized_pnl`).
- `on_order_rejected`: just remove from `_pending_closes` — the trade stays in
  `open_trades` naturally.

**Advantages over Option A:**
- No optimistic mutations to revert — ledger always reflects only confirmed state.
- No `_pending_close_pnl` field needed.

**Disadvantages:**
- Requires new `PendingClose` dataclass and tracking dict.
- Must handle the race where a new bar arrives before the fill/rejection event
  (the trade stays in `open_trades` with an in-flight close).
- TP1 partial closes set `tp1_hit = True` optimistically — reverting on
  rejection requires saving the pre-TP1 state.
