"""
strategies/fvg_strategy.py
────────────────────────────────────────────────────────────────────────
Stage 5: min_free_margin_usdt added to build_config().
All other logic unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from core.atr import ATR
from core.config import StrategySettingsBase
from core.fvg_zones import FVGZones
from strategies.base_smc_strategy import BaseSmcConfig, BaseSmcStrategy


@dataclass
class FvgSettings(StrategySettingsBase):
    """Fields that appear in the `fvg:` YAML block (common + FVG-specific)."""
    fvg_atr_len:      int
    fvg_atr_mult:     float
    fvg_max_zones:    int
    fvg_sig_lookback: int
    fvg_ifvg_enable:  bool
    fvg_sig_cooldown: int
    fvg_max_age:      int

    def build_config(
        self, strategy_id: str, instrument_id: InstrumentId, venue: str,
        position_mode: str,
        state_dir: str, mode: str,
        primary_bar: BarType, htf_bar: BarType,
    ) -> FvgStrategyConfig:
        return FvgStrategyConfig(
            strategy_id           = strategy_id,
            instrument_id         = instrument_id,
            venue                 = venue,
            position_mode         = position_mode,
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
            fvg_atr_len           = self.fvg_atr_len,
            fvg_atr_mult          = self.fvg_atr_mult,
            fvg_max_zones         = self.fvg_max_zones,
            fvg_sig_lookback      = self.fvg_sig_lookback,
            fvg_ifvg_enable       = self.fvg_ifvg_enable,
            fvg_sig_cooldown      = self.fvg_sig_cooldown,
            fvg_max_age           = self.fvg_max_age,
        )


class FvgStrategyConfig(BaseSmcConfig, frozen=True):
    fvg_atr_len:      int
    fvg_atr_mult:     float
    fvg_max_zones:    int
    fvg_sig_lookback: int
    fvg_ifvg_enable:  bool
    fvg_sig_cooldown: int
    fvg_max_age:      int


class FvgStrategy(BaseSmcStrategy):

    def __init__(self, config: FvgStrategyConfig) -> None:
        super().__init__(config)
        self.atr_ind: ATR      | None = None
        self.fvg:     FVGZones | None = None

    def _init_signal_modules(self) -> None:
        cfg = self.config
        self.atr_ind = ATR(period=cfg.fvg_atr_len)
        self.fvg     = FVGZones(
            atr_mult     = cfg.fvg_atr_mult,
            max_zones    = cfg.fvg_max_zones,
            sig_lookback = cfg.fvg_sig_lookback,
            ifvg_enable  = cfg.fvg_ifvg_enable,
            sig_cooldown = cfg.fvg_sig_cooldown,
            max_age      = cfg.fvg_max_age,
        )
        self.log.info(
            f"FVGZones ready  atr_len={cfg.fvg_atr_len}  "
            f"atr_mult={cfg.fvg_atr_mult:.2f}  max_zones={cfg.fvg_max_zones}  "
            f"ifvg={cfg.fvg_ifvg_enable}  cooldown={cfg.fvg_sig_cooldown}  "
            f"max_age={cfg.fvg_max_age}"
        )

    def _process_primary_bar(
        self, high: float, low: float, close: float, bar_idx: int,
    ) -> tuple[float, bool, bool]:
        self.atr_ind.update(high, low, close)
        atr = self.atr_ind.value
        self.fvg.update(high, low, close, atr)
        return atr, self.fvg.bull_signal, self.fvg.bear_signal

    def _log_warmup_health(self) -> None:
        atr = self.atr_ind.value if self.atr_ind is not None else 0.0
        if self.fvg is None:
            return
        bull_zones = sum(1 for z in self.fvg.zones if z.is_bull)
        bear_zones = len(self.fvg.zones) - bull_zones
        self.log.info(
            f"Post-warmup — ATR={atr:.1f}  "
            f"zones={len(self.fvg.zones)} ({bull_zones} bull / {bear_zones} bear)  "
            f"near: bull={self.fvg.bull_near} bear={self.fvg.bear_near}"
        )