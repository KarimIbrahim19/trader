# Stage 6 Review — Ledger Sync Plan

**Subject: Stage 6 — ledger architecture needs rethinking before we code**

Thanks for the plan. It correctly identifies the gap, but several proposed phases need adjustment given how the system actually runs. Here's a phase-by-phase review and an alternative.

---

## 6A — Enable NT's native reconciliation

| Pros | Cons |
|---|---|
| One-line change in `node_builder.py` | The check only keeps NT's own position state correct — it has zero visibility into our custom ledger |
| Free safety net, zero maintenance | |

**Verdict: Worth doing.** Cheap and makes NT's portfolio self-healing. But it doesn't solve the core problem: if NT fixes its position and our ledger doesn't reflect it, we still have a permanent mismatch.

## 6B — Cross-strategy reconciler (periodic timer)

| Pros | Cons |
|---|---|
| Detects the exact failure scenario we care about (drift between expected and actual exposure) | **False positives on every entry/close.** Our ledger mutates optimistically before Binance confirms. A reconciler on a timer will fire alerts on 100% of bars that generate trades — the mismatch window is 100–500ms per MARKET order. |
| | Doesn't know which side to trust. If expected ≠ exchange, which one wins? The plan says "does not auto-correct," so it just generates noise. |
| | Requires a pending-grace-period workaround (skip trades < N seconds old), adding complexity that still leaks edge cases. |

**Verdict: Wrong design with a timer.** A periodic reconciler can't distinguish between "in-flight order" and "permanent drift." A better approach is event-driven: reconcile only when there's reason to believe something changed, not on a fixed schedule.

## 6C — listenKey watchdog

| Pros | Cons |
|---|---|
| Would catch user-data WebSocket disconnects before they cause missed fill events | NT manages the user data WS internally. Not sure we can query connection health from Python without digging into Rust internals. |

**Verdict: Risky to scope.** Investigate first, build only if NT exposes the hook. Drop from Stage 6 — revisit later.

## 6D — Startup reconciliation upgrade

| Pros | Cons |
|---|---|
| Catches drift that accumulated while the process was down | Still can't auto-correct per-trade attribution — Binance tracks one net position, not individual trades |
| Straightforward to implement | |

**Verdict: Valuable but limited.** Net qty check works as a sanity check. But it can't verify per-trade details like "was TP1 really hit for trade #3?" — that's fundamentally unrecoverable in NETTING mode.

---

## What we think we should do instead

The real issue isn't that we need more reconciliation — it's that **we have two separate sources of truth and they don't agree on fundamentals.** 6B tries to manage this with an alert layer, but alerts that trigger on every trade become noise.

**Change the authority model instead.**

**Source of truth = NT portfolio** (it syncs with Binance automatically via `ACCOUNT_UPDATE` events and `ExecMassStatus`).

**Our ledger = best-effort tracking for:**
- SL/TP level management (ATR-based — NT doesn't know these)
- Verbose Telegram notifications (per-trade open/close messages)
- Market order tracking (we submit them, NT tracks the fills)

**The key change:** in `on_bar()`, before processing SL/TP logic, we run a **lightweight pre-bar reconciliation**:
1. Get NT's position qty from `self.portfolio.position(instrument_id)`
2. Compare against our ledger's expected aggregate exposure
3. If they match → proceed normally (use ledger for SL/TP decisions)
4. If they diverge → trust NT, force-recalculate our state, log + notify

This means:
- No noisy timer-based alerts
- One clean comparison point per bar (after all fills from the previous bar are settled, before new SL/TP checks)
- Auto-healing instead of alert-only
- The mismatch window is gone — by the next bar, any in-flight order has settled

The only open question is whether to simplify the ledger to an **aggregate model** (single `StrategyPosition` per strategy with weighted avg entry and TP1-hit flag) or keep per-trade tracking as informational. Per-trade is still useful for backtest parity and granular notifications, but must never block trading decisions.

## Proposed Stage 6 plan

| Step | What | Priority |
|---|---|---|
| 6A | Enable NT native reconciliation (`node_builder.py`) | ✅ Keep |
| Replace 6B | Pre-bar reconciliation (event-driven, auto-healing, no timer) | Build this instead |
| 6C | listenKey watchdog | Drop — revisit when NT exposes health hooks |
| 6D | Startup reconciliation, same pre-bar approach | ✅ Keep, adapt |

The core difference: **self-healing instead of alert-only.** The reconciler doesn't just flag mismatches — it corrects our ledger to match the exchange, logs what it did, and only escalates when correction is impossible (e.g., our ledger says 0.003 but exchange says 0).
