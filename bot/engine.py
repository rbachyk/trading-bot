"""Main engine loop (Phase 2: regime-switching + alerts).

Per cycle (priority stack: capital preservation first):
  1. Halt flag (kill switch / breakers / dashboard stop)
  2. Equity snapshot (virtual if equity_cap set) -> daily-loss + drawdown gates
  3. Regime classification on 15m (TRENDING/RANGING/CHAOS); alert on change
  4. Open position managed by the strategy that OPENED it (trailing only for momentum)
  5. New entries routed by regime; CHAOS = stand aside (a position, not a bug)

Startup reconciles DB state with the exchange (restart safety).
"""
from __future__ import annotations

import datetime as dt
import logging
import time

from alerts.notifier import Notifier
from bot.config import AppConfig
from bot.exchange.bybit_client import BybitClient
from bot.persistence.db import Database
from bot.risk import manager as risk
from bot.strategy import regime as rg
from bot.strategy.meanreversion import MeanReversionStrategy
from bot.strategy.momentum import MomentumStrategy

log = logging.getLogger("engine")


class Engine:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.db = Database(cfg.db.path)
        self.notify = Notifier(on_error=self.db.log_error)
        self.client = BybitClient(
            cfg.api_key, cfg.api_secret, cfg.exchange.testnet,
            cfg.exchange.symbol, cfg.exchange.category,
            tld=cfg.exchange.tld, demo=cfg.exchange.demo,
            settle_coin=cfg.exchange.settle_coin,
            on_error=self.db.log_error,
        )
        s = cfg.strategy
        self.momentum = MomentumStrategy(s.ema_fast, s.ema_slow, s.donchian_period,
                                         s.atr_period, s.trail_atr_mult)
        self.meanrev = MeanReversionStrategy(atr_period=s.atr_period)
        self.detector = rg.RegimeDetector()
        self.risk_params = risk.RiskParams(
            cfg.risk.max_position_pct, cfg.risk.daily_loss_halt_pct,
            cfg.risk.max_drawdown_pct, cfg.risk.stop_loss_atr_mult,
            cfg.exchange.leverage,
        )
        self.running = False

    # ---- helpers -----------------------------------------------------------
    def _env_signature(self) -> str:
        e = self.cfg.exchange
        return (f"{e.testnet}|{e.demo}|{e.tld}|{e.symbol}|{e.settle_coin}"
                f"|cap={self.cfg.risk.equity_cap}|eqv=coin")

    def _ensure_epoch(self) -> float:
        """Equity scales are only comparable within one environment+cap combo.
        On any change, start a new epoch: breakers and the cap baseline reset so
        e.g. an old testnet peak can never trip the drawdown switch in demo."""
        sig = self._env_signature()
        if self.db.get_state("env_sig") != sig:
            self.db.set_state("env_sig", sig)
            self.db.set_state("epoch_start", str(time.time()))
            self.db.set_state("cap_baseline", "")
            log.warning("New equity epoch (environment/cap changed): %s — "
                        "breaker baselines and cap baseline reset.", sig)
        return float(self.db.get_state("epoch_start", "0") or 0)

    def _strategy_by_name(self, name: str):
        return self.meanrev if name == "meanrev" else self.momentum

    def _effective_equity(self, real_equity: float) -> float:
        cap = self.cfg.risk.equity_cap
        if cap is None:
            return real_equity
        baseline_s = self.db.get_state("cap_baseline", "")
        if not baseline_s:  # new epoch -> rebase on first observed real equity
            self.db.set_state("cap_baseline", str(real_equity))
            log.info("equity_cap rebased: cap=%.2f baseline=%.2f", cap, real_equity)
            baseline_s = str(real_equity)
        return risk.virtual_equity(real_equity, float(baseline_s), cap)

    # ---- startup -----------------------------------------------------------
    def reconcile(self) -> None:
        """DB says one thing, exchange says another -> exchange wins."""
        pos = self.client.get_position()
        open_trade = self.db.get_open_trade()
        if pos and not open_trade:
            log.warning("Exchange position with no DB trade — adopting it.")
            self.db.open_trade(self.cfg.exchange.symbol, pos["side"], pos["qty"],
                               pos["entry"], pos["stop"] or 0.0, "reconciled")
            if not pos["stop"]:
                df = self.client.get_klines(self.cfg.timeframes.execution)
                sig = self.momentum.evaluate(df, pos["side"])
                sl = risk.initial_stop(pos["entry"], sig.atr, pos["side"],
                                       self.risk_params.stop_loss_atr_mult)
                self.client.update_stop(sl)
                log.warning("Attached missing exchange-side stop at %.2f", sl)
        elif open_trade and not pos:
            log.warning("DB open trade %s but no exchange position — marking closed.", open_trade["id"])
            df = self.client.get_klines(self.cfg.timeframes.execution)
            self.db.close_trade(open_trade["id"], float(df["close"].iloc[-1]), 0.0, 0.0)

    def start(self) -> None:
        mode = "DEMO" if self.cfg.exchange.demo else ("TESTNET" if self.cfg.exchange.testnet else "MAINNET")
        log.info("Starting engine on %s (%s)", self.cfg.exchange.symbol, mode)
        self.client.set_leverage(self.cfg.exchange.leverage)
        self._ensure_epoch()
        self.reconcile()
        self.running = True
        self.loop()

    # ---- risk gates ---------------------------------------------------------
    def _risk_gates(self, equity: float) -> bool:
        if self.db.is_halted():
            return False

        epoch = float(self.db.get_state("epoch_start", "0") or 0)
        day_start = dt.datetime.now(dt.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        dse = self.db.day_start_equity(max(day_start, epoch))
        if dse and risk.daily_loss_breached(dse, equity, self.risk_params):
            self._emergency_flatten(
                f"DAILY LOSS CIRCUIT BREAKER: -{self.risk_params.daily_loss_halt_pct}% reached")
            return False

        peak = max(self.db.peak_equity(since=epoch), equity)
        if risk.drawdown_breached(peak, equity, self.risk_params):
            self._emergency_flatten(
                f"MAX DRAWDOWN KILL SWITCH: -{self.risk_params.max_drawdown_pct}% from peak")
            return False
        return True

    def _emergency_flatten(self, reason: str) -> None:
        log.error(reason)
        self.db.log_error("risk", reason)
        try:
            self.client.flatten()
        finally:
            self.db.halt(reason)
            self.notify.breaker(reason)

    # ---- regime --------------------------------------------------------------
    def _current_regime(self) -> str:
        df15 = self.client.get_klines(self.cfg.timeframes.filter, limit=200)
        regime = self.detector.classify(df15)
        last = self.db.get_state("regime", "")
        if regime != last:
            self.db.set_state("regime", regime)
            self.db.conn.execute("INSERT INTO regime_states(ts, regime) VALUES(?, ?)",
                                 (time.time(), regime))
            self.db.conn.commit()
            log.info("Regime: %s -> %s", last or "n/a", regime)
            if last:
                self.notify.regime(last, regime)
        return regime

    # ---- main loop -------------------------------------------------------------
    def loop(self) -> None:
        while self.running:
            try:
                self.cycle()
            except Exception as e:  # never let one bad cycle kill the process
                log.exception("cycle error")
                self.db.log_error("cycle", repr(e))
                self.notify.error("cycle", repr(e))
            time.sleep(self.cfg.loop.poll_seconds)

    def _book_external_close(self, open_trade) -> None:
        """Position gone from the exchange (stop hit / kill switch / manual close):
        book the REAL realized PnL. Prefer the exchange's closed-PnL record; fall
        back to an estimate from the entry/stop prices. Never book 0."""
        rec = None
        try:
            rec = self.client.get_last_closed_pnl()
        except RuntimeError:
            pass
        if rec and rec["ts"] >= float(open_trade["ts"]) - 60:
            net, exit_price = rec["pnl"], rec["exit"]
            fees = 0.0  # included in the exchange's closedPnl
        else:
            exit_price = float(open_trade["stop_price"] or open_trade["entry_price"])
            net, fees = risk.estimate_net_pnl(
                open_trade["side"], float(open_trade["qty"]),
                float(open_trade["entry_price"]), exit_price,
                self.cfg.costs.taker_fee_pct)
        self.db.close_trade(open_trade["id"], exit_price, net, fees)
        log.info("Booked external close (trade %s): net %.4f @ %.2f",
                 open_trade["id"], net, exit_price)
        self.notify.exited(open_trade["side"], exit_price, net, "closed on exchange (stop/kill/manual)")

    def cycle(self) -> None:
        equity = self._effective_equity(self.client.get_equity())
        self.db.log_equity(equity)

        # bookkeeping FIRST, even when halted — a kill-switch flatten must be
        # booked immediately, not after a resume
        pos = self.client.get_position()
        open_trade = self.db.get_open_trade()
        if open_trade and not pos:
            self._book_external_close(open_trade)

        if not self._risk_gates(equity):
            return

        regime = self._current_regime()
        df = self.client.get_klines(self.cfg.timeframes.execution, limit=200)

        if pos:
            self._manage_position(pos, df)
            return

        if regime == rg.CHAOS:
            return  # stand aside: no new entries

        strat_name = "momentum" if regime == rg.TRENDING else "meanrev"
        self._try_enter(self._strategy_by_name(strat_name), strat_name, df, equity, regime)

    # ---- position management -----------------------------------------------------
    def _manage_position(self, pos: dict, df) -> None:
        open_trade = self.db.get_open_trade()
        strat_name = open_trade["strategy"] if open_trade else "momentum"
        strat = self._strategy_by_name(strat_name)
        sig = strat.evaluate(df, pos["side"])

        # trailing only for momentum; meanrev keeps its fixed stop (Section E)
        if getattr(strat, "trailing", True) and pos["stop"]:
            new_stop = risk.trailing_stop(pos["side"], pos["stop"], sig.close,
                                          sig.atr, self.momentum.trail_atr_mult)
            if abs(new_stop - pos["stop"]) / pos["stop"] > 0.0005:
                self.client.update_stop(new_stop)
                log.info("Trailing stop -> %.2f", new_stop)

        if sig.action == "exit":
            self.client.close_position_market()
            time.sleep(1.0)  # let the exchange settle the closed-PnL record
            rec = None
            try:
                rec = self.client.get_last_closed_pnl()
            except RuntimeError:
                pass
            if rec and rec["ts"] >= time.time() - 120:
                net, exit_price, fees = rec["pnl"], rec["exit"], 0.0
            else:
                exit_price = sig.close
                net, fees = risk.estimate_net_pnl(pos["side"], pos["qty"], pos["entry"],
                                                  exit_price, self.cfg.costs.taker_fee_pct)
            if open_trade:
                self.db.close_trade(open_trade["id"], exit_price, net, fees)
            log.info("EXIT %s: %s | net %.4f", pos["side"], sig.reason, net)
            self.notify.exited(pos["side"], exit_price, net, sig.reason)

    def _try_enter(self, strat, strat_name: str, df, equity: float, regime: str) -> None:
        sig = strat.evaluate(df, None)
        if sig.action not in ("long", "short"):
            return
        order_side = "Buy" if sig.action == "long" else "Sell"
        stop = risk.initial_stop(sig.close, sig.atr, order_side,
                                 self.risk_params.stop_loss_atr_mult)
        qty = self.client.round_qty(risk.size_position(equity, sig.close, stop, self.risk_params))
        if qty <= 0:
            log.info("Signal %s skipped: qty rounds to 0 (equity too small for min lot).", sig.action)
            return
        resp = self.client.market_entry_with_stop(order_side, qty, stop)
        self.db.log_order(order_id=resp["result"].get("orderId"), symbol=self.cfg.exchange.symbol,
                          side=order_side, order_type="Market", qty=qty, price=sig.close,
                          stop_loss=stop, status="submitted", raw=str(resp))
        self.db.open_trade(self.cfg.exchange.symbol, order_side, qty, sig.close, stop,
                           strat_name, regime)
        log.info("ENTER %s qty=%.3f @~%.2f stop=%.2f [%s/%s] (%s)",
                 order_side, qty, sig.close, stop, strat_name, regime, sig.reason)
        self.notify.fill(order_side, qty, sig.close, stop, f"{strat_name}/{regime}")
