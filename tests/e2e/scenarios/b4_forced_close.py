"""
B4 (spec section 5): "MARKET STRUCTURE SHIFTED! DON'T HOLD SELL. Close now"
-> action close_now (trade_manager.apply_mgmt_action) closes both legs.
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until
from tests.e2e.scenarios._management_common import open_position_for_management_test, SYMBOL

MGMT_POLL_TIMEOUT_SECONDS = 120
MGMT_POLL_INTERVAL_SECONDS = 5
MESSAGE = "MARKET STRUCTURE SHIFTED! DON'T HOLD SELL. Close now"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    positions = await open_position_for_management_test(ctx)
    if len(positions) < 2:
        return ScenarioResult(
            name="b4_forced_close", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)

    try:
        async def check_all_closed():
            current = await ctx.observer.positions_for_symbol(SYMBOL)
            # _poll_until treats a falsy return as "not done yet" — an empty
            # list is falsy in Python, so returning `current` itself here
            # would make a genuinely-closed position indistinguishable from
            # "keep polling" and this check could never succeed. Return a
            # truthy sentinel instead once the position list is empty.
            return True if len(current) == 0 else None

        closed = await _poll_until(check_all_closed, MGMT_POLL_TIMEOUT_SECONDS, MGMT_POLL_INTERVAL_SECONDS)
        logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] mgmt_close_now")

        if closed is None:
            if not logs:
                return ScenarioResult(
                    name="b4_forced_close", outcome=ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE,
                    evidence={}, detail="no mgmt_close_now event logged — n8n/Ollama likely did not act",
                )
            return ScenarioResult(
                name="b4_forced_close", outcome=ScenarioOutcome.FAIL,
                evidence={"logs": logs}, detail="event was logged but positions remain open in MT5",
            )
        return ScenarioResult(
            name="b4_forced_close", outcome=ScenarioOutcome.PASS,
            evidence={"logs": logs}, detail="forced-close message correctly closed both legs",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)  # no-op if already closed
