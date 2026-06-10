"""Config loading + validation. Single source: config/config.yaml, secrets from .env."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent


class ExchangeCfg(BaseModel):
    testnet: bool = True
    symbol: str = "ETHUSDT"
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


class TimeframesCfg(BaseModel):
    execution: str = "5"
    filter: str = "15"


class RiskCfg(BaseModel):
    max_position_pct: float = Field(1.5, gt=0, le=5)
    daily_loss_halt_pct: float = Field(4.0, gt=0, le=20)
    max_drawdown_pct: float = Field(12.0, gt=0, le=50)
    stop_loss_atr_mult: float = Field(2.0, gt=0)


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
