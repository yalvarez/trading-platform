"""
C2 (spec section 5): text that is neither a signal nor recognizable
management text. router_parser forwards it to N8N_INBOUND_WEBHOOK_URL
(app.py::forward_to_n8n) -- this suite cannot observe that outbound POST
directly (it targets n8n, external to this VPS' own logs/Redis), so it
asserts the two things it CAN observe: the text reached raw_messages, and
it produced no trade and no mgmt action.
"""
import asyncio
from datetime import datetime, timezone

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult
from tests.e2e.scenarios._management_common import MUTATING_EVENTS

SETTLE_SECONDS = 30
MESSAGE = "Anyone else watching the Fed announcement today? Curious how gold reacts."


async def run(ctx: ScenarioContext) -> ScenarioResult:
    scenario_start_time = datetime.now(timezone.utc)

    # Snapshot BEFORE sending — same rationale as b8_spam_noop: this demo
    # account may already carry real, unrelated production positions in
    # XAUUSD, so only a NEW position between before/after counts as this
    # scenario's own false positive.
    positions_before = await ctx.observer.positions_for_symbol("XAUUSD")

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, MESSAGE)
    await asyncio.sleep(SETTLE_SECONDS)

    raw_messages = await ctx.observer.read_raw_messages(count=20)
    reached_raw = any(MESSAGE in m.get("text", "") for m in raw_messages)

    positions_after = await ctx.observer.positions_for_symbol("XAUUSD")
    new_positions = [p for p in positions_after if p not in positions_before]
    mutating_logs = [
        line for line in ctx.observer.grep_container_logs(
            "atp-trade-orchestrator", "[TM][EVENT]", since=scenario_start_time.isoformat()
        )
        if any(ev in line for ev in MUTATING_EVENTS)
    ]

    if not reached_raw:
        return ScenarioResult(
            name="c2_unrecognized_to_n8n", outcome=ScenarioOutcome.FAIL,
            evidence={"raw_messages": raw_messages},
            detail="message never reached raw_messages — ingestor/filter issue, not an n8n issue",
        )
    if new_positions or mutating_logs:
        return ScenarioResult(
            name="c2_unrecognized_to_n8n", outcome=ScenarioOutcome.FAIL,
            evidence={"new_positions": new_positions, "logs": mutating_logs},
            detail="unrecognized text incorrectly produced a trade or mgmt action",
        )
    return ScenarioResult(
        name="c2_unrecognized_to_n8n", outcome=ScenarioOutcome.PASS,
        evidence={"raw_messages_matched": reached_raw},
        detail="unrecognized text reached the pipeline and produced no trade/mgmt action "
                "(the outbound POST to n8n itself is not directly observable from this VPS)",
    )
