# BTC SMC Algorithmic Trader

A live Bitcoin trading system built on NautilusTrader, applying Smart Money Concepts (SMC) / ICT strategy logic with Telegram notifications and Binance Futures execution.

See `PROJECT.md` for the full strategy background, backtesting history, and architectural decisions.

---

## Project structure

```
btc_trader/
├── config/
│   ├── settings.yaml        ← all non-secret config (edit this)
│   └── .env                 ← secrets: API keys, Telegram token (create from .env.example)
├── core/                    ← pure Python infrastructure
│   ├── config.py            ← settings loader + validation
│   ├── logging_setup.py     ← console + rotating file logging
│   ├── node_builder.py      ← builds NautilusTrader TradingNode
│   ├── market_structure.py  ← MS BOS/CHoCH signal engine  ← copy from ~/data/
│   ├── htf_bias.py          ← 1H HMA bias filter           ← copy from ~/data/
│   ├── fvg_zones.py         ← FVG/IFVG zone tracker        ← copy from ~/data/
│   └── atr.py               ← standalone ATR               ← copy from ~/data/
├── strategies/              ← NautilusTrader Strategy subclasses (Stage 3)
├── actors/                  ← NautilusTrader Actor subclasses (Stage 4)
├── events/                  ← custom event types: SignalEvent, etc. (Stage 4)
├── risk/                    ← OpenTrade ledger + SL/TP management (Stage 3)
├── execution/               ← order type abstraction (Stage 5)
├── persistence/             ← save/load open trade state for restarts (Stage 3)
├── monitoring/              ← heartbeat, health checks (Stage 6)
├── scripts/
│   └── check_infra.py       ← Stage 1 validation tool
├── logs/                    ← rotating log files (git-ignored)
├── state/                   ← trade ledger persistence (git-ignored)
└── main.py                  ← entry point
```

---

## Setup (Stage 1)

### 1. Install dependencies

```bash
cd btc_trader
pip install -r requirements.txt
```

### 2. Install and start Redis

Redis is required by NautilusTrader's live TradingNode for internal cache state.

```bash
# Ubuntu / Debian
sudo apt install redis-server
sudo systemctl start redis-server
sudo systemctl enable redis-server

# macOS
brew install redis
brew services start redis

# Verify
redis-cli ping   # should return PONG
```

### 3. Copy signal modules from backtesting directory

```bash
cp ~/data/market_structure.py core/
cp ~/data/htf_bias.py         core/
cp ~/data/fvg_zones.py        core/
cp ~/data/atr.py              core/
```

### 4. Configure secrets

```bash
cp config/.env.example config/.env
# Now edit config/.env with your real API keys and Telegram token
```

### 5. Review config/settings.yaml

The default mode is `dry_run` — no orders will be placed. Review the strategy and risk parameters before enabling paper or live mode.

### 6. Run the infrastructure check

```bash
python scripts/check_infra.py
```

All 8 checks must pass before proceeding to Stage 2.

### 7. Start the system

```bash
python main.py
```

---

## Build stages

| Stage | What it adds | Status |
|---|---|---|
| 1 | Project foundation, config, logging, Redis, infra check | ✅ Current |
| 2 | Live WebSocket data feed (Binance Futures 15m + 1H bars) | 🔜 Next |
| 3 | Strategy port + paper trading + trade ledger | ⏳ Pending |
| 4 | Telegram notifications (TelegramActor + custom events) | ⏳ Pending |
| 5 | Real order execution (Binance Futures REST) | ⏳ Pending |
| 6 | Reliability, monitoring, emergency stop | ⏳ Pending |
| 7 | Multi-timeframe + multi-strategy scaling | ⏳ Future |

---

## Configuration reference

All settings are in `config/settings.yaml`.  
Secrets go in `config/.env` (never commit this file).

Key settings to review before going live:

| Setting | Default | Description |
|---|---|---|
| `mode` | `dry_run` | `dry_run` / `paper` / `live` |
| `risk.trade_size` | `0.001` | BTC per trade — start very small |
| `risk.max_open_trades` | `5` | Hard cap on simultaneous open trades |
| `risk.daily_loss_limit_usdt` | `50.0` | Kill switch: no new entries below this |
| `strategy.htf_filter` | `true` | Require 1H HMA agreement before entry |
| `telegram.enabled` | `false` | Enable after bot is configured |

---

## Trading modes

**`dry_run`** — Live data, signals logged and optionally sent to Telegram, no orders placed. Use this for at least 1–2 weeks to confirm live signals match backtesting expectations.

**`paper`** — Same as dry_run, plus simulated order fills via Binance Testnet. Tracks virtual P&L. Run for at least 4 weeks before going live.

**`live`** — Real orders on your Binance Futures account. Only enable after sustained positive paper trading results and with minimum position sizing.
