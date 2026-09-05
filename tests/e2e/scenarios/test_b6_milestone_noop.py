import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import b6_milestone_noop
from tests.e2e.scenarios import a1_fast_only


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # b6 sleeps a QUIET_WINDOW_SECONDS directly (its own `asyncio` import)
    # in addition to reusing a1_fast_only._poll_until for setup — patch
    # asyncio.sleep globally via either module's reference since asyncio is
    # a singleton module.
    monkeypatch.setattr(a1_fast_only.asyncio, "sleep", AsyncMock(return_value=None))


def _ctx_with_open_position():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    same_positions = [
        {"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
        {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
    ]
    observer.positions_for_symbol = AsyncMock(return_value=same_positions)
    observer.grep_container_logs = MagicMock(return_value=[])  # no mutating events at all
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_b6_sends_all_milestone_messages_and_confirms_no_mutation():
    ctx = _ctx_with_open_position()

    result = await b6_milestone_noop.run(ctx)

    sent_texts = [c.args[1] for c in ctx.sender.send.await_args_list]
    assert "+240 PIPS SKYROCKETING" in sent_texts
    assert "TP 1 DONE" in sent_texts
    assert "Road to TP ONE" in sent_texts
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_b6_fails_when_a_milestone_message_triggers_mutating_action():
    ctx = _ctx_with_open_position()
    ctx.observer.grep_container_logs = MagicMock(
        return_value=["[TM][EVENT] mgmt_move_sl_be_applied {'group_id': 1}"]  # false positive
    )

    result = await b6_milestone_noop.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_b6_fails_when_setup_does_not_open_two_legs():
    ctx = _ctx_with_open_position()
    ctx.observer.positions_for_symbol = AsyncMock(return_value=[])  # fast signal never opens

    result = await b6_milestone_noop.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
