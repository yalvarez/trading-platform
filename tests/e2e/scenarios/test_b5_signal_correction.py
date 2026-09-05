import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import b5_signal_correction
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
        return_value=[
            {"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
            {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
        ]
    )
    observer.grep_container_logs = MagicMock(return_value=["[TM][EVENT] group_updated {'group_id': 1, 'tp2': 4687.0}"])
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_b5_sends_both_correction_messages_and_confirms_group_updated_logged():
    ctx = _ctx_with_open_position()

    result = await b5_signal_correction.run(ctx)

    sent_texts = [c.args[1] for c in ctx.sender.send.await_args_list]
    assert "SIGNAL UPDATED" in sent_texts
    assert "TP 2 IS 4687 Correction" in sent_texts
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_b5_reports_external_dependency_failure_when_no_group_updated_logged():
    ctx = _ctx_with_open_position()
    ctx.observer.grep_container_logs = MagicMock(return_value=[])  # no correction event logged at all

    result = await b5_signal_correction.run(ctx)

    assert result.outcome == ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE


@pytest.mark.asyncio
async def test_b5_fails_when_setup_does_not_open_two_legs():
    ctx = _ctx_with_open_position()
    ctx.observer.positions_for_symbol = AsyncMock(return_value=[])  # fast signal never opens

    result = await b5_signal_correction.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
