"""Config loading + validation. Single source: config/config.yaml, secrets from .env."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent


class ExchangeCfg(BaseModel):
    testnet: bool = True
    demo: bool = False        # Bybit Demo Trading: api-demo.bybit.com, key from the
                              # PRODUCTION bybit.com account while in Demo mode.
                              # Requires testnet: false. Demo funds, real instruments.
    tld: str = "com"          # "eu" for testnet.bybit.eu accounts; "com" for bybit.com
    symbol: str = "ETHUSDT"
    settle_coin: str = "USDT"  # equity is scoped to THIS coin only (USDC for EU testnet
                               # ETHPERP). totalEquity of the whole wallet includes the
                               # demo coin basket and drifts with market prices.
    category: str = "linear"
    leverage: float = Field(1, ge=1)

    @field_validator("leverage")
    @classmethod
    def leverage_cap(cls, v: float) -> float:
        # Priority 1: capital preservation. >2x requires deliberate edit of this guard,
        # not just config — per project Section C.
        if v > 2:
            raise ValueError("Leverage above 2x is blocked by the risk policy (Section C).")
        return v

    @model_validator(mode="after")
    def eu_mainnet_blocked(self) -> "ExchangeCfg":
        # api.bybit.eu mainnet only supports the API-broker "third-party app" flow —
        # regular accounts cannot bot-trade EU mainnet. Live trading must use the
        # bybit.com (e.g. Ukrainian) account: tld "com" + ETHUSDT.
        if self.demo and self.testnet:
            raise ValueError("Demo trading uses testnet: false AND demo: true (pybit demo mode).")
        if self.demo and self.tld != "com":
            raise ValueError("Demo trading is on bybit.com: set tld: \"com\".")
        if not self.testnet and not self.demo and self.tld == "eu":
            raise ValueError(
                "Mainnet via bybit.eu API is not available for regular users. "
                "Use the bybit.com account: tld: \"com\", symbol: ETHUSDT."
            )
        return self


class TimeframesCfg(BaseModel):
    execution: str = "5"
    filter: str = "15"


class RiskCfg(BaseModel):
    max_position_pct: float = Field(1.5, gt=0, le=5)
    daily_loss_halt_pct: float = Field(4.0, gt=0, le=20)
    max_drawdown_pct: float = Field(12.0, gt=0, le=50)
    stop_loss_atr_mult: float = Field(2.0, gt=0)
    # Optional small-account simulation: virtual starting equity (e.g. 100) so a
    # rich testnet wallet behaves like the real account you plan to fund. None = off.
    equity_cap: float | None = Field(None, gt=0)


class StrategyCfg(BaseModel):
    name: str = "momentum"
    ema_fast: int = Field(20, ge=2)
    ema_slow: int = Field(50, ge=3)
    donchian_period: int = Field(20, ge=5)
    atr_period: int = Field(14, ge=2)
    trail_atr_mult: float = Field(2.5, gt=0)


class CostsCfg(BaseModel):
    taker_fee_pct: float = 0.055
    slippage_pct: float = 0.02


class LoopCfg(BaseModel):
    poll_seconds: int = Field(15, ge=5)


class DbCfg(BaseModel):
    path: str = "data/bot.db"


class AppConfig(BaseModel):
    exchange: ExchangeCfg
    timeframes: TimeframesCfg
    risk: RiskCfg
    strategy: StrategyCfg
    costs: CostsCfg
    loop: LoopCfg
    db: DbCfg

    api_key: str = ""
    api_secret: str = ""


def load_config(path: str | Path | None = None) -> AppConfig:
    path = Path(path) if path else ROOT / "config" / "config.yaml"
    with open(path) as f:
        raw = yaml.safe_load(f)
    cfg = AppConfig(**raw)

    load_dotenv(ROOT / ".env")
    cfg.api_key = os.getenv("BYBIT_API_KEY", "")
    cfg.api_secret = os.getenv("BYBIT_API_SECRET", "")
    if not cfg.api_key or not cfg.api_secret:
        raise RuntimeError("Missing BYBIT_API_KEY / BYBIT_API_SECRET in .env (see .env.example).")
    return cfg
