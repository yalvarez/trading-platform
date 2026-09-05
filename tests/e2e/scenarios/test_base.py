import pytest
from unittest.mock import AsyncMock, MagicMock, patch
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


@pytest.mark.asyncio
async def test_cleanup_group_uses_rpyc_default_path():
    observer = MagicMock()
    observer.mt5_host = "localhost"
    observer.mt5_port = 18812
    observer.positions_for_symbol = AsyncMock(return_value=[{"ticket": 222, "sl": 0, "tp": 0, "volume": 0.05}])

    ctx = ScenarioContext(cfg=MagicMock(), price_reader=MagicMock(), sender=MagicMock(), observer=observer)

    mock_client = MagicMock()
    mock_client.root.partial_close = MagicMock()

    with patch("rpyc.connect", return_value=mock_client) as mock_connect:
        await cleanup_group(ctx, "XAUUSD")

        mock_connect.assert_called_once_with("localhost", 18812)
        mock_client.root.partial_close.assert_called_once()
        call_args = mock_client.root.partial_close.call_args
        assert call_args[0][1] == 222  # ticket
        assert call_args[0][2] == 100  # percent (100%)
        assert call_args[0][0]["host"] == "localhost"
        assert call_args[0][0]["port"] == 18812
        mock_client.close.assert_called_once()
