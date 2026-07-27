# Binance USDS Futures Data Guide

> A progressive reference: from raw ticks to profitable signals.
> Written for a trader who wants to understand *every* data type Binance
> provides, what each field means, and how to turn data into trading
> decisions.

---

## Table of Contents

1. [The Data Stack](#1-the-data-stack)
2. [Data Level 1 — Raw Events (Ticks)](#2-data-level-1--raw-events-ticks)
3. [Data Level 2 — Aggregated Windows (Bars / Snapshots)](#3-data-level-2--aggregated-windows-bars--snapshots)
4. [Data Level 3 — Derived / Analytical (Calculated Metrics)](#4-data-level-3--derived--analytical-calculated-metrics)
5. [NT Adapter Reference](#5-nt-adapter-reference)
6. [How to Use Each Data Type for Signals](#6-how-to-use-each-data-type-for-signals)
7. [Putting It Together — Which Data for Which Strategy](#7-putting-it-together)

---

## 1. The Data Stack

All trading data lives at one of three levels of abstraction:

```
Level 3:  DERIVED METRICS (OI history, L/S ratio, funding history)
              ↑ polls / computes from
Level 2:  AGGREGATED WINDOWS (bars, book snapshots, 24h ticker)
              ↑ windows over
Level 1:  RAW EVENTS (trades, order-book deltas, liquidations, mark price updates)
              ↑ happen at
           THE EXCHANGE (matching engine)
```

- **Level 1** is the truth. Every fill, every order-book change, every liquidation — these are raw facts.
- **Level 2** groups Level 1 events into windows. A 1-minute bar summarises all trades in that minute.
- **Level 3** computes ratios, histories, and statistics from Level 1 or Level 2 data.

The closer you stay to Level 1, the more signal you can extract — but the more data you must process. All three levels are useful; the trick is knowing which level fits each signal.

---

## 2. Data Level 1 — Raw Events (Ticks)

### 2.1 Aggregate Trade Stream (`@aggTrade`)

**What it is:** Every individual trade fill on the exchange, aggregated per taker order. Binance groups fills from the same taker order at the same price into one message, sent every ~100ms.

**Why this exists:** The raw matching engine produces individual fills. The `@aggTrade` stream rolls fills from the same taker order into a single message so you don't get overwhelmed.

**What's in each message:**

| Field | Example | Description |
|-------|---------|-------------|
| Aggregate trade ID | `296781` | Unique ID for this aggregate trade. Monotonic increasing. |
| Price | `64321.50` | Execution price (string, to avoid float precision loss) |
| Quantity | `0.050` | Base asset quantity filled (e.g., 0.05 BTC) |
| First trade ID | `301452` | First individual fill ID in this aggregate |
| Last trade ID | `301455` | Last individual fill ID in this aggregate |
| Trade time | `1625564712345` | Timestamp in milliseconds when the trade occurred |
| Is buyer the maker | `false` | `false` = buyer is taker (sell order ate the ask); `true` = seller is taker (buy order ate the bid). **This is the most important field** — it tells you which side was aggressive. |
| Is best match | `true` | Whether this was the best available price match |

**Trade side logic:**

- `is_buyer_maker = false` → buyer is taker → the trade was **buyer-aggressive** (the buyer lifted the ask)
- `is_buyer_maker = true` → seller is taker → the trade was **seller-aggressive** (the seller hit the bid)

**NT subscription:**

```python
# In your strategy's on_start():
self.subscribe_trade_ticks(instrument_id)

# Handler:
def on_trade_tick(self, tick: TradeTick) -> None:
    # tick.price       -> Price object
    # tick.quantity    -> Quantity object
    # tick.aggressor_side  -> OrderSide.BUY or OrderSide.SELL
    # tick.ts_event    -> timestamp
    self._process_trade(tick)
```

---

### 2.2 Mark Price + Funding Rate Stream (`@markPrice`)

**What it is:** Periodic updates (every 1s or 3s) of the mark price, index price, and funding rate. This is NOT the last traded price — it's a separate calculation used for liquidations and funding payments.

**Why mark price != last price:** If only the last traded price determined liquidations, whales could manipulate it with small orders to trigger mass liquidations. The mark price is a **fair-value average** across multiple venues, making it much harder to manipulate.

**What's in each message:**

| Field | Example | Description |
|-------|---------|-------------|
| Instrument | `BTCUSDT` | Which symbol |
| **Mark price** | `64320.00` | The fair value price used for liquidation calculations and unrealized PnL. Computed from the index price + basis decay. |
| **Index price** | `64318.50` | The price of BTC across multiple spot exchanges (Binance spot, Coinbase, Kraken, etc.), weighted by volume. This is the closest thing to "the real price of Bitcoin." |
| Estimated settle price | `64320.00` | For perpetuals, this equals mark price most of the time. For quarterly futures, it converges to index price at settlement. |
| **Funding rate** | `0.0001` | The rate paid between long and short positions every 8 hours. Positive (0.01%) means longs pay shorts; negative means shorts pay longs. |
| Next funding time | `1625565600000` | Unix ms timestamp of the next funding settlement |
| Update speed | `1000` or `3000` | Milliseconds between updates (1s or 3s) |

**Mark vs Index vs Last — the three prices you need to know:**

```
Last price  = what the last trade executed at
Index price = what BTC is "really worth" (multi-exchange average)
Mark price  = what your position is valued at for liquidation (index + fair basis)
```

**Why three prices?** The gap between them tells you something:
- `last > mark` → the market is bidding up aggressively (potential short squeeze)
- `last < mark` → the market is selling off (potential long squeeze)
- `mark - index` → the basis (premium or discount of futures vs spot)

**NT subscription:**

```python
# Via dedicated method:
self.subscribe_mark_prices(instrument_id)

# Or via generic DataType subscription:
from nautilus_trader.adapters.binance import BinanceFuturesMarkPriceUpdate

self.subscribe_data(
    DataType(BinanceFuturesMarkPriceUpdate, metadata={"instrument_id": instrument_id})
)

# Handler:
def on_data(self, data: Data) -> None:
    if isinstance(data, BinanceFuturesMarkPriceUpdate):
        # data.mark          -> mark price (Price)
        # data.index         -> index price (Price)
        # data.funding_rate  -> Decimal (e.g., 0.0001 = 0.01%)
        # data.next_funding_ns -> nanoseconds until next funding
        self._process_mark_price(data)
```

---

### 2.3 Liquidation Order Stream (`@forceOrder` / `!forceOrder@arr`)

**What it is:** Every time a position gets force-liquidated (margin too low), Binance pushes one message. Only the latest liquidation per symbol per 1000ms window is sent — if 10 positions get liquidated in 1 second, you only see the most recent one.

**What's in each message:**

| Field | Example | Description |
|-------|---------|-------------|
| Instrument | `BTCUSDT` | |
| **Side** | `SELL` | Side of the liquidation order. `SELL` = longs getting liquidated (they are being sold). `BUY` = shorts getting liquidated. |
| **Price** | `63800.00` | The price at which the liquidation was executed |
| **Average price** | `63810.50` | Average fill price of the liquidation order |
| **Accumulated quantity** | `12.500` | Total filled quantity of the liquidation order (how much BTC was liquidated) |
| **Last filled quantity** | `2.300` | The most recent fill chunk |

**How to read the side:**
- `side = SELL` → **longs** are being liquidated → these are forced sellers → downward pressure
- `side = BUY` → **shorts** are being liquidated → these are forced buyers → upward pressure

**Why liquidations matter:** Liquidations create **cascades**. A falling price triggers long liquidations → forced selling → price drops more → more long liquidations. This is how flash crashes happen. But cascades also exhaust themselves — once all weak longs are flushed, the selling stops and the price can snap back. That snap-back creates the wicks that FVG strategies love.

**NT subscription:**

```python
# NOTE: NT's Binance adapter defines the BinanceFuturesLiquidation data type
# but does NOT handle subscription to the !forceOrder@arr stream yet.
# Calling subscribe_data(DataType(BinanceFuturesLiquidation, ...)) will log
# "Cannot subscribe to ... (not implemented)" and do nothing.
#
# Two options to use liquidation data:

# --- Option A: Subscribe via raw WebSocket client (in node_builder.py) ---
# In core/node_builder.py, after building the data client, add:
#
# ws_client = adapter._get_ws_client(data_client)
# await ws_client._subscribe("!forceOrder@arr")
# ws_client._ws_handlers["!forceOrder@arr"] = my_liquidation_handler
#
# Where my_liquidation_handler(raw_bytes) decodes the JSON and creates
# BinanceFuturesLiquidation objects manually.

# --- Option B: Extend the adapter (recommended for production) ---
# Add to BinanceCommonDataClient._subscribe():
#
#     elif command.data_type.type == BinanceFuturesLiquidation:
#         await self._ws_client.subscribe("!forceOrder@arr")
#
# And add a _handle_liquidation() method to decode the JSON.
# The Binance forceOrder message format is:
# {
#   "e": "forceOrder",
#   "E": 1625564712345,
#   "o": {
#     "s": "BTCUSDT",      # symbol
#     "S": "SELL",          # side (BUY or SELL)
#     "p": "63800.00",      # liquidation price
#     "ap": "63810.50",     # average price
#     "q": "12.500",        # accumulated quantity
#     "l": "2.300",         # last filled quantity
#     "T": 1625564712345    # trade time
#   }
# }

# Handler (works once data is flowing):
def on_data(self, data: Data) -> None:
    if isinstance(data, BinanceFuturesLiquidation):
        # data.side              -> OrderSide.BUY (short liq) or SELL (long liq)
        # data.price             -> liquidation price
        # data.average_price     -> average fill
        # data.accumulated_qty   -> total liquidated size
        # data.last_filled_qty   -> last fill chunk
        self._process_liquidation(data)
```

---

### 2.4 Book Ticker Stream (`@bookTicker`)

**What it is:** Real-time updates to the **best bid** and **best ask**. Every time the top of the order book changes, you get a message. This is NOT the full order book — it's only the best price on each side.

**What's in each message:**

| Field | Example | Description |
|-------|---------|-------------|
| Instrument | `BTCUSDT` | |
| **Best bid price** | `64318.00` | Highest price a buyer is willing to pay right now |
| **Best bid quantity** | `0.850` | How many BTC are bid at that price |
| **Best ask price** | `64318.50` | Lowest price a seller is willing to accept right now |
| **Best ask quantity** | `1.200` | How many BTC are offered at that price |

**What you can compute from these four numbers:**

- **Spread** = `ask_price - bid_price`. The cost of crossing the spread. Tighter spread = more liquid.
- **Bid/ask imbalance** = `bid_qty / ask_qty`. If 5 BTC bid vs 1 BTC asked, buyers are more aggressive.
- **Bid/ask ratio** = `bid_qty / (bid_qty + ask_qty)`. >0.5 means more liquidity on bid side.

**NT subscription:**

```python
# Subscribes to best bid/ask updates
self.subscribe_quote_ticks(instrument_id)

# Handler:
def on_quote_tick(self, tick: QuoteTick) -> None:
    # tick.bid      -> best bid price (Price)
    # tick.ask      -> best ask price (Price)
    # tick.bid_size -> best bid quantity (Quantity)
    # tick.ask_size -> best ask quantity (Quantity)

    spread = tick.ask.as_double() - tick.bid.as_double()
    imbalance = tick.bid_size.as_double() / tick.ask_size.as_double()
```

---

### 2.5 Order Book Depth Stream (`@depth`, `@depth5`, `@depth10`, `@depth20`)

**What it is:** Changes to the order book. Two modes:

- **Diff depth** (`@depth`) — every change to the book (bids or asks added, removed, or modified). You must maintain a local copy of the book and apply these deltas to it.
- **Partial depth** (`@depth5` / `@depth10` / `@depth20`) — a snapshot of the top N levels on each side, sent at intervals. Much simpler to use — no local book management needed.

**What's in a partial depth snapshot:**

```
bids = [(price, qty), (price, qty), ...]  # top N bid levels, best first
asks = [(price, qty), (price, qty), ...]  # top N ask levels, best first
```

**What you can compute:**

- **Cumulative bid/ask** — sum of quantities across all levels. "How much is being bought/sold at these prices?"
- **Wall detection** — a level with 10x the typical quantity is a "wall" (support or resistance).
- **Bid/ask stack ratio** — `total_bid_qty / total_ask_qty` across all levels. Extreme values indicate imbalance.
- **Depth slope** — how fast quantity falls off as you move away from the top. Steep = thin book, slippery.

**NT subscription:**

```python
# Option A: Partial snapshots (simpler)
self.subscribe_order_book_depth(
    instrument_id=instrument_id,
    depth=10,  # top 10 levels on each side
    interval_ms=100,  # 100ms updates (also 500, 1000)
)

# Option B: Full order book deltas (requires local book management)
self.subscribe_order_book_deltas(
    instrument_id=instrument_id,
    interval_ms=100,
)

# Handler for both:
def on_order_book_deltas(self, book: OrderBookDelta) -> None:
    pass

def on_order_book_depth(self, snapshot: OrderBookSnapshot) -> None:
    # snapshot.bids  -> list of (price, size) tuples
    # snapshot.asks  -> list of (price, size) tuples
    for level in snapshot.bids[:5]:
        price = level.price.as_double()
        size = level.size.as_double()
```

---

## 3. Data Level 2 — Aggregated Windows (Bars / Snapshots)

### 3.1 Kline / OHLCV Bars (`@kline_<interval>`)

**What it is:** A candlestick that summarises all trades in a fixed time window. Binance pushes a new kline every time the window closes.

**Supported intervals:** `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, `1d`, `3d`, `1w`, `1M`

**What's in each bar (NT's `BinanceBar`):**

| Field | Example | Description |
|-------|---------|-------------|
| `bar_type` | `BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL` | Full bar identifier |
| `open` | `64300.00` | Price of the first trade in the window |
| `high` | `64380.00` | Highest trade price in the window |
| `low` | `64290.00` | Lowest trade price in the window |
| `close` | `64350.00` | Price of the last trade in the window |
| `volume` | `1250.500` | Total base asset volume traded (e.g., 1250 BTC) |
| `quote_volume` | `80,437,500` | Total quote asset volume (e.g., 80M USDT) |
| `count` | `8432` | Number of individual trades in the bar |
| `taker_buy_base_volume` | `720.300` | Base volume from aggressive buys. **This is the most informative field.** |
| `taker_buy_quote_volume` | `46,334,000` | Quote volume from aggressive buys |
| `taker_sell_base_volume` | `530.200` | Computed as `volume - taker_buy_base_volume`. Aggressive sell volume. |
| `taker_sell_quote_volume` | `34,103,500` | Computed as `quote_volume - taker_buy_quote_volume` |

**Why `taker_buy_base_volume` is gold:** It separates *aggressive* buying from *passive* buying. A bar where the close went up but taker buys were low means the move was on thin air (weak). A bar where close went up AND taker buys are high means genuine buying pressure.

**Delta and CVD:**

```
delta = taker_buy_base_volume - taker_sell_base_volume
      = 2 * taker_buy_base_volume - volume

CVD = running sum of delta across consecutive bars
```

**NT subscription:**

```python
# Subscribe to bars (what we already do):
self.subscribe_bars(bar_type)

# Handler:
def on_bar(self, bar: Bar) -> None:
    pass  # Standard Bar — has open/high/low/close/volume

# To get the richer BinanceBar with taker volume data,
# you must use BinanceBar type specifically:

from nautilus_trader.adapters.binance import BinanceBar
from nautilus_trader.core.data import Data

self.subscribe_data(
    DataType(BinanceBar, metadata={"bar_type": bar_type})
)

def on_data(self, data: Data) -> None:
    if isinstance(data, BinanceBar):
        # data.open, data.high, data.low, data.close
        # data.volume
        # data.taker_buy_base_volume  -> taker buy base volume
        # data.taker_sell_base_volume -> computed as volume - taker_buy_base

        delta = data.taker_buy_base_volume - data.taker_sell_base_volume
        cvd_ratio = data.taker_buy_base_volume / data.volume if data.volume > 0 else 0.5
```

---

### 3.2 24hr Ticker Stream (`@ticker`)

**What it is:** Rolling 24-hour statistics for a symbol, updated every ~1 second.

**What's in each message:**

| Field | Description |
|-------|-------------|
| `open_price` | Price 24 hours ago |
| `high_price` | Highest price in the last 24h |
| `low_price` | Lowest price in the last 24h |
| `last_price` | Current last traded price |
| `volume` | Total base volume in the last 24h |
| `quote_volume` | Total quote volume in the last 24h |
| `price_change` | `last_price - open_price` |
| `price_change_percent` | `(last_price - open_price) / open_price * 100` |
| `weighted_avg_price` | VWAP over the last 24h |
| `count` | Number of trades in the last 24h |
| `bid_price`, `bid_qty` | Current best bid |
| `ask_price`, `ask_qty` | Current best ask |

**How to use it:** This is a "dashboard at a glance" stream. Good for monitoring but too slow for most signals — the rolling window makes it sluggish. Use bars instead for short-term signals.

**NT subscription:**

```python
from nautilus_trader.adapters.binance import BinanceTicker

self.subscribe_data(
    DataType(BinanceTicker, metadata={"instrument_id": instrument_id})
)

def on_data(self, data: Data) -> None:
    if isinstance(data, BinanceTicker):
        # 24h rolling stats
        vol = data.volume
        vwap = data.weighted_avg_price
        change_pct = data.price_change_percent
```

---

### 3.3 Open Interest Stream (`@openInterest`)

**What it is:** The total number of open contracts (longs + shorts) for a symbol. Pushed every ~5 minutes via WebSocket. Also available via REST at any time.

**What it measures:** How much capital is committed to the market. Rising OI = new money entering. Falling OI = money leaving.

**NT subscription:**

```python
# NOTE: NT's Binance adapter defines the BinanceFuturesOpenInterest data type
# but does NOT handle subscription to the @openInterest stream yet in _subscribe().
# Use REST polling instead (simple and reliable).

# REST polling (recommended — works today):
import httpx

async def get_open_interest(symbol: str) -> float:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": symbol},
        )
        data = resp.json()
        return float(data["openInterest"])

# To poll every 5 minutes in your strategy:
def on_start(self):
    self.clock.set_time_alert(
        "poll_oi",
        self.clock.utc_now() + timedelta(minutes=5),
        timedelta(minutes=5),
    )

def on_event(self, event):
    if isinstance(event, TimeEvent) and event.name == "poll_oi":
        asyncio.ensure_future(self._poll_oi())

async def _poll_oi(self):
    oi = await get_open_interest("BTCUSDT")
    self._oi_history.append((self.clock.utc_now(), oi))
```

**WebSocket approach (requires adapter extension):**
If you want real-time OI updates via WebSocket, you'll need to add handler code to the adapter similar to the liquidation example above. The `@openInterest` stream message format is:

```json
{
  "e": "openInterest",    // event type
  "E": 1625564712345,     // event time
  "s": "BTCUSDT",         // symbol
  "o": "15004.5"          // open interest quantity
}
```

---

## 4. Data Level 3 — Derived / Analytical (Calculated Metrics)

These aren't raw events or simple aggregations — they are **calculated by Binance** from raw data and served via REST endpoints. Most update every minute.

### 4.1 Open Interest History (`GET /fapi/v1/openInterestHist`)

**What it is:** Historical open interest snapshots at regular intervals (5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d).

**Why this matters vs the OI stream:** The stream gives you the *current* value. The history lets you see the *trend*. Is OI rising or falling over the last 24 hours?

**How to use:**

```python
async def get_oi_history(symbol: str, period: str = "1h", limit: int = 24):
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://fapi.binance.com/futures/data/openInterestHist",
            params={"symbol": symbol, "period": period, "limit": limit},
        )
        return resp.json()
        # Each entry: {"symbol":"BTCUSDT","sumOpenInterest":"15004.5",
        #               "sumOpenInterestValue":"964500000","timestamp":"..."}
```

### 4.2 Top Trader Long/Short Ratio (REST)

Two endpoints:

| Endpoint | What it measures |
|----------|-----------------|
| `GET /futures/data/topLongShortAccountRatio` | Ratio of long to short accounts among the top 20% of traders by margin balance |
| `GET /futures/data/topLongShortPositionRatio` | Ratio of long to short position size among the top 20% of traders |

**What each field means:**

```json
{
  "symbol": "BTCUSDT",
  "longShortRatio": "1.43",   // long position % / short position %
  "longAccount": "0.59",      // 59% of top traders are long
  "shortAccount": "0.41"      // 41% of top traders are short
}
```

**How to interpret:** `longShortRatio > 1.5` means top traders are heavily net long — they see a bullish market. Extreme values (> 2.0 or < 0.5) can be contrarian signals: if everyone is already long, who's left to buy?

**NT subscription:** No built-in method. Call the REST endpoint on a timer.

```python
async def get_top_trader_ls(symbol: str, period: str = "1h"):
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://fapi.binance.com/futures/data/topLongShortAccountRatio",
            params={"symbol": symbol, "period": period, "limit": 1},
        )
        data = resp.json()
        if data:
            ratio = float(data[0]["longShortRatio"])
            long_pct = float(data[0]["longAccount"])
            return ratio, long_pct
```

### 4.3 Global Long/Short Ratio (REST)

**Endpoint:** `GET /futures/data/globalLongShortAccountRatio`

Same format as top trader ratio, but computed across **all** traders, not just the top 20%. This is the "retail" version — less informed but still useful for sentiment extremes.

### 4.4 Taker Long/Short Ratio (REST)

**Endpoint:** `GET /futures/data/takerlongshortRatio`

**What it is:** `taker_buy_volume / taker_sell_volume` over a period (5m, 15m, 30m, etc.). This is the **actual aggressive order flow**, not positions or accounts.

**Why it's different from the L/S ratios:**
- L/S ratios measure **what people hold** (positions after they're opened)
- Taker ratio measures **what people are doing right now** (buying or selling aggressively)

A taker ratio > 1.0 for multiple hours means aggressive buying is sustained — strong bullish signal. A sudden spike to 3.0+ means panic buying (potential local top).

### 4.5 Funding Rate History (REST)

**Endpoint:** `GET /fapi/v1/fundingRate`

**What it is:** Historical funding rate payments. Each record shows the rate that was applied at each funding time (every 8 hours for most perpetuals).

**How to use:** Look at the 30-day trend of funding rates:
- Consistently positive (0.01%+) → the market is structurally long → downside risk if sentiment flips
- Consistently negative → the market is structurally short → upside squeeze potential
- Sudden spike to 0.1%+ → extreme positioning → reversal expected

### 4.6 Basis (REST)

**Endpoint:** `GET /futures/data/basis`

**What it is:** The difference between the futures price and the index (spot) price, over time. For perpetuals, this is closely related to funding rate.

**Formula:** `basis = mark_price - index_price` or `basis = last_price - index_price`

**What it tells you:**
- Positive basis (contango) → futures are trading above spot → bullish sentiment
- Negative basis (backwardation) → futures are trading below spot → bearish sentiment
- Basis widening → sentiment strengthening
- Basis narrowing → sentiment weakening

---

## 5. NT Adapter Reference

### 5.1 Complete Data Type and Subscribe Method Matrix

| Data Type | Class | Subscribe Method | WS Stream | REST Endpoint | NT Built-in? |
|-----------|-------|-----------------|-----------|---------------|-------------|
| Trade ticks | `TradeTick` | `subscribe_trade_ticks()` | `@aggTrade` | — | ✅ |
| Mark price / funding | `BinanceFuturesMarkPriceUpdate` | `subscribe_mark_prices()` or `subscribe_data()` | `@markPrice` | `GET /fapi/v1/premiumIndex` | ✅ |
| Quote tick (book ticker) | `QuoteTick` | `subscribe_quote_ticks()` | `@bookTicker` | — | ✅ |
| Order book snapshot | `OrderBookSnapshot` | `subscribe_order_book_depth()` | `@depth5/10/20` | — | ✅ |
| Order book deltas | `OrderBookDelta` | `subscribe_order_book_deltas()` | `@depth` | — | ✅ |
| Bars (standard NT) | `Bar` | `subscribe_bars()` | `@kline_<interval>` | `GET /fapi/v1/klines` | ✅ |
| Bars (with taker volume) | `BinanceBar` | `subscribe_data()` | `@kline_<interval>` | same | ✅ |
| 24hr ticker | `BinanceTicker` | `subscribe_data()` | `@ticker` | `GET /fapi/v1/ticker/24hr` | ✅ |
| Liquidations | `BinanceFuturesLiquidation` | None (manual WS or adapter patch) | `!forceOrder@arr` | — | ❌ Type exists, no adapter handler — requires custom code |
| Open Interest (live) | `BinanceFuturesOpenInterest` | None (REST poll instead) | `@openInterest` | `GET /fapi/v1/openInterest` | ❌ Type exists, no adapter handler — use REST poll |
| OI History | — | — | — | `GET /fapi/v1/openInterestHist` | ❌ Manual REST call |
| Funding History | — | — | — | `GET /fapi/v1/fundingRate` | ❌ Manual REST call |
| Top Trader L/S (accounts) | — | — | — | `GET /futures/data/topLongShortAccountRatio` | ❌ Manual REST call |
| Top Trader L/S (positions) | — | — | — | `GET /futures/data/topLongShortPositionRatio` | ❌ Manual REST call |
| Global L/S ratio | — | — | — | `GET /futures/data/globalLongShortAccountRatio` | ❌ Manual REST call |
| Taker L/S ratio | — | — | — | `GET /futures/data/takerlongshortRatio` | ❌ Manual REST call |
| Basis | — | — | — | `GET /futures/data/basis` | ❌ Manual REST call |

**Legend:**
- ✅ = Full adapter support. Just call the subscribe method, handle the data type.
- ⚠️ = Type is defined and exported by NT but the data client's `_subscribe()` / handler may need a small update. May require subscribing via the raw WebSocket client.
- ❌ = No NT adapter support. Poll the REST endpoint yourself via `httpx`.

### 5.2 Wiring a REST Poll in Your Strategy

For REST-only data, add a timer in your strategy:

```python
def on_start(self):
    # Subscribe to a periodic clock event
    self.clock.set_time_alert(
        "poll_ls_ratio",
        self.clock.utc_now() + timedelta(minutes=1),
        timedelta(minutes=1),  # repeat every minute
    )

def on_event(self, event):
    if isinstance(event, TimeEvent) and event.name == "poll_ls_ratio":
        asyncio.ensure_future(self._poll_data())

async def _poll_data(self):
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://fapi.binance.com/futures/data/topLongShortAccountRatio",
            params={"symbol": "BTCUSDT", "period": "5m", "limit": 1},
        )
        data = resp.json()
        if data:
            ratio = float(data[0]["longShortRatio"])
            self._check_signal(ratio)
```

---

## 6. How to Use Each Data Type for Signals

### 6.1 Aggregate Trades — Cumulative Volume Delta (CVD)

**Concept:** Every trade tick has an aggressor side. Sum up aggressive buy volume minus aggressive sell volume over a window. The result is CVD.

**The signal:**
- **Price going up + CVD going up** → genuine buying pressure, trend is strong.
- **Price going up + CVD flat or down** → divergence. The move is on thin volume. Reversal likely.
- **Price going down + CVD going up** → hidden accumulation. Smart buyers absorbing selling. Bottom likely.

**Implementation:**

```python
class CVDIndicator:
    def __init__(self):
        self.delta = 0.0  # per-bar delta
        self.cvd = 0.0    # running cumulative

    def on_trade(self, tick: TradeTick):
        qty = tick.quantity.as_double()
        if tick.aggressor_side == OrderSide.BUY:
            self.delta += qty
        else:
            self.delta -= qty

    def on_bar_close(self):
        self.cvd += self.delta
        # Store delta, reset for next bar
        bar_delta = self.delta
        self.delta = 0.0
        return bar_delta, self.cvd
```

**Pro tip:** CVD works best as a **divergence indicator** — compare the CVD trend to the price trend on a 15m or 1h chart. If price makes a higher high but CVD makes a lower high, that is a bearish divergence.

### 6.2 Taker Buy/Sell from BinanceBar — Per-Bar Delta

If you don't want to process individual trade ticks (they arrive ~50-200 times per second for BTC), use `BinanceBar.taker_buy_base_volume` instead. It's the same data, pre-aggregated per bar.

**Signal:** Per-bar delta ratio `delta / volume`. When this exceeds +0.3 (30% more aggressive buying than selling) on a green bar, the buying is real. When it's near zero on a green bar, the move is fake.

```python
def on_data(self, data):
    if isinstance(data, BinanceBar):
        buy = data.taker_buy_base_volume
        total = data.volume
        delta_ratio = (buy - (total - buy)) / total  # ranges from -1 to +1

        if data.close > data.open and delta_ratio > 0.3:
            self._strong_buying_bar(data)
        elif data.close < data.open and delta_ratio < -0.3:
            self._strong_selling_bar(data)
```

### 6.3 Mark Price / Funding Rate — Regime Filter

**The concept:** Funding rate tells you which side of the market is crowded. Trade *against* the crowd at extremes.

**The signal logic:**
- Funding rate > 0.01% → longs are paying shorts → market is bullish-crowded. Consider taking short signals only, or reduce long position size.
- Funding rate > 0.05% → extreme bullish crowding. High risk of long squeeze (longs getting liquidated). Look for reversal setups to the downside.
- Funding rate < -0.01% → shorts are paying longs → market is bearish-crowded. Consider taking long signals only.
- Funding rate near zero → neutral. Trade normally with any signal.

**Why this works:** Positive funding means longs are paying to hold their positions. The longer this continues, the more expensive it becomes to stay long → eventual unwind.

**Implementation as a strategy filter:**

```python
class FundingRegimeFilter:
    def __init__(self):
        self.funding_rate = 0.0

    def update(self, mark_price_update):
        self.funding_rate = float(mark_price_update.funding_rate)

    def allow_long(self) -> bool:
        # Don't take longs if funding is too positive (too crowded)
        return self.funding_rate < 0.0005  # 0.05%

    def allow_short(self) -> bool:
        # Don't take shorts if funding is too negative (too crowded)
        return self.funding_rate > -0.0005  # -0.05%
```

### 6.4 Liquidations — Wick Hunting / Squeeze Detection

**The concept:** Large liquidations create massive market orders that push price beyond fair value, creating wicks. After the liquidation cascade exhausts, price snaps back — filling the wick.

**The signal:**
1. A cluster of SELL liquidations (longs liquidated) → price drops sharply → this creates an FVG below the wick → look for buy entries.
2. A cluster of BUY liquidations (shorts liquidated) → price spikes up → creates an FVG above the wick → look for sell entries.
3. **Cascade detection:** When `accumulated_qty` exceeds 10x the typical trade size, a cascade is underway. Wait for it to stop, then enter the reversal.

**How to quantify:**

```python
class LiquidationMonitor:
    def __init__(self, threshold_qty=1.0):
        self.threshold = threshold_qty
        self.recent_liqs = []  # list of (side, price, qty, timestamp)

    def on_liquidation(self, liq):
        qty = float(liq.accumulated_qty)
        if qty < self.threshold:
            return  # Small liquidation, ignore

        side = "long_liq" if liq.side == OrderSide.SELL else "short_liq"
        self.recent_liqs.append((side, float(liq.price), qty, liq.ts_event))

        # Keep last 5 minutes
        cutoff = liq.ts_event - 300_000_000_000  # 5 min in nanoseconds
        self.recent_liqs = [x for x in self.recent_liqs if x[3] > cutoff]

        total_long_liq = sum(x[2] for x in self.recent_liqs if x[0] == "long_liq")
        total_short_liq = sum(x[2] for x in self.recent_liqs if x[0] == "short_liq")

        if total_long_liq > self.threshold * 5:
            self._alert("Large long liquidation cascade!")
        if total_short_liq > self.threshold * 5:
            self._alert("Large short liquidation cascade!")
```

### 6.5 Book Ticker — Order Flow Pressure

**The concept:** The best bid and ask sizes tell you where the "line in the sand" is. When bid size grows while price stays flat, buyers are accumulating (support forming). When ask size grows while price stays flat, sellers are distributing (resistance forming).

**Signals:**
- **Ask wall:** ask_qty >> bid_qty, price near the ask wall → unlikely to break through. Short bias.
- **Bid wall:** bid_qty >> ask_qty, price near the bid wall → unlikely to break down. Long bias.
- **Spoofing:** A large order appears on one side, stays for a few seconds, then disappears as price moves the other way. Someone is faking support/resistance.

**Quick check:**

```python
def on_quote_tick(self, tick):
    bid = tick.bid.as_double()
    ask = tick.ask.as_double()
    bid_size = tick.bid_size.as_double()
    ask_size = tick.ask_size.as_double()
    ratio = bid_size / ask_size if ask_size > 0 else 1.0

    if ratio > 3.0:
        # 3x more bids than asks — strong support
        self._bias = "long"
    elif ratio < 0.33:
        # 3x more asks than bids — strong resistance
        self._bias = "short"
```

### 6.6 Open Interest — Trend Confirmation

**The concept:** Open interest tracks the total number of open positions. The combination of price direction and OI direction tells you whether a trend is real or fake.

**Price + OI matrix:**

| Price | OI | Meaning |
|-------|----|---------|
| ↑ | ↑ | **Trend confirmed.** New money entering. Trend is strong. |
| ↑ | ↓ | **Trend weakening.** Old positions closing. Reversal incoming. |
| ↓ | ↑ | **Downtrend confirmed.** New shorts entering. |
| ↓ | ↓ | **Downtrend weakening.** Shorts covering. Reversal incoming. |
| → | ↑ | **Accumulation/distribution.** Big money positioning. Big move imminent. |
| → | ↓ | **Indecision.** Traders leaving. Volatility decreasing. |

**Implementation note:** OI changes slowly. A 1-minute bar comparison is fine. Compare the OI at bar close vs. the OI 24 bars ago (24h lookback if using 1h bars).

### 6.7 Top Trader L/S Ratio — Crowded Trade Detection

**The concept:** Top traders (top 20% by margin balance) are generally more informed than retail. When they are extremely one-sided, it's a signal.

**Interpretation:**
- `longShortRatio = 1.0` → balanced
- `longShortRatio = 2.0` → top traders have 2x more long than short position size
- `longShortRatio = 0.5` → top traders have 2x more short than long

**Signal rule:**
- Ratio > 1.5 + price making new highs → trend with the smart money
- Ratio > 2.5 → **extreme** — the smart money is fully positioned. Reversal is near.
- Ratio < 0.5 → **extreme bearish** — smart money is all short. Reversal to the upside is near.

**Why this works:** Even smart money can't buy forever. When everyone who wants to be long is already long, there's no one left to push price higher. The market becomes "top-heavy."

---

## 7. Putting It Together — Which Data for Which Strategy

### Current System (MS + FVG)

We currently use:
- **Level 2:** 1m and 1h bars (`PriceType.LAST`, no taker volume) — live
- **Level 3:** CVD (from REST klines taker volume) — backtest only

### Suggested Add-Ons (Effort vs. Value)

| Data | Effort | Value | Best For |
|------|--------|-------|----------|
| `BinanceBar` (taker vol) | Low (NT already supports) | High | Replace standard Bar with BinanceBar → per-bar CVD in live |
| `BinanceFuturesMarkPriceUpdate` | Low (NT supports) | Medium | Funding rate as regime filter for MS/FVG |
| `BinanceFuturesLiquidation` | Medium (adapter patch needed, or manual WS) | High | Detect wicks → FVG entries, exit before liquidations hit your SL |
| `BinanceFuturesOpenInterest` | Low (REST poll every 5min) | Medium | Confirm trend strength |
| REST: Top Trader L/S ratio | Low (httpx poll every 5-15 min) | Medium | Contrarian signal at extremes |
| `subscribe_trade_ticks()` | Medium (process 50-200 msg/s) | High | Real-time CVD, divergence detection |
| `subscribe_quote_ticks()` | Medium | Medium | Book pressure intra-bar |
| REST: Taker L/S ratio | Low | Low-Medium | Alternative to CVD without tick subscription |

### Recommended Integration Order

**Phase 1 (this week, minimal code):**
1. Switch from `Bar` to `BinanceBar` in strategy data subscription → get per-bar CVD for free
2. Subscribe to `BinanceFuturesMarkPriceUpdate` → add funding regime filter to FVG and MS strategies
3. Add REST polling for Open Interest (5 lines of code) → trend confirmation

**Phase 2 (soon):**
4. Add REST polling for Top Trader L/S ratio → extreme warning system
5. Extend adapter for `BinanceFuturesLiquidation` → detect wicks and squeezes near your positions

**Phase 3 (when you need edge):**
6. Subscribe to raw trade ticks → build real-time CVD with divergence detection
7. Use CVD divergences as entry triggers alongside FVG patterns

### Signal Stacking Example

The most robust signals combine multiple data levels:

```
SIGNAL TO GO LONG (FVG found on 1m chart):

Level 1 filter:
  ✅ Funding rate < 0.01% (not over-crowded long)
  ✅ Recent long liquidations detected (weak hands flushed)
  ✅ Book ticker shows bid wall forming (real support)

Level 2 filter:
  ✅ Bar delta positive (taker_buy >> taker_sell on entry bar)
  ✅ OI rising (new money entering)

Level 3 filter:
  ✅ Top trader ratio < 1.5 (not over-extended)

→ Trade with higher confidence, wider SL, full position size
```

Without filters:
```
SIGNAL TO GO LONG (FVG found on 1m chart):

Level 1:
  ❌ Funding rate = 0.03% (very crowded long)
  ❌ No recent liquidations

Level 2:
  ❌ Bar delta near zero (move not backed by real buying)

→ Skip the trade, or reduce size and tighten SL
```

---

> **Final thought:** Data without context is just noise. A liquidation event is meaningless until you check if it's part of a cascade. A funding rate spike is just a number until you check if it's at an extreme vs. its 30-day range. The best traders don't just read the data — they read the *relationships between data types*. Use at least two independent data sources to confirm every signal.
