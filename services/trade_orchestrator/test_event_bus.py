import json
import os
import tempfile

import fakeredis.aioredis
import pytest

from services.trade_orchestrator.event_bus import EventBus
from services.trade_orchestrator.n8n_retry_worker import QUEUE_KEY


@pytest.mark.asyncio
async def test_emit_writes_full_envelope_to_jsonl():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit_log.jsonl")
        bus = EventBus(path, redis_client=None)

        event_id = await bus.emit("group_opened", "both", "hola", {"group_id": 1})

        with open(path, "r", encoding="utf-8") as f:
            record = json.loads(f.readline())
        assert record["event_id"] == event_id
        assert record["event_type"] == "group_opened"
        assert record["channel"] == "both"
        assert record["message"] == "hola"
        assert record["payload"] == {"group_id": 1}
        assert "timestamp" in record


@pytest.mark.asyncio
async def test_emit_generates_unique_event_ids():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit_log.jsonl")
        bus = EventBus(path, redis_client=None)

        id1 = await bus.emit("a", "audit", "m1", {})
        id2 = await bus.emit("b", "audit", "m2", {})

        assert id1 != id2


@pytest.mark.asyncio
async def test_emit_enqueues_to_redis_when_configured():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit_log.jsonl")
        r = fakeredis.aioredis.FakeRedis()
        bus = EventBus(path, redis_client=r)

        await bus.emit("group_opened", "both", "hola", {"group_id": 1})

        assert await r.llen(QUEUE_KEY) == 1


@pytest.mark.asyncio
async def test_emit_still_writes_jsonl_when_redis_is_none():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit_log.jsonl")
        bus = EventBus(path, redis_client=None)

        await bus.emit("group_opened", "both", "hola", {})

        assert os.path.exists(path)
