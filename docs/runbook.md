# Runbook

## Mac (dev) vs VPS (production)
- Mac: development & testing only. macOS sleep/app-nap will stall the loop; no static IP means no API-key IP whitelist. Use `caffeinate -i python -m bot.main run` for short test sessions only.
- VPS: production target. Static IP -> whitelist the Bybit API key to it. systemd auto-restarts the bot.

## VPS deploy
```bash
sudo mkdir -p /opt/trading-bot && sudo chown $USER /opt/trading-bot
git clone <your-remote> /opt/trading-bot && cd /opt/trading-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env          # keys; chmod 600 .env
sudo cp scripts/tradingbot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tradingbot
journalctl -u tradingbot -f                # live logs
```

## Recovery behavior
- Process restart: engine reconciles with the exchange on startup. Exchange position with no DB record -> adopted and a stop is attached if missing. DB trade with no exchange position -> marked closed.
- Network loss / rate limit: exponential-backoff retries; errors logged to SQLite `errors` table.
- Any breaker trip: position flattened, halt flag set. Resuming requires `python -m bot.main resume` — deliberately manual.

## Forced-failure verification (do before trusting the bot)
1. Open a testnet position, then `python -m bot.main kill` -> confirm flat + halted.
2. Open a position, `kill -9` the bot process, restart -> confirm reconciliation log lines and intact exchange-side stop.
3. Temporarily set `daily_loss_halt_pct: 0.1`, take a losing trade -> confirm flatten + halt. Restore config.
4. Check the stop exists ON BYBIT (web UI, position panel) — not only in logs.

## Live Gate (Section H) — ALL must pass before mainnet
1. >=72h continuous testnet run on the VPS, no unhandled crash
2. Stop-loss, circuit breaker, kill switch (CLI; dashboard in Phase 2) each verified by deliberate test
3. Restart-reconciliation verified with an open position
4. Alert delivery verified end-to-end (Phase 2)
5. You confirm capital is fully loseable and re-confirm sizing/leverage/drawdown numbers
