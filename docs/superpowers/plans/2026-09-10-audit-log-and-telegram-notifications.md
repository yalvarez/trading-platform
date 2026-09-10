# Audit Log + Telegram Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `trade_orchestrator` a real, queryable audit trail of every event plus human-readable Telegram notifications (via n8n) for the events the user cares about as a trader — including real money P&L on every close.

**Architecture:** A new `EventBus` class replaces `TradeManager._notify`. Every event is (1) written synchronously to a local, never-compacted JSONL file (`data/audit_log.jsonl`) — the source of truth — and (2) pushed onto a Redis list that a background retry worker drains with backoff, POSTing to a single n8n webhook that carries the full envelope (`event_id`, `event_type`, `channel`, `timestamp`, `message`, `payload`). n8n branches on `channel` to decide Data Table vs. Data Table + Telegram. Separately, close-cause detection in the management loop moves from a price-tolerance heuristic to reading MT5's real `deal.reason` field, which also powers two new automatic events (`sl_hit_detected`, `external_close_detected`) and a new manual action (`close_partial_now`).

**Tech Stack:** Python 3, `httpx` (already a dependency) for the webhook POST, `redis.asyncio` (already a dependency, already used for Streams) for the retry queue, `pytest` + `pytest-asyncio` (existing test stack), `SimuladorMT5` (existing MT5 test double).

**Spec:** `docs/superpowers/specs/2026-09-10-audit-log-and-telegram-notifications-design.md`

## Global Constraints

- Local JSONL write (`data/audit_log.jsonl`) happens synchronously and **before** any network attempt, and is never skipped, retried, or blocking-failed by a Redis/n8n outage — this is the non-negotiable safety net from the spec.
- Every event carries the full envelope (`event_id`, `event_type`, `channel`, `timestamp`, `message`, `payload`) regardless of `channel` value — there is exactly one n8n webhook, no separate audit/notify endpoints.
- `channel` is decided in Python per event type (never by n8n) — `"audit"` or `"both"`, per the table in spec §6.
- Retry backoff for the n8n webhook: 1s, 5s, 30s, 2min, then mark `dead_letter` — 4 attempts total, confirmed with the user.
- P&L in money always comes from MT5's real deal fields (`profit`, `volume`, `commission`, `swap` via `history_deals_get`) — never computed manually from price deltas.
- Close-cause detection uses `deal.reason` (`DEAL_REASON_TP` / `DEAL_REASON_SL` / `DEAL_REASON_CLIENT`), not price-tolerance heuristics, for any new logic this plan adds.
- `message` (the Telegram-ready Spanish text) is always built in Python; n8n only relays it verbatim — n8n never reformats.
- Existing event names/behavior are not renamed or removed — only extended (new `payload` fields) or supplemented (new event types alongside old ones).

---

## File Structure

New files:
- `services/trade_orchestrator/event_bus.py` — `EventBus` class: builds the envelope, writes JSONL synchronously, pushes to the Redis retry queue.
- `services/trade_orchestrator/audit_log.py` — pure functions for JSONL read/write/dead-letter-marking, kept separate from `EventBus` so both `EventBus` and the retry worker can use them without circular imports.
- `services/trade_orchestrator/n8n_event_client.py` — thin async HTTP client wrapping the single n8n webhook POST (replaces `N8nWebhookNotifier`'s role for this new flow; the old `N8nWebhookNotifier`/`N8nNotifierAdapter` classes are left in place but no longer called once `TradeManager` is migrated — see Task 8).
- `services/trade_orchestrator/n8n_retry_worker.py` — background asyncio loop: pops from the Redis retry queue, calls `n8n_event_client`, re-queues with backoff or marks dead-letter.
- `services/trade_orchestrator/channel_names.py` — `resolve_channel_name(chat_id, config_provider) -> str`, reading a `CHANNEL_NAMES_JSON` env var, fallback to raw `chat_id`.
- `services/trade_orchestrator/event_messages.py` — pure functions that build the Spanish `message` string per event type (isolates all the Telegram copy in one place, easy to tweak without touching business logic).
- Tests: `services/trade_orchestrator/test_event_bus.py`, `services/trade_orchestrator/test_audit_log.py`, `services/trade_orchestrator/test_n8n_retry_worker.py`, `services/trade_orchestrator/test_channel_names.py`, `services/trade_orchestrator/test_event_messages.py` — following the existing convention of colocating tests with the service (see `test_trade_manager_dual_tp.py`), not under a top-level `tests/` mirror.

Modified files:
- `services/trade_orchestrator/trade_manager.py` — `_notify` becomes a thin wrapper around `EventBus.emit`; `_get_close_price` gains a sibling `_get_close_deal_info` returning `reason`/`profit`/`volume`/`commission`/`swap`; `_closed_at_tp1` replaced by reason-based detection; `_tick_once_account` gains `sl_hit_detected`/`external_close_detected` dispatch; `apply_mgmt_action` gains the `close_partial_now` branch; every `_notify(...)` call site gains the new `payload` fields the spec's catalog requires (channel_name, entry_price, volume, close_price, close_volume, pnl_money, etc.).
- `services/trade_orchestrator/mgmt_api.py` — `MgmtActionRequest` gains `percent: Optional[float] = None`.
- `services/trade_orchestrator/app.py` — wires up `EventBus`, `n8n_retry_worker`, and passes the new `EventBus` into `TradeManager` instead of the old `notifier_adapter`.
- `tests/test_simulador_mt5.py` — `_record_deal` gains `reason`/`profit`/`volume`/`commission`/`swap` parameters (defaulted so existing calls don't break); a new `close_position_directly`-like helper or an extended existing one to simulate SL/TP/external closes with a specific `reason`.
- `services/trade_orchestrator/test_trade_manager_dual_tp.py` — existing tests that assert on `_closed_at_tp1`'s heuristic get updated to the new reason-based mechanism (only if any test exercises it directly; most call through `_tick_once_account` and should keep passing once the simulator defaults `reason` sensibly).
- `.env.example` — add `CHANNEL_NAMES_JSON`, `N8N_EVENT_WEBHOOK_URL`, `N8N_EVENT_WEBHOOK_TOKEN`, `N8N_EVENT_RETRY_BACKOFF_SECONDS` (optional override).
- `docker-compose.yml` — no changes needed (Redis and trade_orchestrator already share the network; no new service).

---

## Task 1: Local audit JSONL writer (`audit_log.py`)

**Files:**
- Create: `services/trade_orchestrator/audit_log.py`
- Test: `services/trade_orchestrator/test_audit_log.py`

**Interfaces:**
- Produces: `append_event(path: str, envelope: dict) -> None` (raises on I/O failure, per Global Constraints — caller decides how to log it, this function doesn't swallow); `mark_dead_letter(path: str, event_id: str) -> bool` (rewrites the matching line in-place with `delivery_status: "dead_letter"` added, returns `True` if found and updated, `False` if `event_id` not found in the file).

- [ ] **Step 1: Write the failing tests**

```python
# services/trade_orchestrator/test_audit_log.py
import json
import os
import tempfile

import pytest

from services.trade_orchestrator.audit_log import append_event, mark_dead_letter


def test_append_event_writes_one_json_line():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit_log.jsonl")
        envelope = {"event_id": "abc-123", "event_type": "group_opened", "channel": "both",
                    "timestamp": "2026-09-10T14:32:01.123Z", "message": "hi", "payload": {"group_id": 1}}
        append_event(path, envelope)

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == envelope


def test_append_event_appends_without_truncating():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit_log.jsonl")
        append_event(path, {"event_id": "1", "event_type": "a", "channel": "audit",
                             "timestamp": "t", "message": "m", "payload": {}})
        append_event(path, {"event_id": "2", "event_type": "b", "channel": "audit",
                             "timestamp": "t", "message": "m", "payload": {}})

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event_id"] == "1"
        assert json.loads(lines[1])["event_id"] == "2"


def test_append_event_creates_file_and_parent_dir_if_missing():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "nested", "audit_log.jsonl")
        append_event(path, {"event_id": "1", "event_type": "a", "channel": "audit",
                             "timestamp": "t", "message": "m", "payload": {}})
        assert os.path.exists(path)


def test_mark_dead_letter_updates_matching_line():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit_log.jsonl")
        append_event(path, {"event_id": "1", "event_type": "a", "channel": "audit",
                             "timestamp": "t", "message": "m", "payload": {}})
        append_event(path, {"event_id": "2", "event_type": "b", "channel": "audit",
                             "timestamp": "t", "message": "m", "payload": {}})

        found = mark_dead_letter(path, "2")

        assert found is True
        with open(path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f.readlines()]
        assert lines[0].get("delivery_status") is None
        assert lines[1]["delivery_status"] == "dead_letter"


def test_mark_dead_letter_returns_false_when_event_id_not_found():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit_log.jsonl")
        append_event(path, {"event_id": "1", "event_type": "a", "channel": "audit",
                             "timestamp": "t", "message": "m", "payload": {}})

        assert mark_dead_letter(path, "does-not-exist") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_audit_log.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.trade_orchestrator.audit_log'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/trade_orchestrator/audit_log.py
"""
audit_log.py
Escritura del log de auditoria local (JSONL append-only, nunca
compactado) que sirve como fuente de verdad primaria de todo evento de
negocio -- independiente de que Redis o n8n esten disponibles.
Ver docs/superpowers/specs/2026-09-10-audit-log-and-telegram-notifications-design.md
"""
import json
import os


def append_event(path: str, envelope: dict) -> None:
    """Agrega `envelope` como una linea JSON al final de `path`. Crea el
    archivo y cualquier directorio padre faltante si no existen."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(envelope, ensure_ascii=False) + "\n")


def mark_dead_letter(path: str, event_id: str) -> bool:
    """Reescribe la linea cuyo event_id coincide, agregando
    delivery_status='dead_letter'. Retorna True si la encontro."""
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    found = False
    new_lines = []
    for line in lines:
        record = json.loads(line)
        if record.get("event_id") == event_id:
            record["delivery_status"] = "dead_letter"
            found = True
        new_lines.append(json.dumps(record, ensure_ascii=False) + "\n")
    if found:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    return found
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_audit_log.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/audit_log.py services/trade_orchestrator/test_audit_log.py
git commit -m "feat(trade_orchestrator): add local audit JSONL writer

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: n8n event HTTP client (`n8n_event_client.py`)

**Files:**
- Create: `services/trade_orchestrator/n8n_event_client.py`
- Test: `services/trade_orchestrator/test_n8n_event_client.py`

**Interfaces:**
- Consumes: `httpx.AsyncClient` (existing dependency, same pattern as `services/common/n8n_notifier.py`).
- Produces: `class N8nEventClient: def __init__(self, webhook_url: str, token: str = ""); async def post_event(self, envelope: dict) -> bool` — returns `True` on 2xx, `False` on any failure (never raises).

- [ ] **Step 1: Write the failing tests**

```python
# services/trade_orchestrator/test_n8n_event_client.py
import httpx
import pytest
import respx

from services.trade_orchestrator.n8n_event_client import N8nEventClient


@pytest.mark.asyncio
@respx.mock
async def test_post_event_returns_true_on_2xx():
    route = respx.post("https://n8n.example.com/webhook/events").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = N8nEventClient("https://n8n.example.com/webhook/events")

    ok = await client.post_event({"event_id": "1", "event_type": "x", "channel": "audit",
                                   "timestamp": "t", "message": "m", "payload": {}})

    assert ok is True
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_post_event_sends_full_envelope_as_json_body():
    envelope = {"event_id": "1", "event_type": "group_opened", "channel": "both",
                "timestamp": "t", "message": "hi", "payload": {"group_id": 61}}
    route = respx.post("https://n8n.example.com/webhook/events").mock(
        return_value=httpx.Response(200)
    )
    client = N8nEventClient("https://n8n.example.com/webhook/events")

    await client.post_event(envelope)

    assert route.calls.last.request.content
    import json
    sent = json.loads(route.calls.last.request.content)
    assert sent == envelope


@pytest.mark.asyncio
@respx.mock
async def test_post_event_sends_token_header_when_configured():
    route = respx.post("https://n8n.example.com/webhook/events").mock(
        return_value=httpx.Response(200)
    )
    client = N8nEventClient("https://n8n.example.com/webhook/events", token="secret123")

    await client.post_event({"event_id": "1", "event_type": "x", "channel": "audit",
                              "timestamp": "t", "message": "m", "payload": {}})

    assert route.calls.last.request.headers["X-N8N-Token"] == "secret123"


@pytest.mark.asyncio
@respx.mock
async def test_post_event_returns_false_on_non_2xx():
    respx.post("https://n8n.example.com/webhook/events").mock(
        return_value=httpx.Response(500)
    )
    client = N8nEventClient("https://n8n.example.com/webhook/events")

    ok = await client.post_event({"event_id": "1", "event_type": "x", "channel": "audit",
                                   "timestamp": "t", "message": "m", "payload": {}})

    assert ok is False


@pytest.mark.asyncio
@respx.mock
async def test_post_event_returns_false_on_network_error_without_raising():
    respx.post("https://n8n.example.com/webhook/events").mock(side_effect=httpx.ConnectError("boom"))
    client = N8nEventClient("https://n8n.example.com/webhook/events")

    ok = await client.post_event({"event_id": "1", "event_type": "x", "channel": "audit",
                                   "timestamp": "t", "message": "m", "payload": {}})

    assert ok is False
```

Note: `respx` is a new test dependency (HTTP mocking library for `httpx`). Add it in Step 3 alongside the implementation.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_n8n_event_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.trade_orchestrator.n8n_event_client'` (and possibly `ModuleNotFoundError: No module named 'respx'` — install it first: `pip install respx`)

- [ ] **Step 3: Write minimal implementation**

```python
# services/trade_orchestrator/n8n_event_client.py
"""
n8n_event_client.py
Cliente HTTP para el unico webhook de eventos de n8n (auditoria +
notificaciones). A diferencia de services/common/n8n_notifier.py, este
cliente envia el envelope completo tal cual (event_id, event_type,
channel, timestamp, message, payload) sin transformarlo -- n8n decide
que hacer segun `channel`.
"""
import logging

import httpx

log = logging.getLogger("trade_orchestrator.n8n_event_client")


class N8nEventClient:
    def __init__(self, webhook_url: str, token: str = ""):
        self.webhook_url = webhook_url
        self.token = token

    async def post_event(self, envelope: dict) -> bool:
        headers = {"X-N8N-Token": self.token} if self.token else None
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(self.webhook_url, json=envelope, headers=headers, timeout=10.0)
            if 200 <= resp.status_code < 300:
                return True
            log.warning("[N8N_EVENT] webhook respondio status=%s event_id=%s", resp.status_code, envelope.get("event_id"))
            return False
        except Exception as e:
            log.warning("[N8N_EVENT] error enviando evento event_id=%s: %s", envelope.get("event_id"), e)
            return False
```

Also add `respx` to `services/trade_orchestrator/requirements.txt` (test-only, but this repo doesn't separate test requirements — append it there):

```
respx
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pip install respx && pytest services/trade_orchestrator/test_n8n_event_client.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/n8n_event_client.py services/trade_orchestrator/test_n8n_event_client.py services/trade_orchestrator/requirements.txt
git commit -m "feat(trade_orchestrator): add n8n event webhook HTTP client

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: Redis-backed retry queue + worker (`n8n_retry_worker.py`)

**Files:**
- Create: `services/trade_orchestrator/n8n_retry_worker.py`
- Test: `services/trade_orchestrator/test_n8n_retry_worker.py`

**Interfaces:**
- Consumes: `redis.asyncio.Redis` (existing pattern, see `services/common/redis_streams.py`), `N8nEventClient.post_event` (Task 2), `audit_log.mark_dead_letter` (Task 1).
- Produces: `QUEUE_KEY = "n8n_event_retry_queue"` (Redis list name, importable constant); `async def enqueue(redis_client, envelope: dict) -> None` (pushes `{"envelope": ..., "attempt": 0}` as JSON via `RPUSH`); `async def run_retry_worker(redis_client, n8n_client: N8nEventClient, audit_log_path: str, *, poll_interval_seconds: float = 1.0) -> None` (infinite loop, meant to be wrapped in `asyncio.create_task`; pops via `BLPOP`, attempts delivery, re-queues with a delay via a Redis sorted set for delayed re-delivery, or marks dead-letter after exhausting `BACKOFF_SECONDS`).
- `BACKOFF_SECONDS = [1, 5, 30, 120]` (module-level constant, matches the 4-attempt schedule agreed with the user).

**Design note for the implementer:** delayed retry without external scheduling infra is done with a Redis sorted set (`ZADD` with score = unix timestamp when the retry is due; a companion loop `ZRANGEBYSCORE` for due items and `ZREM`s them before requeueing to the main list). Keep this inside `n8n_retry_worker.py` as a private `_DELAYED_KEY = "n8n_event_retry_delayed"` — don't expose it as part of the public interface above.

- [ ] **Step 1: Write the failing tests**

```python
# services/trade_orchestrator/test_n8n_retry_worker.py
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
```

Note: `fakeredis` (with asyncio support) is a new test dependency. Add it in Step 3.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_n8n_retry_worker.py -v`
Expected: FAIL with `ModuleNotFoundError` (missing module and/or missing `fakeredis` — install first: `pip install fakeredis`)

- [ ] **Step 3: Write minimal implementation**

```python
# services/trade_orchestrator/n8n_retry_worker.py
"""
n8n_retry_worker.py
Cola de reintento (Redis) para el envio de eventos al webhook de n8n.
Un evento que falla se reencola con backoff creciente
(BACKOFF_SECONDS); agotados los intentos, se marca dead_letter en el
JSONL local de auditoria para revision manual -- nunca se descarta en
silencio. El JSONL local ya tiene el evento (audit_log.append_event lo
escribe antes de llegar aqui), asi que ningun evento se pierde aunque
n8n este caido.
"""
import asyncio
import json
import logging
import time

from .audit_log import mark_dead_letter

log = logging.getLogger("trade_orchestrator.n8n_retry_worker")

QUEUE_KEY = "n8n_event_retry_queue"
_DELAYED_KEY = "n8n_event_retry_delayed"
BACKOFF_SECONDS = [1, 5, 30, 120]


async def enqueue(redis_client, envelope: dict) -> None:
    item = {"envelope": envelope, "attempt": 0}
    await redis_client.rpush(QUEUE_KEY, json.dumps(item, ensure_ascii=False))


async def _requeue_due_delayed(redis_client) -> None:
    """Mueve de vuelta a la cola principal los items cuyo tiempo de espera ya paso."""
    now = time.time()
    due = await redis_client.zrangebyscore(_DELAYED_KEY, "-inf", now)
    for raw in due:
        removed = await redis_client.zrem(_DELAYED_KEY, raw)
        if removed:
            await redis_client.rpush(QUEUE_KEY, raw)


async def run_retry_worker(redis_client, n8n_client, audit_log_path: str, *, poll_interval_seconds: float = 1.0) -> None:
    while True:
        await _requeue_due_delayed(redis_client)

        raw = await redis_client.lpop(QUEUE_KEY)
        if raw is None:
            await asyncio.sleep(poll_interval_seconds)
            continue

        item = json.loads(raw)
        envelope = item["envelope"]
        attempt = item["attempt"]

        ok = await n8n_client.post_event(envelope)
        if ok:
            continue

        if attempt >= len(BACKOFF_SECONDS) - 1:
            log.error("[N8N_RETRY] evento agoto reintentos, marcado dead_letter event_id=%s", envelope.get("event_id"))
            mark_dead_letter(audit_log_path, envelope.get("event_id"))
            continue

        next_attempt = attempt + 1
        delay = BACKOFF_SECONDS[next_attempt]
        due_at = time.time() + delay
        next_item = json.dumps({"envelope": envelope, "attempt": next_attempt}, ensure_ascii=False)
        await redis_client.zadd(_DELAYED_KEY, {next_item: due_at})
        log.warning("[N8N_RETRY] evento fallo, reintento %s en %ss event_id=%s", next_attempt, delay, envelope.get("event_id"))
```

Also add `fakeredis` to `services/trade_orchestrator/requirements.txt`:

```
fakeredis
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pip install fakeredis && pytest services/trade_orchestrator/test_n8n_retry_worker.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/n8n_retry_worker.py services/trade_orchestrator/test_n8n_retry_worker.py services/trade_orchestrator/requirements.txt
git commit -m "feat(trade_orchestrator): add Redis-backed retry worker for n8n event delivery

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: `EventBus` — ties audit_log + retry queue together

**Files:**
- Create: `services/trade_orchestrator/event_bus.py`
- Test: `services/trade_orchestrator/test_event_bus.py`

**Interfaces:**
- Consumes: `audit_log.append_event` (Task 1), `n8n_retry_worker.enqueue` (Task 3).
- Produces: `class EventBus: def __init__(self, audit_log_path: str, redis_client=None); async def emit(self, event_type: str, channel: str, message: str, payload: dict) -> str` — builds the envelope (generates `event_id` via `uuid.uuid4()`, `timestamp` via `datetime.now(timezone.utc).isoformat()`), writes it to the JSONL synchronously, then (if `redis_client` is not `None`) enqueues it for delivery; returns the generated `event_id`. If `redis_client` is `None` (no Redis configured), logs a warning and skips delivery — JSONL write still always happens.

- [ ] **Step 1: Write the failing tests**

```python
# services/trade_orchestrator/test_event_bus.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_event_bus.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.trade_orchestrator.event_bus'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/trade_orchestrator/event_bus.py
"""
event_bus.py
Punto unico de emision de eventos de negocio de trade_orchestrator.
Reemplaza TradeManager._notify: siempre escribe al log de auditoria
local (sincrono, antes que cualquier llamada de red) y, si hay Redis
configurado, encola el evento para entrega (con reintento) al webhook
de n8n. Ver spec:
docs/superpowers/specs/2026-09-10-audit-log-and-telegram-notifications-design.md
"""
import logging
import uuid
from datetime import datetime, timezone

from .audit_log import append_event
from .n8n_retry_worker import enqueue

log = logging.getLogger("trade_orchestrator.event_bus")


class EventBus:
    def __init__(self, audit_log_path: str, redis_client=None):
        self.audit_log_path = audit_log_path
        self.redis_client = redis_client

    async def emit(self, event_type: str, channel: str, message: str, payload: dict) -> str:
        event_id = str(uuid.uuid4())
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "channel": channel,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": message,
            "payload": payload,
        }
        append_event(self.audit_log_path, envelope)

        if self.redis_client is None:
            log.warning("[EVENT_BUS] Redis no configurado, evento no se envia a n8n event_id=%s", event_id)
            return event_id

        try:
            await enqueue(self.redis_client, envelope)
        except Exception as e:
            log.warning("[EVENT_BUS] fallo encolando evento a Redis event_id=%s: %s", event_id, e)

        return event_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_event_bus.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/event_bus.py services/trade_orchestrator/test_event_bus.py
git commit -m "feat(trade_orchestrator): add EventBus tying audit log and n8n retry queue together

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 5: Channel name resolution (`channel_names.py`)

**Files:**
- Create: `services/trade_orchestrator/channel_names.py`
- Test: `services/trade_orchestrator/test_channel_names.py`

**Interfaces:**
- Produces: `def resolve_channel_name(chat_id: Optional[str], channel_names: dict) -> str` — pure function, no I/O; `channel_names` is a plain `{chat_id_str: name_str}` dict already parsed from config (parsing `CHANNEL_NAMES_JSON` happens at the `app.py` wiring level, Task 9 — keeping this function pure makes it trivial to test).

- [ ] **Step 1: Write the failing tests**

```python
# services/trade_orchestrator/test_channel_names.py
from services.trade_orchestrator.channel_names import resolve_channel_name


def test_resolves_known_chat_id_to_name():
    mapping = {"-1001234567890": "Oro Premium"}
    assert resolve_channel_name("-1001234567890", mapping) == "Oro Premium"


def test_falls_back_to_raw_chat_id_when_unmapped():
    mapping = {"-1001234567890": "Oro Premium"}
    assert resolve_channel_name("-999", mapping) == "-999"


def test_falls_back_to_placeholder_when_chat_id_is_none():
    mapping = {"-1001234567890": "Oro Premium"}
    assert resolve_channel_name(None, mapping) == "N/D"


def test_works_with_empty_mapping():
    assert resolve_channel_name("-1001234567890", {}) == "-1001234567890"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_channel_names.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.trade_orchestrator.channel_names'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/trade_orchestrator/channel_names.py
"""
channel_names.py
Resuelve un chat_id de Telegram a un nombre legible de canal, usando un
mapeo simple mantenido en config (env var CHANNEL_NAMES_JSON, parseada
en app.py). No existe ningun mapeo asi en el sistema hoy -- ver spec
seccion "Nombre de canal".
"""
from typing import Optional


def resolve_channel_name(chat_id: Optional[str], channel_names: dict) -> str:
    if chat_id is None:
        return "N/D"
    return channel_names.get(str(chat_id), str(chat_id))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_channel_names.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/channel_names.py services/trade_orchestrator/test_channel_names.py
git commit -m "feat(trade_orchestrator): add chat_id to channel name resolution

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 6: Telegram message builders (`event_messages.py`)

**Files:**
- Create: `services/trade_orchestrator/event_messages.py`
- Test: `services/trade_orchestrator/test_event_messages.py`

**Interfaces:**
- Produces: one pure function per user-facing event type, each taking only the fields it needs and returning a formatted Spanish string. Names match the event catalog in spec §6:
  - `build_group_opened_message(channel_name, group_id, symbol, direction, entry_price, sl, tp1, tp2, volume) -> str`
  - `build_tp1_hit_message(channel_name, group_id, symbol, direction, close_price, close_volume, pnl_money, account_currency) -> str`
  - `build_tp2_partial_closed_message(channel_name, group_id, symbol, direction, close_price, close_volume, pnl_money, remaining_volume) -> str`
  - `build_sl_hit_message(channel_name, group_id, symbol, direction, close_price, close_volume, pnl_money) -> str`
  - `build_external_close_message(channel_name, group_id, symbol, direction, leg, close_price, close_volume, pnl_money) -> str`
  - `build_close_now_message(channel_name, group_id, raw_text, leg_results, total_pnl_money) -> str` where `leg_results` is `list[dict]` with keys `leg, close_price, close_volume, pnl_money`
  - `build_close_partial_now_message(channel_name, group_id, raw_text, percent_requested, leg_results) -> str`
  - `build_move_sl_be_applied_message(channel_name, group_id, new_sl, raw_text) -> str`
  - `build_partial_failure_message(channel_name, group_id, leg_summaries) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# services/trade_orchestrator/test_event_messages.py
from services.trade_orchestrator.event_messages import (
    build_group_opened_message, build_tp1_hit_message, build_tp2_partial_closed_message,
    build_sl_hit_message, build_external_close_message, build_close_now_message,
    build_close_partial_now_message, build_move_sl_be_applied_message,
    build_partial_failure_message,
)


def test_group_opened_message_includes_all_key_fields():
    msg = build_group_opened_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction="BUY",
        entry_price=1.09345, sl=1.09100, tp1=1.09500, tp2=1.09800, volume=0.02,
    )
    assert "Oro Premium" in msg
    assert "61" in msg
    assert "EURUSD" in msg
    assert "BUY" in msg
    assert "1.09345" in msg
    assert "1.091" in msg  # sl
    assert "1.095" in msg  # tp1
    assert "1.098" in msg  # tp2
    assert "0.02" in msg


def test_tp1_hit_message_includes_pnl():
    msg = build_tp1_hit_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction="BUY",
        close_price=1.09500, close_volume=0.01, pnl_money=12.50, account_currency="USD",
    )
    assert "TP1" in msg
    assert "12.50" in msg
    assert "USD" in msg


def test_sl_hit_message_shows_negative_pnl_clearly():
    msg = build_sl_hit_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction="BUY",
        close_price=1.09100, close_volume=0.01, pnl_money=-24.50,
    )
    assert "-24.50" in msg
    assert "STOP" in msg.upper()


def test_external_close_message_flags_it_as_outside_the_system():
    msg = build_external_close_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction="BUY",
        leg="runner", close_price=1.09200, close_volume=0.01, pnl_money=-5.0,
    )
    assert "externo" in msg.lower() or "fuera" in msg.lower()


def test_close_now_message_includes_raw_text_and_total():
    msg = build_close_now_message(
        channel_name="Oro Premium", group_id=61, raw_text="cierren esa operacion",
        leg_results=[
            {"leg": "tp1", "close_price": 1.095, "close_volume": 0.01, "pnl_money": 5.0},
            {"leg": "runner", "close_price": 1.093, "close_volume": 0.01, "pnl_money": 3.2},
        ],
        total_pnl_money=8.2,
    )
    assert "cierren esa operacion" in msg
    assert "8.2" in msg


def test_close_partial_now_message_includes_percent_requested():
    msg = build_close_partial_now_message(
        channel_name="Oro Premium", group_id=61, raw_text="cierra 30%",
        percent_requested=30.0,
        leg_results=[{"leg": "runner", "close_price": 1.093, "close_volume": 0.006, "pnl_money": 1.9}],
    )
    assert "30" in msg


def test_move_sl_be_applied_message_includes_new_sl():
    msg = build_move_sl_be_applied_message(
        channel_name="Oro Premium", group_id=61, new_sl=1.09345, raw_text="pon en be",
    )
    assert "1.09345" in msg
    assert "breakeven" in msg.lower() or "BE" in msg


def test_partial_failure_message_flags_it_needs_review():
    msg = build_partial_failure_message(
        channel_name="Oro Premium", group_id=61,
        leg_summaries=["tp1 (ticket=1, rechazado)"],
    )
    assert "revisar" in msg.lower() or "revis" in msg.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_event_messages.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.trade_orchestrator.event_messages'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/trade_orchestrator/event_messages.py
"""
event_messages.py
Construye el texto legible en espanol (`message`) de cada evento
user-facing, listo para reenviar tal cual a Telegram via n8n. Toda la
redaccion vive aqui, separada de la logica de negocio de trade_manager.py,
para poder ajustar el tono/formato sin tocar la logica de gestion.
"""
from typing import Optional


def _fmt_price(value: Optional[float]) -> str:
    return f"{value:.5f}" if value is not None else "N/D"


def _fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "N/D"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}"


def build_group_opened_message(*, channel_name, group_id, symbol, direction, entry_price, sl, tp1, tp2, volume) -> str:
    return (
        f"\U0001F7E2 APERTURA — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {direction.upper()}\n"
        f"Entrada: {_fmt_price(entry_price)}\n"
        f"SL: {_fmt_price(sl)} | TP1: {_fmt_price(tp1)} | TP2: {_fmt_price(tp2)}\n"
        f"Volumen: {volume} lots"
    )


def build_tp1_hit_message(*, channel_name, group_id, symbol, direction, close_price, close_volume, pnl_money, account_currency) -> str:
    return (
        f"✅ TP1 ALCANZADO — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {direction.upper()}\n"
        f"Cerrado: {close_volume} lots @ {_fmt_price(close_price)}\n"
        f"Resultado: {_fmt_money(pnl_money)} {account_currency}\n"
        f"SL movido a break-even"
    )


def build_tp2_partial_closed_message(*, channel_name, group_id, symbol, direction, close_price, close_volume, pnl_money, remaining_volume) -> str:
    return (
        f"✅ TP2 ALCANZADO — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {direction.upper()}\n"
        f"Cerrado 50%: {close_volume} lots @ {_fmt_price(close_price)}\n"
        f"Resultado: {_fmt_money(pnl_money)}\n"
        f"Runner sigue abierto con trailing ({remaining_volume} lots restantes)"
    )


def build_sl_hit_message(*, channel_name, group_id, symbol, direction, close_price, close_volume, pnl_money) -> str:
    return (
        f"\U0001F534 STOP LOSS — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {direction.upper()}\n"
        f"Cerrado: {close_volume} lots @ {_fmt_price(close_price)}\n"
        f"Resultado: {_fmt_money(pnl_money)}"
    )


def build_external_close_message(*, channel_name, group_id, symbol, direction, leg, close_price, close_volume, pnl_money) -> str:
    return (
        f"\U0001F6A8 CIERRE EXTERNO DETECTADO — Canal: {channel_name} (grupo {group_id})\n"
        f"{symbol} {direction.upper()} ({leg})\n"
        f"Cerrado por fuera del sistema: {close_volume} lots @ {_fmt_price(close_price)}\n"
        f"Resultado: {_fmt_money(pnl_money)}\n"
        f"Revisar la cuenta — este cierre no fue TP, SL ni una orden via Telegram."
    )


def build_close_now_message(*, channel_name, group_id, raw_text, leg_results, total_pnl_money) -> str:
    legs_text = ", ".join(
        f"{lr['leg']} ({lr['close_volume']} lots @ {_fmt_price(lr['close_price'])}, {_fmt_money(lr['pnl_money'])})"
        for lr in leg_results
    )
    return (
        f"⚠️ CIERRE MANUAL — Canal: {channel_name} (grupo {group_id})\n"
        f"Motivo: \"{raw_text}\"\n"
        f"{legs_text}\n"
        f"Total: {_fmt_money(total_pnl_money)}"
    )


def build_close_partial_now_message(*, channel_name, group_id, raw_text, percent_requested, leg_results) -> str:
    legs_text = ", ".join(
        f"{lr['leg']} ({lr['close_volume']} lots @ {_fmt_price(lr['close_price'])}, {_fmt_money(lr['pnl_money'])})"
        for lr in leg_results
    )
    return (
        f"⚠️ CIERRE PARCIAL MANUAL ({percent_requested:.0f}%) — Canal: {channel_name} (grupo {group_id})\n"
        f"Motivo: \"{raw_text}\"\n"
        f"{legs_text}"
    )


def build_move_sl_be_applied_message(*, channel_name, group_id, new_sl, raw_text) -> str:
    return (
        f"\U0001F6E1️ BREAKEVEN — Canal: {channel_name} (grupo {group_id})\n"
        f"SL movido a breakeven manualmente: {_fmt_price(new_sl)}\n"
        f"Motivo: \"{raw_text}\""
    )


def build_partial_failure_message(*, channel_name, group_id, leg_summaries) -> str:
    legs_text = ", ".join(leg_summaries) if leg_summaries else "ninguna pierna confirmada"
    return (
        f"⚠️ CIERRE INCOMPLETO — Canal: {channel_name} (grupo {group_id})\n"
        f"Al menos una pierna fue rechazada por el broker. Piernas: {legs_text}\n"
        f"Revisar manualmente — puede quedar una posicion abierta."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_event_messages.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/event_messages.py services/trade_orchestrator/test_event_messages.py
git commit -m "feat(trade_orchestrator): add Telegram message builders for user-facing events

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 7: Extend `SimuladorMT5` to model `deal.reason`/`profit`/`volume`/`commission`/`swap`

**Files:**
- Modify: `tests/test_simulador_mt5.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_record_deal(self, ticket, *, entry, price, reason=0, profit=0.0, volume=None, commission=0.0, swap=0.0)` (defaults keep every existing caller working unchanged, `reason=0` is `DEAL_REASON_CLIENT`); `partial_close(self, account, ticket, percent, *, reason=0, profit=0.0)` gains optional `reason`/`profit` kwargs (MT5's `DEAL_REASON_CLIENT == 0`, the current implicit default, stays the default); a new method `close_position_by_sl(self, ticket, *, close_price=None, profit=0.0)` and `close_position_by_tp(self, ticket, *, close_price=None, profit=0.0)` — test helpers that call `_record_deal` with the right `reason` constant and delete the position, mirroring the existing `close_position_directly` but with an explicit, named cause (needed so tests in Task 8 can simulate real SL/TP closes distinctly from `close_position_directly`'s "any other cause").

**Design note:** MT5's real constants are `DEAL_REASON_CLIENT = 0`, `DEAL_REASON_SL = 4`, `DEAL_REASON_TP = 5` (per MetaTrader5 Python API). Use these exact integer values so any future comparison against `mt5.DEAL_REASON_SL` (if the code ever imports the real enum) still lines up.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_simulador_mt5.py` (or a new focused test file `services/trade_orchestrator/test_simulador_mt5_deal_reason.py` — pick the existing file since `SimuladorMT5` already lives there and its own tests are colocated):

```python
def test_record_deal_defaults_keep_backward_compatible_shape():
    sim = SimuladorMT5()
    sim.positions[1] = {"ticket": 1, "symbol": "XAUUSD", "volume": 0.02, "price_open": 2500.0,
                         "sl": 0.0, "tp": 0.0, "price_current": 2500.0, "type": 0, "comment": "", "magic": 0}
    sim._record_deal(1, entry=1, price=2510.0)

    deals = sim.history_deals_get(position=1)
    assert deals[0].reason == 0  # DEAL_REASON_CLIENT default
    assert deals[0].profit == 0.0
    assert deals[0].commission == 0.0
    assert deals[0].swap == 0.0


def test_close_position_by_sl_sets_reason_and_profit():
    sim = SimuladorMT5()
    sim.positions[1] = {"ticket": 1, "symbol": "XAUUSD", "volume": 0.02, "price_open": 2500.0,
                         "sl": 2490.0, "tp": 0.0, "price_current": 2490.0, "type": 0, "comment": "", "magic": 0}

    sim.close_position_by_sl(1, close_price=2490.0, profit=-20.0)

    deals = sim.history_deals_get(position=1)
    assert deals[-1].reason == 4  # DEAL_REASON_SL
    assert deals[-1].price == 2490.0
    assert deals[-1].profit == -20.0
    assert 1 not in sim.positions


def test_close_position_by_tp_sets_reason_and_profit():
    sim = SimuladorMT5()
    sim.positions[1] = {"ticket": 1, "symbol": "XAUUSD", "volume": 0.02, "price_open": 2500.0,
                         "sl": 0.0, "tp": 2510.0, "price_current": 2510.0, "type": 0, "comment": "", "magic": 0}

    sim.close_position_by_tp(1, close_price=2510.0, profit=20.0)

    deals = sim.history_deals_get(position=1)
    assert deals[-1].reason == 5  # DEAL_REASON_TP
    assert deals[-1].profit == 20.0
    assert 1 not in sim.positions


def test_partial_close_still_defaults_to_client_reason():
    sim = SimuladorMT5()
    sim.positions[1] = {"ticket": 1, "symbol": "XAUUSD", "volume": 0.02, "price_open": 2500.0,
                         "sl": 0.0, "tp": 0.0, "price_current": 2505.0, "type": 0, "comment": "", "magic": 0}

    sim.partial_close(None, 1, 100)

    deals = sim.history_deals_get(position=1)
    assert deals[-1].reason == 0  # DEAL_REASON_CLIENT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_simulador_mt5.py -k "reason or close_position_by" -v`
Expected: FAIL — `AttributeError: 'TradeDeal' object has no attribute 'reason'` (or similar) and `AttributeError: 'SimuladorMT5' object has no attribute 'close_position_by_sl'`

- [ ] **Step 3: Modify `SimuladorMT5`**

Replace the existing `_record_deal` and `partial_close`, and add the two new helper methods:

```python
DEAL_REASON_CLIENT = 0
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5


class SimuladorMT5:
    # ... (existing __init__ unchanged) ...

    def _record_deal(self, ticket, *, entry, price, reason=DEAL_REASON_CLIENT, profit=0.0,
                      volume=None, commission=0.0, swap=0.0):
        self.last_deal += 1
        pos = self.positions.get(ticket, {})
        self.deals_by_position.setdefault(ticket, []).append({
            'ticket': self.last_deal,
            'order': ticket,
            'position_id': ticket,
            'price': price,
            'entry': entry,
            'time': self.last_deal,
            'reason': reason,
            'profit': profit,
            'volume': volume if volume is not None else pos.get('volume', 0.0),
            'commission': commission,
            'swap': swap,
        })

    # ... order_send, positions_get, symbol_info, symbol_select, tick_price unchanged ...

    def partial_close(self, account, ticket, percent, *, reason=DEAL_REASON_CLIENT, profit=0.0):
        """Cierra (parcial o totalmente) una posicion simulada. Si percent>=100, elimina la posicion."""
        pos = self.positions.get(ticket)
        if not pos:
            return False
        closed_volume = float(pos.get('volume', 0.0)) * (percent / 100.0)
        self._record_deal(ticket, entry=1, price=pos.get('price_current', self.price),
                           reason=reason, profit=profit, volume=closed_volume)
        if percent >= 100:
            del self.positions[ticket]
        else:
            pos['volume'] = max(0.0, float(pos.get('volume', 0.0)) * (1 - percent / 100.0))
        return True

    def history_deals_get(self, *args, position=None, ticket=None, **kwargs):
        """Devuelve los deals registrados para `position` (o `ticket`, tratado como alias)."""
        pos_ticket = position if position is not None else ticket
        deals = self.deals_by_position.get(pos_ticket, [])
        return [type('TradeDeal', (), d)() for d in deals]

    def close_position_directly(self, ticket, *, close_price=None):
        """Test helper: simula un cierre fuera de banda de causa desconocida
        (equivalente a DEAL_REASON_CLIENT generico, sin profit real)."""
        pos = self.positions.get(ticket)
        if not pos:
            return
        price = close_price if close_price is not None else pos.get('price_current', self.price)
        self._record_deal(ticket, entry=1, price=price, reason=DEAL_REASON_CLIENT)
        del self.positions[ticket]

    def close_position_by_sl(self, ticket, *, close_price=None, profit=0.0):
        """Test helper: simula que el broker cerro la posicion por stop loss."""
        pos = self.positions.get(ticket)
        if not pos:
            return
        price = close_price if close_price is not None else pos.get('sl', self.price)
        self._record_deal(ticket, entry=1, price=price, reason=DEAL_REASON_SL, profit=profit)
        del self.positions[ticket]

    def close_position_by_tp(self, ticket, *, close_price=None, profit=0.0):
        """Test helper: simula que el broker cerro la posicion por take profit."""
        pos = self.positions.get(ticket)
        if not pos:
            return
        price = close_price if close_price is not None else pos.get('tp', self.price)
        self._record_deal(ticket, entry=1, price=price, reason=DEAL_REASON_TP, profit=profit)
        del self.positions[ticket]
```

- [ ] **Step 4: Run all simulator + existing trade_manager tests to verify nothing broke**

Run: `pytest tests/test_simulador_mt5.py services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS — all pre-existing tests still pass (defaults preserve behavior), plus the 4 new tests from Step 1.

- [ ] **Step 5: Commit**

```bash
git add tests/test_simulador_mt5.py
git commit -m "test(simulador_mt5): model deal.reason/profit/volume/commission/swap

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 8: Replace `_closed_at_tp1` heuristic with `deal.reason`-based detection + new automatic events

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py:570-599` (`_closed_at_tp1`), `:649-668` (`_get_close_price`), `:513-568` (`_tick_once_account`)
- Modify: `services/trade_orchestrator/test_trade_manager_dual_tp.py` (add new tests; keep existing ones passing)

**Interfaces:**
- Consumes: `SimuladorMT5.close_position_by_sl`/`close_position_by_tp`/`close_position_directly` (Task 7), `channel_names.resolve_channel_name` (Task 5), `event_messages.build_sl_hit_message`/`build_external_close_message` (Task 6). `self.event_bus` and `self.channel_names` don't exist on `TradeManager` yet at this point in the plan — they're added in Task 9 — so this task's tests construct `TradeManager` the same way existing tests do (`notifier=DummyNotifier()`, no `event_bus`) and rely on the placeholder `_channel_names()` method added below (returning `{}` via `getattr`, since `self.channel_names` isn't a real attribute until Task 9).
- Produces: `_get_close_deal_info(self, client, ticket: int) -> Optional[dict]` returning `{"price": float, "reason": int, "profit": float, "volume": float, "commission": float, "swap": float}` or `None` (same failure semantics as `_get_close_price`, which becomes a thin wrapper: `_get_close_price` now calls `_get_close_deal_info` and returns `result["price"] if result else None` — kept for the handful of existing call sites that only need price). `_closed_at_tp1` is removed; its call sites in `_tick_once_account` are replaced by a new `_classify_leg_closure(self, client, closed_trade) -> dict` returning `{"cause": "tp1"|"tp2"|"sl"|"external"|"unknown", **deal_info}` using `reason` directly (`DEAL_REASON_TP == 5` → `"tp1"` only for `leg == "tp1"`; `DEAL_REASON_SL == 4` → `"sl"`; anything else → `"external"` since synchronous closes from `close_now`/`close_partial_now` already removed the ticket before this code runs, per spec §5's explicit note).

- [ ] **Step 1: Write the failing tests**

Add to `services/trade_orchestrator/test_trade_manager_dual_tp.py`:

```python
@pytest.mark.asyncio
async def test_tp1_leg_closed_by_real_tp_reason_triggers_tp1_hit():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")

    sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=20.0)
    await tm._tick_once_account(ACCOUNT)

    events = [event for event, kwargs in tm.notifier.events if event == "tp1_hit"]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_tp1_leg_closed_by_sl_reason_does_not_trigger_tp1_hit():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")

    sim.close_position_by_sl(tp1_leg.ticket, close_price=2490.0, profit=-20.0)
    await tm._tick_once_account(ACCOUNT)

    tp1_events = [event for event, kwargs in tm.notifier.events if event == "tp1_hit"]
    sl_events = [kwargs for event, kwargs in tm.notifier.events if event == "sl_hit_detected"]
    assert len(tp1_events) == 0
    assert len(sl_events) == 1
    assert sl_events[0]["pnl_money"] == -20.0


@pytest.mark.asyncio
async def test_runner_closed_by_sl_reason_triggers_sl_hit_detected():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")

    sim.close_position_by_sl(runner.ticket, close_price=2495.0, profit=-5.0)
    await tm._tick_once_account(ACCOUNT)

    sl_events = [kwargs for event, kwargs in tm.notifier.events if event == "sl_hit_detected"]
    assert len(sl_events) == 1
    assert sl_events[0]["leg"] == "runner"


@pytest.mark.asyncio
async def test_leg_closed_by_unknown_external_cause_triggers_external_close_detected():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")

    # Closed directly in MT5 by the user, outside the system -- not via
    # close_now/close_partial_now (which would have removed the ticket from
    # tm.trades synchronously before this tick ever ran).
    sim.close_position_directly(runner.ticket, close_price=2505.0)
    await tm._tick_once_account(ACCOUNT)

    external_events = [kwargs for event, kwargs in tm.notifier.events if event == "external_close_detected"]
    assert len(external_events) == 1


@pytest.mark.asyncio
async def test_close_now_does_not_trigger_external_close_detected():
    """Real bug class this guards against: apply_mgmt_action's close_now
    removes the ticket from tm.trades synchronously (trade_manager.py:908)
    before any _tick_once_account runs, so the passive detection loop must
    never see that ticket as 'closed' -- confirming no spurious
    external_close_detected fires for a close the system itself ordered."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="cierra todo", correction=None)
    await tm._tick_once_account(ACCOUNT)

    external_events = [event for event, kwargs in tm.notifier.events if event == "external_close_detected"]
    assert len(external_events) == 0
```

Note: `test_close_now_does_not_trigger_external_close_detected` requires `open_group` to have been called with a `chat_id` matching `CHAT_ID` for `apply_mgmt_action` to find the group — check the existing `apply_mgmt_action` tests in this file for how `CHAT_ID` and `open_group(..., chat_id=...)` are wired together, and match that pattern exactly (adjust the `open_group` call above to pass `chat_id=CHAT_ID` if the existing convention requires it explicitly rather than defaulting).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "reason or external_close or sl_hit_detected" -v`
Expected: FAIL — `sl_hit_detected`/`external_close_detected` events never fire yet (old code always either fires `tp1_hit` or the generic `runner_closed`/`tp1_leg_closed_not_at_tp1`).

- [ ] **Step 3: Implement `_get_close_deal_info` and `_classify_leg_closure`, rewire `_tick_once_account`**

Replace `_get_close_price` (trade_manager.py:649-668) with:

```python
    async def _get_close_deal_info(self, client, ticket: int) -> Optional[dict]:
        """
        Busca el deal real de salida (DEAL_ENTRY_OUT=1) de `ticket` en el
        historial de MT5, con toda la informacion necesaria para
        auditoria/notificacion: precio, causa (reason), P&L real, volumen
        cerrado, comision y swap. Nunca debe tumbar el flujo de notificacion
        -- cualquier fallo (de red, o el deal aun no propago) devuelve None.
        """
        try:
            deals = await self._call(client.history_deals_get, position=ticket)
        except Exception as e:
            log.warning("[TM] fallo obteniendo historial de deals para ticket=%s: %s", ticket, e)
            return None
        if not deals:
            return None
        out_deals = [d for d in deals if getattr(d, "entry", None) == 1]
        if not out_deals:
            return None
        closing = max(out_deals, key=lambda d: getattr(d, "time", 0))
        return {
            "price": float(closing.price),
            "reason": getattr(closing, "reason", None),
            "profit": float(getattr(closing, "profit", 0.0) or 0.0),
            "volume": float(getattr(closing, "volume", 0.0) or 0.0),
            "commission": float(getattr(closing, "commission", 0.0) or 0.0),
            "swap": float(getattr(closing, "swap", 0.0) or 0.0),
        }

    async def _get_close_price(self, client, ticket: int) -> Optional[float]:
        """Compat: varios call sites solo necesitan el precio de cierre."""
        info = await self._get_close_deal_info(client, ticket)
        return info["price"] if info else None
```

Remove `_closed_at_tp1` entirely (lines 570-599) and replace with:

```python
    DEAL_REASON_TP = 5
    DEAL_REASON_SL = 4

    async def _classify_leg_closure(self, client, closed_trade: "ManagedTrade") -> dict:
        """
        Clasifica por que se cerro una pierna que desaparecio de
        positions_get, usando deal.reason directamente (no heuristica de
        tolerancia de precio -- ver spec 2026-09-10, seccion 5). Los
        cierres que el propio TradeManager origina de forma sincrona
        (close_now, close_partial_now, TP2 partial) ya remueven el ticket
        de self.trades ANTES de que este metodo se llame -- por eso
        cualquier otra causa detectada aqui (fuera de TP genuino y SL) se
        trata como 'external': nadie del sistema lo pidio.
        """
        info = await self._get_close_deal_info(client, closed_trade.ticket)
        if info is None:
            # Deal aun no propago o fallo de red -- comportamiento previo:
            # asumir TP1 para no bloquear el BE automatico en el caso comun.
            return {"cause": "tp1" if closed_trade.leg == "tp1" else "unknown", "price": None,
                    "reason": None, "profit": None, "volume": None, "commission": None, "swap": None}
        reason = info["reason"]
        if reason == self.DEAL_REASON_TP and closed_trade.leg == "tp1":
            cause = "tp1"
        elif reason == self.DEAL_REASON_SL:
            cause = "sl"
        elif reason is not None:
            # A real deal was found with a real reason that's neither TP nor
            # SL (typically DEAL_REASON_CLIENT) -- since close_now/
            # close_partial_now/TP2-partial already remove the ticket from
            # self.trades synchronously before this code ever runs (see the
            # module docstring note above), this really is an outside close.
            cause = "external"
        else:
            # reason is None: the deal was found but MT5 didn't report a
            # reason (or the simulator/test double left it unset). Too
            # uncertain to raise a security alert over -- fall back to the
            # old undifferentiated audit-only event instead of guessing.
            cause = "unknown"
        return {"cause": cause, **info}
```

Update `_tick_once_account` (trade_manager.py:513-568), replacing lines 538-549:

```python
                classification = await self._classify_leg_closure(client, closed_trade)
                cause = classification["cause"]
                if cause == "tp1":
                    await self._on_tp1_leg_closed(account, client, closed_trade)
                elif cause == "sl":
                    channel_name = resolve_channel_name(closed_trade.chat_id, self._channel_names())
                    message = build_sl_hit_message(
                        channel_name=channel_name, group_id=closed_trade.group_id, symbol=closed_trade.symbol,
                        direction=closed_trade.direction, close_price=classification["price"],
                        close_volume=classification["volume"], pnl_money=classification["profit"],
                    )
                    await self._notify(
                        "sl_hit_detected", channel="both", group_id=closed_trade.group_id, chat_id=closed_trade.chat_id,
                        channel_name=channel_name, symbol=closed_trade.symbol, direction=closed_trade.direction,
                        leg=closed_trade.leg, close_price=classification["price"], close_volume=classification["volume"],
                        pnl_money=classification["profit"], message=message,
                    )
                else:
                    channel_name = resolve_channel_name(closed_trade.chat_id, self._channel_names())
                    if cause == "external":
                        message = build_external_close_message(
                            channel_name=channel_name, group_id=closed_trade.group_id, symbol=closed_trade.symbol,
                            direction=closed_trade.direction, leg=closed_trade.leg,
                            close_price=classification["price"], close_volume=classification["volume"],
                            pnl_money=classification["profit"],
                        )
                        await self._notify(
                            "external_close_detected", channel="both", group_id=closed_trade.group_id,
                            chat_id=closed_trade.chat_id, channel_name=channel_name, symbol=closed_trade.symbol,
                            direction=closed_trade.direction, leg=closed_trade.leg,
                            close_price=classification["price"], close_volume=classification["volume"],
                            pnl_money=classification["profit"], message=message,
                        )
                    else:
                        leg_label = "Runner" if closed_trade.leg == "runner" else "tp1_leg"
                        await self._notify(
                            "runner_closed" if closed_trade.leg == "runner" else "tp1_leg_closed_not_at_tp1",
                            channel="audit", group_id=closed_trade.group_id, ticket=ticket, symbol=closed_trade.symbol,
                            message=f"{leg_label} del grupo {closed_trade.group_id} ({closed_trade.symbol}, ticket={ticket}) "
                                    f"se cerro sin poder determinar la causa. Precio de apertura "
                                    f"{self._fmt_price(closed_trade.entry_price)}.",
                        )
```

Add the two new imports at the top of `trade_manager.py`:

```python
from .channel_names import resolve_channel_name
from .event_messages import build_sl_hit_message, build_external_close_message
```

Add a placeholder `_channel_names(self) -> dict` method on `TradeManager` for now (real wiring happens in Task 10):

```python
    def _channel_names(self) -> dict:
        return getattr(self, "channel_names", {}) or {}
```

**Note for the implementer:** `_notify` at this point in the plan still has its Task-4-era signature (`event, **kwargs`, no explicit `channel` param) — the calls above pass `channel="both"`/`channel="audit"` as a kwarg that `_notify` doesn't yet understand. This is intentional: Task 9 is where `_notify` itself is rewritten to read `channel` out of kwargs and call `EventBus.emit`. Until Task 9 lands, these new call sites will pass `channel` through to the old `notifier.notify_trade_event(event, **kwargs)` as a harmless extra kwarg (the `DummyNotifier` in tests just stores `**kwargs`, so tests here still pass). Do not skip ahead — keep this task focused only on classification correctness.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS — all previously-passing tests still pass, plus the 5 new tests from Step 1.

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "fix(trade_orchestrator): classify leg closures via deal.reason instead of price-tolerance heuristic

Adds automatic sl_hit_detected and external_close_detected events.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 9: Migrate `_notify` to `EventBus`, enrich existing events with new payload fields

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py` (`__init__`, `_notify`, and every existing `_notify(...)` call site listed in spec §6's table)
- Modify: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Consumes: `EventBus.emit` (Task 4).
- Produces: `TradeManager.__init__` gains `event_bus: Optional[EventBus] = None` parameter (kept optional, alongside the existing `notifier`, for a transition period — but `_notify` now calls `event_bus.emit` when present instead of `notifier.notify_trade_event`; if `event_bus` is `None`, falls back to the old `notifier` path unchanged, so existing tests that only pass `notifier=DummyNotifier()` keep working without modification). Every `_notify` call site gains an explicit `channel` kwarg (default `"audit"` if omitted, matching spec §6 for the internal/noisy events) and the new `payload` fields spec §6 lists (`channel_name`, `entry_price`, `volume`, `close_price`, `close_volume`, `pnl_money`, etc., using `_get_close_deal_info` from Task 8 wherever a close is involved).

- [ ] **Step 1: Write the failing tests**

Add to `services/trade_orchestrator/test_trade_manager_dual_tp.py`:

```python
import os
import tempfile
import json


@pytest.mark.asyncio
async def test_notify_uses_event_bus_when_configured():
    from services.trade_orchestrator.event_bus import EventBus

    with tempfile.TemporaryDirectory() as d:
        audit_path = os.path.join(d, "audit_log.jsonl")
        bus = EventBus(audit_path, redis_client=None)
        sim = SimuladorMT5()
        sim.price = 2500.0
        tm = TradeManager(DummyExecutor(sim), event_bus=bus)

        await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

        with open(audit_path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f.readlines()]
        opened = [l for l in lines if l["event_type"] == "group_opened"]
        assert len(opened) == 1
        assert opened[0]["channel"] == "both"
        assert "entry_price" in opened[0]["payload"]
        assert "volume" in opened[0]["payload"]


@pytest.mark.asyncio
async def test_group_opened_message_is_telegram_ready_text():
    from services.trade_orchestrator.event_bus import EventBus

    with tempfile.TemporaryDirectory() as d:
        audit_path = os.path.join(d, "audit_log.jsonl")
        bus = EventBus(audit_path, redis_client=None)
        sim = SimuladorMT5()
        sim.price = 2500.0
        tm = TradeManager(DummyExecutor(sim), event_bus=bus)

        await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

        with open(audit_path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f.readlines()]
        opened = next(l for l in lines if l["event_type"] == "group_opened")
        assert "APERTURA" in opened["message"]
        assert "XAUUSD" in opened["message"]


@pytest.mark.asyncio
async def test_tp1_hit_event_includes_money_pnl_in_payload():
    from services.trade_orchestrator.event_bus import EventBus

    with tempfile.TemporaryDirectory() as d:
        audit_path = os.path.join(d, "audit_log.jsonl")
        bus = EventBus(audit_path, redis_client=None)
        sim = SimuladorMT5()
        sim.price = 2500.0
        tm = TradeManager(DummyExecutor(sim), event_bus=bus)
        group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
        tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")

        sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=20.0)
        await tm._tick_once_account(ACCOUNT)

        with open(audit_path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f.readlines()]
        tp1_hit = next(l for l in lines if l["event_type"] == "tp1_hit")
        assert tp1_hit["payload"]["pnl_money"] == 20.0
        assert tp1_hit["channel"] == "both"


@pytest.mark.asyncio
async def test_notify_falls_back_to_old_notifier_when_no_event_bus_configured():
    """Backward-compat: existing tests across the suite construct TradeManager
    with only notifier=DummyNotifier() (no event_bus) and must keep working
    unmodified."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    events = [event for event, kwargs in tm.notifier.events if event == "group_opened"]
    assert len(events) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "event_bus or telegram_ready or money_pnl" -v`
Expected: FAIL — `TradeManager.__init__` doesn't accept `event_bus` yet; `_notify` doesn't write payload fields yet.

- [ ] **Step 3: Rewrite `_notify` and `__init__`, enrich call sites**

Update `__init__` (trade_manager.py:39-45):

```python
    def __init__(self, mt5_executor, *, notifier=None, event_bus=None, config_provider=None, state_store=None, channel_names=None):
        self.mt5 = mt5_executor
        self.notifier = notifier
        self.event_bus = event_bus
        self.config_provider = config_provider
        self.state_store = state_store
        self.channel_names = channel_names or {}
        self.trades: dict[int, ManagedTrade] = {}
        self._next_group_id = 1

    def _channel_names(self) -> dict:
        return self.channel_names
```

(This replaces the placeholder `_channel_names` stub added in Task 8.)

Rewrite `_notify` (trade_manager.py:57-64):

```python
    async def _notify(self, event: str, *, channel: str = "audit", **kwargs) -> None:
        log.info("[TM][EVENT] %s %s", event, kwargs)
        message = kwargs.pop("message", "")
        if self.event_bus is not None:
            try:
                await self.event_bus.emit(event, channel, message, kwargs)
            except Exception as e:
                log.warning("[TM] event_bus.emit failed for event=%s: %s", event, e)
            return
        if not self.notifier:
            return
        try:
            await self.notifier.notify_trade_event(event, message=message, **kwargs)
        except Exception as e:
            log.warning("[TM] notify failed for event=%s: %s", event, e)
```

Update the `group_opened` call site (trade_manager.py:352-357) to pass `channel="both"` and the new payload fields (`entry_price`, `volume`, `chat_id`, `channel_name`) and use the message builder from Task 6:

```python
        channel_name = resolve_channel_name(chat_id, self._channel_names())
        message = build_group_opened_message(
            channel_name=channel_name, group_id=group_id, symbol=symbol, direction=direction,
            entry_price=price, sl=sl, tp1=tp1, tp2=tp2, volume=account.get("fixed_lot", 0.01),
        )
        await self._notify(
            "group_opened", channel="both", group_id=group_id, symbol=symbol, direction=direction,
            tp1_ticket=tickets["tp1"], runner_ticket=tickets["runner"], sl=sl, tp1=tp1, tp2=tp2,
            chat_id=chat_id, channel_name=channel_name, entry_price=price, volume=account.get("fixed_lot", 0.01),
            message=message,
        )
```

(`price` here is the variable already in scope from `_get_price_with_retry`/`_wait_for_entry_range` earlier in `open_group` — verify the exact local variable name at the call site before editing; it's the entry price used to place the order.)

Add the import at the top of `trade_manager.py`:

```python
from .event_messages import build_group_opened_message, build_sl_hit_message, build_external_close_message
```

(Consolidates with the imports added in Task 8 — don't duplicate the import line, merge them.)

`_on_tp1_leg_closed` (trade_manager.py:601-647) already has `runner` and `client` in scope, and already calls `self._get_close_price(client, tp1_leg.ticket)` right before its `tp1_hit` notify (inside the `if ok:` branch, since only a successful BE reports `tp1_hit` — a failed BE reports `tp1_hit_be_failed` instead, in the `else:` branch). Replace that single line and the notify call with:

```python
            deal_info = await self._get_close_deal_info(client, tp1_leg.ticket)
            channel_name = resolve_channel_name(tp1_leg.chat_id, self._channel_names())
            close_price = deal_info["price"] if deal_info else None
            pnl_money = deal_info["profit"] if deal_info else None
            close_volume = deal_info["volume"] if deal_info else None
            message = build_tp1_hit_message(
                channel_name=channel_name, group_id=tp1_leg.group_id, symbol=tp1_leg.symbol, direction=tp1_leg.direction,
                close_price=close_price, close_volume=close_volume, pnl_money=pnl_money, account_currency="USD",
            )
            await self._notify(
                "tp1_hit", channel="both", group_id=tp1_leg.group_id, symbol=tp1_leg.symbol,
                runner_ticket=runner.ticket, chat_id=tp1_leg.chat_id, channel_name=channel_name,
                close_price=close_price, close_volume=close_volume, pnl_money=pnl_money,
                message=message,
            )
```

This replaces the existing `close_price = await self._get_close_price(client, tp1_leg.ticket)` line and its following `await self._notify("tp1_hit", ...)` call verbatim — everything else in `_on_tp1_leg_closed` (the `TP1_HITS.inc()`, the `runner.be_applied`/`runner.planned_sl` updates, `_persist_group`, and the entire `else:` branch handling `tp1_hit_be_failed`) stays untouched. `account_currency="USD"` is a hardcoded placeholder for now — pulling the real account currency from MT5 (`account_info().currency`) is a natural follow-up but not in the spec, so it's out of scope for this plan.

Apply the same enrichment pattern (channel, channel_name, chat_id, plus any close-price/pnl fields the event's row in spec §6 lists) to the remaining existing call sites: `tp2_partial_closed` (~line 744-748, add `channel="both"`, get `pnl_money`/`close_volume` via `_get_close_deal_info`), `mgmt_close_now` (~line 918-922, add `channel="both"`, build `leg_results` list and `total_pnl_money` from each leg's `_get_close_deal_info`, use `build_close_now_message`), `mgmt_close_now_partial_failure` (~line 910-915, change to `channel="both"`, use `build_partial_failure_message`), `mgmt_move_sl_be_applied` (~line 980-984, add `channel="both"`, use `build_move_sl_be_applied_message`). Leave `mgmt_no_active_trade`, `mgmt_account_unresolved`, `mgmt_no_runner_leg`, `mgmt_move_sl_be_already_satisfied`, `mgmt_invalid_correction`, `mgmt_unknown_action`, `mgmt_note_sl_hit`, `open_aborted`, `open_failed`, `group_updated`, `tp1_hit_be_failed`, `reconciliation_summary` with their default `channel="audit"` (no `channel=` kwarg needed since that's the `_notify` default) — per spec §6 these stay audit-only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS — full existing suite plus the 4 new tests from Step 1.

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "feat(trade_orchestrator): migrate _notify to EventBus, enrich events with money P&L and channel names

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 10: `close_partial_now` action in `/mgmt/action`

**Files:**
- Modify: `services/trade_orchestrator/mgmt_api.py` (`MgmtActionRequest`)
- Modify: `services/trade_orchestrator/trade_manager.py` (`apply_mgmt_action`)
- Modify: `services/trade_orchestrator/test_trade_manager_dual_tp.py`

**Interfaces:**
- Consumes: `event_messages.build_close_partial_now_message`, `build_partial_failure_message` (Task 6).
- Produces: `MgmtActionRequest.percent: Optional[float] = None`; `apply_mgmt_action` gains a `close_partial_now` branch, applying `percent or 50.0` to every active leg of each resolved group via `client.partial_close(account, ticket, percent)`, emitting `mgmt_close_partial_now` (channel both, success) or `mgmt_close_partial_now_failure` (channel both, any leg rejected) per group — the group is NOT removed from the store (unlike `close_now`) since volume remains.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_close_partial_now_applies_default_50_percent_when_no_percent_given():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    legs_before = [t for t in tm.trades.values() if t.group_id == group_id]
    tickets_before = {t.ticket for t in legs_before}

    result = await tm.apply_mgmt_action(action="close_partial_now", chat_id=CHAT_ID, raw_text="cierra parte", correction=None)

    assert result["status"] == "completed"
    # Both legs still open (partial, not full close) -- tickets unchanged.
    remaining_tickets = {t.ticket for t in tm.trades.values() if t.group_id == group_id}
    assert remaining_tickets == tickets_before
    for ticket in tickets_before:
        pos = sim.positions[ticket]
        assert pos["volume"] == pytest.approx(0.01)  # 50% of the 0.02 default fixed_lot


@pytest.mark.asyncio
async def test_close_partial_now_applies_explicit_percent():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    legs = [t for t in tm.trades.values() if t.group_id == group_id]

    await tm.apply_mgmt_action(action="close_partial_now", chat_id=CHAT_ID, raw_text="cierra 30%", correction=None, percent=30.0)

    for t in legs:
        pos = sim.positions[t.ticket]
        assert pos["volume"] == pytest.approx(0.02 * 0.7)


@pytest.mark.asyncio
async def test_close_partial_now_notifies_success_event_with_both_channel():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    await tm.apply_mgmt_action(action="close_partial_now", chat_id=CHAT_ID, raw_text="cierra 30%", correction=None, percent=30.0)

    events = [(event, kwargs) for event, kwargs in tm.notifier.events if event == "mgmt_close_partial_now"]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_close_partial_now_reports_failure_when_broker_rejects_a_leg():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    original_partial_close = sim.partial_close
    def failing_partial_close(account, ticket, percent):
        return False
    sim.partial_close = failing_partial_close

    result = await tm.apply_mgmt_action(action="close_partial_now", chat_id=CHAT_ID, raw_text="cierra 30%", correction=None, percent=30.0)

    events = [event for event, kwargs in tm.notifier.events if event == "mgmt_close_partial_now_failure"]
    assert len(events) == 1
    assert result["results"][0]["status"] == "failed"
```

Note: verify `CHAT_ID` constant and the exact `open_group(..., chat_id=...)` call convention already used by existing `apply_mgmt_action` tests in this file (mentioned in the Task 8 exploration) — match it exactly rather than assuming the signature above is complete.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -k "close_partial_now" -v`
Expected: FAIL — `apply_mgmt_action` doesn't accept a `percent` kwarg yet and has no `close_partial_now` branch.

- [ ] **Step 3: Implement**

Update `MgmtActionRequest` in `mgmt_api.py`:

```python
class MgmtActionRequest(BaseModel):
    action: str
    chat_id: str
    raw_text: str
    correction: Optional[Correction] = None
    percent: Optional[float] = None
```

Update the `mgmt_action` endpoint function to pass `percent` through:

```python
        result = await trade_manager.apply_mgmt_action(
            action=req.action, chat_id=req.chat_id, raw_text=req.raw_text, correction=correction, percent=req.percent,
        )
```

Update `apply_mgmt_action`'s signature in `trade_manager.py`:

```python
    async def apply_mgmt_action(self, *, action: str, chat_id: str, raw_text: str, correction: Optional[dict], percent: Optional[float] = None) -> dict:
```

Add a new branch, placed after the `close_now` branch (after line 928's `return {"status": "completed", "results": results}`):

```python
        if action == "close_partial_now":
            effective_percent = percent if percent is not None else 50.0
            results = []
            for group_id in group_ids:
                try:
                    legs = [t for t in self.trades.values() if t.group_id == group_id]
                    account = self._ensure_account_dict(legs[0].account_name)
                    if not account:
                        log.error("[TM][MGMT] no se pudo resolver la cuenta para group_id=%s chat_id=%s", group_id, chat_id)
                        await self._notify(
                            "mgmt_account_unresolved",
                            message=f"No se pudo resolver la cuenta del grupo {group_id} al aplicar '{action}'.",
                            chat_id=chat_id, group_id=group_id, action=action,
                        )
                        results.append({"group_id": group_id, "status": "failed", "reason": "account_unresolved"})
                        continue
                    client = self.mt5._client_for(account)
                    channel_name = resolve_channel_name(chat_id, self._channel_names())
                    leg_results = []
                    any_leg_failed = False
                    leg_summaries = []
                    for t in list(legs):
                        ok = await self._call(client.partial_close, account, t.ticket, effective_percent)
                        if not ok:
                            any_leg_failed = True
                            leg_summaries.append(f"{t.leg} (ticket={t.ticket}, rechazado)")
                            log.error("[TM][MGMT] partial_close (parcial %.0f%%) rechazado | ticket=%s leg=%s group_id=%s",
                                      effective_percent, t.ticket, t.leg, group_id)
                            continue
                        deal_info = await self._get_close_deal_info(client, t.ticket)
                        leg_results.append({
                            "leg": t.leg,
                            "close_price": deal_info["price"] if deal_info else None,
                            "close_volume": deal_info["volume"] if deal_info else None,
                            "pnl_money": deal_info["profit"] if deal_info else None,
                        })
                    if any_leg_failed:
                        message = build_partial_failure_message(channel_name=channel_name, group_id=group_id, leg_summaries=leg_summaries)
                        await self._notify(
                            "mgmt_close_partial_now_failure", channel="both", group_id=group_id, chat_id=chat_id,
                            channel_name=channel_name, raw_text=raw_text, percent_requested=effective_percent,
                            leg_summaries=leg_summaries, message=message,
                        )
                        results.append({"group_id": group_id, "status": "failed", "reason": "partial_close_rejected"})
                        continue
                    message = build_close_partial_now_message(
                        channel_name=channel_name, group_id=group_id, raw_text=raw_text,
                        percent_requested=effective_percent, leg_results=leg_results,
                    )
                    await self._notify(
                        "mgmt_close_partial_now", channel="both", group_id=group_id, chat_id=chat_id,
                        channel_name=channel_name, raw_text=raw_text, percent_requested=effective_percent,
                        leg_results=leg_results, message=message,
                    )
                    results.append({"group_id": group_id, "status": "applied"})
                except Exception as e:
                    log.error("[TM][MGMT] excepcion en close_partial_now group_id=%s chat_id=%s: %s", group_id, chat_id, e)
                    results.append({"group_id": group_id, "status": "failed", "reason": "exception"})
            return {"status": "completed", "results": results}
```

Add the import at the top of `trade_manager.py` (merge with existing `event_messages` import from Tasks 8/9):

```python
from .event_messages import (
    build_group_opened_message, build_sl_hit_message, build_external_close_message,
    build_close_partial_now_message, build_partial_failure_message,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_trade_manager_dual_tp.py -v`
Expected: PASS — full suite plus the 4 new tests.

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/mgmt_api.py services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_trade_manager_dual_tp.py
git commit -m "feat(trade_orchestrator): add close_partial_now management action with arbitrary percent

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 11: Wire everything into `app.py` + config

**Files:**
- Modify: `services/trade_orchestrator/app.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `EventBus` (Task 4), `n8n_retry_worker.run_retry_worker` + `N8nEventClient` (Tasks 2-3), `channel_names.resolve_channel_name`'s `channel_names` dict shape (Task 5).
- Produces: nothing new consumed by later tasks — this is the final integration point.

- [ ] **Step 1: Write the failing test**

This task is integration wiring in `main()`, which isn't itself unit-testable in isolation the way prior tasks were (it's mostly object construction and `asyncio.gather` wiring). Add one small, focused unit test for the one piece of new *logic* introduced here — parsing `CHANNEL_NAMES_JSON`:

```python
# services/trade_orchestrator/test_app_channel_names_parsing.py
import json

from services.trade_orchestrator.app import parse_channel_names_json


def test_parse_channel_names_json_parses_valid_json():
    raw = json.dumps({"-1001234567890": "Oro Premium"})
    assert parse_channel_names_json(raw) == {"-1001234567890": "Oro Premium"}


def test_parse_channel_names_json_returns_empty_dict_for_blank_string():
    assert parse_channel_names_json("") == {}


def test_parse_channel_names_json_returns_empty_dict_and_logs_on_invalid_json():
    assert parse_channel_names_json("{not valid json") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/trade_orchestrator/test_app_channel_names_parsing.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_channel_names_json'`

- [ ] **Step 3: Implement `parse_channel_names_json` and wire up `main()`**

Add to `app.py` (near the top, after the logging setup, as a module-level function so it's importable for the test above):

```python
def parse_channel_names_json(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("CHANNEL_NAMES_JSON invalido, usando mapeo vacio: %s", e)
        return {}
```

Update `main()` — replace the notifier/TradeManager construction block (lines 123-146):

```python
    from services.trade_orchestrator.event_bus import EventBus
    from services.trade_orchestrator.n8n_event_client import N8nEventClient
    from services.trade_orchestrator.n8n_retry_worker import run_retry_worker

    audit_log_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "audit_log.jsonl")
    event_webhook_url = _config.get("N8N_EVENT_WEBHOOK_URL", "")
    event_bus = None
    n8n_event_client = None
    if event_webhook_url:
        n8n_event_client = N8nEventClient(event_webhook_url, token=_config.get("N8N_EVENT_WEBHOOK_TOKEN", ""))
        event_bus = EventBus(audit_log_path, redis_client=r)
        log.info("EventBus initialized (event_webhook_url=%s)", event_webhook_url)
    else:
        event_bus = EventBus(audit_log_path, redis_client=None)
        log.warning("N8N_EVENT_WEBHOOK_URL not configured — audit log still writes locally, n8n delivery disabled")

    channel_names = parse_channel_names_json(_config.get("CHANNEL_NAMES_JSON", ""))

    tradeExecutor = MT5Executor(
        accounts,
        magic=987654,
        notifier=None,
        trading_windows=s["trading_windows"],
        entry_wait_seconds=int(s["entry_wait_seconds"]),
        entry_poll_ms=int(s["entry_poll_ms"]),
        entry_buffer_points=float(s["entry_buffer_points"]),
        config_provider=_config,
    )
    state_store = TradeStateStore(r, os.path.join(os.path.dirname(__file__), "..", "..", "data", "trade_state.jsonl"))
    tradeManager = TradeManager(
        tradeExecutor, event_bus=event_bus, config_provider=_config, state_store=state_store,
        channel_names=channel_names,
    )
```

Note: `MT5Executor._notify_bg` (mt5_executor.py:34-39) does reference `self.notifier`, but `_notify_bg` itself is dead code — grep confirms nothing in the codebase calls it (not `TradeManager`, not any test). Passing `notifier=None` to `MT5Executor` here is therefore safe and doesn't remove any live behavior.

Add the retry worker task to the `asyncio.gather` at the bottom of `main()` (line 169-170):

```python
    asyncio.create_task(tradeManager.run_forever())
    tasks = [loop_signals(), uvicorn_server.serve()]
    if n8n_event_client is not None:
        tasks.append(run_retry_worker(r, n8n_event_client, audit_log_path))
    await asyncio.gather(*tasks)
```

Add to `.env.example`, in a new section after the existing `N8N_WEBHOOK_URL`/`N8N_WEBHOOK_TOKEN` block:

```
# --- Audit log + Telegram notifications (2026-09-10 design) ---
# Single webhook receiving the full event envelope (event_id, event_type,
# channel, timestamp, message, payload). n8n branches on `channel`
# ("audit" -> Data Table only, "both" -> Data Table + Telegram).
N8N_EVENT_WEBHOOK_URL=https://your-n8n-instance.example.com/webhook/events
N8N_EVENT_WEBHOOK_TOKEN=

# Simple chat_id -> human channel name mapping, JSON object. Falls back to
# the raw chat_id when a chat_id isn't listed here.
CHANNEL_NAMES_JSON={}
```

- [ ] **Step 4: Run test to verify it passes, then run the full trade_orchestrator suite**

Run: `pytest services/trade_orchestrator/test_app_channel_names_parsing.py -v`
Expected: PASS (3 tests)

Run: `pytest services/trade_orchestrator/ tests/test_simulador_mt5.py -v`
Expected: PASS — the entire suite, confirming nothing in the wiring change broke existing behavior.

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/app.py services/trade_orchestrator/test_app_channel_names_parsing.py .env.example
git commit -m "feat(trade_orchestrator): wire EventBus, n8n retry worker, and channel names into app startup

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 12: Verify `tests/test_orchestrator.py`'s import status and full-suite regression pass

**Files:**
- Read (and fix if broken): `tests/test_orchestrator.py`

**Interfaces:** none new — this is a verification/cleanup task the spec's §9 flagged explicitly.

- [ ] **Step 1: Check whether the file collects under pytest**

Run: `pytest tests/test_orchestrator.py --collect-only -v`
Expected: either it collects cleanly (memory's old note about a broken `NotifierAdapter` import was already stale per Task 8's exploration confirming the dual-TP rewrite changed this file's surroundings), or it fails with an `ImportError`/`ModuleNotFoundError`.

- [ ] **Step 2: If broken, fix the import; if clean, skip to Step 4**

If it fails importing a name like `NotifierAdapter` that no longer exists, open the file, find the actual current equivalent (`N8nNotifierAdapter` from `services/trade_orchestrator/notifications/n8n.py`, or — after this plan's Task 9 — potentially nothing, since `EventBus` replaces that role for new code) and update the import to match what the file's tests actually exercise. Do not guess blindly — read the file's test bodies first to see what they actually need from the import before choosing the fix.

- [ ] **Step 3: Re-run to confirm collection succeeds**

Run: `pytest tests/test_orchestrator.py -v`
Expected: PASS or a clean, meaningful failure (not an import error).

- [ ] **Step 4: Run the entire project test suite as a final regression check**

Run: `pytest -v`
Expected: PASS across the whole repo (or only pre-existing, unrelated failures documented separately — if any unrelated failure appears, note it, don't fix it as part of this plan's scope).

- [ ] **Step 5: Commit (only if Step 2 required a change)**

```bash
git add tests/test_orchestrator.py
git commit -m "fix(tests): repair broken import in test_orchestrator.py

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Post-implementation note (out of scope for this plan, needs the user's n8n instance)

Once this plan is merged and deployed, the n8n side still needs manual setup (explicitly out of scope per the spec, §2): a Webhook node at `N8N_EVENT_WEBHOOK_URL`, an IF/Switch node branching on the received JSON's top-level `channel` field (a sibling of `payload`, not nested inside it — see the envelope shape in spec §4 / Task 4), a Data Table insert for both branches, and a Telegram send node using the `message` field verbatim for the `"both"` branch.
