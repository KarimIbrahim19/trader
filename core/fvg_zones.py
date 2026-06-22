"""
fvg_zones.py
──────────────────────────────────────────────────────────────────────
Pure Python port of the Pine Script v6 FVG / IFVG module.

Designed for two independent use cases — pick whichever fits your strategy:

  SIGNAL MODE  (standalone entry signal)
  ───────────────────────────────────────
  fvg.bull_signal   fires on the exact bar price bounces OUT of a bull zone
  fvg.bear_signal   fires on the exact bar price bounces OUT of a bear zone

  Used as a direct entry:
      if fvg.bull_signal:
          enter_long()

  FILTER MODE  (confluence gate)
  ───────────────────────────────────────
  fvg.long_filter   = bull zone is within 1 ATR  AND  bounce was recent (≤N bars)
  fvg.short_filter  = bear zone is within 1 ATR  AND  bounce was recent (≤N bars)

  Used to qualify another signal:
      if ms.momentum_long and htf.bull and fvg.long_filter:
          enter_long()

  INDIVIDUAL COMPONENTS  (build your own logic)
  ───────────────────────────────────────
  fvg.bull_near     nearest bull zone is within 1 ATR below close
  fvg.bear_near     nearest bear zone is within 1 ATR above close
  fvg.bull_recent   a bull bounce happened within sig_lookback bars
  fvg.bear_recent   a bear bounce happened within sig_lookback bars

  Mix and match however you need:
      if fvg.bull_near and my_other_condition:
          ...

──────────────────────────────────────────────────────────────────────
ICT 3-bar FVG pattern:
  Bull FVG: low[0] > high[2]  (gap between current low and 2-bar-ago high)
  Bear FVG: high[0] < low[2]  (gap between current high and 2-bar-ago low)

Invalidation / inversion:
  Bull FVG → Bear IFVG when wick breaks below zone bot - 0.3×ATR
  Bear FVG → Bull IFVG when wick breaks above zone top + 0.3×ATR
──────────────────────────────────────────────────────────────────────
"""

import math
from dataclasses import dataclass


@dataclass
class FvgZone:
    top:       float   # upper boundary of the gap
    bot:       float   # lower boundary of the gap
    is_bull:   bool    # current acting direction (flips on IFVG inversion)
    orig_bull: bool    # direction at formation — never changes
    state:     int     # 0 = FVG active | 1 = IFVG (inverted)
    has_fired: bool = False
    bars_since_signal: int = 9999
    age:       int = 0


class FVGZones:

    def __init__(
        self,
        atr_mult:     float = 0.25,   # gap must be > ATR × this     (i_fvg_atr_mult)
        max_zones:    int   = 10,     # maximum tracked zones         (i_fvg_max)
        sig_lookback: int   = 3,      # recency window bars           (i_fvg_sig_lookback)
        ifvg_enable:  bool  = True,   # allow FVG → IFVG inversion    (i_ifvg_enable)
        sig_cooldown: int   = -1,     # bars to wait before refiring (-1 = single signal)
        max_age:      int   = -1,     # bars to wait before removing zone (-1 = unlimited)
    ) -> None:
        self.atr_mult     = atr_mult
        self.max_zones    = max_zones
        self.sig_lookback = sig_lookback
        self.ifvg_enable  = ifvg_enable
        self.sig_cooldown = sig_cooldown
        self.max_age      = max_age

        self.zones: list[FvgZone] = []

        # Rolling 3-bar (high, low, close) buffer for gap detection
        self._buf: list[tuple[float, float, float]] = []

        # Bars since last bounce signal fired (large = never fired)
        self._bull_bars_ago: int = 9999
        self._bear_bars_ago: int = 9999

        # ── SIGNAL MODE outputs ───────────────────────────────────────
        # Fire on the exact bar price bounces from a zone (fvg_bull_sig / fvg_bear_sig)
        self.bull_signal: bool = False
        self.bear_signal: bool = False

        # ── FILTER MODE outputs ───────────────────────────────────────
        # Combination of proximity + recency (fvg_long_filter / fvg_short_filter)
        self.long_filter:  bool = False
        self.short_filter: bool = False

        # ── INDIVIDUAL COMPONENTS (exposed for custom logic) ──────────
        self.bull_near:   bool = False   # nearest bull zone within 1 ATR
        self.bear_near:   bool = False   # nearest bear zone within 1 ATR
        self.bull_recent: bool = False   # bounce happened within sig_lookback bars
        self.bear_recent: bool = False   # bounce happened within sig_lookback bars

    # ── Main update ───────────────────────────────────────────────────
    def update(self, high: float, low: float, close: float, atr: float) -> None:
        """
        Call once per closed bar.
        After returning, read any combination of the output properties.
        """
        if atr <= 0:
            return

        # Age recency counters and reset per-bar signal flags
        self._bull_bars_ago += 1
        self._bear_bars_ago += 1
        self.bull_signal = False
        self.bear_signal = False

        for z in self.zones:
            z.bars_since_signal += 1
            z.age += 1

        # Update rolling OHLC buffer (keep last 5 bars)
        self._buf.append((high, low, close))
        if len(self._buf) > 5:
            self._buf.pop(0)

        gap_min = atr * self.atr_mult   # minimum gap size to register
        inv_buf = atr * 0.3             # wick allowance before invalidation

        # Previous bar values for bounce detection
        prev_high = self._buf[-2][0] if len(self._buf) >= 2 else high
        prev_low  = self._buf[-2][1] if len(self._buf) >= 2 else low

        # Proximity tracking
        nearest_bull_dist = math.inf
        nearest_bear_dist = math.inf

        # ── Zone state machine ─────────────────────────────────────────
        zones_next: list[FvgZone] = []

        for z in self.zones:
            keep = True

            if 0 < self.max_age <= z.age:
                keep = False
            elif z.is_bull:
                # ── Bull zone (support) ────────────────────────────────
                # Proximity: find nearest bull zone whose top is below close
                if z.top < close:
                    dist = close - z.top
                    if dist < nearest_bull_dist:
                        nearest_bull_dist = dist

                # ── SIGNAL: price exits zone upward on this bar ────────
                # current low ABOVE zone top  +  previous low WAS below/inside zone
                can_fire = (not z.has_fired) or (self.sig_cooldown >= 0 and z.bars_since_signal >= self.sig_cooldown)
                if low > z.top and prev_low < z.top and can_fire:
                    self.bull_signal     = True   # signal mode output
                    self._bull_bars_ago  = 0      # reset recency counter
                    z.has_fired          = True
                    z.bars_since_signal  = 0

                # Invalidation: wick breaks below zone bottom
                elif low < z.bot - inv_buf:
                    if self.ifvg_enable and z.state == 0:
                        z.state   = 1        # mark as IFVG
                        z.is_bull = False    # flip direction to bearish
                        z.has_fired = False  # Reset signal status for IFVG state
                        z.bars_since_signal = 9999
                        z.age = 0            # Reset zone age for IFVG state
                    else:
                        keep = False         # remove zone entirely

            else:
                # ── Bear zone (resistance) ─────────────────────────────
                if z.bot > close:
                    dist = z.bot - close
                    if dist < nearest_bear_dist:
                        nearest_bear_dist = dist

                # ── SIGNAL: price exits zone downward on this bar ───────
                # current high BELOW zone bot  +  previous high WAS above/inside zone
                can_fire = (not z.has_fired) or (self.sig_cooldown >= 0 and z.bars_since_signal >= self.sig_cooldown)
                if high < z.bot and prev_high > z.bot and can_fire:
                    self.bear_signal     = True
                    self._bear_bars_ago  = 0
                    z.has_fired          = True
                    z.bars_since_signal  = 0

                elif high > z.top + inv_buf:
                    if self.ifvg_enable and z.state == 0:
                        z.state   = 1
                        z.is_bull = True     # flip to bullish IFVG
                        z.has_fired = False  # Reset signal status for IFVG state
                        z.bars_since_signal = 9999
                        z.age = 0            # Reset zone age for IFVG state
                    else:
                        keep = False

            if keep:
                zones_next.append(z)

        self.zones = zones_next

        # ── Detect new FVGs from the 3-bar window ─────────────────────
        if len(self._buf) >= 3:
            h0, l0, _  = self._buf[-3]    # 2 bars ago
            h1, l1, c1 = self._buf[-2]    # 1 bar ago (confirmation candle)
            h2, l2, _  = self._buf[-1]    # current bar

            # Bull FVG: current low > 2-bars-ago high
            if l2 > h0 and c1 > h0 and (l2 - h0) > gap_min:
                self._add_zone(top=l2, bot=h0, is_bull=True)

            # Bear FVG: current high < 2-bars-ago low
            if h2 < l0 and c1 < l0 and (l0 - h2) > gap_min:
                self._add_zone(top=l0, bot=h2, is_bull=False)

        # ── Compute individual component flags ─────────────────────────
        self.bull_near   = nearest_bull_dist < atr
        self.bear_near   = nearest_bear_dist < atr
        self.bull_recent = self._bull_bars_ago <= self.sig_lookback
        self.bear_recent = self._bear_bars_ago <= self.sig_lookback

        # ── Compute combined filter outputs ────────────────────────────
        # Matches Pine Script:
        #   fvg_long_filter  = fvg_bull_near and fvg_bull_recent
        #   fvg_short_filter = fvg_bear_near and fvg_bear_recent
        self.long_filter  = self.bull_near and self.bull_recent
        self.short_filter = self.bear_near and self.bear_recent

    # ── Zone management ───────────────────────────────────────────────
    def _add_zone(self, top: float, bot: float, is_bull: bool) -> None:
        if len(self.zones) >= self.max_zones:
            self.zones.pop(0)   # evict oldest (FIFO)
        self.zones.append(FvgZone(
            top=top, bot=bot,
            is_bull=is_bull, orig_bull=is_bull,
            state=0,
        ))

    # ── Diagnostics ───────────────────────────────────────────────────
    def zone_counts(self) -> tuple[int, int]:
        """Returns (bull_zone_count, bear_zone_count) — useful for logging."""
        bull = sum(1 for z in self.zones if z.is_bull)
        return bull, len(self.zones) - bull