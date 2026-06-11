"""Regime detector (15m): TRENDING / RANGING / CHAOS.

Rules-based and bounded (project Section E/F) — this is the only autonomous
adaptation in the system:
  - CHAOS    : ATR%-of-price is in its top decile over the lookback AND well above
               the median (guards against slow-creep false positives) -> stand aside
  - TRENDING : ADX(14) above threshold -> momentum strategy
  - RANGING  : otherwise -> mean reversion strategy
CHAOS is checked first: high-vol spikes often produce high ADX too, and standing
aside wins that conflict (capital preservation).
"""
from __future__ import annotations

import pandas as pd

TRENDING = "TRENDING"
RANGING = "RANGING"
CHAOS = "CHAOS"


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ADX."""
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)

    alpha = 1 / period
    atr_ = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-12)
    return dx.ewm(alpha=alpha, adjust=False).mean()


def atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    a = tr.ewm(alpha=1 / period, adjust=False).mean()
    return a / df["close"] * 100


class RegimeDetector:
    def __init__(self, adx_period: int = 14, adx_threshold: float = 25.0,
                 chaos_percentile: float = 0.90, chaos_min_ratio: float = 1.5,
                 vol_lookback: int = 96):
        # vol_lookback 96 x 15m = 24h of volatility context
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.chaos_percentile = chaos_percentile
        self.chaos_min_ratio = chaos_min_ratio  # vol must also exceed median * ratio
        self.vol_lookback = vol_lookback

    def min_bars(self) -> int:
        return max(self.vol_lookback, self.adx_period * 3) + 5

    def classify(self, df15: pd.DataFrame) -> str:
        """df15: 15m candles, oldest first, last row = last closed bar."""
        if len(df15) < self.min_bars():
            return CHAOS  # not enough context -> safest answer is stand aside

        vol = atr_pct(df15, self.adx_period)
        recent = vol.iloc[-self.vol_lookback:]
        v = vol.iloc[-1]
        # both conditions: in the top decile AND a genuine spike vs normal conditions.
        # Percentile alone misfires on monotonically creeping vol (always at its own q90).
        if v >= recent.quantile(self.chaos_percentile) and v >= self.chaos_min_ratio * recent.median():
            return CHAOS

        if adx(df15, self.adx_period).iloc[-1] >= self.adx_threshold:
            return TRENDING
        return RANGING
