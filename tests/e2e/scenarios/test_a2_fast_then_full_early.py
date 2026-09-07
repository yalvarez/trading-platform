import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import a2_fast_then_full_early


def _ctx(price=2500.0, parsed_signals=None, positions=None):
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=price)

    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)

    observer = MagicMock()
    observer.read_parsed_signals = AsyncMock(return_value=parsed_signals or [])
    observer.positions_for_symbol = AsyncMock(return_value=positions or [])
    observer.grep_container_logs = MagicMock(return_value=[])

    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # The scenario's poll loops call asyncio.sleep between attempts using
    # production timeout/interval constants (e.g. 600s TP1 poll). Unit tests
    # must not actually wait on wall-clock time, so replace sleep with a
    # no-op for every test in this module.
    monkeypatch.setattr(a2_fast_then_full_early.asyncio, "sleep", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_a2_sends_fast_signal_then_full_signal_and_updates_sl():
    ctx = _ctx(price=2500.0)
    expected_sl = 2500.0 - 6

    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot: nothing open before the scenario starts
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # two legs opened by fast signal
            [{"ticket": 1, "sl": expected_sl, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": expected_sl, "tp": 0.0, "volume": 0.01}],  # SL updated by full signal
            [{"ticket": 2, "sl": expected_sl, "tp": 0.0, "volume": 0.01}],  # TP1 leg closed, runner remains
        ]
    )

    result = await a2_fast_then_full_early.run(ctx)

    assert ctx.sender.send.await_count == 2
    first_call, second_call = ctx.sender.send.await_args_list
    assert first_call.args == (-1009999999999, "XAUUSD BUY NOW")
    assert first_call.args[1] == "XAUUSD BUY NOW"
    assert second_call.args[0] == -1009999999999
    assert "SIGNAL ALERT" in second_call.args[1]
    assert result.outcome in (ScenarioOutcome.PASS, ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED)


@pytest.mark.asyncio
async def test_a2_fails_when_fast_signal_never_opens_two_legs():
    ctx = _ctx()
    ctx.observer.positions_for_symbol = AsyncMock(return_value=[])  # never opens

    result = await a2_fast_then_full_early.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
    ctx.sender.send.assert_awaited_once_with(-1009999999999, "XAUUSD BUY NOW")


@pytest.mark.asyncio
async def test_a2_reports_entry_range_timeout_when_full_signal_aborted():
    ctx = _ctx(price=2500.0)
    # First call is the preexisting_tickets snapshot (nothing open yet); every
    # call after that (two-legs-open poll, then the SL-update poll that times
    # out) returns the same unmodified positions forever, so the SL-update
    # poll never sees an update and the scenario checks the abort logs.
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[[]] + [[
            {"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
            {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
        ]] * 50
    )
    ctx.observer.grep_container_logs = MagicMock(
        return_value=["[TM][EVENT] open_aborted reason=entry_range symbol=XAUUSD"]
    )

    result = await a2_fast_then_full_early.run(ctx)

    assert result.outcome == ScenarioOutcome.INCONCLUSIVE_ENTRY_RANGE_TIMEOUT
