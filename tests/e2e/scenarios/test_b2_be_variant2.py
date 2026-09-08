import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import b2_be_variant2, b1_be_variant1
from tests.e2e.scenarios import a1_fast_only


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # b2 delegates to b1_be_variant1.run, which reuses a1_fast_only._poll_until.
    # asyncio is a singleton module, so patching via a1_fast_only's reference
    # to it affects every module's `asyncio.sleep` calls.
    monkeypatch.setattr(a1_fast_only.asyncio, "sleep", AsyncMock(return_value=None))


# tp1_leg always carries a real, nonzero tp; the runner leg is the one
# open_group leaves at tp=0.0 -- see b1_be_variant1._find_runner.
TP1_LEG = {"ticket": 1, "sl": 2470.0, "tp": 2530.0, "volume": 0.01, "price_open": 2500.0}
RUNNER_LEG = {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01, "price_open": 2500.0}


def _ctx_with_open_position():
    price_reader = MagicMock()
    # Already PRICE_CLEARANCE_MARGIN away from entry (2500.0) so
    # _wait_for_price_clearance succeeds on its first poll.
    price_reader.read_price = AsyncMock(return_value=2500.0 + b1_be_variant1.PRICE_CLEARANCE_MARGIN)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot
            [TP1_LEG, RUNNER_LEG],  # after fast open
            [TP1_LEG, {**RUNNER_LEG, "sl": 2500.0}],  # after BE applied
        ]
    )
    observer.grep_container_logs = MagicMock(return_value=["[TM][EVENT] mgmt_move_sl_be_applied {'group_id': 1}"])
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_b2_sends_variant_message_and_confirms_sl_moved_to_entry():
    ctx = _ctx_with_open_position()

    result = await b2_be_variant2.run(ctx)

    sent_texts = [c.args[1] for c in ctx.sender.send.await_args_list]
    assert "Make sure you adjust your sl to Entry for zero risk" in sent_texts
    assert result.outcome == ScenarioOutcome.PASS
    assert result.name == "b2_be_variant2"


@pytest.mark.asyncio
async def test_b2_sets_shared_message_constant_on_b1_before_delegating():
    ctx = _ctx_with_open_position()

    await b2_be_variant2.run(ctx)

    # b2/b3 mutate b1's module-level MESSAGE before delegating to b1.run —
    # scenarios always run serially in one process, never concurrently.
    assert b1_be_variant1.MESSAGE == "Make sure you adjust your sl to Entry for zero risk"


@pytest.mark.asyncio
async def test_b2_reports_external_dependency_failure_on_timeout_without_error():
    ctx = _ctx_with_open_position()
    setup_positions = [TP1_LEG, RUNNER_LEG]
    unchanged = [TP1_LEG, RUNNER_LEG]
    calls = {"n": 0}

    async def _positions_for_symbol(_symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # preexisting_tickets snapshot
        return setup_positions if calls["n"] == 2 else unchanged

    ctx.observer.positions_for_symbol = _positions_for_symbol
    ctx.observer.grep_container_logs = MagicMock(return_value=[])

    result = await b2_be_variant2.run(ctx)

    assert result.outcome == ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE
    assert result.name == "b2_be_variant2"
