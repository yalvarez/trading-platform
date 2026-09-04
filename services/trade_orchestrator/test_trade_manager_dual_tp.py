import time
import pytest

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager, ManagedTrade


class DummyExecutor:
    """Minimal stand-in for MT5Executor — exposes only what TradeManager needs."""
    def __init__(self, sim):
        self.sim = sim
        self.accounts = [{"name": "demo", "active": True, "host": "x", "port": 1}]
        self.magic = 987654
        self.default_deviation = 50
        self.comment_prefix = "TM"

    def _client_for(self, account):
        return self.sim


class DummyNotifier:
    def __init__(self):
        self.events = []

    async def notify_trade_event(self, event, **kwargs):
        self.events.append((event, kwargs))

    async def notify(self, target, message):
        pass


ACCOUNT = {"name": "demo", "active": True, "host": "x", "port": 1}


@pytest.mark.asyncio
async def test_open_group_opens_two_positions_with_shared_group_id():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    assert group_id is not None
    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    assert len(legs) == 2
    leg_names = {t.leg for t in legs}
    assert leg_names == {"tp1", "runner"}
    for t in legs:
        assert t.planned_sl == 2490.0
        assert t.tp1_price == 2510.0
        assert t.tp2_price == 2530.0


@pytest.mark.asyncio
async def test_open_group_aborts_when_tp2_not_above_tp1_for_buy():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2505.0)

    assert group_id is None
    assert len(tm.trades) == 0


@pytest.mark.asyncio
async def test_open_group_without_tp2_opens_fast_guard_pair():
    """Fast signal (no real TP yet): both legs open with a temporary SL, tp1/tp2 unset."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2470.0, tp1=None, tp2=None)

    assert group_id is not None
    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    assert len(legs) == 2
    for t in legs:
        assert t.tp1_price is None
        assert t.tp2_price is None
        assert t.planned_sl == 2470.0


@pytest.mark.asyncio
async def test_update_group_signal_fills_in_tp1_tp2_on_fast_guard_pair():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2470.0, tp1=None, tp2=None)

    await tm.update_group_signal(group_id, sl=2490.0, tp1=2510.0, tp2=2530.0)

    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    for t in legs:
        assert t.planned_sl == 2490.0
        assert t.tp1_price == 2510.0
        assert t.tp2_price == 2530.0
    tp1_leg = next(t for t in legs if t.leg == "tp1")
    runner_leg = next(t for t in legs if t.leg == "runner")
    tp1_pos = sim.positions_get(ticket=tp1_leg.ticket)[0]
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert tp1_pos.tp == 2510.0
    # runner never gets a real MT5 TP (dual-TP spec section 4)
    assert runner_pos.tp != 2530.0


@pytest.mark.asyncio
async def test_find_active_group_for_symbol_returns_most_recent():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    found = tm.find_active_group_for_symbol("XAUUSD")
    assert found == g1

    found_none = tm.find_active_group_for_symbol("EURUSD")
    assert found_none is None


@pytest.mark.asyncio
async def test_tick_moves_runner_sl_to_be_when_tp1_leg_closes():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")

    # Simulate TP1 leg having closed (no longer in MT5 positions)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    del sim.positions[tp1_leg.ticket]

    await tm._tick_once_account(ACCOUNT)

    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2500.0) < 1e-6  # moved to entry price (BE)
    assert tm.trades[runner_leg.ticket].be_applied is True
    assert tp1_leg.ticket not in tm.trades


@pytest.mark.asyncio
async def test_trailing_raises_runner_sl_proportionally_to_peak():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # applies BE

    # unit = tp2 - tp1 = 20. Move price to 60% of unit past tp1 = 2510 + 12 = 2522
    sim.positions[runner_leg.ticket]["price_current"] = 2522.0
    sim.price = 2522.0
    await tm._tick_once_account(ACCOUNT)

    # peak_multiple = 0.6, SL = tp1 + (0.6 * 20)/3 = 2510 + 4 = 2514
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2514.0) < 1e-6
    assert abs(tm.trades[runner_leg.ticket].peak_multiple - 0.6) < 1e-9


@pytest.mark.asyncio
async def test_trailing_sl_never_decreases_on_price_pullback():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)

    sim.positions[runner_leg.ticket]["price_current"] = 2522.0  # peak 60%
    sim.price = 2522.0
    await tm._tick_once_account(ACCOUNT)
    sl_at_peak = sim.positions_get(ticket=runner_leg.ticket)[0].sl

    sim.positions[runner_leg.ticket]["price_current"] = 2515.0  # pulls back to 25%
    sim.price = 2515.0
    await tm._tick_once_account(ACCOUNT)
    sl_after_pullback = sim.positions_get(ticket=runner_leg.ticket)[0].sl

    assert sl_after_pullback == sl_at_peak  # never decreases
    assert tm.trades[runner_leg.ticket].peak_multiple == 0.6  # peak retained


@pytest.mark.asyncio
async def test_trailing_extrapolates_unit_beyond_tp2():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)

    # price at 150% of unit past tp1 = 2510 + 30 = 2540 (beyond tp2=2530)
    sim.positions[runner_leg.ticket]["price_current"] = 2540.0
    sim.price = 2540.0
    await tm._tick_once_account(ACCOUNT)

    # SL = tp1 + (1.5 * 20)/3 = 2510 + 10 = 2520
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2520.0) < 1e-6


@pytest.mark.asyncio
async def test_apply_mgmt_action_close_now_closes_both_legs_before_tp1():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    result = await tm.apply_mgmt_action(action="close_now", symbol="XAUUSD", raw_text="Close now", correction=None)

    assert result["status"] == "closed"
    remaining = [t for t in tm.trades.values() if t.group_id == group_id]
    assert remaining == []


@pytest.mark.asyncio
async def test_apply_mgmt_action_no_active_trade_returns_no_active_trade():
    sim = SimuladorMT5()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    result = await tm.apply_mgmt_action(action="close_now", symbol="EURUSD", raw_text="Close now", correction=None)

    assert result["status"] == "no_active_trade"


@pytest.mark.asyncio
async def test_apply_mgmt_action_move_sl_be_now_forces_be_when_worse():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    # SL still at original 2490 (TP1 not hit yet) — worse than BE (2500 entry)

    result = await tm.apply_mgmt_action(action="move_sl_be_now", symbol="XAUUSD", raw_text="adjust sl to entry", correction=None)

    assert result["status"] == "applied"
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2500.0) < 1e-6
    assert tm.trades[runner_leg.ticket].be_applied is True


@pytest.mark.asyncio
async def test_apply_mgmt_action_move_sl_be_now_noop_when_already_better():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # BE applied at 2500
    sim.positions[runner_leg.ticket]["price_current"] = 2522.0
    sim.price = 2522.0
    await tm._tick_once_account(ACCOUNT)  # trailing raises SL above BE
    sl_before = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert sl_before > 2500.0

    result = await tm.apply_mgmt_action(action="move_sl_be_now", symbol="XAUUSD", raw_text="secure be", correction=None)

    assert result["status"] == "already_satisfied"
    sl_after = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert sl_after == sl_before  # unchanged, never reduced


@pytest.mark.asyncio
async def test_apply_mgmt_action_note_sl_hit_does_not_touch_mt5():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    legs_before = {t.ticket: (sim.positions_get(ticket=t.ticket)[0].sl) for t in tm.trades.values() if t.group_id == group_id}

    result = await tm.apply_mgmt_action(action="note_sl_hit", symbol="XAUUSD", raw_text="HIT SL, recovery incoming", correction=None)

    assert result["status"] == "noted"
    for ticket, sl_before in legs_before.items():
        assert sim.positions_get(ticket=ticket)[0].sl == sl_before


@pytest.mark.asyncio
async def test_apply_mgmt_action_signal_correction_updates_tp1_on_mt5_and_tp2_reference_only():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")

    result = await tm.apply_mgmt_action(action="signal_correction", symbol="XAUUSD", raw_text="TP 2 IS 4687 Correction", correction={"field": "tp2", "value": 4687.0})

    assert result["status"] == "applied"
    assert tm.trades[runner_leg.ticket].tp2_price == 4687.0
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert runner_pos.tp != 4687.0  # never sent to MT5 for the runner leg


@pytest.mark.asyncio
async def test_apply_mgmt_action_ignore_is_a_noop():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    result = await tm.apply_mgmt_action(action="ignore", symbol="XAUUSD", raw_text="spam your feedbacks", correction=None)

    assert result["status"] == "ignored"


# --- Review fix 1: update_group_signal must not regress a trailed/BE'd SL ---

@pytest.mark.asyncio
async def test_signal_correction_after_trailing_does_not_regress_sl_and_trailing_still_progresses():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # BE applied at 2500

    # Trail forward: price at 150% of unit past tp1 = 2510 + 30 = 2540 -> SL = 2510 + 10 = 2520
    sim.positions[runner_leg.ticket]["price_current"] = 2540.0
    sim.price = 2540.0
    await tm._tick_once_account(ACCOUNT)
    sl_after_trailing = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert abs(sl_after_trailing - 2520.0) < 1e-6

    # A signal_correction that only touches tp2 must NOT regress the live SL
    # back down to the original planned_sl (2490).
    result = await tm.apply_mgmt_action(
        action="signal_correction", symbol="XAUUSD", raw_text="TP2 correction",
        correction={"field": "tp2", "value": 4687.0},
    )
    assert result["status"] == "applied"
    sl_after_correction = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert sl_after_correction == sl_after_trailing  # unchanged, never regressed
    assert sl_after_correction >= 2500.0  # still at/above BE, not stranded below entry

    # Trailing must still be able to progress afterward with a further price move.
    # unit is now huge (tp2=4687, tp1=2510), so even a further advance keeps the
    # multiple tiny — but peak_multiple must have been rescaled (not stuck at the
    # old 1.5), so a genuine further advance still raises SL further.
    sim.positions[runner_leg.ticket]["price_current"] = 2600.0
    sim.price = 2600.0
    await tm._tick_once_account(ACCOUNT)
    sl_after_further_move = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert sl_after_further_move >= sl_after_correction  # trailing not dead/frozen


@pytest.mark.asyncio
async def test_update_group_signal_skips_sl_write_when_current_is_already_better():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # BE applied at 2500 (better than planned_sl=2490)

    # Directly call update_group_signal with the (unchanged) original sl=2490 —
    # this must not regress the runner's live SL of 2500 back down.
    await tm.update_group_signal(group_id, sl=2490.0, tp1=None, tp2=None)

    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2500.0) < 1e-6


# --- Review fix 2: find_active_group_for_symbol must tie-break on group_id ---

@pytest.mark.asyncio
async def test_find_active_group_for_symbol_tie_breaks_on_group_id_when_opened_ts_equal():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    assert g2 > g1

    # Force an identical opened_ts on every trade in both groups, simulating
    # coarse timer resolution (e.g. ~15.6ms on Windows) causing a tie.
    same_ts = 12345.0
    for t in tm.trades.values():
        t.opened_ts = same_ts

    found = tm.find_active_group_for_symbol("XAUUSD")
    assert found == g2  # the higher group_id (the actually-newer group) wins


# --- Review fix 3: entry_price=None must not raise ---

@pytest.mark.asyncio
async def test_move_sl_be_now_with_missing_entry_price_returns_failed_not_raise():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    runner_leg.entry_price = None

    result = await tm.apply_mgmt_action(action="move_sl_be_now", symbol="XAUUSD", raw_text="be now", correction=None)

    assert result["status"] == "failed"
    assert result.get("reason") == "no_entry_price"


@pytest.mark.asyncio
async def test_on_tp1_leg_closed_with_missing_entry_price_does_not_raise():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    runner_leg.entry_price = None
    del sim.positions[tp1_leg.ticket]

    # Must not raise even though runner.entry_price is None.
    await tm._tick_once_account(ACCOUNT)

    assert tm.trades[runner_leg.ticket].be_applied is False  # BE was skipped, not crashed through


class FakeConfigProvider:
    """Minimal config_provider stub for entry-range-gate tests — fast timings."""
    def __init__(self, **overrides):
        self.values = {"ENTRY_WAIT_SECONDS": 1, "ENTRY_POLL_MS": 20, "TOLERANCE_PIPS": 30, **overrides}

    def get(self, key, default=None):
        return self.values.get(key, default)


@pytest.mark.asyncio
async def test_open_group_executes_immediately_when_price_already_in_entry_range():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), config_provider=FakeConfigProvider())

    group_id = await tm.open_group(
        ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0,
        entry_range=(2495.0, 2505.0),
    )

    assert group_id is not None
    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    assert len(legs) == 2


@pytest.mark.asyncio
async def test_open_group_aborts_when_price_already_past_entry_range():
    sim = SimuladorMT5()
    sim.price = 2520.0  # already past the range's high end for a BUY
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), config_provider=FakeConfigProvider())

    group_id = await tm.open_group(
        ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2530.0, tp2=2550.0,
        entry_range=(2495.0, 2505.0),
    )

    assert group_id is None
    assert len(tm.trades) == 0


@pytest.mark.asyncio
async def test_open_group_waits_and_executes_once_price_enters_entry_range():
    sim = SimuladorMT5()
    sim.price = 2490.0  # starts below the range, not yet past it favorably — worth waiting

    async def move_price_into_range_soon():
        import asyncio
        await asyncio.sleep(0.05)
        sim.price = 2500.0  # now inside [2495, 2505]

    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), config_provider=FakeConfigProvider())

    import asyncio
    mover = asyncio.create_task(move_price_into_range_soon())
    group_id = await tm.open_group(
        ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2520.0, tp2=2540.0,
        entry_range=(2495.0, 2505.0),
    )
    await mover

    assert group_id is not None
    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    assert len(legs) == 2
    for t in legs:
        assert 2495.0 <= t.entry_price <= 2505.0


@pytest.mark.asyncio
async def test_open_group_aborts_when_price_never_enters_entry_range_within_wait():
    sim = SimuladorMT5()
    sim.price = 2500.0  # inside a range that has nothing to do with the target range
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), config_provider=FakeConfigProvider(ENTRY_WAIT_SECONDS=1))

    # target range is far from current price but not "already past" (below entry_lo, not
    # past entry_hi for a BUY) -- it should wait, time out, and abort with no positions opened.
    group_id = await tm.open_group(
        ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2440.0, tp1=2470.0, tp2=2480.0,
        entry_range=(2460.0, 2465.0),
    )

    assert group_id is None
    assert len(tm.trades) == 0


@pytest.mark.asyncio
async def test_open_group_recovers_from_transient_empty_tick_on_first_price_read():
    """
    Reproduces a real production incident: mt5linux opens a fresh RPyC
    connection per call (no persistent session), so symbol_select immediately
    followed by tick_price can race the MT5 terminal's own state propagation
    under Wine -- tick_price then returns 0.0 with no exception raised (so
    PooledMT5Client's reconnect-on-exception logic never triggers). A real
    TradePulse signal was silently dropped by this exact sequence. open_group
    must retry the initial price read instead of aborting on the first empty tick.
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    calls = {"n": 0}
    real_tick_price = sim.tick_price

    def flaky_tick_price(symbol, direction):
        calls["n"] += 1
        if calls["n"] == 1:
            return 0.0  # simulates the transient empty tick seen in production
        return real_tick_price(symbol, direction)

    sim.tick_price = flaky_tick_price
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="SELL", sl=2510.0, tp1=2480.0, tp2=2460.0)

    assert group_id is not None
    assert calls["n"] == 2
    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    assert len(legs) == 2


@pytest.mark.asyncio
async def test_open_group_aborts_after_exhausting_price_retries():
    """If tick_price stays empty across every retry, open_group still aborts
    cleanly (no positions opened) rather than retrying forever."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    sim.tick_price = lambda symbol, direction: 0.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="SELL", sl=2510.0, tp1=2480.0, tp2=2460.0)

    assert group_id is None
    assert len(tm.trades) == 0
