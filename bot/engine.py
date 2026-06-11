"""Main engine loop.

Order of operations each cycle (priority stack: capital preservation first):
  1. Halt flag check (kill switch / circuit breakers set it)
  2. Equity snapshot -> daily-loss + drawdown checks; breach => flatten + halt
  3. Trailing-stop maintenance on open position (exchange-side)
  4. Strategy signal on closed 5m candles -> sized entry/exit through risk module

Startup: reconcile DB state with the exchange (Section D: process restart safety).
"""
from __future__ import annotations

import datetime as dt
import logging
import time

from bot.config import AppConfig
from bot.exchange.bybit_client import BybitClient
from bot.persistence.db import Database
from bot.risk import manager as risk
from bot.strategy.momentum import MomentumStrategy

log = logging.getLogger("engine")


class Engine:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.db = Database(cfg.db.path)
        self.client = BybitClient(
            cfg.api_key, cfg.api_secret, cfg.exchange.testnet,
            cfg.exchange.symbol, cfg.exchange.category, tld=cfg.exchange.tld,
            on_error=self.db.log_error,
        )
        s = cfg.strategy
        self.strategy = MomentumStrategy(s.ema_fast, s.ema_slow, s.donchian_period,
                                         s.atr_period, s.trail_atr_mult)
        self.risk_params = risk.RiskParams(
            cfg.risk.max_position_pct, cfg.risk.daily_loss_halt_pct,
            cfg.risk.max_drawdown_pct, cfg.risk.stop_loss_atr_mult,
            cfg.exchange.leverage,
        )
        self.running = False

    # ---- startup ---------------------------------------------------------------
    def reconcile(self) -> None:
        """DB says one thing, exchange says another -> exchange wins."""
        pos = self.client.get_position()
        open_trade = self.db.get_open_trade()
        if pos and not open_trade:
            log.warning("Exchange position with no DB trade — adopting it.")
            stop = pos["stop"] or 0.0
            self.db.open_trade(self.cfg.exchange.symbol, pos["side"], pos["qty"],
                               pos["entry"], stop, "reconciled")
            if not pos["stop"]:
                # Position without a stop violates Section C — attach one immediately
                df = self.client.get_klines(self.cfg.timeframes.execution)
                sig = self.strategy.evaluate(df, pos["side"])
                sl = risk.initial_stop(pos["entry"], sig.atr, pos["side"],
                                       self.risk_params.stop_loss_atr_mult)
                self.client.update_stop(sl)
                log.warning("Attached missing exchange-side stop at %.2f", sl)
        elif open_trade and not pos:
            log.warning("DB open trade %s but no exchange position — marking closed (stopped out or closed externally).", open_trade["id"])
            df = self.client.get_klines(self.cfg.timeframes.execution)
            self.db.close_trade(open_trade["id"], float(df["close"].iloc[-1]), 0.0, 0.0)

    def start(self) -> None:
        mode = "TESTNET" if self.cfg.exchange.testnet else "MAINNET"
        log.info("Starting engine on %s (%s)", self.cfg.exchange.symbol, mode)
        self.client.set_leverage(self.cfg.exchange.leverage)
        self.reconcile()
        self.running = True
        self.loop()

    # ---- per-cycle risk gates ---------------------------------------------------
    def _risk_gates(self, equity: float) -> bool:
        """Returns True if trading may continue this cycle."""
        if self.db.is_halted():
            log.info("Halted: %s", self.db.get_state("halt_reason"))
            return False

        day_start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        dse = self.db.day_start_equity(day_start)
        if dse and risk.daily_loss_breached(dse, equity, self.risk_params):
            self._emergency_flatten(f"DAILY LOSS CIRCUIT BREAKER: -{self.risk_params.daily_loss_halt_pct}% reached")
            return False

        peak = max(self.db.peak_equity(), equity)
        if risk.drawdown_breached(peak, equity, self.risk_params):
            self._emergency_flatten(f"MAX DRAWDOWN KILL SWITCH: -{self.risk_params.max_drawdown_pct}% from peak")
            return False
        return True

    def _emergency_flatten(self, reason: str) -> None:
        log.error(reason)
        self.db.log_error("risk", reason)
        try:
            self.client.flatten()
        finally:
            self.db.halt(reason)

    # ---- main loop ----------------------------------------------------------------
    def loop(self) -> None:
        while self.running:
            try:
                self.cycle()
            except Exception as e:  # never let one bad cycle kill the process
                log.exception("cycle error")
                self.db.log_error("cycle", repr(e))
            time.sleep(self.cfg.loop.poll_seconds)

    def _effective_equity(self, real_equity: float) -> float:
        """Apply the equity_cap simulation if configured (see config.yaml)."""
        cap = self.cfg.risk.equity_cap
        if cap is None:
            return real_equity
        stored_cap = self.db.get_state("cap_value")
        if stored_cap != str(cap):  # cap newly set or changed -> rebase
            self.db.set_state("cap_value", str(cap))
            self.db.set_state("cap_baseline", str(real_equity))
            log.info("equity_cap simulation (re)based: cap=%.2f baseline=%.2f", cap, real_equity)
        baseline = float(self.db.get_state("cap_baseline", str(real_equity)))
        return risk.virtual_equity(real_equity, baseline, cap)

    def cycle(self) -> None:
        equity = self._effective_equity(self.client.get_equity())
        self.db.log_equity(equity)  # snapshots are virtual when equity_cap is set

        if not self._risk_gates(equity):
            return

        df = self.client.get_klines(self.cfg.timeframes.execution, limit=200)
        pos = self.client.get_position()
        side = pos["side"] if pos else None
        sig = self.strategy.evaluate(df, side)

        if pos:
            open_trade = self.db.get_open_trade()
            # exchange stop fired since last cycle? get_position already None-handles that;
            # here pos exists, so maintain the trail
            if pos["stop"]:
                new_stop = risk.trailing_stop(pos["side"], pos["stop"], sig.close,
                                              sig.atr, self.strategy.trail_atr_mult)
                if abs(new_stop - pos["stop"]) / pos["stop"] > 0.0005:
                    self.client.update_stop(new_stop)
                    log.info("Trailing stop -> %.2f", new_stop)
            if sig.action == "exit":
                self.client.close_position_market()
                fees = pos["qty"] * sig.close * (self.cfg.costs.taker_fee_pct / 100)
                pnl = (sig.close - pos["entry"]) * pos["qty"] * (1 if pos["side"] == "Buy" else -1)
                if open_trade:
                    self.db.close_trade(open_trade["id"], sig.close, pnl - fees, fees)
                log.info("EXIT %s: %s | pnl(net est) %.2f", pos["side"], sig.reason, pnl - fees)
            return

        # detect exchange-side stop-out: DB open trade but no position
        open_trade = self.db.get_open_trade()
        if open_trade:
            self.db.close_trade(open_trade["id"], sig.close, 0.0, 0.0)
            log.info("Position closed by exchange stop (trade %s).", open_trade["id"])

        if sig.action in ("long", "short"):
            order_side = "Buy" if sig.action == "long" else "Sell"
            stop = risk.initial_stop(sig.close, sig.atr, order_side,
                                     self.risk_params.stop_loss_atr_mult)
            qty = self.client.round_qty(
                risk.size_position(equity, sig.close, stop, self.risk_params)
            )
            if qty <= 0:
                log.info("Signal %s skipped: qty rounds to 0 (equity too small for min lot).", sig.action)
                return
            resp = self.client.market_entry_with_stop(order_side, qty, stop)
            self.db.log_order(order_id=resp["result"].get("orderId"), symbol=self.cfg.exchange.symbol,
                              side=order_side, order_type="Market", qty=qty,
                              price=sig.close, stop_loss=stop, status="submitted", raw=str(resp))
            self.db.open_trade(self.cfg.exchange.symbol, order_side, qty, sig.close, stop,
                               self.cfg.strategy.name)
            log.info("ENTER %s qty=%.3f @~%.2f stop=%.2f (%s)", order_side, qty, sig.close, stop, sig.reason)
