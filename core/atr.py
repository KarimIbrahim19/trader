"""
atr.py
──────────────────────────────────────────────────────────────────────
Standalone Average True Range (Wilder's smoothing).

Extracted as its own module so any signal generator — FVG, MS, a
future volatility filter — can compute ATR independently without
depending on MarketStructure. Same math as the ATR embedded inside
market_structure.py, just decoupled for standalone testing.
"""

from typing import Optional


class ATR:

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._prev_close: Optional[float] = None
        self._tr_buf: list[float] = []
        self.value: float = 0.0

    def update(self, high: float, low: float, close: float) -> None:
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

        n     = self.period
        count = len(self._tr_buf)
        if count < n:
            self.value = sum(self._tr_buf) / count       # seed with SMA
        elif count == n:
            self.value = sum(self._tr_buf) / n
        else:
            self.value = (self.value * (n - 1) + tr) / n  # Wilder's smoothing