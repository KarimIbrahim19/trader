from nautilus_trader.persistence.catalog import ParquetDataCatalog
import pandas as pd

cat = ParquetDataCatalog("./catalog")

# Check all timeframes
for bt in [
    "BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL",
    "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
    "BTCUSDT-PERP.BINANCE-4-HOUR-LAST-EXTERNAL",
    "BTCUSDT-PERP.BINANCE-1-DAY-LAST-EXTERNAL",
]:
    bars = cat.bars(bar_types=[bt])
    print(f"{bt.split('BINANCE-')[1]:<30} {len(bars):>6} bars")

# Check CVD files
from pathlib import Path
for f in sorted(Path("./catalog/cvd").glob("*.parquet")):
    df = pd.read_parquet(f)
    print(f"CVD {f.stem:<6} {len(df):>6} rows  cols: {list(df.columns)}")