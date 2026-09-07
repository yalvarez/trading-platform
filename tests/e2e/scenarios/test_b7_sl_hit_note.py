import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import b7_sl_hit_note
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
    same_positions = [
        {"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
        {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
    ]
    # First call is the preexisting_tickets snapshot (nothing open yet); every
    # call after that returns the same two legs this scenario opened.
    observer.positions_for_symbol = AsyncMock(side_effect=[[]] + [same_positions] * 20)
    # note_sl_hit legitimately logs a [TM][EVENT] line — it just must not be
    # one of the mutating actions (close/BE/update).
    observer.grep_container_logs = MagicMock(return_value=["[TM][EVENT] note_sl_hit {'group_id': 1}"])
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_b7_sends_sl_hit_message_and_confirms_no_sl_tp_change():
    ctx = _ctx_with_open_position()

    result = await b7_sl_hit_note.run(ctx)

    sent_texts = [c.args[1] for c in ctx.sender.send.await_args_list]
    assert any("HIT SL" in t for t in sent_texts)
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_b7_fails_when_sl_hit_message_mutates_the_position():
    ctx = _ctx_with_open_position()
    ctx.observer.grep_container_logs = MagicMock(
        return_value=["[TM][EVENT] mgmt_close_now {'group_id': 1}"]  # false positive: it closed instead of noting
    )

    result = await b7_sl_hit_note.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_b7_fails_when_setup_does_not_open_two_legs():
    ctx = _ctx_with_open_position()
    ctx.observer.positions_for_symbol = AsyncMock(return_value=[])  # fast signal never opens

    result = await b7_sl_hit_note.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
