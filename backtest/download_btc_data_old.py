#!/usr/bin/env python3
"""
download_btc_data.py
──────────────────────────────────────────────────────────────────────
One-time script to download BTCUSDT-PERP historical klines from Binance
Futures and store them in a NautilusTrader ParquetDataCatalog.

Run this once before your first backtest, then again whenever you want
to extend the date range.

Extra dependency:
    uv pip install requests

Usage:
    python download_btc_data.py
"""

import time
from decimal import Decimal
from pathlib import Path

import pandas as pd
import requests

from nautilus_trader.model.currencies import BTC, USDT
from nautilus_trader.model.data import BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import BarDataWrangler


# ── Configuration ──────────────────────────────────────────────────────────────
CATALOG_PATH = Path("./catalog_test")        # Where all data will be stored locally
SYMBOL       = "BTCUSDT"               # Binance native symbol
NT_SYMBOL    = "BTCUSDT-PERP"          # NautilusTrader appends -PERP to perpetuals
VENUE        = "BINANCE"
BASE_URL     = "https://fapi.binance.com"  # Binance USDT-margined futures base URL

# Timeframes to download — add or remove as needed.
# Tuple layout: (binance_interval, BarAggregation, step)
TIMEFRAMES = [
    ("5m",  BarAggregation.MINUTE, 5),
    ("15m", BarAggregation.MINUTE, 15),
    ("1h",  BarAggregation.HOUR, 1),
    ("4h",  BarAggregation.HOUR, 4),
    ("1d",  BarAggregation.DAY,  1),
]

START = "2026-06-01"    # adjust this to how far back you want history
END   = "2026-06-24"    # adjust to your desired end date


# ── Step 1: Build the BTCUSDT-PERP instrument ─────────────────────────────────
def build_instrument() -> CryptoPerpetual:
    """
    Fetch live instrument specs from Binance /exchangeInfo (no API key needed)
    and construct a NautilusTrader CryptoPerpetual instrument object.

    NautilusTrader needs this to know tick size, lot size, and decimal
    precision — it uses them when converting raw floats into its internal
    Price and Quantity fixed-precision types.
    """
    print("→ Fetching instrument specs from Binance...")
    resp = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo", timeout=30)
    resp.raise_for_status()

    sym_info = next(
        s for s in resp.json()["symbols"] if s["symbol"] == SYMBOL
    )
    filters   = {f["filterType"]: f for f in sym_info["filters"]}
    tick_size = filters["PRICE_FILTER"]["tickSize"]   # e.g. "0.10"
    step_size = filters["LOT_SIZE"]["stepSize"]       # e.g. "0.001"
    min_qty   = filters["LOT_SIZE"]["minQty"]
    max_qty   = filters["LOT_SIZE"]["maxQty"]

    instrument = CryptoPerpetual(
        instrument_id       = InstrumentId(Symbol(NT_SYMBOL), Venue(VENUE)),
        raw_symbol          = Symbol(SYMBOL),
        base_currency       = BTC,
        quote_currency      = USDT,
        settlement_currency = USDT,
        is_inverse          = False,
        price_precision     = sym_info["pricePrecision"],    # e.g. 2
        size_precision      = sym_info["quantityPrecision"], # e.g. 3
        price_increment     = Price.from_str(tick_size),
        size_increment      = Quantity.from_str(step_size),
        max_quantity        = Quantity.from_str(max_qty),
        min_quantity        = Quantity.from_str(min_qty),
        max_notional        = None,
        min_notional        = None,
        max_price           = None,
        min_price           = Price.from_str(tick_size),
        margin_init         = Decimal("0.01"),
        margin_maint        = Decimal("0.005"),
        maker_fee           = Decimal("0.0002"),
        taker_fee           = Decimal("0.0004"),
        ts_event            = 0,
        ts_init             = 0,
    )

    print(
        f"  ✓ {instrument.id}  "
        f"price_precision={instrument.price_precision}  "
        f"size_precision={instrument.size_precision}  "
        f"tick={tick_size}  step={step_size}"
    )
    return instrument


# ── Step 2: Download klines from Binance Futures REST API ─────────────────────
def download_klines(interval: str, start: str, end: str) -> pd.DataFrame:
    """
    Pull OHLCV candles in paginated 1 000-bar chunks (Binance's max per request).
    No API key is required — klines are a public endpoint.

    Returns a DataFrame with a UTC DatetimeIndex and float columns:
        open | high | low | close | volume
    """
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms   = int(pd.Timestamp(end,   tz="UTC").timestamp() * 1000)
    rows     = []

    print(f"→ Downloading {SYMBOL} {interval}  ({start} → {end})")

    while start_ms < end_ms:
        resp = requests.get(
            f"{BASE_URL}/fapi/v1/klines",
            params={
                "symbol":    SYMBOL,
                "interval":  interval,
                "startTime": start_ms,
                "endTime":   end_ms,
                "limit":     1000,      # max per Binance API call
            },
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()

        if not batch:
            break

        rows.extend(batch)
        # Advance the cursor past the last candle's open time
        start_ms = batch[-1][0] + 1

        print(
            f"  fetched {len(rows):>6} bars  "
            f"up to {pd.Timestamp(start_ms, unit='ms', tz='UTC').strftime('%Y-%m-%d %H:%M')}"
        )
        time.sleep(0.12)    # stay well within Binance's rate limits

    if not rows:
        raise RuntimeError(f"No klines returned for interval={interval}")

    df = pd.DataFrame(rows, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = (
        df.set_index("timestamp")
          [["open", "high", "low", "close", "volume"]]
          .astype(float)
    )
    # Drop the last bar if it's still forming (open_time >= END)
    df = df[df.index < pd.Timestamp(end, tz="UTC")]

    print(
        f"  ✓ {len(df)} bars total  "
        f"({df.index[0].date()} → {df.index[-1].date()})"
    )
    return df


# ── Step 3: Convert to NautilusTrader Bars and write to catalog ───────────────
def save_to_catalog(
    df: pd.DataFrame,
    instrument: CryptoPerpetual,
    aggregation: BarAggregation,
    step: int,
) -> None:
    """
    1. Define the BarType (e.g. BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL)
    2. Run BarDataWrangler.process() to turn the DataFrame into Bar objects
    3. Write to the ParquetDataCatalog — NautilusTrader handles partitioning
    """
    bar_type = BarType(
        instrument_id      = instrument.id,
        bar_spec           = BarSpecification(
            step           = step,
            aggregation    = aggregation,
            price_type     = PriceType.LAST,
        ),
        aggregation_source = AggregationSource.EXTERNAL,
    )

    wrangler = BarDataWrangler(bar_type=bar_type, instrument=instrument)
    bars     = wrangler.process(df)

    catalog = ParquetDataCatalog(CATALOG_PATH)
    catalog.write_data(bars)

    print(f"  ✓ Saved  →  {bar_type}  ({len(bars)} bars)")


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    CATALOG_PATH.mkdir(parents=True, exist_ok=True)

    # Fetch and persist the instrument definition first.
    # The backtest engine needs it to resolve instrument_id references.
    instrument = build_instrument()
    catalog = ParquetDataCatalog(CATALOG_PATH)
    catalog.write_data([instrument])
    print()

    # Download and store each timeframe
    for interval_str, aggregation, step in TIMEFRAMES:
        df = download_klines(interval_str, START, END)
        save_to_catalog(df, instrument, aggregation, step)
        print()

    print("══════════════════════════════════════════════")
    print("  Catalog ready. Run your backtest next.")
    print(f"  Path: {CATALOG_PATH.resolve()}")
    print()
    print("  To verify, in Python:")
    print("    from nautilus_trader.persistence.catalog import ParquetDataCatalog")
    print('    cat = ParquetDataCatalog("./catalog")')
    print('    bars = cat.bars(["BTCUSDT-PERP.BINANCE"])')
    print("    print(len(bars), bars[0])")
    print("══════════════════════════════════════════════")


if __name__ == "__main__":
    main()
