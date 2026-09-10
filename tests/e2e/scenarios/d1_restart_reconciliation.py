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
from tests.e2e.scenarios.a1_fast_only import _poll_until, _preexisting_tickets, _new_positions
from tests.e2e.scenarios._management_common import open_position_for_management_test, SYMBOL
from tests.e2e.scenarios.b1_be_variant1 import MESSAGE as BE_MESSAGE, _find_runner

RESTART_SETTLE_SECONDS = 30
POST_RESTART_POLL_TIMEOUT_SECONDS = 60
POST_RESTART_POLL_INTERVAL_SECONDS = 5
CONTAINER_NAME = "atp-trade-orchestrator"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    preexisting_tickets = await _preexisting_tickets(ctx, SYMBOL)

    positions = await open_position_for_management_test(ctx, preexisting_tickets)
    if len(positions) < 2:
        return ScenarioResult(
            name="d1_restart_reconciliation", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )
    runner_at_setup = _find_runner(positions)
    if runner_at_setup is None:
        return ScenarioResult(
            name="d1_restart_reconciliation", outcome=ScenarioOutcome.FAIL,
            evidence={"positions": positions}, detail="setup failed: could not identify the runner leg (no position with tp=0.0)",
        )
    runner_ticket = runner_at_setup["ticket"]
    runner_sl_before_be = runner_at_setup["sl"]

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, BE_MESSAGE)

    try:
        async def check_be_applied():
            current = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
            runner = next((p for p in current if p["ticket"] == runner_ticket), None)
            if runner and runner["sl"] != runner_sl_before_be:
                return runner
            return None

        runner_before_restart = await _poll_until(check_be_applied, 120, 5)
        be_logs = ctx.observer.grep_container_logs(CONTAINER_NAME, "[TM][EVENT] mgmt_move_sl_be_applied")
        if not runner_before_restart:
            if not be_logs:
                # No success event at all -- disambiguate the same way B1
                # does: n8n can call /mgmt/action correctly and still have
                # MT5 reject the order_send (trade_stops_level -- BE
                # requested too soon after opening, price still too close
                # to entry). That's a real system limitation, not "n8n/
                # Ollama didn't act" -- conflating the two mislabels a
                # working n8n integration as EXTERNAL_DEPENDENCY_FAILURE.
                rejection_logs = ctx.observer.grep_container_logs(
                    CONTAINER_NAME, "reason=mgmt-fallback-BE"
                )
                if rejection_logs:
                    return ScenarioResult(
                        name="d1_restart_reconciliation", outcome=ScenarioOutcome.INCONCLUSIVE_MT5_REJECTED_BE,
                        evidence={"rejection_logs": rejection_logs},
                        detail="setup failed: n8n called /mgmt/action correctly, but MT5 rejected the SL move "
                               "(likely too close to entry right after opening) — not a bot defect, not an n8n failure",
                    )
                return ScenarioResult(
                    name="d1_restart_reconciliation", outcome=ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE,
                    evidence={}, detail="setup failed: no mgmt_move_sl_be_applied before restart — n8n/Ollama likely did not act",
                )
            # The event WAS logged (order_send succeeded), but the runner
            # closed (e.g. price touched the freshly-moved BE SL) before the
            # next 5s poll caught it with a changed SL -- same false-positive
            # pattern already fixed in B1. If the runner (by ticket) is gone,
            # that's the mechanism working correctly, not a defect worth
            # aborting D1's real purpose (the restart) over. Since there's no
            # position left to carry across the restart, this scenario can't
            # proceed -- report it as inconclusive rather than a bot FAIL.
            still_open = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
            runner_still_open = next((p for p in still_open if p["ticket"] == runner_ticket), None)
            if runner_still_open is None:
                return ScenarioResult(
                    name="d1_restart_reconciliation", outcome=ScenarioOutcome.INCONCLUSIVE_RUNNER_CLOSED_BEFORE_RESTART,
                    evidence={"be_logs": be_logs},
                    detail="BE was applied and the runner closed (price touched BE) before a restart could be exercised — "
                           "correct mechanism, but nothing left to test reconciliation against this run",
                )
            return ScenarioResult(
                name="d1_restart_reconciliation", outcome=ScenarioOutcome.FAIL,
                evidence={"be_logs": be_logs, "runner_still_open": runner_still_open},
                detail="setup failed: BE was logged but runner's SL did not change before restart",
            )

        sl_before_restart = runner_before_restart["sl"]

        await ctx.observer.restart_container(CONTAINER_NAME, settle_seconds=RESTART_SETTLE_SECONDS)

        async def check_single_position_unchanged():
            after = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
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
        await cleanup_group(ctx, SYMBOL, preexisting_tickets=preexisting_tickets)
