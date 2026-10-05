import pytest

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager
from services.trade_orchestrator.test_trade_manager_dual_tp import DummyExecutor, DummyNotifier, ACCOUNT


def _legs(tm, group_id):
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    return tp1_leg, runner


def _tm(sim):
    return TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), runner_mode="fixed_tp2")


async def _fast_group(tm, direction):
    return await tm.open_group(ACCOUNT, symbol="XAUUSD", direction=direction, sl=None, tp1=None, tp2=None,
                               fast_pips={"sl": 100, "tp1": 100, "tp2_extra": 40})


@pytest.mark.asyncio
async def test_tp1_cap_is_off_by_default(monkeypatch):
    monkeypatch.delenv("TP1_MAX_DISTANCE", raising=False)
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await _fast_group(tm, "BUY")
    await tm.update_group_signal(group_id, sl=2480.0, tp1=2549.0, tp2=2589.0)
    tp1_leg, _ = _legs(tm, group_id)
    assert sim.positions[tp1_leg.ticket]["tp"] == 2549.0


@pytest.mark.asyncio
async def test_full_signal_update_caps_buy_tp1_from_entry(monkeypatch):
    monkeypatch.setenv("TP1_MAX_DISTANCE", "35")
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await _fast_group(tm, "BUY")
    await tm.update_group_signal(group_id, sl=2480.0, tp1=2549.0, tp2=2589.0)
    tp1_leg, runner = _legs(tm, group_id)
    assert sim.positions[tp1_leg.ticket]["tp"] == 2535.0
    assert tp1_leg.tp1_price == 2535.0 and runner.tp1_price == 2535.0
    assert sim.positions[runner.ticket]["tp"] == 2589.0  # tp2 untouched


@pytest.mark.asyncio
async def test_full_signal_update_caps_sell_tp1_from_entry(monkeypatch):
    monkeypatch.setenv("TP1_MAX_DISTANCE", "35")
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await _fast_group(tm, "SELL")
    await tm.update_group_signal(group_id, sl=2520.0, tp1=2451.0, tp2=2411.0)
    tp1_leg, runner = _legs(tm, group_id)
    assert sim.positions[tp1_leg.ticket]["tp"] == 2465.0
    assert sim.positions[runner.ticket]["tp"] == 2411.0


@pytest.mark.asyncio
async def test_tp1_within_cap_is_unchanged(monkeypatch):
    monkeypatch.setenv("TP1_MAX_DISTANCE", "35")
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await _fast_group(tm, "BUY")
    await tm.update_group_signal(group_id, sl=2480.0, tp1=2530.0, tp2=2560.0)
    tp1_leg, _ = _legs(tm, group_id)
    assert sim.positions[tp1_leg.ticket]["tp"] == 2530.0


@pytest.mark.asyncio
async def test_fresh_full_signal_open_caps_tp1_from_fill_price(monkeypatch):
    monkeypatch.setenv("TP1_MAX_DISTANCE", "35")
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2480.0, tp1=2549.0, tp2=2589.0)
    tp1_leg, runner = _legs(tm, group_id)
    assert sim.positions[tp1_leg.ticket]["tp"] == 2535.0
    assert runner.tp1_price == 2535.0
    assert sim.positions[runner.ticket]["tp"] == 2589.0
