"""RANGING regime strategy: Bollinger + RSI mean reversion (project Section E).

Entries fade band extremes confirmed by RSI; profit target is the band midline;
the protective stop is FIXED at entry (no trailing — trailing chokes mean
reversion). Parameters are unoptimized defaults until the Phase 3 walk-forward.
"""
from __future__ import annotations

import pandas as pd

from bot.strategy.momentum import Signal, atr


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-12)
    return 100 - 100 / (1 + rs)


class MeanReversionStrategy:
    trailing = False  # engine: keep the initial exchange-side stop fixed

    def __init__(self, bb_period: int = 20, bb_std: float = 2.0,
                 rsi_period: int = 14, rsi_low: float = 30, rsi_high: float = 70,
                 atr_period: int = 14):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high
        self.atr_period = atr_period

    def min_bars(self) -> int:
        return max(self.bb_period, self.rsi_period, self.atr_period) + 5

    def evaluate(self, df: pd.DataFrame, position_side: str | None) -> Signal:
        if len(df) < self.min_bars():
            return Signal("hold", "insufficient history", 0.0, float(df["close"].iloc[-1]))

        close = df["close"]
        mid = close.rolling(self.bb_period).mean()
        sd = close.rolling(self.bb_period).std()
        upper = mid + self.bb_std * sd
        lower = mid - self.bb_std * sd
        r = rsi(close, self.rsi_period)
        a = atr(df, self.atr_period)

        c = float(close.iloc[-1])
        cur_atr = float(a.iloc[-1])
        cur_mid = float(mid.iloc[-1])

        if position_side is None:
            if c <= float(lower.iloc[-1]) and float(r.iloc[-1]) <= self.rsi_low:
                return Signal("long", f"below lower band, RSI {r.iloc[-1]:.0f}", cur_atr, c)
            if c >= float(upper.iloc[-1]) and float(r.iloc[-1]) >= self.rsi_high:
                return Signal("short", f"above upper band, RSI {r.iloc[-1]:.0f}", cur_atr, c)
            return Signal("hold", "inside bands", cur_atr, c)

        # Profit target: midline touch closes the trade (tight targets per Section E)
        if position_side == "Buy" and c >= cur_mid:
            return Signal("exit", "midline target reached", cur_atr, c)
        if position_side == "Sell" and c <= cur_mid:
            return Signal("exit", "midline target reached", cur_atr, c)
        return Signal("hold", "waiting for midline", cur_atr, c)
