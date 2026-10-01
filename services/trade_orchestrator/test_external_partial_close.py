"""
Auditoria de cierres parciales externos (el usuario cerrando manualmente una
porcion de una posicion desde MT5, sin pasar por el sistema). Caso real:
grupos 168/172 (2026-09-2x) tuvieron cierres parciales manuales que el audit
log nunca registro porque el ticket seguia vivo en positions_get -- solo se
detectaban cierres TOTALES (ticket desaparecido). Ver tambien el bug
relacionado de _get_close_deal_info tomando solo el ultimo deal de salida en
vez de sumar todos los deals nuevos desde la ultima auditoria.
"""
import pytest

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager

ACCOUNT = {"name": "demo", "active": True, "host": "x", "port": 1, "fixed_lot": 0.10}
CHAT_ID = "-1001234567890"


class DummyExecutor:
    def __init__(self, sim):
        self.sim = sim
        self.accounts = [ACCOUNT]
        self.magic = 987654

    def _client_for(self, account):
        return self.sim


class DummyNotifier:
    def __init__(self):
        self.events = []

    async def notify_trade_event(self, event, **kwargs):
        self.events.append((event, kwargs))

    async def notify(self, target, message):
        pass


@pytest.mark.asyncio
async def test_manual_partial_close_is_detected_and_audited():
    """The exact production gap: user closes 50% of the runner manually from
    MT5. The ticket stays alive with less volume -- must still generate an
    external_partial_close_detected event with the real P&L, not silence."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    runner = next(t for t in tm.trades.values() if t.leg == "runner")

    # First tick establishes the baseline last_known_volume -- no event yet.
    await tm._tick_once_account(ACCOUNT)
    assert runner.last_known_volume == pytest.approx(0.10)
    assert not any(e == "external_partial_close_detected" for e, _ in notifier.events)

    # User manually closes 50% from MT5 (not via the system's close_partial_now/TP2 paths).
    sim.partial_close(ACCOUNT, runner.ticket, 50, profit=18.24)
    await tm._tick_once_account(ACCOUNT)

    partial_events = [kw for e, kw in notifier.events if e == "external_partial_close_detected"]
    assert len(partial_events) == 1
    ev = partial_events[0]
    assert ev["group_id"] == group_id
    assert ev["leg"] == "runner"
    assert ev["closed_volume"] == pytest.approx(0.05)
    assert ev["remaining_volume"] == pytest.approx(0.05)
    assert ev["pnl_money"] == pytest.approx(18.24)

    # Ticket must still be tracked (position wasn't fully closed).
    assert runner.ticket in tm.trades
    assert runner.last_known_volume == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_manual_partial_close_not_duplicated_across_ticks():
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    runner = next(t for t in tm.trades.values() if t.leg == "runner")

    await tm._tick_once_account(ACCOUNT)  # baseline
    sim.partial_close(ACCOUNT, runner.ticket, 50, profit=18.24)
    await tm._tick_once_account(ACCOUNT)
    await tm._tick_once_account(ACCOUNT)  # nothing changed -- must not re-fire
    await tm._tick_once_account(ACCOUNT)

    partial_events = [kw for e, kw in notifier.events if e == "external_partial_close_detected"]
    assert len(partial_events) == 1


@pytest.mark.asyncio
async def test_tp2_partial_close_not_misreported_as_external():
    """The system's OWN TP2 partial close reduces volume too -- must not be
    reclassified as an external close by the new volume-drop detector."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    runner = next(t for t in tm.trades.values() if t.leg == "runner")
    runner.be_applied = True  # TP2 partial only runs once be_applied

    await tm._tick_once_account(ACCOUNT)  # baseline volume

    sim.price = 2530.0  # reaches tp2_price
    sim.positions[runner.ticket]["price_current"] = sim.price
    await tm._tick_once_account(ACCOUNT)

    assert runner.tp2_partial_applied is True
    partial_events = [kw for e, kw in notifier.events if e == "external_partial_close_detected"]
    assert partial_events == []
    tp2_events = [kw for e, kw in notifier.events if e == "tp2_partial_closed"]
    assert len(tp2_events) == 1


@pytest.mark.asyncio
async def test_close_partial_now_not_misreported_as_external():
    """A management-driven close_partial_now (Telegram command) reduces
    volume too -- must not be flagged as an unexplained external close."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    await tm._tick_once_account(ACCOUNT)  # baseline

    await tm.apply_mgmt_action(action="close_partial_now", chat_id=CHAT_ID, raw_text="Close 50% now", correction=None)
    await tm._tick_once_account(ACCOUNT)

    partial_events = [kw for e, kw in notifier.events if e == "external_partial_close_detected"]
    assert partial_events == []


@pytest.mark.asyncio
async def test_multiple_external_partial_closes_before_full_close_sum_all_deals():
    """Real case (group 169, 2026-09-28): the user closed the runner in TWO
    manual partials before it fully closed. The final audit (when the ticket
    disappears) must sum BOTH partials' profit, not just the last one."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    runner = next(t for t in tm.trades.values() if t.leg == "runner")

    await tm._tick_once_account(ACCOUNT)  # baseline

    # First manual partial: close 50%, +33.66
    sim.partial_close(ACCOUNT, runner.ticket, 50, profit=33.66)
    await tm._tick_once_account(ACCOUNT)
    first = [kw for e, kw in notifier.events if e == "external_partial_close_detected"]
    assert len(first) == 1
    assert first[0]["pnl_money"] == pytest.approx(33.66)

    # Second manual close: the rest, +33.26 -- fully closes the ticket.
    sim.partial_close(ACCOUNT, runner.ticket, 100, profit=33.26)
    await tm._tick_once_account(ACCOUNT)

    full_close_events = [kw for e, kw in notifier.events if e == "external_close_detected"]
    assert len(full_close_events) == 1
    # Must be exactly this deal's profit (33.26), not double-counting the
    # already-audited first partial (last_audited_deal_time excludes it).
    assert full_close_events[0]["pnl_money"] == pytest.approx(33.26)

    total_audited = sum(kw["pnl_money"] for e, kw in notifier.events
                         if e in ("external_partial_close_detected", "external_close_detected"))
    assert total_audited == pytest.approx(33.66 + 33.26)
