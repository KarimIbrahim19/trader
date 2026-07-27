# Binance USDS Futures — Historical Data for Backtesting

> A reference for every historical dataset Binance provides,
> what we already have, what's missing, and how to fill the gaps
> so your backtests run on real exchange data.

---

## Table of Contents

1. [Data Sources](#1-data-sources)
2. [REST API Endpoints (Backfill via HTTP)](#2-rest-api-endpoints-backfill-via-http)
3. [Public Data Repository (Bulk Download)](#3-public-data-repository-bulk-download)
4. [Our Current Catalog](#4-our-current-catalog)
5. [What's Missing and How to Proxy It](#5-whats-missing-and-how-to-proxy-it)
6. [Download Strategy](#6-download-strategy)
7. [Catalog Structure Reference](#7-catalog-structure-reference)

---

## 1. Data Sources

Binance provides historical data through two channels:

| Source | Requires API Key? | Rate Limits | Best For |
|--------|-------------------|-------------|----------|
| **REST API** (`GET /fapi/v1/...`) | No | IP-based, ~1200 req/min | Small backfills, recent data, any interval |
| **Public Data Repository** (`data.binance.vision`) | No | None (direct download) | Large backfills, full months, no gap |

The public repository is almost always better — it gives you entire months in a single zip, no rate limits, and includes all kline intervals and data types.

---

## 2. REST API Endpoints (Backfill via HTTP)

Use these when you need a specific slice of recent data (e.g., last 24 hours) or when the public repo doesn't have a data type (e.g., L/S ratios).

### 2.1 Klines — OHLCV Bars

**Endpoint:** `GET https://fapi.binance.com/fapi/v1/klines`

**Parameters:**

| Param | Example | Notes |
|-------|---------|-------|
| `symbol` | `BTCUSDT` | |
| `interval` | `1h` | `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, `1d`, `3d`, `1w`, `1M` |
| `startTime` | `1622505600000` | Optional, ms |
| `endTime` | `1622592000000` | Optional, ms |
| `limit` | `500` | Max 1500 per request |

**Response format (array of arrays):**

```
Index  Field              Type         Notes
─────────────────────────────────────────────────────
0      open_time          int (ms)
1      open               str          Price
2      high               str
3      low                str
4      close              str
5      volume             str          Base asset volume
6      close_time         int (ms)     Use as ts_event
7      quote_volume       str          Quote asset volume
8      trades_count       int          Number of trades in the bar
9      taker_buy_base     str          ★ Taker buy base volume (CVD source)
10     taker_buy_quote    str          Taker buy quote volume
11     ignore             str          Unused
```

**Max lookback:** From the earliest available data (varies by symbol, BTCUSDT goes back to ~2019).

**Rate limit:** IP weight 2 per request.

**Use case:** Backfill a few days of recent bars, or fill gaps in the public data.

### 2.2 Continuous Klines

**Endpoint:** `GET https://fapi.binance.com/fapi/v1/continuousKlines`

Same format as klines, but for continuous contracts (pairs that roll over). For perpetuals like `BTCUSDT-PERP`, this endpoint is generally not needed — use standard klines instead.

### 2.3 Mark Price Klines

**Endpoint:** `GET https://fapi.binance.com/fapi/v1/markPriceKlines`

Historical mark price klines. Same format as standard klines, but the price fields (open/high/low/close) are **mark prices** instead of last trade prices.

**Use case:** Backtesting mark-price-based signals, comparing last vs mark divergence historically.

### 2.4 Index Price Klines

**Endpoint:** `GET https://fapi.binance.com/fapi/v1/indexPriceKlines`

Historical index price klines. Same format, but prices are **index prices** (multi-exchange BTC index).

**Use case:** Calculate historical basis (`last - index`) in backtests.

### 2.5 Aggregate Trades

**Endpoint:** `GET https://fapi.binance.com/fapi/v1/aggTrades`

**Response fields:**

```
Field              Notes
─────────────────────────────────
Aggregate tradeId  Monotonic, can paginate
Price              Execution price
Quantity           Base asset quantity
First tradeId      Individual fill range
Last tradeId
Timestamp          ms
Buyer is maker     False = buyer aggressive
```

**Max lookback:** Limited — goes back a few hours/days, not months.

**Rate limit:** IP weight 20 per request.

**Limitation:** Cannot batch-download years of tick data via REST. Use the public repository for bulk aggTrade data.

### 2.6 Funding Rate History

**Endpoint:** `GET https://fapi.binance.com/fapi/v1/fundingRate`

**Parameters:** `symbol`, `startTime`, `endTime`, `limit` (max 1000)

**Response:**

```json
{
  "symbol": "BTCUSDT",
  "fundingTime": 1622505600000,  // funding settlement timestamp
  "fundingRate": "0.000100"       // the rate applied
}
```

**Max lookback:** ~3-6 months from current date.

**Rate limit:** IP weight 20.

**Funding interval:** Every 8 hours for most perpetuals (00:00, 08:00, 16:00 UTC).

### 2.7 Open Interest History

**Endpoint:** `GET https://fapi.binance.com/fapi/v1/openInterestHist`

**Parameters:** `symbol`, `period` (`5m`/`15m`/`30m`/`1h`/`2h`/`4h`/`6h`/`12h`/`1d`), `limit` (max 500), `startTime`, `endTime`

**Response:**

```json
{
  "symbol": "BTCUSDT",
  "sumOpenInterest": "15004.5",
  "sumOpenInterestValue": "964500000",  // in USDT
  "timestamp": 1622505600000
}
```

**Max lookback:** ~3-6 months.

**Rate limit:** IP weight 0 (free tier).

### 2.8 Top Trader Long/Short Ratio

**Endpoint:** `GET https://fapi.binance.com/futures/data/topLongShortAccountRatio`

**Parameters:** `symbol`, `period`, `limit`, `startTime`, `endTime`

**Response:**

```json
{
  "symbol": "BTCUSDT",
  "longShortRatio": "1.43",
  "longAccount": "0.59",
  "shortAccount": "0.41",
  "timestamp": 1583139600000
}
```

**Also available:**
- `topLongShortPositionRatio` — by position size instead of account count
- `globalLongShortAccountRatio` — all traders, not just top 20%

**Max lookback:** ~30 days.

**Rate limit:** IP weight 0.

### 2.9 Taker Long/Short Ratio

**Endpoint:** `GET https://fapi.binance.com/futures/data/takerlongshortRatio`

**What it is:** `taker_buy_volume / taker_sell_volume` over the period. This is the aggressive order flow ratio.

**Max lookback:** ~30 days.

### 2.10 Basis

**Endpoint:** `GET https://fapi.binance.com/futures/data/basis`

**What it is:** Historical basis data (`futures_price - index_price`) for perpetuals and quarterly contracts.

**Max lookback:** ~30 days.

---

## 3. Public Data Repository (Bulk Download)

**Base URL:** `https://data.binance.vision/data/futures/um/`

### 3.1 Available Data Types

The repository provides **monthly** and **daily** zipped CSV files.

| Data Type | Path Prefix | Available Since |
|-----------|-------------|-----------------|
| **Klines** | `monthly/klines/{SYMBOL}/{INTERVAL}/` | ~2019 (varies by symbol) |
| **Aggregate Trades** | `monthly/aggTrades/{SYMBOL}/` | ~2020 |
| **Trades** (raw fills) | `monthly/trades/{SYMBOL}/` | ~2020 |

**Daily** variants are also available: replace `monthly` with `daily` in the path.

### 3.2 Kline Files (What We Already Use)

**URL pattern:**
```
https://data.binance.vision/data/futures/um/monthly/klines/{SYMBOL}/{INTERVAL}/{SYMBOL}-{INTERVAL}-{YYYY}-{MM}.zip
```

**Example:**
```
https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2025-01.zip
```

CSV columns match the REST kline format exactly (11 fields including `taker_buy_base`).

**Supported intervals:** `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, `1d`, `3d`, `1w`, `1mo`.

### 3.3 Aggregate Trade Files

**URL pattern:**
```
https://data.binance.vision/data/futures/um/monthly/aggTrades/{SYMBOL}/{SYMBOL}-aggTrades-{YYYY}-{MM}.zip
```

**CSV columns:**

```
Aggregate tradeId  Price  Quantity  First tradeId  Last tradeId  Timestamp  Is buyer maker
```

**Why you'd want these:** To build **real tick-level CVD** in backtests instead of relying on bar-level taker volume. With individual aggTrades, you can:
- Build CVD at any resolution (not just bar boundaries)
- Detect CVD divergences within a bar
- Compute order flow imbalance at tick scale

### 3.4 Trade Files (Raw Fills)

**URL pattern:**
```
https://data.binance.vision/data/futures/um/monthly/trades/{SYMBOL}/{SYMBOL}-trades-{YYYY}-{MM}.zip
```

**CSV columns:**

```
trade Id  price  qty  quoteQty  time  isBuyerMaker
```

**Caveat:** Raw trades are very large. A month of BTCUSDT trades can be several GB. Only download these if you need tick-level data for a specific backtesting window.

### 3.5 Data That Does NOT Exist in the Public Repository

The following futures data types are **not available** from data.binance.vision:

- ❌ Funding rate history
- ❌ Open interest history
- ❌ Top trader L/S ratios
- ❌ Taker L/S ratios
- ❌ Basis data
- ❌ Liquidation history (`!forceOrder`)
- ❌ Order book depth snapshots
- ❌ Mark price klines
- ❌ Index price klines

For these, use the REST API instead (see Section 2).

---

## 4. Our Current Catalog

The live BTCUSDT catalog sits at `/mnt/btc_catalog/` (shared mount from
the data collector server). Reference copies of the download scripts are
at `~/catalog/`. For the full current structure see `docs/catalog_structure.md`.

**We currently have:**
- ✅ OHLCV bars at 1m, 5m, 15m, 1h, 4h, 1d (via `download_btc_data.py`)
- ✅ CVD data (taker buy volume) at same intervals (computed from klines)
- ✅ Instrument definition (CryptoPerpetual)
- ✅ Mark price klines at 1m, 5m, 15m, 1h, 4h, 1d (REST backfill, 2020-09 → present)
- ✅ Index price klines at 1m, 5m, 15m, 1h, 4h, 1d (REST backfill, 2020-09 → present)
- ✅ Funding rate history (~6 months via REST)
- ✅ Open Interest history (~21 days via REST)
- ✅ L/S ratios and basis data (~21 days via REST)
- ✅ Live liquidation recording (live collector, accumulating since 2026-07-22)
- ✅ Live depth20 snapshots (live collector, accumulating since 2026-07-22)
- ✅ AggTrades and RawTrades (on-demand monthly download)

**We are missing (no historical source):**
- ❌ Liquidation history before the live collector started recording
- ❌ Historical order book snapshots (depth data before live collector)

---

## 5. What's Missing and How to Proxy It

> **Note:** Most items listed below as "missing" have since been added to
> the catalog (1m bars, mark/indices klines, funding rate, OI, L/S ratios,
> basis, aggTrades). See `docs/catalog_structure.md` for the current state.
> The sections below are kept as reference for how they were implemented.

### 5.1 1-Minute Bars — ✅ Now collected

**Missing from catalog:** We only have 5m+ bars, but our live strategy uses 1m bars.

**Solution:** Add `1m` to `TIMEFRAMES` in `download_btc_data.py` and re-run. The public repo has 1m klines for BTCUSDT going back years.

```python
TIMEFRAMES = [
    ("1m",  BarAggregation.MINUTE, 1),   # ADD THIS
    ("5m",  BarAggregation.MINUTE, 5),
    ("15m", BarAggregation.MINUTE, 15),
    ("1h",  BarAggregation.HOUR,   1),
    ("4h",  BarAggregation.HOUR,   4),
    ("1d",  BarAggregation.DAY,    1),
]
```

### 5.2 Taker Buy Volume in Catalog (BinanceBar)

**Missing:** Our catalog stores standard NT `Bar` objects, not `BinanceBar`. The standard `Bar` does NOT include `taker_buy_base_volume`.

**Solution:** The download script already saves CVD data separately as parquet files (`cvd/{interval}.parquet`). The backtest code (e.g., `backtest_layer4.py`) reads these separately. This is fine — no catalog change needed.

To get `BinanceBar` directly in the catalog, we'd need to write a custom catalog writer, which is unnecessary since CVD is already available.

### 5.3 Mark Price Klines

**Missing:** We have last-price klines, not mark-price klines.

**Solution:** REST API provides them. Add a download function:

```python
def download_mark_price_klines(symbol: str, interval: str, start: int, end: int):
    """Download mark price klines from the REST API."""
    rows = []
    for start_time in range(start, end, 1500 * interval_ms(interval)):
        resp = requests.get(
            "https://fapi.binance.com/fapi/v1/markPriceKlines",
            params={"symbol": symbol, "interval": interval,
                    "startTime": start_time, "limit": 1500},
        )
        rows.extend(resp.json())
    return pd.DataFrame(rows, columns=[...])
```

**Use case:** Backtesting strategies that use mark price (e.g., funding rate arbitrage, mark-vs-last divergence).

### 5.4 Funding Rate History

**Missing from catalog.**

**Available via:** REST API (`GET /fapi/v1/fundingRate`), max 1000 records per request, ~3-6 month lookback.

**Storage:** Save as parquet at `catalog/funding_rate.parquet`.

**Use case:** Backtesting the funding regime filter from `binance_data_guide.md`.

### 5.5 Open Interest History

**Missing from catalog.**

**Available via:** REST API (`GET /fapi/v1/openInterestHist`), various periods (5m to 1d).

**Storage:** Save as parquet at `catalog/oi/{period}.parquet`.

**Use case:** Backtesting OI trend confirmation (price + OI matrix).

### 5.6 Top Trader L/S Ratio History

**Missing from catalog.**

**Available via:** REST API (`GET /futures/data/topLongShortAccountRatio`), ~30 day lookback, 5m to 1d periods.

**Storage:** Save as parquet at `catalog/ls_ratio/{period}.parquet`.

**Use case:** Backtesting sentiment-based entry filters.

### 5.7 Aggregate Trades (Tick-Level CVD)

**Missing from catalog.**

**Available via:** Public data repository at `data.binance.vision` — monthly zips.

**Storage:** Save as parquet at `catalog/agg_trades/{YYYY-MM}.parquet`.

**Use case:** Tick-level CVD backtesting. Instead of per-bar delta (which only gives you one data point per bar), aggTrades let you compute delta at any resolution. This is the most accurate way to backtest CVD divergence strategies.

**Download approach:**

```python
def download_agg_trades_month(symbol: str, year: int, month: int) -> pd.DataFrame:
    url = (
        f"https://data.binance.vision/data/futures/um/monthly/aggTrades/"
        f"{symbol}/{symbol}-aggTrades-{year}-{month:02d}.zip"
    )
    resp = requests.get(url)
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    csv_file = z.namelist()[0]
    df = pd.read_csv(z.open(csv_file), names=[
        "agg_trade_id", "price", "qty", "first_trade_id",
        "last_trade_id", "timestamp", "is_buyer_maker",
    ])
    df["side"] = df["is_buyer_maker"].apply(
        lambda x: "sell" if x else "buy"
    )  # False = buyer aggressive
    return df
```

### 5.8 Liquidation History — The Critical Gap

**NOT AVAILABLE** from any Binance source. Binance does not serve historical liquidation data via REST or the public repository. You only get live liquidation data via the `!forceOrder@arr` WebSocket stream — and once it's gone, it's gone.

**How to proxy liquidation data in backtests:**

| Proxy Method | Accuracy | Complexity |
|-------------|----------|------------|
| **Use bar wicks as liquidation proxies** — A long liquidation cascade creates a wick below the bar body. If `(close - low) > 2 * (high - close)`, selling was aggressive and likely included liquidations. | Low-Medium | Very Low |
| **Use CVD delta** — A negative delta bar with a large wick down is likely liquidation-driven. `cvd_delta < -0.3` + wick > 2x body = liquidation cascade. | Medium | Low |
| **Use volume spikes** — If volume is > 2x the 20-bar average and the bar has a long wick, liquidations are likely happening. | Low | Very Low |
| **Record live data to a file** — Subscribe to `!forceOrder@arr` in your live system and save every event to a parquet file. Over months of live running, you build your own liquidation dataset. | High (for future) | Medium |

**Recommendation:** Start with the CVD + wick proxy (cheap, easy, reasonably accurate). If liquidations become a core part of your strategy, build the live recording pipeline and accumulate a dataset over time.

---

## 6. Download Strategy (legacy reference)

> **Note:** Data collection is now managed by `update_catalog.py` on the
> data collector server, running weekly via cron (Sun 3AM). The `download_btc_data.py`
> script (at `~/catalog/`) is the single entry point for all REST-based
> downloads. The sections below document the original approach; the script
> now has built-in append mode for incremental updates and rate-limited
> pagination for every endpoint.

### Current `download_btc_data.py` capabilities

The script downloads all available data types in a single invocation:

1. OHLCV bars (all intervals from public repository)
2. CVD data (computed from klines)
3. Mark price klines (REST, all intervals, 2020-09 → present)
4. Index price klines (REST, all intervals, 2020-09 → present)
5. Funding rate history (REST, ~6 months)
6. Open Interest history (REST, ~21 days, multiple periods)
7. L/S ratios (REST, ~21 days, multiple types and periods)
8. Basis data (REST, ~21 days)
9. AggTrades / RawTrades (on-demand via public repository)

The `update_catalog.py` cron wrapper adds append-mode invocations for
each data type so that weekly runs only fetch new data since the last run.

### Priority Order (historical — all now collected)

| Priority | Data | Why | Size Impact |
|----------|------|-----|-------------|
| **P0** | **1m bars** | Needed to backtest the live strategy | ~300 MB/month |
| **P1** | **Funding rate** | Funding regime filter is a game-changer for strategy robustness | ~1 MB total |
| **P1** | **Open Interest history** | Trend confirmation, OI + price matrix | ~10 MB total |
| **P2** | **Top Trader L/S ratio** | Sentiment filter for entries | ~5 MB total |
| **P3** | **Mark price klines** | Basis calculation, mark-vs-last divergence | ~300 MB/month |
| **P4** | **Aggregate trades** | Tick-level CVD, only if you need sub-bar precision | ~5-10 GB/month |

### Rate Limit Management

The REST API allows ~1200 IP-weighted requests per minute. Since most historical endpoints have weight 0-20, you can safely download months of data in a single run.

```python
import time

def rate_limited_get(url, params, weight=2):
    resp = requests.get(url, params=params)
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", 60))
        time.sleep(wait)
        return rate_limited_get(url, params, weight)
    time.sleep(0.1 * weight)  # Polite delay
    return resp
```

---

## 7. Catalog Structure Reference

The live catalog at `/mnt/btc_catalog/`:

```
/mnt/btc_catalog/
├── data/                             ← NautilusTrader ParquetDataCatalog
│   ├── bar/
│   │   ├── BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL/
│   │   ├── BTCUSDT-PERP.BINANCE-5-MINUTE-LAST-EXTERNAL/
│   │   ├── BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL/
│   │   ├── BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL/
│   │   ├── BTCUSDT-PERP.BINANCE-4-HOUR-LAST-EXTERNAL/
│   │   └── BTCUSDT-PERP.BINANCE-1-DAY-LAST-EXTERNAL/
│   └── crypto_perpetual/
│       └── BTCUSDT-PERP.BINANCE/
├── cvd/                             ← taker buy/sell volume per bar
│   ├── 1m.parquet
│   ├── 5m.parquet
│   ├── 15m.parquet
│   ├── 1h.parquet
│   ├── 4h.parquet
│   └── 1d.parquet
├── funding_rate.parquet             ← 8h snapshots, ~6 months
├── oi/                              ← open interest history
│   ├── 1h.parquet
│   ├── 4h.parquet
├── ls_ratio/                        ← long/short ratios
│   ├── top_accounts_1h.parquet
│   ├── top_accounts_4h.parquet
│   ├── top_positions_1h.parquet
│   ├── top_positions_4h.parquet
│   ├── global_accounts_1h.parquet
│   ├── global_accounts_4h.parquet
│   ├── taker_1h.parquet
│   └── taker_4h.parquet
├── mark_price_klines/               ← mark price bars (all intervals)
│   ├── 1m.parquet
│   ├── 5m.parquet
│   ├── 15m.parquet
│   ├── 1h.parquet
│   ├── 4h.parquet
│   └── 1d.parquet
├── index_price_klines/              ← index price bars (all intervals)
│   ├── 1m.parquet
│   ├── 5m.parquet
│   ├── 15m.parquet
│   ├── 1h.parquet
│   ├── 4h.parquet
│   └── 1d.parquet
├── basis/                           ← futures basis
│   ├── 1h.parquet
│   └── 4h.parquet
├── depth20/                         ← live-collected depth snapshots
│   └── *.part-*.parquet
├── liquidations/                    ← live-collected liquidation events
│   └── *.part-*.parquet
├── aggTrades/                       ← aggregate trades (on-demand)
│   └── 2026-06.parquet
└── rawTrades/                       ← raw trades (on-demand)
    └── 2026-06.parquet
```

See `docs/catalog_structure.md` for the full data coverage summary.
---

> **Bottom line:** Everything you need to backtest OHLCV-based strategies (MS, FVG) plus CVD is already available with the current download script. Adding 1m bars is the single highest-impact change — it lets you backtest at the same resolution you trade live. Funding rate and OI history are the next most valuable additions, enabling regime-based filtering in backtests just like you'd use live.
