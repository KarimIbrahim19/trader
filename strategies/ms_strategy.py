"""
strategies/ms_strategy.py
────────────────────────────────────────────────────────────────────────
Market Structure strategy — wires the MarketStructure signal module
into the BaseSmcStrategy framework.

This file contains only what is unique to MS:
  • MsStrategyConfig  — adds swing_len / atr_dist / atr_len fields
  • MsStrategy        — creates MarketStructure in _init_signal_modules(),
                        feeds it bars in _process_primary_bar()

Everything else (bar routing, HTF gate, position management, risk
toggles, order submission, persistence, summary logging) lives in
BaseSmcStrategy and is inherited unchanged.

Enabling / disabling MS:  set strategies.ms.enabled in settings.yaml.
Changing bar type or risk: edit the ms block in settings.yaml.
No code changes needed for either.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from core.config import StrategySettingsBase
from core.market_structure import MarketStructure
from strategies.base_smc_strategy import BaseSmcConfig, BaseSmcStrategy


# ── YAML settings ─────────────────────────────────────────────────────────
@dataclass
class MsSettings(StrategySettingsBase):
    """Fields that appear in the `ms:` YAML block (common + MS-specific)."""
    swing_len: int
    atr_dist:  float
    atr_len:   int

    def build_config(
        self, strategy_id: str, instrument_id: InstrumentId,
        state_dir: str, mode: str,
        primary_bar: BarType, htf_bar: BarType,
    ) -> MsStrategyConfig:
        return MsStrategyConfig(
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
            swing_len        = self.swing_len,
            atr_dist         = self.atr_dist,
            atr_len          = self.atr_len,
        )


# ── NT config ──────────────────────────────────────────────────────────────
class MsStrategyConfig(BaseSmcConfig, frozen=True):
    """
    MS-specific signal parameters on top of the shared risk / bar config.
    All values provided by MsSettings.build_config() at startup.
    """
    swing_len: int
    atr_dist:  float
    atr_len:   int


# ── Strategy ──────────────────────────────────────────────────────────────
class MsStrategy(BaseSmcStrategy):
    """
    Live Market Structure strategy.
    """

    def __init__(self, config: MsStrategyConfig) -> None:
        super().__init__(config)
        self.ms: MarketStructure | None = None

    # ── Abstract implementations ──────────────────────────────────────────
    def _init_signal_modules(self) -> None:
        """Create the MarketStructure engine with config parameters."""
        cfg = self.config
        self.ms = MarketStructure(
            swing_len  = cfg.swing_len,
            atr_dist   = cfg.atr_dist,
            atr_len    = cfg.atr_len,
        )
        self.log.info(
            f"MarketStructure ready  swing_len={cfg.swing_len}  "
            f"atr_dist={cfg.atr_dist:.2f}  atr_len={cfg.atr_len}"
        )

    def _process_primary_bar(
        self,
        high:    float,
        low:     float,
        close:   float,
        bar_idx: int,
    ) -> tuple[float, bool, bool]:
        """
        Update MarketStructure for one 15m bar.
        Returns (atr, momentum_long, momentum_short).

        ATR comes from ms.atr — the same Wilder ATR embedded in the MS
        engine that sizes its own pivot distance filter. Using the same
        ATR source for SL/TP sizing keeps the system consistent with
        the Pine Script original and the backtest scripts.
        """
        self.ms.update(high, low, close, bar_idx)
        return self.ms.atr, self.ms.momentum_long, self.ms.momentum_short
