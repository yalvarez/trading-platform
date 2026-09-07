import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import b8_spam_noop


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # b8 has no open-position setup step (it never calls _poll_until), but
    # it does sleep QUIET_WINDOW_SECONDS directly via its own `asyncio`
    # import — patch that.
    monkeypatch.setattr(b8_spam_noop.asyncio, "sleep", AsyncMock(return_value=None))


def _ctx():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(return_value=[])  # no trade opened
    observer.grep_container_logs = MagicMock(return_value=[])  # no mgmt/open events
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_b8_sends_spam_message_and_confirms_no_effects():
    ctx = _ctx()

    result = await b8_spam_noop.run(ctx)

    ctx.sender.send.assert_awaited_once()
    sent_text = ctx.sender.send.await_args.args[1]
    assert "VIP POOL" in sent_text
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_b8_fails_when_spam_opens_a_position():
    ctx = _ctx()
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # before: nothing open (may include unrelated production positions in reality)
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # after: a NEW position appeared
        ]
    )

    result = await b8_spam_noop.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_b8_passes_when_only_a_preexisting_unrelated_position_is_present():
    # This demo account may already carry real production positions in
    # XAUUSD unrelated to this scenario — the same position present both
    # before and after must NOT be treated as a false positive.
    ctx = _ctx()
    preexisting = [{"ticket": 999, "sl": 2400.0, "tp": 0.0, "volume": 0.04}]
    ctx.observer.positions_for_symbol = AsyncMock(side_effect=[preexisting, preexisting])

    result = await b8_spam_noop.run(ctx)

    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_b8_fails_when_spam_triggers_a_mgmt_action_log():
    ctx = _ctx()
    ctx.observer.grep_container_logs = MagicMock(
        return_value=["[TM][EVENT] group_opened {'group_id': 1}"]  # false positive
    )

    result = await b8_spam_noop.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_b8_scopes_log_grep_since_to_scenario_start_not_hardcoded_5m():
    ctx = _ctx()

    await b8_spam_noop.run(ctx)

    ctx.observer.grep_container_logs.assert_called_once()
    _args, kwargs = ctx.observer.grep_container_logs.call_args
    assert "since" in kwargs
    assert kwargs["since"] != "5m"
    # Must be a real RFC3339 timestamp parseable back to a datetime.
    from datetime import datetime
    datetime.fromisoformat(kwargs["since"])
