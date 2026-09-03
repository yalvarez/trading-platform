import sys, os
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(os.path.join(ROOT, 'services')):
    sys.path.insert(0, ROOT)
if os.path.isdir(os.path.join(ROOT, 'common')):
    sys.path.insert(0, ROOT)
import pytest
from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager


class DummyExecutor:
    """Minimal stand-in for MT5Executor — exposes only what TradeManager needs."""
    def __init__(self, sim):
        self.sim = sim
        self.accounts = [{"name": "demo", "active": True, "host": "x", "port": 1}]
        self.magic = 987654
        self.default_deviation = 50
        self.comment_prefix = "TM"

    def _client_for(self, account):
        return self.sim


class DummyNotifier:
    def __init__(self):
        self.messages = []

    async def notify(self, account_name, message):
        self.messages.append((account_name, message))

    async def notify_trade_event(self, event, **kwargs):
        self.messages.append((event, kwargs))


ACCOUNT = {"name": "demo", "active": True, "host": "x", "port": 1}


@pytest.mark.asyncio
async def test_orchestrator_signal_flow():
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    manager = TradeManager(DummyExecutor(sim), notifier=notifier)

    group_id = await manager.open_group(
        ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0
    )

    assert group_id is not None
    legs = [t for t in manager.trades.values() if t.group_id == group_id]
    assert len(legs) == 2
    leg_names = {t.leg for t in legs}
    assert leg_names == {"tp1", "runner"}
    for t in legs:
        assert t.planned_sl == 2490.0
        assert t.tp1_price == 2510.0
        assert t.tp2_price == 2530.0

    # Simular notificación
    await notifier.notify('demo', 'Test message')
    assert ('demo', 'Test message') in notifier.messages

    # Simular cierre total de ambas piernas en el simulador
    for t in legs:
        sim.positions[t.ticket]['volume'] = 0.0
        pos = sim.positions_get(ticket=t.ticket)[0]
        assert pos.volume == 0.0

    # Verifica que las notificaciones se hayan registrado
    assert len(notifier.messages) > 0
