# Position Mode: NETTING vs HEDGE

> **Superseded.** This document was written when the system ran in NETTING
> mode and recommended staying on it. Hedge mode has since been enabled
> (see `docs/hedge_mode_implementation.md`). The NETTING-vs-HEDGE
> comparison below is still accurate as reference, but the "Recommendation:
> Stay on NETTING" conclusion no longer applies to the live system.

## Current Setup: NETTING (One-Way)

Binance USDT-M Futures **default** mode. One position per symbol, expressed as a signed
quantity:

| Trade | Net Position |
|---|---|
| BUY 0.001 | `+0.001` |
| BUY 0.001 | `+0.002` |
| SELL 0.003 | `-0.001` |
| BUY 0.001 | `0` |

`reduce_only` on an order means: *"reject if this order would increase the absolute
position size."*

### Our System on NETTING

- **NautilusTrader config:** `OmsType.NETTING` (default, no explicit config needed)
- **Strategy:** Our custom `TradeLedger` tracks each trade independently (trade_id,
  entry, exit, PnL), ignoring NT's blended position. Every MARKET order still hits the
  real exchange net position.
- **Exit-signal problem:** When the signal flips (LONG → SHORT), exit-signal closes
  **and** the new entry submit simultaneously. The new entry's fill can shift net
  position to 0 before all exit-signal `reduce_only` orders land → `-2022`.
- **Liquidation:** One liquidation price for the entire blended position. A single
  price decides whether the position is force-closed.

---

## What HEDGE Mode Changes

### Binance Account Setting

Contact Binance support or use the API to enable dual position mode
(`/fapi/v1/positionSide/dual`). Once active, every order must carry a `positionSide`
field: `"LONG"` or `"SHORT"`.

### NautilusTrader Config

In `node_builder.py`:
```python
exec_client = BinanceExecClientConfig(
    ...,
    oms_type=OmsType.HEDGING,
    ...
)
```

NT then exposes two position IDs per instrument:

| Position ID | Side |
|---|---|
| `BTCUSDT-PERP.BINANCE-LONG` | LONG only |
| `BTCUSDT-PERP.BINANCE-SHORT` | SHORT only |

Each tracks its own quantity, avg entry price, PnL, and liquidation price independently.

### Strategy Changes

| Aspect | NETTING | HEDGE |
|---|---|---|
| Entry order | `self.order_factory.market(...)` | Same call + `position_id=` matching side |
| Close order | `self.order_factory.market(..., reduce_only=True)` | Same + `position_id=` matching the trade's side |
| Exit-signal close+open | Concurrent same-side orders collide → `-2022` | LONG and SHORT slots are independent → no collision |
| `reduce_only` on TP/SL | Works (reduces the single blended position) | Works (reduces the specific position slot) |
| `_order_to_trade` mapping | Unchanged | Unchanged |
| `TradeLedger` | Unchanged | Unchanged |

### Exit-signal With HEDGE

```
LONG slot:  +0.002    SHORT slot:  0

Bar: SHORT signal fires
  → Exit-signal closes LONG trades (SELL reduce_only on LONG slot)
  → New SHORT entry (SELL on SHORT slot)

LONG slot accepts both SELL reduce_only fills  →  +0.002 → 0 ✓
SHORT slot accepts the SELL new entry           →  0 → -0.001 ✓
```

No collision — different position slots, independent quantities.

### Liquidation With HEDGE

Each side has its own liquidation price. A losing LONG can be liquidated while a
winning SHORT remains open. In NETTING, a single blended liquidation price means the
net loss across both sides determines liquidation — potentially safer or riskier
depending on correlation.

---

## Key Tradeoffs

| Concern | NETTING | HEDGE |
|---|---|---|
| Concurrent close+open orders | `-2022` on exit-signal | Works cleanly |
| Liquidation risk | Single price, blended position | Independent per side |
| `reduce_only` for TP/SL | Works | Works |
| Implementation effort | Fix exit-signal (~3 lines in `position_manager.py`) | Config + strategy + testing |
| Strategy results (PnL) | Identical | Identical |
| Binance change needed | No | Yes (API or support ticket) |
| Backward compatible | No change | Old state persistence incompatible |

---

## Recommendation

Stay on NETTING. Fix exit-signal close orders to use `reduce_only=False` when running
concurrently with a new entry. The concurrency bug is the only practical problem,
it is a 3-line fix, and trading results are identical between the two modes.

HEDGE mode would add complexity with zero benefit at the current scale (single
instrument, ~3 concurrent trades).

_____________________________________________________________________________




## Claude Hint for Netting and Hedge ## 

NT's Built-in Position System
NT does have its own position tracking. When you submit orders, NT's ExecEngine processes fills and updates an internal Position object. You can subscribe to these events in a strategy:
pythondef on_position_opened(self, event: PositionOpened) -> None: ...
def on_position_changed(self, event: PositionChanged) -> None: ...
def on_position_closed(self, event: PositionClosed) -> None: ...

Why We Can't Use It — The NETTING Problem
We're using OmsType.NETTING which matches how Binance Futures actually works (one-way mode). Under NETTING, NT maintains one position per instrument — not one per signal.
So if MS opens a LONG and then FVG also opens a LONG:
NT sees:          ONE position, 0.002 BTC long, avg entry ~64,500
Our ledger sees:  Trade #1 (MS)  0.001 BTC, entry 64,200
                  Trade #2 (FVG) 0.001 BTC, entry 64,800
When MS hits its TP1 and closes 50%:
NT sees:  position reduced to 0.001 BTC — but which trade? NT doesn't know.
Our ledger: Trade #1 leg 1 closed at TP1, PnL +$0.72 attributed to MS
            Trade #2 still fully open, attributed to FVG
NT's position system cannot answer "which signal generated which PnL" under NETTING. That's the fundamental limitation.

What NT's HEDGING Mode Would Give Us
If we switched to OmsType.HEDGING (which Binance supports via their hedge mode account setting), NT would track each entry as an independent position. We'd then get:
pythondef on_position_closed(self, event: PositionClosed) -> None:
    pnl      = event.realized_pnl   # per-trade PnL ✓
    duration = event.duration       # time in trade ✓
    side     = event.entry_side     # LONG or SHORT ✓
And we could use NT's built-in events for Telegram notifications directly, without our own ledger at all.

HEDGING vs NETTING — Should We Switch?
NETTING (current)HEDGINGMatches Binance standard account✅❌ (needs hedge mode enabled)Per-trade PnL from NT❌✅Multi-strategy isolation❌✅Our custom ledger needed✅❌ComplexityLowerHigherBacktest parity✅ (backtest uses NETTING)❌ (different from backtest)
The key constraint: switching to HEDGING requires enabling hedge mode on your Binance Futures account. It also means our backtest scripts (which use NETTING) would no longer model the live system exactly — the position blending behavior would differ.

My Recommendation: Keep Our Ledger
For this project, our custom TradeLedger + PositionManager is actually the right choice because:

Backtest parity — the live system uses the exact same logic as the backtest scripts. Every risk finding transfers directly.
Multi-strategy independence — each strategy has its own ledger. MS's daily loss limit and PnL tracking is completely isolated from FVG's, even though both are NETTING positions on the same instrument under NT.
Signal-level attribution — we know exactly which signal (#00001, #00002) produced which PnL, which exit reason (SL/TP1/TP2), for which strategy. NT's NETTING position can never give us this.
No Binance account change needed — switching to hedge mode on a live account is a destructive operation that closes all open positions first.


What This Means for Stage 4
For Telegram, we tap into our own ledger events via the notify_fn callback pattern already planned. We don't need NT's position events at all. Our PositionManager already knows:

Exact entry price, SL, TP1, TP2 per trade
Which leg closed (TP1 partial vs final)
Exit reason (SL / BE / TP2 / TP2-trail / exit-signal)
Gross PnL per leg and cumulative

NT's position events would give us less information than we already have.
