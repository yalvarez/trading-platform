import pytest
from unittest.mock import MagicMock, patch

from services.trade_orchestrator.mt5_pool import PooledMT5Client


@pytest.fixture
def pooled_client():
    with patch("services.common.mt5_client.MT5Client") as MockMT5Client:
        instance = MockMT5Client.return_value
        client = PooledMT5Client("mt5_acct1", 8001)
        yield client, instance


def test_history_deals_get_passes_through_to_underlying_client(pooled_client):
    """
    Real production bug found live (2026-09-09): PooledMT5Client -- the
    real client TradeManager uses in production, not the plain MT5Client
    tests exercise directly -- had no history_deals_get passthrough at
    all. Every TradeManager._get_close_price call against a real account
    silently failed with AttributeError (caught by _get_close_price's own
    try/except), degrading every close-price message to "N/D" and making
    it impossible to verify whether a tp1_leg closure actually reached
    tp1_price.
    """
    client, instance = pooled_client
    instance.history_deals_get.return_value = ["deal1", "deal2"]

    result = client.history_deals_get(position=12345)

    assert result == ["deal1", "deal2"]
    instance.history_deals_get.assert_called_once_with(position=12345)
