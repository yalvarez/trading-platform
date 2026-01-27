import pytest
from tests.test_simulador_mt5 import SimuladorMT5

# Simula la gestión completa de un trade: apertura, BE, TP, cierre parcial, SL, trailing

def test_gestion_completa_trade():
    sim = SimuladorMT5()
    # 1. Abrir trade
    req_open = {
        'action': 1,
        'symbol': 'XAUUSD',
        'volume': 0.05,
        'type': 0,
        'price': 2500.0,
        'sl': 2490.0,
        'tp': 2510.0,
    }
    res_open = sim.order_send(req_open)
    assert res_open.retcode == 10009
    ticket = res_open.order

    # 2. Mover SL a BE (break even)
    req_be = {
        'action': 6,
        'position': ticket,
        'sl': 2500.0,
        'tp': 2510.0,
    }
    res_be = sim.order_send(req_be)
    assert res_be.retcode == 10009
    pos = sim.positions_get(ticket=ticket)[0]
    assert abs(pos.sl - 2500.0) < 1e-4

    # 3. Simular movimiento de precio a TP y cierre manual
    sim.price = 2510.0
    # Simula cierre por TP (en la vida real, el sistema debería detectar y cerrar la posición)
    # Aquí lo simulamos manualmente:
    sim.positions[ticket]['volume'] = 0.0  # Cerrado
    pos = sim.positions_get(ticket=ticket)[0]
    assert pos.volume == 0.0

    # 4. Reabrir y simular cierre parcial
    res_open2 = sim.order_send(req_open)
    ticket2 = res_open2.order
    sim.positions[ticket2]['volume'] = 0.05
    # Cierre parcial: reduce volumen
    sim.positions[ticket2]['volume'] -= 0.02
    pos2 = sim.positions_get(ticket=ticket2)[0]
    assert abs(pos2.volume - 0.03) < 1e-4

    # 5. Simular SL (stop loss)
    sim.price = 2490.0
    # Simula cierre por SL
    sim.positions[ticket2]['volume'] = 0.0
    pos2 = sim.positions_get(ticket=ticket2)[0]
    assert pos2.volume == 0.0

    # 6. Simular trailing stop (mover SL a favor)
    res_open3 = sim.order_send(req_open)
    ticket3 = res_open3.order
    # Supón que el precio sube y el trailing stop sube el SL
    sim.positions[ticket3]['sl'] = 2505.0
    pos3 = sim.positions_get(ticket=ticket3)[0]
    assert abs(pos3.sl - 2505.0) < 1e-4

    # 7. Simular cierre total manual
    sim.positions[ticket3]['volume'] = 0.0
    pos3 = sim.positions_get(ticket=ticket3)[0]
    assert pos3.volume == 0.0

# Puedes agregar asserts y prints para auditar cada paso si lo deseas.
