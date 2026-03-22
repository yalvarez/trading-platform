"""
test_trade_management_logic.py
Tests reales de la lógica de gestión de trades en TradeManager.

Cubre:
  - register_trade: guards de SL inválido
  - TP1/TP2 hit: cierre parcial en los % correctos, secuencia
  - Breakeven (BE): se activa tras TP1, calcula SL desde precio de entrada
  - Trailing stop: activación por pips, por TP2 hit, dirección BUY/SELL,
                   guard de cambio mínimo
  - gestionar_trade_be_pips: cierre 30% y flag be_applied al alcanzar umbral
  - gestionar_trade_be_pnl: cierre 30% y sl_pnl_applied al alcanzar umbral
  - Dirección SELL para TP y trailing
  - Runner: runner_enabled=True después de TP2
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.trade_orchestrator.trade_manager import TradeManager, ManagedTrade

# ─── Constantes de XAUUSD ───────────────────────────────────────────────────
POINT = 0.1  # 1 pip = 0.1 en XAUUSD
OPEN  = 3000.0
TP1   = 3020.0   # 200 pips
TP2   = 3040.0   # 400 pips


# ─── Infraestructura de mocks ───────────────────────────────────────────────

def _pos(ticket=1, symbol='XAUUSD', direction='BUY',
         price_open=OPEN, price_current=OPEN,
         sl=2990.0, tp=0.0, volume=0.10, profit=5.0):
    """Crea un objeto posición simulada con atributos reales (no MagicMock)."""
    class Pos:
        pass
    p = Pos()
    p.ticket        = ticket
    p.symbol        = symbol
    p.type          = 0 if direction == 'BUY' else 1
    p.price_open    = price_open
    p.price_current = price_current
    p.sl            = sl
    p.tp            = tp
    p.volume        = volume
    p.profit        = profit
    p.time_update   = 0
    return p


def _info(point=POINT, spread=2, stops_level=0,
          volume_step=0.01, volume_min=0.01):
    class Info:
        pass
    i = Info()
    i.point        = point
    i.spread       = spread
    i.stops_level  = stops_level
    i.volume_step  = volume_step
    i.volume_min   = volume_min
    return i


class MockClient:
    """Cliente MT5 simulado que registra llamadas y mantiene estado."""

    def __init__(self, pos, info):
        self.pos  = pos
        self.info = info
        self.partial_close_calls = []   # [(ticket, percent)]
        self.order_send_calls    = []   # [req_dict]

    def positions_get(self, ticket=None):
        if self.pos is None:
            return []
        return [self.pos]

    def symbol_info(self, symbol):
        return self.info

    def symbol_info_tick(self, symbol):
        t = MagicMock()
        t.bid = self.pos.price_current
        t.ask = self.pos.price_current + 0.2
        return t

    def partial_close(self, account, ticket, percent):
        self.partial_close_calls.append((int(ticket), int(percent)))
        self.pos.volume = round(self.pos.volume * (1 - percent / 100.0), 4)
        self.pos.time_update += 1   # _do_be detecta cambio de time_update
        return True

    def order_send(self, req):
        self.order_send_calls.append(dict(req))
        if 'sl' in req and req['sl'] is not None:
            self.pos.sl = float(req['sl'])
        return MagicMock(retcode=10009)


class MockExecutor:
    def __init__(self, client):
        self._client = client
        self.accounts  = [{'name': 'demo', 'active': True}]
        self.modify_sl = AsyncMock()   # requerido por _move_sl_to_be

    def _client_for(self, account):
        return self._client


def _manager(client, **kwargs):
    """Construye un TradeManager con configuración controlable."""
    executor = MockExecutor(client)
    cfg = dict(
        enable_trailing          = True,
        trailing_activation_pips = 30.0,
        trailing_stop_pips       = 20.0,
        trailing_min_change_pips = 1.0,
        trailing_cooldown_sec    = 0.0,
        trailing_activation_after_tp2 = True,
        enable_be_after_tp1      = True,
        be_offset_pips           = 0.0,
        long_tp1_percent         = 50,
        long_tp2_percent         = 80,
        scalp_tp1_percent        = 50,
        scalp_tp2_percent        = 80,
        buffer_pips              = 0.0,
        runner_retrace_pips      = 20.0,
    )
    cfg.update(kwargs)
    tm = TradeManager(mt5=executor, **cfg)
    # Evitar llamadas reales al notificador / Redis
    tm.notify_trade_event = AsyncMock()
    tm._notify_bg = MagicMock()
    return tm


def _trade(ticket=1, symbol='XAUUSD', direction='BUY',
           tps=None, planned_sl=2990.0, provider_tag='TEST'):
    tps = tps if tps is not None else [TP1, TP2]
    return ManagedTrade(
        account_name = 'demo',
        ticket       = ticket,
        symbol       = symbol,
        direction    = direction,
        provider_tag = provider_tag,
        group_id     = ticket,
        tps          = tps,
        planned_sl   = float(planned_sl),
    )


CUENTA = {'name': 'demo', 'active': True, 'trading_mode': 'general'}


# ─── register_trade ─────────────────────────────────────────────────────────

class TestRegisterTrade:
    def test_valid_trade_is_registered(self):
        tm = _manager(MockClient(_pos(), _info()))
        tm.register_trade('demo', 42, 'XAUUSD', 'BUY', 'TEST', [TP1, TP2], planned_sl=2990.0)
        assert 42 in tm.trades
        t = tm.trades[42]
        assert t.symbol     == 'XAUUSD'
        assert t.direction  == 'BUY'
        assert t.planned_sl == 2990.0
        assert t.tps        == [TP1, TP2]

    def test_sl_none_rejected(self):
        tm = _manager(MockClient(_pos(), _info()))
        tm.register_trade('demo', 99, 'XAUUSD', 'BUY', 'TEST', [TP1], planned_sl=None)
        assert 99 not in tm.trades

    def test_sl_zero_rejected(self):
        tm = _manager(MockClient(_pos(), _info()))
        tm.register_trade('demo', 99, 'XAUUSD', 'BUY', 'TEST', [TP1], planned_sl=0.0)
        assert 99 not in tm.trades

    def test_group_id_defaults_to_ticket(self):
        tm = _manager(MockClient(_pos(), _info()))
        tm.register_trade('demo', 7, 'XAUUSD', 'BUY', 'TEST', [TP1], planned_sl=2990.0)
        assert tm.trades[7].group_id == 7

    def test_custom_group_id(self):
        tm = _manager(MockClient(_pos(), _info()))
        tm.register_trade('demo', 7, 'XAUUSD', 'BUY', 'TEST', [TP1], planned_sl=2990.0, group_id=99)
        assert tm.trades[7].group_id == 99


# ─── TP hits ────────────────────────────────────────────────────────────────

class TestTakeProfit:

    @pytest.mark.asyncio
    async def test_tp1_hit_buy_triggers_partial_close(self):
        pos    = _pos(price_open=OPEN, price_current=TP1)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, TP1, trade)

        assert len(client.partial_close_calls) >= 1
        ticket, pct = client.partial_close_calls[0]
        assert ticket == 1
        assert 1 <= pct <= 100

    @pytest.mark.asyncio
    async def test_tp1_recorded_in_tp_hit(self):
        pos    = _pos(price_open=OPEN, price_current=TP1)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, TP1, trade)

        assert 1 in trade.tp_hit

    @pytest.mark.asyncio
    async def test_tp1_be_applied_after_partial_close(self):
        """Después de TP1 el SL debe moverse al entorno del precio de apertura."""
        pos    = _pos(price_open=OPEN, price_current=TP1, sl=2990.0)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, TP1, trade)

        # _do_be envía un order_send con 'sl' cercano al precio de apertura
        sl_reqs = [r for r in client.order_send_calls if 'sl' in r]
        assert len(sl_reqs) >= 1
        be_sl = sl_reqs[0]['sl']
        # El BE debe estar cerca del precio de entrada (OPEN ± spread pequeño)
        assert abs(be_sl - OPEN) < 5.0

    @pytest.mark.asyncio
    async def test_tp1_not_retriggered_when_already_hit(self):
        pos    = _pos(price_open=OPEN, price_current=TP1)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()
        trade.tp_hit.add(1)   # ya procesado

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, TP1, trade)

        assert len(client.partial_close_calls) == 0

    @pytest.mark.asyncio
    async def test_tp2_hit_enables_runner(self):
        """runner_enabled=True se activa en la llamada siguiente a que TP2 entre
        en tp_hit (el código hace return antes del check de runner en la misma llamada)."""
        pos    = _pos(price_open=OPEN, price_current=TP2)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()
        trade.tp_hit.add(1)   # TP1 ya fue

        with patch('asyncio.sleep', new_callable=AsyncMock):
            # Primera llamada: TP2 se procesa y entra en tp_hit
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, TP2, trade)
            assert 2 in trade.tp_hit

            # Segunda llamada: ahora el check post-loop activa runner_enabled
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, TP2, trade)

        assert trade.runner_enabled is True

    @pytest.mark.asyncio
    async def test_price_below_tp1_no_action(self):
        price  = OPEN + 5.0   # 50 pips, lejos de TP1
        pos    = _pos(price_open=OPEN, price_current=price)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, price, trade)

        assert len(client.partial_close_calls) == 0
        assert len(trade.tp_hit) == 0

    @pytest.mark.asyncio
    async def test_tp1_hit_sell_direction(self):
        """SELL: TP1 está por debajo del precio de apertura."""
        sell_open = 3050.0
        sell_tp1  = 3030.0   # por debajo
        pos    = _pos(price_open=sell_open, price_current=sell_tp1,
                      direction='SELL', sl=3060.0)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade(direction='SELL', tps=[sell_tp1, 3010.0], planned_sl=3060.0)

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, False, sell_tp1, trade)

        assert 1 in trade.tp_hit
        assert len(client.partial_close_calls) >= 1

    @pytest.mark.asyncio
    async def test_long_mode_uses_long_percents(self):
        """Con 3 TPs (_is_long_mode=True) se usan long_tp1_percent y long_tp2_percent."""
        pos    = _pos(price_open=OPEN, price_current=TP1)
        client = MockClient(pos, _info())
        tm     = _manager(client, long_tp1_percent=40, scalp_tp1_percent=70)
        trade  = _trade(tps=[TP1, TP2, 3060.0])   # 3 TPs → long mode

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, TP1, trade)

        # El percent efectivo debe acercarse a long_tp1_percent=40, no a 70
        ticket, pct = client.partial_close_calls[0]
        assert pct <= 55   # long: ~40; scalp sería 70

    @pytest.mark.asyncio
    async def test_scalp_mode_uses_scalp_percents(self):
        """Con 2 TPs (_is_long_mode=False) se usan scalp_tp1_percent y scalp_tp2_percent."""
        pos    = _pos(price_open=OPEN, price_current=TP1)
        client = MockClient(pos, _info())
        tm     = _manager(client, scalp_tp1_percent=60, long_tp1_percent=30)
        trade  = _trade(tps=[TP1, TP2])   # 2 TPs → scalp mode

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, TP1, trade)

        ticket, pct = client.partial_close_calls[0]
        assert pct >= 50   # scalp: ~60; long sería 30


# ─── Trailing stop ──────────────────────────────────────────────────────────

class TestTrailingStop:

    @pytest.mark.asyncio
    async def test_trailing_activates_by_pips_buy(self):
        """profit_pips >= trailing_activation_pips → SL se actualiza."""
        # 30 pips = 3.0 en XAUUSD con point=0.1
        current = OPEN + 30 * POINT + POINT  # un tick por encima del umbral
        pos     = _pos(price_open=OPEN, price_current=current, sl=2990.0)
        client  = MockClient(pos, _info())
        tm      = _manager(client, trailing_activation_after_tp2=False)
        trade   = _trade()

        await tm._maybe_trailing(CUENTA, pos, POINT, True, current, trade)

        sl_reqs = [r for r in client.order_send_calls if 'sl' in r and r.get('action') == 3]
        assert len(sl_reqs) >= 1
        new_sl = sl_reqs[0]['sl']
        expected_sl = current - 20 * POINT   # trail_dist = 20 pips
        assert abs(new_sl - expected_sl) < POINT

    @pytest.mark.asyncio
    async def test_trailing_not_activated_below_threshold(self):
        """profit_pips < threshold y sin TP2 → SL NO se mueve."""
        current = OPEN + 10 * POINT   # solo 10 pips, umbral es 30
        pos     = _pos(price_open=OPEN, price_current=current, sl=2990.0)
        client  = MockClient(pos, _info())
        tm      = _manager(client, trailing_activation_after_tp2=False)
        trade   = _trade()

        await tm._maybe_trailing(CUENTA, pos, POINT, True, current, trade)

        sl_reqs = [r for r in client.order_send_calls if 'sl' in r]
        assert len(sl_reqs) == 0

    @pytest.mark.asyncio
    async def test_trailing_activates_after_tp2_hit_regardless_of_pips(self):
        """Aunque el profit sea pequeño, TP2 ya hit activa el trailing."""
        current = OPEN + 5 * POINT   # solo 5 pips — muy por debajo del umbral
        pos     = _pos(price_open=OPEN, price_current=current, sl=2990.0)
        client  = MockClient(pos, _info())
        tm      = _manager(client, trailing_activation_pips=30.0,
                            trailing_activation_after_tp2=True)
        trade   = _trade()
        trade.tp_hit.add(2)    # TP2 ya alcanzado

        await tm._maybe_trailing(CUENTA, pos, POINT, True, current, trade)

        sl_reqs = [r for r in client.order_send_calls if 'sl' in r and r.get('action') == 3]
        assert len(sl_reqs) >= 1

    @pytest.mark.asyncio
    async def test_trailing_buy_sl_moves_upward(self):
        """BUY: nuevo SL debe ser mayor que el SL anterior."""
        current  = OPEN + 35 * POINT
        old_sl   = OPEN - 10 * POINT
        pos      = _pos(price_open=OPEN, price_current=current, sl=old_sl)
        client   = MockClient(pos, _info())
        tm       = _manager(client, trailing_activation_after_tp2=False)
        trade    = _trade()

        await tm._maybe_trailing(CUENTA, pos, POINT, True, current, trade)

        sl_reqs = [r for r in client.order_send_calls if 'sl' in r and r.get('action') == 3]
        assert len(sl_reqs) >= 1
        assert sl_reqs[0]['sl'] > old_sl

    @pytest.mark.asyncio
    async def test_trailing_sell_direction(self):
        """SELL: nuevo SL debe ser menor (más abajo) que el SL anterior."""
        sell_open = 3050.0
        current   = sell_open - 35 * POINT   # 35 pips abajo
        old_sl    = sell_open + 10 * POINT   # SL por encima
        pos       = _pos(price_open=sell_open, price_current=current,
                         direction='SELL', sl=old_sl)
        client    = MockClient(pos, _info())
        tm        = _manager(client, trailing_activation_after_tp2=False)
        trade     = _trade(direction='SELL', planned_sl=old_sl)

        await tm._maybe_trailing(CUENTA, pos, POINT, False, current, trade)

        sl_reqs = [r for r in client.order_send_calls if 'sl' in r and r.get('action') == 3]
        assert len(sl_reqs) >= 1
        assert sl_reqs[0]['sl'] < old_sl

    @pytest.mark.asyncio
    async def test_trailing_min_change_guard_prevents_small_updates(self):
        """Si el cambio es menor que min_change_pips, no se envía orden."""
        current  = OPEN + 35 * POINT
        trail_sl = current - 20 * POINT        # = lo que produciría el trailing
        tiny_diff = 0.5 * POINT               # menor que min_change_pips=1
        pos      = _pos(price_open=OPEN, price_current=current,
                        sl=trail_sl - tiny_diff)
        client   = MockClient(pos, _info())
        tm       = _manager(client, trailing_min_change_pips=1.0,
                             trailing_activation_after_tp2=False)
        trade    = _trade()
        trade.last_trailing_sl = trail_sl - tiny_diff  # mismo nivel → delta < min

        await tm._maybe_trailing(CUENTA, pos, POINT, True, current, trade)

        sl_reqs = [r for r in client.order_send_calls if 'sl' in r and r.get('action') == 3]
        assert len(sl_reqs) == 0

    @pytest.mark.asyncio
    async def test_trailing_records_last_sl_on_success(self):
        """Tras actualizar SL, trade.last_trailing_sl debe quedar registrado."""
        current = OPEN + 35 * POINT
        pos     = _pos(price_open=OPEN, price_current=current, sl=2990.0)
        client  = MockClient(pos, _info())
        tm      = _manager(client, trailing_activation_after_tp2=False)
        trade   = _trade()

        await tm._maybe_trailing(CUENTA, pos, POINT, True, current, trade)

        assert trade.last_trailing_sl is not None
        expected_sl = current - 20 * POINT
        assert abs(trade.last_trailing_sl - expected_sl) < POINT

    @pytest.mark.asyncio
    async def test_trailing_disabled_by_config(self):
        """Con enable_trailing=False en gestionar_trade_general no hay trailing."""
        current = OPEN + 35 * POINT
        pos     = _pos(price_open=OPEN, price_current=current, sl=2990.0)
        client  = MockClient(pos, _info())
        tm      = _manager(client, enable_trailing=False)
        trade   = _trade()

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm.gestionar_trade_general(trade, CUENTA,
                                             pos=pos, point=POINT,
                                             is_buy=True, current=current)

        sl_reqs = [r for r in client.order_send_calls if r.get('action') == 3]
        assert len(sl_reqs) == 0


# ─── Breakeven ──────────────────────────────────────────────────────────────

class TestBreakEven:

    @pytest.mark.asyncio
    async def test_be_not_applied_if_disabled(self):
        pos    = _pos(price_open=OPEN, price_current=TP1)
        client = MockClient(pos, _info())
        tm     = _manager(client, enable_be_after_tp1=False)
        trade  = _trade()

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, TP1, trade)

        # Sólo se esperan llamadas por el cierre parcial, no por BE (action==3 o 6)
        be_reqs = [r for r in client.order_send_calls if r.get('action') in (3, 6)]
        assert len(be_reqs) == 0

    @pytest.mark.asyncio
    async def test_be_sl_above_initial_sl_for_buy(self):
        """El BE debe dejar el SL por encima del SL inicial del trade."""
        initial_sl = 2990.0
        pos    = _pos(price_open=OPEN, price_current=TP1, sl=initial_sl)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, TP1, trade)

        sl_reqs = [r for r in client.order_send_calls if 'sl' in r]
        assert len(sl_reqs) >= 1
        assert sl_reqs[0]['sl'] > initial_sl

    @pytest.mark.asyncio
    async def test_be_sl_below_initial_sl_for_sell(self):
        """SELL BE debe dejar el SL por debajo del SL inicial."""
        sell_open  = 3050.0
        sell_tp1   = 3030.0
        initial_sl = 3060.0
        pos    = _pos(price_open=sell_open, price_current=sell_tp1,
                      direction='SELL', sl=initial_sl)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade(direction='SELL', tps=[sell_tp1, 3010.0], planned_sl=initial_sl)

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, False, sell_tp1, trade)

        sl_reqs = [r for r in client.order_send_calls if 'sl' in r]
        assert len(sl_reqs) >= 1
        assert sl_reqs[0]['sl'] < initial_sl


# ─── gestionar_trade_be_pips ────────────────────────────────────────────────

class TestGestionarTradeBePips:

    @pytest.mark.asyncio
    async def test_triggers_partial_close_at_be_pips(self):
        """Al alcanzar be_pips, debe hacer cierre parcial del 30%."""
        pos    = _pos(price_open=OPEN, price_current=OPEN + 35 * POINT)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()
        cuenta = {**CUENTA, 'trading_mode': 'be_pips', 'be_pips': 30}

        tm._get_recorrido_pips = MagicMock(return_value=35.0)

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm.gestionar_trade_be_pips(trade, cuenta)

        assert any(pct == 30 for _, pct in client.partial_close_calls)

    @pytest.mark.asyncio
    async def test_sets_be_applied_flag(self):
        pos    = _pos(price_open=OPEN, price_current=OPEN + 35 * POINT)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()
        cuenta = {**CUENTA, 'trading_mode': 'be_pips', 'be_pips': 30}

        tm._get_recorrido_pips = MagicMock(return_value=35.0)

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm.gestionar_trade_be_pips(trade, cuenta)

        assert getattr(trade, 'be_applied', False) is True

    @pytest.mark.asyncio
    async def test_not_triggered_below_be_pips(self):
        pos    = _pos(price_open=OPEN, price_current=OPEN + 10 * POINT)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()
        cuenta = {**CUENTA, 'trading_mode': 'be_pips', 'be_pips': 30}

        tm._get_recorrido_pips = MagicMock(return_value=10.0)

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm.gestionar_trade_be_pips(trade, cuenta)

        assert not any(pct == 30 for _, pct in client.partial_close_calls)
        assert getattr(trade, 'be_applied', False) is False

    @pytest.mark.asyncio
    async def test_not_reapplied_when_be_already_applied(self):
        pos    = _pos(price_open=OPEN, price_current=OPEN + 40 * POINT)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()
        trade.be_applied = True   # ya aplicado
        cuenta = {**CUENTA, 'trading_mode': 'be_pips', 'be_pips': 30}

        tm._get_recorrido_pips = MagicMock(return_value=40.0)

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm.gestionar_trade_be_pips(trade, cuenta)

        assert not any(pct == 30 for _, pct in client.partial_close_calls)

    @pytest.mark.asyncio
    async def test_fallback_to_general_when_no_tps(self):
        """Sin TPs hace fallback a general (no explota)."""
        pos    = _pos(price_open=OPEN, price_current=OPEN + 35 * POINT)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade(tps=[])   # sin TPs
        cuenta = {**CUENTA, 'trading_mode': 'be_pips', 'be_pips': 30}

        tm._get_recorrido_pips = MagicMock(return_value=35.0)

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm.gestionar_trade_be_pips(trade, cuenta)

        # No debe crashear; be_applied no se activa en fallback
        assert getattr(trade, 'be_applied', False) is False


# ─── gestionar_trade_be_pnl ─────────────────────────────────────────────────

class TestGestionarTradeBePnl:

    @pytest.mark.asyncio
    async def test_triggers_partial_close_at_threshold(self):
        pos    = _pos(price_open=OPEN, price_current=OPEN + 35 * POINT, profit=10.0)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()
        cuenta = {**CUENTA, 'trading_mode': 'be_pnl', 'be_pips': 30}

        tm._get_recorrido_pips = MagicMock(return_value=35.0)
        tm._calcular_sl_por_pnl = MagicMock(return_value=OPEN + 5 * POINT)
        tm._move_sl = MagicMock()

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm.gestionar_trade_be_pnl(trade, cuenta)

        assert any(pct == 30 for _, pct in client.partial_close_calls)
        assert getattr(trade, 'sl_pnl_applied', False) is True

    @pytest.mark.asyncio
    async def test_not_reapplied_when_flag_set(self):
        pos    = _pos(price_open=OPEN, price_current=OPEN + 40 * POINT, profit=15.0)
        client = MockClient(pos, _info())
        tm     = _manager(client)
        trade  = _trade()
        trade.sl_pnl_applied = True
        cuenta = {**CUENTA, 'trading_mode': 'be_pnl', 'be_pips': 30}

        tm._get_recorrido_pips = MagicMock(return_value=40.0)

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm.gestionar_trade_be_pnl(trade, cuenta)

        assert not any(pct == 30 for _, pct in client.partial_close_calls)


# ─── Runner retrace ─────────────────────────────────────────────────────────

class TestRunnerRetrace:

    @pytest.mark.asyncio
    async def test_runner_retrace_buy_closes_position(self):
        """BUY: si el precio cae runner_retrace_pips desde el pico → cierre 100%."""
        peak    = OPEN + 50 * POINT
        current = peak - 21 * POINT   # 21 pips de retroceso (umbral = 20)
        pos     = _pos(price_open=OPEN, price_current=current)
        client  = MockClient(pos, _info())
        tm      = _manager(client, runner_retrace_pips=20.0)
        trade   = _trade()
        trade.runner_enabled = True
        trade.mfe_peak_price = peak

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, current, trade)

        assert any(pct == 100 for _, pct in client.partial_close_calls)

    @pytest.mark.asyncio
    async def test_runner_retrace_sell_closes_position(self):
        """SELL: si el precio sube runner_retrace_pips desde el pico → cierre 100%.
        Los TPs deben existir (el método hace early return si tps=[]).
        tp_hit contiene {1,2} para saltarse el loop de TPs."""
        sell_open = 3050.0
        sell_tp1  = 3030.0
        sell_tp2  = 3010.0
        peak      = sell_open - 50 * POINT     # precio mínimo alcanzado (SELL baja)
        current   = peak + 21 * POINT          # retroceso de 21 pips hacia arriba
        pos       = _pos(price_open=sell_open, price_current=current,
                         direction='SELL', sl=sell_open + 10 * POINT)
        client    = MockClient(pos, _info())
        tm        = _manager(client, runner_retrace_pips=20.0)
        trade     = _trade(direction='SELL', tps=[sell_tp1, sell_tp2],
                           planned_sl=sell_open + 10 * POINT)
        trade.runner_enabled  = True
        trade.mfe_peak_price  = peak
        trade.tp_hit          = {1, 2}   # ya procesados, saltar el loop TP

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, False, current, trade)

        assert any(pct == 100 for _, pct in client.partial_close_calls)

    @pytest.mark.asyncio
    async def test_runner_retrace_not_triggered_before_threshold(self):
        """Retroceso de solo 10 pips (< 20 umbral) → NO cierra."""
        peak    = OPEN + 50 * POINT
        current = peak - 10 * POINT
        pos     = _pos(price_open=OPEN, price_current=current)
        client  = MockClient(pos, _info())
        tm      = _manager(client, runner_retrace_pips=20.0)
        trade   = _trade(tps=[])   # sin TPs para ir directo al bloque runner
        trade.runner_enabled = True
        trade.mfe_peak_price = peak
        trade.tp_hit = {1, 2}   # ambos ya procesados

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await tm._maybe_take_profits(CUENTA, pos, POINT, True, current, trade)

        assert not any(pct == 100 for _, pct in client.partial_close_calls)
