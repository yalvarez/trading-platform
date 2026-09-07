import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome
from tests.e2e.scenarios import c1b_reopen_after_cooldown


def _ctx(positions_side_effect, grep_side_effect):
    price_reader = MagicMock()
    sender = MagicMock()
    sender.send = AsyncMock(return_value=1)
    observer = MagicMock()
    observer.positions_for_symbol = AsyncMock(side_effect=positions_side_effect)
    observer.grep_container_logs = MagicMock(side_effect=grep_side_effect)
    cfg = MagicMock(tg_test_chat_id=-1009999999999)
    return ScenarioContext(cfg=cfg, price_reader=price_reader, sender=sender, observer=observer)


@pytest.mark.asyncio
async def test_c1b_signal_past_cooldown_opens_a_second_independent_group(monkeypatch):
    monkeypatch.setenv("REOPEN_COOLDOWN_SECONDS", "300")
    monkeypatch.setattr("tests.e2e.scenarios.c1b_reopen_after_cooldown.asyncio.sleep", AsyncMock())

    two_legs = [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
                {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}]
    ctx = _ctx(
        positions_side_effect=[[], two_legs],  # snapshot, then first group opens
        grep_side_effect=[
            ["[TM][EVENT] group_opened {'group_id': 5, 'symbol': 'XAUUSD'}"],  # captured after first open
            ["[TM][EVENT] group_opened {'group_id': 5, 'symbol': 'XAUUSD'}",
             "[TM][EVENT] group_opened {'group_id': 6, 'symbol': 'XAUUSD'}"],  # new group_id 6 appears
        ],
    )

    result = await c1b_reopen_after_cooldown.run(ctx)

    assert ctx.sender.send.await_count == 2
    assert result.outcome == ScenarioOutcome.PASS
    assert result.evidence["new_group_ids"] == [6]


@pytest.mark.asyncio
async def test_c1b_passes_even_when_the_first_group_already_closed_by_the_time_the_second_opens(monkeypatch):
    # Real observed outcome, not a bug: XAUUSD can move fast enough with
    # default SL/TP that the first group closes BOTH legs entirely before
    # the cooldown elapses. Position counts alone can't distinguish this
    # from "the second signal was discarded" — the event log can, since it
    # records that a NEW group_id was opened regardless of what's live now.
    monkeypatch.setenv("REOPEN_COOLDOWN_SECONDS", "300")
    monkeypatch.setattr("tests.e2e.scenarios.c1b_reopen_after_cooldown.asyncio.sleep", AsyncMock())

    two_legs = [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
                {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}]
    ctx = _ctx(
        # snapshot, first group opens, then it's fully closed by market
        # movement before the second signal is even sent
        positions_side_effect=[[], two_legs, []],
        grep_side_effect=[
            ["[TM][EVENT] group_opened {'group_id': 8, 'symbol': 'XAUUSD'}"],
            ["[TM][EVENT] group_opened {'group_id': 8, 'symbol': 'XAUUSD'}",
             "[TM][EVENT] group_opened {'group_id': 9, 'symbol': 'XAUUSD'}"],
        ],
    )

    result = await c1b_reopen_after_cooldown.run(ctx)

    assert result.outcome == ScenarioOutcome.PASS
    assert result.evidence["new_group_ids"] == [9]


@pytest.mark.asyncio
async def test_c1b_fails_when_signal_past_cooldown_is_still_discarded(monkeypatch):
    monkeypatch.setenv("REOPEN_COOLDOWN_SECONDS", "300")
    monkeypatch.setattr("tests.e2e.scenarios.c1b_reopen_after_cooldown.asyncio.sleep", AsyncMock())

    two_legs = [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
                {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}]
    same_group_log = ["[TM][EVENT] group_opened {'group_id': 5, 'symbol': 'XAUUSD'}"]
    ctx = _ctx(
        positions_side_effect=[[], two_legs] + [two_legs] * 20,
        # No new group_id ever appears — the second signal was discarded,
        # not treated as a reopen (the exact bug this cooldown fixes).
        grep_side_effect=[same_group_log] * 20,
    )

    result = await c1b_reopen_after_cooldown.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL


@pytest.mark.asyncio
async def test_c1b_fails_when_first_fast_signal_never_opens_two_legs(monkeypatch):
    monkeypatch.setattr("tests.e2e.scenarios.c1b_reopen_after_cooldown.asyncio.sleep", AsyncMock())

    ctx = _ctx(positions_side_effect=[[], []] + [[]] * 20, grep_side_effect=[[]] * 20)

    result = await c1b_reopen_after_cooldown.run(ctx)

    assert result.outcome == ScenarioOutcome.FAIL
    assert ctx.sender.send.await_count == 1


@pytest.mark.asyncio
async def test_c1b_uses_env_cooldown_plus_margin(monkeypatch):
    monkeypatch.setenv("REOPEN_COOLDOWN_SECONDS", "300")
    sleep_mock = AsyncMock()
    monkeypatch.setattr("tests.e2e.scenarios.c1b_reopen_after_cooldown.asyncio.sleep", sleep_mock)

    two_legs = [{"ticket": 1, "sl": 2470.0, "tp": 0.0, "volume": 0.01},
                {"ticket": 2, "sl": 2470.0, "tp": 0.0, "volume": 0.01}]
    ctx = _ctx(
        positions_side_effect=[[], two_legs],
        grep_side_effect=[
            ["[TM][EVENT] group_opened {'group_id': 5, 'symbol': 'XAUUSD'}"],
            ["[TM][EVENT] group_opened {'group_id': 5, 'symbol': 'XAUUSD'}",
             "[TM][EVENT] group_opened {'group_id': 6, 'symbol': 'XAUUSD'}"],
        ],
    )

    await c1b_reopen_after_cooldown.run(ctx)

    # the wait-past-cooldown sleep (300 + 20s margin) must be among the calls
    sleep_mock.assert_any_await(320.0)
