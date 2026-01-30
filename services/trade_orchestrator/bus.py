# Adaptador para consumir el bus centralizado de comandos/eventos

import asyncio
import redis.asyncio as aioredis
import json
from services.market_data.centralized.schema import TRADE_COMMANDS_STREAM, TRADE_EVENTS_STREAM

class TradeBus:
    def __init__(self, redis_url=None):
        import os
        # Usar REDIS_URL de entorno si no se pasa explícito
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://redis:6379/0")
        self.redis = None

    async def connect(self):
        # Usar la nueva API de redis-py (redis.asyncio)
        self.redis = aioredis.from_url(self.redis_url, decode_responses=False)

    async def close(self):
        self.redis.close()
        await self.redis.wait_closed()

    async def listen_commands(self, last_id="$"):
        while True:
            # Usar la API moderna: xread(streams: dict, block=ms)
            streams = await self.redis.xread({TRADE_COMMANDS_STREAM: last_id}, block=1000)
            for stream, msgs in streams or []:
                for msg_id, msg in msgs:
                    yield msg_id, json.loads(msg[b"data"].decode())
                    last_id = msg_id

    async def publish_event(self, event: dict):
        await self.redis.xadd(TRADE_EVENTS_STREAM, {"data": json.dumps(event)})
