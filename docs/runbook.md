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

## Account topology (this user's setup)
- Testnet: account on testnet.bybit.eu (EU entity, USDC instruments only) -> config: tld "eu", symbol ETHPERP. pybit then targets api-testnet.bybit.eu.
- Mainnet: Ukrainian account on bybit.com -> when going live: tld "com", symbol ETHUSDT. (bybit.eu mainnet API is broker-only and is hard-blocked in config validation.)
- Note: mainnet quote is USDT, testnet quote is USDC. Fees/tick/lot may differ slightly between ETHPERP and ETHUSDT; re-check minOrderQty before the live switch.

## Small-account simulation (equity_cap)
`risk.equity_cap: 100` makes sizing AND breakers act as if the account holds a virtual
balance = cap + PnL since the cap was set, regardless of the real testnet balance.
This mirrors the planned funding ladder. To "add funds" in simulation, raise the cap
(e.g. 100 -> 1000): the baseline rebases automatically and is logged.
Set `equity_cap: null` before mainnet — live trading sizes on real equity.

## Funding ladder reality check
Minimum order size is exchange-enforced (e.g. 0.01 ETH on ETHUSDT). At ~$2-3k ETH that
is $20-30 notional. With leverage capped at 1-2x, a $10 account CANNOT place the minimum
order — the bot will correctly size to 0 and never trade. First viable live step is ~$100.
Ladder: $100 -> $1k -> $5k -> $10k, advancing only after stable growth at each step.
