import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import c2_unrecognized_to_n8n


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # c2 sleeps SETTLE_SECONDS directly via its own `asyncio` import — patch
    # that so the unit test doesn't actually wait 30s.
    monkeypatch.setattr(c2_unrecognized_to_n8n.asyncio, "sleep", AsyncMock(return_value=None))


def _ctx(raw_messages=None):
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.read_raw_messages = AsyncMock(
        return_value=raw_messages if raw_messages is not None else [{"text": c2_unrecognized_to_n8n.MESSAGE}]
    )
    observer.positions_for_symbol = AsyncMock(return_value=[])  # no trade opened
    observer.grep_container_logs = MagicMock(return_value=[])  # no mgmt/open events
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_c2_sends_unrecognized_text_and_confirms_no_trade_or_mgmt_action():
    ctx = _ctx()

    result = await c2_unrecognized_to_n8n.run(ctx)

    ctx.sender.send.assert_awaited_once_with(-1009999999999, c2_unrecognized_to_n8n.MESSAGE)
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_c2_fails_when_message_never_reaches_raw_messages():
    ctx = _ctx(raw_messages=[])  # ingestor/filter dropped it

    result = await c2_unrecognized_to_n8n.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_c2_fails_when_unrecognized_text_opens_a_position():
    ctx = _ctx()
    ctx.observer.positions_for_symbol = AsyncMock(
        return_value=[{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01}]  # false positive: it opened a trade
    )

    result = await c2_unrecognized_to_n8n.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_c2_fails_when_unrecognized_text_triggers_a_mgmt_action_log():
    ctx = _ctx()
    ctx.observer.grep_container_logs = MagicMock(
        return_value=["[TM][EVENT] group_opened {'group_id': 1}"]  # false positive
    )

    result = await c2_unrecognized_to_n8n.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_c2_scopes_log_grep_since_to_scenario_start_not_hardcoded_5m():
    ctx = _ctx()

    await c2_unrecognized_to_n8n.run(ctx)

    ctx.observer.grep_container_logs.assert_called_once()
    _args, kwargs = ctx.observer.grep_container_logs.call_args
    assert "since" in kwargs
    assert kwargs["since"] != "5m"
    # Must be a real RFC3339 timestamp parseable back to a datetime.
    from datetime import datetime
    datetime.fromisoformat(kwargs["since"])
