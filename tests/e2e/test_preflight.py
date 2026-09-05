import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.config import E2EConfig
from tests.e2e.preflight import run_preflight


def _cfg(chat_id=-1009999999999):
    return E2EConfig(
        redis_url="redis://redis:6379/0", tg_test_chat_id=chat_id,
        tg_api_id="1", tg_api_hash="h", tg_phone="+1",
        mt5_host="mt5_acct1", mt5_port=8001,
        n8n_action_api_key="key", mgmt_api_port=8200,
        trade_orchestrator_host="trade_orchestrator",
    )


def _mock_demo_mt5_client(monkeypatch, trade_mode=0):
    fake_client = MagicMock()
    fake_client.account_info.return_value = MagicMock(trade_mode=trade_mode)
    monkeypatch.setattr(
        "tests.e2e.preflight.build_mt5_client",
        lambda host, port: fake_client,
    )
    return fake_client


@pytest.mark.asyncio
async def test_preflight_ok_when_channel_allowed_and_health_up(monkeypatch):
    _mock_demo_mt5_client(monkeypatch)
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1009999999999]}]
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=MagicMock(status_code=200))

    result = await run_preflight(_cfg(), accounts, http_client)

    assert result.ok is True
    assert result.problems == []


@pytest.mark.asyncio
async def test_preflight_fails_when_test_chat_id_not_in_allowed_channels(monkeypatch):
    _mock_demo_mt5_client(monkeypatch)
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1002293184715]}]
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=MagicMock(status_code=200))

    result = await run_preflight(_cfg(), accounts, http_client)

    assert result.ok is False
    assert any("allowed_channels" in p for p in result.problems)


@pytest.mark.asyncio
async def test_preflight_fails_when_trade_orchestrator_unreachable(monkeypatch):
    _mock_demo_mt5_client(monkeypatch)
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1009999999999]}]
    http_client = MagicMock()
    http_client.get = AsyncMock(side_effect=ConnectionError("refused"))

    result = await run_preflight(_cfg(), accounts, http_client)

    assert result.ok is False
    assert any("trade_orchestrator" in p for p in result.problems)


@pytest.mark.asyncio
async def test_preflight_ok_when_mt5_account_is_demo(monkeypatch):
    fake_client = _mock_demo_mt5_client(monkeypatch, trade_mode=0)
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1009999999999]}]
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=MagicMock(status_code=200))

    result = await run_preflight(_cfg(), accounts, http_client)

    assert result.ok is True
    assert result.problems == []
    fake_client.account_info.assert_called_once()


@pytest.mark.asyncio
async def test_preflight_fails_when_mt5_account_is_not_demo(monkeypatch):
    # ACCOUNT_TRADE_MODE_REAL = 2 (mt5linux.Constants) — a live account.
    _mock_demo_mt5_client(monkeypatch, trade_mode=2)
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1009999999999]}]
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=MagicMock(status_code=200))

    result = await run_preflight(_cfg(), accounts, http_client)

    assert result.ok is False
    assert any("DEMO" in p or "demo" in p for p in result.problems)


@pytest.mark.asyncio
async def test_preflight_fails_gracefully_when_mt5_connection_fails(monkeypatch):
    def raise_connection_error(host, port):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("tests.e2e.preflight.build_mt5_client", raise_connection_error)
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1009999999999]}]
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=MagicMock(status_code=200))

    result = await run_preflight(_cfg(), accounts, http_client)

    assert result.ok is False
    assert any("MT5" in p or "mt5" in p for p in result.problems)
