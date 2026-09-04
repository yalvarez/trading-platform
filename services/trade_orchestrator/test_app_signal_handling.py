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
async def test_fast_signal_runner_still_gets_be_and_trailing_when_full_signal_never_arrives():
    """
    The user's actual question: if the full signal never arrives, does the
    runner still get the same BE + proportional trailing behavior as with a
    full signal? Yes — tp1_leg's temporary TP and a synthetic tp2 (1 point
    past it, same direction) give the runner a valid unit > 0 from the start.
    The unit is purely a scaling constant in SL = tp1 + (peak*unit)/3 with
    peak = advance/unit — it cancels out algebraically, so any unit > 0
    yields the same SL for the same price advance. This test asserts the SL
    value directly to prove that, independent of which internal unit was used.
    """
    from services.trade_orchestrator.app import handle_signal_fields

    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim, ACCOUNTS), notifier=DummyNotifier())

    fast_fields = {"symbol": "XAUUSD", "direction": "BUY", "fast": "true", "sl": "", "tps": "[]", "entry_range": ""}
    await handle_signal_fields(fast_fields, tm, ACCOUNTS)

    by_leg = {t.leg: t for t in tm.trades.values()}
    tp1_leg, runner_leg = by_leg["tp1"], by_leg["runner"]
    assert runner_leg.tp1_price is not None and runner_leg.tp2_price is not None

    # tp1_leg closes on its temporary TP (simulated: removed from MT5 positions)
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNTS[0])
    assert runner_leg.be_applied is True
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - runner_leg.entry_price) < 1e-6  # moved to BE

    # Price advances well past tp1_price -- trailing must kick in exactly like
    # the full-signal path (same SL = tp1 + advance/3 formula), not stay frozen at BE.
    advance = 30.0
    sim.price = runner_leg.tp1_price + advance
    sim.positions[runner_leg.ticket]['price_current'] = sim.price
    await tm._tick_once_account(ACCOUNTS[0])

    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    expected_sl = runner_leg.tp1_price + advance / 3.0
    assert abs(runner_pos.sl - expected_sl) < 1e-6
    assert runner_pos.sl > runner_leg.entry_price  # progressed beyond BE


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
