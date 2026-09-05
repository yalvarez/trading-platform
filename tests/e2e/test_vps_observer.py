import asyncio
import pytest
import subprocess
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.vps_observer import VpsObserver


@pytest.mark.asyncio
async def test_read_raw_messages_calls_xrange_on_raw_messages_stream():
    fake_redis = MagicMock()
    fake_redis.xrange = AsyncMock(return_value=[
        ("1-0", {"chat_id": "-100123", "text": "XAUUSD BUY NOW"}),
    ])
    observer = VpsObserver(redis_client=fake_redis, mt5_host="mt5_acct1", mt5_port=8001)

    messages = await observer.read_raw_messages(count=10)

    fake_redis.xrange.assert_awaited_once_with("raw_messages", "-", "+", count=10)
    assert messages == [{"chat_id": "-100123", "text": "XAUUSD BUY NOW"}]


@pytest.mark.asyncio
async def test_read_parsed_signals_calls_xrange_on_parsed_signals_stream():
    fake_redis = MagicMock()
    fake_redis.xrange = AsyncMock(return_value=[
        ("2-0", {"symbol": "XAUUSD", "direction": "BUY", "fast": "true"}),
    ])
    observer = VpsObserver(redis_client=fake_redis, mt5_host="mt5_acct1", mt5_port=8001)

    signals = await observer.read_parsed_signals(count=10)

    fake_redis.xrange.assert_awaited_once_with("parsed_signals", "-", "+", count=10)
    assert signals == [{"symbol": "XAUUSD", "direction": "BUY", "fast": "true"}]


def test_grep_container_logs_filters_matching_lines(monkeypatch):
    fake_output = (
        "2026-09-04 INFO [TM][EVENT] group_opened {'group_id': 1}\n"
        "2026-09-04 INFO some other line\n"
        "2026-09-04 INFO [TM][EVENT] open_aborted {'reason': 'no_price'}\n"
    )
    monkeypatch.setattr(
        "tests.e2e.vps_observer.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=0, stdout=fake_output, stderr=""),
    )
    observer = VpsObserver(redis_client=MagicMock(), mt5_host="mt5_acct1", mt5_port=8001)

    lines = observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT]")

    assert len(lines) == 2
    assert "open_aborted" in lines[1]


@pytest.mark.asyncio
async def test_positions_for_symbol_returns_position_dicts(monkeypatch):
    fake_pos = MagicMock(ticket=555, sl=2490.0, tp=0.0, volume=0.01)
    fake_client = MagicMock()
    fake_client.root.positions_get.return_value = [fake_pos]
    monkeypatch.setattr(
        "tests.e2e.vps_observer.rpyc.connect",
        lambda host, port: fake_client,
    )
    observer = VpsObserver(redis_client=MagicMock(), mt5_host="mt5_acct1", mt5_port=8001)

    positions = await observer.positions_for_symbol("XAUUSD")

    assert positions == [{"ticket": 555, "sl": 2490.0, "tp": 0.0, "volume": 0.01}]
    fake_client.root.positions_get.assert_called_once_with(symbol="XAUUSD")


@pytest.mark.asyncio
async def test_positions_for_symbol_closes_connection_on_success(monkeypatch):
    fake_pos = MagicMock(ticket=555, sl=2490.0, tp=0.0, volume=0.01)
    fake_client = MagicMock()
    fake_client.root.positions_get.return_value = [fake_pos]
    monkeypatch.setattr(
        "tests.e2e.vps_observer.rpyc.connect",
        lambda host, port: fake_client,
    )
    observer = VpsObserver(redis_client=MagicMock(), mt5_host="mt5_acct1", mt5_port=8001)

    positions = await observer.positions_for_symbol("XAUUSD")

    assert positions == [{"ticket": 555, "sl": 2490.0, "tp": 0.0, "volume": 0.01}]
    fake_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_positions_for_symbol_closes_connection_on_failure(monkeypatch):
    fake_client = MagicMock()
    fake_client.root.positions_get.side_effect = RuntimeError("connection error")
    monkeypatch.setattr(
        "tests.e2e.vps_observer.rpyc.connect",
        lambda host, port: fake_client,
    )
    observer = VpsObserver(redis_client=MagicMock(), mt5_host="mt5_acct1", mt5_port=8001)

    with pytest.raises(RuntimeError):
        await observer.positions_for_symbol("XAUUSD")

    fake_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_restart_container_calls_docker_restart_and_waits(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr("tests.e2e.vps_observer.subprocess.run", fake_run)
    monkeypatch.setattr("tests.e2e.vps_observer.asyncio.sleep", fake_sleep)

    observer = VpsObserver(redis_client=MagicMock(), mt5_host="mt5_acct1", mt5_port=8001)
    await observer.restart_container("atp-trade-orchestrator", settle_seconds=45)

    assert calls == [["docker", "restart", "atp-trade-orchestrator"]]
    assert sleep_calls == [45]
