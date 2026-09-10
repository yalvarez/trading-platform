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
async def test_d1_does_not_flag_a_still_open_tp1_leg_as_a_duplicate_runner():
    """
    Real production bug found live (2026-09-10), group 103: tp1_leg and the
    runner both closed within 0.6s of each other right after the restart --
    meaning both were genuinely still open, simultaneously, on D1's
    post-restart poll. len(positions_after_restart) != 1 treated tp1_leg's
    continued (legitimate) existence as "reconciliation duplicated the
    group", when in fact there was no duplication at all -- tp1_leg is a
    distinct, real leg of the same group, not a second runner. Only a
    genuine duplicate runner (a second ticket also carrying tp=0.0) is a
    real defect; tp1_leg being alongside the runner is not.
    """
    ctx = _ctx_with_be_applied_position()
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot
            [TP1_LEG, RUNNER_LEG],  # after fast open
            [{**RUNNER_LEG, "sl": 2500.0}],  # BE applied
            # Post-restart: both tp1_leg (its own unrelated sl/tp, still
            # open) and the runner (correctly BE'd) are alive at once.
            [TP1_LEG, {**RUNNER_LEG, "sl": 2500.0}],
        ]
    )

    result = await d1_restart_reconciliation.run(ctx)

    assert result.outcome == ScenarioOutcome.PASS


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


@pytest.mark.asyncio
async def test_d1_identifies_the_runner_by_ticket_after_restart_not_by_being_the_only_position():
    """
    Real production bug found live (2026-09-10): after the fix that made
    planned_sl sync correctly (confirmed via data/trade_state.jsonl), D1
    still reported a false "SL not preserved" FAIL. Root cause: the
    post-restart check treated "the one position open for the symbol" as
    "the runner", without checking its ticket -- the same bug already fixed
    for the PRE-restart identification in B1/D1. In the real run, the
    runner (BE-applied) closed at its BE price moments after the restart
    (correct mechanism), while tp1_leg -- which has its own unrelated SL and
    was still alive at that instant -- was the sole XAUUSD position D1 saw
    on its first post-restart poll. D1 compared tp1_leg's own (unrelated)
    SL against the runner's pre-restart BE SL and reported a false failure.
    The post-restart check must identify the runner by ticket, exactly like
    the pre-restart check already does, and must not conflate a still-open
    tp1_leg with "the runner survived unchanged".
    """
    ctx = _ctx_with_be_applied_position()
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot
            [TP1_LEG, RUNNER_LEG],  # after fast open
            [{**RUNNER_LEG, "sl": 2500.0}],  # after BE applied (tp1 leg closed... in this poll)
            # After restart: the runner (ticket=2) already closed at its BE
            # price moments after the restart -- only tp1_leg (ticket=1,
            # its own unrelated sl, never touched by BE) is still open.
            [TP1_LEG],
            [],  # next poll: tp1_leg closes too -- runner (by ticket) confirmed gone
            [],  # cleanup_group's own positions_for_symbol call
        ]
    )

    result = await d1_restart_reconciliation.run(ctx)

    # tp1_leg being the sole survivor must NOT be misread as "the runner,
    # unchanged" -- the runner (ticket=2) is genuinely gone (closed at BE,
    # the correct mechanism), so this must not be a false FAIL about SL
    # preservation.
    assert result.outcome != ScenarioOutcome.FAIL
    ctx.observer.restart_container.assert_awaited_once()


@pytest.mark.asyncio
async def test_d1_accepts_a_sl_that_advanced_further_via_trailing_between_be_and_the_restart():
    """
    Real production bug found live (2026-09-10), group 93: TP1 was reached
    for real, triggering automatic BE -- but trailing kept advancing the
    runner's SL twice more (BUY, so higher is better) in the ~12s between
    BE and D1 actually calling restart_container. check_be_applied captured
    the SL at the FIRST change it saw after BE (the earliest, lowest value),
    while the real SL at restart time -- and thus after restart, once
    correctly identified by ticket -- was higher, from the extra trailing.
    An exact "==" comparison flagged this legitimate improvement as "SL not
    preserved". D1 must accept a post-restart SL that is the same or BETTER
    (matching update_group_signal's own never-regress semantics for BUY:
    higher is better), and must only fail on a genuine regression.
    """
    ctx = _ctx_with_be_applied_position()
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot
            [TP1_LEG, RUNNER_LEG],  # after fast open
            [{**RUNNER_LEG, "sl": 2500.0}],  # check_be_applied's first caught change (earliest BE value)
            # Post-restart: the runner (ticket=2, found correctly) has a
            # HIGHER sl -- trailing advanced it further between BE and the
            # restart. This is an improvement, not a regression.
            [{**RUNNER_LEG, "sl": 2503.5}],
        ]
    )

    result = await d1_restart_reconciliation.run(ctx)

    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_d1_still_fails_when_the_post_restart_sl_genuinely_regresses():
    """Companion to the test above: a LOWER (worse, for BUY) SL after restart
    is a genuine regression and must still FAIL."""
    ctx = _ctx_with_be_applied_position()
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot
            [TP1_LEG, RUNNER_LEG],  # after fast open
            [{**RUNNER_LEG, "sl": 2500.0}],  # BE applied
            [{**RUNNER_LEG, "sl": 2470.0}],  # SL reverted to the pre-BE value after restart -- a real regression
        ]
    )

    result = await d1_restart_reconciliation.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_d1_reports_inconclusive_mt5_rejected_be_when_order_send_was_rejected():
    """
    Real production behavior observed live (2026-09-10): n8n correctly
    called /mgmt/action with move_sl_be_now, but MT5 rejected the
    order_send (BE requested too soon after opening, price still within
    trade_stops_level of entry) -- so mgmt_move_sl_be_applied never gets
    logged, only the generic "reason=mgmt-fallback-BE" rejection line.
    This must NOT be conflated with "n8n/Ollama never called /mgmt/action
    at all" (EXTERNAL_DEPENDENCY_FAILURE) -- same fix already applied to
    B1. restart_container must never be called in this case either.
    """
    ctx = _ctx_with_be_applied_position()
    calls = {"n": 0}

    async def _positions_for_symbol(_symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # preexisting_tickets snapshot
        if calls["n"] == 2:
            return [TP1_LEG, RUNNER_LEG]  # after fast open
        return [TP1_LEG, RUNNER_LEG]  # SL never moves -- order_send was rejected

    ctx.observer.positions_for_symbol = _positions_for_symbol

    def _grep(container, pattern, since="5m"):
        if pattern == "reason=mgmt-fallback-BE":
            return ["[TM] fallo moviendo SL runner=2 reason=mgmt-fallback-BE tras 3 intentos"]
        return []  # no mgmt_move_sl_be_applied event -- it was never logged

    ctx.observer.grep_container_logs = _grep

    result = await d1_restart_reconciliation.run(ctx)

    assert result.outcome == ScenarioOutcome.INCONCLUSIVE_MT5_REJECTED_BE
    ctx.observer.restart_container.assert_not_awaited()
