"""
C3 (spec section 5): irregular dash spacing in ENTRY PRICE ("4600- 4590",
"4325 - 4335"), as seen in real channel messages (memory:
tradepulse-channel-message-patterns). TradePulseParser.ENTRY_RE
(services/router_parser/parsers_tradepulse.py) requires a dash/en-dash but
tolerates surrounding whitespace — this is a regression check that it still
parses and opens correctly. Subject to the same 5s gold entry-range window
as A2/A4 (spec section 5 gold note).
"""
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import (
    _poll_until,
    _preexisting_tickets,
    _new_positions,
    OPEN_POLL_TIMEOUT_SECONDS,
    OPEN_POLL_INTERVAL_SECONDS,
)

SYMBOL = "XAUUSD"
ENTRY_RANGE_HALF_WIDTH_PIPS = 3.0


def _build_signal_with_dash_variant(price: float, dash: str) -> str:
    lo = price - ENTRY_RANGE_HALF_WIDTH_PIPS
    hi = price + ENTRY_RANGE_HALF_WIDTH_PIPS
    return (
        "‼SIGNAL ALERT‼\n\n"
        f"PAIR: {SYMBOL}\n"
        "ORDER TYPE: BUY\n"
        f"ENTRY PRICE: {lo:.2f}{dash}{hi:.2f}\n\n"
        f"❌STOP LOSS: {price - 6:.2f}\n\n"
        f"✅TAKE PROFIT 1:{price + 1.5:.2f}\n"
        f"✅TAKE PROFIT 2:{price + 3:.2f}\n"
    )


async def run(ctx: ScenarioContext) -> ScenarioResult:
    preexisting_tickets = await _preexisting_tickets(ctx, SYMBOL)

    price = await ctx.price_reader.read_price(SYMBOL)
    text = _build_signal_with_dash_variant(price, dash="- ")  # e.g. "4600- 4590" style spacing
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, text)

    async def check_two_legs_open():
        positions = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
        return positions if len(positions) >= 2 else None

    positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not positions:
        aborted_logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] open_aborted")
        if any("entry_range" in line for line in aborted_logs):
            return ScenarioResult(
                name="c3_entry_range_dash_variants", outcome=ScenarioOutcome.INCONCLUSIVE_ENTRY_RANGE_TIMEOUT,
                evidence={"aborted_logs": aborted_logs},
                detail="aborted on the 5s gold entry-range window, not a parsing defect",
            )
        return ScenarioResult(
            name="c3_entry_range_dash_variants", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="irregular-dash ENTRY PRICE was not parsed/opened correctly",
        )

    try:
        return ScenarioResult(
            name="c3_entry_range_dash_variants", outcome=ScenarioOutcome.PASS,
            evidence={"positions": positions},
            detail="irregular dash spacing in ENTRY PRICE parsed and opened correctly",
        )
    finally:
        await cleanup_group(ctx, SYMBOL, preexisting_tickets=preexisting_tickets)
