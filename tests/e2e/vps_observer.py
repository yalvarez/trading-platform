"""
Reads observable state across the three layers the e2e suite verifies:
docker container logs, Redis streams (raw_messages, parsed_signals — there
is no Redis stream for management messages, see spec section 3.1), and
live MT5 positions via the same RPyC pattern as price_reader. Returns raw
data only — scenarios own the assertions.
"""
import asyncio
import subprocess

from tests.e2e.mt5_client_factory import build_mt5_client


class VpsObserver:
    def __init__(self, redis_client, mt5_host: str, mt5_port: int):
        self.redis = redis_client
        self.mt5_host = mt5_host
        self.mt5_port = mt5_port

    async def read_raw_messages(self, count: int = 20) -> list[dict]:
        # XRANGE - + returns the OLDEST `count` entries in the whole stream —
        # on a stream that already has real production history (this is the
        # live raw_messages stream, not a fresh test-only one), a fast-moving
        # message just sent would never be among the first N ever recorded.
        # XREVRANGE + - reads newest-first instead, which is what every
        # caller actually wants: "did the message I just sent show up".
        entries = await self.redis.xrevrange("raw_messages", "+", "-", count=count)
        return [fields for _msg_id, fields in entries]

    async def read_parsed_signals(self, count: int = 20) -> list[dict]:
        entries = await self.redis.xrevrange("parsed_signals", "+", "-", count=count)
        return [fields for _msg_id, fields in entries]

    def grep_container_logs(self, container: str, pattern: str, since: str = "5m") -> list[str]:
        result = subprocess.run(
            ["docker", "logs", "--since", since, container],
            capture_output=True, text=True, check=False,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        return [line for line in combined.splitlines() if pattern in line]

    async def positions_for_symbol(self, symbol: str) -> list[dict]:
        client = build_mt5_client(self.mt5_host, self.mt5_port)
        positions = client.positions_get(symbol=symbol) or []
        return [
            {"ticket": p.ticket, "sl": p.sl, "tp": p.tp, "volume": p.volume}
            for p in positions
        ]

    async def restart_container(self, container: str, settle_seconds: float = 30) -> None:
        """
        Restarts a docker-compose service container by its container_name
        (e.g. "atp-trade-orchestrator") and waits settle_seconds for it to
        reconnect to Redis/mt5_acct1 and remount its volumes before the
        caller starts polling for post-restart state. Used by
        d1_restart_reconciliation to exercise TradeManager.reconcile_from_mt5
        against a real restart (spec section 5, Familia D).
        """
        result = subprocess.run(["docker", "restart", container], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"docker restart {container} failed (exit {result.returncode}): {result.stderr}"
            )
        await asyncio.sleep(settle_seconds)
