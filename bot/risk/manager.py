"""Risk module (project Section C). Pure functions where possible so they are unit-testable.

Every order flows through size_position() and initial_stop(); the engine enforces
daily_loss_breached() and drawdown_breached() every loop. The kill switch lives in
bot/main.py (CLI) and DB halt flag.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskParams:
    max_position_pct: float      # % of equity risked between entry and stop
    daily_loss_halt_pct: float
    max_drawdown_pct: float
    stop_loss_atr_mult: float
    leverage: float


def initial_stop(entry: float, atr: float, side: str, atr_mult: float) -> float:
    """Exchange-side stop price. side: 'Buy' (long) or 'Sell' (short)."""
    if atr <= 0 or entry <= 0:
        raise ValueError("entry and atr must be positive")
    dist = atr * atr_mult
    return entry - dist if side == "Buy" else entry + dist


def size_position(equity: float, entry: float, stop: float, p: RiskParams) -> float:
    """Qty such that (entry->stop) loss == max_position_pct of equity,
    capped so notional never exceeds equity * leverage.

    Returns 0.0 if inputs are degenerate — caller must treat 0 as 'no trade'.
    """
    if equity <= 0 or entry <= 0:
        return 0.0
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return 0.0
    risk_capital = equity * (p.max_position_pct / 100.0)
    qty = risk_capital / stop_dist
    # Hard notional cap: leverage policy (Section C, capped <=2x in config validation)
    max_qty = (equity * p.leverage) / entry
    return round(min(qty, max_qty), 3)  # ETHUSDT qty step 0.01; 3dp is safe [UNVERIFIED step — engine re-rounds via instrument info]


def daily_loss_breached(day_start_equity: float, equity_now: float, p: RiskParams) -> bool:
    if day_start_equity <= 0:
        return False
    loss_pct = (day_start_equity - equity_now) / day_start_equity * 100.0
    return loss_pct >= p.daily_loss_halt_pct


def drawdown_breached(peak_equity: float, equity_now: float, p: RiskParams) -> bool:
    if peak_equity <= 0:
        return False
    dd_pct = (peak_equity - equity_now) / peak_equity * 100.0
    return dd_pct >= p.max_drawdown_pct


def trailing_stop(side: str, current_stop: float, close: float, atr: float, mult: float) -> float:
    """Ratchet-only trailing stop: moves with price, never against it."""
    if side == "Buy":
        candidate = close - atr * mult
        return max(current_stop, candidate)
    candidate = close + atr * mult
    return min(current_stop, candidate)


def estimate_net_pnl(side: str, qty: float, entry: float, exit_price: float,
                     taker_fee_pct: float) -> tuple[float, float]:
    """(net_pnl, fees) estimate for a round trip. Fallback only — prefer the
    exchange's own closed-PnL record, which includes its exact fee math."""
    d = 1 if side == "Buy" else -1
    gross = (exit_price - entry) * qty * d
    fees = qty * (entry + exit_price) * taker_fee_pct / 100.0
    return gross - fees, fees


def virtual_equity(real_equity: float, baseline: float, cap: float) -> float:
    """Small-account simulation: pretend the account started at `cap` when the real
    (e.g. testnet) wallet held `baseline`. Virtual equity = cap + PnL since then.
    Used for BOTH sizing and breakers so a 100k-USDC testnet wallet behaves like
    the $100 account you actually plan to fund. Floored at 0."""
    return max(0.0, cap + (real_equity - baseline))


def round_trip_cost_pct(taker_fee_pct: float, slippage_pct: float) -> float:
    """Total expected cost of one round trip (entry+exit), in %. Used to sanity-check
    that expected move per trade exceeds costs — fees are first-class at 5m."""
    return 2 * (taker_fee_pct + slippage_pct)
