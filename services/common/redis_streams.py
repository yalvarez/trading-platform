import asyncio
import redis.asyncio as redis
from typing import Any, Dict, Optional

class Streams:
    RAW = "raw_messages"
    SIGNALS = "parsed_signals"
    MGMT = "mgmt_messages"
    EVENTS = "trade_events"

async def redis_client(redis_url: str) -> redis.Redis:
    r = redis.from_url(redis_url, decode_responses=True)
    await r.ping()
    return r

async def xadd(r: redis.Redis, stream: str, data: Dict[str, Any]) -> str:
    return await r.xadd(stream, data, maxlen=10000, approximate=True)

async def xread_loop(
    r: redis.Redis,
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
