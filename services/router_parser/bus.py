import redis.asyncio as aioredis
import json

TRADE_COMMANDS_STREAM = "trade_commands"

class TradeBus:
    def __init__(self, redis_url="redis://localhost:6379/0"):
        self.redis_url = redis_url
        self.redis = None

    async def connect(self):
        self.redis = aioredis.from_url(self.redis_url, decode_responses=False)

    async def publish_command(self, command: dict):
        await self.redis.xadd(TRADE_COMMANDS_STREAM, {"data": json.dumps(command)})
