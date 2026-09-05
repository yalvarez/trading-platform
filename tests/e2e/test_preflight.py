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


@pytest.mark.asyncio
async def test_preflight_ok_when_channel_allowed_and_health_up():
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1009999999999]}]
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=MagicMock(status_code=200))

    result = await run_preflight(_cfg(), accounts, http_client)

    assert result.ok is True
    assert result.problems == []


@pytest.mark.asyncio
async def test_preflight_fails_when_test_chat_id_not_in_allowed_channels():
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1002293184715]}]
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=MagicMock(status_code=200))

    result = await run_preflight(_cfg(), accounts, http_client)

    assert result.ok is False
    assert any("allowed_channels" in p for p in result.problems)


@pytest.mark.asyncio
async def test_preflight_fails_when_trade_orchestrator_unreachable():
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1009999999999]}]
    http_client = MagicMock()
    http_client.get = AsyncMock(side_effect=ConnectionError("refused"))

    result = await run_preflight(_cfg(), accounts, http_client)

    assert result.ok is False
    assert any("trade_orchestrator" in p for p in result.problems)
