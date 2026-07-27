# Changelog

## 2026-07-27 — Feature 2: Sub-bar SL/TP via mark price ticks

### Added
- `risk/position_manager.py` — `on_price()` method checks SL/TP for all open
  trades against a single price point (high=low=close). Called from the strategy
  layer on each mark price tick. No new entries — only exits. Submits close
  orders immediately via `_flush_pending()`.
- `strategies/base_smc_strategy.py` — `subscribe_mark_prices()` in
  `_subscribe_live()` subscribes to NT's standard `MarkPriceUpdate` stream for
  each strategy's instrument.
- `strategies/base_smc_strategy.py` — `on_mark_price()` handler fires ~1/s.
  Throttled to once per second (no double-close risk — `exit_ts` guard in
  `_manage_open_trades()` prevents re-closing). Calls `pm.on_price()` then
  persists state if dirty.
- `strategies/base_smc_strategy.py` — Shared class-level `_last_mark_log` dict
  throttles the diagnostic log to once per 10s per symbol at DEBUG level, so N
  strategies on the same symbol produce one log line per 10s total.

### Changed
- `nautilus_trader` import in `strategies/base_smc_strategy.py` — added
  `MarkPriceUpdate` from `nautilus_trader.model.data`.

## 2026-07-26 — Config validation: SL/TP independence + netting duplicate-symbol guard

### Removed
- Removed `tp1_atr > sl_atr` validation (`core/config.py`). `sl_atr` and
  `tp1_atr` are now independent — SL can be wider, tighter, or equal to TP1 as
  the strategy requires.

### Added
- `_validate()` in `core/config.py`: duplicate (venue, symbol) check for
  netting mode. If 2+ enabled strategies share the same symbol on the same
  venue and that venue is in `netting` mode, startup is rejected with a clear
  error. Hedge mode allows any number of strategies per symbol.

## 2026-07-11 — Hedge mode support (netting stays default) + NT v1.230.0

### Added (hedge mode)
- `venues.<name>.position_mode: netting | hedge` in `settings.yaml` (default
  `netting`). Account-wide Binance setting (`POST /fapi/v1/positionSide/dual`
  per venue), not per symbol or strategy.
- `core/exchanges/binance.py`: `verify_position_mode()` — read-only
  `GET /fapi/v1/positionSide/dual` startup check. Refuses to start if config
  mismatches exchange. `build_exec_client_cfg()` derives `use_reduce_only` from
  `position_mode` (`False` for hedge — Binance rejects `reduceOnly` combined
  with `positionSide`).
- `core/exchanges/base.py`: `verify_position_mode()` interface method.
- `main.py`: startup check verifies each venue's actual mode matches config
  before strategy/node construction.
- `scripts/check_infra.py`: "6. Position mode" diagnostic section (renumbered
  existing sections 7–11).
- `risk/position_manager.py`: `PositionManagerConfig.position_mode`. Under
  hedge, `on_bar()` never calls `_is_flip_scenario()`/`_execute_netted_flip()`
  — LONG and SHORT are independent exchange slots. Pending-order buffer keyed
  by per-side bucket ("LONG"/"SHORT"). `SubmitOrderFn` gains `position_side`
  parameter. `reduce_only` never sent in hedge mode.
- `strategies/base_smc_strategy.py`: `BaseSmcConfig.position_mode` resolved
  from venue at build time. `_make_submit_fn()` tags every order with
  `position_id` ending `-LONG`/`-SHORT` in hedge mode. `_make_position_fn()`
  returns `{"LONG": qty, "SHORT": qty}` dict for hedge reconciler.
- `risk/reconciler.py`: `check()` detects `portfolio_fn()` return shape (float
  vs dict) and branches. Hedge groups compare LONG and SHORT independently
  (no sum-based blind spot). `is_halted()` takes optional `side` — a Case B
  halt on one side only blocks that side's new entries.
- `docs/hedge_mode_implementation.md`, `docs/position_mode_netting_vs_hedge.md`.

### Changed
- `nautilus_trader>=1.228.0` → `>=1.230.0` (`requirements.txt`) — picks up
  upstream hedge position ID fix (PR #4327) and leverage init fix (PR #4289).

### Removed
- **Monkey-patch** `BinanceFuturesExecutionClient._update_account_state`
  (lines 56–85 of `core/exchanges/binance.py`) — dead code since NT
  v1.229.0 fixed the leverage init crash upstream (PR #4289). v1.230.0
  confirmed running clean for 2+ days before removal.

## 2026-07-06 — Multi-exchange / multi-symbol refactor

### Added
- `venues:` and `symbols:` YAML blocks (`config/settings.yaml`) replacing the
  single global `instrument:`/`futures:` blocks. `venues:` holds connection-
  level settings per exchange; `symbols:` (keyed `"venue:SYMBOL"`) holds
  exchange-account settings (leverage, margin type, filter fallbacks) shared
  by every strategy trading that symbol on that venue.
- Every strategy block now declares its own `venue:` and `symbol:`. An
  optional `type:` field decouples a strategy's instance name from its
  REGISTRY class, so the same strategy type can run multiple instances on
  different symbols (e.g. `ms` on BTCUSDT + `ms_eth` on ETHUSDT).
- `core/exchanges/` adapter package (`base.py`, `binance.py`, `__init__.py`
  registry) — all Binance-specific NT wiring (client configs, testnet URLs,
  the v1.228 leverage-init monkey-patch, exchange-filter/API-key HTTP calls)
  moved out of `core/node_builder.py` and `scripts/check_infra.py` into one
  adapter. Adding a new exchange now means one new adapter file + one
  registry line — no changes elsewhere.
- `VenueCredentials` (`core/config.py`) — resolves each venue's API
  key/secret from `{VENUE}_API_KEY`/`{VENUE}_TESTNET_API_KEY`-style env
  vars by naming convention, so a new venue's credentials need no code
  changes, only new `.env` entries.
- `docs/multi_exchange_architecture.md` — design rationale and migration
  notes for this refactor.

### Changed
- `core/node_builder.py`: now loops over configured venues and builds one
  data/exec client pair per venue via its adapter, instead of one hardcoded
  Binance pair.
- `main.py`: resolves each strategy's `instrument_id` and exchange-filter
  fallback from its own `(venue, symbol)` pair instead of one shared global.
- `strategies/base_smc_strategy.py`: `BaseSmcConfig` gains a `venue` field;
  the balance-check closure resolves the NT `Venue` and quote currency from
  the strategy's own config/instrument instead of hardcoded
  `Venue("BINANCE")` / `USDT`; exchange-filter fetch delegates to the
  venue's adapter instead of a hardcoded Binance HTTP call.
- **`risk/reconciler.py` (breaking internal API change)**: `LedgerReconciler`
  now groups all state — registered ledgers, the portfolio-position
  callable, the mutation grace period, and the halt flag — by
  `(venue, instrument_id)` instead of one shared global. Every method
  (`register_strategy`, `set_portfolio_fn`, `record_mutation`, `check`,
  `is_halted`) takes `(venue, instrument_id, ...)`. A Case B halt on one
  symbol/venue no longer affects any other. This was necessary groundwork
  for both this refactor and the upcoming reconciler self-healing redesign
  (`docs/stage6_reply.md`), which also needs per-instrument grouping.
- `actors/telegram_actor.py`: `on_reconcile_warning`/`on_reconcile_halt` take
  an additional `group` label identifying which `(venue, instrument)` group
  triggered the alert; unit labels changed from hardcoded "BTC" to plain
  numbers since positions are no longer necessarily BTC-denominated.
- `scripts/check_infra.py`: connectivity and API-key checks now loop over
  every configured venue via its adapter instead of hardcoding Binance.

### Verified
- Config loads, validates, and rejects bad `venue`/`symbol` references with
  clear errors.
- `build_node()` constructs a real `TradingNode` (against installed
  `nautilus_trader==1.230.0`) with correctly keyed data/exec clients.
- Reconciler groups are independent: a Case B halt on one `(venue,
  instrument)` group does not affect another, verified with a standalone
  BTCUSDT + ETHUSDT test.
- Full pipeline proof: three strategy instances (`ms`→BTCUSDT, `fvg`→BTCUSDT,
  `ms_eth`→ETHUSDT, all on `binance`) build correctly against one shared
  BINANCE data/exec client with per-symbol leverage.

## 2026-07-03 — Close-order rejection revert + flip exit reason labeling

### Added
- `OpenTrade._pending_close_pnl` field — stores the PnL delta from the last
  close submission, used for rejection revert. (`risk/trade_ledger.py`)
- `docs/close_rejection_handling.md` — documents the current revert-based
  approach (Option A) and the deferred-to-fill alternative (Option B) for
  future reference.

### Fixed
- **Close-order rejection ledger corruption**: `_close_trade()` now registers
  close orders in the strategy's `_order_to_trade` map (same pattern as entries).
  `on_order_rejected()` detects close rejections by finding the trade in
  `closed_trades`, then reverts: removes from `closed_trades`, clears `exit_ts`/
  `exit_reason`, subtracts `_pending_close_pnl` from `realized_pnl`, and re-adds
  to `open_trades`. The next bar's management loop retries the close.
  (`risk/position_manager.py`, `strategies/base_smc_strategy.py`)
- **Flip exit reason**: `_execute_netted_flip()` now checks each opposing trade's
  SL/TP levels against the bar's `high/low` to assign the real exit reason
  (`"SL"`, `"BE"`, `"TP1"`, `"TP2"`, or `"exit-signal"`) instead of always
  hardcoding `"exit-signal"`. The FLIP CLOSE log line now includes `reason=`
  for at-a-glance debugging. (`risk/position_manager.py`)
- **Close revert Telegram notification**: new `on_close_reverted()` notifier
  sends `🔄 CLOSE REVERTED — will retry next bar` when a close rejection is
  reverted. (`actors/telegram_actor.py`, `strategies/base_smc_strategy.py`)

## 2026-07-03 — Netted flip: purge dead trades from open_trades (fixes Case B / -2022 cascade)

### Fixed
- `_execute_netted_flip()` in `risk/position_manager.py` now purges closed opposing trades from
  `open_trades` after setting `exit_ts`, preventing the dead trade from offsetting the live flip
  trade in the reconciler's position calculation.
- `_manage_open_trades()` in `risk/position_manager.py` now guards against trades with `exit_ts`
  set, preventing any future code path from re-adding an already-closed trade to `still_open`.

### Bug chain
1. Flip closed opposing SHORT via `record_close(final=True)` but never removed it from `open_trades`.
2. `_manage_open_trades` re-added the dead SHORT to `still_open` (no `exit_ts` check).
3. Reconciler saw FVG ledger with `SHORT -0.001 + LONG +0.001 = 0.000` → **Case B halt**.
4. After real LONG was closed by SL, only the dead SHORT remained → FVG=−0.001 → halt persisted.
5. `reduce_only=True` close orders for trades already gone on exchange → **`-2022` rejection**.

## 2026-07-02 — Futures leverage/margin type config (fixes startup crash after Redis flush)

### Added

- `core/config.py` — New `FuturesSettings` dataclass with `leverage: int` (required) and `margin_type: str | None` (optional). Validation checks leverage >= 1 and margin_type ∈ {CROSSED, ISOLATED}.

- `config/settings.yaml` — New `futures:` block with documented constraints: leverage is safe to change anytime; margin_type requires zero position (Binance error `-4046` if a position exists). When `margin_type` is omitted, no API call is made.

### Changed

- `core/node_builder.py` — Added imports `BinanceSymbol`, `BinanceFuturesMarginType`. `BinanceExecClientConfig` now receives `futures_leverages` (always) and `futures_margin_types` (only when set in YAML). This tells NT to call `POST /fapi/v1/leverage` directly instead of fetching stale testnet position risk data.

- `core/node_builder.py` — Added monkey-patch on `BinanceFuturesExecutionClient._update_account_state` that catches `ValueError` from the per-symbol leverage sync loop (Stage B). NT calls `GET /fapi/v1/symbolConfig` with no filter, then iterates all returned symbols calling `account.set_leverage()`. If any symbol has `leverage=0` (testnet quirk), the dead `except KeyError` handler fails to catch the `ValueError`, crashing `_connect()`. Patch wraps the method and logs a warning instead. Fixed upstream in NT v1.229.0 PR #4289; remove after upgrade.

### Documentation

- `docs/binance_leverage_init_bug.md` — Detailed document explaining the leverage initialization bug, root cause (dead `except KeyError` handler), crash sequence, upstream fix reference, monkey-patch workaround, and removal instructions.

## 2026-07-02 — Option C: netted flip orders (fixes `-2022` ReduceOnly race)

### Added

- `risk/position_manager.py` — `_is_flip_scenario()` detects when the bar signal opposes all open trades (e.g. SHORT signal while LONG trades exist). Returns True even with mixed-direction trades (edge case from restart recovery).

- `risk/position_manager.py` — `_execute_netted_flip()` submits a single atomic market order instead of N closes + 1 entry. Sums opposing-side remaining qty (accounting for partial TP1 closes), checks entry gates with adjusted count (`open_count - opposing + 1`), submits one order with `reduce_only=False`. Closes oppose trades in the ledger with leg-level PnL, opens new trade if gates pass. Logs distinct `FLIP` lines and fires `on_netted_flip` notification.

- `risk/position_manager.py` — Modified `on_bar()` routes to `_execute_netted_flip()` when `_is_flip_scenario()` is true, then still runs `_manage_open_trades()` for any remaining same-direction trades. Non-flip bars unchanged.

- `actors/telegram_actor.py` — `on_netted_flip()` handler sends a summary message (net side/qty, sum opposing, count closed, whether new entry was included) for operational verification of net calculations.

## 2026-06-30 — Exit-signal `reduce_only` fix + warmup timeout safeguard

### Added

- `strategies/base_smc_strategy.py` — Added `_log_warmup_health()` hook, called at end of `_on_warmup_done()`. Default is no-op; subclasses override to log post-warmup indicator state.

- `strategies/ms_strategy.py` — `_log_warmup_health()` logs ATR value and momentum signal states after warmup.

- `strategies/fvg_strategy.py` — `_log_warmup_health()` logs ATR value, total zone count (bull/bear split), and near-zone proximity flags after warmup.

- `NOTES.md` — Added close order rejection retry gap under Ledger Architecture. Documents that Stage 6 should add automatic retry with exponential backoff for rejected close orders in live trading.

- `docs/position_mode_netting_vs_hedge.md` — Reference doc explaining NETTING vs HEDGE position modes on Binance Futures, how our system works on NETTING, what switching to HEDGE would require, and why NETTING is preferred.

### Changed

- `risk/position_manager.py` — Exit-signal close orders now pass `reduce_only=False`. Previously all close types hardcoded `reduce_only=True`, causing `-2022 ReduceOnly Order is rejected` when an exit-signal batch (closing old trades + opening a new opposite-direction trade) had its new entry fill first, shifting net position to zero before all reduce-only exit orders landed. TP/SL/Trailing closes keep `reduce_only=True`.
