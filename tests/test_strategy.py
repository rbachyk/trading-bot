"""Momentum strategy tests on synthetic candles — verifies signal logic, not profitability."""
import numpy as np
import pandas as pd

from bot.strategy.momentum import MomentumStrategy

STRAT = MomentumStrategy(ema_fast=20, ema_slow=50, donchian_period=20,
                         atr_period=14, trail_atr_mult=2.5)


def make_df(closes):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "open": closes,
        "high": closes * 1.002,
        "low": closes * 0.998,
        "close": closes,
        "volume": np.ones_like(closes),
    })


def test_insufficient_history_holds():
    sig = STRAT.evaluate(make_df([2000] * 10), None)
    assert sig.action == "hold"


def test_uptrend_breakout_goes_long():
    base = np.linspace(2000, 2100, 80)          # steady uptrend
    closes = np.append(base, 2150)              # breakout above prior 20-bar high
    sig = STRAT.evaluate(make_df(closes), None)
    assert sig.action == "long"


def test_downtrend_breakout_goes_short():
    base = np.linspace(2100, 2000, 80)
    closes = np.append(base, 1950)
    sig = STRAT.evaluate(make_df(closes), None)
    assert sig.action == "short"


def test_flat_market_holds():
    closes = 2000 + np.sin(np.linspace(0, 10, 100))  # tiny oscillation
    sig = STRAT.evaluate(make_df(closes), None)
    assert sig.action == "hold"


def test_long_exits_on_trend_flip():
    closes = np.concatenate([np.linspace(2000, 2100, 60), np.linspace(2100, 1900, 40)])
    sig = STRAT.evaluate(make_df(closes), "Buy")
    assert sig.action == "exit"


def test_atr_positive():
    sig = STRAT.evaluate(make_df(np.linspace(2000, 2100, 100)), None)
    assert sig.atr > 0
