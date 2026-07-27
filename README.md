# Live Trader

A live Bitcoin algorithmic trading system built on NautilusTrader, applying Smart Money Concepts (MS/FVG) strategies against Binance Futures USDS markets with Telegram notifications, hedge-mode execution, and a ledger reconciler.

---

## Project structure

```
test_trader/
├── config/
│   ├── settings.yaml       ← all non-secret config (edit this)
│   └── .env                ← secrets: API keys, Telegram token
├── core/
│   ├── config.py           ← settings loader + validation
│   ├── logging_setup.py    ← console + rotating file logging
│   ├── node_builder.py     ← builds NautilusTrader TradingNode
│   ├── market_structure.py ← MS BOS/CHoCH signal engine
│   ├── htf_bias.py         ← 1H HMA bias filter
│   ├── fvg_zones.py        ← FVG/IFVG zone tracker
│   ├── atr.py              ← standalone ATR
│   └── exchanges/          ← exchange adapters (base.py, binance.py)
├── strategies/
│   ├── base_smc_strategy.py ← shared SMC strategy base class
│   ├── ms_strategy.py      ← Market Structure strategy
│   ├── fvg_strategy.py     ← Fair Value Gap strategy
│   └── data_validator.py   ← bar data sanity checks
├── actors/
│   └── telegram_actor.py   ← Telegram notifications
├── events/                 ← custom event types
├── risk/
│   ├── position_manager.py ← SL/TP/entry management + ledger
│   ├── trade_ledger.py     ← OpenTrade dataclass + ledger
│   └── reconciler.py       ← Stage 6 ledger reconciliation
├── persistence/
│   └── state_store.py      ← save/load open trade state for restarts
├── monitoring/             ← heartbeat, health checks
├── scripts/
│   ├── check_infra.py      ← infrastructure check (or via `--check`)
│   └── compare_bars.py     ← compare live bars against data catalog
├── docs/                   ← implementation notes, decision records
├── logs/                   ← rotating log files (git-ignored)
├── state/                  ← trade ledger persistence (git-ignored)
├── main.py                 ← entry point
└── CHANGELOG.md
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Install and start Redis

Redis is required by NautilusTrader's live TradingNode for internal cache state.

```bash
# Ubuntu / Debian
sudo apt install redis-server
sudo systemctl start redis-server
sudo systemctl enable redis-server

# Verify
redis-cli ping   # should return PONG
```

### 3. Configure secrets

```bash
cp config/.env.example config/.env
# Now edit config/.env with your real API keys and Telegram token
```

### 4. Review `config/settings.yaml`

Default mode is `dry_run` — no orders are placed. Strategy parameters, position mode, and risk limits are all per-strategy.

### 5. Run the infrastructure check

```bash
python main.py --check
```

All checks must pass before starting.

### 6. Start the system

```bash
python main.py
```

---

## Trading modes

| Mode | Behavior |
|------|----------|
| `dry_run` | Live data, signals logged, **no orders placed**. Use for validation. |
| `paper` | Live data, simulated fills via exchange testnet. Tracks virtual PnL. |
| `live` | Real orders on your exchange account. |

---

## Key concepts

### Venue model
Each exchange has a `venues:` entry in settings.yaml with `account_type` and `position_mode` (`netting` or `hedge`). Multiple strategies can share the same venue.

### Position mode
- **Hedge** (current): LONG and SHORT positions on the same symbol are independent. Multiple strategies can trade the same symbol simultaneously.
- **Netting**: One position per symbol. Only one strategy per symbol is allowed (enforced by config validation).

### Strategies
| Strategy | Type key | Signal | Entry |
|----------|----------|--------|-------|
| MS | `ms` | Market Structure BOS/CHoCH | ATR-based distance |
| FVG | `fvg` | Fair Value Gap / IFVG zones | Zone proximity + ATR filter |

SL and TP are calculated from ATR and checked in-memory on each bar — no exchange-managed bracket orders.

### Reconciliation (Stage 6)
Compares the internal ledger's net exposure against the exchange position for each `(venue, instrument)` group. Case A (exchange < expected) → warning. Case B (exchange > expected) → halt new entries for that group.

### Data catalog
Historical and live-collected BTCUSDT data lives at `/mnt/btc_catalog/` (shared mount from the data collector server). Reference copies of collector scripts and backtest tools are at `~/catalog/` and `~/backtest/` respectively.

---

## Running

```bash
# Infrastructure check (all systems go?)
python main.py --check

# Start trading
python main.py

# With a custom config
python main.py --config path/to/settings.yaml
```
