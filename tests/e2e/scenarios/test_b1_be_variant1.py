import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import b1_be_variant1
from tests.e2e.scenarios import a1_fast_only


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # b1's poll loop (and _management_common's setup poll) reuse
    # a1_fast_only._poll_until, which calls asyncio.sleep between attempts
    # using production timeout/interval constants (e.g. 120s mgmt poll).
    # Unit tests must not actually wait on wall-clock time, so replace
    # sleep with a no-op for every test here. asyncio is a singleton
    # module, so patching the attribute via a1_fast_only's reference to it
    # also affects any other module's `asyncio.sleep` calls.
    monkeypatch.setattr(a1_fast_only.asyncio, "sleep", AsyncMock(return_value=None))


# tp1_leg always carries a real, nonzero tp; the runner leg is the one
# open_group leaves at tp=0.0 (its only mechanical exit is the trailing
# SL) -- see _find_runner's docstring in b1_be_variant1.py. Fixtures below
# use ticket 1 for tp1_leg (tp=2530.0) and ticket 2 for the runner
# (tp=0.0), matching real open_group output, so _find_runner and the
# by-ticket poll checks exercise the same identification logic B1 uses
# for real.
TP1_LEG = {"ticket": 1, "sl": 2470.0, "tp": 2530.0, "volume": 0.01}
RUNNER_LEG = {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}


def _ctx_with_open_position():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot: nothing open before the scenario starts
            [TP1_LEG, RUNNER_LEG],  # after fast open
            [TP1_LEG, {**RUNNER_LEG, "sl": 2500.0}],  # after BE applied — tp1_leg still open, unrelated SL unchanged
        ]
    )
    observer.grep_container_logs = MagicMock(return_value=["[TM][EVENT] mgmt_move_sl_be_applied {'group_id': 1}"])
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_b1_sends_be_message_and_confirms_sl_moved_to_entry():
    ctx = _ctx_with_open_position()

    result = await b1_be_variant1.run(ctx)

    sent_texts = [c.args[1] for c in ctx.sender.send.await_args_list]
    assert "Set BE for zero risk" in sent_texts
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_b1_reports_external_dependency_failure_on_timeout_without_error():
    ctx = _ctx_with_open_position()
    setup_positions = [TP1_LEG, RUNNER_LEG]
    unchanged = [TP1_LEG, RUNNER_LEG]

    calls = {"n": 0}

    async def _positions_for_symbol(_symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # preexisting_tickets snapshot: nothing open yet
        return setup_positions if calls["n"] == 2 else unchanged  # SL never moves after setup

    ctx.observer.positions_for_symbol = _positions_for_symbol
    ctx.observer.grep_container_logs = MagicMock(return_value=[])  # no mgmt event logged at all

    result = await b1_be_variant1.run(ctx)

    assert result.outcome == ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE


@pytest.mark.asyncio
async def test_b1_passes_when_position_closes_at_be_before_poll_catches_the_sl_change():
    """
    Real production behavior observed live (2026-09-08): the mgmt event was
    logged (order_send to move SL to BE succeeded), but real market
    movement touched that new BE SL and closed the runner before the
    next 5s poll ran -- the runner simply isn't there anymore to compare
    its SL against. This is the mechanism working correctly (a genuine
    zero-risk exit), not a bot defect, and must be a PASS.
    """
    ctx = _ctx_with_open_position()
    setup_positions = [TP1_LEG, RUNNER_LEG]

    calls = {"n": 0}

    async def _positions_for_symbol(_symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # preexisting_tickets snapshot
        if calls["n"] == 2:
            return setup_positions  # after fast open
        return []  # every poll after that: both legs already closed

    ctx.observer.positions_for_symbol = _positions_for_symbol
    ctx.observer.grep_container_logs = MagicMock(return_value=["[TM][EVENT] mgmt_move_sl_be_applied {'group_id': 1}"])

    result = await b1_be_variant1.run(ctx)

    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_b1_passes_when_runner_closes_at_be_while_tp1_leg_is_still_open():
    """
    Real production bug found live (2026-09-08): the OLD version of this
    scenario picked "the first position" as if it were the runner, and
    checked "is there ANY new position" to decide if the runner survived.
    Once the pre-message delay (below) started giving BE a real chance to
    succeed, tp1_leg was often still open (its own TP not reached yet)
    while the runner had already closed at its new BE SL -- the old logic
    kept comparing tp1_leg's unrelated, unchanged SL against the runner's
    "before" SL and reported a false FAIL. Identifying the runner by
    ticket (and by tp == 0.0 at setup) must see the runner is gone and
    report PASS even though tp1_leg is still live.
    """
    ctx = _ctx_with_open_position()
    setup_positions = [TP1_LEG, RUNNER_LEG]

    calls = {"n": 0}

    async def _positions_for_symbol(_symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # preexisting_tickets snapshot
        if calls["n"] == 2:
            return setup_positions  # after fast open
        return [TP1_LEG]  # tp1_leg still open, unrelated SL unchanged; runner is gone

    ctx.observer.positions_for_symbol = _positions_for_symbol
    ctx.observer.grep_container_logs = MagicMock(return_value=["[TM][EVENT] mgmt_move_sl_be_applied {'group_id': 1}"])

    result = await b1_be_variant1.run(ctx)

    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_b1_fails_when_event_logged_but_position_still_open_with_unchanged_sl():
    """The genuine bot-defect case: order_send reported success, but the
    live runner never actually got the new SL and is still open --
    this must stay a FAIL, distinct from the runner-already-closed case
    above."""
    ctx = _ctx_with_open_position()
    setup_positions = [TP1_LEG, RUNNER_LEG]
    unchanged = [TP1_LEG, RUNNER_LEG]  # runner still open, SL unchanged

    calls = {"n": 0}

    async def _positions_for_symbol(_symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # preexisting_tickets snapshot
        return setup_positions if calls["n"] == 2 else unchanged

    ctx.observer.positions_for_symbol = _positions_for_symbol
    ctx.observer.grep_container_logs = MagicMock(return_value=["[TM][EVENT] mgmt_move_sl_be_applied {'group_id': 1}"])

    result = await b1_be_variant1.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_b1_reports_inconclusive_mt5_rejected_be_when_order_send_was_rejected():
    """
    Real production behavior observed live (2026-09-08): n8n correctly
    classified the message and called /mgmt/action, but MT5 rejected the
    order_send (BE requested too soon after opening, price still within
    trade_stops_level of entry) -- so mgmt_move_sl_be_applied never gets
    logged, only the generic "reason=mgmt-fallback-BE" rejection line. This
    must NOT be conflated with "n8n/Ollama never called /mgmt/action at
    all" (EXTERNAL_DEPENDENCY_FAILURE) -- it's a distinct, real system
    limitation.
    """
    ctx = _ctx_with_open_position()
    setup_positions = [TP1_LEG, RUNNER_LEG]
    unchanged = [TP1_LEG, RUNNER_LEG]

    calls = {"n": 0}

    async def _positions_for_symbol(_symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # preexisting_tickets snapshot
        return setup_positions if calls["n"] == 2 else unchanged  # SL never moves — order_send was rejected

    def _grep(container, pattern, since="5m"):
        if pattern == "reason=mgmt-fallback-BE":
            return ["[TM] fallo moviendo SL runner=2 reason=mgmt-fallback-BE tras 3 intentos"]
        return []  # no mgmt_move_sl_be_applied event -- it was never logged

    ctx.observer.positions_for_symbol = _positions_for_symbol
    ctx.observer.grep_container_logs = _grep

    result = await b1_be_variant1.run(ctx)

    assert result.outcome == ScenarioOutcome.INCONCLUSIVE_MT5_REJECTED_BE
