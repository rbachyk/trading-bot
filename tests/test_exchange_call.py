"""Exchange wrapper error-handling tests (mocked pybit, no network).

Regression for the 110043 bug: pybit raises InvalidRequestError for non-zero
retCodes; the wrapper must convert to RuntimeError so set_leverage can swallow
the benign 'leverage not modified' case, and must retry rate limits.
"""
import pytest
from pybit import exceptions as bybit_exc

from bot.exchange.bybit_client import BybitClient


def make_client():
    c = object.__new__(BybitClient)  # skip __init__ (no network)
    c.symbol, c.category = "ETHUSDT", "linear"
    c.errors = []
    c._on_error = lambda ctx, msg: c.errors.append((ctx, msg))
    return c


def invalid(msg, code):
    return bybit_exc.InvalidRequestError(
        request="POST /test", message=msg, status_code=code, time="t", resp_headers=None)


def test_api_error_becomes_runtime_error_with_code():
    c = make_client()
    def fn(**kw):
        raise invalid("leverage not modified", 110043)
    with pytest.raises(RuntimeError) as ei:
        c._call(fn, "set_leverage")
    assert "110043" in str(ei.value) or "not modified" in str(ei.value)
    assert c.errors  # logged


def test_set_leverage_swallows_110043():
    c = make_client()
    def fn(**kw):
        raise invalid("leverage not modified", 110043)
    c.http = type("H", (), {"set_leverage": staticmethod(fn)})()
    c.set_leverage(1.0)  # must NOT raise


def test_set_leverage_propagates_real_errors():
    c = make_client()
    def fn(**kw):
        raise invalid("position exists", 110044)
    c.http = type("H", (), {"set_leverage": staticmethod(fn)})()
    with pytest.raises(RuntimeError):
        c.set_leverage(2.0)


def test_rate_limit_is_retried_then_succeeds(monkeypatch):
    import bot.exchange.bybit_client as mod
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    c = make_client()
    calls = {"n": 0}
    def fn(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise invalid("Too many visits. Exceeded the API Rate Limit.", 10006)
        return {"retCode": 0, "result": {}}
    assert c._call(fn, "x")["retCode"] == 0
    assert calls["n"] == 3


def test_transport_error_retried_then_exhausted(monkeypatch):
    import bot.exchange.bybit_client as mod
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    c = make_client()
    def fn(**kw):
        raise bybit_exc.FailedRequestError(
            request="GET /x", message="conn reset", status_code=503, time="t", resp_headers=None)
    with pytest.raises(RuntimeError) as ei:
        c._call(fn, "x", retries=2)
    assert "exhausted" in str(ei.value)
