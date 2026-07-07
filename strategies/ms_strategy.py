"""
strategies/ms_strategy.py
────────────────────────────────────────────────────────────────────────
Stage 5: min_free_margin_usdt added to build_config().
All other logic unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from core.config import StrategySettingsBase
from core.market_structure import MarketStructure
from strategies.base_smc_strategy import BaseSmcConfig, BaseSmcStrategy


@dataclass
class MsSettings(StrategySettingsBase):
    """Fields that appear in the `ms:` YAML block (common + MS-specific)."""
    swing_len: int
    atr_dist:  float
    atr_len:   int

    def build_config(
        self, strategy_id: str, instrument_id: InstrumentId, venue: str,
        state_dir: str, mode: str,
        primary_bar: BarType, htf_bar: BarType,
    ) -> MsStrategyConfig:
        return MsStrategyConfig(
            strategy_id           = strategy_id,
            instrument_id         = instrument_id,
            venue                 = venue,
            bar_type              = primary_bar,
            bar_type_htf          = htf_bar,
            state_dir             = state_dir,
            mode                  = mode,
            trade_size            = Decimal(str(self.trade_size)),
            sl_atr                = self.sl_atr,
            tp1_atr               = self.tp1_atr,
            tp2_atr               = self.tp2_atr,
            trailing_tp2          = self.trailing_tp2,
            trail_atr_mult        = self.trail_atr_mult,
            breakeven_sl          = self.breakeven_sl,
            enable_exit_signal    = self.enable_exit_signal,
            enable_sl             = self.enable_sl,
            max_open_trades       = self.max_open_trades,
            daily_loss_limit_usdt = self.daily_loss_limit_usdt,
            min_free_margin_usdt  = self.min_free_margin_usdt,   # Stage 5
            warmup_bars           = self.warmup_bars,
            htf_warmup_bars       = self.htf_warmup_bars,
            htf_filter            = self.htf_filter,
            htf_period            = self.htf_period,
            swing_len             = self.swing_len,
            atr_dist              = self.atr_dist,
            atr_len               = self.atr_len,
        )


class MsStrategyConfig(BaseSmcConfig, frozen=True):
    swing_len: int
    atr_dist:  float
    atr_len:   int


class MsStrategy(BaseSmcStrategy):

    def __init__(self, config: MsStrategyConfig) -> None:
        super().__init__(config)
        self.ms: MarketStructure | None = None

    def _init_signal_modules(self) -> None:
        cfg = self.config
        self.ms = MarketStructure(
            swing_len = cfg.swing_len,
            atr_dist  = cfg.atr_dist,
            atr_len   = cfg.atr_len,
        )
        self.log.info(
            f"MarketStructure ready  swing_len={cfg.swing_len}  "
            f"atr_dist={cfg.atr_dist:.2f}  atr_len={cfg.atr_len}"
        )

    def _process_primary_bar(
        self, high: float, low: float, close: float, bar_idx: int,
    ) -> tuple[float, bool, bool]:
        self.ms.update(high, low, close, bar_idx)
        return self.ms.atr, self.ms.momentum_long, self.ms.momentum_short

    def _log_warmup_health(self) -> None:
        if self.ms is not None:
            self.log.info(
                f"Post-warmup — ATR={self.ms.atr:.1f}  "
                f"momentum_long={self.ms.momentum_long}  "
                f"momentum_short={self.ms.momentum_short}"
            )