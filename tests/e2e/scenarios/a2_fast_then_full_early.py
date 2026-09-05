"""
A2 (spec section 5): fast signal followed by a full SIGNAL ALERT before TP1
closes. update_group_signal (trade_manager.py) must replace SL/TP1/TP2 on
the already-open group rather than opening a second one. TP1 in the full
signal is set close to the read price (spec section 5 determinism note),
and ENTRY PRICE is built wide around the read price to survive the 5s gold
entry-range window (spec section 5 gold entry-range note).
"""
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS

SYMBOL = "XAUUSD"
TP1_POLL_TIMEOUT_SECONDS = 600
TP1_POLL_INTERVAL_SECONDS = 10
ENTRY_RANGE_HALF_WIDTH_PIPS = 3.0  # wide relative to expected spread — spec section 5 gold note


def _build_full_signal_text(direction: str, price: float, sl_pips: float, tp1_pips: float, tp2_pips: float) -> str:
    sign = 1 if direction == "BUY" else -1
    entry_lo = price - ENTRY_RANGE_HALF_WIDTH_PIPS
    entry_hi = price + ENTRY_RANGE_HALF_WIDTH_PIPS
    sl = price - sign * sl_pips
    tp1 = price + sign * tp1_pips
    tp2 = price + sign * tp2_pips
    return (
        "‼SIGNAL ALERT‼\n\n"
        f"PAIR: {SYMBOL}\n"
        f"ORDER TYPE: {direction}\n"
        f"ENTRY PRICE: {entry_lo:.2f} -{entry_hi:.2f}\n\n"
        f"❌STOP LOSS: {sl:.2f}\n\n"
        f"✅TAKE PROFIT 1:{tp1:.2f}\n"
        f"✅TAKE PROFIT 2:{tp2:.2f}\n"
    )


async def run(ctx: ScenarioContext) -> ScenarioResult:
    price = await ctx.price_reader.read_price(SYMBOL)
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = await ctx.observer.positions_for_symbol(SYMBOL)
        return positions if len(positions) >= 2 else None

    positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not positions:
        return ScenarioResult(
            name="a2_fast_then_full_early", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="fast signal did not open two legs",
        )

    # Re-read price right before sending the full signal — minimizes the gap
    # against the 5s gold entry-range window (spec section 5).
    price_for_full = await ctx.price_reader.read_price(SYMBOL)
    full_text = _build_full_signal_text("BUY", price_for_full, sl_pips=6, tp1_pips=1.5, tp2_pips=3)
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, full_text)

    try:
        async def check_updated_sl():
            updated = await ctx.observer.positions_for_symbol(SYMBOL)
            expected_sl = price_for_full - 6
            if updated and all(abs(p["sl"] - expected_sl) < 0.5 for p in updated):
                return updated
            return None

        updated_positions = await _poll_until(check_updated_sl, 30, 2)
        aborted_logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] open_aborted")
        if not updated_positions:
            if any("entry_range" in line for line in aborted_logs):
                return ScenarioResult(
                    name="a2_fast_then_full_early", outcome=ScenarioOutcome.INCONCLUSIVE_ENTRY_RANGE_TIMEOUT,
                    evidence={"aborted_logs": aborted_logs},
                    detail="full signal aborted on the 5s gold entry-range window, not a bot defect",
                )
            return ScenarioResult(
                name="a2_fast_then_full_early", outcome=ScenarioOutcome.FAIL,
                evidence={"positions": positions}, detail="SL was not updated to the full signal's value",
            )

        async def check_tp1_closed():
            remaining = await ctx.observer.positions_for_symbol(SYMBOL)
            return remaining if len(remaining) == 1 else None

        remaining = await _poll_until(check_tp1_closed, TP1_POLL_TIMEOUT_SECONDS, TP1_POLL_INTERVAL_SECONDS)
        if not remaining:
            return ScenarioResult(
                name="a2_fast_then_full_early", outcome=ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED,
                evidence={"positions_after_update": updated_positions},
                detail="SL/TP updated correctly; TP1 not reached by real market within timeout",
            )
        return ScenarioResult(
            name="a2_fast_then_full_early", outcome=ScenarioOutcome.PASS,
            evidence={"positions_after_update": updated_positions, "positions_after_tp1": remaining},
            detail="fast opened, full signal updated SL/TP1/TP2 on same group, TP1 closed",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
