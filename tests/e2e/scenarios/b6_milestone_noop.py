"""
B6 (spec section 5): progress/milestone messages ("+240 PIPS SKYROCKETING",
"TP 1 DONE", "Road to TP ONE") must not trigger any mgmt action -- a false
positive here is the failure mode under test.
"""
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _preexisting_tickets
from tests.e2e.scenarios._management_common import (
    open_position_for_management_test,
    SYMBOL,
    MUTATING_EVENTS_EXCLUDING_GROUP_OPENED,
)

QUIET_WINDOW_SECONDS = 60
MESSAGES = ["+240 PIPS SKYROCKETING", "TP 1 DONE", "Road to TP ONE"]


async def run(ctx: ScenarioContext) -> ScenarioResult:
    preexisting_tickets = await _preexisting_tickets(ctx, SYMBOL)

    positions_before = await open_position_for_management_test(ctx, preexisting_tickets)
    if len(positions_before) < 2:
        return ScenarioResult(
            name="b6_milestone_noop", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )

    for text in MESSAGES:
        await ctx.sender.send(ctx.cfg.tg_test_chat_id, text)
    await asyncio.sleep(QUIET_WINDOW_SECONDS)

    try:
        positions_after = await ctx.observer.positions_for_symbol(SYMBOL)
        mutating_logs = [
            line for line in ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT]")
            if any(ev in line for ev in MUTATING_EVENTS_EXCLUDING_GROUP_OPENED)
        ]
        if mutating_logs or positions_after != positions_before:
            return ScenarioResult(
                name="b6_milestone_noop", outcome=ScenarioOutcome.FAIL,
                evidence={"logs": mutating_logs, "before": positions_before, "after": positions_after},
                detail="a milestone/progress message triggered a mutating mgmt action (false positive)",
            )
        return ScenarioResult(
            name="b6_milestone_noop", outcome=ScenarioOutcome.PASS,
            evidence={"before": positions_before, "after": positions_after},
            detail="milestone messages correctly produced no mgmt action",
        )
    finally:
        await cleanup_group(ctx, SYMBOL, preexisting_tickets=preexisting_tickets)
