"""
strategies/fvg_strategy.py
────────────────────────────────────────────────────────────────────────
Fair Value Gap strategy — wires the FVGZones signal module (signal mode)
into the BaseSmcStrategy framework.

This file contains only what is unique to FVG:
  • FvgStrategyConfig  — adds all FVG zone tuning parameters
  • FvgStrategy        — creates ATR + FVGZones in _init_signal_modules(),
                         feeds them bars in _process_primary_bar()

Everything else (bar routing, HTF gate, position management, risk
toggles, order submission, persistence, summary logging) lives in
BaseSmcStrategy and is inherited unchanged.

Signal mode:  fvg.bull_signal / fvg.bear_signal
  Fires on the exact bar price bounces out of an active FVG zone.
  This is the standalone entry mode — NOT filter mode (long_filter /
  short_filter). Filter mode is for use as a confluence gate on top of
  another signal, which is Layer 3 of the backtest stack and is not
  yet validated as a standalone entry.

Enabling / disabling FVG:  set strategies.fvg.enabled in settings.yaml.
Changing params:            edit the fvg block in settings.yaml.
No code changes needed for either.
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


# ── YAML settings ─────────────────────────────────────────────────────────
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
        self, strategy_id: str, instrument_id: InstrumentId,
        state_dir: str, mode: str,
        primary_bar: BarType, htf_bar: BarType,
    ) -> FvgStrategyConfig:
        return FvgStrategyConfig(
            strategy_id      = strategy_id,
            instrument_id    = instrument_id,
            bar_type         = primary_bar,
            bar_type_htf     = htf_bar,
            state_dir        = state_dir,
            mode             = mode,
            trade_size       = Decimal(str(self.trade_size)),
            sl_atr           = self.sl_atr,
            tp1_atr          = self.tp1_atr,
            tp2_atr          = self.tp2_atr,
            trailing_tp2     = self.trailing_tp2,
            trail_atr_mult   = self.trail_atr_mult,
            breakeven_sl     = self.breakeven_sl,
            enable_exit_signal = self.enable_exit_signal,
            enable_sl        = self.enable_sl,
            max_open_trades  = self.max_open_trades,
            daily_loss_limit_usdt = self.daily_loss_limit_usdt,
            htf_filter       = self.htf_filter,
            htf_period       = self.htf_period,
            fvg_atr_len      = self.fvg_atr_len,
            fvg_atr_mult     = self.fvg_atr_mult,
            fvg_max_zones    = self.fvg_max_zones,
            fvg_sig_lookback = self.fvg_sig_lookback,
            fvg_ifvg_enable  = self.fvg_ifvg_enable,
            fvg_sig_cooldown = self.fvg_sig_cooldown,
            fvg_max_age      = self.fvg_max_age,
        )


# ── NT config ──────────────────────────────────────────────────────────────
class FvgStrategyConfig(BaseSmcConfig, frozen=True):
    """
    FVG-specific signal parameters on top of the shared risk / bar config.
    All values provided by FvgSettings.build_config() at startup.
    """
    fvg_atr_len:      int
    fvg_atr_mult:     float
    fvg_max_zones:    int
    fvg_sig_lookback: int
    fvg_ifvg_enable:  bool
    fvg_sig_cooldown: int
    fvg_max_age:      int


# ── Strategy ──────────────────────────────────────────────────────────────
class FvgStrategy(BaseSmcStrategy):
    """
    Live Fair Value Gap strategy (signal mode).
    """

    def __init__(self, config: FvgStrategyConfig) -> None:
        super().__init__(config)
        self.atr_ind: ATR      | None = None
        self.fvg:     FVGZones | None = None

    # ── Abstract implementations ──────────────────────────────────────────
    def _init_signal_modules(self) -> None:
        """Create ATR and FVGZones engines with config parameters."""
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
            f"FVGZones ready  atr_len={cfg.fvg_atr_len}  atr_mult={cfg.fvg_atr_mult:.2f}  "
            f"max_zones={cfg.fvg_max_zones}  ifvg={cfg.fvg_ifvg_enable}  "
            f"cooldown={cfg.fvg_sig_cooldown}  max_age={cfg.fvg_max_age}"
        )

    def _process_primary_bar(
        self,
        high:    float,
        low:     float,
        close:   float,
        bar_idx: int,   # unused by FVG but required by the interface
    ) -> tuple[float, bool, bool]:
        """
        Update ATR and FVGZones for one primary bar.
        Returns (atr, bull_signal, bear_signal).
        """
        self.atr_ind.update(high, low, close)
        atr = self.atr_ind.value
        self.fvg.update(high, low, close, atr)
        return atr, self.fvg.bull_signal, self.fvg.bear_signal
