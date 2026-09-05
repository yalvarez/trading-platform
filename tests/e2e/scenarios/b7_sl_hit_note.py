"""
B7 (spec section 5, corrected): "HIT SL. GET READY FOR RECOVERY" maps to
the real note_sl_hit action (trade_manager.apply_mgmt_action) -- it DOES
log an event and notify, but must NOT change SL/TP or close the position.
Assert "no mutation", not "no action" (spec section 5 correction).
"""
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios._management_common import (
    open_position_for_management_test,
    SYMBOL,
    MUTATING_EVENTS_EXCLUDING_GROUP_OPENED,
)

QUIET_WINDOW_SECONDS = 60
MESSAGE = "HIT SL ❌. GET READY FOR RECOVERY \U0001f91d"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    positions_before = await open_position_for_management_test(ctx)
    if len(positions_before) < 2:
        return ScenarioResult(
            name="b7_sl_hit_note", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)
    await asyncio.sleep(QUIET_WINDOW_SECONDS)

    try:
        positions_after = await ctx.observer.positions_for_symbol(SYMBOL)
        mutating_logs = [
            line for line in ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT]")
            if any(ev in line for ev in MUTATING_EVENTS_EXCLUDING_GROUP_OPENED)
        ]
        if mutating_logs or positions_after != positions_before:
            return ScenarioResult(
                name="b7_sl_hit_note", outcome=ScenarioOutcome.FAIL,
                evidence={"logs": mutating_logs, "before": positions_before, "after": positions_after},
                detail="SL-hit/recovery message mutated the position — it should only be noted (note_sl_hit)",
            )
        return ScenarioResult(
            name="b7_sl_hit_note", outcome=ScenarioOutcome.PASS,
            evidence={"before": positions_before, "after": positions_after},
            detail="SL-hit/recovery message correctly left the position unmutated",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
