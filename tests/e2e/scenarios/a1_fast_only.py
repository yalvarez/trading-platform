"""
A1 (spec section 5, Familia A): "XAUUSD BUY NOW" with no follow-up full
signal. Opens with DEFAULT_SL_XAUUSD_PIPS/DEFAULT_TP_XAUUSD_PIPS. Verifies
two legs (tp1 + runner) open, then polls for TP1 closing the tp1 leg within
a timeout — reporting INCONCLUSIVE_TP1_NOT_REACHED (not FAIL) if the real
market never gets there in time (spec section 5/7 determinism note).
"""
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group

SYMBOL = "XAUUSD"
OPEN_POLL_TIMEOUT_SECONDS = 30
OPEN_POLL_INTERVAL_SECONDS = 2
TP1_POLL_TIMEOUT_SECONDS = 600
TP1_POLL_INTERVAL_SECONDS = 10


async def _poll_until(condition_fn, timeout_seconds: float, interval_seconds: float):
    elapsed = 0.0
    while elapsed < timeout_seconds:
        value = await condition_fn()
        if value:
            return value
        await asyncio.sleep(interval_seconds)
        elapsed += interval_seconds
    return None


async def _preexisting_tickets(ctx: ScenarioContext, symbol: str) -> set:
    """
    Snapshot of tickets already open for `symbol` BEFORE this scenario sends
    anything. This demo account is shared with real, unrelated live trading
    on this VPS — every scenario must take this snapshot first and use it to
    tell "a position this scenario opened" apart from "a position that was
    already there," both when detecting its own open/close and when cleaning
    up (see cleanup_group's `preexisting_tickets` parameter).
    """
    existing = await ctx.observer.positions_for_symbol(symbol)
    return {p["ticket"] for p in existing}


def _new_positions(positions: list, preexisting_tickets: set) -> list:
    return [p for p in positions if p["ticket"] not in preexisting_tickets]


async def run(ctx: ScenarioContext) -> ScenarioResult:
    preexisting_tickets = await _preexisting_tickets(ctx, SYMBOL)

    await ctx.price_reader.read_price(SYMBOL)  # sanity read; fast signal carries no price itself
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
        return positions if len(positions) >= 2 else None

    positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not positions:
        return ScenarioResult(
            name="a1_fast_only", outcome=ScenarioOutcome.FAIL,
            evidence={"positions": positions or []},
            detail="two legs (tp1+runner) did not appear within timeout after fast signal",
        )

    try:
        async def check_tp1_closed():
            remaining = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
            return remaining if len(remaining) == 1 else None

        remaining = await _poll_until(check_tp1_closed, TP1_POLL_TIMEOUT_SECONDS, TP1_POLL_INTERVAL_SECONDS)
        if not remaining:
            return ScenarioResult(
                name="a1_fast_only", outcome=ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED,
                evidence={"positions": positions},
                detail="opened correctly; TP1 not reached by real market within timeout",
            )
        return ScenarioResult(
            name="a1_fast_only", outcome=ScenarioOutcome.PASS,
            evidence={"positions_after_open": positions, "positions_after_tp1": remaining},
            detail="opened two legs, TP1 leg closed, runner remains under BE/trailing",
        )
    finally:
        await cleanup_group(ctx, SYMBOL, preexisting_tickets=preexisting_tickets)
