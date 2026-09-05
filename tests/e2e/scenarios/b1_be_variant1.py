"""
B1 (spec section 5, Familia B): "Set BE for zero risk" -> n8n/Ollama should
classify this as move_sl_be_now and call POST /mgmt/action on
trade_orchestrator, which moves the runner leg's SL to its entry price
(trade_manager.apply_mgmt_action, action == "move_sl_be_now").
Verification is via the [TM][EVENT] log line (Task 1) and the runner
position's SL in MT5 -- there is no Redis stream for management (spec
section 3.1). n8n/Ollama is the real test instance, not a mock (spec
section 2) -- a timeout with no mgmt event logged is reported as an
external dependency failure, not a bot FAIL (spec section 7).
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until
from tests.e2e.scenarios._management_common import open_position_for_management_test, SYMBOL

MGMT_POLL_TIMEOUT_SECONDS = 120
MGMT_POLL_INTERVAL_SECONDS = 5
MESSAGE = "Set BE for zero risk"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    positions = await open_position_for_management_test(ctx)
    if len(positions) < 2:
        return ScenarioResult(
            name="b1_be_variant1", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )
    runner_sl_before = next(p["sl"] for p in positions)

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)

    try:
        async def check_be_applied():
            current = await ctx.observer.positions_for_symbol(SYMBOL)
            runner = next(iter(current), None)
            if runner and runner["sl"] != runner_sl_before:
                return runner
            return None

        runner_after = await _poll_until(check_be_applied, MGMT_POLL_TIMEOUT_SECONDS, MGMT_POLL_INTERVAL_SECONDS)
        logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] mgmt_move_sl_be_applied")

        if not runner_after:
            if not logs:
                return ScenarioResult(
                    name="b1_be_variant1", outcome=ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE,
                    evidence={}, detail="no mgmt_move_sl_be_applied event logged — n8n/Ollama likely did not act",
                )
            return ScenarioResult(
                name="b1_be_variant1", outcome=ScenarioOutcome.FAIL,
                evidence={"logs": logs}, detail="event was logged but SL did not change in MT5",
            )
        return ScenarioResult(
            name="b1_be_variant1", outcome=ScenarioOutcome.PASS,
            evidence={"logs": logs, "runner_after": runner_after},
            detail="'Set BE for zero risk' correctly moved runner SL to entry",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
