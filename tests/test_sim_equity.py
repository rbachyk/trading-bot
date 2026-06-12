"""Regression (dashboard 2026-06-12 09:34): epoch rebase reset virtual equity to
100, erasing the -0.33 of recorded history. Sim equity is now anchored to the
bot's own trade ledger and must survive epochs, restarts, and wallet drift."""
import time

import pytest

from bot.persistence.db import Database
from bot.risk import manager as risk


def test_sim_equity_formula():
    assert risk.sim_equity(100, -0.33, 0.0) == pytest.approx(99.67)
    assert risk.sim_equity(100, 2.5, -0.4) == pytest.approx(102.1)
    assert risk.sim_equity(100, -150, 0) == 0.0  # floored


def test_screenshot_case_survives_epoch_change(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    tid = db.open_trade("ETHUSDT", "Buy", 0.15, 1662.1, 1655.0, "meanrev", "RANGING")
    db.close_trade(tid, 1661.95, pnl=-0.33, fees=0.18)
    # epoch changes (deploys, resumes, env switches) do not touch the ledger:
    db.set_state("epoch_start", str(time.time()))
    assert risk.sim_equity(100, db.realized_pnl_total(), 0.0) == pytest.approx(99.67)


def test_cap_raise_is_adding_funds(tmp_path):
    db = Database(str(tmp_path / "f.db"))
    tid = db.open_trade("ETHUSDT", "Buy", 0.1, 2000, 1960, "momentum", "TRENDING")
    db.close_trade(tid, 2030, pnl=2.7, fees=0.3)
    # ladder step 100 -> 1000 keeps earned pnl
    assert risk.sim_equity(1000, db.realized_pnl_total(), 0.0) == pytest.approx(1002.7)


def test_realized_pnl_ignores_open_trades(tmp_path):
    db = Database(str(tmp_path / "o.db"))
    db.open_trade("ETHUSDT", "Sell", 0.1, 2000, 2040, "momentum", "TRENDING")
    assert db.realized_pnl_total() == 0.0
