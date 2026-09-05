"""
C2 (spec section 5): text that is neither a signal nor recognizable
management text. router_parser forwards it to N8N_INBOUND_WEBHOOK_URL
(app.py::forward_to_n8n) -- this suite cannot observe that outbound POST
directly (it targets n8n, external to this VPS' own logs/Redis), so it
asserts the two things it CAN observe: the text reached raw_messages, and
it produced no trade and no mgmt action.
"""
import asyncio

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult

SETTLE_SECONDS = 30
MESSAGE = "Anyone else watching the Fed announcement today? Curious how gold reacts."


async def run(ctx: ScenarioContext) -> ScenarioResult:
    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)
    await asyncio.sleep(SETTLE_SECONDS)

    raw_messages = await ctx.observer.read_raw_messages(count=20)
    reached_raw = any(MESSAGE in m.get("text", "") for m in raw_messages)

    positions = await ctx.observer.positions_for_symbol("XAUUSD")
    mutating_logs = [
        line for line in ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT]")
        if any(ev in line for ev in ("group_opened", "mgmt_close_now", "mgmt_move_sl_be_applied", "group_updated"))
    ]

    if not reached_raw:
        return ScenarioResult(
            name="c2_unrecognized_to_n8n", outcome=ScenarioOutcome.FAIL,
            evidence={"raw_messages": raw_messages},
            detail="message never reached raw_messages — ingestor/filter issue, not an n8n issue",
        )
    if positions or mutating_logs:
        return ScenarioResult(
            name="c2_unrecognized_to_n8n", outcome=ScenarioOutcome.FAIL,
            evidence={"positions": positions, "logs": mutating_logs},
            detail="unrecognized text incorrectly produced a trade or mgmt action",
        )
    return ScenarioResult(
        name="c2_unrecognized_to_n8n", outcome=ScenarioOutcome.PASS,
        evidence={"raw_messages_matched": reached_raw},
        detail="unrecognized text reached the pipeline and produced no trade/mgmt action "
                "(the outbound POST to n8n itself is not directly observable from this VPS)",
    )
