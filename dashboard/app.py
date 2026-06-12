"""Dashboard (Phase 2): FastAPI API + single-page UI.

Security model: binds to 127.0.0.1 by default. On the VPS, access it through an
SSH tunnel (`ssh -L 8080:127.0.0.1:8080 user@vps`). If DASHBOARD_TOKEN is set in
.env, every request must carry it (?token=... or X-Token header) — required if
you ever bind beyond localhost. A kill switch reachable from the open internet
without auth would itself be a capital-preservation failure.

Control semantics:
  stop   -> sets the halt flag: engine stops trading (positions/stops stay on)
  kill   -> KILL SWITCH: cancel all orders, market-close position, halt
  resume -> clears halt; engine resumes next cycle
Config editor: validates via the same pydantic models, writes config.yaml, and
tells you a restart is required — never hot-applied mid-position (Section D).

Run:  uvicorn dashboard.app:app --host 127.0.0.1 --port 8080
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from bot.config import ROOT, AppConfig, load_config
from bot.exchange.bybit_client import BybitClient
from bot.persistence.db import Database

app = FastAPI(title="trading-bot dashboard")

DAY = 86400.0


def _auth(request: Request) -> None:
    token = os.getenv("DASHBOARD_TOKEN", "")
    if token and request.query_params.get("token") != token \
            and request.headers.get("x-token") != token:
        raise HTTPException(401, "missing/invalid token")


def _db() -> Database:
    cfg = load_config()
    return Database(cfg.db.path)


def _client(cfg: AppConfig) -> BybitClient:
    return BybitClient(cfg.api_key, cfg.api_secret, cfg.exchange.testnet,
                       cfg.exchange.symbol, cfg.exchange.category,
                       tld=cfg.exchange.tld, demo=cfg.exchange.demo,
                       settle_coin=cfg.exchange.settle_coin)


# ---------- read APIs -------------------------------------------------------------
@app.get("/api/summary", dependencies=[Depends(_auth)])
def summary():
    db = _db()
    now = time.time()

    def window(since: float | None):
        q = "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, " \
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins " \
            "FROM trades WHERE status='closed'"
        args: tuple = ()
        if since is not None:
            q += " AND closed_ts >= ?"
            args = (since,)
        r = db.conn.execute(q, args).fetchone()
        n = r["n"] or 0
        return {"trades": n, "pnl": round(r["pnl"] or 0, 4),
                "win_rate": round((r["wins"] or 0) / n * 100, 1) if n else None}

    eq = db.conn.execute(
        "SELECT equity FROM equity_snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    return {
        "equity": eq["equity"] if eq else None,
        "halted": db.is_halted(),
        "halt_reason": db.get_state("halt_reason"),
        "regime": db.get_state("regime", "n/a"),
        "day": window(now - DAY),
        "week": window(now - 7 * DAY),
        "month": window(now - 30 * DAY),
        "all_time": window(None),
    }


@app.get("/api/equity", dependencies=[Depends(_auth)])
def equity_curve(points: int = 500):
    db = _db()
    rows = db.conn.execute(
        "SELECT ts, equity FROM equity_snapshots ORDER BY ts DESC LIMIT ?", (points,)
    ).fetchall()
    return [{"ts": r["ts"], "equity": r["equity"]} for r in reversed(rows)]


@app.get("/api/trades", dependencies=[Depends(_auth)])
def trades(limit: int = 50):
    db = _db()
    rows = db.conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/position", dependencies=[Depends(_auth)])
def position():
    cfg = load_config()
    return {"position": _client(cfg).get_position()}


# ---------- controls ----------------------------------------------------------------
@app.post("/api/control/stop", dependencies=[Depends(_auth)])
def control_stop():
    _db().halt("dashboard stop")
    return {"ok": True, "msg": "halted — open position and stop remain on the exchange"}


@app.post("/api/control/kill", dependencies=[Depends(_auth)])
def control_kill():
    cfg = load_config()
    db = _db()
    db.halt("dashboard KILL switch")
    _client(cfg).flatten()
    return {"ok": True, "msg": "all orders cancelled, position flattened, halted"}


@app.post("/api/control/resume", dependencies=[Depends(_auth)])
def control_resume():
    db = _db()
    db.clear_halt()
    db.reanchor_breakers()
    return {"ok": True, "msg": "halt cleared, breaker baselines re-anchored — engine resumes next cycle"}


# ---------- config editor -------------------------------------------------------------
CFG_PATH = Path(ROOT) / "config" / "config.yaml"


@app.get("/api/config", dependencies=[Depends(_auth)])
def get_config():
    return yaml.safe_load(CFG_PATH.read_text())


@app.post("/api/config", dependencies=[Depends(_auth)])
async def set_config(request: Request):
    raw = await request.json()
    try:
        AppConfig.model_validate({**raw, "api_key": "x", "api_secret": "x"})
    except ValidationError as e:
        raise HTTPException(422, detail=e.errors(include_url=False, include_context=False))
    CFG_PATH.write_text(yaml.safe_dump(raw, sort_keys=False))
    return {"ok": True, "msg": "saved — RESTART the bot service to apply (never hot-applied)"}


# ---------- optimizer proposals (Phase 3) ----------------------------------------------
def _proposals_table(db: Database) -> None:
    db.conn.execute(
        """CREATE TABLE IF NOT EXISTS proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
            params TEXT NOT NULL, evidence TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending')""")
    db.conn.commit()


@app.get("/api/proposals", dependencies=[Depends(_auth)])
def proposals():
    db = _db()
    _proposals_table(db)
    rows = db.conn.execute("SELECT * FROM proposals ORDER BY id DESC LIMIT 20").fetchall()
    import json as _json
    return [{**dict(r), "params": _json.loads(r["params"]),
             "evidence": _json.loads(r["evidence"])} for r in rows]


@app.post("/api/proposals/{pid}/apply", dependencies=[Depends(_auth)])
def apply_proposal(pid: int):
    """User approval step (Section F): merge proposed strategy/risk params into
    config.yaml (validated), mark applied. Restart required; commit to git with
    the evidence in the message."""
    import json as _json
    db = _db()
    _proposals_table(db)
    row = db.conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, "no such proposal")
    params = _json.loads(row["params"])
    raw = yaml.safe_load(CFG_PATH.read_text())
    for k, v in params.items():
        section = "risk" if k in ("stop_loss_atr_mult", "max_position_pct") else "strategy"
        raw[section][k] = v
    try:
        AppConfig.model_validate({**raw, "api_key": "x", "api_secret": "x"})
    except ValidationError as e:
        raise HTTPException(422, detail=e.errors(include_url=False, include_context=False))
    CFG_PATH.write_text(yaml.safe_dump(raw, sort_keys=False))
    db.conn.execute("UPDATE proposals SET status='applied' WHERE id=?", (pid,))
    db.conn.commit()
    return {"ok": True, "msg": "applied to config.yaml — RESTART the bot, then commit: "
            f"git commit -am 'tune: apply proposal #{pid} (walk-forward evidence in dashboard)'"}


@app.post("/api/proposals/{pid}/dismiss", dependencies=[Depends(_auth)])
def dismiss_proposal(pid: int):
    db = _db()
    _proposals_table(db)
    db.conn.execute("UPDATE proposals SET status='dismissed' WHERE id=?", (pid,))
    db.conn.commit()
    return {"ok": True}


# ---------- optimizer proposals (Section F) ----------------------------------------------
@app.get("/api/proposals", dependencies=[Depends(_auth)])
def proposals():
    import json as _json
    return [{**dict(r), "params": _json.loads(r["params"]),
             "evidence": _json.loads(r["evidence"])} for r in _db().proposals()]


@app.post("/api/proposals/{pid}/approve", dependencies=[Depends(_auth)])
def approve_proposal(pid: int):
    """Apply proposed strategy/risk params to config.yaml (validated), mark approved.
    Requires bot restart; commit to git with the evidence (Section F)."""
    import json as _json
    db = _db()
    rows = [r for r in db.proposals() if r["id"] == pid]
    if not rows:
        raise HTTPException(404, "no such proposal")
    if rows[0]["status"] != "pending":
        raise HTTPException(409, f"proposal is {rows[0]['status']}")
    params = _json.loads(rows[0]["params"])

    raw = yaml.safe_load(CFG_PATH.read_text())
    for k, v in params.items():
        section = "risk" if k in ("stop_loss_atr_mult", "max_position_pct") else "strategy"
        raw[section][k] = v
    try:
        AppConfig.model_validate({**raw, "api_key": "x", "api_secret": "x"})
    except ValidationError as e:
        raise HTTPException(422, detail=e.errors(include_url=False, include_context=False))
    CFG_PATH.write_text(yaml.safe_dump(raw, sort_keys=False))
    db.set_proposal_status(pid, "approved")
    return {"ok": True, "msg": (
        "applied to config.yaml — RESTART the bot, then commit: "
        f"git commit -am 'tune: apply proposal #{pid} (see proposals table for evidence)'")}


@app.post("/api/proposals/{pid}/reject", dependencies=[Depends(_auth)])
def reject_proposal(pid: int):
    _db().set_proposal_status(pid, "rejected")
    return {"ok": True}


# ---------- UI ------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "index.html").read_text()
