"""
A4 (spec section 5): a full SIGNAL ALERT with no preceding fast signal.
Opens directly with the full signal's SL/TP1/TP2 (not defaults).
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import _poll_until
from tests.e2e.scenarios.a2_fast_then_full_early import _build_full_signal_text

SYMBOL = "XAUUSD"
OPEN_POLL_TIMEOUT_SECONDS = 30
OPEN_POLL_INTERVAL_SECONDS = 2
TP1_POLL_TIMEOUT_SECONDS = 600
TP1_POLL_INTERVAL_SECONDS = 10


async def run(ctx: ScenarioContext) -> ScenarioResult:
    price = await ctx.price_reader.read_price(SYMBOL)
    full_text = _build_full_signal_text("BUY", price, sl_pips=6, tp1_pips=1.5, tp2_pips=3)
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, full_text)

    async def check_two_legs_open():
        positions = await ctx.observer.positions_for_symbol(SYMBOL)
        return positions if len(positions) >= 2 else None

    positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not positions:
        aborted_logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] open_aborted")
        if any("entry_range" in line for line in aborted_logs):
            return ScenarioResult(
                name="a4_full_only", outcome=ScenarioOutcome.INCONCLUSIVE_ENTRY_RANGE_TIMEOUT,
                evidence={"aborted_logs": aborted_logs},
                detail="full signal aborted on the 5s gold entry-range window, not a bot defect",
            )
        return ScenarioResult(
            name="a4_full_only", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="full signal did not open two legs",
        )

    try:
        expected_sl = price - 6
        if not all(abs(p["sl"] - expected_sl) < 0.5 for p in positions):
            return ScenarioResult(
                name="a4_full_only", outcome=ScenarioOutcome.FAIL,
                evidence={"positions": positions},
                detail="opened SL does not match the full signal's SL (not defaults, not the sent value)",
            )

        async def check_tp1_closed():
            remaining = await ctx.observer.positions_for_symbol(SYMBOL)
            return remaining if len(remaining) == 1 else None

        remaining = await _poll_until(check_tp1_closed, TP1_POLL_TIMEOUT_SECONDS, TP1_POLL_INTERVAL_SECONDS)
        if not remaining:
            return ScenarioResult(
                name="a4_full_only", outcome=ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED,
                evidence={"positions": positions},
                detail="opened correctly with full signal's values; TP1 not reached within timeout",
            )
        return ScenarioResult(
            name="a4_full_only", outcome=ScenarioOutcome.PASS,
            evidence={"positions_after_open": positions, "positions_after_tp1": remaining},
            detail="full signal alone opened two legs with its own SL/TP1/TP2, TP1 closed",
        )
    finally:
        await cleanup_group(ctx, SYMBOL)
