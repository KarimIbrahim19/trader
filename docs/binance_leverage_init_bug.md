# Binance Futures Leverage Initialization Bug (NT v1.228)

## Symptom

After a Redis flush (or any cache wipe), the system crashes on startup with:

```
ERROR  ExecClient-BINANCE: Error on '_connect'
ValueError(leverage was not >= 1)
```

This happens even though the log shows leverage was set correctly:

```
INFO  ExecClient-BINANCE: Set default leverage BTCUSDT 10X
```

## Root Cause

`BinanceFuturesExecClient._update_account_state()` (in
`nautilus_trader/adapters/binance/futures/execution.py`) has **two**
leverage-setting stages:

### Stage A — Config-driven API call (succeeds, lines 208-216)

```python
if self._leverages:
    for symbol, leverage in self._leverages.items():
        await self._futures_http_account.set_leverage(symbol, leverage)
```

This calls `POST /fapi/v1/leverage` for our configured symbols. It
**succeeds** — BTCUSDT gets leverage=10 and we see the log line.

### Stage B — Symbol config sync (crashes, lines 233-243)

```python
symbol_configs = await self._futures_http_account.query_futures_symbol_config()
for config in symbol_configs:
    try:
        instrument_id = self._get_cached_instrument_id(config.symbol)
        leverage = Decimal(config.leverage)
        account.set_leverage(instrument_id, leverage)
    except KeyError:
        continue    # ← DEAD CODE — _get_cached_instrument_id NEVER raises
```

This calls `GET /fapi/v1/symbolConfig` **without a symbol filter**, so
Binance returns configurations for **every symbol the account has ever
interacted with** (including ones with no position). If any of those symbols
has `leverage: 0` (common on testnet for unused/discontinued symbols), then
`account.set_leverage(instrument_id, Decimal(0))` raises:

```
ValueError("leverage was not >= 1")
```

The `except KeyError` on line 241 is **dead code** — `_get_cached_instrument_id()`
creates a synthetic `InstrumentId` for any symbol string and never raises.
The ValueError propagates unhandled, aborting `_connect()` and the entire
node startup.

### Crash sequence

```
_connect()
  → _update_account_state()
    → POST /fapi/v1/leverage — sets BTCUSDT 10X  ✓
    → GET /fapi/v1/symbolConfig (ALL symbols)     ✓
    → loop:
        account.set_leverage(XXXUSDT, Decimal(0)) — CRASH ✗
```

### Why it appeared after Redis flush

Before the flush, NT cached parsed leverage data in Redis. On restart it
read from cache and never re-fetched from the exchange. Flushing Redis
exposed the raw testnet response, triggering the bug.

### Why the testnet asset reset didn't help

Resetting testnet assets resets wallet balances and position data but
does **not** clear the symbol configuration history. The `symbolConfig`
endpoint still returns records for every symbol the account has ever
traded or configured, and some retain `leverage: 0`.

## Upstream Fix

PR [#4289](https://github.com/nautechsystems/nautilus_trader/pull/4289)
was merged in NT v1.229.0, which fixes the dead `except KeyError` to
properly catch `ValueError` (or validates leverage before calling
`set_leverage`). The changelog entry reads:

> **Fixed Binance Futures leverage initialization aborting execution
> client connect**

## Our Workaround (Monkey-patch)

**File:** `core/node_builder.py`

A monkey-patch wraps `_update_account_state` to catch the specific
`ValueError` and log a warning instead of crashing:

```python
import nautilus_trader.adapters.binance.futures.execution as _futures_exec

_orig = _futures_exec.BinanceFuturesExecutionClient._update_account_state

async def _patched(self):
    try:
        await _orig(self)
    except ValueError as e:
        if "leverage was not >= 1" in str(e):
            logger.warning("Ignored testnet leverage quirk: %s", e)
        else:
            raise

_futures_exec.BinanceFuturesExecutionClient._update_account_state = _patched
```

### Why this is safe

1. **Stage A (API leverage set) completes before Stage B** — our BTCUSDT
   leverage=10 is applied to the exchange regardless.
2. **Account state generation, API auth check, and account registration**
   all happen before Stage B in the method body.
3. The crash only kills the per-symbol `set_leverage` copy for symbols
   with invalid leverage — BTCUSDT trading is unaffected.
4. The warning log makes the issue visible for debugging.

## Removing the Patch

- The patch is safe to leave in place indefinitely.
- After upgrading to **NT v1.2290+**, remove the entire monkey-patch block
  from `node_builder.py` (search for `"Monkey-patch"` — lines 39-65).
  Also remove the `import _futures_exec` line at line 50 — this import
  is only needed for the monkey-patch and has no other use in the file.
- Verify the fix works by flushing Redis and restarting.
