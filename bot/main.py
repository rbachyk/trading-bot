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
    if not cfg.exchange.testnet:
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
    Database(cfg.db.path).clear_halt()
    log.info("Halt cleared. Restart the engine (or it resumes next cycle).")


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="bot")
    parser.add_argument("command", choices=["run", "kill", "resume", "status"])
    args = parser.parse_args()
    {"run": cmd_run, "kill": cmd_kill, "resume": cmd_resume, "status": cmd_status}[args.command]()


if __name__ == "__main__":
    main()
