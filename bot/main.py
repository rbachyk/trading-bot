"""CLI: python -m bot.main {run|kill|resume|status}

kill   = KILL SWITCH (Section C): cancels all orders, market-closes any position,
         sets DB halt flag so a running engine stops trading immediately.
resume = clears the halt flag (deliberate human action required).
"""
from __future__ import annotations

import argparse
import logging
import sys

from bot.config import load_config
from bot.engine import Engine
from bot.exchange.bybit_client import BybitClient
from bot.persistence.db import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


def cmd_run() -> None:
    cfg = load_config()
    if not cfg.exchange.testnet and not cfg.exchange.demo:
        print("MAINNET MODE. Live Gate (docs/runbook.md Section H) must be fully passed.")
        if input("Type 'I ACCEPT FULL LOSS RISK' to continue: ") != "I ACCEPT FULL LOSS RISK":
            sys.exit("Aborted.")
    Engine(cfg).start()


def cmd_kill() -> None:
    cfg = load_config()
    db = Database(cfg.db.path)
    db.halt("manual kill switch")
    client = BybitClient(cfg.api_key, cfg.api_secret, cfg.exchange.testnet,
                         cfg.exchange.symbol, cfg.exchange.category, on_error=db.log_error)
    client.flatten()
    log.info("KILL SWITCH: all orders cancelled, position flattened, bot halted.")


def cmd_resume() -> None:
    cfg = load_config()
    db = Database(cfg.db.path)
    db.clear_halt()
    db.reanchor_breakers()
    log.info("Halt cleared; breaker baselines re-anchored at current equity. "
             "Engine resumes next cycle.")


def cmd_status() -> None:
    cfg = load_config()
    db = Database(cfg.db.path)
    client = BybitClient(cfg.api_key, cfg.api_secret, cfg.exchange.testnet,
                         cfg.exchange.symbol, cfg.exchange.category, on_error=db.log_error)
    print(f"mode      : {'TESTNET' if cfg.exchange.testnet else 'MAINNET'}")
    print(f"halted    : {db.is_halted()} ({db.get_state('halt_reason')})")
    print(f"equity    : {client.get_equity():.2f} USDT")
    print(f"position  : {client.get_position()}")
    print(f"open trade: {dict(db.get_open_trade()) if db.get_open_trade() else None}")


def cmd_instruments() -> None:
    """List ETH contracts actually tradable in the configured environment."""
    cfg = load_config()
    from pybit.unified_trading import HTTP
    http = HTTP(testnet=cfg.exchange.testnet, demo=cfg.exchange.demo,
                api_key=cfg.api_key, api_secret=cfg.api_secret, tld=cfg.exchange.tld)
    resp = http.get_instruments_info(category=cfg.exchange.category, baseCoin="ETH")
    print(f"env: testnet={cfg.exchange.testnet} demo={cfg.exchange.demo} tld={cfg.exchange.tld}")
    for i in resp["result"]["list"]:
        lot = i.get("lotSizeFilter", {})
        print(f"  {i['symbol']:<16} status={i.get('status'):<10} quote={i.get('quoteCoin'):<5} "
              f"minQty={lot.get('minOrderQty')} minNotional={lot.get('minNotionalValue', '-')}")


def cmd_gate() -> None:
    """Live Gate (Section H): auto-check what can be checked, list the rest."""
    import os
    import time as _t
    cfg = load_config()
    db = Database(cfg.db.path)
    now = _t.time()

    rows = db.conn.execute(
        "SELECT ts FROM equity_snapshots WHERE ts >= ? ORDER BY ts", (now - 72 * 3600,)
    ).fetchall()
    cont = False
    if rows and rows[0]["ts"] <= now - 71.5 * 3600:
        gaps = [b["ts"] - a["ts"] for a, b in zip(rows, rows[1:])]
        cont = bool(gaps) and max(gaps) < 300  # no gap > 5 min
    tg = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    halts = db.conn.execute(
        "SELECT COUNT(*) c FROM errors WHERE context='risk' OR message LIKE '%kill%'"
    ).fetchone()["c"]

    checks = [
        (cont, "1. >=72h continuous run (equity snapshots, no gap >5min)"),
        (halts > 0, "2. breaker/kill events recorded (forced-failure tests done?)"),
        (None, "3. restart-reconciliation verified with an open position [MANUAL]"),
        (tg, "4. telegram alert delivery configured (send verified end-to-end? [MANUAL])"),
        (None, "5. capital fully loseable; sizing/leverage/drawdown re-confirmed [MANUAL]"),
    ]
    ok = True
    for state, label in checks:
        mark = "?" if state is None else ("PASS" if state else "FAIL")
        ok = ok and state is not False
        print(f"[{mark:4}] {label}")
    extra = []
    if cfg.exchange.demo or cfg.exchange.testnet:
        extra.append("config still demo/testnet (expected until gate passes)")
    if cfg.risk.equity_cap is not None:
        extra.append("equity_cap is SET — must be null for live")
    for e in extra:
        print(f"[NOTE] {e}")
    print("Gate", "NOT passed — mainnet help stays refused." if not ok or extra else
          "auto-checks pass — manual items remain your call.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="bot")
    parser.add_argument("command", choices=["run", "kill", "resume", "status", "instruments", "gate"])
    args = parser.parse_args()
    {"run": cmd_run, "kill": cmd_kill, "resume": cmd_resume,
     "status": cmd_status, "instruments": cmd_instruments, "gate": cmd_gate}[args.command]()


if __name__ == "__main__":
    main()
