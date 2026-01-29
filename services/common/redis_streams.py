import redis.asyncio as redis
from typing import Any, Dict, Optional

import redis.asyncio as redis
from typing import Any, Dict, Optional

async def create_consumer_group(r: "redis.Redis", stream: str, group: str):
    import logging
    import asyncio
    log = logging.getLogger("redis_streams")
    retries = 5
    for attempt in range(1, retries + 1):
        try:
            await r.xgroup_create(stream, group, id='0', mkstream=True)
            log.info(f"[REDIS] Grupo '{group}' creado en stream '{stream}'")
            return
        except Exception as e:
            if 'BUSYGROUP' in str(e):
                log.info(f"[REDIS] Grupo '{group}' ya existe en stream '{stream}'")
                return  # Grupo ya existe
            log.warning(f"[REDIS] Intento {attempt}/{retries} - Error creando grupo '{group}' en stream '{stream}': {e}")
            if attempt < retries:
                await asyncio.sleep(0.5 * attempt)
            else:
                log.error(f"[REDIS] Fallo definitivo creando grupo '{group}' en stream '{stream}': {e}")
                raise

async def xreadgroup_loop(
    r: "redis.Redis",
    stream: str,
    group: str,
    consumer: str,
    block_ms: int = 200,
    count: int = 50,
    ): 
    while True:
        resp = await r.xreadgroup(group, consumer, {stream: '>'}, block=block_ms, count=count)
        if not resp:
            continue
        for _, msgs in resp:
            for msg_id, fields in msgs:
                yield msg_id, fields

async def xack(r: "redis.Redis", stream: str, group: str, msg_id: str):
    await r.xack(stream, group, msg_id)

import asyncio

class Streams:
    RAW = "raw_messages"
    SIGNALS = "parsed_signals"
    MGMT = "mgmt_messages"
    EVENTS = "trade_events"

async def redis_client(redis_url: str) -> "redis.Redis":
    r = redis.from_url(redis_url, decode_responses=True)
    await r.ping()
    return r

async def xadd(r: "redis.Redis", stream: str, data: Dict[str, Any]) -> str:
    return await r.xadd(stream, data, maxlen=10000, approximate=True)

async def xread_loop(
    r: "redis.Redis",
    stream: str,
    last_id: str = "$",
    block_ms: int = 200,
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
