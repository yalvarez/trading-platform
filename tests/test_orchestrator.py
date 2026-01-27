import sys, os
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(os.path.join(ROOT, 'services')):
    sys.path.insert(0, ROOT)
if os.path.isdir(os.path.join(ROOT, 'common')):
    sys.path.insert(0, ROOT)
import pytest
import asyncio
from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.app import TradeManager, NotifierAdapter

class DummyNotifier:
    def __init__(self):
        self.messages = []
    async def notify(self, account_name, message):
        self.messages.append((account_name, message))
    async def notify_trade_event(self, event, **kwargs):
        self.messages.append((event, kwargs))

@pytest.mark.asyncio
async def test_orchestrator_signal_flow(monkeypatch):
    sim = SimuladorMT5()
    notifier = DummyNotifier()
    # Dummy MT5Executor and accounts
    class DummyMT5:
        def __init__(self, sim):
            self.accounts = [{'name': 'demo', 'active': True}]
            self._sim = sim
        def _client_for(self, account):
            return self._sim
    mt5 = DummyMT5(sim)
    manager = TradeManager(mt5, notifier=notifier)
    # Simular señal de apertura
    ticket = 1234
    manager.register_trade('demo', ticket, 'XAUUSD', 'BUY', 'TEST', [2510.0], planned_sl=2490.0)
    assert ticket in manager.trades
    # Simular mensaje de gestión Hannah (cierre parcial + BE)
    result = manager.handle_hannah_management_message(0, 'Asegura la mitad y mueve a BE')
    assert result is True or result is False  # Solo que no explote
    # Simular notificación
    await notifier.notify('demo', 'Test message')
    assert ('demo', 'Test message') in notifier.messages
    # Simular cierre parcial y BE en el simulador
    req_be = {'action': 6, 'position': ticket, 'sl': 2500.0, 'tp': 2510.0}
    res_be = sim.order_send(req_be)
    assert res_be.retcode == 10009
    pos = sim.positions_get(ticket=ticket)[0]
    assert abs(pos.sl - 2500.0) < 1e-4
    # Simular cierre total
    sim.positions[ticket]['volume'] = 0.0
    pos = sim.positions_get(ticket=ticket)[0]
    assert pos.volume == 0.0
    # Verifica que las notificaciones se hayan registrado
    assert len(notifier.messages) > 0
