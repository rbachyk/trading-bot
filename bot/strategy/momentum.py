"""Phase 1 strategy: momentum (EMA trend filter + Donchian breakout).
Regime detector + mean-reversion arrive in Phase 2 (project Section E/G).

Parameters are config defaults, NOT optimized. Optimization happens offline in
Phase 3 via walk-forward analysis — never live (Section F).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Signal:
    action: str          # 'long' | 'short' | 'exit' | 'hold'
    reason: str
    atr: float
    close: float


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


class MomentumStrategy:
    def __init__(self, ema_fast: int, ema_slow: int, donchian_period: int,
                 atr_period: int, trail_atr_mult: float):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.donchian = donchian_period
        self.atr_period = atr_period
        self.trail_atr_mult = trail_atr_mult

    def min_bars(self) -> int:
        return max(self.ema_slow, self.donchian, self.atr_period) + 5

    def evaluate(self, df: pd.DataFrame, position_side: str | None) -> Signal:
        """df: columns open, high, low, close, volume; oldest first; last row = last CLOSED candle.
        position_side: 'Buy', 'Sell', or None.
        """
        if len(df) < self.min_bars():
            return Signal("hold", "insufficient history", 0.0, float(df["close"].iloc[-1]))

        close = df["close"]
        fast = ema(close, self.ema_fast)
        slow = ema(close, self.ema_slow)
        a = atr(df, self.atr_period)
        # Donchian channel of the PRIOR N bars (exclude current bar to avoid lookahead)
        upper = df["high"].shift(1).rolling(self.donchian).max()
        lower = df["low"].shift(1).rolling(self.donchian).min()

        c = float(close.iloc[-1])
        cur_atr = float(a.iloc[-1])
        uptrend = fast.iloc[-1] > slow.iloc[-1]
        downtrend = fast.iloc[-1] < slow.iloc[-1]

        if position_side is None:
            if uptrend and c > float(upper.iloc[-1]):
                return Signal("long", "donchian breakout up + EMA uptrend", cur_atr, c)
            if downtrend and c < float(lower.iloc[-1]):
                return Signal("short", "donchian breakout down + EMA downtrend", cur_atr, c)
            return Signal("hold", "no breakout", cur_atr, c)

        # In a position: exit on trend flip (trailing stop is handled exchange-side by engine)
        if position_side == "Buy" and downtrend:
            return Signal("exit", "EMA trend flipped down", cur_atr, c)
        if position_side == "Sell" and uptrend:
            return Signal("exit", "EMA trend flipped up", cur_atr, c)
        return Signal("hold", "in position, trend intact", cur_atr, c)
