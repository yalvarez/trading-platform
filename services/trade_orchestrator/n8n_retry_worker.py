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
        try:
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

            delay = BACKOFF_SECONDS[attempt]
            next_attempt = attempt + 1
            due_at = time.time() + delay
            next_item = json.dumps({"envelope": envelope, "attempt": next_attempt}, ensure_ascii=False)
            await redis_client.zadd(_DELAYED_KEY, {next_item: due_at})
            log.warning("[N8N_RETRY] evento fallo, reintento %s en %ss event_id=%s", next_attempt, delay, envelope.get("event_id"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("[N8N_RETRY] error inesperado en el loop del worker, se continua en la siguiente iteracion: %s", e)
            await asyncio.sleep(poll_interval_seconds)
