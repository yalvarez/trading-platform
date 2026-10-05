"""
Aperturas sin confirmar y posiciones sin gestion (2026-10-05).

Caso real grupo 204 (Vantage): la terminal estuvo colgada ~100s; el order_send
de tp1 dio timeout, la verificacion tambien, y open_group dio el grupo por
fallido ("confirmado ausente en MT5"). MT5 ejecuto la orden 99s despues -- y el
pool la reenvio tras reconectar, abriendo una segunda. Ambas posiciones quedaron
sin gestion: sin la actualizacion de la señal completa (SL 4154 / TP 4100) y
fuera del alcance del "TRADE INVALID" de las 06:04, mientras la cuenta gemela
(grupo 205) si se gestiono y cerro.
"""
import time

import pytest

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager, MAGIC


class DummyExecutor:
    def __init__(self, sim):
        self.sim = sim
        self.accounts = [ACCOUNT]

    def _client_for(self, account):
        return self.sim


class DummyNotifier:
    def __init__(self):
        self.events = []

    async def notify_trade_event(self, event, **kwargs):
        self.events.append((event, kwargs))

    def of(self, name):
        return [kw for e, kw in self.events if e == name]


ACCOUNT = {"name": "demo", "active": True, "host": "x", "port": 1, "fixed_lot": 0.02}
CHAT = "-1003321565807"


def _manager(monkeypatch, sim):
    monkeypatch.setenv("MT5_CALL_TIMEOUT_SECONDS", "0.2")
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier, runner_mode="fixed_tp2")
    tm.adopt_grace_seconds = 0.0
    return tm, notifier


def _first_order_fills_late(sim, delay=0.5):
    """El primer order_send no responde a tiempo, pero MT5 lo ejecuta `delay`
    segundos despues (lo que hizo Vantage con el grupo 204). Los siguientes
    responden normal."""
    real = sim.order_send
    calls = {"n": 0}

    def order_send(req):
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(delay)
            return real(req)
        return real(req)

    sim.order_send = order_send
    return calls


def _first_order_never_fills(sim):
    real = sim.order_send
    calls = {"n": 0}

    def order_send(req):
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.5)
            return None
        return real(req)

    sim.order_send = order_send
    return calls


async def _wait_late_fill(sim, n=1, timeout=2.0):
    import asyncio
    deadline = time.time() + timeout
    while len(sim.positions) < n and time.time() < deadline:
        await asyncio.sleep(0.02)


async def _open_sell_fast(tm):
    return await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="SELL", sl=4140.96, tp1=4130.96,
                               tp2=4126.96, chat_id=CHAT)


@pytest.mark.asyncio
async def test_group_204_late_fill_is_adopted_with_full_signal_levels_and_runner(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_fills_late(sim)

    group_id = await _open_sell_fast(tm)

    assert group_id is not None
    assert notifier.of("open_pending") and not notifier.of("open_failed")
    # La señal completa llega mientras MT5 sigue sin confirmar: debe actualizar
    # ESTE grupo, no abrir otro encima.
    assert tm.find_active_group_for_symbol("XAUUSD", chat_id=CHAT, direction="SELL",
                                           account_name="demo") == group_id
    await tm.update_group_signal(group_id, sl=4154.0, tp1=4100.0, tp2=4074.0)

    await _wait_late_fill(sim)
    await tm._tick_once_account(ACCOUNT)

    legs = {t.leg: t for t in tm.trades.values() if t.group_id == group_id}
    assert set(legs) == {"tp1", "runner"}
    for t in legs.values():
        assert t.planned_sl == 4154.0 and t.tp1_price == 4100.0 and t.tp2_price == 4074.0
        assert t.chat_id == CHAT
    tp1_pos = sim.positions[legs["tp1"].ticket]
    assert (tp1_pos["sl"], tp1_pos["tp"]) == (4154.0, 4100.0)  # niveles de la señal completa aplicados en MT5
    assert sim.positions[legs["runner"].ticket]["tp"] == 4074.0  # fixed_tp2: TP real del runner = tp2
    assert len(sim.positions) == 2  # nada duplicado
    opened = notifier.of("group_opened")
    assert len(opened) == 1 and "retraso" in opened[0]["message"]
    assert group_id not in tm._pending
    # Y el cierre del canal ahora SI lo alcanza.
    assert tm.find_active_groups_for_chat(CHAT) == [group_id]


@pytest.mark.asyncio
async def test_close_now_while_pending_closes_the_late_fill_instead_of_adopting_it(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_fills_late(sim)
    group_id = await _open_sell_fast(tm)

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT, raw_text="XAUUSD TRADE INVALID",
                                        correction=None)

    assert result["results"] == [{"group_id": group_id, "status": "pending_cancelled"}]
    await _wait_late_fill(sim)
    await tm._tick_once_account(ACCOUNT)

    assert sim.positions == {}  # cerrada, y sin runner abierto
    assert not [t for t in tm.trades.values() if t.group_id == group_id]
    closed = notifier.of("pending_leg_closed")
    assert len(closed) == 1 and closed[0]["closed"] is True
    assert "TRADE INVALID" in closed[0]["message"]


@pytest.mark.asyncio
async def test_late_runner_is_not_opened_when_price_left_the_entry_tolerance(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_fills_late(sim)
    group_id = await _open_sell_fast(tm)
    await _wait_late_fill(sim)
    sim.price = 4134.96 - 3.5  # SELL: el precio ya bajo mas de $3 (TOLERANCE_PIPS=30) desde la entrada

    await tm._tick_once_account(ACCOUNT)

    assert [t.leg for t in tm.trades.values() if t.group_id == group_id] == ["tp1"]
    assert len(sim.positions) == 1
    opened = notifier.of("group_opened")
    assert len(opened) == 1 and "no se abrio" in opened[0]["message"]


@pytest.mark.asyncio
async def test_never_executed_order_is_reported_only_after_a_real_mt5_check(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    calls = _first_order_never_fills(sim)
    group_id = await _open_sell_fast(tm)

    await tm._tick_once_account(ACCOUNT)  # dentro de la ventana: nada que reportar aun
    assert not notifier.of("open_failed")

    tm._pending[group_id].deadline_ts = 0
    await tm._tick_once_account(ACCOUNT)

    failed = notifier.of("open_failed")
    assert len(failed) == 1 and failed[0]["reason"] == "not_executed"
    assert calls["n"] == 1  # tras un timeout jamas se reenvia
    assert tm.find_active_group_for_symbol("XAUUSD", chat_id=CHAT) is None


@pytest.mark.asyncio
async def test_fill_after_the_window_expired_is_still_adopted_with_its_channel(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_fills_late(sim, delay=0.8)
    group_id = await _open_sell_fast(tm)
    tm._pending[group_id].deadline_ts = 0
    await tm._tick_once_account(ACCOUNT)  # vence antes de que MT5 la ejecute
    assert notifier.of("open_failed")

    await _wait_late_fill(sim)
    await tm._tick_once_account(ACCOUNT)

    legs = {t.leg: t for t in tm.trades.values() if t.group_id == group_id}
    # Tras el vencimiento la pierna tardia se adopta, pero no se abren piernas
    # nuevas: la señal pudo haberse vuelto a copiar en otro grupo entretanto.
    assert set(legs) == {"tp1"}
    assert legs["tp1"].chat_id == CHAT
    opened = notifier.of("group_opened")
    assert len(opened) == 1 and "retraso" in opened[0]["message"]
    assert tm.find_active_groups_for_chat(CHAT) == [group_id]


@pytest.mark.asyncio
async def test_late_fill_after_expiry_is_closed_when_the_signal_was_copied_in_a_newer_group(monkeypatch):
    """Revision 2026-10-05 #3: vencido el pendiente, la señal completa ya no lo
    encuentra y abre su propio grupo; si la orden vieja se ejecuta despues, es
    exposicion duplicada de la misma señal."""
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_fills_late(sim, delay=0.8)
    old_group = await _open_sell_fast(tm)
    tm._pending[old_group].deadline_ts = 0
    await tm._tick_once_account(ACCOUNT)
    new_group = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="SELL", sl=4154.0, tp1=4100.0,
                                    tp2=4074.0, chat_id=CHAT)

    await _wait_late_fill(sim, n=3)
    await tm._tick_once_account(ACCOUNT)

    assert not [t for t in tm.trades.values() if t.group_id == old_group]
    assert {t.leg for t in tm.trades.values() if t.group_id == new_group} == {"tp1", "runner"}
    assert len(sim.positions) == 2
    closed = notifier.of("pending_leg_closed")
    assert len(closed) == 1 and f"grupo {new_group}" in closed[0]["message"]


@pytest.mark.asyncio
async def test_close_now_after_expiry_still_closes_a_very_late_fill(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_fills_late(sim, delay=0.8)
    group_id = await _open_sell_fast(tm)
    tm._pending[group_id].deadline_ts = 0
    await tm._tick_once_account(ACCOUNT)

    await tm.apply_mgmt_action(action="close_now", chat_id=CHAT, raw_text="TRADE INVALID", correction=None)
    await _wait_late_fill(sim)
    await tm._tick_once_account(ACCOUNT)

    assert sim.positions == {}
    assert len(notifier.of("pending_leg_closed")) == 1


@pytest.mark.asyncio
async def test_error_kind_pending_with_leg_in_grace_is_neither_expired_nor_resent(monkeypatch):
    """Revision 2026-10-05 #1: la pierna ya aparecio en MT5 pero sigue en el
    periodo de gracia -- vencerla daria un falso "no se ejecuto" y, tras un
    error de conexion, un reenvio que duplicaria la posicion."""
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    tm.adopt_grace_seconds = 60.0
    real = sim.order_send
    calls = {"n": 0}
    captured = {}

    def order_send(req):
        calls["n"] += 1
        if calls["n"] == 1:
            captured["req"] = dict(req)
            raise EOFError("connection closed")
        return real(req)

    sim.order_send = order_send
    group_id = await _open_sell_fast(tm)
    real(captured["req"])  # MT5 la ejecuto igual; aparece en la foto, todavia en gracia
    tm._pending[group_id].deadline_ts = 0

    await tm._tick_once_account(ACCOUNT)

    assert not notifier.of("open_failed")
    assert calls["n"] == 1  # nada reenviado
    assert len(sim.positions) == 1
    for key in tm._untracked_seen:
        tm._untracked_seen[key] -= 61
    await tm._tick_once_account(ACCOUNT)
    assert {t.leg for t in tm.trades.values() if t.group_id == group_id} == {"tp1", "runner"}
    assert len(sim.positions) == 2


@pytest.mark.asyncio
async def test_close_now_during_adoption_prevents_the_late_runner(monkeypatch):
    """Revision 2026-10-05 #2: close_now llega mientras se adopta la pierna
    tardia (durante el order_send que aplica SL/TP): el runner no debe abrirse
    despues contra la instruccion del canal."""
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_fills_late(sim)
    group_id = await _open_sell_fast(tm)
    await tm.update_group_signal(group_id, sl=4154.0, tp1=4100.0, tp2=4074.0)  # fuerza el action=6 al adoptar
    await _wait_late_fill(sim)

    original_send = sim.order_send
    state = {"done": False}

    def order_send_with_concurrent_close(req):
        if req.get("action") == 6 and not state["done"]:
            state["done"] = True
            tm._pending[group_id].cancel_reason = "TRADE INVALID"  # lo que hace _close_group_now
        return original_send(req)

    sim.order_send = order_send_with_concurrent_close
    await tm._tick_once_account(ACCOUNT)

    assert not [p for p in sim.positions.values() if p["comment"].endswith("runner")]


@pytest.mark.asyncio
async def test_cancelled_pending_does_not_swallow_the_next_signal(monkeypatch):
    """Revision 2026-10-05 #5."""
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_never_fills(sim)
    group_id = await _open_sell_fast(tm)
    await tm.apply_mgmt_action(action="close_now", chat_id=CHAT, raw_text="TRADE INVALID", correction=None)

    assert tm.find_active_group_for_symbol("XAUUSD", chat_id=CHAT, direction="SELL") is None
    tm._pending[group_id].deadline_ts = 0
    await tm._tick_once_account(ACCOUNT)
    assert not notifier.of("open_failed")  # no se anuncia como "no copiada" algo que el canal anulo


@pytest.mark.asyncio
async def test_runner_adopted_after_tp1_hit_is_moved_to_breakeven(monkeypatch):
    """Revision 2026-10-05 #4: tp1 toca TP mientras el runner sigue sin
    confirmar; al aparecer, el runner debe quedar en BE como si hubiera abierto
    a tiempo."""
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    real = sim.order_send
    calls = {"n": 0}

    def order_send(req):
        calls["n"] += 1
        if calls["n"] == 2:
            time.sleep(0.5)
        return real(req)

    sim.order_send = order_send
    group_id = await _open_sell_fast(tm)
    tp1 = next(t for t in tm.trades.values() if t.leg == "tp1")
    sim.close_position_by_tp(tp1.ticket, close_price=4130.96, profit=8.0)
    await tm._tick_once_account(ACCOUNT)
    assert len(notifier.of("tp1_hit")) == 1  # se notifica aunque no haya runner todavia

    await _wait_late_fill(sim, n=1)
    await tm._tick_once_account(ACCOUNT)

    runner = next(t for t in tm.trades.values() if t.leg == "runner")
    assert runner.be_applied and sim.positions[runner.ticket]["sl"] == runner.entry_price


@pytest.mark.asyncio
async def test_be_requested_while_pending_is_applied_on_adoption(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_fills_late(sim)
    group_id = await _open_sell_fast(tm)
    await tm.apply_mgmt_action(action="move_sl_be_now", chat_id=CHAT, raw_text="move SL to BE", correction=None)

    await _wait_late_fill(sim)
    await tm._tick_once_account(ACCOUNT)

    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    tp1 = next(t for t in legs if t.leg == "tp1")
    assert tp1.be_applied and sim.positions[tp1.ticket]["sl"] == tp1.entry_price


@pytest.mark.asyncio
async def test_rpyc_result_expired_is_a_timeout_and_never_resent(monkeypatch):
    """Revision 2026-10-05 #7: en Python 3.10 el "result expired" de rpyc es un
    TimeoutError builtin, no asyncio.TimeoutError. La orden SI llego a la
    terminal: debe tratarse como timeout (sin reenvio), no como error."""
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    real = sim.order_send
    calls = {"n": 0}

    def order_send(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("result expired")
        return real(req)

    sim.order_send = order_send
    group_id = await _open_sell_fast(tm)

    assert tm._pending[group_id].failure_kind == "timeout"
    tm._pending[group_id].deadline_ts = 0
    await tm._tick_once_account(ACCOUNT)
    assert calls["n"] == 1
    assert notifier.of("open_failed")


@pytest.mark.asyncio
async def test_connection_error_on_runner_after_tp1_keeps_tp1_and_waits_for_runner(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    real = sim.order_send
    calls = {"n": 0}

    def order_send(req):
        calls["n"] += 1
        if calls["n"] == 2:
            raise EOFError("connection closed")
        return real(req)

    sim.order_send = order_send
    group_id = await _open_sell_fast(tm)

    assert [t.leg for t in tm.trades.values()] == ["tp1"]
    assert tm._pending[group_id].unconfirmed_legs == {"runner"}
    tm._pending[group_id].deadline_ts = 0
    await tm._tick_once_account(ACCOUNT)  # no aparecio: se reenvia una vez
    assert {t.leg for t in tm.trades.values() if t.group_id == group_id} == {"tp1", "runner"}


@pytest.mark.asyncio
async def test_orphan_duplicate_gets_its_group_levels_applied_in_mt5(monkeypatch):
    """Revision 2026-10-05 #6: heredar niveles solo en memoria dejaba MT5 con
    los del fast mientras el estado decia otra cosa."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm, notifier = _manager(monkeypatch, sim)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0,
                                   chat_id=CHAT)
    dup_ticket = sim.order_send({"action": 1, "symbol": "XAUUSD", "volume": 0.02, "type": 0, "price": 2500.4,
                                 "sl": 2494.0, "tp": 2504.0, "comment": f"TM-GRP{group_id}-tp1",
                                 "magic": MAGIC}).order

    await tm._tick_once_account(ACCOUNT)

    assert (sim.positions[dup_ticket]["sl"], sim.positions[dup_ticket]["tp"]) == (2490.0, 2510.0)


@pytest.mark.asyncio
async def test_unexpected_exception_after_legs_opened_keeps_their_channel_for_adoption(monkeypatch):
    """Revision 2026-10-05 #8: si open_group falla por algo inesperado despues
    de abrir en MT5, la adopcion del tick debe conservar canal y niveles."""
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    real_insert = tm._insert_leg

    def insert_raises(*args, **kwargs):
        raise RuntimeError("bug inesperado")

    tm._insert_leg = insert_raises
    group_id_before = tm._next_group_id
    assert await _open_sell_fast(tm) is None
    tm._insert_leg = real_insert

    await tm._tick_once_account(ACCOUNT)

    legs = [t for t in tm.trades.values() if t.group_id == group_id_before]
    assert {t.leg for t in legs} == {"tp1", "runner"}
    assert all(t.chat_id == CHAT for t in legs)


@pytest.mark.asyncio
async def test_runner_timeout_keeps_tp1_managed_and_adopts_runner_when_it_appears(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    real = sim.order_send
    calls = {"n": 0}

    def order_send(req):
        calls["n"] += 1
        if calls["n"] == 2:  # runner: responde tarde
            time.sleep(0.5)
        return real(req)

    sim.order_send = order_send
    group_id = await _open_sell_fast(tm)
    assert [t.leg for t in tm.trades.values()] == ["tp1"]

    await _wait_late_fill(sim, n=2)
    await tm._tick_once_account(ACCOUNT)

    assert {t.leg for t in tm.trades.values() if t.group_id == group_id} == {"tp1", "runner"}
    assert len(notifier.of("group_opened")) == 1
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_connection_error_is_resent_once_after_mt5_confirms_absence(monkeypatch):
    """Un error de conexion inmediato (la orden pudo no salir) es el unico caso
    en que se reenvia -- una sola vez, tras una foto real de MT5 sin la
    posicion, y solo si el precio sigue dentro de tolerancia."""
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    real = sim.order_send
    calls = {"n": 0}

    def order_send(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise EOFError("connection closed by peer")
        return real(req)

    sim.order_send = order_send
    group_id = await _open_sell_fast(tm)
    assert tm._pending[group_id].deadline_ts - time.time() <= 30.0

    tm._pending[group_id].deadline_ts = 0
    await tm._tick_once_account(ACCOUNT)

    assert {t.leg for t in tm.trades.values() if t.group_id == group_id} == {"tp1", "runner"}
    assert len(sim.positions) == 2
    assert calls["n"] == 3  # 1 fallida + tp1 reenviada + runner
    assert not notifier.of("open_failed")


@pytest.mark.asyncio
async def test_untracked_position_waits_the_grace_period_before_adoption(monkeypatch):
    """Una foto de positions_get tomada justo antes de que close_now cierre una
    pierna aun la muestra: no debe re-adoptarse en ese mismo instante."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm, notifier = _manager(monkeypatch, sim)
    tm.adopt_grace_seconds = 60.0
    sim.order_send({"action": 1, "symbol": "XAUUSD", "volume": 0.02, "type": 0, "price": 2500.0, "sl": 2490.0,
                    "tp": 2510.0, "comment": "TM-GRP9-tp1", "magic": MAGIC})

    await tm._tick_once_account(ACCOUNT)
    assert tm.trades == {}

    for key in tm._untracked_seen:
        tm._untracked_seen[key] -= 61
    await tm._tick_once_account(ACCOUNT)
    assert len(tm.trades) == 1


@pytest.mark.asyncio
async def test_orphan_with_unknown_group_is_adopted_and_flagged_as_unreachable_by_channel(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm, notifier = _manager(monkeypatch, sim)
    sim.order_send({"action": 1, "symbol": "XAUUSD", "volume": 0.02, "type": 1, "price": 2500.0, "sl": 2506.0,
                    "tp": 2496.0, "comment": "TM-GRP77-tp1", "magic": MAGIC})
    sim.order_send({"action": 1, "symbol": "XAUUSD", "volume": 0.02, "type": 0, "price": 2500.0, "sl": 2490.0,
                    "tp": 2510.0, "comment": "manual trade", "magic": 0})  # ajena: no se toca

    await tm._tick_once_account(ACCOUNT)

    assert len(tm.trades) == 1
    t = next(iter(tm.trades.values()))
    assert (t.group_id, t.leg, t.direction, t.planned_sl, t.tp1_price, t.chat_id) == (77, "tp1", "SELL", 2506.0, 2496.0, None)
    adopted = notifier.of("orphan_position_adopted")
    assert len(adopted) == 1 and "NO la alcanzaran" in adopted[0]["message"]
    assert tm._next_group_id == 78
    assert len(sim.positions) == 2


@pytest.mark.asyncio
async def test_duplicate_position_of_a_live_group_inherits_its_levels_and_channel(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm, notifier = _manager(monkeypatch, sim)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0,
                                   chat_id=CHAT)
    sim.order_send({"action": 1, "symbol": "XAUUSD", "volume": 0.02, "type": 0, "price": 2500.4, "sl": 2494.0,
                    "tp": 2504.0, "comment": f"TM-GRP{group_id}-tp1", "magic": MAGIC})

    await tm._tick_once_account(ACCOUNT)

    dup = [t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1"]
    assert len(dup) == 2
    assert all(t.chat_id == CHAT and t.planned_sl == 2490.0 and t.tp1_price == 2510.0 for t in dup)
    assert "NO la alcanzaran" not in notifier.of("orphan_position_adopted")[0]["message"]


@pytest.mark.asyncio
async def test_positions_of_a_group_still_opening_are_not_taken_for_orphans(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm, notifier = _manager(monkeypatch, sim)
    sim.order_send({"action": 1, "symbol": "XAUUSD", "volume": 0.02, "type": 0, "price": 2500.0, "sl": 2490.0,
                    "tp": 2510.0, "comment": "TM-GRP5-tp1", "magic": MAGIC})
    tm._opening_groups.add(5)

    await tm._tick_once_account(ACCOUNT)

    assert tm.trades == {}
    assert not notifier.of("orphan_position_adopted")


@pytest.mark.asyncio
async def test_opposite_signal_cancels_a_pending_group(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_fills_late(sim)
    group_id = await _open_sell_fast(tm)

    results = await tm.close_opposite_groups_before_tp1(chat_id=CHAT, symbol="XAUUSD", direction="BUY")

    assert results == [{"group_id": group_id, "status": "pending_cancelled"}]
    await _wait_late_fill(sim)
    await tm._tick_once_account(ACCOUNT)
    assert sim.positions == {}


@pytest.mark.asyncio
async def test_pending_group_counts_for_the_fast_duplicate_cooldown(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_never_fills(sim)
    group_id = await _open_sell_fast(tm)

    age = tm.group_age_seconds(group_id)

    assert age is not None and age < 5


@pytest.mark.asyncio
async def test_partial_and_be_actions_skip_groups_with_nothing_confirmed_yet(monkeypatch):
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_never_fills(sim)
    await _open_sell_fast(tm)

    for action in ("close_partial_now", "move_sl_be_now"):
        result = await tm.apply_mgmt_action(action=action, chat_id=CHAT, raw_text="secure profits", correction=None)
        assert result == {"status": "completed", "results": []}


@pytest.mark.asyncio
async def test_skipped_resend_keeps_watching_so_a_late_fill_keeps_its_channel(monkeypatch):
    """Re-revision A: tras un error de conexion, si el reenvio no sale (precio
    fuera de tolerancia), la orden original pudo llegar igual: el registro debe
    seguir vigilandola para adoptarla con su canal (y que close_now la alcance)."""
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    real = sim.order_send
    calls = {"n": 0}
    captured = {}

    def order_send(req):
        calls["n"] += 1
        if calls["n"] == 1:
            captured["req"] = dict(req)
            raise EOFError("connection closed")
        return real(req)

    sim.order_send = order_send
    group_id = await _open_sell_fast(tm)
    sim.price = 4134.96 - 4.0  # fuera de tolerancia: no se reenvia
    tm._pending[group_id].deadline_ts = 0
    await tm._tick_once_account(ACCOUNT)
    assert calls["n"] == 1 and notifier.of("open_failed")
    assert group_id in tm._pending

    real(captured["req"])  # la terminal la ejecuta tarde
    await tm._tick_once_account(ACCOUNT)

    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    assert [(t.leg, t.chat_id) for t in legs] == [("tp1", CHAT)]
    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT, raw_text="TRADE INVALID", correction=None)
    assert result["results"][0]["status"] == "closed"
    assert sim.positions == {}


@pytest.mark.asyncio
async def test_be_on_a_group_with_tp1_open_and_runner_pending_protects_both(monkeypatch):
    """Re-revision B: tp1 abierto, runner sin confirmar, el canal pide BE: tp1
    va a BE ya, y el runner al adoptarse."""
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    real = sim.order_send
    calls = {"n": 0}

    def order_send(req):
        calls["n"] += 1
        if calls["n"] == 2:
            time.sleep(0.5)
        return real(req)

    sim.order_send = order_send
    group_id = await _open_sell_fast(tm)
    sim.price = 4133.0  # en ganancia (SELL): BE es valido

    result = await tm.apply_mgmt_action(action="move_sl_be_now", chat_id=CHAT, raw_text="move SL to BE",
                                        correction=None)

    assert result["results"][0]["status"] == "applied"
    tp1 = next(t for t in tm.trades.values() if t.leg == "tp1")
    assert tp1.be_applied
    await _wait_late_fill(sim, n=2)
    await tm._tick_once_account(ACCOUNT)
    runner = next(t for t in tm.trades.values() if t.leg == "runner")
    assert runner.be_applied and sim.positions[runner.ticket]["sl"] == runner.entry_price


@pytest.mark.asyncio
async def test_no_fresh_late_runner_after_the_channel_asked_for_be(monkeypatch):
    """Re-revision C."""
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_fills_late(sim)
    group_id = await _open_sell_fast(tm)
    await tm.apply_mgmt_action(action="close_partial_now", chat_id=CHAT, raw_text="secure profits", correction=None)

    await _wait_late_fill(sim)
    await tm._tick_once_account(ACCOUNT)

    assert [t.leg for t in tm.trades.values() if t.group_id == group_id] == ["tp1"]
    assert "no se abrio" in notifier.of("group_opened")[0]["message"]


@pytest.mark.asyncio
async def test_no_changes_retcode_is_not_reported_as_a_failed_sync(monkeypatch):
    """Re-revision D: MT5 responde 10025 (sin cambios) cuando los niveles solo
    difieren por redondeo -- no es un fallo que avisar al canal."""
    sim = SimuladorMT5()
    sim.price = 4134.96
    tm, notifier = _manager(monkeypatch, sim)
    _first_order_fills_late(sim)
    group_id = await _open_sell_fast(tm)
    await tm.update_group_signal(group_id, sl=4154.0, tp1=4100.0, tp2=4074.0)
    await _wait_late_fill(sim)
    real = sim.order_send

    def order_send(req):
        if req.get("action") == 6:
            return type("R", (), {"retcode": 10025, "order": 0})()
        return real(req)

    sim.order_send = order_send
    await tm._tick_once_account(ACCOUNT)

    assert "No se pudieron aplicar" not in notifier.of("group_opened")[0]["message"]
