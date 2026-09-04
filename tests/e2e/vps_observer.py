"""
Reads observable state across the three layers the e2e suite verifies:
docker container logs, Redis streams (raw_messages, parsed_signals — there
is no Redis stream for management messages, see spec section 3.1), and
live MT5 positions via the same RPyC pattern as price_reader. Returns raw
data only — scenarios own the assertions.
"""
import subprocess
import rpyc


class VpsObserver:
    def __init__(self, redis_client, mt5_host: str, mt5_port: int):
        self.redis = redis_client
        self.mt5_host = mt5_host
        self.mt5_port = mt5_port

    async def read_raw_messages(self, count: int = 20) -> list[dict]:
        entries = await self.redis.xrange("raw_messages", "-", "+", count=count)
        return [fields for _msg_id, fields in entries]

    async def read_parsed_signals(self, count: int = 20) -> list[dict]:
        entries = await self.redis.xrange("parsed_signals", "-", "+", count=count)
        return [fields for _msg_id, fields in entries]

    def grep_container_logs(self, container: str, pattern: str, since: str = "5m") -> list[str]:
        result = subprocess.run(
            ["docker", "logs", "--since", since, container],
            capture_output=True, text=True, check=False,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        return [line for line in combined.splitlines() if pattern in line]

    async def positions_for_symbol(self, symbol: str) -> list[dict]:
        client = rpyc.connect(self.mt5_host, self.mt5_port)
        positions = client.root.positions_get(symbol=symbol) or []
        return [
            {"ticket": p.ticket, "sl": p.sl, "tp": p.tp, "volume": p.volume}
            for p in positions
        ]
