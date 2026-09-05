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


def _ctx_with_open_position():
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=2500.0)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # after fast open
            [{"ticket": 2, "sl": 2500.0, "tp": 0.0, "volume": 0.01}],  # after BE applied
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
    setup_positions = [
        {"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
        {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
    ]
    unchanged_runner = [{"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}]

    calls = {"n": 0}

    async def _positions_for_symbol(_symbol):
        calls["n"] += 1
        return setup_positions if calls["n"] == 1 else unchanged_runner  # SL never moves after setup

    ctx.observer.positions_for_symbol = _positions_for_symbol
    ctx.observer.grep_container_logs = MagicMock(return_value=[])  # no mgmt event logged at all

    result = await b1_be_variant1.run(ctx)

    assert result.outcome == ScenarioOutcome.EXTERNAL_DEPENDENCY_FAILURE
