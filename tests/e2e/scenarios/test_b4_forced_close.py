import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import b4_forced_close
from tests.e2e.scenarios import a1_fast_only


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(a1_fast_only.asyncio, "sleep", AsyncMock(return_value=None))


def _ctx_with_open_position():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # after fast open
            [],  # both legs closed by close_now
            [],  # cleanup_group's own read: nothing left to close
        ]
    )
    observer.grep_container_logs = MagicMock(return_value=["[TM][EVENT] mgmt_close_now {'group_id': 1}"])
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_b4_sends_forced_close_message_and_confirms_both_legs_closed():
    ctx = _ctx_with_open_position()

    result = await b4_forced_close.run(ctx)

    sent_texts = [c.args[1] for c in ctx.sender.send.await_args_list]
    assert "MARKET STRUCTURE SHIFTED! DON'T HOLD SELL. Close now" in sent_texts
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_b4_reports_external_dependency_failure_on_timeout_without_error():
    ctx = _ctx_with_open_position()
    setup_positions = [
        {"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
        {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
    ]

    async def _positions_for_symbol(_symbol):
        return setup_positions  # positions never close

    ctx.observer.positions_for_symbol = _positions_for_symbol
    ctx.observer.grep_container_logs = MagicMock(return_value=[])  # no mgmt event logged at all

    result = await b4_forced_close.run(ctx)

    assert result.outcome == ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE


@pytest.mark.asyncio
async def test_b4_fails_when_setup_does_not_open_two_legs():
    ctx = _ctx_with_open_position()
    ctx.observer.positions_for_symbol = AsyncMock(return_value=[])  # fast signal never opens

    result = await b4_forced_close.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
