"""
market_structure.py
──────────────────────────────────────────────────────────────────────
Pure Python port of the Pine Script v6 Market Structure module.

No NautilusTrader imports — can be unit-tested independently.

Features (matching the Pine Script exactly):
  • Pivot detection with swing_len lookback (mirrors ta.pivothigh/low)
  • ATR distance filter — rejects pivots too close to the last opposite
  • HH / LH / HL / LL classification
  • Two-stage confirmation: CHoCH fires first, then waits for BOS
  • Watch-level logic: in a bullish trend only track HIGHER swing highs
  • Momentum signals: LH→HH + active bull CHoCH  |  HL→LL + bear CHoCH

Usage:
    ms = MarketStructure(swing_len=10, atr_dist=0.5)
    for i, bar in enumerate(bars):
        ms.update(bar.high, bar.low, bar.close, bar_idx=i)
        if ms.momentum_long:  ...
        if ms.momentum_short: ...
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class MsEvent:
    bar_idx: int
    level:   float
    is_bull: bool   # True = bullish break
    is_bos:  bool   # True = BOS  |  False = CHoCH


class MarketStructure:

    def __init__(
        self,
        swing_len:  int   = 10,
        atr_dist:   float = 0.5,
        max_events: int   = 10,
        atr_len:    int   = 14,
    ) -> None:
        self.swing_len  = swing_len
        self.atr_dist   = atr_dist
        self.max_events = max_events
        self.atr_len    = atr_len

        # ── Trend state ──────────────────────────────────────────────
        self.trend:   int = 0   # 1=bull | -1=bear | 0=undefined
        self.pending: int = 0   # 1=pending bull | -1=pending bear

        # ── Swing points: list of (price, bar_idx), oldest → newest ─
        self.swing_highs: list[tuple[float, int]] = []
        self.swing_lows:  list[tuple[float, int]] = []

        # ── Watch levels — break of these confirms BOS or CHoCH ──────
        self.watch_high:     Optional[float] = None
        self.watch_high_bar: Optional[int]   = None
        self.watch_low:      Optional[float] = None
        self.watch_low_bar:  Optional[int]   = None

        # ── Confirmed events log ──────────────────────────────────────
        self.events: list[MsEvent] = []

        # ── Rolling OHLC buffers for pivot detection ──────────────────
        self._highs:    list[float] = []
        self._lows:     list[float] = []
        self._bar_idxs: list[int]   = []

        # ── Wilder's ATR ──────────────────────────────────────────────
        self._prev_close: Optional[float] = None
        self._tr_buf:     list[float]     = []
        self.atr: float = 0.0

        # ── Per-bar output flags (reset on every update()) ────────────
        self.bull_bos:       bool = False
        self.bear_bos:       bool = False
        self.bull_choch:     bool = False
        self.bear_choch:     bool = False
        self.momentum_long:  bool = False
        self.momentum_short: bool = False

    # ── ATR (Wilder's smoothing) ──────────────────────────────────────
    def _update_atr(self, high: float, low: float, close: float) -> None:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low  - self._prev_close),
            )
        self._tr_buf.append(tr)
        self._prev_close = close

        n = self.atr_len
        count = len(self._tr_buf)
        if count < n:
            self.atr = sum(self._tr_buf) / count          # seed with SMA
        elif count == n:
            self.atr = sum(self._tr_buf) / n              # first full value
        else:
            self.atr = (self.atr * (n - 1) + tr) / n     # Wilder's

    # ── Pivot detection ───────────────────────────────────────────────
    def _detect_pivots(self) -> tuple[Optional[float], Optional[float]]:
        """
        Check whether the bar swing_len positions back is a local
        high / low — mirrors ta.pivothigh(high, n, n) and pivotlow.
        """
        n      = self.swing_len
        needed = 2 * n + 1
        if len(self._highs) < needed:
            return None, None

        win_h  = self._highs[-needed:]
        win_l  = self._lows[-needed:]
        ctr_h  = win_h[n]   # candidate: n bars ago
        ctr_l  = win_l[n]

        ph = ctr_h if ctr_h >= max(win_h) else None
        pl = ctr_l if ctr_l <= min(win_l) else None
        return ph, pl

    def _pivot_bar_idx(self) -> int:
        """Bar index of the pivot that was just detected (n bars ago)."""
        return self._bar_idxs[-(self.swing_len + 1)]

    # ── Swing registration ────────────────────────────────────────────
    def _push_swing_high(self, price: float, bar_idx: int) -> None:
        if len(self.swing_highs) >= 5:
            self.swing_highs.pop(0)
        self.swing_highs.append((price, bar_idx))

    def _push_swing_low(self, price: float, bar_idx: int) -> None:
        if len(self.swing_lows) >= 5:
            self.swing_lows.pop(0)
        self.swing_lows.append((price, bar_idx))

    # ── Watch-level update logic ──────────────────────────────────────
    def _update_watch_high(self, price: float, bar_idx: int) -> None:
        """
        Bullish/pending-bull: only raise watch high (track the HIGHEST
        swing high for a BOS above).  Otherwise: always update.
        """
        if self.trend == 1 or self.pending == 1:
            if self.watch_high is None or price > self.watch_high:
                self.watch_high, self.watch_high_bar = price, bar_idx
        else:
            self.watch_high, self.watch_high_bar = price, bar_idx

    def _update_watch_low(self, price: float, bar_idx: int) -> None:
        """
        Bearish/pending-bear: only lower watch low (track the LOWEST
        swing low for a BOS below).  Otherwise: always update.
        """
        if self.trend == -1 or self.pending == -1:
            if self.watch_low is None or price < self.watch_low:
                self.watch_low, self.watch_low_bar = price, bar_idx
        else:
            self.watch_low, self.watch_low_bar = price, bar_idx

    # ── Break detection → BOS / CHoCH ────────────────────────────────
    def _check_breaks(self, close: float, bar_idx: int) -> None:

        # ── Bullish break ─────────────────────────────────────────────
        if self.watch_high is not None and close > self.watch_high:
            is_bos = self.trend == 1 or self.pending == 1

            if len(self.events) >= self.max_events:
                self.events.pop(0)
            self.events.append(MsEvent(bar_idx, self.watch_high, True, is_bos))

            if is_bos:
                self.bull_bos = True
                self.trend    = 1
                self.pending  = 0
            else:                        # CHoCH — wait for confirming BOS
                self.bull_choch = True
                self.pending    = 1

            self.watch_high = self.watch_high_bar = None

        # ── Bearish break ─────────────────────────────────────────────
        if self.watch_low is not None and close < self.watch_low:
            is_bos = self.trend == -1 or self.pending == -1

            if len(self.events) >= self.max_events:
                self.events.pop(0)
            self.events.append(MsEvent(bar_idx, self.watch_low, False, is_bos))

            if is_bos:
                self.bear_bos = True
                self.trend    = -1
                self.pending  = 0
            else:
                self.bear_choch = True
                self.pending    = -1

            self.watch_low = self.watch_low_bar = None

    # ── Momentum signals ──────────────────────────────────────────────
    def _compute_momentum(self, close: float, ph_registered: bool, pl_registered: bool) -> None:
        """
        Long:  LH → HH sequence in swing highs  +  active bull CHoCH  +  pivot registered on this bar
        Short: HL → LL sequence in swing lows   +  active bear CHoCH  +  pivot registered on this bar

        Mirrors Pine Script:
            ms_momentum_long  = ms_seq_lh_hh and ms_choch_bull_active
            ms_momentum_short = ms_seq_hl_ll and ms_choch_bear_active
        """
        sh = self.swing_highs
        sl = self.swing_lows

        # LH → HH: newest high > previous (HH), previous < one before (was LH)
        seq_lh_hh = (
            len(sh) >= 3
            and sh[-1][0] > sh[-2][0]
            and sh[-2][0] < sh[-3][0]
        )

        # HL → LL: newest low < previous (LL), previous > one before (was HL)
        seq_hl_ll = (
            len(sl) >= 3
            and sl[-1][0] < sl[-2][0]
            and sl[-2][0] > sl[-3][0]
        )

        # Active bull CHoCH: last event is a bull CHoCH + price still above it
        choch_bull = (
            bool(self.events)
            and self.events[-1].is_bull
            and not self.events[-1].is_bos
            and close > self.events[-1].level
        )

        # Active bear CHoCH: last event is a bear CHoCH + price still below it
        choch_bear = (
            bool(self.events)
            and not self.events[-1].is_bull
            and not self.events[-1].is_bos
            and close < self.events[-1].level
        )

        self.momentum_long  = seq_lh_hh and choch_bull and ph_registered
        self.momentum_short = seq_hl_ll and choch_bear and pl_registered

    # ── Main entry point ──────────────────────────────────────────────
    def update(self, high: float, low: float, close: float, bar_idx: int) -> None:
        """
        Call once per closed bar.
        After returning, read .momentum_long / .momentum_short for signals.
        """
        # Reset per-bar flags
        self.bull_bos = self.bear_bos = False
        self.bull_choch = self.bear_choch = False
        self.momentum_long = self.momentum_short = False

        # Update ATR
        self._update_atr(high, low, close)

        # Append to rolling buffers
        self._highs.append(high)
        self._lows.append(low)
        self._bar_idxs.append(bar_idx)

        max_buf = 2 * self.swing_len + 5
        if len(self._highs) > max_buf:
            self._highs.pop(0)
            self._lows.pop(0)
            self._bar_idxs.pop(0)

        # Detect pivot (requires at least 2n+1 bars in buffer)
        ph, pl = self._detect_pivots()
        if len(self._bar_idxs) < self.swing_len + 1:
            return  # not enough bars yet

        pbar = self._pivot_bar_idx()

        # ── Register pivot high ───────────────────────────────────────
        ph_registered = False
        if ph is not None:
            dist_ok = (
                not self.swing_lows
                or ph - self.swing_lows[-1][0] > self.atr * self.atr_dist
            )
            if dist_ok:
                self._push_swing_high(ph, pbar)
                self._update_watch_high(ph, pbar)
                ph_registered = True

        # ── Register pivot low ────────────────────────────────────────
        pl_registered = False
        if pl is not None:
            dist_ok = (
                not self.swing_highs
                or self.swing_highs[-1][0] - pl > self.atr * self.atr_dist
            )
            if dist_ok:
                self._push_swing_low(pl, pbar)
                self._update_watch_low(pl, pbar)
                pl_registered = True

        # ── Break detection ───────────────────────────────────────────
        self._check_breaks(close, bar_idx)

        # ── Momentum signals ──────────────────────────────────────────
        self._compute_momentum(close, ph_registered, pl_registered)