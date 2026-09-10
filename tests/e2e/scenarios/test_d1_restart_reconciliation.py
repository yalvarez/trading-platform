import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import d1_restart_reconciliation
from tests.e2e.scenarios import a1_fast_only


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # d1's BE-confirmation poll (up to 120s / 5s interval) and post-restart
    # poll reuse a1_fast_only._poll_until, which calls asyncio.sleep between
    # attempts. Unit tests must not wait on wall-clock time. asyncio is a
    # singleton module, so patching via a1_fast_only's reference to it also
    # affects every other module's `asyncio.sleep` calls.
    monkeypatch.setattr(a1_fast_only.asyncio, "sleep", AsyncMock(return_value=None))


# tp1_leg always carries a real, nonzero tp; the runner leg is the one
# open_group leaves at tp=0.0 -- see b1_be_variant1._find_runner.
TP1_LEG = {"ticket": 1, "sl": 2470.0, "tp": 2530.0, "volume": 0.01}
RUNNER_LEG = {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}


def _ctx_with_be_applied_position():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.restart_container = AsyncMock()
    observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot: nothing open before the scenario starts
            [TP1_LEG, RUNNER_LEG],  # after fast open
            [{**RUNNER_LEG, "sl": 2500.0}],  # after BE applied (tp1 leg closed)
            [{**RUNNER_LEG, "sl": 2500.0}],  # after restart: same SL, same single position
        ]
    )
    observer.grep_container_logs = MagicMock(
        side_effect=[
            ["[TM][EVENT] mgmt_move_sl_be_applied {'group_id': 1}"],  # BE confirmation before restart
            ["[RECONCILE] al arranque: {'recovered_from_redis': 1, 'recovered_from_file': 0, 'degraded': 0, 'orphaned': []}"],  # after restart
        ]
    )
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_d1_position_survives_restart_with_same_sl_and_no_duplicate():
    ctx = _ctx_with_be_applied_position()

    result = await d1_restart_reconciliation.run(ctx)

    ctx.observer.restart_container.assert_awaited_once()
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_d1_fails_when_position_is_orphaned_after_restart():
    ctx = _ctx_with_be_applied_position()
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot
            [TP1_LEG, RUNNER_LEG],
            [{**RUNNER_LEG, "sl": 2500.0}],
            [{**RUNNER_LEG, "sl": 2470.0}],  # SL reverted after restart!
        ]
    )

    result = await d1_restart_reconciliation.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_d1_fails_when_restart_duplicates_the_group():
    ctx = _ctx_with_be_applied_position()
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot
            [TP1_LEG, RUNNER_LEG],
            [{**RUNNER_LEG, "sl": 2500.0}],
            [{**RUNNER_LEG, "sl": 2500.0},
             {"ticket": 3, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # a second, duplicate position appeared
        ]
    )

    result = await d1_restart_reconciliation.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_d1_reports_inconclusive_when_runner_closes_before_restart_is_exercised():
    """
    Real production bug found live (2026-09-10): D1 used to identify the
    runner as "the first position" and grab positions[0]'s SL as the
    baseline, same bug already fixed in B1. Confirmed live: BE was applied
    successfully (mgmt_move_sl_be_applied logged), but the runner closed
    (price touched the fresh BE SL) 2 seconds later -- before the next 5s
    poll could catch it -- and D1 reported a false "BE was logged but SL
    did not change" FAIL. Since there's nothing left to carry across a
    restart in that case, this must be INCONCLUSIVE, not a bot FAIL, and
    must never call restart_container.
    """
    ctx = _ctx_with_be_applied_position()
    calls = {"n": 0}

    async def _positions_for_symbol(_symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # preexisting_tickets snapshot
        if calls["n"] == 2:
            return [TP1_LEG, RUNNER_LEG]  # after fast open
        return []  # every poll after that: runner closed before the SL change was ever caught

    ctx.observer.positions_for_symbol = _positions_for_symbol

    result = await d1_restart_reconciliation.run(ctx)

    assert result.outcome == ScenarioOutcome.INCONCLUSIVE_RUNNER_CLOSED_BEFORE_RESTART
    ctx.observer.restart_container.assert_not_awaited()
