"""
C1b (spec section 5, Familia C, new): the same fast signal sent twice, more
than REOPEN_COOLDOWN_SECONDS apart. Past that window, TradeManager.
group_age_seconds (commit afbeb18) makes app.py's handle_signal_fields treat
the repeat as a legitimate reopen rather than a duplicate -- it must open a
SECOND, independent group (BUY or SELL). This is the exact production
incident that motivated REOPEN_COOLDOWN_SECONDS: two real fast signals ~7
minutes apart were both silently dropped forever before this fix. Slower
than the rest of the suite (waits past the 300s default cooldown) --
accepted because it reproduces the real incident.
"""
import asyncio
import os

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import (
    _poll_until,
    _preexisting_tickets,
    _new_positions,
    OPEN_POLL_TIMEOUT_SECONDS,
    OPEN_POLL_INTERVAL_SECONDS,
)

SYMBOL = "XAUUSD"
SETTLE_AFTER_SECOND_SEND_SECONDS = 15


def _reopen_cooldown_seconds() -> float:
    return float(os.environ.get("REOPEN_COOLDOWN_SECONDS", 300))


async def run(ctx: ScenarioContext) -> ScenarioResult:
    preexisting_tickets = await _preexisting_tickets(ctx, SYMBOL)

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
        return positions if len(positions) >= 2 else None

    first_group_positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not first_group_positions:
        return ScenarioResult(
            name="c1b_reopen_after_cooldown", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="first fast signal did not open two legs",
        )

    try:
        # Wait past REOPEN_COOLDOWN_SECONDS with a margin, so the retest
        # isn't flaky against clock/latency skew between this container and
        # trade_orchestrator's own timing.
        wait_seconds = _reopen_cooldown_seconds() + 20
        await asyncio.sleep(wait_seconds)

        await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

        async def check_second_group_opened():
            positions = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
            return positions if len(positions) >= 4 else None

        all_positions = await _poll_until(check_second_group_opened, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
        if not all_positions:
            after_wait = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
            return ScenarioResult(
                name="c1b_reopen_after_cooldown", outcome=ScenarioOutcome.FAIL,
                evidence={"positions_after_wait": after_wait},
                detail="fast signal past REOPEN_COOLDOWN_SECONDS was still discarded as a duplicate "
                       "(the exact production bug this cooldown was built to fix)",
            )
        return ScenarioResult(
            name="c1b_reopen_after_cooldown", outcome=ScenarioOutcome.PASS,
            evidence={"positions": all_positions},
            detail="fast signal past the reopen cooldown correctly opened a second, independent group",
        )
    finally:
        await cleanup_group(ctx, SYMBOL, preexisting_tickets=preexisting_tickets)
