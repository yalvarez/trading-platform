"""B8 (spec section 5): promotional spam must produce zero effects — no
trade, no mgmt action."""
import asyncio
from datetime import datetime, timezone

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult
from tests.e2e.scenarios._management_common import MUTATING_EVENTS

QUIET_WINDOW_SECONDS = 60
MESSAGE = (
    "\U0001f680 JOIN OUR VIP POOL TRADING PROGRAM TODAY! \U0001f680\n"
    "Limited spots left — DM now to secure your spot and 10x your account!"
)


async def run(ctx: ScenarioContext) -> ScenarioResult:
    scenario_start_time = datetime.now(timezone.utc)

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)

    await asyncio.sleep(QUIET_WINDOW_SECONDS)

    positions = await ctx.observer.positions_for_symbol("XAUUSD")
    mutating_logs = [
        line for line in ctx.observer.grep_container_logs(
            "atp-trade-orchestrator", "[TM][EVENT]", since=scenario_start_time.isoformat()
        )
        if any(ev in line for ev in MUTATING_EVENTS)
    ]
    if positions or mutating_logs:
        return ScenarioResult(
            name="b8_spam_noop", outcome=ScenarioOutcome.FAIL,
            evidence={"positions": positions, "logs": mutating_logs},
            detail="promotional spam produced a trade or mgmt action (false positive)",
        )
    return ScenarioResult(
        name="b8_spam_noop", outcome=ScenarioOutcome.PASS,
        evidence={}, detail="promotional spam correctly produced no effects",
    )
