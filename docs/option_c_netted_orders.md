# Option C — Netted Flip Orders

## The Problem: `-2022` ReduceOnly Rejections

### Current architecture
In `position_manager._manage_open_trades()`, each trade is closed independently via `_close_trade()` which defaults to `reduce_only=True` for SL, TP1, TP2, TP2-trail, and BE closes. Only `exit-signal` closes use `reduce_only=False`

### How the race manifests
A flip bar (e.g. SHORT signal while NET position is LONG) triggers multiple simultaneous orders on the same bar:

```
on_bar():
  _manage_open_trades()     → for each LONG trade close (TP1/exit-signal/SL):
                                 _submit(SELL, 0.0005, reduce_only=True)   ← order 1
                                 _submit(SELL, 0.0005, reduce_only=True)   ← order 2
                               ...
  _enter("SHORT")            → _submit(SELL, 0.001, reduce_only=False)     ← order 3
```

All three orders are non-blocking HTTP requests to Binance's REST API. They arrive within microseconds of each other. There is no execution ordering guarantee — Binance's matching engine may fill order 3 before orders 1/2.

If order 3 fills first:
1. Net position goes from LONG 0.001 to net 0 or net SHORT
2. Orders 1/2 arrive with `reduce_only=True` — but there's no LONG position left
3. Binance rejects with `-2022`: "ReduceOnly Order is rejected"
4. The rejected closing orders never execute → **position remains open without TP/SL protection**

### Log evidence
```
2026-07-02T05:45:01  -2022 ReduceOnly Order is rejected  (O-20260702-054500-001-001-210)
2026-07-02T06:50:01  -2022 ReduceOnly Order is rejected  (O-20260702-065000-001-001-219)
2026-07-02T06:55:00  -2022 ReduceOnly Order is rejected  (O-20260702-065500-001-001-221)
2026-07-02T06:55:01  -2022 ReduceOnly Order is rejected  (O-20260702-065500-001-001-222)
2026-07-02T07:40:00  -2022 ReduceOnly Order is rejected  (O-20260702-074000-001-001-226)
2026-07-02T08:35:01  -2022 ReduceOnly Order is rejected  (O-20260702-083500-001-001-229)
2026-07-02T09:30:00  -2022 ReduceOnly Order is rejected  (O-20260702-093000-001-001-235)
2026-07-02T09:30:01  -2022 ReduceOnly Order is rejected  (O-20260702-093000-001-001-234)
2026-07-02T09:35:00  -2022 ReduceOnly Order is rejected  (O-20260702-093500-001-001-236)
```
9 total today, all FVG-001, clustered on flip bars (LONG → SHORT).

---

## The Options

### Option A — `reduce_only=False` on flip closes
**Change**: In `_manage_open_trades()`, detect when an opposing signal is present and pass `reduce_only=False` to all close orders from trades in the opposing direction.

```python
force_no_reduce = (t.side == "LONG" and short_signal) or (t.side == "SHORT" and long_signal)
# … pass reduce_only=not force_no_reduce to every _close_trade() call
```

| Pro | Con |
|-----|-----|
| ~5 lines changed | Race still exists — entry can fill before close |
| Preserves per-trade PnL | If entry is rejected, close with `reduce_only=False` could overshoot (open a position) |
| Simple, easy to verify | Doesn't align our order model with Binance NETTING |
| Handle existing min_notional check in the trade_ledger. | Might still cause -4164 MIN_NOTIONAL with small leftover sizes |

---

### Option B — Deferred entry
**Change**: Don't submit the new entry immediately. Set a `_pending_entry` flag. In `on_order_filled()`, detect when the closing flip order fills, then submit the deferred entry.

```python
def on_bar(…):
    if flip:
        submit_flip_closes_first()
        self._pending_entry = (side, close, atr, ts)  # defer
    else:
        normal entry logic

def on_order_filled(event):
    if self._pending_entry:
        self._enter(self._pending_entry)
        self._pending_entry = None
```

| Pro | Con |
|-----|-----|
| Guarantees close fills before entry | ~30 lines of async state machine |
| No race condition | Adds state flag + edge cases (entry rejected, close rejected, restart mid-state) |
| Preserves per-trade ledger | Harder to test and reason about |
| | Entry is delayed 1+ seconds (RTT to exchange) |
| | Two consecutive flip bars could interleave |

---

### Option C — Netted single order (recommended)
**Change**: On a flip bar, calculate the **net** quantity needed to transition from the current blended position to the target position. Submit **one** market order instead of N closes + 1 entry.

#### How it works

| Before bar | Signal | Closes needed | Entry | Net order |
|-----------|--------|--------------|-------|----------|
| LONG 0.001 | SHORT | SELL 0.001 | SELL 0.001 | **SELL 0.002** |
| LONG 0.0005 (TP1 hit) | SHORT | SELL 0.0005 | SELL 0.001 | **SELL 0.0015** |
| LONG 0.001 + LONG 0.001 | SHORT | SELL 0.002 | SELL 0.001 | **SELL 0.003** |
| SHORT 0.001 | LONG | BUY 0.001 | BUY 0.001 | **BUY 0.002** |

Binance sees one market order, fills it atomically. No race.

#### Ledger handling
The ledger **still records individual trade events** for notification/reference:
- All opposing-side trades marked as closed (exit-signal) with calculated PnL
- New trade opened with entry price = signal price
- Net order qty = sum of opposing-side remaining qty + new trade qty

This keeps Telegram notifications ("CLOSED #00034", "OPENED #00036") and the Stage 6 reconciler happy. NT portfolio remains the financial source of truth.

#### Modified `on_bar()` flow
```python
def on_bar(…):
    if self._is_flip_scenario(long_signal, short_signal):
        self._execute_netted_flip(close, atr, ts, long_signal, short_signal)
    else:
        self._manage_open_trades(…)   # normal single-trade logic
        normal entry logic
```

#### Scenarios NOT affected (remain unchanged)
- SL hit (no signal flip) → normal single close
- TP1 hit (no signal flip) → normal single close
- TP2 hit → normal single close
- Adding to position (same direction) → normal entry
- No signal → no change

| Pro | Con |
|-----|-----|
| **Zero race** — single atomic order | ~50 lines new code |
| Aligns our submission model with Binance NETTING | Per-trade PnL on flip bars is approximated (exit calculated from avg or signal price) |
| No `-2022` or `-4164` on flips | You must trust NT portfolio for true PnL on flip bars |
| Doesn't change non-flip behavior | |
| Preserves per-trade ledger for Telegram + reconciler | |
| No async state machine | |
| Prevents `-4164` MIN_NOTIONAL (single order ≥ 0.001 BTC vs tiny leftover fractions) | |

---

## Recommendation: Option C

| Criterion | Option A | Option B | Option C |
|-----------|----------|----------|----------|
| Fixes race | Partial — still possible | Yes | **Yes** |
| Lines changed | ~5 | ~30 | **~50** |
| Async complexity | None | Medium | **None** |
| Per-trade PnL preserved | Yes | Yes | **Approximated on flips** |
| Aligns with NETTING | No | No | **Yes** |
| Risk if entry rejected | Overshoot close (new position) | Missing close stuck in limbo | **Single order either fills or doesn't — clean** |
| `-4164` MIN_NOTIONAL risk | Still possible | Same | **Eliminated** (combined qty ≥ 0.001) |

Option C is the architecturally correct choice for NETTING mode. The current per-trade submission model fights the exchange model. Netted orders match how Binance actually sees the position.

The one trade-off — approximate per-trade PnL on flip bars — is acceptable because:
- True financial PnL comes from NT portfolio (the source of truth)
- The ledger is for Telegram notifications and Stage 6 reconciliation reference
- The approximation is calculated from signal price, which is already the convention used for entry_price

---

## Implementation plan (when approved)

### Files to modify
1. **`risk/position_manager.py`** — Add `_is_flip_scenario()` and `_execute_netted_flip()`, modify `on_bar()`
2. **`strategies/base_smc_strategy.py`** — No changes (interface unchanged)

### `position_manager.py` changes
1. New helper: `_is_flip_scenario()` — detects opposing signal + open trades in opposite direction
2. New method: `_execute_netted_flip()` — calculates net qty, submits one order, mutates ledger
3. Modified `on_bar()` — routes to `_execute_netted_flip()` or existing logic

### `_execute_netted_flip()` pseudocode
```
sum_opposing_qty = sum(t.full_qty * (0.5 if t.tp1_hit else 1.0) for opposing trades)
net_qty = sum_opposing_qty + new_trade_qty
net_side = SELL if short_signal else BUY
_submit(net_side, net_qty, reduce_only=False)

# Update ledger
for each opposing trade:
    mark closed with exit-signal reason, calculate PnL from signal price
record new trade
_state_dirty = True
notify all trades
```
