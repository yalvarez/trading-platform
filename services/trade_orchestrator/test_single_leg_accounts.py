import pytest

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager
from services.trade_orchestrator.test_trade_manager_dual_tp import DummyExecutor, DummyNotifier, ACCOUNT

CHAT_ID = "-100123"
TP1_ONLY = {**ACCOUNT, "legs": ["tp1"]}


def _tm(sim):
    return TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), runner_mode="fixed_tp2")


def _group_legs(tm, group_id):
    return sorted(t.leg for t in tm.trades.values() if t.group_id == group_id)


@pytest.mark.asyncio
async def test_tp1_only_account_opens_a_single_leg_with_broker_tp_at_tp1():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await tm.open_group(TP1_ONLY, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0,
                                   chat_id=CHAT_ID)
    assert group_id is not None
    assert _group_legs(tm, group_id) == ["tp1"]
    assert len(sim.positions) == 1
    (pos,) = sim.positions.values()
    assert pos["tp"] == 2510.0
    assert [e for e, _ in tm.notifier.events].count("group_opened") == 1


@pytest.mark.asyncio
async def test_default_account_still_opens_both_legs():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    assert _group_legs(tm, group_id) == ["runner", "tp1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legs", [[], ["bogus"], "tp1", None])
async def test_invalid_legs_config_falls_back_to_both_legs(legs):
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await tm.open_group({**ACCOUNT, "legs": legs}, symbol="XAUUSD", direction="BUY", sl=2490.0,
                                   tp1=2510.0, tp2=2530.0)
    assert _group_legs(tm, group_id) == ["runner", "tp1"]


@pytest.mark.asyncio
async def test_tp1_only_group_hitting_tp1_notifies_tp1_hit():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    group_id = await tm.open_group(TP1_ONLY, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0,
                                   chat_id=CHAT_ID)
    (tp1_leg,) = [t for t in tm.trades.values() if t.group_id == group_id]
    sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=10.0)
    await tm._tick_once_account(TP1_ONLY)
    hits = [kw for e, kw in tm.notifier.events if e == "tp1_hit"]
    assert len(hits) == 1 and hits[0]["group_id"] == group_id


@pytest.mark.asyncio
async def test_move_sl_be_now_on_tp1_only_group_moves_the_tp1_leg():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = _tm(sim)
    tm.mt5.accounts = [TP1_ONLY]
    group_id = await tm.open_group(TP1_ONLY, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0,
                                   chat_id=CHAT_ID)
    (tp1_leg,) = [t for t in tm.trades.values() if t.group_id == group_id]
    sim.price = 2505.0

    result = await tm.apply_mgmt_action(action="move_sl_be_now", chat_id=CHAT_ID, raw_text="be now", correction=None)

    assert result == {"status": "completed", "results": [{"group_id": group_id, "status": "applied"}]}
    assert abs(sim.positions_get(ticket=tp1_leg.ticket)[0].sl - tp1_leg.entry_price) < 1e-6
