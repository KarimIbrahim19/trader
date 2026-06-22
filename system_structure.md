
Overall structure: 3 phases, 7 stages
PHASE A — CONCURRENT WITH BACKTESTING (build now)
├── Stage 1: Project foundation & infrastructure
├── Stage 2: Live data feed validation
├── Stage 3: Strategy port + paper trading
└── Stage 4: Telegram integration

PHASE B — AFTER BACKTESTING CONCLUDES (production)
├── Stage 5: Real order execution
└── Stage 6: Reliability, monitoring & safety

PHASE C — FUTURE EXPANSION (when strategy evolves)
└── Stage 7: Multi-timeframe & multi-strategy scaling


________________________________________________________________________________

Stage 1: Project foundation & infrastructure

What this stage builds:

The project skeleton — directory structure, configuration system, environment management, Redis, and a minimal TradingNode that can start and stop cleanly with no strategy attached yet. Nothing trades, nothing connects to Binance's market data. This stage is purely about getting the infrastructure right before anything is built on top of it.

Directory structure:

──────────────────────
live_trader/
├── config/
│   ├── settings.yaml        ← strategy params, risk levels, timeframes, mode
│   └── .env                 ← API keys, Telegram token  (never committed to git)
├── core/                    ← the pure Python signal modules (copied from ~/data)
│   ├── market_structure.py
│   ├── htf_bias.py
│   ├── fvg_zones.py
│   └── atr.py
├── strategies/              ← NautilusTrader Strategy subclasses
├── actors/                  ← NautilusTrader Actor subclasses (Telegram, monitoring)
├── events/                  ← custom event types (SignalEvent, TradeOpenedEvent...)
├── risk/                    ← OpenTrade ledger + SL/TP management
├── execution/               ← order type abstraction layer
├── persistence/             ← save/restore open trade state across restarts
├── monitoring/              ← heartbeat, health checks
├── main.py                  ← entry point, builds and runs TradingNode
└── PROJECT.md               ← (the doc we just wrote, lives here too)
──────────────────────

The configuration system:
A settings.yaml that controls everything without touching code — this is critical for scaling later:

──────────────────────
mode: paper          # paper | live | dry-run

instrument:
  symbol: BTCUSDT-PERP.BINANCE
  venue: BINANCE

timeframes:
  primary: 15m
  htf: 1h            # HTF bias timeframe

strategy:
  name: ms_htf       # which strategy to run
  swing_len: 10
  atr_dist: 0.5
  atr_len: 14
  htf_period: 21
  htf_filter: true

risk:
  trade_size: 0.001  # start very small (minimum viable size)
  sl_atr: 1.5
  tp1_atr: 2.0
  tp2_atr: 3.5
  trailing_tp2: false
  breakeven_sl: true
  max_open_trades: 5
  daily_loss_limit_usdt: 50.0   # kill switch threshold

telegram:
  enabled: true
  notify_signals: true
  notify_entries: true
  notify_exits: true
  daily_summary_time: "00:00"   # UTC
──────────────────────
 
What Redis is used for:

NautilusTrader's TradingNode requires Redis for its internal cache (instrument definitions, account state, open orders, open positions). This is not optional in live mode. It also gives you free state persistence — if the process restarts, NautilusTrader can reconcile its internal state against what's actually open on the exchange.

Done when:

TradingNode starts and shuts down cleanly with no errors
Redis is running and connected
Config loads correctly from settings.yaml and .env
Git repository initialized with .env in .gitignore


________________________________________________________________________________

Stage 2: Live data feed validation
What this stage builds:
A TradingNode that connects to Binance Futures WebSocket and receives live kline bars — no strategy, no orders, no signals. Just data flowing in cleanly, logged, and verifiable.
Why this stage is its own step:
The WebSocket kline stream is the foundation everything else depends on. You need to answer these questions with real data before building on top:

Does the 15m bar close event arrive at the right time (exactly on the minute boundary)?
Does bar data match what you see on TradingView or in your catalog?
How does NautilusTrader deliver the 1H bar relative to the 15m bar when both close at the same hour boundary? (bar ordering matters for HTF correctness)
What happens when the WebSocket drops? Does the adapter reconnect automatically, and does the reconnection lose any bars?
Does the instrument spec from the live API match your catalog's instrument definition?

What specifically gets validated:

Subscribe to 15m klines (primary)
Subscribe to 1H klines (HTF bias)
Subscribe to 4H klines (for future use — costs nothing to subscribe now, validate timing)
Log every bar to a file for 24 hours, then compare a sample against catalog data
Confirm 1H bar always arrives before the 15m bar that closes at the same hour boundary (critical for HTF correctness — this was handled correctly in backtesting, must confirm it works identically in live)
Confirm reconnection behavior by simulating a network interruption

Done when:

24 hours of live bar data logged with no gaps or duplicates
Bar prices match catalog/TradingView within rounding tolerance
Reconnection after a dropped connection produces no missing bars
1H/15m bar ordering at the hour boundary confirmed correct


________________________________________________________________________________

Stage 3: Strategy port + paper trading
What this stage builds:
The Strategy class that wraps your existing signal modules, plus the multi-position OpenTrade ledger ported into the live context, running in paper trading mode (real WebSocket data, simulated order fills, no real money).
The critical design decision in this stage:
The same multi-position ledger architecture from your backtest scripts applies here — OpenTrade dataclass, summarize_trades(), breakdown_by_reason(), all carried over unchanged. But live introduces one new concern the backtest didn't have: what happens if the process restarts while trades are open?
In the backtest, on_stop() force-closes everything with an "EOD" label. In live, you cannot close trades just because the process restarted — they're real (or paper) positions that need to be recovered. So this stage also builds a persistence layer: the open_trades list is written to a JSON file on every state change, and loaded on startup. NautilusTrader's Redis cache handles its own internal reconciliation; this persistence layer handles YOUR trade ledger's reconciliation.

Paper trading mode behavior:

1-NautilusTrader provides a paper execution client that simulates fills at the bar's close price (same as your current SL/TP checking logic)
2-Every signal → simulated entry at current bar close
3-SL/TP/exit-signal all work identically to the backtest logic
4-P&L computed per trade, same as backtest
5-Full ledger written to paper_trades.json continuously


What specifically gets validated:

1-Signals fire at the same moments you'd expect from running the same data through the backtest scripts — this is the most important check. If a date range in live paper mode produces meaningfully different signals than the same date range through backtest_ms_signal.py, something is wrong in the porting
2-The ledger correctly survives a process restart (open trades are recovered, SL/TP levels restored, trailing state restored)
3-Position sizing respects the max_open_trades limit from config


Done when:

Two weeks of paper trading with signals logged and compared periodically against what backtest scripts would produce for the same period
At least one process restart during open trades with successful state recovery
Zero "phantom" positions (the ledger and simulated fills always agree)


________________________________________________________________________________

Stage 4: Telegram integration
What this stage builds:
A custom NautilusTrader Actor — the TelegramActor — that subscribes to custom events published by the strategy and sends formatted Telegram messages. Built in Stage 4 (before live execution) so notifications are validated and reliable before real money is involved.
Why a custom Actor (not just calling the Telegram API directly from the strategy):
The Strategy class should only know about signals, trades, and risk management. Notification is a separate concern. NautilusTrader's internal MessageBus allows publishing custom typed events from the strategy and subscribing to them from a separate Actor — clean, testable, independent. If you later want to add Discord, email, or a dashboard, you add another Actor subscribed to the same events.

Custom event types:
python# events/custom_events.py

──────────────────────
@dataclass
class SignalEvent:
    """Fired when a momentum signal is detected (before any entry)."""
    ts: int
    direction: str        # "LONG" or "SHORT"
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    atr: float
    signal_source: str    # "MS" | "FVG" | "MS+HTF"

@dataclass
class TradeOpenedEvent:
    trade_id: int
    direction: str
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    size: float
    open_count: int       # how many trades currently open after this one

@dataclass
class TradeClosedEvent:
    trade_id: int
    direction: str
    entry_price: float
    exit_price: float
    exit_reason: str      # SL | BE | TP1 | TP2 | TP2-trail | exit-signal | EOD
    realized_pnl: float
    running_total_pnl: float

@dataclass
class DailySummaryEvent:
    date: str
    trades_closed: int
    winners: int
    losers: int
    win_rate: float
    total_pnl: float
    open_trades: int
──────────────────────

Telegram message formats (what the bot actually sends):

──────────────────────
��� LONG SIGNAL  —  MS + HTF
Entry ≈ $97,450
SL     $96,800  (-$650)
TP1    $98,750  (+$1,300)  ← 50% close
TP2    $99,625  (+$2,175)  ← remainder
ATR    $433
15:45 UTC · BTCUSDT-PERP

──────────────────────

��� Trade #47 OPENED  —  LONG
Entry $97,460  ·  Size 0.001 BTC
SL $96,800  ·  TP1 $98,750  ·  TP2 $99,625
3 trades currently open

──────────────────────

✅ Trade #44 CLOSED  —  TP2
LONG  $96,200 → $98,710
P&L: +$2.51  ·  Running: +$18.40

──────────────────────

��� Daily Summary  —  2026-06-19
Closed: 8 trades  (5W / 3L)
Win rate: 62.5%
P&L today: +$11.20
Open trades: 2
──────────────────────

Done when:

Every event type produces the correct message in Telegram
Rate limiting handled (Telegram allows 30 messages/minute to one chat)
Notification failures don't crash or block the strategy (the Actor catches exceptions silently)
Process restart doesn't send duplicate notifications for already-notified events (timestamps tracked)


________________________________________________________________________________

Stage 5: Real order execution
What this stage builds:

Switch the execution client from paper to live Binance Futures. Real money, real orders.

Order types needed, in priority order:

 Priority     Order type                When used
1 (now)       MARKET                    Entry — always market for maximum fill certainty
2 (now)       STOP_MARKET               Hard stop loss — native exchange order so it executes even if the process is down
3 (now)       LIMIT                     TP1 and fixed TP2 — allows precise fill at target price
4 (later)     TRAILING_STOP_MARKET      Native trailing TP2 — Binance-native trailing stop via callbackRate, more accurate than bar-close simulation
5 (future)    STOP_LIMITSL              with a price floor to avoid slippage in flash crashes

Key design for this stage:
When a trade opens via a market entry order, the strategy immediately places two additional orders:

A STOP_MARKET at the SL level (kill the trade if it goes wrong even if the process is down)
A LIMIT at the TP1 level for 50% of the position

When TP1 fills:

Cancel the original full-size SL order
Place a new STOP_MARKET at either the original SL (default) or entry price (if breakeven_sl is on)
Place a new LIMIT at the TP2 level for the remaining 50%

This means real risk control doesn't depend on the process being alive — a hard stop is always sitting on the exchange.
Risk controls that must be enforced before any real order is placed:

daily_loss_limit_usdt from config — if the day's realized + unrealized PnL drops below this threshold, no new entries until midnight UTC. Existing trades continue to be managed.
max_open_trades — hard cap, checked before every entry
max_position_size_usdt — total notional across all open trades must not exceed this
Minimum ATR check — if atr < price * 0.001 (ATR is less than 0.1% of price), something is wrong with the data, block entries

Done when:

At least 2 weeks of paper trading with the full order-placement logic (placing real-format orders in paper mode, not just tracking fills internally)
Manual test of every order type: entry market order, SL stop-market, TP1 limit, TP2 limit, cancel-and-replace on TP1 fill
Kill switch tested: manual Telegram command /stop halts new entries immediately
Risk controls tested: intentionally trigger each one and verify the block works


________________________________________________________________________________

Stage 6: Reliability, monitoring & safety

What this stage builds:
Everything needed to run 24/7 without supervision.
Heartbeat: The TelegramActor sends a silent ✅ every hour. If you don't see one for two hours, something is wrong.
Exchange reconciliation on startup: On process start, compare NautilusTrader's recovered state from Redis against what's actually open on Binance via the REST API. If they disagree (e.g. an order was filled while the process was down), reconcile before doing anything else.
Order fill monitoring: Every open order is periodically checked via REST API as a backup to the WebSocket fill notification. If a fill notification was missed on the WebSocket (rare but possible), the REST check catches it within 60 seconds.
Emergency stop: A Telegram bot command /emergency_stop cancels all open orders and closes all positions immediately at market, then halts the process. Requires a confirmation reply to execute (prevents accidental triggers).
Daily P&L summary: Automated daily summary at midnight UTC via the DailySummaryEvent.
Process management: The system runs as a systemd service on Linux (or equivalent), configured to restart automatically if it crashes, with a 30-second delay between restarts to avoid thrashing.

Done when:

Intentional process kill with open orders: restarts correctly, reconciles state, continues managing positions
WebSocket drop simulation: reconnects within 30 seconds, no bars missed
Emergency stop works correctly via Telegram
System has run for 2 weeks with no manual intervention required


________________________________________________________________________________

Stage 7: Multi-timeframe & multi-strategy scaling
What this stage builds (future):
This is where the scalable architecture decisions from Stages 1–6 pay off.
Multiple primary timeframes: The config gains a list of active timeframes. The TradingNode subscribes to all of them. Each timeframe runs its own strategy instance with its own signal modules, its own trade ledger, and its own risk budget. A 5m strategy trades more frequently with smaller SL/TP multiples; a 4H strategy trades less frequently with larger ones.

strategies:
  - name: ms_htf_15m
    primary: 15m
    htf: 1h
    risk_budget_usdt: 200
  - name: ms_htf_4h
    primary: 4h
    htf: 1d
    risk_budget_usdt: 500

	
Multiple order types per signal: The execution/order_builder.py module (built in Stage 5) already abstracts order construction. Upgrading to native trailing stops is just a config flag, not a code change.
Claude API integration (Phase B): When a signal fires, instead of (or in addition to) executing mechanically, the system can package a structured snapshot of current market state and query the Claude API for a confidence score or a pass/no-pass judgment before entry. This is where the AI layer from the original project vision comes in.

________________________________________________________________________________

What to build now (parallel with backtesting)
Given that backtesting is still ongoing and you're not going live yet, the goal right now is to complete Stages 1–4 — producing a system that runs in paper mode with Telegram notifications. 
This is the minimal viable live infrastructure, and it can be built completely independently of the remaining backtesting work.

StageCan start now?
Estimated scope
1 — Foundation              ✅ Yes
2 — Data feed               ✅ Yes, after Stage 1 + 24hr validation
3 — Strategy port + paper   ✅ Yes, after Stage 2
4 — Telegram                ✅ Yes, after Stage 3
5 — Real execution⏳ After backtesting concludes
6 — Reliability⏳ After Stage 5
7 — Scaling⏳ Future

_________________________________________________________________________________
