import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import a1_fast_only


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # The scenario's poll loops call asyncio.sleep between attempts using
    # production timeout/interval constants (e.g. 600s TP1 poll). Unit tests
    # must not actually wait on wall-clock time, so replace sleep with a
    # no-op for every test in this module.
    monkeypatch.setattr(a1_fast_only.asyncio, "sleep", AsyncMock(return_value=None))


def _ctx(price=2500.0, parsed_signals=None, positions=None):
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=price)

    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)

    observer = MagicMock()
    observer.read_parsed_signals = AsyncMock(return_value=parsed_signals or [])
    observer.positions_for_symbol = AsyncMock(return_value=positions or [])
    observer.grep_container_logs = MagicMock(return_value=[])

    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_a1_sends_fast_signal_text():
    ctx = _ctx()
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # before send: nothing open
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # after: two legs opened
            [{"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # TP1 leg closed, runner remains
        ]
    )

    result = await a1_fast_only.run(ctx)

    ctx.sender.send.assert_awaited_once_with(-1009999999999, "XAUUSD BUY NOW")
    assert result.outcome in (ScenarioOutcome.PASS, ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED)


@pytest.mark.asyncio
async def test_a1_fails_when_no_positions_open_after_signal():
    ctx = _ctx()
    ctx.observer.positions_for_symbol = AsyncMock(return_value=[])  # never opens

    result = await a1_fast_only.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
