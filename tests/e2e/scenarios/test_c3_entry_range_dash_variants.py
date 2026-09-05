import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import c3_entry_range_dash_variants
from tests.e2e.scenarios import a1_fast_only


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # c3's poll loop reuses a1_fast_only._poll_until, which calls
    # asyncio.sleep between attempts using production timeout/interval
    # constants. Unit tests must not actually wait on wall-clock time.
    monkeypatch.setattr(a1_fast_only.asyncio, "sleep", AsyncMock(return_value=None))


def _ctx(price=2500.0):
    price_reader = MagicMock()
    price_reader.read_price = AsyncMock(return_value=price)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(return_value=[])
    observer.grep_container_logs = MagicMock(return_value=[])
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_c3_sends_dash_variant_signal_and_confirms_two_legs_open():
    ctx = _ctx(price=2500.0)
    ctx.observer.positions_for_symbol = AsyncMock(
        side_effect=[
            [{"ticket": 1, "sl": 2494.0, "tp": 0.0, "volume": 0.01},
             {"ticket": 2, "sl": 2494.0, "tp": 0.0, "volume": 0.01}],  # two legs opened
        ]
    )

    result = await c3_entry_range_dash_variants.run(ctx)

    ctx.sender.send.assert_awaited_once()
    sent_text = ctx.sender.send.await_args.args[1]
    assert "ENTRY PRICE: 2497.00- 2503.00" in sent_text
    assert result.outcome == ScenarioOutcome.PASS


@pytest.mark.asyncio
async def test_c3_reports_entry_range_timeout_when_aborted():
    ctx = _ctx(price=2500.0)
    ctx.observer.positions_for_symbol = AsyncMock(return_value=[])  # never opens
    ctx.observer.grep_container_logs = MagicMock(
        return_value=["[TM][EVENT] open_aborted reason=entry_range symbol=XAUUSD"]
    )

    result = await c3_entry_range_dash_variants.run(ctx)

    assert result.outcome == ScenarioOutcome.INCONCLUSIVE_ENTRY_RANGE_TIMEOUT


@pytest.mark.asyncio
async def test_c3_fails_when_dash_variant_is_not_parsed():
    ctx = _ctx(price=2500.0)
    ctx.observer.positions_for_symbol = AsyncMock(return_value=[])  # never opens
    ctx.observer.grep_container_logs = MagicMock(return_value=[])  # no abort logged either — plain parse failure

    result = await c3_entry_range_dash_variants.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
