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
from pybit import exceptions as bybit_exc
from pybit.unified_trading import HTTP

log = logging.getLogger("exchange")

RETRYABLE = (ConnectionError, TimeoutError, OSError)


class BybitClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool,
                 symbol: str, category: str, tld: str = "com", demo: bool = False,
                 on_error: Callable[[str, str], None] | None = None):
        self.symbol = symbol
        self.category = category
        self._on_error = on_error or (lambda ctx, msg: None)
        # tld="eu" -> api(-testnet).bybit.eu | demo=True -> api-demo.bybit.com
        self.http = HTTP(testnet=testnet, demo=demo,
                         api_key=api_key, api_secret=api_secret, tld=tld)
        self._qty_step, self._min_qty, self._tick = self._fetch_filters()

    # ---- plumbing -----------------------------------------------------------
    def _call(self, fn, ctx: str, retries: int = 4, **kwargs) -> dict:
        """pybit raises on every non-zero retCode (InvalidRequestError) and on
        transport problems (FailedRequestError) — it does NOT return error responses.
        We normalize everything to RuntimeError so callers handle one type:
          - rate limits (10006/10018 or 'rate limit' text) and transport errors -> retried with backoff
          - other API errors -> RuntimeError immediately (message keeps the ErrCode)
        """
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                return fn(**kwargs)
            except bybit_exc.InvalidRequestError as e:
                msg = str(e)
                if attempt < retries - 1 and (
                        "10006" in msg or "10018" in msg or "rate limit" in msg.lower()):
                    log.warning("%s rate limited, retry in %.1fs", ctx, delay)
                    time.sleep(delay)
                    delay *= 2
                    last_exc = e
                    continue
                self._on_error(ctx, f"{ctx}: {msg}")
                raise RuntimeError(f"{ctx}: {msg}") from e
            except (bybit_exc.FailedRequestError, *RETRYABLE) as e:
                last_exc = e
                log.warning("%s transient error (%s), retry in %.1fs", ctx, e, delay)
                time.sleep(delay)
                delay *= 2
        self._on_error(ctx, f"exhausted retries: {last_exc}")
        raise RuntimeError(f"{ctx}: exhausted retries ({last_exc})")

    def _fetch_filters(self) -> tuple[float, float, float]:
        resp = self._call(self.http.get_instruments_info, "instruments_info",
                          category=self.category, symbol=self.symbol)
        if not resp["result"]["list"]:
            raise RuntimeError(
                f"{self.symbol} not found in this environment. "
                f"Live alternatives: {self.list_live_symbols()}"
            )
        info = resp["result"]["list"][0]
        if info.get("status") != "Trading":
            # e.g. ErrCode 110074 territory: contract listed but not live (EU testnet quirk)
            raise RuntimeError(
                f"{self.symbol} exists but status={info.get('status')!r} — not tradable here. "
                f"Live alternatives in this environment: {self.list_live_symbols()}"
            )
        lot = info["lotSizeFilter"]
        tick = float(info["priceFilter"]["tickSize"])
        return float(lot["qtyStep"]), float(lot["minOrderQty"]), tick

    def list_live_symbols(self, base_coin: str = "ETH") -> list[str]:
        """All currently tradable linear contracts for base_coin in THIS environment."""
        resp = self._call(self.http.get_instruments_info, "instruments_info_all",
                          category=self.category, baseCoin=base_coin)
        return [i["symbol"] for i in resp["result"]["list"] if i.get("status") == "Trading"]

    def round_price(self, price: float) -> float:
        """Round to the instrument tick size (replaces the old hardcoded 2dp)."""
        ticks = round(price / self._tick)
        # keep a sane decimal representation
        return round(ticks * self._tick, 10)

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
            qty=str(qty), stopLoss=str(self.round_price(stop_loss)),
            timeInForce="IOC", reduceOnly=False, positionIdx=0,
        )

    def update_stop(self, stop_loss: float) -> None:
        self._call(self.http.set_trading_stop, "set_trading_stop",
                   category=self.category, symbol=self.symbol,
                   stopLoss=str(self.round_price(stop_loss)), positionIdx=0)

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

    def get_last_closed_pnl(self) -> dict | None:
        """Most recent realized-PnL record (v5 /position/closed-pnl). The exchange's
        own number includes its exact fees — always preferred over our estimate."""
        resp = self._call(self.http.get_closed_pnl, "closed_pnl",
                          category=self.category, symbol=self.symbol, limit=1)
        lst = resp["result"]["list"]
        if not lst:
            return None
        r = lst[0]
        return {"pnl": float(r["closedPnl"]), "exit": float(r["avgExitPrice"]),
                "qty": float(r["qty"]), "ts": float(r["updatedTime"]) / 1000.0}

    # ---- safety -------------------------------------------------------------------
    def flatten(self) -> None:
        """Kill-switch primitive: cancel everything, close any position."""
        self.cancel_all()
        self.close_position_market()
