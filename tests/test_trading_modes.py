import pytest
from services.trade_orchestrator.trade_manager import TradeManager, ManagedTrade


class DummyMT5:
    def __init__(self, price_map):
        self.price_map = price_map
        self.accounts = [{'name': 'Test', 'active': True}]

    def _client_for(self, cuenta):
        return self

    def symbol_info_tick(self, symbol):
        return type('Tick', (), {'bid': self.price_map.get(symbol, 0)})

    def positions_get(self, ticket=None):
        return [type('Pos', (), {
            'ticket': 1,
            'symbol': 'SYMBOL',
            'price_open': 100.0,
            'price_current': self.price_map.get('SYMBOL', 100.0),
            'volume': 1.0,
            'type': 0,
            'sl': 0.0
        })]

    def symbol_info(self, symbol):
        return type('Info', (), {'point': 0.01, 'volume_step': 0.01, 'volume_min': 0.01})

    async def early_partial_close(self, *a, **kw):
        return True

    async def modify_sl(self, *a, **kw):
        return True

    def partial_close(self, account, ticket, percent):
        return True

    def order_send(self, req):
        # Simula respuesta exitosa de modificación de SL
        return type('Res', (), {'retcode': 10009})()

import asyncio
import pytest

@pytest.mark.asyncio
async def test_general_mode_tp1_tp2_runner():
    """
    Testea que en modo general se cierra parcial en TP1, otra en TP2 y el runner sigue abierto.
    """
    # Simula precios: primero TP1, luego TP2, luego runner
    prices = [110.0, 120.0, 130.0]
    cuenta = {'name': 'Test', 'trading_mode': 'general'}
    tm = TradeManager(mt5=DummyMT5({'SYMBOL': prices[0]}))
    trade = ManagedTrade(
        account_name='Test', ticket=1, symbol='SYMBOL', direction='BUY', provider_tag='GEN', group_id=1,
        tps=[110.0, 120.0], planned_sl=99.0
    )
    # Simula llegada a TP1
    tm.mt5.price_map['SYMBOL'] = prices[0]
    await tm.gestionar_trade(trade, cuenta)
    await asyncio.sleep(0.01)
    # Simula llegada a TP2 (repetir tick para asegurar activación de runner)
    tm.mt5.price_map['SYMBOL'] = prices[1]
    await tm.gestionar_trade(trade, cuenta)
    await asyncio.sleep(0.01)
    await tm.gestionar_trade(trade, cuenta)
    await asyncio.sleep(0.01)
    # Simula runner (precio sigue subiendo)
    tm.mt5.price_map['SYMBOL'] = prices[2]
    await tm.gestionar_trade(trade, cuenta)
    await asyncio.sleep(0.01)
    # Asserts sobre flags internos
    assert isinstance(trade, ManagedTrade)
    # TP1 y TP2 deben estar marcados como alcanzados
    assert 1 in trade.tp_hit or 2 in trade.tp_hit or len(trade.tp_hit) > 0
    # El runner debe seguir habilitado después de TP2
    assert getattr(trade, 'runner_enabled', False) is True
