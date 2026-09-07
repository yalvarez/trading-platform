import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import a3_fast_then_full_late


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
    # production timeout/interval constants (e.g. 900s TP1-close poll).
    # Unit tests must not actually wait on wall-clock time, so replace sleep
    # with a no-op for every test in this module.
    monkeypatch.setattr(a3_fast_then_full_late.asyncio, "sleep", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_a3_passes_when_late_full_signal_does_not_regress_sl():
    ctx = _ctx(price=2500.0)
    sl_before_full = 2495.0  # improved by BE/trailing before the late full signal arrives

    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot: nothing open before the scenario starts
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],  # two legs opened by fast signal
            [{"ticket": 2, "sl": sl_before_full, "tp": 0.0, "volume": 0.01}],  # tp1 leg closed, BE/trailing applied
            [{"ticket": 2, "sl": sl_before_full, "tp": 0.0, "volume": 0.01}],  # SL unchanged (not regressed) after late full signal
        ]
    )

    result = await a3_fast_then_full_late.run(ctx)

    assert ctx.sender.send.await_count == 2
    first_call, second_call = ctx.sender.send.await_args_list
    assert first_call.args == (-1009999999999, "XAUUSD BUY NOW")
    assert "SIGNAL ALERT" in second_call.args[1]
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_a3_fails_when_sl_regresses_after_late_full_signal():
    ctx = _ctx(price=2500.0)
    sl_before_full = 2495.0
    sl_after_full = 2480.0  # worse than sl_before_full -> regression

    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [],  # preexisting_tickets snapshot
            [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}],
            [{"ticket": 2, "sl": sl_before_full, "tp": 0.0, "volume": 0.01}],
            [{"ticket": 2, "sl": sl_after_full, "tp": 0.0, "volume": 0.01}],
        ]
    )

    result = await a3_fast_then_full_late.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
    assert result.evidence["sl_before_full"] == sl_before_full
    assert result.evidence["sl_after_full"] == sl_after_full


@pytest.mark.asyncio
async def test_a3_inconclusive_when_tp1_never_reached_before_full_signal():
    ctx = _ctx(price=2500.0)
    two_legs = [
        {"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
        {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
    ]
    # First call is the preexisting_tickets snapshot (nothing open yet); every
    # call after that keeps returning the same two legs forever (TP1 never
    # closes to just the runner).
    ctx.observer.positions_for_symbol = AsyncMock(side_effect=[[]] + [two_legs] * 100)

    result = await a3_fast_then_full_late.run(ctx)

    assert result.outcome == ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED
    # Full signal is never sent in this path since we never reach the late-update state.
    ctx.sender.send.assert_awaited_once_with(-1009999999999, "XAUUSD BUY NOW")


@pytest.mark.asyncio
async def test_a3_fails_when_fast_signal_never_opens_two_legs():
    ctx = _ctx()
    ctx.observer.positions_for_symbol = AsyncMock(return_value=[])

    result = await a3_fast_then_full_late.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
