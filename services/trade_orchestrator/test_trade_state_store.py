import asyncio
import json
import os
import tempfile

import pytest

from services.trade_orchestrator.trade_state_store import TradeStateStore


class FakeRedis:
    """Minimal redis.asyncio.Redis stand-in — just the three methods TradeStateStore uses."""
    def __init__(self):
        self.store: dict[str, str] = {}
        self.fail = False

    async def set(self, key, value):
        if self.fail:
            raise ConnectionError("simulated redis failure")
        self.store[key] = value

    async def get(self, key):
        if self.fail:
            raise ConnectionError("simulated redis failure")
        return self.store.get(key)

    async def delete(self, key):
        if self.fail:
            raise ConnectionError("simulated redis failure")
        self.store.pop(key, None)

    async def keys(self, pattern):
        if self.fail:
            raise ConnectionError("simulated redis failure")
        prefix = pattern.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]


@pytest.fixture
def tmp_file():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    os.remove(path)  # store must create it on first write, like open(path, "a")
    yield path
    if os.path.exists(path):
        os.remove(path)
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)


@pytest.mark.asyncio
async def test_save_and_load_round_trip(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    doc = {"group_id": 1, "symbol": "XAUUSD", "direction": "BUY", "tp1_price": 4439.8}

    await store.save_group(doc)
    loaded, source = await store.load_group(1)

    assert loaded == doc
    assert source == "redis"


@pytest.mark.asyncio
async def test_close_group_removes_it_from_redis_and_load(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 1, "symbol": "XAUUSD"})

    await store.close_group(1)

    loaded, source = await store.load_group(1)
    assert loaded is None
    assert source == "none"
    assert "trade_groups:1" not in redis.store


@pytest.mark.asyncio
async def test_load_falls_back_to_file_when_redis_has_nothing(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 5, "symbol": "EURUSD"})
    # Simulate Redis losing the key (e.g. flushed) but the file surviving.
    redis.store.clear()

    loaded, source = await store.load_group(5)

    assert loaded == {"group_id": 5, "symbol": "EURUSD"}
    assert source == "file"


@pytest.mark.asyncio
async def test_load_falls_back_to_file_when_redis_errors(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 5, "symbol": "EURUSD"})
    redis.fail = True  # Redis reachable at write time, now erroring

    loaded, source = await store.load_group(5)

    assert loaded == {"group_id": 5, "symbol": "EURUSD"}
    assert source == "file"


@pytest.mark.asyncio
async def test_file_last_entry_wins_across_multiple_writes(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 1, "peak_multiple": 0.1})
    await store.save_group({"group_id": 1, "peak_multiple": 0.5})
    await store.save_group({"group_id": 1, "peak_multiple": 0.9})
    redis.store.clear()  # force reading from the file

    loaded, source = await store.load_group(1)

    assert loaded["peak_multiple"] == 0.9
    assert source == "file"


@pytest.mark.asyncio
async def test_file_close_marker_wins_over_earlier_save(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 1, "symbol": "XAUUSD"})
    await store.close_group(1)
    redis.store.clear()

    loaded, source = await store.load_group(1)

    assert loaded is None
    assert source == "none"


@pytest.mark.asyncio
async def test_save_group_does_not_raise_when_redis_fails(tmp_file):
    redis = FakeRedis()
    redis.fail = True
    store = TradeStateStore(redis, tmp_file)

    # Must not raise -- the file write still succeeds even if Redis is down.
    await store.save_group({"group_id": 1, "symbol": "XAUUSD"})

    redis.fail = False
    loaded, source = await store.load_group(1)  # comes from the file since Redis never had it
    assert loaded == {"group_id": 1, "symbol": "XAUUSD"}
    assert source == "file"


@pytest.mark.asyncio
async def test_load_all_group_ids_unions_redis_and_file(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 1, "symbol": "XAUUSD"})
    await store.save_group({"group_id": 2, "symbol": "EURUSD"})
    await store.close_group(2)  # closed groups still count -- MT5 might still reference the id
    # Simulate group 3 existing only in Redis (e.g. file write raced/failed once)
    redis.store["trade_groups:3"] = json.dumps({"group_id": 3, "symbol": "GBPUSD"})

    ids = await store.load_all_group_ids()

    assert ids == {1, 2, 3}


@pytest.mark.asyncio
async def test_compact_keeps_only_latest_entry_for_active_groups(tmp_file):
    redis = FakeRedis()
    store = TradeStateStore(redis, tmp_file)
    await store.save_group({"group_id": 1, "peak_multiple": 0.1})
    await store.save_group({"group_id": 1, "peak_multiple": 0.5})
    await store.save_group({"group_id": 2, "symbol": "EURUSD"})
    await store.close_group(2)

    await store.compact(active_group_ids={1})

    with open(tmp_file) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 1
    assert lines[0] == {"group_id": 1, "peak_multiple": 0.5}
