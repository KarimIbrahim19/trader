from nautilus_trader.persistence.catalog import ParquetDataCatalog
import pandas as pd

cat = ParquetDataCatalog("./catalog")

# List what's in the catalog
print(cat.instruments())

# Load 1H bars and inspect
bars = cat.bars(["BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"])
print(f"Total 1H bars: {len(bars)}")
print(f"First bar: {bars[0]}")
print(f"Last bar:  {bars[-1]}")

# Quick sanity check on a known date
# (Bitcoin hit ~$69k ATH in November 2021)
from nautilus_trader.core.datetime import dt_to_unix_nanos
start = dt_to_unix_nanos(pd.Timestamp("2025-11-08", tz="UTC"))
end   = dt_to_unix_nanos(pd.Timestamp("2025-11-10", tz="UTC"))
ath_bars = [b for b in bars if start <= b.ts_event <= end]
print(f"\nNov 8-10 2025 bars: {len(ath_bars)}")
for b in ath_bars[:3]:
    print(f"  {b}")
