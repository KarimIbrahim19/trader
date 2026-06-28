"""
strategies/base_smc_strategy.py
────────────────────────────────────────────────────────────────────────
Abstract base class for all SMC strategy implementations.

Stage 4 additions:
  • set_notifier(notifier) — called from main.py before node.build().
    Stores the shared TelegramNotifier instance.
  • on_start(): calls notifier.register_ledger() so heartbeat has all
    strategy data, calls on_state_restored() if trades were loaded,
    passes strategy_id + notifier to PositionManager.
  • on_bar(): notifies signals BEFORE pm.on_bar() so the signal message
    always arrives first — the "entry blocked" note follows if needed.
  • on_stop(): no Telegram call here — system stop is handled by main.py
    which sends on_system_stop() after all strategies have stopped.

Everything from Stage 3 is unchanged.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from core.htf_bias import HTFBias
from persistence.state_store import StateStore
from risk.position_manager import PositionManager, PositionManagerConfig
from risk.trade_ledger import TradeLedger


# ── Base config ───────────────────────────────────────────────────────────
class BaseSmcConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type:      BarType
    bar_type_htf:  BarType
    mode:          str
    state_dir:     str

    htf_filter:    bool
    htf_period:    int

    trade_size:            Decimal
    sl_atr:                float
    tp1_atr:               float
    tp2_atr:               float
    trailing_tp2:          bool
    trail_atr_mult:        float
    breakeven_sl:          bool
    enable_exit_signal:    bool
    enable_sl:             bool
    max_open_trades:       int
    daily_loss_limit_usdt: float


# ── Base strategy ─────────────────────────────────────────────────────────
class BaseSmcStrategy(Strategy):

    def __init__(self, config: BaseSmcConfig) -> None:
        super().__init__(config)

        self.ledger      = TradeLedger()
        self.htf         = HTFBias(period=config.htf_period)
        self.state_store = StateStore(
            strategy_id = str(config.strategy_id),
            state_dir   = config.state_dir,
        )

        self._bar_count: int            = 0
        self.pm:         PositionManager | None = None
        self.instrument  = None
        self._notifier   = None    # set via set_notifier() from main.py
        self._strategy_name = ""   # set via set_notifier() from main.py

    # ── Stage 4: notifier wiring ──────────────────────────────────────────
    def set_notifier(self, notifier, strategy_name: str = "") -> None:
        """
        Called from main.py after strategy construction, before node.build().
        The notifier is a TelegramNotifier but typed as object to avoid
        circular imports. strategy_name is the YAML key ("ms", "fvg") used
        as the ledger identifier for heartbeat/daily-summary messages.
        """
        self._notifier       = notifier
        self._strategy_name  = strategy_name

    # ── Abstract interface ────────────────────────────────────────────────
    def _init_signal_modules(self) -> None:
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement _init_signal_modules()"
        )

    def _process_primary_bar(
        self,
        high:    float,
        low:     float,
        close:   float,
        bar_idx: int,
    ) -> tuple[float, bool, bool]:
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement _process_primary_bar()"
        )

    # ── NT lifecycle ──────────────────────────────────────────────────────
    def on_start(self) -> None:
        cfg = self.config

        self.instrument = self.cache.instrument(cfg.instrument_id)
        self._init_signal_modules()

        self.subscribe_bars(cfg.bar_type)
        if cfg.htf_filter:
            self.subscribe_bars(cfg.bar_type_htf)

        # Register ledger with notifier so heartbeat/summary have this strategy's data
        if self._notifier is not None:
            self._notifier.register_ledger(
                self._strategy_name or str(cfg.strategy_id).split("-")[0].lower(),
                self.ledger,
            )

        # Restore persisted open trades
        saved = self.state_store.load()
        if saved:
            open_trades, next_id = saved
            self.ledger.restore_from_persistence(open_trades, next_id)
            if cfg.mode != "dry_run":
                self.log.warning(
                    f"⚠ {len(open_trades)} open trade(s) restored from state. "
                    "Verify these match your exchange positions before "
                    "trusting PnL figures."
                )
            # Telegram notification for state restore
            if self._notifier is not None:
                try:
                    self._notifier.on_state_restored(
                        str(cfg.strategy_id), len(open_trades)
                    )
                except Exception as e:
                    self.log.warning(f"Notifier on_state_restored error: {e}")

        # Build PositionManager — pass strategy_id and notifier
        pm_cfg = PositionManagerConfig(
            trade_size            = cfg.trade_size,
            sl_atr                = cfg.sl_atr,
            tp1_atr               = cfg.tp1_atr,
            tp2_atr               = cfg.tp2_atr,
            trailing_tp2          = cfg.trailing_tp2,
            trail_atr_mult        = cfg.trail_atr_mult,
            breakeven_sl          = cfg.breakeven_sl,
            enable_exit_signal    = cfg.enable_exit_signal,
            enable_sl             = cfg.enable_sl,
            max_open_trades       = cfg.max_open_trades,
            daily_loss_limit_usdt = cfg.daily_loss_limit_usdt,
        )
        self.pm = PositionManager(
            config          = pm_cfg,
            ledger          = self.ledger,
            submit_order_fn = self._make_submit_fn(),
            log             = self.log,
            strategy_id     = str(cfg.strategy_id),
            notifier        = self._notifier,
        )

        self.log.info(
            f"{str(cfg.strategy_id):<20} started  mode={cfg.mode}  "
            f"primary={cfg.bar_type}  "
            f"htf={'off' if not cfg.htf_filter else str(cfg.bar_type_htf)}  "
            f"size={cfg.trade_size} BTC  "
            f"max_open={cfg.max_open_trades}  "
            f"daily_limit={cfg.daily_loss_limit_usdt:.2f} USDT"
        )

    def on_stop(self) -> None:
        if self.pm:
            self.pm.on_stop(reason="RESTART")

        self.state_store.save(self.ledger)
        self.ledger.print_summary(self.log)

        self.log.info(
            f"{str(self.config.strategy_id)} stopped. "
            f"State saved → {self.state_store.path}"
        )

    # ── Bar routing ───────────────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> None:
        cfg = self.config

        if cfg.htf_filter and bar.bar_type == cfg.bar_type_htf:
            self.htf.update(bar.close.as_double())
            return

        high  = bar.high.as_double()
        low   = bar.low.as_double()
        close = bar.close.as_double()
        ts    = bar.ts_init

        atr, raw_long, raw_short = self._process_primary_bar(
            high, low, close, self._bar_count
        )
        self._bar_count += 1

        if cfg.htf_filter:
            long_signal  = raw_long  and self.htf.bull
            short_signal = raw_short and self.htf.bear
        else:
            long_signal  = raw_long
            short_signal = raw_short

        # Stage 4: notify signal BEFORE pm.on_bar() so signal message
        # always arrives first, followed by "entry blocked" note if needed.
        if self._notifier is not None:
            if long_signal:
                try:
                    self._notifier.on_signal("LONG", close, str(cfg.strategy_id))
                except Exception as e:
                    self.log.warning(f"Notifier on_signal error: {e}")
            if short_signal:
                try:
                    self._notifier.on_signal("SHORT", close, str(cfg.strategy_id))
                except Exception as e:
                    self.log.warning(f"Notifier on_signal error: {e}")

        if self.pm is not None:
            self.pm.on_bar(
                high, low, close, atr, ts,
                long_signal  = long_signal,
                short_signal = short_signal,
            )
            # Crash safety: persist ledger if any trade state changed this bar.
            # Runs after ALL on_bar() logic completes so the saved snapshot is
            # always consistent (tp1_hit, new SL, best_price all included).
            if self.pm.flush_state():
                try:
                    self.state_store.save(self.ledger)
                except Exception as e:
                    self.log.error(f"State save failed after trade event: {e}")

    # ── Order submission closure ──────────────────────────────────────────
    def _make_submit_fn(self):
        def _submit(side: str, qty: Decimal) -> None:
            if self.config.mode == "dry_run":
                self.log.info(
                    f"DRY_RUN {side:<4}  {float(qty):.6f} BTC  "
                    f"[{str(self.config.strategy_id)}]"
                )
                return
            order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id = self.config.instrument_id,
                order_side    = order_side,
                quantity      = self.instrument.make_qty(qty),
                time_in_force = TimeInForce.GTC,
            )
            self.submit_order(order)
        return _submit