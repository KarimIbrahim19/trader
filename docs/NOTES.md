# Architecture & Operational Notes

## Binance Futures — NT Client Configuration

### Three separate URL parameters on `BinanceExecClientConfig`

| Parameter | Routes | Default (None) | Paper fix |
|---|---|---|---|
| `base_url_http` | REST API (orders, account) | production `fapi.binance.com` | `testnet.binancefuture.com` |
| `base_url_ws` | Market data WS | production `fstream.binance.com` | `stream.binancefuture.com` |
| `base_url_ws_stream` | **User data WS** (listenKey for fills/rejects) | production `fstream.binance.com` | `stream.binancefuture.com` |

All three must be set individually — `base_url_ws` does NOT imply `base_url_ws_stream`.

### `BinanceDataClientConfig` also needs testnet routing

Same three URL params exist on the data client config. Without explicit testnet URLs, the data client hits production with testnet API keys → `-2015 Invalid API-key`.

### Instrument provider defaults to `load_all=False`

Both `BinanceDataClientConfig` and `BinanceExecClientConfig` create instrument providers with `load_all=False, load_ids=None`. No instruments are cached. `self.cache.instrument(instrument_id)` returns `None`. The strategy silently stores `self.instrument = None` and crashes on `make_qty()` at first order submission.

Fix: `instrument_provider=InstrumentProviderConfig(load_all=True)` on both configs.

---

## NT Strategy ID Behavior

### Auto-rename via `order_id_tag`

In `trader.py:408-413`:

```python
if strategy.order_id_tag is None:                              # ← always True when not set
    order_id_tag = f"{len(order_id_tags):03d}"                  # "000" for first strategy
    strategy_id = StrategyId(f"{prefix}-{order_id_tag}")        # "MS-000"
    strategy.change_id(strategy_id)                              # ← renames the strategy
```

- First strategy registered gets tag `000`, second gets `001`
- `MS-001` → internally renamed to `MS-000` (because `order_id_tags` is empty → `000`)
- `FVG-001` → stays `FVG-001` (because `order_id_tags = ["000"]` → `001`)
- Setting `order_id_tag` explicitly in config (e.g., `"001"`) skips the rename entirely
- `StrategyId` requires a hyphen — `MS` alone fails

---

## Binance Error Codes & Edge Cases

### `-4164 MIN_NOTIONAL`

```
Order's notional must be no smaller than 50 (unless you choose reduce only)
```

At ~60k BTC, a half-position TP1 close (0.0005 BTC) is ~30 USDT — below the 50 USDT floor. Fix: `reduce_only=True` on close orders. Binance waives the minimum for reduce-only orders.

`reduce_only=True` is semantically correct on ALL close orders (they always reduce an existing position). Entry orders must use `reduce_only=False`.

### `-2015 Invalid API-key, IP, or permissions`

Testnet API keys only work on testnet base URLs. Production endpoints reject them.

### `BSBUSDT_260618` parse warnings

NT 1.228.0's `BinanceFuturesContractType` enum doesn't include `CURRENT_WEEK`/`NEXT_WEEK`. These are delivery futures, irrelevant to USDT perpetual trading. Warnings are harmless — parser skips and continues.

---

## Ledger Architecture

### Optimistic mutation model with revert (Option A)

The `TradeLedger` + `PositionManager` mutate ledger state **before** Binance
confirms the order, but revert on rejection:

1. `_close_trade()` records PnL, submits the order, registers in `_order_to_trade`
2. If Binance rejects, `on_order_rejected()` finds the trade in `closed_trades`
   and **reverts**: removes from `closed_trades`, clears `exit_ts`/`exit_reason`,
   subtracts `_pending_close_pnl` from `realized_pnl`, re-adds to `open_trades`
3. On the next bar, `_manage_open_trades` retries the close automatically

Both entry and close orders now use the same `_order_to_trade` tracking for
rejection handling — see `docs/close_rejection_handling.md`.

### Known gap: exponential backoff (future)

The revert + next-bar retry works, but there's no retry limit or escalation.
If SL keeps getting rejected bar after bar (e.g., persistent net-flat condition),
it retries indefinitely. Should add max 3 retries → escalate to Telegram.

### Ledger data model

- `open_trades: list[OpenTrade]` — trades still open (mutated by `_manage_open_trades()` each bar)
- `closed_trades: list[OpenTrade]` — fully closed trades (appended by `record_close(final=True)`)
- `record_close(final=False)` for TP1: does NOT remove from open_trades, does NOT reduce `full_qty` — tracks partial state via `tp1_hit` flag and accumulated `realized_pnl`
- `record_close(final=True)` for SL/TP2: appends to `closed_trades`; removal from `open_trades` is implicit (trade not added to `still_open` in the management loop)

---

## Binance API — Useful Endpoints (via Agent Native)

### USDⓈ-M Futures REST

| Endpoint | What it returns |
|---|---|
| `GET /fapi/v2/positionRisk` | Current open positions (size, entry price, PnL) |
| `GET /fapi/v2/account` | Full account state (balances, positions) |
| `GET /fapi/v1/openOrders` | All open orders |
| `GET /fapi/v1/order?symbol=&orderId=` | Single order status |
| `POST /fapi/v1/listenKey` | Start user data WS stream |

### NT already wraps all of these

No need to call Binance REST directly. NT's exec client handles reconciliation automatically:
- `inflight_check_interval_ms=2000` — polls open orders every 2 seconds
- User data WS streams real-time `ACCOUNT_UPDATE` / `ORDER_TRADE_UPDATE` events
- `ExecMassStatus` on startup queries all positions, orders, and fills

### Agent Native discovery

```
https://developers.binance.com/en/docs/llms.txt         # Summary index
https://developers.binance.com/en/docs/llms-full.txt     # Full docs (5MB+)
```

Useful for finding specific endpoints and error codes without browsing the web manually.

---

## Verified: Stage 5 Execution Path Works

Confirmed in paper mode run:
- ✅ Market orders submitted to Binance testnet
- ✅ Fill notifications via user data WS (after `base_url_ws_stream` fix)
- ✅ Slippage logging (`FILL  #00001 LONG signal_px=59645.50 fill_px=59645.50 slippage=+0.0000`)
- ✅ State persistence (`StateStore: saved 1 open trade(s) → state/fvg-001_state.json`)
- ✅ Position opened/changed events from NT portfolio
- ✅ Balance gate working (5,000 USDT free on testnet)
- ✅ TP1 and SL prices calculated correctly from ATR
