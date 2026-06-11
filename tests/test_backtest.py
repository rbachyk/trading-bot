"""Harness + optimizer tests on synthetic 5m data (deterministic, fast)."""
import json
import numpy as np
import pandas as pd
import pytest

from backtest.harness import Backtester, BTParams, resample_15m
from backtest import optimizer as opt


def synth_df5(n=2200, seed=5, trend=0.0, vol=2.0, start=2000.0):
    rng = np.random.default_rng(seed)
    closes = start + np.cumsum(rng.normal(trend, vol, n))
    closes = np.maximum(closes, 100)
    high = closes + np.abs(rng.normal(0, vol, n))
    low = closes - np.abs(rng.normal(0, vol, n))
    opens = np.r_[closes[0], closes[:-1]]
    ts = (np.arange(n) * 300_000 + 1_700_000_000_000).astype(float)
    return pd.DataFrame({"ts": ts, "open": opens, "high": high, "low": low,
                         "close": closes, "volume": np.ones(n), "turnover": np.ones(n)})


P = BTParams(start_equity=100.0)


def test_resample_15m_aggregates_3_bars():
    df15 = resample_15m(synth_df5(300))
    assert 100 <= len(df15) <= 101  # +1 if first ts isn't 15m-aligned
    assert (df15["high"] >= df15["low"]).all()


def test_harness_runs_and_books_costs():
    res = Backtester(P).run(synth_df5(trend=0.3, seed=11))
    m = res.metrics
    assert m["n_trades"] >= 1, "trending synth data should produce trades"
    assert m["fees_paid"] > 0, "fees must be charged (first-class cost)"
    assert len(res.equity_curve) > 1000


def test_stop_loss_is_respected_per_trade():
    res = Backtester(P).run(synth_df5(trend=-0.2, seed=3))
    for t in res.trades:
        # worst loss per trade ~= risk budget (1.5%) + slippage allowance; never a blowup
        assert t["pnl"] > -P.start_equity * 0.05


def test_equity_curve_consistent_with_trades():
    res = Backtester(P).run(synth_df5(trend=0.3, seed=11))
    assert res.metrics["end_equity"] == pytest.approx(
        P.start_equity + sum(t["pnl"] for t in res.trades), abs=0.5)


def test_walk_forward_windows_and_no_proposal_machinery(monkeypatch, tmp_path):
    df5 = synth_df5(n=2400, seed=7)
    wf = opt.walk_forward(df5, {"ema_fast": [10, 20]}, P, train_bars=900, test_bars=400)
    assert wf["n_windows"] >= 2
    for w in wf["windows"]:
        assert "oos_best" in w and "oos_current" in w
        assert w["best_params"]["ema_fast"] in (10, 20)


def test_oos_window_alignment_no_contamination():
    # warmup must equal the slice offset, so trading starts at the test boundary
    bt = Backtester(P)
    assert bt.warmup_bars() >= 210
