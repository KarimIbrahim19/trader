# 🔍 Reconcile CASE A — Full Root Cause Analysis

**Date:** 2026-07-04 (UTC)  
**System:** CLTRADER-001 | NautilusTrader on Binance USDT Futures  
**Strategies Affected:** MS-001 (Market Structure), FVG-001 (Fair Value Gap)  
**Total CASE A warnings:** 266 (in btc_trader.log.1) + 21 (in btc_trader.log.2)

---

## What Is RECONCILE CASE A?

Your reconciler compares:
- **exchange** = actual net BTC position reported by Binance (`/fapi/v3/positionRisk`)
- **expected** = net position your bot believes it holds (sum of MS + FVG open trades)

**CASE A fires when: `exchange < expected`** — i.e., the exchange has *less* (or opposite) position than the bot thinks it does. The bot suspects this was caused by an **external close** (liquidation, manual close, or ADL), and since it cannot safely auto-correct, it flags it for manual review.

---

## Episode 1 — 2026-07-04 03:57 → 04:17 UTC (80 warnings)

### Root Cause: Binance Silently Closed a LONG Before the Bot Knew It

#### Timeline

| UTC Time | Event |
|---|---|
| **03:34:00** | FVG opens trade **#00020 LONG 0.0010 BTC** @ 62,675 USDT (filled) |
| **03:34 → 03:41** | Position confirmed LONG on Binance every minute via PositionStatusReport |
| **03:42:00** | FVG opens new SHORT **#00021** — a SELL order (O-155) closes the LONG position. NautilusTrader emits **PositionClosed** event (avg close = 62,668.1, pnl = -0.057 USDT) |
| **03:42:53** | Binance confirms: **0 open positions** |
| **03:43 → 03:55** | Exchange reports **0 positions** every minute. Bot however still has **trade #00020 LONG alive** in its internal ledger |
| **03:56:00** | FVG strategy triggers SL exit for #00020 → submits SELL 0.0005 (reduce-only=False) → **REJECTED by Binance: error -4164** (notional < $50). Trade #00020 reverted to open. TP exit for #00021 also rejected. |
| **03:57:00** | 🚨 **FIRST RECONCILE CASE A**: `exchange=0.0000  expected=+0.0010  diff=-0.0010` |
| **03:57 → 04:11** | Bot retries SL close for #00020 every minute → all fail with **Binance error -2022** ("ReduceOnly Order is rejected") — because there's no actual position on the exchange to reduce |
| **04:12:00** | Bot opens NEW trade **#00022 LONG 0.0010 BTC** @ 62,673.8 USDT — exchange now shows `+0.0010` but bot expects `+0.0020` (thinks #00020 + #00022 are both open). CASE A shifts: `exchange=+0.0010  expected=+0.0020` |
| **04:17:00** | SL triggered for both #00020 and #00022. SELL 0.0010 fills → **PositionClosed** confirmed. Reconcile stops. |

#### The Core Problem

**Trade #00020's position on Binance was already closed at 03:42 (by the SELL order for trade #00021).** However, because the SL/TP logic for #00020 and #00021 are tracked independently in your ledger, **#00020 was never marked as closed in the strategy's `open_trades` list.**

When the reconciler checked at 03:57, it saw the bot ledger expected +0.0010 but exchange had 0. Since no liquidation event was received, it correctly flagged CASE A. The bot kept trying to close a position that no longer existed on the exchange (causing `-2022` errors), and interpreted each failure as "position still open."

> **This is NOT a liquidation.** The position was closed by your own strategy (trade #00021's entry SELL). The reconciler's "possible external close" guess was incorrect — it was actually a legitimate close by the same strategy that opened #00020 in a round-trip.

---

## Episode 2 — 2026-07-04 05:22 → 05:33+ UTC (continuing warnings)

### Root Cause: Phantom Short Position From a Rejected Entry + Timing Gap

#### Timeline

| UTC Time | Event |
|---|---|
| **05:10:00** | SHORT position #00025 closed (PositionClosed, exchange = 0) |
| **05:12:00** | FVG opens trade **#00026 SHORT 0.0010 BTC** @ 62,557.9 (filled, PositionOpened). Exchange confirms SHORT -0.001 from 05:12 to 05:21 |
| **05:21:00** | FVG hits TP1 for #00026 → tries to open new trade #00027 SHORT + partial TP close. Submits SELL 0.0005 (combined order) → **REJECTED: -4164** (notional < $50). Both #00026 and #00027 **removed from ledger** (`ENTRY REJECTED`) |
| **05:21:59** | Binance still shows SHORT -0.0010 (the position from #00026 is still OPEN on the exchange — only removed from bot's ledger!) |
| **05:22:00** | 🚨 **SECOND EPISODE CASE A**: `exchange=-0.0010  expected=+0.0000  diff=-0.0010`. Bot ledger is flat but exchange still has the SHORT |
| **05:30:00** | Bot opens #00028 SHORT, exchange changes to -0.0020. CASE A shifts: `exchange=-0.0020  expected=-0.0010` |

#### The Core Problem

When a SELL order combining a TP close + new SHORT entry is **rejected with -4164**, your error handler removes the trade from the ledger (`ENTRY REJECTED → removed from ledger`). However, **the existing SHORT position from trade #00026 was already open on the exchange and is not closed by the rejected order**. The ledger removal orphans the live exchange position — the bot thinks it's flat while holding -0.0010 SHORT on Binance.

> **This is also NOT a real external close.** The `-4164` rejection caused the bot to drop #00026 from its books while it was still live on the exchange.

---

## Summary Table

| Episode | Time (UTC) | Exchange | Expected | Diff | Root Cause |
|---|---|---|---|---|---|
| 1 | 03:57–04:17 | +0.0000 → +0.0010 | +0.0010 → +0.0020 | -0.0010 | Trade #00020 position closed by strategy's own SELL (for trade #00021), but #00020 never removed from open_trades |
| 2 | 05:22–05:33+ | -0.0010 → -0.0020 | 0.0000 → -0.0010 | -0.0010 | Trade #00026 SHORT removed from ledger on -4164 rejection while position still open on Binance |

---

## Recurring Pattern: Binance Error -4164 (Notional < $50)

This error appeared **multiple times** and is a consistent trigger for state desync. At `quantity=0.0005 BTC` and BTC ≈ 62,500 USDT, the notional is ~**$31.25** — below Binance Futures' $50 minimum for non-reduce-only orders.

**This is a systematic risk:** any combined entry+TP order at half-size (0.0005 BTC) will fail with -4164 whenever BTC is below ~$100,000.

---

## ⚠️ Actions Required

> [!CAUTION]
> There may currently be a **live orphaned position on Binance Futures** (SHORT -0.0010 BTC from Episode 2). Verify and close manually if still open.

> [!IMPORTANT]
> **Immediate fixes needed:**
> 1. **Check your Binance account now** — is there an open SHORT position that the bot doesn't know about? If yes, close it manually.
> 2. **Fix ENTRY REJECTED handler** — when an order that was supposed to do TP + new entry is rejected, do NOT remove the existing open trade from the ledger. Only remove the *new entry portion*; the existing position is still live on the exchange.
> 3. **Add minimum notional guard** — before submitting any order, check `quantity × price ≥ 50 USDT` (or current Binance minimum). Split the TP and new entry into two separate orders if needed, or only submit the TP as reduce-only when notional is too small.
> 4. **Fix trade closure tracking** — when a SELL order fills and closes a position, ensure ALL trades that were part of that position are marked as closed in your `open_trades` ledger.

> [!TIP]
> The reconciler's "Possible external close (liquidation/manual/ADL)" diagnosis was **incorrect** in both episodes — these were caused by internal state management bugs, not external events. Consider improving the reconciler's diagnostic messaging to distinguish "position missing (bot sold it but didn't track it)" from "position was externally closed."
