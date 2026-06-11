"""Telegram alerts via the plain Bot API (no extra SDK).

Setup (docs/runbook.md): create a bot with @BotFather -> TELEGRAM_BOT_TOKEN;
message the bot once, read chat id from getUpdates -> TELEGRAM_CHAT_ID.
Both go in .env. If unset, the notifier is a silent no-op so the bot still runs.

Alerts must NEVER break trading: every send failure is swallowed and logged.
"""
from __future__ import annotations

import logging
import os
from typing import Callable

import requests

log = logging.getLogger("alerts")


class Notifier:
    def __init__(self, on_error: Callable[[str, str], None] | None = None):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        self._on_error = on_error or (lambda ctx, msg: None)
        if not self.enabled:
            log.info("Telegram alerts disabled (no TELEGRAM_BOT_TOKEN/CHAT_ID in .env).")

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=5,
            ).raise_for_status()
        except Exception as e:  # alerts are best-effort, trading continues
            log.warning("telegram send failed: %s", e)
            self._on_error("telegram", repr(e))

    # convenience wrappers used by the engine
    def fill(self, side: str, qty: float, price: float, stop: float, strategy: str) -> None:
        self.send(f"🟢 ENTER {side} {qty} @ ~{price:.2f} | stop {stop:.2f} | {strategy}")

    def exited(self, side: str, price: float, pnl: float, reason: str) -> None:
        emoji = "✅" if pnl >= 0 else "🔻"
        self.send(f"{emoji} EXIT {side} @ ~{price:.2f} | net PnL {pnl:+.2f} | {reason}")

    def breaker(self, reason: str) -> None:
        self.send(f"🛑 HALTED: {reason}\nPositions flattened. `resume` required to continue.")

    def regime(self, old: str, new: str) -> None:
        self.send(f"🔄 Regime: {old} → {new}")

    def error(self, context: str, message: str) -> None:
        self.send(f"⚠️ Error [{context}]: {message[:300]}")
