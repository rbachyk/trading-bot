"""Historical kline fetcher (public endpoint, no auth) with CSV cache.

Usage:
    python -m backtest.data --days 60
The harness/optimizer read the cached CSV so repeated runs don't hammer the API.
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pandas as pd
from pybit.unified_trading import HTTP

from bot.config import ROOT, load_config

log = logging.getLogger("backtest.data")
COLS = ["ts", "open", "high", "low", "close", "volume", "turnover"]


def cache_path(symbol: str, interval: str) -> Path:
    return Path(ROOT) / "data" / f"klines_{symbol}_{interval}m.csv"


def fetch_klines(http: HTTP, symbol: str, interval: str, days: int,
                 category: str = "linear") -> pd.DataFrame:
    """Paginate backwards from now; v5 get_kline returns newest-first, max 1000/page."""
    end = int(time.time() * 1000)
    start = end - days * 86400_000
    frames = []
    cursor_end = end
    while cursor_end > start:
        resp = http.get_kline(category=category, symbol=symbol,
                              interval=interval, end=cursor_end, limit=1000)
        rows = resp["result"]["list"]
        if not rows:
            break
        df = pd.DataFrame(rows, columns=COLS).astype(float)
        frames.append(df)
        oldest = int(df["ts"].min())
        if oldest <= start or oldest >= cursor_end:
            break
        cursor_end = oldest - 1
        time.sleep(0.15)  # stay far under public rate limits
    if not frames:
        raise RuntimeError("no kline data returned")
    out = (pd.concat(frames).drop_duplicates("ts").sort_values("ts").reset_index(drop=True))
    return out[out["ts"] >= start].reset_index(drop=True)


def load_or_fetch(days: int, interval: str | None = None) -> pd.DataFrame:
    cfg = load_config()
    interval = interval or cfg.timeframes.execution
    path = cache_path(cfg.exchange.symbol, interval)
    if path.exists():
        df = pd.read_csv(path)
        span_days = (df["ts"].max() - df["ts"].min()) / 86400_000
        age_h = (time.time() * 1000 - df["ts"].max()) / 3600_000
        if span_days >= days * 0.95 and age_h < 24:
            log.info("using cache %s (%.0f days, %.1fh old)", path.name, span_days, age_h)
            return df
    # Public data: fetch PRODUCTION market history (no auth) regardless of demo/testnet:
    # strategy evidence must come from real markets, not sandbox prints.
    http = HTTP(testnet=False)
    df = fetch_klines(http, cfg.exchange.symbol, interval, days, cfg.exchange.category)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    log.info("fetched %d bars -> %s", len(df), path.name)
    return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    print(f"{len(load_or_fetch(ap.parse_args().days))} bars cached")
