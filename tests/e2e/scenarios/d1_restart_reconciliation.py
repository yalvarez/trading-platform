"""
D1 (spec section 5, Familia D, new): open a group, force BE onto the runner
(reusing B1's "Set BE for zero risk" message rather than waiting on real
TP1 movement -- avoids stacking two non-deterministic market conditions),
then restart trade_orchestrator mid-run. TradeManager.reconcile_from_mt5
(wired in app.py's main(), see
docs/superpowers/specs/2026-09-04-trade-state-persistence-design.md) must
rebuild the runner's managed state from TradeStateStore (Redis primary,
JSONL file backup) before run_forever() starts ticking again -- this is
the exact production incident that motivated that subsystem: a restart
silently dropping management of an open group.
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until
from tests.e2e.scenarios._management_common import open_position_for_management_test, SYMBOL
from tests.e2e.scenarios.b1_be_variant1 import MESSAGE as BE_MESSAGE

RESTART_SETTLE_SECONDS = 30
POST_RESTART_POLL_TIMEOUT_SECONDS = 60
POST_RESTART_POLL_INTERVAL_SECONDS = 5
CONTAINER_NAME = "atp-trade-orchestrator"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    positions = await open_position_for_management_test(ctx)
    if len(positions) < 2:
        return ScenarioResult(
            name="d1_restart_reconciliation", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, BE_MESSAGE)

    try:
        async def check_be_applied():
            current = await ctx.observer.positions_for_symbol(SYMBOL)
            runner = next(iter(current), None)
            entry_sl = positions[0]["sl"]
            if runner and runner["sl"] != entry_sl:
                return runner
            return None

        runner_before_restart = await _poll_until(check_be_applied, 120, 5)
        be_logs = ctx.observer.grep_container_logs(CONTAINER_NAME, "[TM][EVENT] mgmt_move_sl_be_applied")
        if not runner_before_restart:
            if not be_logs:
                return ScenarioResult(
                    name="d1_restart_reconciliation", outcome=ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE,
                    evidence={}, detail="setup failed: no mgmt_move_sl_be_applied before restart — n8n/Ollama likely did not act",
                )
            return ScenarioResult(
                name="d1_restart_reconciliation", outcome=ScenarioOutcome.FAIL,
                evidence={"be_logs": be_logs}, detail="setup failed: BE was logged but SL did not change before restart",
            )

        sl_before_restart = runner_before_restart["sl"]

        await ctx.observer.restart_container(CONTAINER_NAME, settle_seconds=RESTART_SETTLE_SECONDS)

        async def check_single_position_unchanged():
            after = await ctx.observer.positions_for_symbol(SYMBOL)
            return after if after else None

        positions_after_restart = await _poll_until(
            check_single_position_unchanged, POST_RESTART_POLL_TIMEOUT_SECONDS, POST_RESTART_POLL_INTERVAL_SECONDS
        )
        reconcile_logs = ctx.observer.grep_container_logs(CONTAINER_NAME, "[RECONCILE] al arranque")

        if not positions_after_restart:
            return ScenarioResult(
                name="d1_restart_reconciliation", outcome=ScenarioOutcome.FAIL,
                evidence={"reconcile_logs": reconcile_logs},
                detail="position disappeared entirely after restart — reconciliation lost the group",
            )
        if len(positions_after_restart) != 1:
            return ScenarioResult(
                name="d1_restart_reconciliation", outcome=ScenarioOutcome.FAIL,
                evidence={"positions_after_restart": positions_after_restart, "reconcile_logs": reconcile_logs},
                detail=f"expected 1 position (the runner) after restart, found {len(positions_after_restart)} — "
                       "reconciliation likely duplicated the group instead of recognizing the existing one",
            )
        if positions_after_restart[0]["sl"] != sl_before_restart:
            return ScenarioResult(
                name="d1_restart_reconciliation", outcome=ScenarioOutcome.FAIL,
                evidence={"sl_before_restart": sl_before_restart, "sl_after_restart": positions_after_restart[0]["sl"],
                          "reconcile_logs": reconcile_logs},
                detail="runner's BE-applied SL was not preserved across the restart",
            )
        if not reconcile_logs:
            return ScenarioResult(
                name="d1_restart_reconciliation", outcome=ScenarioOutcome.FAIL,
                evidence={"positions_after_restart": positions_after_restart},
                detail="position and SL survived, but no [RECONCILE] log line was emitted — "
                       "reconcile_from_mt5 may not have run, or logging regressed",
            )
        return ScenarioResult(
            name="d1_restart_reconciliation", outcome=ScenarioOutcome.PASS,
            evidence={"positions_after_restart": positions_after_restart, "reconcile_logs": reconcile_logs},
            detail="trade_orchestrator restart correctly reconciled the BE-applied group with no duplication or state loss",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
