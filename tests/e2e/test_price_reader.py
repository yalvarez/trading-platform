import pytest
from unittest.mock import MagicMock
from tests.e2e.price_reader import PriceReader


class FakeTick:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask


def _mt5_client_returning(tick):
    client = MagicMock()
    client.symbol_info_tick.return_value = tick
    return client


@pytest.mark.asyncio
async def test_read_price_returns_mid_price(monkeypatch):
    fake_client = _mt5_client_returning(FakeTick(bid=2499.5, ask=2500.5))
    monkeypatch.setattr(
        "tests.e2e.price_reader.build_mt5_client",
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
    fake_client.symbol_info_tick.side_effect = fake_tick
    monkeypatch.setattr(
        "tests.e2e.price_reader.build_mt5_client",
        lambda host, port: fake_client,
    )

    reader = PriceReader(host="mt5_acct1", port=8001)
    price = await reader.read_price("XAUUSD")

    assert price == 2500.0
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_read_price_raises_after_exhausting_retries(monkeypatch):
    fake_client = MagicMock()
    fake_client.symbol_info_tick.return_value = None
    monkeypatch.setattr(
        "tests.e2e.price_reader.build_mt5_client",
        lambda host, port: fake_client,
    )

    reader = PriceReader(host="mt5_acct1", port=8001)
    with pytest.raises(RuntimeError):
        await reader.read_price("XAUUSD")


@pytest.mark.asyncio
async def test_read_price_calls_build_mt5_client_with_host_and_port(monkeypatch):
    fake_client = _mt5_client_returning(FakeTick(bid=2499.5, ask=2500.5))
    calls = []

    def fake_build(host, port):
        calls.append((host, port))
        return fake_client

    monkeypatch.setattr("tests.e2e.price_reader.build_mt5_client", fake_build)

    reader = PriceReader(host="mt5_acct1", port=8001)
    price = await reader.read_price("XAUUSD")

    assert price == 2500.0
    assert calls == [("mt5_acct1", 8001)]


@pytest.mark.asyncio
async def test_read_price_does_not_require_closing_client(monkeypatch):
    # MT5Client (services/common/mt5_client.MT5Client) has no close()/disconnect()
    # method, so read_price must not attempt to call one on the object it gets
    # back from build_mt5_client. Use a MagicMock with spec=[] so any attribute
    # access (e.g. a stray .close()) raises AttributeError.
    fake_client = MagicMock(spec=["symbol_info_tick"])
    fake_client.symbol_info_tick.return_value = FakeTick(bid=2499.5, ask=2500.5)
    monkeypatch.setattr(
        "tests.e2e.price_reader.build_mt5_client",
        lambda host, port: fake_client,
    )

    reader = PriceReader(host="mt5_acct1", port=8001)
    price = await reader.read_price("XAUUSD")

    assert price == 2500.0
