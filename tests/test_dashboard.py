"""Dashboard API tests (no exchange calls)."""
import os
import time

os.environ.setdefault("BYBIT_API_KEY", "test")
os.environ.setdefault("BYBIT_API_SECRET", "test")

from fastapi.testclient import TestClient  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.persistence.db import Database  # noqa: E402
from dashboard.app import app  # noqa: E402

client = TestClient(app)


def seed():
    db = Database(load_config().db.path)
    db.log_equity(100.0)
    tid = db.open_trade("ETHUSDT", "Buy", 0.05, 2000, 1960, "momentum", "TRENDING")
    db.close_trade(tid, 2020, pnl=0.9, fees=0.1)
    return db


def test_summary_and_equity():
    seed()
    s = client.get("/api/summary").json()
    assert s["all_time"]["trades"] >= 1
    assert isinstance(s["all_time"]["pnl"], float)
    eq = client.get("/api/equity").json()
    assert eq and "equity" in eq[0]


def test_stop_and_resume_toggle_halt():
    db = seed()
    assert client.post("/api/control/stop").json()["ok"]
    assert db.is_halted() is True
    assert client.post("/api/control/resume").json()["ok"]
    assert Database(load_config().db.path).is_halted() is False


def test_config_editor_rejects_invalid():
    cfg = client.get("/api/config").json()
    bad = dict(cfg)
    bad["exchange"] = dict(cfg["exchange"], leverage=50)  # violates risk policy
    r = client.post("/api/config", json=bad)
    assert r.status_code == 422


def test_config_editor_roundtrip():
    cfg = client.get("/api/config").json()
    r = client.post("/api/config", json=cfg)
    assert r.status_code == 200 and "RESTART" in r.json()["msg"]


def test_token_auth_when_set():
    os.environ["DASHBOARD_TOKEN"] = "s3cret"
    try:
        assert client.get("/api/summary").status_code == 401
        assert client.get("/api/summary?token=s3cret").status_code == 200
    finally:
        del os.environ["DASHBOARD_TOKEN"]




def test_proposals_review_and_approve(tmp_path):
    import json
    from pathlib import Path
    import dashboard.app as dash

    db = seed()
    pid = db.add_proposal(
        json.dumps({"ema_fast": 10, "stop_loss_atr_mult": 1.5}),
        json.dumps({"mean_oos_best_pct": 2.1, "mean_oos_current_pct": 0.4,
                    "candidate_stability": 0.6, "windows": [], "caveats": "historical"}))

    ps = client.get("/api/proposals").json()
    assert any(p["id"] == pid and p["status"] == "pending" for p in ps)

    original = dash.CFG_PATH.read_text()
    try:
        r = client.post(f"/api/proposals/{pid}/approve")
        assert r.status_code == 200 and "RESTART" in r.json()["msg"]
        import yaml as _y
        raw = _y.safe_load(dash.CFG_PATH.read_text())
        assert raw["strategy"]["ema_fast"] == 10
        assert raw["risk"]["stop_loss_atr_mult"] == 1.5
        # approving twice must conflict
        assert client.post(f"/api/proposals/{pid}/approve").status_code == 409
    finally:
        dash.CFG_PATH.write_text(original)


def test_proposal_reject():
    import json
    db = seed()
    pid = db.add_proposal(json.dumps({"ema_fast": 12}), json.dumps({"windows": []}))
    assert client.post(f"/api/proposals/{pid}/reject").json()["ok"]
    assert [p for p in client.get("/api/proposals").json() if p["id"] == pid][0]["status"] == "rejected"
