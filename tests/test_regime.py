"""Regime detector tests on synthetic 15m data."""
import numpy as np
import pandas as pd

from bot.strategy import regime as rg

DET = rg.RegimeDetector()


def make_df(closes, spread=0.002):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": closes, "high": closes * (1 + spread),
                         "low": closes * (1 - spread), "close": closes,
                         "volume": np.ones_like(closes)})


def test_insufficient_history_is_chaos():
    assert DET.classify(make_df([2000] * 20)) == rg.CHAOS


def test_strong_trend_detected():
    closes = np.linspace(2000, 2600, 150)  # persistent directional move
    assert DET.classify(make_df(closes)) == rg.TRENDING


def test_quiet_range_detected():
    rng = np.random.default_rng(7)
    closes = 2000 + np.cumsum(rng.normal(0, 0.5, 150))  # tiny drift, no trend
    out = DET.classify(make_df(closes))
    assert out in (rg.RANGING, rg.CHAOS)  # never TRENDING on noise
    assert out == rg.RANGING


def test_vol_spike_is_chaos():
    closes = list(2000 + np.sin(np.linspace(0, 12, 145)) * 5)
    closes += [2000, 2080, 1990, 2120, 2010]  # violent last bars
    df = make_df(np.array(closes))
    # widen the true range on the spike bars
    df.loc[df.index[-5:], "high"] = df["close"].iloc[-5:] * 1.03
    df.loc[df.index[-5:], "low"] = df["close"].iloc[-5:] * 0.97
    assert DET.classify(df) == rg.CHAOS
