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
`risk.equity_cap: 100` -> virtual equity = cap + SUM(closed trade pnl in the DB)
+ unrealized pnl of the open position. Anchored to the bot's own trade ledger:
wallet drift, demo top-ups, restarts, deploys, and resumes cannot move it.
- "Add funds" on the ladder: raise the cap (100 -> 1000); earned pnl carries over.
- Reset the simulation: archive data/bot.db (the ledger IS the sim).
- Unmodeled: funding fees on held positions (small at these holding times).
Set `equity_cap: null` before mainnet — live trading sizes on real coin equity.

## Funding ladder reality check
Minimum order size is exchange-enforced (e.g. 0.01 ETH on ETHUSDT). At ~$2-3k ETH that
is $20-30 notional. With leverage capped at 1-2x, a $10 account CANNOT place the minimum
order — the bot will correctly size to 0 and never trade. First viable live step is ~$100.
Ladder: $100 -> $1k -> $5k -> $10k, advancing only after stable growth at each step.

## Test environments, ranked for this user
1. DEMO TRADING (recommended): bybit.com production account (the Ukrainian one) -> switch
   the web UI to Demo Trading mode, create an API key THERE, set config
   `testnet: false`, `demo: true`, `tld: "com"`, `symbol: ETHUSDT`.
   Real instruments/fees/lots, demo funds — closest to live. Live Gate soak runs here.
2. EU testnet (testnet.bybit.eu): USDC sandbox; ETHPERP is listed but NOT live
   (ErrCode 110074). Run `python -m bot.main instruments` to see what is tradable;
   only useful if a live ETH contract exists there.
Demo/testnet differences vs live (funding, liquidity, fills) still apply — the Live Gate
72h soak validates plumbing, not strategy performance.

## Phase 2 operations
### Dashboard
- VPS: `sudo cp scripts/tradingbot-dashboard.service /etc/systemd/system/ && sudo systemctl enable --now tradingbot-dashboard`
- It binds to 127.0.0.1 ONLY. Access from the Mac: `ssh -L 8080:127.0.0.1:8080 user@vps`, open http://localhost:8080
- If you must expose it, set DASHBOARD_TOKEN in .env and put it behind HTTPS — an
  unauthenticated kill switch on the internet is a capital-preservation failure.
- Config editor validates with the same rules as startup and requires a service
  restart to apply: `sudo systemctl restart tradingbot`.

### Telegram alerts
1. In Telegram, talk to @BotFather -> /newbot -> copy the token to TELEGRAM_BOT_TOKEN
2. Send any message to your new bot
3. Open https://api.telegram.org/bot<TOKEN>/getUpdates -> "chat":{"id": ...} -> TELEGRAM_CHAT_ID
4. Restart services. Alerts cover: entries, exits, breakers/halts, regime changes, cycle errors.
   Alert failures never block trading (best-effort, logged to the errors table).

### Regime behavior
The bot now trades momentum only in TRENDING, mean reversion only in RANGING, and
stands aside in CHAOS (no new entries; an existing position keeps being managed by
the strategy that opened it). Standing aside is expected behavior, not a bug —
don't "fix" quiet periods by loosening thresholds without Phase 3 evidence.

## Phase 3: supervised improvement loop (Section F)
1. Fetch history (production market data, public, no key): `python -m backtest.data --days 60`
2. Run the optimizer offline: `python -m backtest.optimizer --days 60`
   - Walk-forward: 21d train / 7d test rolling windows, bounded 48-combo grid
   - Score = net return / (1 + max drawdown) — steady growth over fast growth
   - All numbers are NET of taker fees + slippage; funding/latency are unmodeled
   - A proposal is written ONLY if the candidate beats current params out-of-sample
     by a margin AND wins most windows. "No proposal" is a valid, honest result.
3. Review on the dashboard (Optimizer proposals panel): evidence shows OOS vs
   current, stability, caveats. Approve -> config.yaml updated (validated),
   restart the bot, commit with the suggested git message. Reject -> archived.
4. Re-run on demand (weekly is plenty at 5m frequency). NEVER automate approval.

Runtime note: a 60-day run over the full grid takes minutes (per-bar simulation
reusing the exact live strategy code — fidelity over speed, deliberately).

## Live Gate checker
`python -m bot.main gate` auto-verifies what it can (72h continuity from equity
snapshots, breaker/kill events present, telegram configured, demo/equity_cap
state) and lists the manual confirmations. Mainnet help stays refused until all pass.
