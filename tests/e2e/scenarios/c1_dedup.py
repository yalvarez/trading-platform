"""
C1 (spec section 5, Familia C): the same fast signal sent twice in a row,
well within REOPEN_COOLDOWN_SECONDS (300s default). SignalDeduplicator
(services/common/signal_dedup.py) discards the second within
DEDUP_TTL_SECONDS, AND the active group is still younger than
REOPEN_COOLDOWN_SECONDS (TradeManager.group_age_seconds, commit afbeb18),
so app.py's handle_signal_fields treats it as a duplicate too -- no second
group should open. See c1b_reopen_after_cooldown.py for the opposite case.
"""
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import (
    _poll_until,
    _preexisting_tickets,
    _new_positions,
    OPEN_POLL_TIMEOUT_SECONDS,
    OPEN_POLL_INTERVAL_SECONDS,
)

SYMBOL = "XAUUSD"
BETWEEN_SENDS_SECONDS = 3  # must stay well under REOPEN_COOLDOWN_SECONDS (300s default)
SETTLE_SECONDS = 10


async def run(ctx: ScenarioContext) -> ScenarioResult:
    preexisting_tickets = await _preexisting_tickets(ctx, SYMBOL)

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
        return positions if len(positions) >= 2 else None

    positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not positions:
        return ScenarioResult(
            name="c1_dedup", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="first fast signal did not open two legs",
        )

    try:
        await asyncio.sleep(BETWEEN_SENDS_SECONDS)
        await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")
        await asyncio.sleep(SETTLE_SECONDS)

        positions_after = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
        if len(positions_after) != 2:
            return ScenarioResult(
                name="c1_dedup", outcome=ScenarioOutcome.FAIL,
                evidence={"positions_after": positions_after},
                detail=f"expected 2 positions (dedup held), found {len(positions_after)} — duplicate was not discarded",
            )
        return ScenarioResult(
            name="c1_dedup", outcome=ScenarioOutcome.PASS,
            evidence={"positions_after": positions_after},
            detail="second identical fast signal within dedup TTL and reopen cooldown correctly discarded",
        )
    finally:
        await cleanup_group(ctx, SYMBOL, preexisting_tickets=preexisting_tickets)
