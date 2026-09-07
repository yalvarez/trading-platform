import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import c1b_reopen_after_cooldown


@pytest.mark.asyncio
async def test_c1b_signal_past_cooldown_opens_a_second_independent_group(monkeypatch):
    monkeypatch.setenv("REOPEN_COOLDOWN_SECONDS", "300")
    monkeypatch.setattr("tests.e2e.scenarios.c1b_reopen_after_cooldown.asyncio.sleep", AsyncMock())

    price_reader = MagicMock()
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot: nothing open before the scenario starts
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # first group opens
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 3, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 4, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # second group opens past cooldown
        ]
    )
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    ctx = ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)

    result = await c1b_reopen_after_cooldown.run(ctx)

    assert ctx.sender.send.await_count == 2
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_c1b_fails_when_signal_past_cooldown_is_still_discarded(monkeypatch):
    monkeypatch.setenv("REOPEN_COOLDOWN_SECONDS", "300")
    monkeypatch.setattr("tests.e2e.scenarios.c1b_reopen_after_cooldown.asyncio.sleep", AsyncMock())

    price_reader = MagicMock()
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    two_legs = [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
                {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}]
    # First call is the preexisting_tickets snapshot (nothing open yet); every
    # call after that keeps returning the same two legs — never grows to 4 —
    # regression: still being discarded as a duplicate.
    observer.positions_for_symbol = AsyncMock(side_effect=[[]] + [two_legs] * 20)
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    ctx = ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)

    result = await c1b_reopen_after_cooldown.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_c1b_uses_env_cooldown_plus_margin(monkeypatch):
    monkeypatch.setenv("REOPEN_COOLDOWN_SECONDS", "300")
    sleep_mock = AsyncMock()
    monkeypatch.setattr("tests.e2e.scenarios.c1b_reopen_after_cooldown.asyncio.sleep", sleep_mock)

    price_reader = MagicMock()
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
             {"ticket": 4, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],
        ]
    )
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    ctx = ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)

    await c1b_reopen_after_cooldown.run(ctx)

    # the wait-past-cooldown sleep (300 + 20s margin) must be among the calls
    sleep_mock.assert_any_await(320.0)
