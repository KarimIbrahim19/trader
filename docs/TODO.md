# Project TODO

Consolidated backlog across the live trading system, the reconciler,
and the data catalog/collector work. Supersedes the narrower
`docs/todo.md` (Stage 6 follow-ups) — that file's items are folded in
below under "Reconciler & execution robustness" rather than duplicated.

Ordered roughly by priority within each section, not strictly overall —
read the whole list before picking what's next.

---

## 1. Data collection — IN PROGRESS

### 1.1 Live collector service — ✅ Deployed and running
**Files:** `~/catalog/live_collector.py`, `btc-collector.service`, `COLLECTOR_SETUP.md`

Collects liquidations (`btcusdt@forceOrder`) and top-20 partial order
book depth (`btcusdt@depth20@500ms`) — the two things with no
historical source, so every week not running it is backtest history
that can never be recovered. Deployed via systemd on the data collector
server (same box that runs `update_catalog.py`'s cron).

**Status:**
- [x] Deployed via systemd on the data collector server
- [x] `HEARTBEAT` log lines confirm both counters climbing
- [ ] Let it run for at least a few weeks before trying to use the data for anything — a handful of days isn't enough to validate against, especially for liquidations (sparse events)
- [ ] Revisit `--depth-interval` (currently 500ms) once you have a concrete use case that needs finer resolution — don't switch to 100ms preemptively, it's ~5x the storage for no known benefit yet

### 1.2 Mark price vs last-trade price backtest realism check — Not started
**Priority:** Medium

Binance's actual SL/TP/liquidation triggers reference **mark price**,
not last-trade price, but the backtest engine currently triggers off
`close` (last-trade) bars. Mark price klines are already fully
backfilled (2020-09 → now, zero extra download needed).

**Desired outcome:** a diagnostic pass comparing mark price bars to
last-trade bars over a sample period — specifically, how often an SL
would have triggered at a different time/price under mark price vs
last-trade. If the divergence is material, backtest fills should
reference mark price for stop triggers to match live behavior.

### 1.3 Targeted aggTrades/rawTrades downloads for deep-dive analysis — Not started
**Priority:** Low

Tick-level data is 5-20 GB/month per type — not worth bulk-downloading
the full history. Use `download_agg_trades.py` selectively on specific
historical windows (known FVG setups, liquidation cascades once 1.1
has accumulated some) rather than broad backfill.

---

## 2. Strategy & risk improvements using new catalog data

All of these should stay **observational/log-only** until they've
earned enough history to validate properly — funding (~6mo), OI/L-S
ratio/basis (~21 days and growing weekly via the existing cron) are
all too short right now. This mirrors the lesson already documented in
`PROJECT_v2.md`: Layer 3 (FVG-as-filter) overfit from being validated
on too little data. Don't repeat that with less data than FVG had.

### 2.1 CVD-weighted FVG zone confidence — Ready to test now
**Priority:** Medium | **Data needed:** `cvd/*.parquet` (fully backfilled, ready today)

Weight/filter FVG zones by the `cvd_delta` at the bar where the gap
formed — a gap with a strong taker imbalance is more likely a genuine
institutional order-flow gap than noise. This is the only item in this
section that doesn't need to wait for more history.

### 2.2 OI confirmation on FVG zone fill — Wait for more OI history
**Priority:** Low (until OI history is longer)

Rising OI when price returns to fill a zone = fresh positioning
(stronger reaction likely); falling OI = unwind/short-covering
(weaker). Log this alongside real FVG fills now; don't gate entries on
it until OI has several months of history.

### 2.3 Liquidation-adjacent FVG setups — Blocked on 1.1 accumulating data
**Priority:** Medium (once unblocked)

An FVG that forms immediately after/during a liquidation cluster is
close to a textbook ICT stop-hunt-then-reversal setup. Can't do
anything here until the collector (1.1) has meaningfully backfilled.

### 2.4 Funding-regime filter — Partially testable, wait before trusting it
**Priority:** Low

~6 months of funding history exists and grows weekly. Could start
correlating extreme funding readings against FVG/MS entry outcomes now
as a logged/observational field, but treat any backtest conclusion from
it as preliminary until there's a full year+ across different regimes.

### 2.5 Basis (contango/backwardation) regime — Wait for more history
**Priority:** Low

Same treatment as 2.2/2.4 — log now, don't gate on it yet (~21 days of
history currently).

---

## 3. Event-based position management (SL/TP beyond pure ATR)

Framed as layering structure/event awareness **on top of** ATR (as a
floor/ceiling), not replacing it — ATR alone doesn't know where the
setup is actually invalidated; structure alone doesn't know if that
distance is reasonable for current volatility.

### 3.1 Structure-based SL, ATR-bounded — Ready to test now
**Priority:** Medium | Uses data already computed by the market structure module (swing pivots, order blocks) — no new catalog dependency.

SL placed just beyond the actual invalidation swing point, with ATR
only setting a minimum buffer (never tighter than volatility justifies).

### 3.2 Liquidity-target TP — Ready to test now
**Priority:** Medium | Same data dependency as 3.1.

TP targets the next unmitigated swing high/low (classic ICT liquidity
pool) instead of a pure ATR multiple.

### 3.3 Liquidation-spike protective action — Blocked on 1.1
**Priority:** Medium (once unblocked)

A live liquidation cluster on the *same side* as an open position (e.g.
longs liquidating while you're long) is a real-time risk-off signal —
tighten SL or take a partial close immediately rather than waiting for
the mechanical ATR level. The most literal answer to "SL/TP on events,
not just ATR."

### 3.4 Funding-driven exit acceleration — Wait for more funding history
**Priority:** Low

A sharp funding flip against your position direction as an early
tightening trigger. Needs enough funding history to validate before
trusting it (roughly halfway there at 6 months).

---

## 4. Hedge mode rollout — ✅ Enabled and running

Hedge mode was merged (2026-07-08) and enabled on the `binance` venue
on 2026-07-11. The system has been running in hedge mode under paper
trading since activation. See `docs/hedge_mode_implementation.md` for
the full implementation details and `CHANGELOG.md`'s 2026-07-11 entry
for what changed.

**Known follow-up:** No end-to-end `BaseSmcStrategy` test exists yet
exercising a full hedge-mode bar sequence (only `PositionManager`/
`LedgerReconciler` were unit-tested directly) — worth adding before
running with real money.

---

## 5. Reconciler & execution robustness (from `docs/todo.md`, carried forward)

### 5.1 `_execute_netted_flip` ignores `enable_exit_signal` (netting mode only)
**Priority:** Medium — **moot under hedge mode**, but still a live bug
if NETTING is ever used again with multiple strategies on one symbol.

`_execute_netted_flip()`/`_is_flip_scenario()` are a separate code path
from `_manage_open_trades()`'s exit-signal handling and don't check
`enable_exit_signal` at all — this was the proximate trigger for the
2026-07-06 incident. Not fixed as part of the hedge-mode work (hedge
mode sidesteps it structurally instead). Fix directly if NETTING with
multiple strategies-per-symbol is ever used again.

### 5.2 Event-driven pre-bar self-healing reconciler redesign
**Priority:** Medium — see `docs/stage6_reply.md` for the original design proposal.

Still pending. Now needs to account for hedge groups' per-side
structure (LONG/SHORT compared independently) if built after hedge
mode is enabled anywhere.

### 5.3 Close-order retry with exponential backoff
**Priority:** Low | Max 3 retries, exponential backoff, escalate to
Telegram after exhausting retries, prevent new same-side entries while
a close is pending. Files: `risk/position_manager.py`,
`strategies/base_smc_strategy.py`, `actors/telegram_actor.py`.

### 5.4 Case B auto-reset
**Priority:** Low | Auto-clear a halt after N bars of consistent
reconciliation within tolerance, with a Telegram notification when
cleared. File: `risk/reconciler.py`.

### 5.5 Case A auto-heal
**Priority:** Low | Currently warn-only. Consider syncing ledger to
exchange after N consecutive Case A checks, with a clear Telegram
explanation — flagged as risky in the original review, needs a design
decision before implementing, not just a code change. File:
`risk/reconciler.py`.

### 5.6 Health-check / monitoring actor
**Priority:** Low | `monitoring/` directory exists but is empty. A
periodic NT Actor checking bar heartbeat, WebSocket liveness, ledger/
exchange mismatch, balance trend. Files: `monitoring/health_actor.py`
(new), `main.py`.

---

## Completed

- ~~Multi-exchange / multi-symbol refactor~~ — 2026-07-06 (see CHANGELOG)
- ~~Hedge mode implementation~~ — 2026-07-08, enabled 2026-07-11 (see CHANGELOG)
- ~~Live data collector service (liquidations + partial depth)~~ — 2026-07-22, deployed and running via systemd
- ~~Close-order rejection revert (Option A)~~ — 2026-07-03
- ~~Flip exit reason SL/TP labeling~~ — 2026-07-03
- ~~Dead trade purge from open_trades after flip~~ — 2026-07-03
- ~~Leverage init crash fix~~ — 2026-07-02
- ~~Live data collector service (liquidations + partial depth)~~ — 2026-07-22, see §1.1 for deployment steps
