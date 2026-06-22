"""
htf_bias.py
──────────────────────────────────────────────────────────────────────
HTF directional bias using a Hull Moving Average on 1H closes.

Matches Pine Script:
    htf_hma     = request.security(ticker, "60", ta.hma(close, ma_len))
    htf_hma_lag = request.security(ticker, "60", ta.hma(close, ma_len)[2])
    htf_bull = htf_close > htf_hma and htf_hma > htf_hma_lag
    htf_bear = htf_close < htf_hma and htf_hma < htf_hma_lag

HMA formula:
    WMA( 2 × WMA(close, n÷2) − WMA(close, n),  √n )
"""

import math


class HTFBias:

    def __init__(self, period: int = 21) -> None:
        self.period = period

        self._closes:   list[float] = []
        self._hma_vals: list[float] = []  # rolling HMA history

        self.bull:        bool = False
        self.bear:        bool = False
        self.initialized: bool = False   # False until enough 1H bars seen

    # ── Public API ────────────────────────────────────────────────────
    def update(self, close: float) -> None:
        """Call once per completed 1H bar."""
        self._closes.append(close)

        # Trim close buffer to the minimum needed for HMA computation
        needed = self.period + int(math.sqrt(self.period)) + 2
        if len(self._closes) > needed + 5:
            self._closes.pop(0)

        # Compute and store current HMA value
        hma_now = self._hma(self._closes, self.period)
        self._hma_vals.append(hma_now)
        if len(self._hma_vals) > 10:
            self._hma_vals.pop(0)

        # Need 3 values: current + [1] + [2] for lag check
        if len(self._hma_vals) < 3 or math.isnan(hma_now):
            return

        hma_lag2 = self._hma_vals[-3]   # HMA[2] in Pine Script terms
        if math.isnan(hma_lag2):
            return

        # Pine Script conditions:
        #   htf_bull = htf_close > htf_hma and htf_hma > htf_hma_lag
        #   htf_bear = htf_close < htf_hma and htf_hma < htf_hma_lag
        self.bull = close > hma_now and hma_now > hma_lag2
        self.bear = close < hma_now and hma_now < hma_lag2
        self.initialized = True

    # ── HMA implementation ────────────────────────────────────────────
    @staticmethod
    def _wma(values: list[float], period: int) -> float:
        """
        Weighted Moving Average — most recent bar has highest weight.
        weight(i) = i+1  for i in 0..period-1
        """
        if len(values) < period:
            return math.nan
        subset = values[-period:]
        denom  = period * (period + 1) / 2
        return sum(v * (i + 1) for i, v in enumerate(subset)) / denom

    def _hma(self, values: list[float], period: int) -> float:
        """
        Hull Moving Average:
            intermediate = 2 × WMA(n÷2) − WMA(n)
            HMA = WMA(intermediate, √n)
        """
        n2    = max(2, period // 2)
        sqrtn = max(2, int(math.sqrt(period)))

        if len(values) < period + sqrtn:
            return math.nan

        # Build intermediate series over the last sqrtn positions
        inter: list[float] = []
        n = len(values)
        for i in range(n - sqrtn, n):
            w_half = self._wma(values[: i + 1], n2)
            w_full = self._wma(values[: i + 1], period)
            if math.isnan(w_half) or math.isnan(w_full):
                return math.nan
            inter.append(2.0 * w_half - w_full)

        return self._wma(inter, sqrtn)