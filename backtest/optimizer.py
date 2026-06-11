"""Walk-forward optimizer (Section F: offline, on demand, proposes — never applies).

Method:
  1. Split history into rolling (train, test) windows
  2. Grid-search a BOUNDED parameter set on each train window (in-sample)
  3. Take each window's in-sample winner and evaluate it on the UNSEEN test window
  4. Compare aggregated out-of-sample results vs the CURRENT config params
     evaluated on the same test windows
  5. Only if the candidate beats current OOS by a margin -> write a 'pending'
     proposal (params + full evidence) to SQLite for dashboard review

Run:
    python -m backtest.optimizer --days 60

Why the score penalizes drawdown: the user's priority is steady growth.
score = net_return_pct / (1 + max_dd_pct). Fees and slippage are inside the
harness, so every number is already net of costs.
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import statistics
import time

import pandas as pd

from backtest.data import load_or_fetch
from backtest.harness import Backtester, BTParams
from bot.config import load_config
from bot.persistence.db import Database

log = logging.getLogger("optimizer")

# Bounded grid (Section F: bounded adaptation only). 48 combos.
DEFAULT_GRID: dict[str, list] = {
    "ema_fast": [10, 20],
    "ema_slow": [40, 60],
    "donchian_period": [20, 30],
    "trail_atr_mult": [2.0, 3.0],
    "stop_loss_atr_mult": [1.5, 2.0, 2.5],
}


def score(metrics: dict) -> float:
    return metrics["net_return_pct"] / (1 + metrics["max_dd_pct"])


def run_params(df5: pd.DataFrame, overrides: dict, base: BTParams) -> dict:
    p = BTParams(**{**base.__dict__, **overrides})
    return Backtester(p).run(df5).metrics


def walk_forward(df5: pd.DataFrame, grid: dict, base: BTParams,
                 train_bars: int, test_bars: int) -> dict:
    combos = [dict(zip(grid, v)) for v in itertools.product(*grid.values())]
    windows = []
    start = 0
    while start + train_bars + test_bars <= len(df5):
        windows.append((start, start + train_bars, start + train_bars + test_bars))
        start += test_bars  # roll forward by one test window
    if not windows:
        raise ValueError("not enough data for one train+test window")

    warmup = Backtester(base).warmup_bars()
    per_window = []
    for w, (a, b, c) in enumerate(windows):
        train = df5.iloc[a:b].reset_index(drop=True)
        # OOS slice starts exactly `warmup` bars before the test boundary, so the
        # harness consumes the train tail as indicator warmup and TRADES only
        # inside [b, c) — no in-sample trades contaminate OOS metrics.
        oos_slice = df5.iloc[b - warmup:c].reset_index(drop=True)
        best, best_s = None, float("-inf")
        for combo in combos:
            m = run_params(train, combo, base)
            s = score(m)
            if s > best_s:
                best, best_s, best_m = combo, s, m
        oos_best = run_params(oos_slice, best, base)
        oos_current = run_params(oos_slice, {}, base)
        per_window.append({"window": w, "best_params": best,
                           "in_sample": best_m, "oos_best": oos_best,
                           "oos_current": oos_current})
        log.info("window %d/%d: IS %.2f%% -> OOS %.2f%% (current %.2f%%)",
                 w + 1, len(windows), best_m["net_return_pct"],
                 oos_best["net_return_pct"], oos_current["net_return_pct"])
    return {"windows": per_window, "n_windows": len(windows)}


def propose(days: int = 60, grid: dict | None = None,
            train_days: int = 21, test_days: int = 7) -> int | None:
    cfg = load_config()
    db = Database(cfg.db.path)
    df5 = load_or_fetch(days)
    bars_per_day = 288  # 5m
    base = BTParams(
        ema_fast=cfg.strategy.ema_fast, ema_slow=cfg.strategy.ema_slow,
        donchian_period=cfg.strategy.donchian_period, atr_period=cfg.strategy.atr_period,
        trail_atr_mult=cfg.strategy.trail_atr_mult,
        max_position_pct=cfg.risk.max_position_pct,
        stop_loss_atr_mult=cfg.risk.stop_loss_atr_mult,
        leverage=cfg.exchange.leverage,
        taker_fee_pct=cfg.costs.taker_fee_pct, slippage_pct=cfg.costs.slippage_pct,
        start_equity=cfg.risk.equity_cap or 100.0,
    )
    wf = walk_forward(df5, grid or DEFAULT_GRID, base,
                      train_days * bars_per_day, test_days * bars_per_day)

    oos_best = [w["oos_best"]["net_return_pct"] for w in wf["windows"]]
    oos_cur = [w["oos_current"]["net_return_pct"] for w in wf["windows"]]
    mean_best, mean_cur = statistics.mean(oos_best), statistics.mean(oos_cur)

    # most frequent winning combo across windows = the stable candidate
    keys = [json.dumps(w["best_params"], sort_keys=True) for w in wf["windows"]]
    candidate = json.loads(max(set(keys), key=keys.count))
    stability = keys.count(json.dumps(candidate, sort_keys=True)) / len(keys)

    evidence = {
        "generated": time.time(), "days": days,
        "train_days": train_days, "test_days": test_days,
        "mean_oos_best_pct": round(mean_best, 2),
        "mean_oos_current_pct": round(mean_cur, 2),
        "candidate_stability": round(stability, 2),
        "windows": wf["windows"],
        "caveats": "Historical, net of modeled fees/slippage; funding and latency "
                   "unmodeled; out-of-sample edge can still be regime luck.",
    }

    improves = mean_best > mean_cur + 0.5          # margin, not noise
    stable = stability >= 0.5                       # same combo wins most windows
    if improves and stable:
        pid = db.add_proposal(json.dumps(candidate), json.dumps(evidence))
        log.info("PROPOSAL #%d: %s | OOS %.2f%% vs current %.2f%% (stability %.0f%%)",
                 pid, candidate, mean_best, mean_cur, stability * 100)
        return pid
    log.info("No proposal: OOS best %.2f%% vs current %.2f%%, stability %.0f%% — "
             "current params stand. (This is the honest outcome, not a failure.)",
             mean_best, mean_cur, stability * 100)
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--train-days", type=int, default=21)
    ap.add_argument("--test-days", type=int, default=7)
    a = ap.parse_args()
    propose(a.days, train_days=a.train_days, test_days=a.test_days)
