import json
import pytest

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager


class DummyExecutor:
    def __init__(self, sim, accounts):
        self.sim = sim
        self.accounts = accounts
        self.magic = 987654

    def _client_for(self, account):
        return self.sim


class DummyNotifier:
    async def notify_trade_event(self, event, **kwargs):
        pass

    async def notify(self, target, message):
        pass


ACCOUNTS = [{"name": "demo", "active": True, "host": "x", "port": 1, "fixed_lot": 0.05}]


@pytest.mark.asyncio
async def test_fast_signal_opens_guard_pair_with_temporary_tp_then_full_signal_completes_it():
    """
    A fast signal's tp1_leg gets a temporary protective TP (DEFAULT_TP_XAUUSD_PIPS,
    default 100 in test env) so it isn't left with only an SL if the full signal
    (real TP1/TP2) never arrives. runner_leg still opens with no TP — it's the
    leg designed to run, unaffected by this change. If the full signal does
    arrive later, it overwrites the temporary TP with the real TP1/TP2.
    """
    from services.trade_orchestrator.app import handle_signal_fields

    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim, ACCOUNTS), notifier=DummyNotifier())

    fast_fields = {"symbol": "XAUUSD", "direction": "BUY", "fast": "true", "sl": "", "tps": "[]", "entry_range": ""}
    await handle_signal_fields(fast_fields, tm, ACCOUNTS)

    assert len(tm.trades) == 2
    group_id = next(iter(tm.trades.values())).group_id
    by_leg = {t.leg: t for t in tm.trades.values()}
    # tp1_price is tracked in-memory on both legs (it's the reference the runner's
    # trailing math uses once tp2 arrives too) but only tp1_leg actually carries a
    # real TP on MT5 — runner_leg's MT5 tp stays 0.0, unaffected by this change.
    assert by_leg["tp1"].tp1_price is not None  # temporary protective TP
    assert by_leg["tp1"].tp1_price > 2500.0  # BUY: TP above entry
    assert sim.positions[by_leg["tp1"].ticket]["tp"] == by_leg["tp1"].tp1_price
    assert sim.positions[by_leg["runner"].ticket]["tp"] == 0.0

    full_fields = {
        "symbol": "XAUUSD", "direction": "BUY", "fast": "false",
        "sl": "2490.0", "tps": json.dumps([2510.0, 2530.0]), "entry_range": "",
    }
    await handle_signal_fields(full_fields, tm, ACCOUNTS)

    assert len(tm.trades) == 2  # same two legs, updated in place — not 4
    for t in tm.trades.values():
        assert t.group_id == group_id
        assert t.tp1_price == 2510.0  # temporary TP overwritten with the real one
        assert t.tp2_price == 2530.0
        assert t.planned_sl == 2490.0


@pytest.mark.asyncio
async def test_fast_signal_tp1_leg_keeps_temporary_tp_when_full_signal_never_arrives():
    """The exact production gap this protects against: a fast signal opens the
    pair, no full signal ever follows. tp1_leg must not be left with only an SL —
    it keeps its temporary protective TP on MT5 (verified via order_send req)."""
    from services.trade_orchestrator.app import handle_signal_fields

    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim, ACCOUNTS), notifier=DummyNotifier())

    fast_fields = {"symbol": "XAUUSD", "direction": "SELL", "fast": "true", "sl": "", "tps": "[]", "entry_range": ""}
    await handle_signal_fields(fast_fields, tm, ACCOUNTS)

    by_leg = {t.leg: t for t in tm.trades.values()}
    tp1_leg = by_leg["tp1"]
    assert tp1_leg.tp1_price is not None
    assert tp1_leg.tp1_price < 2500.0  # SELL: TP below entry

    pos = sim.positions[tp1_leg.ticket]
    assert pos["tp"] == tp1_leg.tp1_price  # actually sent to MT5, not just tracked in-memory
    assert pos["sl"] == tp1_leg.planned_sl


@pytest.mark.asyncio
async def test_full_signal_without_prior_fast_opens_group_directly():
    from services.trade_orchestrator.app import handle_signal_fields

    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim, ACCOUNTS), notifier=DummyNotifier())

    full_fields = {
        "symbol": "XAUUSD", "direction": "BUY", "fast": "false",
        "sl": "2490.0", "tps": json.dumps([2510.0, 2530.0]), "entry_range": "",
    }
    await handle_signal_fields(full_fields, tm, ACCOUNTS)

    assert len(tm.trades) == 2
    for t in tm.trades.values():
        assert t.tp1_price == 2510.0
        assert t.tp2_price == 2530.0
