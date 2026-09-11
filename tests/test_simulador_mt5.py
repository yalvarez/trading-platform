import pytest

DEAL_REASON_CLIENT = 0
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5


class SimuladorMT5:
    def __init__(self):
        self.positions = {}
        self.deals_by_position = {}  # position ticket -> list of deal dicts (entry: 0=IN, 1=OUT)
        self.last_ticket = 1000
        self.last_deal = 5000
        self.price = 2500.0
        self.spread = 0.2
        self.stops_level = 20  # en puntos
        self.point = 0.1

    def _record_deal(self, ticket, *, entry, price, reason=DEAL_REASON_CLIENT, profit=0.0,
                      volume=None, commission=0.0, swap=0.0):
        self.last_deal += 1
        pos = self.positions.get(ticket, {})
        self.deals_by_position.setdefault(ticket, []).append({
            'ticket': self.last_deal,
            'order': ticket,
            'position_id': ticket,
            'price': price,
            'entry': entry,
            'time': self.last_deal,  # monotonic stand-in for a real timestamp
            'reason': reason,
            'profit': profit,
            'volume': volume if volume is not None else pos.get('volume', 0.0),
            'commission': commission,
            'swap': swap,
        })

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
                'comment': req.get('comment', ''),
                'magic': req.get('magic', 0),
            }
            self._record_deal(ticket, entry=0, price=self.positions[ticket]['price_open'])
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

    def symbol_select(self, symbol, enable=True):
        return True

    def tick_price(self, symbol, direction):
        """Devuelve el precio simulado actual (ask/bid no se distinguen en este simulador)."""
        return float(self.price)

    def partial_close(self, account, ticket, percent, *, reason=DEAL_REASON_CLIENT, profit=0.0):
        """Cierra (parcial o totalmente) una posicion simulada. Si percent>=100, elimina la posicion."""
        pos = self.positions.get(ticket)
        if not pos:
            return False
        closed_volume = float(pos.get('volume', 0.0)) * (percent / 100.0)
        self._record_deal(ticket, entry=1, price=pos.get('price_current', self.price),
                           reason=reason, profit=profit, volume=closed_volume)
        if percent >= 100:
            del self.positions[ticket]
        else:
            pos['volume'] = max(0.0, float(pos.get('volume', 0.0)) * (1 - percent / 100.0))
        return True

    def history_deals_get(self, *args, position=None, ticket=None, **kwargs):
        """Devuelve los deals registrados para `position` (o `ticket`, tratado como alias)."""
        pos_ticket = position if position is not None else ticket
        deals = self.deals_by_position.get(pos_ticket, [])
        return [type('TradeDeal', (), d)() for d in deals]

    def close_position_directly(self, ticket, *, close_price=None):
        """Test helper: simula un cierre fuera de banda (SL/TP hit en el broker) —
        registra el deal de salida y borra la posicion, sin pasar por partial_close."""
        pos = self.positions.get(ticket)
        if not pos:
            return
        price = close_price if close_price is not None else pos.get('price_current', self.price)
        self._record_deal(ticket, entry=1, price=price)
        del self.positions[ticket]

    def close_position_by_sl(self, ticket, *, close_price=None, profit=0.0):
        """Test helper: simula que el broker cerro la posicion por stop loss."""
        pos = self.positions.get(ticket)
        if not pos:
            return
        price = close_price if close_price is not None else pos.get('sl', self.price)
        self._record_deal(ticket, entry=1, price=price, reason=DEAL_REASON_SL, profit=profit)
        del self.positions[ticket]

    def close_position_by_tp(self, ticket, *, close_price=None, profit=0.0):
        """Test helper: simula que el broker cerro la posicion por take profit."""
        pos = self.positions.get(ticket)
        if not pos:
            return
        price = close_price if close_price is not None else pos.get('tp', self.price)
        self._record_deal(ticket, entry=1, price=price, reason=DEAL_REASON_TP, profit=profit)
        del self.positions[ticket]

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


def test_record_deal_defaults_keep_backward_compatible_shape():
    sim = SimuladorMT5()
    sim.positions[1] = {"ticket": 1, "symbol": "XAUUSD", "volume": 0.02, "price_open": 2500.0,
                         "sl": 0.0, "tp": 0.0, "price_current": 2500.0, "type": 0, "comment": "", "magic": 0}
    sim._record_deal(1, entry=1, price=2510.0)

    deals = sim.history_deals_get(position=1)
    assert deals[0].reason == 0  # DEAL_REASON_CLIENT default
    assert deals[0].profit == 0.0
    assert deals[0].commission == 0.0
    assert deals[0].swap == 0.0


def test_close_position_by_sl_sets_reason_and_profit():
    sim = SimuladorMT5()
    sim.positions[1] = {"ticket": 1, "symbol": "XAUUSD", "volume": 0.02, "price_open": 2500.0,
                         "sl": 2490.0, "tp": 0.0, "price_current": 2490.0, "type": 0, "comment": "", "magic": 0}

    sim.close_position_by_sl(1, close_price=2490.0, profit=-20.0)

    deals = sim.history_deals_get(position=1)
    assert deals[-1].reason == 4  # DEAL_REASON_SL
    assert deals[-1].price == 2490.0
    assert deals[-1].profit == -20.0
    assert 1 not in sim.positions


def test_close_position_by_tp_sets_reason_and_profit():
    sim = SimuladorMT5()
    sim.positions[1] = {"ticket": 1, "symbol": "XAUUSD", "volume": 0.02, "price_open": 2500.0,
                         "sl": 0.0, "tp": 2510.0, "price_current": 2510.0, "type": 0, "comment": "", "magic": 0}

    sim.close_position_by_tp(1, close_price=2510.0, profit=20.0)

    deals = sim.history_deals_get(position=1)
    assert deals[-1].reason == 5  # DEAL_REASON_TP
    assert deals[-1].profit == 20.0
    assert 1 not in sim.positions


def test_partial_close_still_defaults_to_client_reason():
    sim = SimuladorMT5()
    sim.positions[1] = {"ticket": 1, "symbol": "XAUUSD", "volume": 0.02, "price_open": 2500.0,
                         "sl": 0.0, "tp": 0.0, "price_current": 2505.0, "type": 0, "comment": "", "magic": 0}

    sim.partial_close(None, 1, 100)

    deals = sim.history_deals_get(position=1)
    assert deals[-1].reason == 0  # DEAL_REASON_CLIENT


def test_partial_close_records_fractional_volume_correctly():
    sim = SimuladorMT5()
    sim.positions[1] = {"ticket": 1, "symbol": "XAUUSD", "volume": 0.02, "price_open": 2500.0,
                         "sl": 0.0, "tp": 0.0, "price_current": 2505.0, "type": 0, "comment": "", "magic": 0}

    sim.partial_close(None, 1, 50)  # cierre parcial del 50%

    deals = sim.history_deals_get(position=1)
    assert deals[-1].volume == pytest.approx(0.01)  # 50% de 0.02
    # la posicion sigue existiendo con el volumen restante
    assert sim.positions[1]["volume"] == pytest.approx(0.01)
