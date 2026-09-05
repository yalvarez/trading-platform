"""B8 (spec section 5): promotional spam must produce zero effects — no
trade, no mgmt action."""
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult

QUIET_WINDOW_SECONDS = 60
MESSAGE = (
    "\U0001f680 JOIN OUR VIP POOL TRADING PROGRAM TODAY! \U0001f680\n"
    "Limited spots left — DM now to secure your spot and 10x your account!"
)


async def run(ctx: ScenarioContext) -> ScenarioResult:
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)

    await asyncio.sleep(QUIET_WINDOW_SECONDS)

    positions = await ctx.observer.positions_for_symbol("XAUUSD")
    mutating_logs = [
        line for line in ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT]")
        if any(ev in line for ev in ("group_opened", "mgmt_close_now", "mgmt_move_sl_be_applied", "group_updated"))
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
