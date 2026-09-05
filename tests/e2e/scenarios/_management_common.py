"""
Shared setup for Family B scenarios (spec section 5, Familia B): open a
position via a fast signal and wait for both legs, so the management
message under test has something real to act on.
"""
from tests.e2e.scenarios.base import ScenarioContext
from tests.e2e.scenarios.a1_fast_only import _poll_until, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS

SYMBOL = "XAUUSD"


async def open_position_for_management_test(ctx: ScenarioContext) -> list[dict]:
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = await ctx.observer.positions_for_symbol(SYMBOL)
        return positions if len(positions) >= 2 else None

    return await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS) or []
