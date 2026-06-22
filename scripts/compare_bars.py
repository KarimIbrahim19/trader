"""
scripts/compare_bars.py
──────────────────────────────────────────────────────────────────────
Stage 2 validation tool.

Compares bars captured from the live Binance WebSocket (written to
state/live_bars_YYYYMMDD.csv by DataFeedValidator) against bars in
the ParquetDataCatalog for the same period.

Run this after the system has been running for at least 1 hour to
have enough bars for a meaningful comparison.

Usage:
    cd ~/live_trader
    python scripts/compare_bars.py
    python scripts/compare_bars.py --csv state/live_bars_20260620.csv
    python scripts/compare_bars.py --catalog ~/data/catalog_24 --timeframe 1h
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare live bars against catalog")
    p.add_argument("--csv", default=None,
                   help="Path to live_bars CSV (default: latest in state/)")
    p.add_argument("--catalog", default=None,
                   help="Path to ParquetDataCatalog (default: ~/data/catalog_24)")
    p.add_argument("--timeframe", default="15m",
                   choices=["15m", "1h", "4h"],
                   help="Timeframe to compare (default: 15m)")
    p.add_argument("--max-rows", type=int, default=100,
                   help="Max rows to show in the diff table (default: 100)")
    return p.parse_args()


def find_latest_csv(state_dir: Path) -> Path | None:
    csvs = sorted(state_dir.glob("live_bars_*.csv"))
    return csvs[-1] if csvs else None


def load_live_bars(csv_path: Path, timeframe: str) -> pd.DataFrame:
    """Load and filter the live bar CSV for a specific timeframe."""
    df = pd.read_csv(csv_path)

    # Filter to the requested timeframe
    tf_filter = f"-{timeframe.upper().replace('M', '-MINUTE').replace('H', '-HOUR')}"
    # Normalise: "15m" → "15-MINUTE", "1h" → "1-HOUR"
    def tf_to_nt(tf: str) -> str:
        tf = tf.lower()
        if tf.endswith("m"):
            return f"{tf[:-1]}-MINUTE"
        if tf.endswith("h"):
            return f"{tf[:-1]}-HOUR"
        if tf.endswith("d"):
            return f"{tf[:-1]}-DAY"
        return tf

    nt_tf = tf_to_nt(timeframe)
    df = df[df["bar_type"].str.contains(nt_tf, case=False)]

    if df.empty:
        return df

    # Parse bar open time from nanoseconds to UTC timestamp
    df["bar_time"] = pd.to_datetime(df["bar_open_ts_ns"], unit="ns", utc=True)
    df = df.set_index("bar_time").sort_index()

    return df


def load_catalog_bars(catalog_path: Path, timeframe: str) -> pd.DataFrame:
    """Load bars from the NautilusTrader ParquetDataCatalog."""
    try:
        from nautilus_trader.persistence.catalog import ParquetDataCatalog
    except ImportError:
        print("ERROR: nautilus_trader not installed in this environment.")
        sys.exit(1)

    tf_map = {
        "15m": "BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL",
        "1h":  "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        "4h":  "BTCUSDT-PERP.BINANCE-4-HOUR-LAST-EXTERNAL",
    }
    bar_type_str = tf_map.get(timeframe.lower())
    if not bar_type_str:
        print(f"ERROR: Unknown timeframe '{timeframe}'")
        sys.exit(1)

    catalog = ParquetDataCatalog(str(catalog_path))
    bars    = catalog.bars(bar_types=[bar_type_str])

    if not bars:
        print(f"WARNING: No catalog bars found for {bar_type_str}")
        return pd.DataFrame()

    rows = []
    for b in bars:
        rows.append({
            "bar_time": pd.Timestamp(b.ts_init, unit="ns", tz="UTC"),
            "open":     b.open.as_double(),
            "high":     b.high.as_double(),
            "low":      b.low.as_double(),
            "close":    b.close.as_double(),
            "volume":   b.volume.as_double(),
        })

    df = pd.DataFrame(rows).set_index("bar_time").sort_index()
    return df


def sep(title: str = "") -> None:
    width = 72
    if title:
        pad = (width - len(title) - 2) // 2
        print("═" * pad + f" {title} " + "═" * pad)
    else:
        print("═" * width)


def main() -> None:
    args    = parse_args()
    project = Path(__file__).parent.parent
    state   = project / "state"

    # ── Find live CSV ──────────────────────────────────────────────────
    csv_path = Path(args.csv) if args.csv else find_latest_csv(state)
    if not csv_path or not csv_path.exists():
        print(f"\nERROR: No live bar CSV found in {state}/")
        print("Run main.py first for at least 1 hour, then re-run this script.")
        sys.exit(1)

    # ── Find catalog ───────────────────────────────────────────────────
    if args.catalog:
        catalog_path = Path(args.catalog)
    else:
        catalog_path = Path.home() / "data" / "catalog_24"
    if not catalog_path.exists():
        print(f"\nERROR: Catalog not found at {catalog_path}")
        print("Pass --catalog <path> to specify the catalog location.")
        sys.exit(1)

    tf = args.timeframe

    print(f"\nComparing live bars vs catalog  [{tf}]")
    print(f"  Live CSV  : {csv_path}")
    print(f"  Catalog   : {catalog_path}")
    print()

    # ── Load data ──────────────────────────────────────────────────────
    live = load_live_bars(csv_path, tf)
    cat  = load_catalog_bars(catalog_path, tf)

    if live.empty:
        print(f"No live bars found for timeframe {tf} in {csv_path.name}")
        print("The system may not have been running long enough for this TF.")
        sys.exit(0)

    print(f"Live bars loaded  : {len(live):,}")
    print(f"Catalog bars      : {len(cat):,}")

    # ── Find overlapping period ────────────────────────────────────────
    live_start = live.index[0]
    live_end   = live.index[-1]

    cat_overlap = cat[
        (cat.index >= live_start) &
        (cat.index <= live_end)
    ]

    if cat_overlap.empty:
        print(f"\nNo catalog bars overlap with the live period:")
        print(f"  Live: {live_start} → {live_end}")
        print(f"  Catalog covers data up to May 2026.")
        print("  If the current date is after the catalog end, this is expected.")
        print("  The catalog only covers historical data — live bars are the ground truth.")
        sys.exit(0)

    sep("TIMING ANALYSIS")

    # Delay distribution
    if "delay_ms" in live.columns:
        delays = live["delay_ms"]
        print(f"\n  Bar arrival delay after expected close time:")
        print(f"  {'Min':>8}  {'P25':>8}  {'Median':>8}  {'P75':>8}  {'Max':>8}  {'Mean':>8}")
        print(f"  {delays.min():>7.0f}ms"
              f"  {delays.quantile(0.25):>7.0f}ms"
              f"  {delays.median():>7.0f}ms"
              f"  {delays.quantile(0.75):>7.0f}ms"
              f"  {delays.max():>7.0f}ms"
              f"  {delays.mean():>7.0f}ms")

        slow = delays[delays > 5000]
        if not slow.empty:
            print(f"\n  ⚠  {len(slow)} bars took >5 seconds to arrive:")
            for ts, d in slow.items():
                print(f"     {ts}  {d:.0f}ms")
        else:
            print(f"\n  ✓  All bars arrived within 5 seconds")

    sep("PRICE COMPARISON")

    # Align live bars with catalog on timestamp
    joined = live[["open", "high", "low", "close", "volume"]].join(
        cat_overlap[["open", "high", "low", "close", "volume"]],
        how="inner",
        lsuffix="_live",
        rsuffix="_catalog",
    )

    if joined.empty:
        print("No matching timestamps between live and catalog data.")
        sys.exit(0)

    n_matched = len(joined)
    print(f"\n  Matched bars: {n_matched}")

    # Compute price differences
    for col in ["open", "high", "low", "close"]:
        diff = (joined[f"{col}_live"] - joined[f"{col}_catalog"]).abs()
        pct  = (diff / joined[f"{col}_catalog"] * 100)
        mismatches = (diff > 0.5)   # >$0.50 difference
        print(
            f"\n  {col.upper():<6}  "
            f"max_diff=${diff.max():.2f}  "
            f"mean_diff=${diff.mean():.4f}  "
            f"max_pct={pct.max():.4f}%  "
            f"mismatches(>$0.50)={mismatches.sum()}"
        )

    # Volume comparison
    vol_diff = (joined["volume_live"] - joined["volume_catalog"]).abs()
    vol_pct  = (vol_diff / (joined["volume_catalog"] + 1e-9) * 100)
    print(
        f"\n  VOLUME  max_diff={vol_diff.max():.4f}  "
        f"mean_diff={vol_diff.mean():.6f}  "
        f"max_pct={vol_pct.max():.2f}%"
    )

    sep("VERDICT")

    close_diff    = (joined["close_live"] - joined["close_catalog"]).abs()
    max_close_err = close_diff.max()
    big_diff      = (close_diff > 1.0).sum()  # >$1 error

    print()
    if max_close_err < 0.5:
        print(f"  ✅ PASS — Max close price diff: ${max_close_err:.2f}")
        print(f"       Live data matches catalog within noise tolerance.")
        print(f"       Stage 2 validated — ready for Stage 3 (strategy port).")
    elif max_close_err < 5.0:
        print(f"  ⚠  WARN — Max close price diff: ${max_close_err:.2f}")
        print(f"       Small discrepancies detected ({big_diff} bars > $1).")
        print(f"       Check for timezone or rounding differences before proceeding.")
    else:
        print(f"  ❌ FAIL — Max close price diff: ${max_close_err:.2f}")
        print(f"       Significant price mismatch — check bar type / instrument config.")
    print()

    # Show sample of largest diffs
    if n_matched > 0:
        joined["close_diff"] = (
            joined["close_live"] - joined["close_catalog"]
        ).abs()
        worst = joined.nlargest(min(5, n_matched), "close_diff")
        if not worst.empty and worst["close_diff"].max() > 0.01:
            print("  Largest close-price differences:")
            print(f"  {'Time':>30}  {'Live':>12}  {'Catalog':>12}  {'Diff':>8}")
            for ts, row in worst.iterrows():
                print(
                    f"  {str(ts):>30}  "
                    f"{row['close_live']:>12.2f}  "
                    f"{row['close_catalog']:>12.2f}  "
                    f"{row['close_diff']:>7.2f}"
                )
    sep()


if __name__ == "__main__":
    main()
