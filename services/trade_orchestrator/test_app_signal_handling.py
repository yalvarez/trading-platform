import json
import pytest

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager


class DummyExecutor:
    def __init__(self, sim, accounts):
        self.sim = sim
        self.accounts = accounts
        self.magic = 987654

    def _client_for(self, account):
        return self.sim


class DummyNotifier:
    async def notify_trade_event(self, event, **kwargs):
        pass

    async def notify(self, target, message):
        pass


ACCOUNTS = [{"name": "demo", "active": True, "host": "x", "port": 1, "fixed_lot": 0.05}]


@pytest.mark.asyncio
async def test_fast_signal_opens_guard_pair_then_full_signal_completes_it():
    from services.trade_orchestrator.app import handle_signal_fields

    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim, ACCOUNTS), notifier=DummyNotifier())

    fast_fields = {"symbol": "XAUUSD", "direction": "BUY", "fast": "true", "sl": "", "tps": "[]", "entry_range": ""}
    await handle_signal_fields(fast_fields, tm, ACCOUNTS)

    assert len(tm.trades) == 2
    group_id = next(iter(tm.trades.values())).group_id
    for t in tm.trades.values():
        assert t.tp1_price is None  # guard pair, no real TPs yet

    full_fields = {
        "symbol": "XAUUSD", "direction": "BUY", "fast": "false",
        "sl": "2490.0", "tps": json.dumps([2510.0, 2530.0]), "entry_range": "",
    }
    await handle_signal_fields(full_fields, tm, ACCOUNTS)

    assert len(tm.trades) == 2  # same two legs, updated in place — not 4
    for t in tm.trades.values():
        assert t.group_id == group_id
        assert t.tp1_price == 2510.0
        assert t.tp2_price == 2530.0
        assert t.planned_sl == 2490.0


@pytest.mark.asyncio
async def test_full_signal_without_prior_fast_opens_group_directly():
    from services.trade_orchestrator.app import handle_signal_fields

    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim, ACCOUNTS), notifier=DummyNotifier())

    full_fields = {
        "symbol": "XAUUSD", "direction": "BUY", "fast": "false",
        "sl": "2490.0", "tps": json.dumps([2510.0, 2530.0]), "entry_range": "",
    }
    await handle_signal_fields(full_fields, tm, ACCOUNTS)

    assert len(tm.trades) == 2
    for t in tm.trades.values():
        assert t.tp1_price == 2510.0
        assert t.tp2_price == 2530.0
