"""Risk module tests — these must pass before any other work (priority 1)."""
import pytest

from bot.risk import manager as risk

P = risk.RiskParams(
    max_position_pct=1.5,
    daily_loss_halt_pct=4.0,
    max_drawdown_pct=12.0,
    stop_loss_atr_mult=2.0,
    leverage=1.0,
)


# ---- initial_stop -----------------------------------------------------------
def test_stop_below_entry_for_long():
    assert risk.initial_stop(2000, 20, "Buy", 2.0) == 1960


def test_stop_above_entry_for_short():
    assert risk.initial_stop(2000, 20, "Sell", 2.0) == 2040


def test_stop_rejects_bad_inputs():
    with pytest.raises(ValueError):
        risk.initial_stop(2000, 0, "Buy", 2.0)
    with pytest.raises(ValueError):
        risk.initial_stop(0, 20, "Buy", 2.0)


# ---- size_position -----------------------------------------------------------
def test_size_risks_exactly_max_position_pct():
    equity, entry, stop = 10_000, 2000, 1960  # 40 USDT stop distance
    qty = risk.size_position(equity, entry, stop, P)
    loss_at_stop = qty * (entry - stop)
    assert loss_at_stop == pytest.approx(equity * 0.015, rel=0.01)


def test_size_capped_by_leverage_notional():
    # Tiny stop distance would imply huge qty; leverage cap must bind
    equity, entry, stop = 10_000, 2000, 1999.5
    qty = risk.size_position(equity, entry, stop, P)
    assert qty * entry <= equity * P.leverage + 1e-6


def test_size_zero_on_degenerate_inputs():
    assert risk.size_position(0, 2000, 1960, P) == 0.0
    assert risk.size_position(10_000, 2000, 2000, P) == 0.0  # stop == entry


# ---- circuit breakers -----------------------------------------------------------
def test_daily_loss_breaker_trips_at_threshold():
    assert risk.daily_loss_breached(10_000, 9600, P) is True      # exactly -4%
    assert risk.daily_loss_breached(10_000, 9601, P) is False
    assert risk.daily_loss_breached(10_000, 9000, P) is True


def test_daily_loss_ignores_gains():
    assert risk.daily_loss_breached(10_000, 11_000, P) is False


def test_drawdown_kill_switch_trips_at_threshold():
    assert risk.drawdown_breached(10_000, 8800, P) is True        # exactly -12%
    assert risk.drawdown_breached(10_000, 8801, P) is False


def test_breakers_safe_on_zero_baselines():
    assert risk.daily_loss_breached(0, 5000, P) is False
    assert risk.drawdown_breached(0, 5000, P) is False


# ---- trailing stop -----------------------------------------------------------
def test_trailing_stop_ratchets_up_for_long_never_down():
    s1 = risk.trailing_stop("Buy", 1960, 2100, 20, 2.5)
    assert s1 == 2050  # moved up
    s2 = risk.trailing_stop("Buy", s1, 2000, 20, 2.5)
    assert s2 == s1    # price fell -> stop holds


def test_trailing_stop_ratchets_down_for_short_never_up():
    s1 = risk.trailing_stop("Sell", 2040, 1900, 20, 2.5)
    assert s1 == 1950
    s2 = risk.trailing_stop("Sell", s1, 2000, 20, 2.5)
    assert s2 == s1


# ---- costs -----------------------------------------------------------
def test_round_trip_cost():
    assert risk.round_trip_cost_pct(0.055, 0.02) == pytest.approx(0.15)
