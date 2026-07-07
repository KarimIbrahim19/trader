"""
strategies/base_smc_strategy.py
────────────────────────────────────────────────────────────────────────
Stage 6 additions:
  • set_reconciler(reconciler) — called from main.py before node.build().
    Stores the shared LedgerReconciler instance.

  • on_start(): registers this strategy's ledger and portfolio_fn with
    the reconciler. Runs a startup reconciliation check (6D) if open
    trades were restored from persistence — by on_start() time NT has
    already queried the exchange via ExecMassStatus.

  • on_bar(): two additions around the existing signal/PM logic:
      1. Before signals: call reconciler.check() — skips gracefully if
         within grace period or if portfolio not ready.
      2. After pm.flush_state(): call reconciler.record_mutation() so
         the grace period starts from the bar where the trade event happened.
      3. Suppress long_signal/short_signal if reconciler.is_halted (Case B).

  • _make_position_fn(): returns a closure that reads the signed net
    position (in this strategy's own instrument) from NT's portfolio.
    Positive = net long, negative = net short, 0 = flat. Returns None
    when portfolio isn't ready yet.

  • BaseSmcConfig: no new fields — reconciler is injected, not configured.

Phase 1 warmup, Stage 5 order handlers, crash-safety dirty flag —
all unchanged.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Optional

from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.data import Data
from nautilus_trader.model.data import Bar, BarAggregation, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.trading.strategy import Strategy

from core.exchanges import get_adapter
from core.htf_bias import HTFBias
from persistence.state_store import StateStore
from risk.position_manager import PositionManager, PositionManagerConfig
from risk.trade_ledger import TradeLedger


_BAR_STEP_SECONDS = {
    BarAggregation.SECOND: 1,
    BarAggregation.MINUTE: 60,
    BarAggregation.HOUR:   3600,
    BarAggregation.DAY:    86400,
}


# ── Base config ───────────────────────────────────────────────────────────
class BaseSmcConfig(StrategyConfig, frozen=True):
    instrument_id:         InstrumentId
    venue:                 str    # e.g. "binance" -- key into core.exchanges.get_adapter()
    bar_type:              BarType
    bar_type_htf:          BarType
    mode:                  str
    state_dir:             str
    htf_filter:            bool
    htf_period:            int
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
    min_free_margin_usdt:  float
    warmup_bars:           int
    htf_warmup_bars:       int


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

        self._bar_count:     int = 0
        self.pm:             PositionManager | None = None
        self.instrument      = None
        self._symbol:        str = ""   # set properly in on_start() once instrument_id is known
        self._notifier       = None
        self._strategy_name  = ""
        self._reconciler     = None    # Stage 6: shared LedgerReconciler

        # Phase 1: warmup state
        self._warmup_done:      bool         = False
        self._warmup_pending:   int          = 0
        self._warmup_bar_types: set[BarType] = set()
        self._warmup_buffer:    dict[int, Bar] = {}

        # Stage 5: maps NT client_order_id → list of trade_ids for rejection handling
        self._order_to_trade: dict[str, list[int]] = {}

        # Exchange filter fallback (from settings.yaml, used if API fetch fails)
        self._exchange_filters_fallback: dict = {}

    # ── Wiring (called from main.py before node.build()) ──────────────────
    def set_notifier(self, notifier, strategy_name: str = "") -> None:
        self._notifier      = notifier
        self._strategy_name = strategy_name

    def set_reconciler(self, reconciler) -> None:
        """
        Inject the shared LedgerReconciler. Called from main.py after
        strategy construction, before node.build(). The reconciler is
        a LedgerReconciler but typed as object to avoid circular imports.
        """
        self._reconciler = reconciler

    def set_exchange_filters(self, filters: dict) -> None:
        """
        Inject per-symbol exchange filter fallback values from
        settings.yaml's ``exchange_filters`` block.  Used only when
        the direct Binance API fetch (GET /fapi/v1/exchangeInfo)
        fails at startup.
        """
        self._exchange_filters_fallback = filters

    # ── Abstract interface ────────────────────────────────────────────────
    def _init_signal_modules(self) -> None:
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement _init_signal_modules()"
        )

    def _process_primary_bar(
        self, high: float, low: float, close: float, bar_idx: int,
    ) -> tuple[float, bool, bool]:
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement _process_primary_bar()"
        )

    def _log_warmup_health(self) -> None:
        pass

    # ── Exchange filter fetch ─────────────────────────────────────────────
    def _fetch_exchange_filters(self) -> tuple[Decimal, float]:
        """
        Fetch MARKET_LOT_SIZE.minQty and MIN_NOTIONAL.notional for this
        strategy's instrument via its venue's adapter (e.g. Binance's
        GET /fapi/v1/exchangeInfo -- see core/exchanges/binance.py).

        Returns (min_qty, min_notional).
        Falls back to self._exchange_filters_fallback if the adapter
        can't fetch it (or in dry_run, where nothing is queried).
        """
        symbol = str(self.config.instrument_id).split("-")[0]  # "BTCUSDT"

        if self.config.mode != "dry_run":
            adapter = get_adapter(self.config.venue)
            fetched = adapter.fetch_exchange_filters(
                symbol, is_paper=(self.config.mode == "paper"),
            )
            if fetched is not None:
                min_qty, min_notional = fetched
                self.log.info(
                    f"Exchange filters fetched  venue={self.config.venue}  symbol={symbol}  "
                    f"min_qty={min_qty}  min_notional=${min_notional:.2f}"
                )
                return min_qty, min_notional
            self.log.warning(
                f"Exchange filter fetch failed for {self.config.venue}:{symbol}  "
                "falling back to config"
            )

        # Fallback
        fb = self._exchange_filters_fallback
        mls_fb = fb.get("market_lot_size", {})
        mn_fb  = fb.get("min_notional", {})
        min_qty      = Decimal(str(mls_fb.get("min_qty", "0.001")))
        min_notional = float(mn_fb.get("notional", "50"))
        self.log.info(
            f"Exchange filters (config fallback)  symbol={symbol}  "
            f"min_qty={min_qty}  min_notional=${min_notional:.2f}"
        )
        return min_qty, min_notional

    # ── NT lifecycle ──────────────────────────────────────────────────────
    def on_start(self) -> None:
        cfg = self.config
        self.instrument = self.cache.instrument(cfg.instrument_id)
        if self.instrument is None:
            self.log.error(
                f"Instrument {cfg.instrument_id} not found in cache — "
                "set instrument_provider load_all=True in node_builder.py. "
                "Strategy will not trade until this is fixed."
            )
            return
        # e.g. "BTCUSDT" -- used only for log/notification labeling, so a
        # reader can tell which symbol a message is about once more than
        # one is running.
        self._symbol = str(cfg.instrument_id).split("-")[0]
        self._init_signal_modules()

        # ── Phase 1 warmup ────────────────────────────────────────────
        self._warmup_pending = 0
        self._warmup_bar_types.clear()
        self._warmup_buffer.clear()
        if cfg.warmup_bars > 0:
            self._warmup_bar_types.add(cfg.bar_type)
            spec      = cfg.bar_type.spec
            step_secs = _BAR_STEP_SECONDS.get(spec.aggregation, 60) * spec.step
            slot_count = cfg.warmup_bars + 1
            start = self.clock.utc_now() - timedelta(seconds=step_secs * slot_count)
            self._warmup_pending += 1
            self.request_bars(
                cfg.bar_type, start,
                limit=slot_count, callback=self._on_warmup_done,
            )
        if cfg.htf_filter and cfg.htf_warmup_bars > 0:
            self._warmup_bar_types.add(cfg.bar_type_htf)
            spec      = cfg.bar_type_htf.spec
            step_secs = _BAR_STEP_SECONDS.get(spec.aggregation, 60) * spec.step
            slot_count = cfg.htf_warmup_bars + 1
            start = self.clock.utc_now() - timedelta(seconds=step_secs * slot_count)
            self._warmup_pending += 1
            self.request_bars(
                cfg.bar_type_htf, start,
                limit=slot_count, callback=self._on_warmup_done,
            )
        if self._warmup_pending == 0:
            self._subscribe_live()
        else:
            self.clock.set_timer(
                name     = f"warmup_timeout_{cfg.strategy_id}",
                interval = timedelta(seconds=60),
                callback = self._on_warmup_timeout,
            )

        # Register ledger with notifier for heartbeat/daily summary
        if self._notifier is not None:
            self._notifier.register_ledger(
                self._strategy_name or "unknown", self.ledger
            )

        # ── Restore persisted open trades ─────────────────────────────
        saved = self.state_store.load()
        if saved:
            open_trades, next_id, order_to_trade = saved
            self.ledger.restore_from_persistence(open_trades, next_id)
            self._order_to_trade = order_to_trade
            if cfg.mode != "dry_run":
                self.log.warning(
                    f"⚠ {len(open_trades)} open trade(s) restored from state. "
                    "Verify these match your exchange positions."
                )
                self._log_reconciliation()
            if self._notifier is not None:
                try:
                    self._notifier.on_state_restored(
                        str(cfg.strategy_id), len(open_trades)
                    )
                except Exception as e:
                    self.log.warning(f"Notifier on_state_restored error: {e}")

        # ── Stage 6: register with reconciler ─────────────────────────
        # on_start() is called AFTER NT's ExecMassStatus reconciliation,
        # so the portfolio already reflects the real exchange state.
        # Grouped by (venue, instrument_id) -- strategies sharing that
        # pair (e.g. MS + FVG both on binance:BTCUSDT) share one group;
        # strategies on a different symbol or venue get their own.
        if self._reconciler is not None and cfg.mode != "dry_run":
            self._reconciler.register_strategy(
                self._strategy_name or "unknown",
                self.ledger,
                cfg.venue,
                cfg.instrument_id,
            )
            # First strategy in a group to call this wins — all
            # strategies in that group share the same NT portfolio
            # position for that (venue, instrument) pair.
            self._reconciler.set_portfolio_fn(
                cfg.venue, cfg.instrument_id, self._make_position_fn()
            )

            # 6D: startup check if restored trades exist
            if saved and len(self.ledger.open_trades) > 0:
                ts_ns = int(self.clock.utc_now().timestamp() * 1e9)
                result = self._reconciler.check(cfg.venue, cfg.instrument_id, ts_ns, self.log)
                if result.checked and result.case not in ("ok", None):
                    self.log.warning(
                        f"Startup reconciliation: Case {result.case}  "
                        f"expected={result.expected:+.4f}  "
                        f"actual={result.actual:+.4f}  "
                        f"diff={result.diff:+.4f}"
                    )

        # ── Fetch exchange filters → PositionManager config ───────────
        min_qty, min_notional = self._fetch_exchange_filters()

        balance_fn = (
            None if cfg.mode == "dry_run"
            else self._make_balance_check_fn()
        )
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
            min_free_margin_usdt  = cfg.min_free_margin_usdt,
            min_qty               = min_qty,
            min_notional          = min_notional,
        )
        self.pm = PositionManager(
            config             = pm_cfg,
            ledger             = self.ledger,
            submit_order_fn    = self._make_submit_fn(),
            log                = self.log,
            strategy_id        = str(cfg.strategy_id),
            symbol             = self._symbol,
            notifier           = self._notifier,
            on_order_submitted = self._make_on_order_submitted(),
            balance_check_fn   = balance_fn,
        )

        self.log.info(
            f"{str(cfg.strategy_id):<20} started  mode={cfg.mode}  "
            f"venue={cfg.venue}  symbol={self._symbol}  "
            f"primary={cfg.bar_type}  "
            f"htf={'off' if not cfg.htf_filter else str(cfg.bar_type_htf)}  "
            f"size={cfg.trade_size} {self._symbol}  max_open={cfg.max_open_trades}  "
            f"daily_limit={cfg.daily_loss_limit_usdt:.2f}  "
            f"margin_gate={cfg.min_free_margin_usdt:.2f} USDT  "
            f"reconciler={'on' if self._reconciler else 'off'}"
        )

    # ── Warmup ────────────────────────────────────────────────────────────
    def on_historical_data(self, data: Data) -> None:
        if not isinstance(data, Bar):
            return
        if data.bar_type not in self._warmup_bar_types:
            return
        self._warmup_buffer[data.ts_init] = data

    def _on_warmup_done(self, client_id) -> None:
        self._warmup_pending -= 1
        if self._warmup_pending > 0:
            return
        self._warmup_bar_types.clear()
        cfg = self.config
        self._bar_count = 0
        for bar in sorted(self._warmup_buffer.values(), key=lambda b: b.ts_init):
            if bar.bar_type == cfg.bar_type:
                self._process_primary_bar(
                    bar.high.as_double(), bar.low.as_double(),
                    bar.close.as_double(), self._bar_count,
                )
                self._bar_count += 1
            elif cfg.htf_filter and bar.bar_type == cfg.bar_type_htf:
                self.htf.update(bar.close.as_double())
        self._warmup_buffer.clear()
        self._subscribe_live()
        self.log.info(f"Warmup complete — {self._bar_count} bars processed")

    def _subscribe_live(self) -> None:
        if self._warmup_done:
            return
        cfg = self.config
        self._warmup_done = True
        try:
            self.clock.cancel_timer(f"warmup_timeout_{cfg.strategy_id}")
        except Exception:
            pass
        self.subscribe_bars(cfg.bar_type)
        if cfg.htf_filter:
            self.subscribe_bars(cfg.bar_type_htf)

    def _on_warmup_timeout(self, event) -> None:
        if not self._warmup_done:
            self.log.warning(
                "Warmup timeout — subscribing to live bars without full warmup. "
                "First signals may be unreliable."
            )
            self._subscribe_live()

    def on_stop(self) -> None:
        if self.pm:
            self.pm.on_stop(reason="RESTART")
        self.state_store.save(self.ledger, self._order_to_trade)
        self.ledger.print_summary(self.log)
        self.log.info(
            f"{str(self.config.strategy_id)} stopped. "
            f"State saved → {self.state_store.path}"
        )

    # ── Bar routing ───────────────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> None:
        if not self._warmup_done:
            return

        cfg = self.config

        if cfg.htf_filter and bar.bar_type == cfg.bar_type_htf:
            self.htf.update(bar.close.as_double())
            return

        # ts declared early so record_mutation can use it after pm.on_bar()
        ts    = bar.ts_init
        high  = bar.high.as_double()
        low   = bar.low.as_double()
        close = bar.close.as_double()

        # ── Stage 6: reconciliation — before signal/SL/TP logic ───────
        # Runs after all fills from the previous bar are settled.
        # Skips silently if within grace period or portfolio not ready.
        # Scoped to this strategy's own (venue, instrument) group.
        if self._reconciler is not None:
            self._reconciler.check(cfg.venue, cfg.instrument_id, ts, self.log)

        atr, raw_long, raw_short = self._process_primary_bar(
            high, low, close, self._bar_count
        )
        self._bar_count += 1

        if self._bar_count % 10 == 0:
            self.log.info(
                f"Bar #{self._bar_count}  ts={ts}  H={high:.1f} L={low:.1f} C={close:.1f}  "
                f"atr={atr:.2f}  raw={'L' if raw_long else ''}{'S' if raw_short else ''}"
            )

        if cfg.htf_filter:
            long_signal  = raw_long  and self.htf.bull
            short_signal = raw_short and self.htf.bear
        else:
            long_signal  = raw_long
            short_signal = raw_short

        # ── Stage 6: suppress new entries if this group is halted (Case B)
        # Only halts entries for this strategy's own (venue, instrument)
        # group -- a halt on one symbol/venue doesn't affect others.
        if self._reconciler is not None and self._reconciler.is_halted(cfg.venue, cfg.instrument_id):
            long_signal  = False
            short_signal = False

        # Notify signal before pm.on_bar() so signal message arrives first
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
                long_signal=long_signal, short_signal=short_signal,
            )
            # Crash-safe persist after all bar logic completes
            if self.pm.flush_state():
                try:
                    self.state_store.save(self.ledger, self._order_to_trade)
                except Exception as e:
                    self.log.error(f"State save failed after trade event: {e}")
                # Stage 6: start grace period from this bar's timestamp,
                # scoped to this strategy's (venue, instrument) group
                if self._reconciler is not None:
                    self._reconciler.record_mutation(cfg.venue, cfg.instrument_id, ts)

    # ── Stage 5: order event handlers ─────────────────────────────────────
    def on_order_rejected(self, event) -> None:
        client_id = str(event.client_order_id)
        trade_ids = self._order_to_trade.pop(client_id, None)

        if trade_ids is not None:
            for trade_id in trade_ids:
                # Close order rejection — trade may be in closed_trades
                ctrade = next(
                    (t for t in self.ledger.closed_trades if t.trade_id == trade_id), None
                )
                if ctrade is not None:
                    ctrade.realized_pnl -= ctrade._pending_close_pnl
                    ctrade._pending_close_pnl = 0.0
                    ctrade.exit_ts = None
                    ctrade.exit_reason = ""
                    self.ledger.closed_trades = [
                        t for t in self.ledger.closed_trades if t.trade_id != trade_id
                    ]
                    self.ledger.open_trades.append(ctrade)
                    self.log.warning(
                        f"CLOSE ORDER REVERTED  #{trade_id:05d}  "
                        f"reason='{event.reason}'  "
                        f"trade returned to open_trades — will retry next bar"
                    )
                    if self._notifier is not None:
                        try:
                            self._notifier.on_close_reverted(
                                str(self.config.strategy_id), trade_id, str(event.reason)
                            )
                        except Exception as e:
                            self.log.warning(f"Notifier on_close_reverted error: {e}")
                else:
                    # Partial close revert (e.g., TP1) — trade is still in open_trades
                    ptrade = next(
                        (t for t in self.ledger.open_trades
                         if t.trade_id == trade_id and t._pending_close_pnl != 0.0),
                        None,
                    )
                    if ptrade is not None:
                        ptrade.realized_pnl -= ptrade._pending_close_pnl
                        ptrade._pending_close_pnl = 0.0
                        ptrade.tp1_hit = False
                        if ptrade._pre_tp1_sl is not None:
                            ptrade.sl = ptrade._pre_tp1_sl
                            ptrade._pre_tp1_sl = None
                        ptrade.best_price = None
                        ptrade.trail_distance = None
                        self.log.warning(
                            f"PARTIAL CLOSE REVERTED  #{trade_id:05d}  "
                            f"reason='{event.reason}'  "
                            f"tp1_hit reset to False  sl restored to {ptrade.sl:.2f}  "
                            f"will retry next bar"
                        )
                        if self._notifier is not None:
                            try:
                                self._notifier.on_close_reverted(
                                    str(self.config.strategy_id), trade_id, str(event.reason)
                                )
                            except Exception as e:
                                self.log.warning(f"Notifier on_close_reverted error: {e}")
                        continue

                    # Entry order rejection — remove trade from open_trades
                    before = len(self.ledger.open_trades)
                    self.ledger.open_trades = [
                        t for t in self.ledger.open_trades if t.trade_id != trade_id
                    ]
                    removed = before - len(self.ledger.open_trades)
                    self.log.error(
                        f"ENTRY REJECTED  #{trade_id:05d}  "
                        f"reason='{event.reason}'  "
                        f"{'removed from ledger' if removed else 'WARNING: not found in ledger'}"
                    )
                    if self._notifier is not None:
                        try:
                            self._notifier.on_order_rejected(
                                str(self.config.strategy_id), trade_id, str(event.reason)
                            )
                        except Exception as e:
                            self.log.warning(f"Notifier on_order_rejected error: {e}")
            try:
                self.state_store.save(self.ledger, self._order_to_trade)
            except Exception as e:
                self.log.error(f"State save failed after rejection: {e}")
        else:
            self.log.error(
                f"CLOSE ORDER REJECTED!  "
                f"client_order_id={client_id}  "
                f"reason='{event.reason}'  "
                f"POSITION MAY BE UNPROTECTED — check exchange immediately!"
            )
            if self._notifier is not None:
                try:
                    self._notifier.on_close_order_rejected(
                        str(self.config.strategy_id), client_id, str(event.reason)
                    )
                except Exception as e:
                    self.log.warning(f"Notifier on_close_order_rejected error: {e}")

    def on_order_filled(self, event) -> None:
        """
        Log fill for the consolidated order.
        No ledger mutations — entry_price stays at signal price for
        backtest parity. Slippage attribution is per-trade if multiple
        trades are associated with this consolidated order.
        """
        client_id  = str(event.client_order_id)
        fill_price = float(event.last_px)
        trade_ids  = self._order_to_trade.pop(client_id, None)
        if not trade_ids:
            return
        # Log one fill line — per-trade slippage attribution
        first_trade = next(
            (t for t in self.ledger.open_trades if t.trade_id == trade_ids[0]), None
        )
        entry_ref = first_trade.entry_price if first_trade else 0.0
        slippage  = fill_price - entry_ref
        self.log.info(
            f"FILL (consolidated)  trades={trade_ids}  "
            f"fill_px={fill_price:.2f}  "
            f"slippage_from_ref={slippage:+.4f}"
        )

    # ── Stage 5 + 6: closures ─────────────────────────────────────────────
    def _make_submit_fn(self):
        def _submit(side: str, qty: Decimal, reduce_only: bool = False) -> Optional[str]:
            if self.config.mode == "dry_run":
                self.log.info(
                    f"DRY_RUN {side:<4}  {float(qty):.6f} {self._symbol}  "
                    f"[{str(self.config.strategy_id)}]"
                )
                return None
            order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id = self.config.instrument_id,
                order_side    = order_side,
                quantity      = self.instrument.make_qty(qty),
                time_in_force = TimeInForce.GTC,
                reduce_only   = reduce_only,
            )
            self.submit_order(order)
            return str(order.client_order_id)
        return _submit

    def _make_on_order_submitted(self):
        def _callback(trade_ids: list[int], client_order_id: str) -> None:
            if client_order_id:
                self._order_to_trade[client_order_id] = trade_ids
                self.log.debug(
                    f"Order submitted  trades={trade_ids}  coid={client_order_id}"
                )
        return _callback

    def _make_balance_check_fn(self):
        """
        Returns free margin currency balance from NT portfolio, for this
        strategy's own venue and instrument (quote currency -- e.g. USDT
        for a *USDT-margined symbol, but derived from the instrument
        rather than hardcoded so a non-USDT-margined symbol on a future
        venue doesn't silently break the gate).
        Used only in paper/live mode — dry_run receives None instead.
        Returns float('inf') when account data isn't available yet
        (e.g. early in startup) so the margin gate never fires spuriously.
        """
        venue = Venue(get_adapter(self.config.venue).venue_name)

        def _check() -> float:
            try:
                account = self.portfolio.account(venue)
                if account is None or self.instrument is None:
                    return float("inf")
                bal = account.balance_free(self.instrument.quote_currency)
                return float(bal.as_double()) if bal is not None else 0.0
            except Exception:
                return float("inf")
        return _check

    def _make_position_fn(self):
        """
        Stage 6: Returns the signed net position (in this strategy's own
        instrument) from NT's portfolio.
        Positive = net long, negative = net short, 0.0 = flat.
        Returns None when the portfolio isn't ready (skip check).
        """
        def _get_net() -> Optional[float]:
            try:
                pos = self.portfolio.net_position(self.config.instrument_id)
                return float(pos) if pos is not None else 0.0
            except Exception:
                return None   # not ready yet — reconciler will skip
        return _get_net

    def _log_reconciliation(self) -> None:
        """Manual-check quality log. Stage 6 adds automated comparison."""
        sep = "═" * 55
        self.log.warning(sep)
        self.log.warning(f"RECONCILIATION CHECK — {self.config.strategy_id}  ({self._symbol})")
        self.log.warning("Trades restored from persistence:")
        for t in self.ledger.open_trades:
            remaining = float(t.full_qty) * (0.5 if t.tp1_hit else 1.0)
            self.log.warning(
                f"  #{t.trade_id:05d} {t.side:<5}  "
                f"{remaining:.4f} {self._symbol}  entry={t.entry_price:.2f}  "
                f"sl={t.sl:.2f}  tp1_hit={t.tp1_hit}  "
                f"pnl_so_far={t.realized_pnl:+.2f}"
            )
        self.log.warning(sep)
