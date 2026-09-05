import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import a1_fast_only
from tests.e2e.scenarios import a4_full_only


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
    # a4_full_only imports _poll_until from a1_fast_only rather than
    # defining its own, so the asyncio.sleep call inside that poll loop
    # lives in a1_fast_only's module namespace. The scenario's poll loops
    # use production timeout/interval constants (e.g. 600s TP1 poll); unit
    # tests must not actually wait on wall-clock time, so replace sleep
    # with a no-op for every test in this module.
    monkeypatch.setattr(a1_fast_only.asyncio, "sleep", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_a4_sends_full_signal_text_and_opens_with_its_own_sl():
    price = 2500.0
    expected_sl = price - 6

    ctx = _ctx(price=price)
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [{"ticket": 1, "sl": expected_sl, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": expected_sl, "tp": 0.0, "volume": 0.01}],  # two legs opened by full signal
            [{"ticket": 2, "sl": expected_sl, "tp": 0.0, "volume": 0.01}],  # TP1 leg closed, runner remains
        ]
    )

    result = await a4_full_only.run(ctx)

    ctx.sender.send.assert_awaited_once()
    call_args = ctx.sender.send.await_args
    assert call_args.args[0] == -1009999999999
    assert "SIGNAL ALERT" in call_args.args[1]
    assert result.outcome in (ScenarioOutcome.PASS, ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED)


@pytest.mark.asyncio
async def test_a4_fails_when_no_positions_open_after_signal():
    ctx = _ctx()
    ctx.observer.positions_for_symbol = AsyncMock(return_value=[])  # never opens

    result = await a4_full_only.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_a4_reports_entry_range_timeout_when_full_signal_aborted():
    ctx = _ctx()
    ctx.observer.positions_for_symbol = AsyncMock(return_value=[])
    ctx.observer.grep_container_logs = MagicMock(
        return_value=["[TM][EVENT] open_aborted reason=entry_range symbol=XAUUSD"]
    )

    result = await a4_full_only.run(ctx)

    assert result.outcome == ScenarioOutcome.INCONCLUSIVE_ENTRY_RANGE_TIMEOUT


@pytest.mark.asyncio
async def test_a4_fails_when_opened_sl_does_not_match_full_signal_sl():
    price = 2500.0
    wrong_sl = price - 999  # not the full signal's SL, not close to it

    ctx = _ctx(price=price)
    ctx.observer.positions_for_symbol = AsyncMock(
        return_value=[
            {"ticket": 1, "sl": wrong_sl, "tp": 0.0, "volume": 0.01},
            {"ticket": 2, "sl": wrong_sl, "tp": 0.0, "volume": 0.01},
        ]
    )

    result = await a4_full_only.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
    assert "SL" in result.detail
