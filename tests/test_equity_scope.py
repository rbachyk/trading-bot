"""Regression: equity must be settlement-coin-scoped, never wallet totalEquity.
(Dashboard 2026-06-12: virtual equity 485 on a 100-cap account — demo coin
basket drift leaked into sizing and breakers, and caused the earlier false halt.)"""
import pytest

from bot.exchange.bybit_client import parse_coin_equity


def resp(coins, total="48503.12"):
    return {"result": {"list": [{"totalEquity": total, "coin": coins}]}}


def test_uses_coin_equity_not_total():
    r = resp([{"coin": "USDT", "equity": "99.67", "walletBalance": "99.67", "unrealisedPnl": "0"},
              {"coin": "BTC", "equity": "48403.45"}])
    assert parse_coin_equity(r, "USDT") == pytest.approx(99.67)  # NOT 48503.12


def test_fallback_wallet_plus_unrealised():
    r = resp([{"coin": "USDT", "equity": "", "walletBalance": "100.0", "unrealisedPnl": "-0.5"}])
    assert parse_coin_equity(r, "USDT") == pytest.approx(99.5)


def test_usdc_scoping_for_eu_testnet():
    r = resp([{"coin": "USDC", "equity": "1000.0"}, {"coin": "USDT", "equity": "5.0"}])
    assert parse_coin_equity(r, "USDC") == 1000.0


def test_missing_coin_is_loud():
    with pytest.raises(RuntimeError):
        parse_coin_equity(resp([{"coin": "BTC", "equity": "1"}]), "USDT")
