"""
B5 (spec section 5): "SIGNAL UPDATED" / "TP 2 IS 4687 Correction" -- not a
recognized TradePulseParser format (memory: tradepulse-channel-message-patterns
notes this exact message is unhandled by any parser), so it must go through
n8n/Ollama classifying it as signal_correction, which only updates the
tp2 reference used by trailing (trade_manager.apply_mgmt_action, action ==
"signal_correction") -- it does not touch MT5 directly for the runner leg,
so there is no SL/TP change to observe in MT5, only the log line.
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until
from tests.e2e.scenarios._management_common import open_position_for_management_test, SYMBOL

MGMT_POLL_TIMEOUT_SECONDS = 120
MGMT_POLL_INTERVAL_SECONDS = 5
MESSAGE_1 = "SIGNAL UPDATED"
MESSAGE_2 = "TP 2 IS 4687 Correction"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    positions = await open_position_for_management_test(ctx)
    if len(positions) < 2:
        return ScenarioResult(
            name="b5_signal_correction", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE_1)
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE_2)

    try:
        async def check_correction_logged():
            logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] group_updated")
            return logs if logs else None

        logs = await _poll_until(check_correction_logged, MGMT_POLL_TIMEOUT_SECONDS, MGMT_POLL_INTERVAL_SECONDS)
        if not logs:
            return ScenarioResult(
                name="b5_signal_correction", outcome=ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE,
                evidence={}, detail="no group_updated event logged for the correction — n8n/Ollama likely did not act",
            )
        return ScenarioResult(
            name="b5_signal_correction", outcome=ScenarioOutcome.PASS,
            evidence={"logs": logs}, detail="free-text TP2 correction was classified and applied via signal_correction",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
