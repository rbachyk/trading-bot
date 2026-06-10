"""Bybit v5 wrapper (pybit unified_trading.HTTP).

Method names verified against pybit >=5.x unified_trading API as of training data;
pin the pybit version in requirements.txt and re-verify on upgrade. Anything I could
not verify is marked [UNVERIFIED].

All mutating calls go through _call() which retries transient failures with backoff
and logs errors to SQLite via the injected logger callback.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import pandas as pd
from pybit.unified_trading import HTTP

log = logging.getLogger("exchange")

RETRYABLE = (ConnectionError, TimeoutError, OSError)


class BybitClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool,
                 symbol: str, category: str,
                 on_error: Callable[[str, str], None] | None = None):
        self.symbol = symbol
        self.category = category
        self._on_error = on_error or (lambda ctx, msg: None)
        self.http = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)
        self._qty_step, self._min_qty = self._fetch_qty_filters()

    # ---- plumbing -----------------------------------------------------------
    def _call(self, fn, ctx: str, retries: int = 4, **kwargs) -> dict:
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                resp = fn(**kwargs)
                if resp.get("retCode") != 0:
                    msg = f"{ctx}: retCode={resp.get('retCode')} {resp.get('retMsg')}"
                    # 10006 = rate limit on v5 [UNVERIFIED exact code — treat any non-zero conservatively]
                    if "rate" in str(resp.get("retMsg", "")).lower() and attempt < retries - 1:
                        time.sleep(delay)
                        delay *= 2
                        continue
                    self._on_error(ctx, msg)
                    raise RuntimeError(msg)
                return resp
            except RETRYABLE as e:
                last_exc = e
                log.warning("%s transient error (%s), retry in %.1fs", ctx, e, delay)
                time.sleep(delay)
                delay *= 2
        self._on_error(ctx, f"exhausted retries: {last_exc}")
        raise RuntimeError(f"{ctx}: exhausted retries ({last_exc})")

    def _fetch_qty_filters(self) -> tuple[float, float]:
        resp = self._call(self.http.get_instruments_info, "instruments_info",
                          category=self.category, symbol=self.symbol)
        f = resp["result"]["list"][0]["lotSizeFilter"]
        return float(f["qtyStep"]), float(f["minOrderQty"])

    def round_qty(self, qty: float) -> float:
        step = self._qty_step
        rounded = (qty // step) * step
        return rounded if rounded >= self._min_qty else 0.0

    # ---- market data ---------------------------------------------------------
    def get_klines(self, interval: str, limit: int = 200) -> pd.DataFrame:
        resp = self._call(self.http.get_kline, "get_kline",
                          category=self.category, symbol=self.symbol,
                          interval=interval, limit=limit)
        rows = resp["result"]["list"]  # newest first per v5 docs
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
        df = df.astype(float).iloc[::-1].reset_index(drop=True)  # oldest first
        # Drop the still-forming candle so signals only use closed bars
        return df.iloc[:-1]

    # ---- account / positions ---------------------------------------------------
    def get_equity(self) -> float:
        resp = self._call(self.http.get_wallet_balance, "wallet_balance", accountType="UNIFIED")
        return float(resp["result"]["list"][0]["totalEquity"])

    def get_position(self) -> dict | None:
        """Returns {'side': 'Buy'|'Sell', 'qty': float, 'entry': float, 'stop': float|None} or None."""
        resp = self._call(self.http.get_positions, "get_positions",
                          category=self.category, symbol=self.symbol)
        for p in resp["result"]["list"]:
            qty = float(p.get("size") or 0)
            if qty > 0:
                sl = p.get("stopLoss")
                return {
                    "side": p["side"],
                    "qty": qty,
                    "entry": float(p.get("avgPrice") or 0),
                    "stop": float(sl) if sl not in (None, "", "0") else None,
                }
        return None

    def set_leverage(self, leverage: float) -> None:
        try:
            self._call(self.http.set_leverage, "set_leverage",
                       category=self.category, symbol=self.symbol,
                       buyLeverage=str(leverage), sellLeverage=str(leverage))
        except RuntimeError as e:
            # Bybit returns an error if leverage already set to this value; ignore that case
            if "110043" not in str(e) and "not modified" not in str(e).lower():
                raise

    # ---- orders -----------------------------------------------------------------
    def market_entry_with_stop(self, side: str, qty: float, stop_loss: float) -> dict:
        """Market order with exchange-side stopLoss attached at order time (Section C:
        stop lives on the exchange, not only in bot logic)."""
        return self._call(
            self.http.place_order, "place_order",
            category=self.category, symbol=self.symbol,
            side=side, orderType="Market",
            qty=str(qty), stopLoss=str(round(stop_loss, 2)),
            timeInForce="IOC", reduceOnly=False, positionIdx=0,
        )

    def update_stop(self, stop_loss: float) -> None:
        self._call(self.http.set_trading_stop, "set_trading_stop",
                   category=self.category, symbol=self.symbol,
                   stopLoss=str(round(stop_loss, 2)), positionIdx=0)

    def close_position_market(self) -> dict | None:
        pos = self.get_position()
        if not pos:
            return None
        opposite = "Sell" if pos["side"] == "Buy" else "Buy"
        return self._call(
            self.http.place_order, "close_position",
            category=self.category, symbol=self.symbol,
            side=opposite, orderType="Market",
            qty=str(pos["qty"]), reduceOnly=True, positionIdx=0,
        )

    def cancel_all(self) -> None:
        self._call(self.http.cancel_all_orders, "cancel_all",
                   category=self.category, symbol=self.symbol)

    # ---- safety -------------------------------------------------------------------
    def flatten(self) -> None:
        """Kill-switch primitive: cancel everything, close any position."""
        self.cancel_all()
        self.close_position_market()
