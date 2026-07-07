# Multi-Exchange / Multi-Symbol Architecture

**Date:** 2026-07-06
**Status:** Implemented. Binance is currently the only registered adapter;
the system trades BTCUSDT (MS + FVG) on it, exactly as before this refactor.

## Why

The system was built assuming exactly one exchange and one symbol
(Binance, BTCUSDT) for the entire process — a single global
`instrument:`/`futures:` config block, a single hardcoded Binance client
pair in `node_builder.py`, `Venue("BINANCE")` and `USDT` hardcoded in the
strategy base class, and a reconciler built around one shared NETTING
position. None of that scales to "MS on ETHUSDT" or "a strategy on a
different exchange" without duplicating the whole stack.

This refactor makes venue and symbol first-class, per-strategy config
values, while keeping Binance/BTCUSDT behavior byte-for-byte identical to
before.

## Config shape

```
venues:                      # connection-level, one entry per exchange
  binance:
    account_type: USDT_FUTURES

symbols:                     # exchange-account-level, one entry per
  binance:BTCUSDT:           # "venue:SYMBOL" pair. Shared by every
    nt_id: BTCUSDT-PERP.BINANCE   # strategy trading that symbol on
    leverage: 10                  # that venue.
    margin_type: CROSSED
    market_lot_size: {min_qty: 0.001, step_size: 0.001}
    min_notional: {notional: 50}

strategies:
  ms:                        # instance name (arbitrary)
    venue: binance
    symbol: BTCUSDT
    ...
  ms_eth:                    # a second instance of the same strategy
    type: ms                 # type: selects the REGISTRY class;
    venue: binance           # defaults to the instance name if omitted
    symbol: ETHUSDT
    ...
```

**Why leverage/margin live under `symbols:`, not per-strategy:** they're
properties of the exchange account + symbol, not of any one strategy. MS
and FVG both trade BTCUSDT today and must agree on leverage — it's a
single exchange-side setting. Keying it on `(venue, symbol)` instead of
per-strategy prevents two strategies from silently fighting over
conflicting leverage values.

**Why `type:` is separate from the instance name:** the instance name is
used for logging, Telegram, state-file naming (`state/{strategy_id}_state.json`,
keyed off `strategy_id` not the YAML key, so this was already fine), and
reconciler bookkeeping — it needs to be unique per strategy *instance*.
The `type:` field is what actually selects a class from
`strategies/__init__.py`'s `REGISTRY`. Without this split, you could never
run the same strategy type on two symbols, since the YAML key doubled as
the REGISTRY lookup key.

## Exchange adapters (`core/exchanges/`)

`core/exchanges/base.py` defines the `ExchangeAdapter` interface:
building NT data/exec client configs, the NT client factory classes,
fetching live exchange filters, connectivity endpoints for
`scripts/check_infra.py`, and API-key validation. `core/exchanges/binance.py`
implements it — this is everything that used to be hardcoded directly in
`node_builder.py` and duplicated in `check_infra.py`, moved into one place.
`core/exchanges/__init__.py` is a registry (`get_adapter(venue)`), mirroring
the existing `strategies/__init__.py` REGISTRY pattern.

**Adding a new exchange** = write `core/exchanges/<name>.py` implementing
the interface, add one line to `ADAPTERS` in `core/exchanges/__init__.py`,
add a `venues:` entry in `settings.yaml`. Nothing else changes — not
`node_builder.py`, not `main.py`, not any strategy code.

## Credentials

Each venue's API key/secret is resolved from environment variables by
naming convention: `{VENUE}_API_KEY` / `{VENUE}_API_SECRET` (live mode),
`{VENUE}_TESTNET_API_KEY` / `{VENUE}_TESTNET_API_SECRET` (paper mode). A
new venue automatically looks for its own env vars — `config/.env` doesn't
need any code changes, just new entries following the pattern already
used for `BINANCE_*`.

## `node_builder.py`

Now loops over every configured venue: for each, resolves its adapter,
builds a data client (and, unless `mode: dry_run`, an exec client) keyed
by the adapter's NT venue string, and registers the client factories on
the `TradingNode`. Symbols are grouped by venue first, so one exec client
per venue carries the leverage/margin settings for every symbol traded on
it (this is how MS + FVG sharing BTCUSDT already worked, generalized to
N symbols per venue).

## Reconciler grouping (`risk/reconciler.py`)

This was the one place the single-instrument assumption ran deep: the
old reconciler summed exposure across *every* registered ledger and
compared it to *one* shared portfolio callable. That's correct only when
every strategy shares one exchange position.

`LedgerReconciler` now keys all internal state — registered ledgers, the
portfolio-position callable, the mutation grace period, and the halt flag
— by `(venue, instrument_id)`. Every public method takes that pair
explicitly. A Case B halt (untracked position) on one symbol/venue no
longer halts any other; each group's grace period after a ledger mutation
is independent. This was necessary groundwork for the reconciler
self-healing redesign in `docs/stage6_reply.md`, which itself needs to
reason per-instrument rather than globally — better to build that on the
correct grouping now than redo it once the redesign lands.

`actors/telegram_actor.py`'s `on_reconcile_warning`/`on_reconcile_halt`
gained a `group` label for the same reason (so an alert says which symbol
it's about), and dropped the hardcoded "BTC" unit suffix since positions
aren't necessarily BTC-denominated anymore.

## Known gaps / deliberately out of scope

- **Cosmetic "BTC" units elsewhere**: a few log lines in
  `risk/position_manager.py` and startup banners still say "BTC" for
  readability. These are informational only (no logic depends on them)
  and weren't touched here to keep this refactor's diff focused — worth a
  pass before actually trading a non-BTC symbol live.
- **Per-venue `mode`**: `mode: dry_run|paper|live` is still global across
  all venues (confirmed acceptable for now — see chat). A venue can't yet
  be paper-traded while another is live in the same process.
- **Only one adapter implemented** (Binance). The interface is designed
  against Binance's shape (leverage/margin per symbol, NETTING positions);
  a very different exchange (e.g. one with no per-symbol leverage concept)
  may need a small interface tweak when it's actually added — not
  speculatively generalized further here.
