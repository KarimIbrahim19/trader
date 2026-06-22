#!/usr/bin/env python3
"""
download_btc_data.py
──────────────────────────────────────────────────────────────────────
Downloads BTCUSDT-PERP historical klines from Binance's official
Public Data Repository (data.binance.vision) — no API key, no rate
limits, entire months in a single zip download.

What gets stored
  • OHLCV bars       → NautilusTrader ParquetDataCatalog  (backtest engine reads this)
  • Taker buy volume → catalog/cvd/{interval}.parquet     (CVD module reads this)

The taker_buy_base column is the actual on-exchange taker buy volume,
more accurate than TradingView's requestVolumeDelta() approximation.

Usage:
    python download_btc_data.py
"""

import io
import zipfile
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
CATALOG_PATH  = Path("./catalog_24")
CVD_PATH      = CATALOG_PATH / "cvd"          # taker buy volume lives here

SYMBOL        = "BTCUSDT"
NT_SYMBOL     = "BTCUSDT-PERP"
VENUE         = "BINANCE"

# Binance Public Data Repository — USDT-M Futures
# Spot would be: https://data.binance.vision/data/spot/monthly/klines/
REPO_BASE     = "https://data.binance.vision/data/futures/um/monthly/klines"
FUTURES_INFO  = "https://fapi.binance.com/fapi/v1/exchangeInfo"

# Date range — (year, month) tuples, inclusive
START = (2024, 1)
END   = (2026, 6)

# Timeframes to download
# (binance_interval, BarAggregation, step)
TIMEFRAMES = [
    ("5m",  BarAggregation.MINUTE, 5),
    ("15m", BarAggregation.MINUTE, 15),
    ("1h",  BarAggregation.HOUR,   1),
    ("4h",  BarAggregation.HOUR,   4),
    ("1d",  BarAggregation.DAY,    1),
]

# Kline CSV column names (Binance futures format)
KLINE_COLS = [
    "timestamp",            # 0  open time (ms)
    "open",                 # 1
    "high",                 # 2
    "low",                  # 3
    "close",                # 4
    "volume",               # 5  base asset volume
    "close_time",           # 6
    "quote_volume",         # 7
    "trades",               # 8
    "taker_buy_base",       # 9  ← taker buy volume (used for CVD)
    "taker_buy_quote",      # 10
    "ignore",               # 11
]


# ── Helpers ────────────────────────────────────────────────────────────────────
def month_range(start: tuple, end: tuple):
    """Yield (year, month) tuples from start to end inclusive."""
    y, m = start
    ey, em = end
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


# ── Step 1: Build instrument from Binance Futures exchange info ────────────────
def build_instrument() -> CryptoPerpetual:
    """
    Fetch live instrument specs from the Futures API (public, no auth).
    fapi.binance.com = Futures API  (BTCUSDT-PERP specs)
    api.binance.com  = Spot API     (different prices & precision — wrong for us)
    """
    print("→ Fetching instrument specs from Binance Futures API…")
    resp = requests.get(FUTURES_INFO, timeout=30)
    resp.raise_for_status()

    sym_info = next(s for s in resp.json()["symbols"] if s["symbol"] == SYMBOL)
    filters  = {f["filterType"]: f for f in sym_info["filters"]}

    tick_size = filters["PRICE_FILTER"]["tickSize"]
    step_size = filters["LOT_SIZE"]["stepSize"]
    min_qty   = filters["LOT_SIZE"]["minQty"]
    max_qty   = filters["LOT_SIZE"]["maxQty"]

    instrument = CryptoPerpetual(
        instrument_id       = InstrumentId(Symbol(NT_SYMBOL), Venue(VENUE)),
        raw_symbol          = Symbol(SYMBOL),
        base_currency       = BTC,
        quote_currency      = USDT,
        settlement_currency = USDT,
        is_inverse          = False,
        price_precision     = sym_info["pricePrecision"],
        size_precision      = sym_info["quantityPrecision"],
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
        f"tick={tick_size}  step={step_size}"
    )
    return instrument


# ── Step 2: Download one monthly zip from data.binance.vision ─────────────────
def download_month(interval: str, year: int, month: int) -> pd.DataFrame | None:
    """
    Download and parse one monthly klines zip from the public repository.
    Returns a DataFrame with timestamp index + OHLCV + taker_buy_base.
    Returns None if the file doesn't exist yet (future month).
    """
    filename = f"{SYMBOL}-{interval}-{year}-{month:02d}.zip"
    url      = f"{REPO_BASE}/{SYMBOL}/{interval}/{filename}"

    resp = requests.get(url, timeout=60)

    if resp.status_code == 404:
        return None   # month not published yet

    resp.raise_for_status()

    # Unzip in memory — newer Binance repo files include a header row
    # ("open_time,open,high,…"). Detect by checking if first byte is a digit
    # (timestamp) or a letter (header), then skip the row when present.
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_name  = zf.namelist()[0]
        raw_bytes = zf.open(csv_name).read()

    has_header = not raw_bytes.lstrip()[:1].isdigit()
    df = pd.read_csv(
        io.BytesIO(raw_bytes),
        header   = None,
        names    = KLINE_COLS,
        skiprows = 1 if has_header else 0,
    )

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    df = df[["open", "high", "low", "close", "volume", "taker_buy_base"]].astype(float)

    return df


# ── Step 3: Download all months and concatenate ────────────────────────────────
def download_all(interval: str) -> pd.DataFrame:
    """Download every monthly file for the configured date range."""
    frames = []
    print(f"→ Downloading {SYMBOL} {interval} from data.binance.vision…")

    for year, month in month_range(START, END):
        df = download_month(interval, year, month)
        if df is None:
            print(f"  ⚠ {year}-{month:02d} not available yet, stopping.")
            break
        frames.append(df)
        print(f"  ✓ {year}-{month:02d}  ({len(df)} bars)")

    if not frames:
        raise RuntimeError(f"No data downloaded for {interval}")

    combined = pd.concat(frames).sort_index()
    combined = combined[~combined.index.duplicated(keep="first")]
    print(f"  Total: {len(combined):,} bars  ({combined.index[0].date()} → {combined.index[-1].date()})")
    return combined


# ── Step 4a: Write OHLCV bars to NautilusTrader catalog ───────────────────────
def save_bars(df: pd.DataFrame, instrument: CryptoPerpetual,
              aggregation: BarAggregation, step: int) -> None:
    """Convert OHLCV DataFrame to Bar objects and write to catalog."""
    bar_type = BarType(
        instrument_id      = instrument.id,
        bar_spec           = BarSpecification(
            step           = step,
            aggregation    = aggregation,
            price_type     = PriceType.LAST,
        ),
        aggregation_source = AggregationSource.EXTERNAL,
    )

    ohlcv = df[["open", "high", "low", "close", "volume"]]
    wrangler = BarDataWrangler(bar_type=bar_type, instrument=instrument)
    bars     = wrangler.process(ohlcv)

    catalog = ParquetDataCatalog(CATALOG_PATH)
    catalog.write_data(bars)
    print(f"  ✓ Bars saved  →  {bar_type}  ({len(bars):,} bars)")


# ── Step 4b: Write taker buy volume to CVD parquet file ───────────────────────
def save_cvd(df: pd.DataFrame, interval: str) -> None:
    """
    Save taker_buy_base alongside total volume as a plain parquet file.

    Columns stored:
      volume          — total bar volume (base asset)
      taker_buy_base  — taker buy volume (base asset)
      taker_sell_base — derived: volume - taker_buy_base
      cvd_delta       — per-bar delta: taker_buy_base - taker_sell_base

    The CVD module loads this file and computes cumulative delta from cvd_delta.
    """
    CVD_PATH.mkdir(parents=True, exist_ok=True)

    cvd = df[["volume", "taker_buy_base"]].copy()
    cvd["taker_sell_base"] = cvd["volume"] - cvd["taker_buy_base"]
    cvd["cvd_delta"]       = cvd["taker_buy_base"] - cvd["taker_sell_base"]

    out = CVD_PATH / f"{interval}.parquet"
    cvd.to_parquet(out)
    print(f"  ✓ CVD data saved  →  {out}  ({len(cvd):,} rows)")


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    CATALOG_PATH.mkdir(parents=True, exist_ok=True)

    # Build and persist instrument once
    instrument = build_instrument()
    catalog    = ParquetDataCatalog(CATALOG_PATH)
    catalog.write_data([instrument])
    print()

    for interval_str, aggregation, step in TIMEFRAMES:
        df = download_all(interval_str)
        save_bars(df, instrument, aggregation, step)
        save_cvd(df, interval_str)
        print()

    print("══════════════════════════════════════════════════════════")
    print("  Done. Catalog layout:")
    print(f"  {CATALOG_PATH}/data/bar/          ← NautilusTrader bars")
    print(f"  {CATALOG_PATH}/cvd/               ← taker buy volume for CVD")
    print()
    print("  Verify with:")
    print("    from nautilus_trader.persistence.catalog import ParquetDataCatalog")
    print('    cat = ParquetDataCatalog("./catalog")')
    print('    bars = cat.bars(bar_types=["BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL"])')
    print("    print(len(bars), bars[0])")
    print("══════════════════════════════════════════════════════════")


if __name__ == "__main__":
    main()
