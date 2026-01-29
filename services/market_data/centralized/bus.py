# Bus centralizado de comandos/eventos para trading

import asyncio
import redis.asyncio as aioredis
import json
from .schema import TRADE_COMMANDS_STREAM, TRADE_EVENTS_STREAM

class TradeBus:
    def __init__(self, redis_url="redis://localhost:6379/0"):
        self.redis_url = redis_url
        self.redis = None

    async def connect(self):
        self.redis = aioredis.from_url(self.redis_url, decode_responses=False)

    async def close(self):
        self.redis.close()
        await self.redis.wait_closed()

    async def publish_command(self, command: dict):
        await self.redis.xadd(TRADE_COMMANDS_STREAM, {"data": json.dumps(command)})

    async def publish_event(self, event: dict):
        await self.redis.xadd(TRADE_EVENTS_STREAM, {"data": json.dumps(event)})

    async def listen_commands(self, last_id="$"):
        while True:
            streams = await self.redis.xread({TRADE_COMMANDS_STREAM: last_id}, block=1000)
            for stream, msgs in streams or []:
                for msg_id, msg in msgs:
                    yield msg_id, json.loads(msg[b"data"].decode())
                    last_id = msg_id

    async def listen_events(self, last_id="$"):
        while True:
            streams = await self.redis.xread({TRADE_EVENTS_STREAM: last_id}, block=1000)
            for stream, msgs in streams or []:
                for msg_id, msg in msgs:
                    yield msg_id, json.loads(msg[b"data"].decode())
                    last_id = msg_id

# Uso:
# bus = TradeBus()
# await bus.connect()
# await bus.publish_command({...})
# async for msg_id, cmd in bus.listen_commands(): ...
