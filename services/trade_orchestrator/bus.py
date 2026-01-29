# Adaptador para consumir el bus centralizado de comandos/eventos
import asyncio
import aioredis
import json
from services.market_data.centralized.schema import TRADE_COMMANDS_STREAM, TRADE_EVENTS_STREAM

class TradeBus:
    def __init__(self, redis_url="redis://localhost:6379/0"):
        self.redis_url = redis_url
        self.redis = None

    async def connect(self):
        self.redis = await aioredis.create_redis_pool(self.redis_url)

    async def close(self):
        self.redis.close()
        await self.redis.wait_closed()

    async def listen_commands(self, last_id="$"):
        while True:
            streams = await self.redis.xread([TRADE_COMMANDS_STREAM], latest_ids=[last_id], timeout=1000)
            for stream, msgs in streams or []:
                for msg_id, msg in msgs:
                    yield msg_id, json.loads(msg[b"data"].decode())
                    last_id = msg_id

    async def publish_event(self, event: dict):
        await self.redis.xadd(TRADE_EVENTS_STREAM, {"data": json.dumps(event)})
