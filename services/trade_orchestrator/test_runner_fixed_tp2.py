import pytest

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager
from services.trade_orchestrator.test_trade_manager_dual_tp import (
    DummyExecutor, DummyNotifier, ACCOUNT, ACCOUNT_BIG_LOT,
)


def _legs(tm, group_id):
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    return tp1_leg, runner


def _tm(sim, notifier=None, mode="fixed_tp2"):
    return TradeManager(DummyExecutor(sim), notifier=notifier or DummyNotifier(), runner_mode=mode)


def test_unknown_runner_mode_is_rejected():
    with pytest.raises(ValueError):
        _tm(SimuladorMT5(), mode="bogus")


@pytest.mark.asyncio
async def test_fixed_tp2_runner_opens_with_broker_tp_at_tp2():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg, runner = _legs(tm, group_id)
    assert sim.positions[tp1_leg.ticket]["tp"] == 2510.0
    assert sim.positions[runner.ticket]["tp"] == 2530.0


@pytest.mark.asyncio
async def test_trailing_mode_runner_still_opens_without_broker_tp():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim, mode="trailing")
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    _, runner = _legs(tm, group_id)
    assert sim.positions[runner.ticket]["tp"] == 0.0


@pytest.mark.asyncio
async def test_fixed_tp2_update_group_signal_sets_runner_broker_tp_to_new_tp2():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2514.0)
    tp1_leg, runner = _legs(tm, group_id)
    await tm.update_group_signal(group_id, sl=2488.0, tp1=2512.0, tp2=2540.0)
    assert sim.positions[tp1_leg.ticket]["tp"] == 2512.0
    assert sim.positions[runner.ticket]["tp"] == 2540.0


@pytest.mark.asyncio
async def test_fixed_tp2_be_after_tp1_keeps_runner_broker_tp():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg, runner = _legs(tm, group_id)
    sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=10.0)
    await tm._tick_once_account(ACCOUNT)
    pos = sim.positions[runner.ticket]
    assert pos["sl"] == runner.entry_price
    assert pos["tp"] == 2530.0


@pytest.mark.asyncio
async def test_fixed_tp2_move_sl_be_keeps_runner_broker_tp():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    _, runner = _legs(tm, group_id)
    ok = await tm._force_runner_sl(ACCOUNT, sim, runner, runner.entry_price, reason="test")
    assert ok
    assert sim.positions[runner.ticket]["tp"] == 2530.0


@pytest.mark.asyncio
async def test_fixed_tp2_no_partial_and_no_trailing_beyond_tp2():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await tm.open_group(ACCOUNT_BIG_LOT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg, runner = _legs(tm, group_id)
    volume = sim.positions[runner.ticket]["volume"]
    sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=10.0)
    await tm._tick_once_account(ACCOUNT_BIG_LOT)

    sim.price = 2535.0
    sim.positions[runner.ticket]["price_current"] = 2535.0
    await tm._tick_once_account(ACCOUNT_BIG_LOT)

    pos = sim.positions[runner.ticket]
    assert pos["volume"] == volume
    assert pos["sl"] == runner.entry_price
    assert tm.trades[runner.ticket].tp2_partial_applied is False


@pytest.mark.asyncio
async def test_runner_closed_by_broker_tp_notifies_tp2_hit_not_external_close():
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = _tm(sim, notifier)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg, runner = _legs(tm, group_id)
    sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=10.0)
    await tm._tick_once_account(ACCOUNT)
    sim.close_position_by_tp(runner.ticket, close_price=2530.0, profit=30.0)
    await tm._tick_once_account(ACCOUNT)

    events = [e for e, _ in notifier.events]
    assert "external_close_detected" not in events
    tp2 = [kw for e, kw in notifier.events if e == "tp2_hit"]
    assert len(tp2) == 1
    assert tp2[0]["group_id"] == group_id
    assert tp2[0]["pnl_money"] == 30.0
    assert "TP2" in tp2[0]["message"]
    assert runner.ticket not in tm.trades
