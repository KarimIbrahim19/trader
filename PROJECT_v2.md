# BTC SMC Live Trading System — Project Reference

> **Engine:** NautilusTrader v1.228.0  
> **Market:** BTCUSDT-PERP · Binance USDT-M Futures (One-Way / NETTING mode)  
> **Strategy:** ICT / Smart Money Concepts — Market Structure + Fair Value Gaps  
> **Status:** Paper trading (testnet) — Stage 6 complete, Stage 7 pending

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Repository Layout](#2-repository-layout)
3. [Architecture Decisions](#3-architecture-decisions)
4. [Backtesting Phase](#4-backtesting-phase)
5. [Live System Stages](#5-live-system-stages)
6. [Configuration Reference](#6-configuration-reference)
7. [Key Constraints — NT v1.228.0](#7-key-constraints--nt-v12280)
8. [Known Bugs and Fixes](#8-known-bugs-and-fixes)
9. [Open Items and Stage 7](#9-open-items-and-stage-7)
10. [Running the System](#10-running-the-system)

---

## 1. Project Overview

This is a fully automated live trading bot for BTC/USDT perpetual futures on Binance. It ports a Pine Script v6 SMC strategy (15m primary bars, 1H HTF bias) into a production Python system using NautilusTrader as the execution engine.

The strategy uses:
- **Market Structure (MS):** BOS/CHoCH detection, HH/LH/HL/LL classification, momentum signals
- **Fair Value Gaps (FVG):** ICT 3-bar gap pattern, IFVG inversion, zone management
- **HTF Bias:** 1H HMA(21) directional filter
- **Risk:** ATR-based SL + TP1 (50% at 2×ATR) + TP2 (remainder at 3.5×ATR)

Both strategies run simultaneously and independently — each has its own ledger, position manager, risk limits, and state file. They share one Binance NETTING position on the exchange.

---

## 2. Repository Layout

```
live_trader/
├── config/
│   ├── settings.yaml           ← Single source of truth for all config
│   ├── .env                    ← API keys + secrets (git-ignored)
│   └── .env.example            ← Template for secrets
│
├── core/
│   ├── config.py               ← Settings loader (YAML + .env → dataclasses)
│   ├── logging_setup.py        ← Coloured console + rotating file handler
│   ├── node_builder.py         ← NautilusTrader TradingNode builder
│   ├── htf_bias.py             ← 1H HMA directional bias indicator
│   ├── market_structure.py     ← BOS/CHoCH engine (ported from backtest)
│   ├── fvg_zones.py            ← FVG/IFVG zone tracker (ported from backtest)
│   └── atr.py                  ← Wilder ATR (ported from backtest)
│
├── strategies/
│   ├── __init__.py             ← REGISTRY: name → {settings_cls, strategy_cls}
│   ├── base_smc_strategy.py    ← Abstract base (Template Method pattern)
│   ├── ms_strategy.py          ← Market Structure strategy
│   └── fvg_strategy.py         ← Fair Value Gap strategy
│
├── risk/
│   ├── trade_ledger.py         ← OpenTrade dataclass + TradeLedger manager
│   ├── position_manager.py     ← Signal-agnostic SL/TP/risk management
│   └── reconciler.py           ← Cross-strategy ledger↔exchange reconciliation
│
├── persistence/
│   └── state_store.py          ← JSON state save/load per strategy
│
├── actors/
│   └── telegram_actor.py       ← TelegramNotifier (fire-and-forget)
│
├── scripts/
│   ├── check_infra.py          ← 10-point infrastructure validator
│   └── compare_bars.py         ← Live bar vs catalog comparison tool
│
├── docs/
│   ├── binance_leverage_init_bug.md  ← NT v1.228 crash diagnosis + patch
│   ├── option_c_netted_orders.md     ← -2022 race analysis + Option C design
│   └── position_mode_netting_vs_hedge.md ← NETTING vs HEDGE reference
│
├── state/                      ← Persisted open trade JSON (git-ignored)
├── logs/                       ← Rotating log files (git-ignored)
├── main.py                     ← Entry point
├── NOTES.md                    ← NT constraints + operational reference
├── CHANGELOG.md                ← Full change history with dates
└── PROJECT.md                  ← This file
```

---

## 3. Architecture Decisions

### 3.1 Why a custom TradeLedger instead of NT's position system

Under `OmsType.NETTING` (Binance one-way mode), NT maintains **one blended position per instrument** — it cannot distinguish which signal or which strategy contributed to a fill. Our `TradeLedger` + `PositionManager` provide per-trade tracking that NT's NETTING model cannot:

- Individual trade SL/TP levels (each entry has its own ATR-sized levels)
- Per-trade PnL attribution (for Telegram notifications and backtest parity)
- Multi-strategy isolation (MS and FVG have separate ledgers)
- Partial close tracking (TP1 fires at 50%, TP2 fires the remainder)

NT's portfolio remains the **financial source of truth** — account balance and actual fills come from there. The ledger is for risk management logic only.

### 3.2 Template Method pattern

```
BaseSmcStrategy (abstract)
    ├── MsStrategy   — implements _init_signal_modules() + _process_primary_bar()
    └── FvgStrategy  — implements _init_signal_modules() + _process_primary_bar()
```

Everything shared (bar routing, HTF gate, order submission, warmup, persistence, notifications, reconciliation) lives in `BaseSmcStrategy`. Adding a new strategy requires only:
1. A new `XxxSettings` dataclass + `XxxStrategyConfig` + `XxxStrategy` class
2. One REGISTRY entry in `strategies/__init__.py`
3. One YAML block in `settings.yaml`

No changes to `config.py`, `main.py`, or any other file.

### 3.3 YAML as single source of truth

All defaults were removed from config dataclasses and NT strategy configs. Every value must be explicitly set in `settings.yaml`. Missing required fields fail fast with a clear error at startup. This prevents silent behavior differences between environments.

### 3.4 NETTING + buffered order submission (Option C)

Because all strategies share one Binance position, submitting N individual close orders + 1 entry for a "flip" bar (opposing signal while trades are open) creates a race condition: if the entry fills before the closes, Binance rejects the reduce-only close orders with `-2022`. The fix is to **buffer all orders for the bar**, then flush them as a single atomic market order:

```
_enqueue() calls throughout on_bar()
    → _flush_pending() at end of on_bar()
    → _submit_split(): close (reduce_only) first, then entry
    → NET qty = Σ opposing_qty + new_trade_qty → one market order
```

This aligns order submission with how Binance actually operates in NETTING mode.

### 3.5 Reconciler authority model

Our ledger mutates optimistically (before exchange confirmation). To detect drift between ledger and exchange, `LedgerReconciler` runs a bar-aligned check before each bar's signal logic:

- **Case A (exchange < expected):** WARNING + Telegram. External close (liquidation/manual/ADL). No auto-correction yet — learn from mismatches first. Auto-heal deferred to Stage 7.
- **Case B (exchange > expected):** HALT all new entries + CRITICAL Telegram. Untracked position. Requires manual restart after resolution.

A grace period (default 15s) skips the check immediately after any ledger mutation to avoid false positives during in-flight order confirmation.

---

## 4. Backtesting Phase

**Location:** `~/data/` (separate from this repo)

### Signal modules (pure Python, zero NT dependency)

All ported from backtest to `core/`:

| Module | What it does |
|---|---|
| `market_structure.py` | BOS/CHoCH detection, HH/LH/HL/LL, pivot ATR filter, two-stage trend confirmation |
| `htf_bias.py` | 1H HMA(21) bull/bear state |
| `fvg_zones.py` | FVG/IFVG zones, signal + filter modes, sig_cooldown, max_age |
| `atr.py` | Wilder ATR (same implementation as Pine Script) |

### Backtesting scripts

- `backtest_ms_signal.py` — MS strategy with multi-position ledger
- `backtest_fvg_signal.py` — FVG strategy with multi-position ledger
- Both have CLI toggles: `--trail`, `--be`, `--no-exit`, `--no-sl`, `--htf-filter`
- JSON export for comparing parameter sets

### Key findings

- **Layer 2 (MS + HTF)** is the validated robust baseline — tested on 29-month BTC catalog
- **FVG-as-filter (Layer 3)** showed improvement on a short catalog but reversed on the full catalog — confirmed overfitting
- FVG in **signal mode** (standalone entries on zone bounces) is used in production, but parameters are still being validated
- HTF filter adds meaningful selectivity on higher timeframes; not yet re-validated on 1m primary

---

## 5. Live System Stages

### Stage 1 — Foundation ✅

**Goal:** Infrastructure skeleton, config loading, NT node builder.

**Files built:**
- `core/config.py` — Settings loader with validation. Reads `settings.yaml` + `.env`. All config flows through typed dataclasses. YAML is single source of truth.
- `core/logging_setup.py` — ISO 8601 timestamps with microseconds. Coloured console (`_ColouredFormatter`), plain rotating file (`_PlainFormatter`). Saves/restores `record.levelname` to prevent ANSI leak into file log.
- `core/node_builder.py` — Builds NT `TradingNode`. Connects Binance USDT-M Futures data client. Redis cache via `DatabaseConfig`.
- `scripts/check_infra.py` — Validates config, env, Redis, Binance network, API keys, Telegram, signal modules, strategy modules, NT version, state files.

### Stage 2 — Live Data Feed Validation ✅

**Goal:** Confirm live Binance bars match backtest catalog quality.

**Files built:**
- `strategies/data_validator.py` — Subscribes to 15m + 1H bars, logs every bar to CSV with arrival delay, checks ordering at hour boundaries.
- `scripts/compare_bars.py` — Diffs live CSV against catalog.

**Result:** $0.00 average price difference vs catalog, zero ordering violations. Live feed validated.

### Stage 3 — Strategy Port ✅

**Goal:** Port backtest ledger + risk logic to live system. Both strategies running in dry_run.

**Key design:**
- `risk/trade_ledger.py` — `OpenTrade` dataclass mirrors backtest exactly. `TradeLedger` manages open/closed lists. Record lifecycle: `record_open()` → `record_close(final=False)` for TP1 → `record_close(final=True)` for SL/TP2.
- `risk/position_manager.py` — `on_bar()` → `_manage_open_trades()` → SL/TP checks → `_enter()`. Crash-safe dirty flag: saves ledger to JSON after every mutation.
- `persistence/state_store.py` — JSON file per strategy in `state/`. Loaded in `on_start()`, updated after every trade event.
- `strategies/base_smc_strategy.py` — Abstract base with bar routing, HTF gate, order submission closure, persistence lifecycle.
- `strategies/ms_strategy.py` + `fvg_strategy.py` — Concrete implementations. Each adds its signal-specific fields and `build_config()`.
- `strategies/__init__.py` — `REGISTRY` dict: `{"ms": {settings: MsSettings, strategy: MsStrategy}, ...}`. Config loader and main.py use this for dispatch.

### Stage 4 — Telegram Notifications ✅

**Goal:** Real-time trade alerts to Telegram.

**Design:** `TelegramNotifier` is a plain Python class (not an NT Actor). Trade events flow through a `notify_fn` callback from `PositionManager`. HTTP calls run in a `ThreadPoolExecutor` (fire-and-forget). Timers use `threading.Timer` daemon threads.

**Events notified:**
- System start/stop (with session summary)
- Signal fired (before PM processes it, so signal arrives before blocked-entry note)
- Entry blocked (max_open_trades or daily_loss_limit)
- Kill switch activated
- Trade opened (entry, SL, TP1, TP2, size)
- TP1 hit (leg PnL, new SL if breakeven)
- Trade closed (reason, total PnL, duration)
- Order rejected (entry or close)
- Close reverted (TP1 rejection rollback)
- State restored on restart
- Reconciler Case A warning / Case B halt
- Heartbeat (every N minutes, combined across strategies)
- Daily summary (at configurable UTC time)

**Shutdown ordering:** `on_system_stop()` must be called before `stop_timers()` — `stop_timers()` calls `executor.shutdown(wait=True)` which blocks until the shutdown message HTTP call completes.

### Stage 5 — Real Order Execution ✅

**Goal:** Real market orders on Binance testnet (paper mode).

**Key fixes required during testing:**

| Issue | Fix |
|---|---|
| `NoneType` on `make_qty()` | `InstrumentProviderConfig(load_all=True)` on both data and exec clients |
| Fill callbacks never fired | All 3 URL params required: `base_url_http`, `base_url_ws`, `base_url_ws_stream` |
| `-4164 MIN_NOTIONAL` on TP1 | `reduce_only=True` on all close orders (Binance waives min notional for reduce-only) |
| `-2022 ReduceOnly` on flip bars | Option C netted orders (see §3.4) |
| Leverage init crash | NT v1.228 bug — monkey-patched in `node_builder.py` (see §8.1) |
| Cold-start warmup delay (26+ hrs for HTF) | Phase 1: `request_bars()` REST warmup in `on_start()` (2-3s) |

**Warmup mechanism:**
- `request_bars()` called in `on_start()` for primary + HTF bars
- `on_historical_data()` buffers bars by `ts_init` (deduplication across strategies sharing a bar type)
- `_on_warmup_done()` replays buffer sorted chronologically, then calls `_subscribe_live()`
- 60s timeout via `clock.set_timer()` — falls back to live subscription without full warmup

**Order submission:**
- `_submit_fn` returns `Optional[str]` (NT `client_order_id`, or `None` in dry_run)
- `_order_to_trade: dict[str, list[int]]` — maps `client_order_id` → list of trade IDs (multiple trades can share one netted order)
- `on_order_rejected()` detects entry vs close rejection by searching `open_trades` vs `closed_trades`
- `on_order_filled()` logs slippage (signal price vs actual fill) — ledger keeps signal price for backtest parity

**Balance gate:**
- `_make_balance_check_fn()` reads free USDT from NT portfolio
- Returns `float("inf")` when portfolio not ready (never blocks on API errors)
- Only wired in paper/live — dry_run always skips

### Stage 6 — Reconciliation & Monitoring ✅

**Goal:** Detect and surface ledger↔exchange position drift.

**6A — NT native reconciliation** (`node_builder.py`):
```python
LiveExecEngineConfig(
    position_check_interval_secs = 60.0,  # verify NT portfolio vs exchange
    open_check_interval_secs     = 60.0,  # verify open orders vs exchange
)
```
Keeps NT's own internal state self-healing, independent of our reconciler.

**6B — LedgerReconciler** (`risk/reconciler.py`):
- Computes expected net BTC exposure: `Σ(remaining_qty per trade, signed by side)` across all ledgers
- Compares to `portfolio.net_position(instrument_id)` from NT
- Grace period: skips check within `grace_secs` (default 15s) of last ledger mutation
- Case A / Case B handling (see §3.5)
- Called at top of `on_bar()` before signal logic

**6D — Startup reconciliation:**
- After `ExecMassStatus` completes (NT queries exchange on connect), `on_start()` runs a reconcile check if trades were restored from persistence
- Surfaces any drift that accumulated while the process was down

**Option C — Netted flip orders:**
- Single atomic market order replaces N closes + 1 entry on flip bars
- `_is_flip_scenario()` detects opposing signal + open trades in opposite direction
- `_execute_netted_flip()` calculates net qty, submits one order with `reduce_only=False`
- Assigns correct exit reason per trade (checks SL/TP levels vs bar high/low)
- Purges closed opposing trades from `open_trades` after the flip (prevents reconciler false positives)
- `_manage_open_trades()` guards against trades with `exit_ts` set

**Close rejection revert:**
- All close orders registered in `_order_to_trade` (same as entries)
- Final close rejection: removes from `closed_trades`, reverts `realized_pnl`, re-adds to `open_trades`
- Partial (TP1) rejection: reverts `realized_pnl`, resets `tp1_hit = False`, restores original SL from `_pre_tp1_sl`, clears trailing TP2 state

**Exchange filters:**
- `_fetch_exchange_filters()` in `on_start()` — queries `GET /fapi/v1/exchangeInfo` for `MARKET_LOT_SIZE` and `MIN_NOTIONAL`
- Falls back to YAML `exchange_filters` block if API call fails
- Used by `_submit_split()` for MIN_NOTIONAL validation before order submission

---

## 6. Configuration Reference

### settings.yaml structure

```yaml
mode: paper              # dry_run | paper | live

trader_id: CLTRADER-001  # unique per instance (prevents Redis key collisions)

instrument:
  symbol: BTCUSDT
  nt_id: BTCUSDT-PERP.BINANCE
  account_type: USDT_FUTURES

futures:
  leverage: 10           # required; applied via API on every startup
  margin_type: CROSSED   # optional; omit if position is open (Binance -4046)

strategies:
  ms:
    enabled: true
    strategy_id: MS-001
    primary_bar: 15m
    htf_bar: 1h
    # ... signal params, risk params, warmup_bars

  fvg:
    enabled: false
    strategy_id: FVG-001
    # ...

exchange_filters:        # fallback if GET /fapi/v1/exchangeInfo fails
  BTCUSDT:
    market_lot_size: {min_qty: 0.001, step_size: 0.001}
    min_notional: {notional: 50}

reconciliation:
  enabled: true
  grace_secs: 15.0       # skip check within N seconds of any ledger mutation
  tolerance_btc: 0.0001  # differences below this = rounding/fees, not a mismatch

redis: {host: localhost, port: 6379, timeout_secs: 20}
logging: {level: INFO, level_file: DEBUG, ...}
telegram: {enabled: true, heartbeat_interval_mins: 60, ...}
```

### .env secrets (never in YAML)

```
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TESTNET_API_KEY=
BINANCE_TESTNET_API_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### Mode behaviour

| Mode | Orders | Exec client | Reconciler |
|---|---|---|---|
| `dry_run` | Logged only | None | Disabled (no portfolio) |
| `paper` | Testnet | Testnet URLs | Enabled |
| `live` | Production | Production URLs | Enabled |

---

## 7. Key Constraints — NT v1.228.0

All documented in `NOTES.md`. Critical ones:

| Constraint | Detail |
|---|---|
| `BinanceAccountType.USDT_FUTURES` | Not `USDT_FUTURE` |
| NT logger is single-string | `self.log.info(f"msg {val}")` — no printf args |
| Three URL params for testnet | `base_url_http`, `base_url_ws`, `base_url_ws_stream` all required |
| `InstrumentProviderConfig(load_all=True)` | Must be on BOTH data and exec clients |
| Strategy ID auto-rename | NT may rename `MS-001` → `MS-000` internally; set `order_id_tag` explicitly to prevent |
| `reduce_only=True` on closes | Required to bypass 50 USDT MIN_NOTIONAL on partial closes |
| Leverage init crash | See §8.1 — monkey-patched, fixed in v1.229.0 |
| `is_stopped` doesn't exist | Use `node.dispose()` only; `node.stop()` is internal |
| No custom signal handlers | NT owns SIGINT/SIGTERM; don't override |

---

## 8. Known Bugs and Fixes

### 8.1 Leverage init crash (NT v1.228.0) — `ValueError: leverage was not >= 1`

**Symptom:** Crashes on startup after Redis flush with `ValueError(leverage was not >= 1)` in `ExecClient-BINANCE`.

**Root cause:** `_update_account_state()` fetches `GET /fapi/v1/symbolConfig` for ALL symbols (no filter). Testnet returns some symbols with `leverage: 0`. The `except KeyError` handler at line 241 is dead code — `_get_cached_instrument_id()` never raises — so the `ValueError` propagates.

**Fix:** Monkey-patch in `core/node_builder.py` wraps `_update_account_state` and catches the specific error. See `docs/binance_leverage_init_bug.md` for full analysis.

**Upstream fix:** Merged in NT v1.229.0 (PR #4289). Remove the monkey-patch block from `node_builder.py` after upgrading.

### 8.2 Dead trade stale in `open_trades` after netted flip

**Symptom:** Reconciler fires Case B halt on every bar after a flip. `-2022` rejections cascade.

**Root cause:** `_execute_netted_flip()` called `record_close(final=True)` (→ added to `closed_trades`) but never removed the trade from `open_trades`. `_manage_open_trades()` re-added it to `still_open` on every subsequent bar.

**Fix:** Purge in `_execute_netted_flip()` after setting `exit_ts`. Guard in `_manage_open_trades()` skips any trade where `exit_ts is not None`.

### 8.3 TP1 rejection left `tp1_hit=True` and SL at breakeven

**Symptom:** After TP1 close order is rejected, the strategy tries to manage TP2 when the exchange still has the full 0.001 BTC position.

**Fix:** On TP1 rejection, the revert now restores all 4 state changes:
1. `realized_pnl` (via `_pending_close_pnl`)
2. `tp1_hit = False`
3. `sl` restored from `_pre_tp1_sl` sentinel saved before the TP1 fire
4. `best_price = None`, `trail_distance = None`

---

## 9. Open Items and Stage 7

### Immediate (before going live)

- [ ] **Extended paper validation** — run both strategies for at least 4 weeks in paper mode. Verify trade lifecycle end-to-end: open → TP1 → TP2 and open → SL.
- [ ] **Re-enable exit signals** — `enable_exit_signal: true` was disabled during testing to avoid `-2022` races. Option C is implemented; re-enable and verify no rejections.
- [ ] **FVG parameter validation** — FVG signal-mode parameters haven't been back-tested on the full 29-month catalog yet.
- [ ] **HTF filter validation** — `htf_filter: false` currently. Re-enable once HTF parameters are re-validated on 1m primary bars.
- [ ] **Upgrade NT to v1.229.0** — removes the need for the monkey-patch. After upgrading, delete the patch block in `node_builder.py` and verify startup still works after Redis flush.

### Stage 7 — Redis Bar Buffer (scalability)

**When to build:** When 3+ strategies are running continuously in live mode and the 2-3s restart warmup becomes operationally costly.

**Design:**
```
services/bar_buffer.py    ← standalone process
    subscribes to Binance WebSocket
    writes bars to Redis sorted sets (ring buffer, last 500 bars)
    key: "bars:{instrument}:{timeframe}"

base_smc_strategy.py
    _load_from_buffer()   ← reads from Redis first
    falls back to request_bars() if buffer not running
```

The buffer is transparent — the strategy checks Redis first and falls back to `request_bars()` if the buffer isn't running. No new failure modes introduced.

### Stage 7 — Reconciler Case A auto-heal

Currently Case A (exchange < expected) is warn-only. Once the system has collected real mismatch events and their causes are understood, auto-heal can be implemented:

**Policy:** FIFO-close the oldest open trade(s) across affected strategies until ledger sum matches exchange. Mark with `exit_reason="EXTERNAL_RECONCILE"`.

**Prerequisite:** At least 4 weeks of paper trading with reconciler enabled and all Case A events reviewed manually.

### Stage 7 — Close order retry

**Current state:** Close rejections revert the ledger and the trade is retried on the next bar's SL/TP check. This is correct but the retry depends on price still being at the SL/TP level.

**Planned improvement:** Retry queue with exponential backoff for close orders that are rejected for transient reasons (network timeout, rate limit). Permanent rejections (insufficient balance, invalid qty) should still just revert.

### listenKey watchdog (dropped from Stage 6)

NT manages the user data WebSocket internally. Querying connection health from Python would require accessing Rust internals — not worth the scope. If fill callbacks stop arriving, the reconciler will detect the resulting drift within one grace period. Document this as "reconciler is the watchdog for WS health."

---

## 10. Running the System

### Prerequisites

```bash
pip install nautilus_trader redis python-dotenv pyyaml
redis-server &
```

### Configuration

```bash
cp config/.env.example config/.env
# Fill in API keys in config/.env
# Edit config/settings.yaml: mode, strategies, etc.
```

### Infrastructure check

```bash
python scripts/check_infra.py
```

Validates Redis, Binance connectivity, API keys, Telegram, all module imports, state files.

### Start trading

```bash
python main.py
python main.py --config config/settings.yaml  # explicit config path
```

### Mode progression

1. Start in `mode: dry_run` — signals logged, no orders placed
2. Confirm signals look correct in logs and Telegram
3. Switch to `mode: paper` — real orders on Binance testnet
4. Run for 4+ weeks. Review every Case A reconcile warning manually.
5. Switch to `mode: live` — real orders, real funds

### State files

```
state/ms-001_state.json    ← MS open trades (git-ignored)
state/fvg-001_state.json   ← FVG open trades (git-ignored)
```

On restart: trades are restored, `_log_reconciliation()` logs the ledger state, and a startup reconciliation check compares against the real exchange position.

### Adding a new strategy

1. Create `strategies/new_strategy.py` with `NewSettings`, `NewStrategyConfig`, `NewStrategy`
2. Add one entry to `strategies/__init__.py` REGISTRY
3. Add one block to `config/settings.yaml`
4. Add signal module to `core/` if needed

No changes to `config.py`, `main.py`, `node_builder.py`, or any other file.
