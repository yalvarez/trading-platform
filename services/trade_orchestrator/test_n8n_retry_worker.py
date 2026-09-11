import asyncio
import json
import os
import tempfile
import time

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


@pytest.mark.asyncio
async def test_first_failure_schedules_retry_after_backoff_seconds_zero_not_one():
    # Regression for the off-by-one in the backoff schedule: the delay used
    # after a failing attempt must be BACKOFF_SECONDS[attempt] (the attempt
    # that just failed), not BACKOFF_SECONDS[attempt + 1]. A first failure
    # (attempt=0) must be scheduled ~BACKOFF_SECONDS[0]=1s out, not 5s.
    r = fakeredis.aioredis.FakeRedis()
    envelope = {"event_id": "retry-2", "event_type": "x", "channel": "audit",
                "timestamp": "t", "message": "m", "payload": {}}
    await r.rpush(QUEUE_KEY, json.dumps({"envelope": envelope, "attempt": 0}))
    n8n_client = FakeN8nClient(results=[False])

    before = time.time()
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
    after = time.time()

    scores = await r.zrange("n8n_event_retry_delayed", 0, -1, withscores=True)
    assert len(scores) == 1
    _, due_at = scores[0]

    # due_at must reflect BACKOFF_SECONDS[0] == 1 second, not
    # BACKOFF_SECONDS[1] == 5 seconds, measured from around when the worker
    # processed the failed attempt.
    assert (before + BACKOFF_SECONDS[0]) - 1 <= due_at <= (after + BACKOFF_SECONDS[0]) + 1
    assert due_at < before + BACKOFF_SECONDS[1]


@pytest.mark.asyncio
async def test_worker_survives_an_exception_and_keeps_processing_next_iteration():
    # A Redis client whose lpop raises once (simulating a transient Redis
    # error or any other unexpected exception inside the loop) must not kill
    # the worker loop -- it must log and keep running so later iterations
    # still process the queue.
    class FlakyRedis:
        def __init__(self, real):
            self._real = real
            self._raised = False

        async def lpop(self, key):
            if not self._raised:
                self._raised = True
                raise ConnectionError("simulated transient redis failure")
            return await self._real.lpop(key)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_redis = fakeredis.aioredis.FakeRedis()
    flaky = FlakyRedis(real_redis)
    envelope = {"event_id": "survive-1", "event_type": "x", "channel": "audit",
                "timestamp": "t", "message": "m", "payload": {}}
    await enqueue(real_redis, envelope)
    n8n_client = FakeN8nClient(results=[True])

    with tempfile.TemporaryDirectory() as d:
        audit_path = os.path.join(d, "audit_log.jsonl")
        worker_task = asyncio.create_task(
            run_retry_worker(flaky, n8n_client, audit_path, poll_interval_seconds=0.01)
        )
        await asyncio.sleep(0.2)
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    # Despite the first lpop raising, the worker kept running afterwards and
    # delivered the enqueued event.
    assert len(n8n_client.calls) == 1
    assert n8n_client.calls[0] == envelope


@pytest.mark.asyncio
async def test_worker_moves_due_delayed_item_back_to_queue_and_retries_it():
    r = fakeredis.aioredis.FakeRedis()
    envelope = {"event_id": "delayed-1", "event_type": "x", "channel": "audit",
                "timestamp": "t", "message": "m", "payload": {}}
    # Schedule the item as already due (due_at in the past) so the worker's
    # next _requeue_due_delayed pass moves it back to the main queue without
    # needing to wait through a real backoff delay.
    due_at = time.time() - 1
    await r.zadd("n8n_event_retry_delayed", {
        json.dumps({"envelope": envelope, "attempt": 1}): due_at
    })
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

    # The due item was moved out of the delayed set, back onto the main
    # queue, and actually retried against n8n_client.
    assert await r.zcard("n8n_event_retry_delayed") == 0
    assert await r.llen(QUEUE_KEY) == 0
    assert len(n8n_client.calls) == 1
    assert n8n_client.calls[0] == envelope
