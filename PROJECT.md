# BTC SMC Algorithmic Trading System — Project Documentation

**Status as of:** June 2026
**Author/Owner:** Karim
**Purpose of this document:** Complete reference for the project's goal, architecture, everything built so far, key findings, and open next steps — written so that development (including AI-assisted development via the Claude API) can continue with full context.

---

## 1. Project Vision & Goal

The end goal is a smart Bitcoin trading signal system that:

1. Pulls live exchange data (Binance USDT-M perpetual futures, BTCUSDT-PERP)
2. Analyzes it using a Smart Money Concepts (SMC) / ICT-style technical framework, combined with AI-based judgment
3. Generates long/short trade signals with defined risk levels (entry, stop loss, take-profit)
4. Notifies the user (originally planned via Telegram)
5. Eventually trades live, or at minimum provides high-confidence manual trading signals

The system is being built in two broad phases:

- **Phase A — Backtesting & strategy validation (current focus, in progress).** Build the SMC signal logic in Python, validate it rigorously against historical data, and only promote modules into the live system once they show a real, generalizable edge.
- **Phase B — Live signal generation with AI augmentation (not yet started).** Once the rule-based core is validated, layer an AI model (Claude API) on top to interpret chart context, score signal quality, or generate natural-language trade rationale alongside the mechanical signal.

The project originated from a fully built **Pine Script v6 strategy** already running on TradingView, which is being ported to Python/NautilusTrader so it can be rigorously backtested with proper statistical controls (the kind of testing Pine Script's bar-replay/strategy tester doesn't make easy) and eventually deployed for live or semi-live trading.

---

## 2. The Original Pine Script Strategy (source of truth for all ported logic)

The Pine Script strategy operates on **15-minute primary bars** with **1-hour HTF bias**, and combines these modules:

- **Market Structure (MS):** BOS (Break of Structure) / CHoCH (Change of Character) detection via swing pivots, with HH/LH/HL/LL classification
- **FVG / IFVG zones:** ICT-style 3-candle Fair Value Gap detection, with inversion logic when a zone fails
- **HTF bias:** Hull Moving Average (HMA) direction on the 1H timeframe
- **Moving averages:** HMA / ZLEMA / ALMA / Kalman filter options (not yet ported to Python)
- **Anchored VWAP + bands** (not yet ported to Python)
- **CVD (Cumulative Volume Delta)** via taker buy/sell volume (data available, module not yet built in Python)
- **Volume spike filtering** — 3-layer (EMA spike, directional spike, dominance ratio) (not yet ported)
- **Session filter** (08:00–22:00) (not yet ported)
- **Candle pattern filter** (engulfing/hammer) (not yet ported)

**Entry signal logic (Pine Script):** `ms_momentum_long` fires on an LH→HH swing sequence combined with an active bullish CHoCH state; `ms_momentum_short` mirrors this for HL→LL + bearish CHoCH.

**Risk management (Pine Script, and carried into every Python port):**
- Stop loss: ATR-based
- TP1: close 50% of position at 2.0 × ATR
- TP2: close the remaining 50% at 3.5 × ATR
- Exit on opposite momentum signal (if still open)

The **planned layered backtesting order** (to isolate each module's marginal contribution before stacking them) is:

| Layer | Adds |
|---|---|
| 1 | Market Structure (MS) only |
| 2 | + HTF 1H HMA bias |
| 3 | + FVG/IFVG zones |
| 4 | + MA (HMA/Kalman) trend confirmation |
| 5 | + Anchored VWAP |
| 6 | + Volume / CVD |
| 7 | + Session + candle pattern filters |

Only Layers 1–3 have been built and tested so far (see Section 6).

---

## 3. System Architecture

### 3.1 Engine choice: NautilusTrader

Several options were evaluated before settling on the core engine:

- **TradingAgents** — rejected: stock-only, no SMC/crypto support
- **Freqtrade** — considered for its backtesting harness, but ultimately not used; its assumptions didn't fit a stateful, multi-module SMC strategy well
- **NautilusTrader** (chosen) — event-driven, Rust/Python hybrid, genuine backtest-to-live parity, built-in `ParquetDataCatalog` for fast historical data access, native Redis/state support for eventual live deployment, and clean extensibility for AI-driven decision layers later

NautilusTrader version in use: **v1.228.0**, installed via `uv pip install nautilus_trader`.

### 3.2 Planned live architecture (NOT yet built)

The original live-trading design was a 3-microservice system communicating via Redis pub/sub:

```
Data Collector  →  Analysis Engine  →  Notifier (Telegram)
```

This was the *initial* plan before NautilusTrader was adopted. With NautilusTrader as the core engine, the live version would likely use NautilusTrader's own Binance adapter for live data + order execution directly, rather than a hand-rolled Data Collector — but this has not been revisited or built yet. **This is an open architectural decision for Phase B.**

### 3.3 Backtesting architecture (current, working)

No separate data collector is needed for backtesting. NautilusTrader's `ParquetDataCatalog` is the storage layer; historical data is downloaded once via Binance's Public Data Repository (Section 4) and read directly by the catalog.

---

## 4. Data Pipeline (✅ Completed)

### 4.1 Key clarification: Binance API endpoints

- `api.binance.com` = **Spot API** (wrong market for this project)
- `fapi.binance.com` = **Futures API** (correct — used for instrument specs, e.g. `/fapi/v1/exchangeInfo`)

The perpetual contract traded is **BTCUSDT-PERP** on venue **BINANCE** (NautilusTrader appends `-PERP` to the raw `BTCUSDT` symbol).

### 4.2 Why the download approach changed

Initial attempts used the paginated REST API (`fapi.binance.com/fapi/v1/klines`), which is slow (rate-limited, many small requests) and was also affected by a CSV-parsing bug related to Binance occasionally including a header row in their bulk files.

**Final working approach** uses Binance's official **Public Data Repository** (`data.binance.vision`) — bulk monthly zip files, no API key, no rate limits:

```
https://data.binance.vision/data/futures/um/monthly/klines/{SYMBOL}/{interval}/{SYMBOL}-{interval}-{YYYY}-{MM}.zip
```

Two scripts implement this, used together:

- **`download_raw.sh`** — bash + `wget`, downloads and extracts every monthly CSV for 15m/1h/4h/1d into `raw_data/{interval}/`. Skips already-downloaded months (safe to re-run after interruption).
- **`build_catalog.py`** — reads the extracted CSVs, **detects header vs. no-header format** by checking if the first character of the file is a digit (timestamp) or letter (header text), builds the `CryptoPerpetual` instrument from live Futures API specs, and writes everything into a NautilusTrader `ParquetDataCatalog`. Also extracts `taker_buy_base_asset_volume` and writes it to a separate CVD-ready parquet file per timeframe.

### 4.3 Catalog contents

Two catalogs exist:

| Catalog | Date range | Months | Status |
|---|---|---|---|
| `./catalog` | 2025-01 → 2026-05 | 17 | Superseded — too short, proved misleading once (see Section 7.2) |
| `./catalog_24` | 2024-01 → 2026-05 | 29 | **Canonical — use this for all testing going forward** |

Catalog directory layout:

```
catalog_24/
├── data/
│   ├── crypto_perpetual/     ← instrument definition
│   └── bar/                  ← OHLCV bars, all 4 timeframes
└── cvd/
    ├── 15m.parquet            ← volume, taker_buy_base, taker_sell_base, cvd_delta
    ├── 1h.parquet
    ├── 4h.parquet
    └── 1d.parquet
```

The CVD module itself (reading these parquet files into a live cumulative-delta indicator) **has not been built yet** — only the raw data is captured and stored, ready for when Layer 6 is implemented.

---

## 5. Core Signal Modules (✅ Completed, reusable, pure Python)

All of these are standalone classes with **zero NautilusTrader dependency**, so they can be unit-tested independently and reused across any strategy script.

### 5.1 `market_structure.py` — `MarketStructure` class

Ports the Pine Script BOS/CHoCH engine:
- Pivot detection with a `swing_len`-bar lookback (mirrors `ta.pivothigh`/`pivotlow`)
- ATR-based minimum distance filter between opposite-direction pivots (`atr_dist`)
- Two-stage confirmation: a CHoCH sets a "pending" trend state without flipping the confirmed trend; a subsequent BOS in the same direction confirms it
- HH/LH/HL/LL swing classification
- Built-in Wilder's ATR (`atr_len`)
- Public outputs: `.momentum_long`, `.momentum_short` (one-bar pulse signals), `.atr`

Default parameters: `swing_len=10`, `atr_dist=0.5`, `atr_len=14`.

### 5.2 `htf_bias.py` — `HTFBias` class

Computes a directional bias from Hull Moving Average (HMA) on a higher timeframe (1H by default):
```
HMA = WMA(2 × WMA(close, n/2) − WMA(close, n), √n)
bull = close > HMA  AND  HMA > HMA[2 bars ago]
bear = close < HMA  AND  HMA < HMA[2 bars ago]
```
Default period: `21`. Exposes `.bull`, `.bear`, `.initialized` (False during warmup — both flags default False, so nothing fires until enough 1H bars have arrived).

### 5.3 `fvg_zones.py` — `FVGZones` class

The most complex of the four modules. Tracks Fair Value Gap zones using the ICT 3-bar pattern and exposes **two independent usage modes from the same engine**:

- **Signal mode** — `.bull_signal` / `.bear_signal`: a one-bar pulse that fires exactly when price touches a zone and then closes back outside it (a "bounce" confirmation). Designed for use as a standalone, reactive entry signal.
- **Filter mode** — `.long_filter` / `.short_filter`: `near AND recent` — true when price is currently within 1×ATR of an active zone **and** a bounce happened within the last `sig_lookback` bars. Designed for use as confluence on top of another signal (e.g. Market Structure).
- **Individual components** also exposed for custom logic: `.bull_near`, `.bear_near`, `.bull_recent`, `.bear_recent`.

Also implements **FVG → IFVG inversion**: if a zone's far boundary is breached (beyond an ATR buffer), it flips polarity rather than disappearing — a broken support becomes resistance going forward.

Default parameters: `atr_mult=0.25` (minimum gap size), `max_zones=10`, `sig_lookback=3`, `ifvg_enable=True`.

### 5.4 `atr.py` — standalone `ATR` class

Extracted so any module needing Average True Range (FVG's gap-size filter, a strategy's risk engine) can compute it independently of `MarketStructure`. Wilder's smoothing, same math as the ATR embedded in `MarketStructure`.

---

## 6. Backtesting Methodology — Phase 1: Layered Single-Position (historical, partially superseded)

### 6.1 Approach

Three scripts were built to test each layer additively, using a **single-position-at-a-time** model (a new signal couldn't open a trade while one was already open):

- `backtest_layer1.py` — MS only (the control group)
- `backtest_layer2.py` — MS + HTF bias filter
- `backtest_layer3.py` — MS + HTF + FVG (filter mode)

Both `backtest_layer1.py` and the FVG/MS signal-mode scripts (Section 8) now support `--catalog --start --end` CLI flags for fair year-by-year date comparisons.

### 6.2 Results

| Layer | Catalog (17mo) Trades / WR / PnL | Catalog_24 (29mo) Trades / WR / PnL |
|---|---|---|
| 1 (MS only) | 464 / 39.2% / **-111.66** | 800 / 37.6% / **-423.69** |
| 2 (+HTF) | 304 / 40.8% / **-14.81** | 512 / 40.0% / **-44.52** |
| 3 (+FVG filter) | 138 / 42.0% / **+2.85** | 245 / 38.4% / **-56.49** |

### 6.3 ⚠️ Critical finding: overfitting on a short test window

On the smaller 17-month catalog, Layer 3 (FVG-as-filter) appeared to be a clear improvement over Layer 2 (+2.85 vs -14.81, and a higher win rate). On the larger 29-month catalog, **this reversed entirely** — Layer 3 performed *worse* than Layer 2 (-56.49 vs -44.52), and its win rate dropped *below* Layer 2's (38.4% vs 40.0%).

Isolating just the new data added (the extra ~12 months from 2024), the FVG filter's performance on that segment alone worked out to roughly **-0.55/trade**, implying a ~35% win rate on the new data alone — worse than even the unfiltered raw MS signal. This is a textbook overfitting signature: an apparent edge that's actually a property of one specific time window, not a generalizable pattern.

**Methodological conclusions adopted from this finding:**
1. **Always test on the largest available dataset** (`catalog_24`) — a short window can actively mislead, not just lack statistical power.
2. **Layer 2 (MS + HTF) is the current validated, robust baseline** — its win-rate improvement over Layer 1 held consistently across both the 17-month and 29-month windows (+1.5 to +2.6 percentage points either way).
3. The FVG module's *filter-mode* contribution is **not yet trusted** as a permanent part of the stack — it needs further investigation (year-by-year breakdown, different parameters, or possibly abandoning filter-mode for FVG in favor of using it as an independent signal — see Section 6.4 / Section 8).

### 6.4 Why FVG was rebuilt as a standalone signal too

Given the overfitting finding above, the user requested a way to directly compare **FVG as a standalone entry signal** (signal mode) against **MS as a standalone entry signal**, on a year-by-year basis, to determine which raw signal source has genuine edge before either is used as a filter on the other.

This led directly into Phase 2 of the methodology (Section 7), since testing this properly required first fixing a configuration bug (see Section 9.5) and then a more significant architectural change (multi-position support).

---

## 7. Backtesting Methodology — Phase 2: Multi-Position Trade Ledger (current, ✅ in active use)

### 7.1 Why this change was made

The single-position model blocked new trades from opening while one was already active — meaning many real signals were silently discarded. The user wanted: (a) every signal to open its own independently tracked trade, even with others already open, (b) each trade tracked by a unique ID with its own entry/exit/PnL, and (c) long and short performance reported separately so directional bias in signal quality is visible.

### 7.2 Design decision: keep `OmsType.NETTING`, build a custom Python ledger

NautilusTrader's own per-instrument position report blends overlapping entries into a single weighted-average position under `NETTING` mode (a realistic one-way futures account, matching how Binance's standard account mode actually works) — so it can't preserve individual trade identity once multiple entries overlap.

**Solution:** keep `OmsType.NETTING` (the realistic, one-way-account venue model) but maintain an **independent Python-level ledger** as the source of truth for all reporting and analysis. Real market orders are still submitted for every entry/exit (so the venue's account balance reflects true fills and fees as a sanity check), but trade identity, PnL attribution, and all summary statistics come from this ledger, not from NautilusTrader's position report.

```python
@dataclass
class OpenTrade:
    trade_id:    int
    side:        str        # "LONG" or "SHORT"
    entry_price: float
    entry_ts:    int         # bar.ts_init at entry (ns)
    full_qty:    Decimal
    sl, tp1, tp2: float
    tp1_hit:      bool  = False
    realized_pnl: float = 0.0   # accumulates as partial/final closes happen
    exit_ts:      int   = None
    exit_reason:  str   = ""
    best_price:     float = None   # trailing-TP2 state (Section 8.3)
    trail_distance: float = None
```

Shared helper functions (identical in both strategy scripts):
- `summarize_trades(trades)` → count, win rate, avg win/loss, R:R, total/best/worst PnL
- `breakdown_by_reason(trades)` → groups by exit reason (SL / BE / TP1 / TP2 / TP2-trail / exit-signal / EOD)

### 7.3 Two mirrored scripts

- **`backtest_fvg_signal.py`** — FVG used as a standalone entry signal (`fvg.bull_signal` / `fvg.bear_signal`)
- **`backtest_ms_signal.py`** — MS used as a standalone entry signal (`ms.momentum_long` / `ms.momentum_short`)

These two files are **deliberately kept in lockstep, feature-for-feature** — every risk-management feature, CLI flag, and reporting format added to one is mirrored into the other, so any comparison between them is a clean test of signal source alone. Only the signal-specific parameters differ (FVG's zone params vs. MS's pivot params).

A parameter-sweep helper also exists:
- **`backtest_fvg_sweep.py`** — runs a grid of `--atr-len` / `--atr-mult` combinations for the FVG signal in one command, printing a sorted comparison table. *(No equivalent `backtest_ms_sweep.py` has been built yet — open item, see Section 11.)*

---

## 8. Current Strategy Scripts — Full Feature Reference

### 8.1 Shared CLI flags (identical in both scripts)

| Flag | Default | Effect |
|---|---|---|
| `--catalog` | `./catalog` | Path to the ParquetDataCatalog (use `./catalog_24` for real testing) |
| `--start` / `--end` | none | Date filter, `YYYY-MM-DD`, inclusive |
| `--bar-type` | 15-min BTCUSDT-PERP | Primary signal timeframe |
| `--instrument` | `BTCUSDT-PERP.BINANCE` | Instrument ID |
| `--sl-atr` | `1.5` | Stop loss distance, × ATR |
| `--tp1-atr` | `2.0` | TP1 distance (closes 50%), × ATR |
| `--tp2-atr` | `3.5` | TP2 distance (closes remainder, fixed mode), × ATR |
| `--trailing-tp2` | off | See 8.3 |
| `--trail-atr` | none (falls back to `--tp2-atr`) | See 8.3 |
| `--breakeven-sl` | off | See 8.3 |
| `--no-exit-signal` | off (exit-signal enabled) | See 8.3 |
| `--no-sl` | off (SL enabled) | See 8.3 |
| `--htf-filter` | off | See 8.3 |
| `--htf-period` | `21` | HTF HMA period |
| `--bar-type-1h` | 1-hour BTCUSDT-PERP | Only loaded/subscribed if `--htf-filter` is set |
| `--export` | none | Path to write a full JSON export (see 8.4); nothing written if omitted |

### 8.2 Signal-specific CLI flags

**MS (`backtest_ms_signal.py`):** `--swing-len` (10), `--atr-dist` (0.5), `--atr-len` (14)

**FVG (`backtest_fvg_signal.py`):** `--fvg-atr-mult` (0.25), `--fvg-max-zones` (10), `--no-ifvg`

### 8.3 Risk-management feature details

All of the following default to the **original/simple behavior**, are opt-in via flag, and are independently combinable (e.g. `--breakeven-sl --trailing-tp2` together).

- **Trailing TP2** (`--trailing-tp2`, `--trail-atr`): once TP1 fires, a reference price starts at the entry price and ratchets in the favorable direction only (highest high reached for longs / lowest low for shorts) as new bars arrive — never moves backward. The remaining 50% closes when price pulls back `--trail-atr × ATR` from that ratcheted peak (the distance is frozen using the live ATR at the moment TP1 fires; only the reference point keeps moving). **Opposite-signal exit is intentionally disabled** once trailing is active — the entire point of a trailing stop is to let a working trade run further than a signal-reversal would otherwise allow. The hard SL still applies throughout as the backstop (unless also disabled via `--no-sl`). If `--trail-atr` isn't explicitly set, it reuses the `--tp2-atr` value as a default callback distance.

- **Breakeven SL** (`--breakeven-sl`): once TP1 fires, SL moves to the entry price. The worst case for the remaining 50% becomes exactly $0 instead of a loss. Exits triggered this way are labeled `"BE"` in reporting, distinct from a genuine `"SL"` loss. Independent of TP2 mode.

- **Disable exit-signal** (`--no-exit-signal`): removes the opposite-signal exit everywhere it would normally apply (both pre-TP1 full-size exits and post-TP1 partial exits in fixed mode). Trailing mode already ignores this leg's exit-signal check regardless of this flag, for the separate design reason above — this flag's effect there is limited to the pre-TP1 stage.

- **Disable SL** (`--no-sl`): removes the stop loss check entirely. A trade can then only close via TP1/TP2(-trail)/exit-signal/forced-EOD-close. A `max_open_trades` diagnostic (peak concurrent open trades) is tracked and printed every run, since disabling SL can let positions accumulate indefinitely — this number is especially informative when this flag is used.

- **HTF filter** (`--htf-filter`, `--htf-period`, `--bar-type-1h`): requires the 1H HMA bias (Section 5.2) to agree with the signal's direction before an entry is allowed. Gates **entries only**, never exits. 1H bars are only ever subscribed to and loaded from the catalog when this flag is set — the default raw-signal run pays zero extra cost. If the flag is set but no 1H data exists for the requested date range, the script aborts with a clear error rather than silently producing a misleading zero-trade result.

### 8.4 JSON export schema (`--export <path>`)

Both scripts write an **identical schema**, so files from either can be loaded and compared with the same code:

```json
{
  "meta": {
    "script": "backtest_fvg_signal.py",
    "exported_at": "...", "catalog": "...", "instrument": "...",
    "bar_type": "...", "requested_start": "...", "requested_end": "...",
    "label": "...", "bars": 12345
  },
  "params": { /* every resolved CLI flag value for this run */ },
  "summary": {
    "all": { "trades": ..., "winners": ..., "losers": ..., "wr": ..., "avg_win": ..., "avg_loss": ..., "rr": ..., "total": ..., "best": ..., "worst": ... },
    "long": { /* same shape */ },
    "short": { /* same shape */ },
    "exit_reasons": { "SL": {"count": ..., "total": ...}, "TP2": {...}, ... }
  },
  "max_open_trades": 12,
  "engine_ending_balance": 9821.45,
  "trades": [ { "id": 1, "side": "LONG", "entry_price": ..., "entry_time": "ISO8601", "exit_time": "ISO8601", "exit_reason": "...", "realized_pnl": ... }, ... ]
}
```

The console no longer prints the first-30-trades table — if per-trade detail is needed, use `--export` and inspect/load the JSON file instead. The console still prints the ALL/LONG/SHORT summary blocks, the exit-reason breakdown, the `max_open_trades` diagnostic, and the venue's fee-inclusive ending balance as a sanity check against the (fee-exclusive) gross ledger total.

---

## 9. Key Analytical Findings (important context, not just "what was built")

### 9.1 The opposite-signal cascade

A single bar-level boolean (`momentum_short`/`bear_signal` etc.) closes **every** currently open trade in the opposite direction on that same bar — not just one. If three longs are open and a short signal fires, all three close (each via `exit-signal`, unless one of them independently hit its own SL/TP first that same bar, which takes priority). The same signal that closes them can also simultaneously open a brand-new opposite-direction trade on that identical bar. This was confirmed directly in real output (trades #28/#29 closing at the identical timestamp). **One asymmetry between the two strategies:** MS's `momentum_long`/`momentum_short` are mutually exclusive (both derive from a single "last event" state, which can only be bullish or bearish), so MS can never cascade-close both directions on the same bar. FVG's `bull_signal`/`bear_signal` come from independent zones and could, in rare cases, both be true on the same bar.

### 9.2 The TP1 > SL structural win-guarantee

Given the default risk parameters (`tp1_atr=2.0 > sl_atr=1.5`), **any trade that reaches TP1 is mathematically guaranteed to end up a net winner**, regardless of what happens to the second leg afterward:

```
Leg 1 (TP1 fires):  profit = tp1_atr × ATR × 0.5Q =  1.00 × ATR × Q
Leg 2 (worst case, SL): loss = sl_atr × ATR × 0.5Q = -0.75 × ATR × Q
                                                       ───────────────
                              worst-case total        = +0.25 × ATR × Q   (still positive)
```

Breakeven-SL and trailing-TP2 can only make leg 2 *better than or equal to* this floor — never worse — so **no combination of the risk-toggle flags can ever flip a TP1-reaching trade's win/loss classification**. This is why, across four different risk-mode test runs on the same data, win/loss *counts* stayed byte-for-byte identical while total PnL still varied — only the *magnitude* of already-guaranteed wins changed. (This guarantee would no longer hold if `sl_atr` were ever set larger than `tp1_atr`.)

### 9.3 Real comparative result: Q1 2025, four risk modes (FVG signal)

| Mode | Total PnL (gross) | Notes |
|---|---|---|
| `--breakeven-sl` alone | **-586.10** (best) | Protects 135 trades from a worse outcome (+440.70 net benefit), at the cost of 26 trades cut short before reaching a bigger fixed TP2 |
| Default (no flags) | -620.08 | Baseline |
| `--breakeven-sl --trailing-tp2` | -815.30 | Combining helps vs. trailing alone, but still worse than breakeven alone |
| `--trailing-tp2` alone | **-868.02** (worst) | Disables the exit-signal mechanism, which was earning ~+$0.43/trade on average in default mode; many trades that would have gotten an early, mild exit instead round-tripped all the way to the harder original stop |

**Caveat:** this is one quarter's data. Given the overfitting lesson from Section 6.3, this ranking should be re-validated across multiple full-year periods before being treated as a settled conclusion — it is not yet considered final.

### 9.4 FVG raw-signal volume and quality (single-position era findings, now superseded by multi-position re-testing)

Before the multi-position rewrite, FVG-as-a-standalone-signal was tested year-by-year and showed it firing far more often than MS (roughly 2,200–2,500 signals/year vs. MS's ~300–340/year), at a win rate consistently *below* the ~41–42% breakeven threshold in both 2024 and 2025 (33–39%). This was the original motivation for re-testing under the multi-position architecture with full risk-toggle flexibility — those newer, more flexible results are what should be trusted going forward, not these older single-position-blocked numbers.

---

## 10. Complete File Inventory

All files live in the user's `~/data/` working directory.

| File | Role | Status |
|---|---|---|
| `download_raw.sh` | Bulk-downloads monthly klines via wget from `data.binance.vision` | ✅ Working |
| `build_catalog.py` | Builds the NautilusTrader ParquetDataCatalog + CVD parquet files from the raw CSVs | ✅ Working |
| `atr.py` | Standalone Wilder ATR class | ✅ Complete |
| `market_structure.py` | `MarketStructure` class — BOS/CHoCH engine | ✅ Complete |
| `htf_bias.py` | `HTFBias` class — 1H HMA directional bias | ✅ Complete |
| `fvg_zones.py` | `FVGZones` class — signal mode + filter mode + IFVG inversion | ✅ Complete |
| `backtest_layer1.py` | MS-only, single-position, layered baseline (now also CLI-date-filterable) | ✅ Working, historical reference |
| `backtest_layer2.py` | MS + HTF, single-position | ✅ Working, historical reference |
| `backtest_layer3.py` | MS + HTF + FVG-filter, single-position | ✅ Working, historical reference — flagged as not robustly validated (Section 6.3) |
| `backtest_fvg_signal.py` | FVG as standalone multi-position signal, full risk-toggle suite, HTF filter, JSON export | ✅ Current, actively used |
| `backtest_ms_signal.py` | MS as standalone multi-position signal — exact feature mirror of the FVG script | ✅ Current, actively used |
| `backtest_fvg_sweep.py` | Grid sweep of FVG `atr_len`/`atr_mult` combinations in one command | ✅ Working |

---

## 11. Open Questions & Next Steps

These are explicitly **not yet done** and are the natural continuation points:

1. **Claude API integration for SMC analysis (the next major phase).** An open question was raised mid-project — *"How to prompt Claude API to analyze Bitcoin SMC setups and output trade signals?"* — and has not yet been answered or explored. This is the natural Phase B starting point: deciding how an LLM should consume chart/indicator state (likely: a structured snapshot of MS/FVG/HTF signal states + recent price action) and what it should output (a directional call? a confidence score? a written rationale alongside the mechanical signal?). This needs real design work before any code is written.

2. **HTF filter results not yet analyzed.** `--htf-filter` was just added to both scripts (mirroring the old Layer 2 logic into the new multi-position architecture) but no comparative results have been run or discussed yet — this is the immediate next analysis step.

3. **No `backtest_ms_sweep.py`.** The FVG parameter-sweep tool has no MS-side equivalent yet (would sweep `--swing-len`/`--atr-dist`).

4. **No cross-run comparison/aggregation tool.** Multiple JSON exports can now be produced, but no script exists yet to auto-discover a folder of exports and build one consolidated comparison table (was discussed as a likely next build once enough exports accumulate).

5. **Layers 4–7 not built.** MA/Kalman confirmation, anchored VWAP, CVD, session/candle filters all remain Pine-Script-only — none ported to Python yet, despite the data pipeline already capturing what CVD needs.

6. **No live-trading architecture decided.** The original 3-microservice plan (Data Collector / Analysis Engine / Notifier) predates the NautilusTrader decision and has not been revisited. Whether to use NautilusTrader's own live Binance adapter directly, or retain a separate live data/notification layer, is still open.

7. **No Telegram (or other) notification layer built.**

8. **FVG-as-filter (Layer 3) needs a verdict.** Per Section 6.3/6.4, this was shelved pending a year-by-year diagnostic that was never completed — it's unclear whether FVG belongs anywhere in the final stack as a filter, only as a standalone signal, or not at all.

---

## 12. Quick-Start Reference (for picking this project back up)

```bash
# Re-verify the canonical catalog exists and is intact
python -c "
from nautilus_trader.persistence.catalog import ParquetDataCatalog
cat = ParquetDataCatalog('./catalog_24')
bars = cat.bars(bar_types=['BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL'])
print(len(bars), bars[0])
"

# Raw signal baselines, full year, exported for record-keeping
python backtest_ms_signal.py  --catalog ./catalog_24 --start 2024-01-01 --end 2024-12-31 --export results/ms_2024_raw.json
python backtest_fvg_signal.py --catalog ./catalog_24 --start 2024-01-01 --end 2024-12-31 --export results/fvg_2024_raw.json

# With HTF filter — the next thing to evaluate
python backtest_ms_signal.py  --catalog ./catalog_24 --start 2024-01-01 --end 2024-12-31 --htf-filter --export results/ms_2024_htf.json
python backtest_fvg_signal.py --catalog ./catalog_24 --start 2024-01-01 --end 2024-12-31 --htf-filter --export results/fvg_2024_htf.json
```

Always test on `./catalog_24` (29 months), never the smaller `./catalog` — Section 6.3 explains why a shorter window actively misled a previous round of conclusions.
