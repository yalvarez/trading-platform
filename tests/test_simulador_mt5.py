import pytest

class SimuladorMT5:
    def __init__(self):
        self.positions = {}
        self.last_ticket = 1000
        self.price = 2500.0
        self.spread = 0.2
        self.stops_level = 20  # en puntos
        self.point = 0.1

    def order_send(self, req):
        action = req.get('action')
        if action == 1:  # OPEN
            self.last_ticket += 1
            ticket = self.last_ticket
            self.positions[ticket] = {
                'ticket': ticket,
                'symbol': req['symbol'],
                'volume': req.get('volume', 0.01),
                'price_open': req.get('price', self.price),
                'sl': req.get('sl', 0.0),
                'tp': req.get('tp', 0.0),
                'price_current': self.price,
                'type': req.get('type', 0),
            }
            return type('OrderSendResult', (), {'retcode': 10009, 'order': ticket, 'deal': ticket, 'comment': 'Request executed'})()
        elif action == 6:  # SL/TP update
            ticket = req.get('position')
            if ticket in self.positions:
                self.positions[ticket]['sl'] = req.get('sl', self.positions[ticket]['sl'])
                self.positions[ticket]['tp'] = req.get('tp', self.positions[ticket]['tp'])
                return type('OrderSendResult', (), {'retcode': 10009, 'order': ticket, 'deal': 0, 'comment': 'Request executed'})()
            return type('OrderSendResult', (), {'retcode': 10016, 'order': ticket, 'deal': 0, 'comment': 'Invalid stops'})()
        return type('OrderSendResult', (), {'retcode': 10030, 'order': 0, 'deal': 0, 'comment': 'Unknown action'})()

    def positions_get(self, ticket=None):
        if ticket:
            pos = self.positions.get(ticket)
            if pos:
                return [type('TradePosition', (), pos)()]
            return []
        return [type('TradePosition', (), v)() for v in self.positions.values()]

    def symbol_info(self, symbol):
        return type('SymbolInfo', (), {
            'spread': self.spread,
            'point': self.point,
            'stops_level': self.stops_level,
            'volume_step': 0.01,
            'volume_min': 0.01,
        })()

# Ejemplo de test de gestión con el simulador

def test_be_aplicado():
    sim = SimuladorMT5()
    # Abrir trade
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
    # Simular gestión: mover SL a BE
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

# Puedes agregar más tests para TP, cierre parcial, etc.
