"""
Shared setup for Family B scenarios (spec section 5, Familia B): open a
position via a fast signal and wait for both legs, so the management
message under test has something real to act on.
"""
from tests.e2e.scenarios.base import ScenarioContext
from tests.e2e.scenarios.a1_fast_only import (
    _poll_until,
    _preexisting_tickets,
    _new_positions,
    OPEN_POLL_TIMEOUT_SECONDS,
    OPEN_POLL_INTERVAL_SECONDS,
)

SYMBOL = "XAUUSD"

# Named constants for every [TM][EVENT] substring a Family B/C "no mutation"
# scenario might filter on. Keeping the literal event-name strings here means
# a typo is a typo everywhere, not a silent divergence between files that
# would otherwise each maintain their own slightly different inline list.
EVENT_GROUP_OPENED = "group_opened"
EVENT_MGMT_CLOSE_NOW = "mgmt_close_now"
EVENT_MGMT_MOVE_SL_BE_APPLIED = "mgmt_move_sl_be_applied"
EVENT_GROUP_UPDATED = "group_updated"

# Full superset: every mutating event any of these scenarios cares about.
MUTATING_EVENTS = (
    EVENT_GROUP_OPENED,
    EVENT_MGMT_CLOSE_NOW,
    EVENT_MGMT_MOVE_SL_BE_APPLIED,
    EVENT_GROUP_UPDATED,
)

# Subset used by scenarios that already open their own position first (b6,
# b7): group_opened is expected/benign there, so it's excluded to avoid a
# false FAIL on the scenario's own setup step.
MUTATING_EVENTS_EXCLUDING_GROUP_OPENED = (
    EVENT_MGMT_CLOSE_NOW,
    EVENT_MGMT_MOVE_SL_BE_APPLIED,
    EVENT_GROUP_UPDATED,
)


async def open_position_for_management_test(ctx: ScenarioContext, preexisting_tickets: set) -> list[dict]:
    """
    `preexisting_tickets`: snapshot from `a1_fast_only._preexisting_tickets`,
    taken by the caller BEFORE calling this — this demo account is shared
    with real, unrelated live trading, so the two legs this opens must be
    told apart from anything already open. Returns only the NEW positions
    (the ones this call itself opened), never a pre-existing one.
    """
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
        return positions if len(positions) >= 2 else None

    return await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS) or []
