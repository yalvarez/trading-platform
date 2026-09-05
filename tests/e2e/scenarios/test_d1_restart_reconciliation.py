import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import d1_restart_reconciliation


def _ctx_with_be_applied_position():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.restart_container = AsyncMock()
    observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # after fast open
            [{"ticket": 2, "sl": 2500.0, "tp": 0.0, "volume": 0.01}],  # after BE applied (tp1 leg closed)
            [{"ticket": 2, "sl": 2500.0, "tp": 0.0, "volume": 0.01}],  # after restart: same SL, same single position
        ]
    )
    observer.grep_container_logs = MagicMock(
        side_effect=[
            ["[TM][EVENT] mgmt_move_sl_be_applied {'group_id': 1}"],  # BE confirmation before restart
            ["[RECONCILE] al arranque: {'recovered_from_redis': 1, 'recovered_from_file': 0, 'degraded': 0, 'orphaned': []}"],  # after restart
        ]
    )
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_d1_position_survives_restart_with_same_sl_and_no_duplicate():
    ctx = _ctx_with_be_applied_position()

    result = await d1_restart_reconciliation.run(ctx)

    ctx.observer.restart_container.assert_awaited_once()
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_d1_fails_when_position_is_orphaned_after_restart():
    ctx = _ctx_with_be_applied_position()
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],
            [{"ticket": 2, "sl": 2500.0, "tp": 0.0, "volume": 0.01}],
            [{"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # SL reverted after restart!
        ]
    )

    result = await d1_restart_reconciliation.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_d1_fails_when_restart_duplicates_the_group():
    ctx = _ctx_with_be_applied_position()
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],
            [{"ticket": 2, "sl": 2500.0, "tp": 0.0, "volume": 0.01}],
            [{"ticket": 2, "sl": 2500.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 3, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # a second, duplicate position appeared
        ]
    )

    result = await d1_restart_reconciliation.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
