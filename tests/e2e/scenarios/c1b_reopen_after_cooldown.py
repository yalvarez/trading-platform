"""
C1b (spec section 5, Familia C, new): the same fast signal sent twice, more
than REOPEN_COOLDOWN_SECONDS apart. Past that window, TradeManager.
group_age_seconds (commit afbeb18) makes app.py's handle_signal_fields treat
the repeat as a legitimate reopen rather than a duplicate -- it must open a
SECOND, independent group (BUY or SELL). This is the exact production
incident that motivated REOPEN_COOLDOWN_SECONDS: two real fast signals ~7
minutes apart were both silently dropped forever before this fix. Slower
than the rest of the suite (waits past the 300s default cooldown) --
accepted because it reproduces the real incident.
"""
import asyncio
import os
import re

from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult, cleanup_group
from tests.e2e.scenarios.a1_fast_only import (
    _poll_until,
    _preexisting_tickets,
    _new_positions,
    OPEN_POLL_TIMEOUT_SECONDS,
    OPEN_POLL_INTERVAL_SECONDS,
)

SYMBOL = "XAUUSD"
SETTLE_AFTER_SECOND_SEND_SECONDS = 15

_GROUP_OPENED_ID_RE = re.compile(r"group_opened \{'group_id': (\d+)")


def _reopen_cooldown_seconds() -> float:
    return float(os.environ.get("REOPEN_COOLDOWN_SECONDS", 300))


def _group_opened_ids(logs: list) -> set:
    """
    Extract every distinct group_id from [TM][EVENT] group_opened log lines.
    Used instead of counting live positions: XAUUSD moves fast enough with
    default SL/TP that the FIRST group can close completely (both legs) on
    its own before REOPEN_COOLDOWN_SECONDS elapses and the second fast
    signal arrives — a real, observed outcome, not a bug. Position counts
    can't tell "reopened cleanly" apart from "second signal was discarded"
    in that case, but the event log can: two distinct group_opened events
    mean two real signals were each accepted and opened, regardless of
    whether either has since closed.
    """
    ids = set()
    for line in logs:
        m = _GROUP_OPENED_ID_RE.search(line)
        if m:
            ids.add(int(m.group(1)))
    return ids


async def run(ctx: ScenarioContext) -> ScenarioResult:
    preexisting_tickets = await _preexisting_tickets(ctx, SYMBOL)

    await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

    async def check_two_legs_open():
        positions = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
        return positions if len(positions) >= 2 else None

    first_group_positions = await _poll_until(check_two_legs_open, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
    if not first_group_positions:
        return ScenarioResult(
            name="c1b_reopen_after_cooldown", outcome=ScenarioOutcome.FAIL,
            evidence={}, detail="first fast signal did not open two legs",
        )

    first_group_ids = _group_opened_ids(
        ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] group_opened")
    )

    try:
        # Wait past REOPEN_COOLDOWN_SECONDS with a margin, so the retest
        # isn't flaky against clock/latency skew between this container and
        # trade_orchestrator's own timing.
        wait_seconds = _reopen_cooldown_seconds() + 20
        await asyncio.sleep(wait_seconds)

        await ctx.sender.send(ctx.cfg.tg_test_chat_id, "XAUUSD BUY NOW")

        async def check_second_group_opened_id():
            logs = ctx.observer.grep_container_logs("atp-trade-orchestrator", "[TM][EVENT] group_opened")
            new_ids = _group_opened_ids(logs) - first_group_ids
            return new_ids if new_ids else None

        new_group_ids = await _poll_until(check_second_group_opened_id, OPEN_POLL_TIMEOUT_SECONDS, OPEN_POLL_INTERVAL_SECONDS)
        if not new_group_ids:
            after_wait = _new_positions(await ctx.observer.positions_for_symbol(SYMBOL), preexisting_tickets)
            return ScenarioResult(
                name="c1b_reopen_after_cooldown", outcome=ScenarioOutcome.FAIL,
                evidence={"positions_after_wait": after_wait, "first_group_ids": sorted(first_group_ids)},
                detail="fast signal past REOPEN_COOLDOWN_SECONDS was still discarded as a duplicate "
                       "(the exact production bug this cooldown was built to fix) — no new group_opened event",
            )
        return ScenarioResult(
            name="c1b_reopen_after_cooldown", outcome=ScenarioOutcome.PASS,
            evidence={"first_group_ids": sorted(first_group_ids), "new_group_ids": sorted(new_group_ids)},
            detail="fast signal past the reopen cooldown correctly opened a second, independent group",
        )
    finally:
        await cleanup_group(ctx, SYMBOL, preexisting_tickets=preexisting_tickets)
