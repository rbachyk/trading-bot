"""Backtest harness.

Fidelity rules (so backtest == live logic, the whole point of reusing the modules):
  - Reuses MomentumStrategy / MeanReversionStrategy / RegimeDetector / risk.* verbatim
  - Signals on CLOSED 5m bars; entries fill at the NEXT bar open +/- slippage
  - Stops checked intra-bar against high/low, filled at stop price +/- slippage
  - Taker fee charged on both sides of every trade; sizing identical to live
  - 15m regime resampled from the same 5m data (no separate feed to drift)

Honesty rules (Section D/I): results are HISTORICAL. They overstate live results
because of unmodeled funding rates, queue/latency effects, and overfitting risk.
Never read a backtest as a promise.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from bot.risk import manager as risk
from bot.strategy import regime as rg
from bot.strategy.meanreversion import MeanReversionStrategy
from bot.strategy.momentum import MomentumStrategy

WINDOW = 210  # bars of history handed to strategies each step (>= min_bars)


@dataclass
class BTParams:
    # strategy
    ema_fast: int = 20
    ema_slow: int = 50
    donchian_period: int = 20
    atr_period: int = 14
    trail_atr_mult: float = 2.5
    # risk
    max_position_pct: float = 1.5
    stop_loss_atr_mult: float = 2.0
    leverage: float = 1.0
    # costs (first-class, Section C)
    taker_fee_pct: float = 0.055
    slippage_pct: float = 0.02
    # account
    start_equity: float = 100.0
    min_qty: float = 0.01


@dataclass
class BTResult:
    params: BTParams
    equity_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)

    @property
    def metrics(self) -> dict:
        eq = self.equity_curve or [self.params.start_equity]
        peak, max_dd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            max_dd = max(max_dd, (peak - v) / peak * 100 if peak > 0 else 0)
        pnls = [t["pnl"] for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [-p for p in pnls if p < 0]
        return {
            "net_return_pct": round((eq[-1] / eq[0] - 1) * 100, 2),
            "max_dd_pct": round(max_dd, 2),
            "n_trades": len(pnls),
            "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
            "profit_factor": round(sum(wins) / sum(losses), 2) if losses else None,
            "fees_paid": round(sum(t["fees"] for t in self.trades), 4),
            "end_equity": round(eq[-1], 4),
        }


def resample_15m(df5: pd.DataFrame) -> pd.DataFrame:
    d = df5.copy()
    d["dt"] = pd.to_datetime(d["ts"], unit="ms")
    out = d.set_index("dt").resample("15min").agg(
        {"ts": "first", "open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}).dropna().reset_index(drop=True)
    return out


class Backtester:
    def __init__(self, p: BTParams):
        self.p = p
        self.momentum = MomentumStrategy(p.ema_fast, p.ema_slow, p.donchian_period,
                                         p.atr_period, p.trail_atr_mult)
        self.meanrev = MeanReversionStrategy(atr_period=p.atr_period)
        self.detector = rg.RegimeDetector()
        self.rp = risk.RiskParams(p.max_position_pct, 100.0, 100.0,  # breakers off in BT
                                  p.stop_loss_atr_mult, p.leverage)

    def _fill(self, price: float, side: str, entry: bool) -> float:
        """Apply slippage against us in the direction of the fill."""
        slip = self.p.slippage_pct / 100
        buy_fill = (side == "Buy") == entry  # buying on entry-long or exit-short
        return price * (1 + slip) if buy_fill else price * (1 - slip)

    def warmup_bars(self) -> int:
        """First tradable index — used by the optimizer to align OOS windows."""
        return max(WINDOW, self.detector.min_bars() * 3)

    def run(self, df5: pd.DataFrame) -> BTResult:
        p = self.p
        res = BTResult(params=p)
        df15 = resample_15m(df5)
        ts15 = df15["ts"].values

        equity = p.start_equity
        pos = None  # {"side","qty","entry","stop","strategy"}
        regime_cache: dict[int, str] = {}

        warmup = self.warmup_bars()
        for i in range(warmup, len(df5) - 1):
            bar = df5.iloc[i]          # last CLOSED bar (signal bar)
            nxt = df5.iloc[i + 1]      # execution bar

            # ---- stop check on the execution bar (before anything else) ----
            if pos is not None:
                hit = (pos["side"] == "Buy" and nxt["low"] <= pos["stop"]) or \
                      (pos["side"] == "Sell" and nxt["high"] >= pos["stop"])
                if hit:
                    equity = self._close(res, pos, self._fill(pos["stop"], pos["side"], False),
                                         equity, "stop")
                    pos = None

            window = df5.iloc[i - WINDOW:i + 1]

            if pos is not None:
                strat = self.meanrev if pos["strategy"] == "meanrev" else self.momentum
                sig = strat.evaluate(window, pos["side"])
                if getattr(strat, "trailing", True):
                    pos["stop"] = risk.trailing_stop(pos["side"], pos["stop"], sig.close,
                                                     sig.atr, p.trail_atr_mult)
                if sig.action == "exit":
                    equity = self._close(res, pos, self._fill(nxt["open"], pos["side"], False),
                                         equity, sig.reason)
                    pos = None
            else:
                # regime from the last closed 15m bar at/<= this 5m bar's time
                j = int(ts15.searchsorted(bar["ts"], side="right")) - 2  # -1 current forming
                if j >= self.detector.min_bars():
                    regime = regime_cache.get(j)
                    if regime is None:
                        regime = self.detector.classify(df15.iloc[:j + 1])
                        regime_cache[j] = regime
                else:
                    regime = rg.CHAOS

                if regime != rg.CHAOS:
                    strat_name = "momentum" if regime == rg.TRENDING else "meanrev"
                    strat = self.momentum if strat_name == "momentum" else self.meanrev
                    sig = strat.evaluate(window, None)
                    if sig.action in ("long", "short"):
                        side = "Buy" if sig.action == "long" else "Sell"
                        fill = self._fill(nxt["open"], side, True)
                        stop = risk.initial_stop(fill, sig.atr, side, p.stop_loss_atr_mult)
                        qty = risk.size_position(equity, fill, stop, self.rp)
                        qty = (qty // p.min_qty) * p.min_qty
                        if qty >= p.min_qty:
                            fee = qty * fill * p.taker_fee_pct / 100
                            equity -= fee
                            pos = {"side": side, "qty": qty, "entry": fill,
                                   "stop": stop, "strategy": strat_name,
                                   "entry_fee": fee, "regime": regime}

            res.equity_curve.append(equity + self._unrealized(pos, float(nxt["close"])))

        if pos is not None:  # mark-to-market close at the end
            equity = self._close(res, pos, float(df5["close"].iloc[-1]), equity, "eod")
            res.equity_curve.append(equity)
        return res

    def _unrealized(self, pos, price: float) -> float:
        if pos is None:
            return 0.0
        d = 1 if pos["side"] == "Buy" else -1
        return (price - pos["entry"]) * pos["qty"] * d

    def _close(self, res: BTResult, pos: dict, fill: float, equity: float, reason: str) -> float:
        d = 1 if pos["side"] == "Buy" else -1
        gross = (fill - pos["entry"]) * pos["qty"] * d
        exit_fee = pos["qty"] * fill * self.p.taker_fee_pct / 100
        fees = pos["entry_fee"] + exit_fee
        net = gross - exit_fee  # entry fee already deducted from equity
        res.trades.append({"side": pos["side"], "qty": pos["qty"], "entry": pos["entry"],
                           "exit": fill, "pnl": round(gross - fees, 6), "fees": round(fees, 6),
                           "strategy": pos["strategy"], "regime": pos.get("regime"),
                           "reason": reason})
        return equity + net
