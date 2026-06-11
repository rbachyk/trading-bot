"""Mean reversion strategy tests on synthetic data."""
import numpy as np
import pandas as pd

from bot.strategy.meanreversion import MeanReversionStrategy

STRAT = MeanReversionStrategy()


def make_df(closes):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": closes, "high": closes * 1.001,
                         "low": closes * 0.999, "close": closes,
                         "volume": np.ones_like(closes)})


def test_oversold_extreme_goes_long():
    closes = np.concatenate([np.full(40, 2000.0) + np.random.default_rng(1).normal(0, 2, 40),
                             np.linspace(2000, 1930, 10)])  # hard selloff through band
    sig = STRAT.evaluate(make_df(closes), None)
    assert sig.action == "long"


def test_overbought_extreme_goes_short():
    closes = np.concatenate([np.full(40, 2000.0) + np.random.default_rng(2).normal(0, 2, 40),
                             np.linspace(2000, 2070, 10)])
    sig = STRAT.evaluate(make_df(closes), None)
    assert sig.action == "short"


def test_inside_bands_holds():
    closes = 2000 + np.random.default_rng(3).normal(0, 1, 60)
    assert STRAT.evaluate(make_df(closes), None).action == "hold"


def test_long_exits_at_midline():
    closes = np.concatenate([2000 + np.random.default_rng(4).normal(0, 2, 50),
                             [2005]])  # back above the ~2000 midline
    sig = STRAT.evaluate(make_df(closes), "Buy")
    assert sig.action == "exit"


def test_no_trailing_flag():
    assert STRAT.trailing is False
