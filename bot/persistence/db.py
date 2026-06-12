"""SQLite persistence: trades, orders, equity snapshots, regime states, errors, bot state.
Single source of truth (project Section D)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL,
    exit_price REAL,
    stop_price REAL,
    pnl REAL,
    fees REAL,
    strategy TEXT,
    regime TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- open | closed
    closed_ts REAL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    order_id TEXT,
    symbol TEXT, side TEXT, order_type TEXT,
    qty REAL, price REAL, stop_loss REAL,
    status TEXT, raw TEXT
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS regime_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    regime TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    context TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    params TEXT NOT NULL,       -- JSON: proposed strategy/risk params
    evidence TEXT NOT NULL,     -- JSON: in/out-of-sample walk-forward results
    status TEXT NOT NULL DEFAULT 'pending'   -- pending | approved | rejected
);
"""


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- bot state / halt flags -------------------------------------------
    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO bot_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_state(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def is_halted(self) -> bool:
        return self.get_state("halted", "0") == "1"

    def halt(self, reason: str) -> None:
        self.set_state("halted", "1")
        self.set_state("halt_reason", reason)

    def clear_halt(self) -> None:
        self.set_state("halted", "0")
        self.set_state("halt_reason", "")

    def reanchor_breakers(self) -> None:
        """Called on RESUME (a deliberate human ack after reviewing a halt):
        breaker baselines (peak / day-start) re-anchor at the present, otherwise
        a drawdown halt re-trips on the very next cycle and resume is useless.
        The cap baseline is NOT touched — simulated PnL history stays truthful."""
        import time as _t
        self.set_state("epoch_start", str(_t.time()))

    # ---- logging -----------------------------------------------------------
    def log_equity(self, equity: float) -> None:
        self.conn.execute(
            "INSERT INTO equity_snapshots(ts, equity) VALUES(?, ?)", (time.time(), equity)
        )
        self.conn.commit()

    def log_error(self, context: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO errors(ts, context, message) VALUES(?, ?, ?)",
            (time.time(), context, message),
        )
        self.conn.commit()

    def log_order(self, **kw) -> None:
        self.conn.execute(
            "INSERT INTO orders(ts, order_id, symbol, side, order_type, qty, price, stop_loss, status, raw) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(), kw.get("order_id"), kw.get("symbol"), kw.get("side"),
                kw.get("order_type"), kw.get("qty"), kw.get("price"),
                kw.get("stop_loss"), kw.get("status"), kw.get("raw", ""),
            ),
        )
        self.conn.commit()

    def open_trade(self, symbol: str, side: str, qty: float, entry: float,
                   stop: float, strategy: str, regime: str = "n/a") -> int:
        cur = self.conn.execute(
            "INSERT INTO trades(ts, symbol, side, qty, entry_price, stop_price, strategy, regime, status) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'open')",
            (time.time(), symbol, side, qty, entry, stop, strategy, regime),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close_trade(self, trade_id: int, exit_price: float, pnl: float, fees: float) -> None:
        self.conn.execute(
            "UPDATE trades SET exit_price=?, pnl=?, fees=?, status='closed', closed_ts=? WHERE id=?",
            (exit_price, pnl, fees, time.time(), trade_id),
        )
        self.conn.commit()

    def get_open_trade(self):
        return self.conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    # ---- equity queries for risk checks -------------------------------------
    def peak_equity(self, since: float = 0.0) -> float:
        """Peak since `since` (epoch start). Mixing equity scales from different
        environments/caps produced false drawdown halts — never query across epochs."""
        row = self.conn.execute(
            "SELECT MAX(equity) AS m FROM equity_snapshots WHERE ts >= ?", (since,)).fetchone()
        return float(row["m"]) if row and row["m"] is not None else 0.0

    def day_start_equity(self, day_start_ts: float) -> float | None:
        row = self.conn.execute(
            "SELECT equity FROM equity_snapshots WHERE ts >= ? ORDER BY ts ASC LIMIT 1",
            (day_start_ts,),
        ).fetchone()
        return float(row["equity"]) if row else None

    # ---- optimizer proposals (Section F: supervised improvement loop) ----------
    def add_proposal(self, params_json: str, evidence_json: str) -> int:
        import time as _t
        cur = self.conn.execute(
            "INSERT INTO proposals(ts, params, evidence) VALUES(?, ?, ?)",
            (_t.time(), params_json, evidence_json))
        self.conn.commit()
        return int(cur.lastrowid)

    def proposals(self, status: str | None = None):
        q = "SELECT * FROM proposals"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        return self.conn.execute(q + " ORDER BY id DESC", args).fetchall()

    def set_proposal_status(self, pid: int, status: str) -> None:
        self.conn.execute("UPDATE proposals SET status=? WHERE id=?", (status, pid))
        self.conn.commit()
