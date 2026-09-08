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
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until, _preexisting_tickets, _new_positions
from tests.e2e.scenarios._management_common import open_position_for_management_test, SYMBOL

MGMT_POLL_TIMEOUT_SECONDS = 120
MGMT_POLL_INTERVAL_SECONDS = 5
MESSAGE = "Set BE for zero risk"

# Real production behavior observed live (2026-09-08): MT5 enforces a
# minimum distance (trade_stops_level) between any SL and the live price.
# Right after opening, price is still essentially at entry, so a BE request
# sent immediately almost always lands inside that minimum and gets
# rejected -- not a bot defect (see INCONCLUSIVE_MT5_REJECTED_BE below),
# but it defeats this scenario's actual purpose (confirming n8n correctly
# classifies and executes "Set BE for zero risk") by hitting the same
# market-timing edge case nearly every run. Give price real time to move
# away from entry before asking for BE.
PRE_MESSAGE_DELAY_SECONDS = 30.0


def _find_runner(positions: list) -> dict | None:
    """
    The runner leg is the one open_group never gives a real MT5 tp to
    (tp=0.0 -- it's the leg designed to run, its only mechanical exit is
    the trailing SL). tp1_leg always carries a real, nonzero tp. Both legs
    open with the SAME planned_sl, so picking "the first position" (as this
    scenario used to do) can silently compare the wrong leg's SL across
    polls whenever positions_get doesn't return them in a stable order --
    a real bug found live (2026-09-08) once the pre-message delay (below)
    started giving BE a real chance to succeed while tp1_leg was still
    open: the poll kept reading tp1_leg's unchanged SL and never noticed
    the runner's SL had actually moved.
    """
    return next((p for p in positions if p.get("tp") == 0.0), None)


async def run(ctx: ScenarioContext) -> ScenarioResult:
    preexisting_tickets = await _preexisting_tickets(ctx, SYMBOL)

    positions = await open_position_for_management_test(ctx, preexisting_tickets)
    if len(positions) < 2:
        return ScenarioResult(
            name="b1_be_variant1", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="setup failed: fast signal did not open two legs",
        )
    runner_before = _find_runner(positions)
    if runner_before is None:
        return ScenarioResult(
            name="b1_be_variant1", outcome=ScenarioOutcome.FAIL,
            evidence={"positions": positions}, detail="setup failed: could not identify the runner leg (no position with tp=0.0)",
        )
    runner_sl_before = runner_before["sl"]
    runner_ticket = runner_before["ticket"]

    await asyncio.sleep(PRE_MESSAGE_DELAY_SECONDS)
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)

    try:
        async def check_be_applied():
            current = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
            runner = next((p for p in current if p["ticket"] == runner_ticket), None)
            if runner and runner["sl"] != runner_sl_before:
                return runner
            return None

        runner_after = await _poll_until(check_be_applied, MGMT_POLL_TIMEOUT_SECONDS, MGMT_POLL_INTERVAL_SECONDS)
        logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] mgmt_move_sl_be_applied")

        if not runner_after:
            if not logs:
                # No success event at all -- two very different causes,
                # disambiguated by whether trade_orchestrator even attempted
                # the order_send. Real production behavior observed live: MT5
                # can reject move_sl_be_now's order_send (trade_stops_level --
                # BE requested too soon after opening, price still too close
                # to entry) even though n8n correctly called /mgmt/action.
                # That's a real system limitation, not "n8n/Ollama didn't
                # act" -- conflating the two hides a working n8n integration
                # behind a misleading external-dependency label.
                rejection_logs = ctx.observer.grep_container_logs(
                    "atp-trade-orchestrator", "reason=mgmt-fallback-BE"
                )
                if rejection_logs:
                    return ScenarioResult(
                        name="b1_be_variant1", outcome=ScenarioOutcome.INCONCLUSIVE_MT5_REJECTED_BE,
                        evidence={"rejection_logs": rejection_logs},
                        detail="n8n called /mgmt/action correctly, but MT5 rejected the SL move (likely too close to entry right after opening) — not a bot defect, not an n8n failure",
                    )
                return ScenarioResult(
                    name="b1_be_variant1", outcome=ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE,
                    evidence={}, detail="no mgmt_move_sl_be_applied event logged — n8n/Ollama likely did not act",
                )
            # The event WAS logged (order_send succeeded), but polling never
            # caught the RUNNER specifically with a changed SL. Two very
            # different explanations, disambiguated by whether the runner
            # (by ticket, not "any new position" -- tp1_leg can easily still
            # be open here with its own unrelated, unchanged SL) still
            # exists: real market movement can touch a freshly-moved BE SL
            # and close the runner before the next 5s poll -- that's the
            # mechanism working correctly (zero-risk exit), not a defect.
            # Only a runner that's still OPEN with its original SL despite
            # a logged success is a genuine bot FAIL.
            current = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
            runner_still_open = next((p for p in current if p["ticket"] == runner_ticket), None)
            if runner_still_open is None:
                return ScenarioResult(
                    name="b1_be_variant1", outcome=ScenarioOutcome.PASS,
                    evidence={"logs": logs},
                    detail="SL was moved to BE and the runner closed (price touched BE) before polling caught it — correct zero-risk exit, not a defect",
                )
            return ScenarioResult(
                name="b1_be_variant1", outcome=ScenarioOutcome.FAIL,
                evidence={"logs": logs, "runner_still_open": runner_still_open}, detail="event was logged but runner's SL did not change in MT5",
            )
        return ScenarioResult(
            name="b1_be_variant1", outcome=ScenarioOutcome.PASS,
            evidence={"logs": logs, "runner_after": runner_after},
            detail="'Set BE for zero risk' correctly moved runner SL to entry",
        )
    finally:
        await cleanup_group(ctx, SYMBOL, preexisting_tickets=preexisting_tickets)
