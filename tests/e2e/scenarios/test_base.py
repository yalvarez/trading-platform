import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group


@pytest.mark.asyncio
async def test_cleanup_group_closes_open_positions():
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(return_value=[{"ticket": 111, "sl": 0, "tp": 0, "volume": 0.01}])

    fake_mt5_client = MagicMock()
    ctx = ScenarioContext(cfg=MagicMock(), price_reader=MagicMock(), sender=MagicMock(), observer=observer)

    closed_tickets = []

    async def fake_close(ticket, volume):
        closed_tickets.append(ticket)

    await cleanup_group(ctx, "XAUUSD", close_fn=fake_close)

    assert closed_tickets == [111]


def test_scenario_result_holds_outcome_and_evidence():
    result = ScenarioResult(
        name="a1_fast_only",
        outcome=ScenarioOutcome.PASS,
        evidence={"raw_messages": [], "positions": []},
        detail="opened and closed cleanly",
    )
    assert result.outcome == ScenarioOutcome.PASS
    assert result.name == "a1_fast_only"
