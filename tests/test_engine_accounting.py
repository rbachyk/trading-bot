"""Regression tests for the PnL-bookkeeping and epoch bugs (dashboard screenshot,
2026-06-11): stop-outs booked as pnl=0, equity scales mixed across environments."""
import os
import time

os.environ.setdefault("BYBIT_API_KEY", "t")
os.environ.setdefault("BYBIT_API_SECRET", "t")

import pytest

from bot.persistence.db import Database
from bot.risk import manager as risk


# ---- estimate_net_pnl: the fallback math that replaced the hardcoded 0.0 ----
def test_short_stopped_higher_is_a_loss():
    net, fees = risk.estimate_net_pnl("Sell", 0.05, 1671.5, 1679.89, 0.055)
    assert net < 0                       # the screenshot trade: must NOT be 0
    assert net == pytest.approx(-0.05 * (1679.89 - 1671.5) - fees, abs=1e-9)
    assert fees > 0


def test_long_win_is_positive_net_of_fees():
    net, fees = risk.estimate_net_pnl("Buy", 0.1, 2000, 2050, 0.055)
    assert net == pytest.approx(0.1 * 50 - fees, abs=1e-9)
    assert net > 0


def test_tiny_move_loses_to_fees():
    net, _ = risk.estimate_net_pnl("Buy", 0.1, 2000, 2000.5, 0.055)
    assert net < 0  # fees are first-class at 5m


# ---- epoch-scoped breaker baselines -------------------------------------------------
def test_peak_equity_scoped_to_epoch(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    t0 = time.time()
    # old environment: huge testnet balance logged
    db.conn.execute("INSERT INTO equity_snapshots(ts, equity) VALUES(?, ?)", (t0 - 100, 50_000.0))
    # new epoch starts; small demo-capped equity
    epoch = t0 - 10
    db.conn.execute("INSERT INTO equity_snapshots(ts, equity) VALUES(?, ?)", (t0 - 5, 100.0))
    db.conn.commit()
    assert db.peak_equity() == 50_000.0                 # unscoped: the old bug
    assert db.peak_equity(since=epoch) == 100.0          # scoped: correct
    # the false halt: 100 vs 50k peak looks like -99.8% DD; scoped it's 0%
    p = risk.RiskParams(1.5, 4.0, 12.0, 2.0, 1.0)
    assert risk.drawdown_breached(db.peak_equity(), 100.0, p) is True
    assert risk.drawdown_breached(db.peak_equity(since=epoch), 100.0, p) is False


def test_resume_reanchors_breakers(tmp_path):
    """A drawdown halt must not re-trip immediately after a deliberate resume."""
    db = Database(str(tmp_path / "r.db"))
    t0 = time.time()
    db.set_state("epoch_start", str(t0 - 1000))
    db.conn.execute("INSERT INTO equity_snapshots(ts, equity) VALUES(?, ?)", (t0 - 900, 120.0))
    db.conn.execute("INSERT INTO equity_snapshots(ts, equity) VALUES(?, ?)", (t0 - 10, 100.0))
    db.conn.commit()
    db.halt("MAX DRAWDOWN")
    p = risk.RiskParams(1.5, 4.0, 12.0, 2.0, 1.0)

    epoch = float(db.get_state("epoch_start"))
    assert risk.drawdown_breached(db.peak_equity(since=epoch), 100.0, p) is True  # would re-trip

    db.clear_halt()
    db.reanchor_breakers()
    epoch = float(db.get_state("epoch_start"))
    assert risk.drawdown_breached(max(db.peak_equity(since=epoch), 100.0), 100.0, p) is False
    assert db.is_halted() is False
