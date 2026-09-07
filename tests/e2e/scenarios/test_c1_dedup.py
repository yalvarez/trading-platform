import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import c1_dedup
from tests.e2e.scenarios import a1_fast_only


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # c1's poll loop reuses a1_fast_only._poll_until, and c1 itself sleeps
    # BETWEEN_SENDS_SECONDS/SETTLE_SECONDS directly via its own `asyncio`
    # import. asyncio is a singleton module, so patching the attribute via
    # a1_fast_only's reference to it also affects c1_dedup's own
    # `asyncio.sleep` calls.
    monkeypatch.setattr(a1_fast_only.asyncio, "sleep", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_c1_second_identical_signal_does_not_open_a_second_group():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot: nothing open before the scenario starts
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # after 1st send
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # after 2nd send: unchanged
        ]
    )
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    ctx = ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)

    result = await c1_dedup.run(ctx)

    assert ctx.sender.send.await_count == 2
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_c1_fails_when_second_signal_opens_a_second_group():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 3, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 4, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # duplicate opened a second group!
        ]
    )
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    ctx = ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)

    result = await c1_dedup.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_c1_fails_when_first_fast_signal_never_opens_two_legs():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(return_value=[])  # never opens
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    ctx = ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)

    result = await c1_dedup.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
    assert ctx.sender.send.await_count == 1
