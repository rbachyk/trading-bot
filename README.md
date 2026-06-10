# ETH Bybit Trading Bot

Regime-switching ETHUSDT perp bot (Bybit v5, testnet by default). **Phase 1: momentum strategy + full risk stack.**
No profitability is implied or guaranteed; most retail bots lose money. This repo exists to test ideas safely.

## Quick start (Mac, dev)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # paste TESTNET keys from https://testnet.bybit.com
pytest                      # risk + strategy tests must pass
python -m bot.main run      # start (testnet)
python -m bot.main status   # equity / position / halt state
python -m bot.main kill     # KILL SWITCH: flatten everything + halt
python -m bot.main resume   # clear halt after a deliberate review
```

## Production = VPS, not Mac
Mac sleep kills 24/7 operation — develop on Mac, deploy on the VPS:
see `docs/runbook.md` (systemd unit in `scripts/tradingbot.service`).
IP-whitelist the API key to the VPS static IP.

## Risk controls (always on)
1.5%/trade sizing • exchange-side stop on every position • -4% daily circuit breaker •
-12% drawdown kill switch • manual kill switch • leverage hard-capped at 2x in code.

## Mainnet
Blocked until the Live Gate in `docs/runbook.md` passes (72h testnet run, forced-failure
tests of every risk control, restart reconciliation, alerts verified, loseable capital confirmed).
