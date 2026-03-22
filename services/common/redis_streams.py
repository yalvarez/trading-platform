import asyncio
import logging
import redis.asyncio as redis
from typing import Any, Dict

log = logging.getLogger("redis_streams")


class Streams:
    RAW = "raw_messages"
    SIGNALS = "parsed_signals"
    MGMT = "mgmt_messages"
    EVENTS = "trade_events"


async def redis_client(redis_url: str) -> "redis.Redis":
    r = redis.from_url(redis_url, decode_responses=True)
    await r.ping()
    return r


async def create_consumer_group(r: "redis.Redis", stream: str, group: str) -> None:
    retries = 5
    for attempt in range(1, retries + 1):
        try:
            await r.xgroup_create(stream, group, id="0", mkstream=True)
            log.info("[REDIS] Grupo '%s' creado en stream '%s'", group, stream)
            return
        except Exception as e:
            if "BUSYGROUP" in str(e):
                log.info("[REDIS] Grupo '%s' ya existe en stream '%s'", group, stream)
                return
            log.warning(
                "[REDIS] Intento %d/%d - Error creando grupo '%s' en stream '%s': %s",
                attempt, retries, group, stream, e,
            )
            if attempt < retries:
                await asyncio.sleep(0.5 * attempt)
            else:
                log.error("[REDIS] Fallo definitivo creando grupo '%s' en stream '%s': %s", group, stream, e)
                raise


async def xadd(r: "redis.Redis", stream: str, data: Dict[str, Any]) -> str:
    return await r.xadd(stream, data, maxlen=10000, approximate=True)


async def xack(r: "redis.Redis", stream: str, group: str, msg_id: str) -> None:
    await r.xack(stream, group, msg_id)


async def xreadgroup_loop(
    r: "redis.Redis",
    stream: str,
    group: str,
    consumer: str,
    block_ms: int = 2000,
    count: int = 50,
):
    while True:
        resp = await r.xreadgroup(group, consumer, {stream: ">"}, block=block_ms, count=count)
        if not resp:
            continue
        for _, msgs in resp:
            for msg_id, fields in msgs:
                yield msg_id, fields


async def xread_loop(
    r: "redis.Redis",
    stream: str,
    last_id: str = "$",
    block_ms: int = 2000,
    count: int = 50,
):
    while True:
        resp = await r.xread({stream: last_id}, block=block_ms, count=count)
        if not resp:
            continue
        for _, msgs in resp:
            for msg_id, fields in msgs:
                last_id = msg_id
                yield msg_id, fields
