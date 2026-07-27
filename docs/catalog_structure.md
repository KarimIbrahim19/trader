# BTCUSDT Data Catalog — Current Structure & Available Data

## Catalog schema (`/mnt/btc_catalog`)

```
/mnt/btc_catalog/
│
├── last_updated.txt                        ← cron writes timestamp here after each update
│
├── data/                                   ← NautilusTrader ParquetDataCatalog
│   ├── bar/
│   │   ├── BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL/
│   │   │   └── 2020-09-01T..._2026-06-30T23-59-00....parquet
│   │   ├── BTCUSDT-PERP.BINANCE-5-MINUTE-LAST-EXTERNAL/
│   │   │   └── 2020-09-01T..._2026-06-30T23-55-00....parquet
│   │   ├── BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL/
│   │   │   └── 2020-09-01T..._2026-06-30T23-45-00....parquet
│   │   ├── BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL/
│   │   │   └── 2020-09-01T..._2026-06-30T23-00-00....parquet
│   │   ├── BTCUSDT-PERP.BINANCE-4-HOUR-LAST-EXTERNAL/
│   │   │   └── 2020-09-01T..._2026-06-30T20-00-00....parquet
│   │   └── BTCUSDT-PERP.BINANCE-1-DAY-LAST-EXTERNAL/
│   │       └── 2020-09-01T..._2026-06-30T00-00-00....parquet
│   └── crypto_perpetual/
│       └── BTCUSDT-PERP.BINANCE/
│           └── 1970-01-01T..._1970-01-01T....parquet  (instrument definition)
│
├── cvd/                                     ← taker buy/sell volume per bar (plain parquet)
│   ├── 1m.parquet
│   ├── 5m.parquet
│   ├── 15m.parquet
│   ├── 1h.parquet
│   ├── 4h.parquet
│   └── 1d.parquet
│
├── funding_rate.parquet                     ← 8h snapshots, ~6 months
│
├── oi/                                      ← open interest history
│   ├── 1h.parquet
│   └── 4h.parquet
│
├── ls_ratio/                                ← long/short ratios
│   ├── top_accounts_1h.parquet              ← top 20% traders by account count
│   ├── top_accounts_4h.parquet
│   ├── top_positions_1h.parquet             ← top 20% traders by position size
│   ├── top_positions_4h.parquet
│   ├── global_accounts_1h.parquet           ← all traders
│   ├── global_accounts_4h.parquet
│   ├── taker_1h.parquet                     ← taker buy/sell volume ratio
│   └── taker_4h.parquet
│
├── mark_price_klines/                       ← mark price bars (all intervals)
│   ├── 1m.parquet
│   ├── 5m.parquet
│   ├── 15m.parquet
│   ├── 1h.parquet
│   ├── 4h.parquet
│   └── 1d.parquet
│
├── index_price_klines/                      ← index price bars (all intervals)
│   ├── 1m.parquet
│   ├── 5m.parquet
│   ├── 15m.parquet
│   ├── 1h.parquet
│   ├── 4h.parquet
│   └── 1d.parquet
│
├── basis/                                   ← futures basis (futures price − index price)
│   ├── 1h.parquet
│   └── 4h.parquet
│
├── depth20/                                 ← top-20 order book snapshots (live, from systemd collector)
│   ├── 2026-07-22.parquet
│   ├── 2026-07-23.parquet
│   ├── 2026-07-24.parquet
│   ├── 2026-07-25.parquet
│   ├── 2026-07-26.parquet
│   └── 2026-07-27.parquet                   ← (date changes daily)
│
├── aggTrades/                               ← aggregate trades (tick-level, on-demand)
│   ├── 2026-04.parquet                      ← ~775 MB
│   ├── 2026-05.parquet                      ← ~632 MB
│   └── 2026-06.parquet                      ← ~1 GB
│
└── rawTrades/                               ← raw trades (tick-level, on-demand)
    ├── 2026-04.parquet                      ← ~1.3 GB
    ├── 2026-05.parquet                      ← ~1.1 GB
    └── 2026-06.parquet                      ← ~1.7 GB
```

---

## Data coverage summary

| Data | Historical range | Source limit |
|------|-----------------|--------------|
| OHLCV bars + CVD | 2020-09 → present | None (public repo) |
| Mark price klines | 2020-09 → present | None (REST, full range) |
| Index price klines | 2020-09 → present | None (REST, full range) |
| Funding rate | ~6 months | API lookback limit |
| Open Interest | ~21 days | API lookback limit |
| L/S ratios (all types) | ~21 days | API lookback limit |
| Basis | ~21 days | API lookback limit |
| Depth20 book snapshots | 2026-07-22 → present | Live only (no historical REST) |
| AggTrades | On-demand (monthly zips) | On-demand download |
| Raw Trades | On-demand (monthly zips) | On-demand download |

---

## Available Binance USDS Futures WebSocket streams

### Already subscribed (live data flowing)

| Stream | Provides | Currently used? |
|--------|----------|-----------------|
| `<symbol>@kline_<interval>` | OHLCV bars | ✅ Strategy entries |
| `<symbol>@markPrice` | Mark price, funding rate | ✅ Strategy mark price ticks |
| `<symbol>@depth20@100ms` | Top-20 order book snapshots | ✅ Live depth20 collector (`live_collector.py`) |
| `<symbol>@bookTicker` | Best bid/ask, spread | ❌ Not in strategy |

### Defined in NT adapter but NOT subscribed (data type exists, handler not wired)

| Stream | Data | Historical source? |
|--------|------|-------------------|
| `!forceOrder@arr` | **Liquidations** — side, price, qty, timestamp | ❌ **No historical source at all**. Only live. |
| `<symbol>@openInterest` | Open Interest (real-time changes) | ✅ Partial — REST has ~21 days of history |

### Available via NT adapter (`subscribe_order_book_deltas`)

| Stream | Data | Notes |
|--------|------|-------|
| `<symbol>@depth` | Full order book diff depth | Very high data volume. No historical book snapshots exist anywhere. |

### NOT exposed by NT adapter (would need custom WebSocket client)

| Stream | Data | Historical source? |
|--------|------|-------------------|
| `<symbol>@aggTrade` | Aggregate trades (real-time) | ✅ Available via `data.binance.vision` monthly zips |

---

## What has no historical source at all

Only **one** data type:

| Stream | Why collect live |
|--------|-----------------|
| `!forceOrder@arr` (liquidations) | Once the WebSocket message fires, it's gone forever. Binance provides no REST endpoint or public repo for historical liquidations. If we want to backtest with liquidation data, we must start recording now and accumulate a dataset over time. |

Everything else in the catalog is either:
- Fully available from 2020 (OHLCV, CVD, mark price, index price)
- Available via REST with limited lookback (funding, OI, L/S, basis)
- Downloadable from `data.binance.vision` (aggTrades, raw trades)
