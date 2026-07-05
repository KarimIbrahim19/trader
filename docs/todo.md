# TODO

## 2. Close-order retry with exponential backoff

**Priority:** Low | **Status:** Pending

The revert + next-bar retry is working, but there's no retry limit or
escalation. If SL keeps getting rejected bar after bar (e.g., persistent
net-flat condition with opposing strategy), it retries indefinitely.

### Desired behavior
- Max 3 retries per trade per close trigger
- Exponential backoff: 1s → 2s → 4s between retries (or N bars)
- After all retries exhausted: escalate to Telegram with manual
  intervention instructions
- Prevent new entries on the same side while close is pending
- Reset retry counter if the market moves past the SL/TP level
  (i.e., the condition still holds)

### Affected files
- `risk/position_manager.py` — `_close_trade()`, `_manage_open_trades()`
- `strategies/base_smc_strategy.py` — `on_order_rejected()`
- `actors/telegram_actor.py` — optional new notifier for escalation

---

## 3. Case B auto-reset

**Priority:** Low | **Status:** Pending

Currently requires manual restart to clear the reconciler halt flag.

### Desired behavior
- Auto-reset halt after N bars of consistent reconciliation where
  exchange position matches expected position (within tolerance)
- Log the auto-reset event
- Send Telegram notification when halt is cleared

### Affected files
- `risk/reconciler.py` — `check()`, new auto-reset logic

---

## 4. Case A auto-heal

**Priority:** Low | **Status:** Pending

Currently warns only (exchange < expected). No auto-correction.

### Desired behavior
- Case A means the exchange has LESS position than the ledger expects
- Possible causes: liquidation, manual close, ADL
- Auto-heal by syncing the ledger to match exchange position?
  Risky — better to just log and let the user decide
- Consider: after N consecutive Case A checks, sync ledger → exchange
  with a Telegram notification explaining the correction

### Affected files
- `risk/reconciler.py` — `_handle_mismatch()`

---

## 5. Health-check / monitoring actor

**Priority:** Low | **Status:** Pending

The `monitoring/` directory exists but is empty. No periodic health
check beyond the startup banner.

### Desired behavior
- A NautilusTrader Actor that runs periodic health checks:
  - Bar arrival heartbeat (did we receive a bar in the last N seconds?)
  - WebSocket connection alive
  - Position mismatch between ledger and exchange
  - Account balance trend
- Could be a simple timer-based actor that logs diagnostics

### Affected files
- `monitoring/health_actor.py` — new Actor
- `main.py` — register the actor

---

## Completed / Not actionable

- ~~Close-order rejection revert (Option A)~~ — Done 2026-07-03 (see CHANGELOG)
- ~~Flip exit reason SL/TP labeling~~ — Done 2026-07-03 (see CHANGELOG)
- ~~Dead trade purge from open_trades after flip~~ — Done 2026-07-03 (see CHANGELOG)
- ~~Leverage init crash fix~~ — Done 2026-07-02 (see CHANGELOG)
