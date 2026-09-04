import pytest
from unittest.mock import MagicMock
from tests.e2e.price_reader import PriceReader


class FakeTick:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask


def _rpyc_client_returning(tick):
    client = MagicMock()
    client.root.symbol_info_tick.return_value = tick
    return client


@pytest.mark.asyncio
async def test_read_price_returns_mid_price(monkeypatch):
    fake_client = _rpyc_client_returning(FakeTick(bid=2499.5, ask=2500.5))
    monkeypatch.setattr(
        "tests.e2e.price_reader.rpyc.connect",
        lambda host, port: fake_client,
    )

    reader = PriceReader(host="mt5_acct1", port=8001)
    price = await reader.read_price("XAUUSD")

    assert price == 2500.0


@pytest.mark.asyncio
async def test_read_price_retries_on_empty_tick_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_tick(symbol):
        calls["n"] += 1
        if calls["n"] < 3:
            return None
        return FakeTick(bid=2499.0, ask=2501.0)

    fake_client = MagicMock()
    fake_client.root.symbol_info_tick.side_effect = fake_tick
    monkeypatch.setattr(
        "tests.e2e.price_reader.rpyc.connect",
        lambda host, port: fake_client,
    )

    reader = PriceReader(host="mt5_acct1", port=8001)
    price = await reader.read_price("XAUUSD")

    assert price == 2500.0
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_read_price_raises_after_exhausting_retries(monkeypatch):
    fake_client = MagicMock()
    fake_client.root.symbol_info_tick.return_value = None
    monkeypatch.setattr(
        "tests.e2e.price_reader.rpyc.connect",
        lambda host, port: fake_client,
    )

    reader = PriceReader(host="mt5_acct1", port=8001)
    with pytest.raises(RuntimeError):
        await reader.read_price("XAUUSD")


@pytest.mark.asyncio
async def test_read_price_closes_connection_on_success(monkeypatch):
    fake_client = _rpyc_client_returning(FakeTick(bid=2499.5, ask=2500.5))
    monkeypatch.setattr(
        "tests.e2e.price_reader.rpyc.connect",
        lambda host, port: fake_client,
    )

    reader = PriceReader(host="mt5_acct1", port=8001)
    price = await reader.read_price("XAUUSD")

    assert price == 2500.0
    fake_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_read_price_closes_connection_on_failure(monkeypatch):
    fake_client = MagicMock()
    fake_client.root.symbol_info_tick.return_value = None
    monkeypatch.setattr(
        "tests.e2e.price_reader.rpyc.connect",
        lambda host, port: fake_client,
    )

    reader = PriceReader(host="mt5_acct1", port=8001)
    with pytest.raises(RuntimeError):
        await reader.read_price("XAUUSD")

    # Called 3 times (one per attempt)
    assert fake_client.close.call_count == 3
