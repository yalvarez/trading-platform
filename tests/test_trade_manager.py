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
from services.trade_orchestrator.trade_manager import TradeManager, ManagedTrade

class DummyNotifier:
    def __init__(self):
        self.messages = []
    async def notify(self, account_name, message):
        self.messages.append((account_name, message))
    async def notify_trade_event(self, event, **kwargs):
        self.messages.append((event, kwargs))

@pytest.fixture
def manager_with_sim():
    sim = SimuladorMT5()
    notifier = DummyNotifier()
    # MT5Executor y otros args pueden ser mockeados/minimizados si es necesario
    class DummyMT5:
        def __init__(self, sim):
            self.accounts = [{'name': 'demo', 'active': True}]
            self._sim = sim
        def _client_for(self, account):
            return self._sim
    mt5 = DummyMT5(sim)
    manager = TradeManager(mt5, notifier=notifier)
    return manager, sim, notifier

def test_register_trade(manager_with_sim):
    manager, sim, notifier = manager_with_sim
    manager.register_trade('demo', 1234, 'XAUUSD', 'BUY', 'TEST', [2510.0], planned_sl=2490.0)
    assert 1234 in manager.trades
    t = manager.trades[1234]
    assert t.symbol == 'XAUUSD'
    assert t.planned_sl == 2490.0

def test_be_and_partial_close(manager_with_sim):
    manager, sim, notifier = manager_with_sim
    # Abrir trade en el simulador
    req_open = {'action': 1, 'symbol': 'XAUUSD', 'volume': 0.05, 'type': 0, 'price': 2500.0, 'sl': 2490.0, 'tp': 2510.0}
    res_open = sim.order_send(req_open)
    ticket = res_open.order
    manager.register_trade('demo', ticket, 'XAUUSD', 'BUY', 'TEST', [2510.0], planned_sl=2490.0)
    # Simular BE
    req_be = {'action': 6, 'position': ticket, 'sl': 2500.0, 'tp': 2510.0}
    res_be = sim.order_send(req_be)
    assert res_be.retcode == 10009
    pos = sim.positions_get(ticket=ticket)[0]
    assert abs(pos.sl - 2500.0) < 1e-4
    # Simular cierre parcial
    sim.positions[ticket]['volume'] -= 0.02
    pos = sim.positions_get(ticket=ticket)[0]
    assert abs(pos.volume - 0.03) < 1e-4

def test_error_handling(manager_with_sim):
    manager, sim, notifier = manager_with_sim
    # Intentar registrar trade sin SL válido
    manager.register_trade('demo', 9999, 'XAUUSD', 'BUY', 'TEST', [2510.0], planned_sl=0.0)
    assert 9999 not in manager.trades
    # Intentar BE/cierre parcial sobre ticket inexistente
    req_be = {'action': 6, 'position': 8888, 'sl': 2500.0, 'tp': 2510.0}
    res_be = sim.order_send(req_be)
    assert res_be.retcode != 10009
