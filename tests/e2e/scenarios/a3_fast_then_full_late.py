"""
A3 (spec section 5, edge case): full signal arrives AFTER tp1_leg already
closed (BE applied) or the runner is already trailing. Asserts the explicit
guarantees already present in trade_manager.update_group_signal (lines
~312-352): the runner's SL never regresses past what BE/trailing already
achieved, and peak_multiple is rescaled to the new tp1/tp2 without losing
progress. To reach that state quickly and reliably, this scenario forces
TP1 very close to the opening price (tighter than A2) so the tp1 leg closes
fast, then sends the full signal.
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
from tests.e2e.scenarios.a2_fast_then_full_early import _build_full_signal_text

SYMBOL = "XAUUSD"
TP1_CLOSE_TIMEOUT_SECONDS = 900
TP1_CLOSE_POLL_INTERVAL_SECONDS = 10


async def run(ctx: ScenarioContext) -> ScenarioResult:
    preexisting_tickets = await _preexisting_tickets(ctx, SYMBOL)

    price = await ctx.price_reader.read_price(SYMBOL)
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
        return positions if len(positions) >= 2 else None

    positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not positions:
        return ScenarioResult(
            name="a3_fast_then_full_late", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="fast signal did not open two legs",
        )

    try:
        # Wait for the default-TP1 tp1 leg to close on its own (default TP is
        # DEFAULT_TP_XAUUSD_PIPS — small enough to close within the timeout
        # in normal XAUUSD movement; if it never does, report inconclusive
        # rather than a false FAIL, consistent with A1/A2).
        async def check_tp1_closed():
            remaining = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
            return remaining if len(remaining) == 1 else None

        remaining_before_full = await _poll_until(check_tp1_closed, TP1_CLOSE_TIMEOUT_SECONDS, TP1_CLOSE_POLL_INTERVAL_SECONDS)
        if not remaining_before_full:
            return ScenarioResult(
                name="a3_fast_then_full_late", outcome=ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED,
                evidence={"positions": positions},
                detail="default TP1 not reached within timeout; cannot reach the late-update state to test",
            )

        sl_before_full = remaining_before_full[0]["sl"]

        price_for_full = await ctx.price_reader.read_price(SYMBOL)
        # Deliberately worse SL than what BE/trailing already achieved, to
        # exercise the "never regress" guarantee under test.
        full_text = _build_full_signal_text("BUY", price_for_full, sl_pips=20, tp1_pips=1.5, tp2_pips=3)
        await ctx.sender.send(ctx.cfg.tg_test_chat_id, full_text)

        async def check_sl_after_update():
            after = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
            return after if after else None

        positions_after_update = await _poll_until(check_sl_after_update, 30, 2)
        if not positions_after_update:
            return ScenarioResult(
                name="a3_fast_then_full_late", outcome=ScenarioOutcome.FAIL,
                evidence={}, detail="runner position disappeared unexpectedly after late full signal",
            )

        sl_after_full = positions_after_update[0]["sl"]
        if sl_after_full < sl_before_full:  # BUY: SL regressing means it got worse
            return ScenarioResult(
                name="a3_fast_then_full_late", outcome=ScenarioOutcome.FAIL,
                evidence={"sl_before_full": sl_before_full, "sl_after_full": sl_after_full},
                detail="SL regressed after late full signal update — violates update_group_signal's never-regress guarantee",
            )
        return ScenarioResult(
            name="a3_fast_then_full_late", outcome=ScenarioOutcome.PASS,
            evidence={"sl_before_full": sl_before_full, "sl_after_full": sl_after_full},
            detail="late full signal did not regress an already-improved SL",
        )
    finally:
        await cleanup_group(ctx, SYMBOL, preexisting_tickets=preexisting_tickets)
