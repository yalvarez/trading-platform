import asyncio
import json
import os
import tempfile

import fakeredis.aioredis
import pytest

from services.trade_orchestrator.n8n_retry_worker import (
    enqueue, run_retry_worker, QUEUE_KEY, BACKOFF_SECONDS,
)
from services.trade_orchestrator.audit_log import append_event


class FakeN8nClient:
    def __init__(self, results):
        # results: list of bools consumed in order, one per post_event call
        self._results = list(results)
        self.calls = []

    async def post_event(self, envelope):
        self.calls.append(envelope)
        return self._results.pop(0) if self._results else False


@pytest.mark.asyncio
async def test_enqueue_pushes_envelope_to_redis_list():
    r = fakeredis.aioredis.FakeRedis()
    envelope = {"event_id": "1", "event_type": "x", "channel": "audit",
                "timestamp": "t", "message": "m", "payload": {}}

    await enqueue(r, envelope)

    raw = await r.lpop(QUEUE_KEY)
    item = json.loads(raw)
    assert item["envelope"] == envelope
    assert item["attempt"] == 0


@pytest.mark.asyncio
async def test_worker_delivers_successfully_on_first_attempt():
    r = fakeredis.aioredis.FakeRedis()
    envelope = {"event_id": "1", "event_type": "x", "channel": "audit",
                "timestamp": "t", "message": "m", "payload": {}}
    await enqueue(r, envelope)
    n8n_client = FakeN8nClient(results=[True])

    with tempfile.TemporaryDirectory() as d:
        audit_path = os.path.join(d, "audit_log.jsonl")
        worker_task = asyncio.create_task(
            run_retry_worker(r, n8n_client, audit_path, poll_interval_seconds=0.01)
        )
        await asyncio.sleep(0.1)
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    assert len(n8n_client.calls) == 1
    assert n8n_client.calls[0] == envelope
    remaining = await r.llen(QUEUE_KEY)
    assert remaining == 0


@pytest.mark.asyncio
async def test_worker_marks_dead_letter_after_exhausting_all_backoff_attempts():
    r = fakeredis.aioredis.FakeRedis()
    envelope = {"event_id": "dead-1", "event_type": "x", "channel": "audit",
                "timestamp": "t", "message": "m", "payload": {}}
    with tempfile.TemporaryDirectory() as d:
        audit_path = os.path.join(d, "audit_log.jsonl")
        append_event(audit_path, envelope)
        # Enqueue already at the last attempt index so the test doesn't need
        # to wait through the real backoff delays.
        await r.rpush(QUEUE_KEY, json.dumps({"envelope": envelope, "attempt": len(BACKOFF_SECONDS) - 1}))
        n8n_client = FakeN8nClient(results=[False])

        worker_task = asyncio.create_task(
            run_retry_worker(r, n8n_client, audit_path, poll_interval_seconds=0.01)
        )
        await asyncio.sleep(0.1)
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        with open(audit_path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f.readlines()]
        assert lines[0]["delivery_status"] == "dead_letter"


@pytest.mark.asyncio
async def test_worker_requeues_with_incremented_attempt_on_failure_before_exhausting():
    r = fakeredis.aioredis.FakeRedis()
    envelope = {"event_id": "retry-1", "event_type": "x", "channel": "audit",
                "timestamp": "t", "message": "m", "payload": {}}
    await r.rpush(QUEUE_KEY, json.dumps({"envelope": envelope, "attempt": 0}))
    n8n_client = FakeN8nClient(results=[False])

    with tempfile.TemporaryDirectory() as d:
        audit_path = os.path.join(d, "audit_log.jsonl")
        worker_task = asyncio.create_task(
            run_retry_worker(r, n8n_client, audit_path, poll_interval_seconds=0.01)
        )
        await asyncio.sleep(0.1)
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    # Not yet dead-lettered, not back on the immediate queue (it's delayed).
    assert await r.llen(QUEUE_KEY) == 0
    delayed_count = await r.zcard("n8n_event_retry_delayed")
    assert delayed_count == 1
