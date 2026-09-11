import asyncio
import time
import os
import tempfile
import json
import pytest

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager, ManagedTrade, MAGIC


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
async def test_open_group_defaults_chat_id_to_none_when_not_passed():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    assert len(legs) == 2
    for t in legs:
        assert t.chat_id is None


@pytest.mark.asyncio
async def test_open_group_propagates_chat_id_to_both_legs():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    group_id = await tm.open_group(
        ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0,
        chat_id="-1001234567890",
    )

    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    assert len(legs) == 2
    for t in legs:
        assert t.chat_id == "-1001234567890"


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
async def test_update_group_signal_applies_real_sl_even_when_narrower_than_fast_default():
    """
    Real production bug: the fast signal opens both legs with a wide default
    protective SL (e.g. 100 pips). When the full signal arrives shortly after
    with the real SL — which is very often numerically "worse" (narrower/
    closer to price) than that wide default — the never-regress guard in
    update_group_signal compared it against the fast default SL and refused
    to write it, leaving both legs stuck on the fast default forever. That
    guard exists to protect a runner's SL after BE/trailing already moved it
    (see be_applied), not to protect an arbitrary fast placeholder. Neither
    leg has be_applied/trailing yet here, so the real SL must always win.
    """
    sim = SimuladorMT5()
    sim.price = 4434.53
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=4424.53, tp1=None, tp2=None)

    await tm.update_group_signal(group_id, sl=4410.0, tp1=4460.0, tp2=4490.0)

    legs = [t for t in tm.trades.values() if t.group_id == group_id]
    tp1_leg = next(t for t in legs if t.leg == "tp1")
    runner_leg = next(t for t in legs if t.leg == "runner")
    assert tp1_leg.planned_sl == 4410.0
    assert runner_leg.planned_sl == 4410.0
    tp1_pos = sim.positions_get(ticket=tp1_leg.ticket)[0]
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert tp1_pos.sl == 4410.0
    assert runner_pos.sl == 4410.0


# --- apply_mgmt_action: chat_id-scoped resolution (chat_id-scoping spec section 5) ---

CHAT_ID = "-1001234567890"


@pytest.mark.asyncio
async def test_apply_mgmt_action_no_active_trade_for_chat_returns_no_active_trade_and_notifies():
    sim = SimuladorMT5()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="Close now", correction=None)

    assert result == {"status": "no_active_trade"}
    events = [kwargs for event, kwargs in tm.notifier.events if event == "mgmt_no_active_trade"]
    assert len(events) == 1
    assert "Close now" in events[0]["message"]
    assert events[0]["chat_id"] == CHAT_ID
    assert events[0]["action"] == "close_now"


@pytest.mark.asyncio
async def test_apply_mgmt_action_close_now_closes_single_group_before_tp1():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="Close now", correction=None)

    assert result == {"status": "completed", "results": [{"group_id": group_id, "status": "closed"}]}
    remaining = [t for t in tm.trades.values() if t.group_id == group_id]
    assert remaining == []


@pytest.mark.asyncio
async def test_apply_mgmt_action_close_now_closes_all_groups_of_the_same_chat():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    other_chat_group = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="other-chat")

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="close both", correction=None)

    assert result == {"status": "completed", "results": [
        {"group_id": g1, "status": "closed"},
        {"group_id": g2, "status": "closed"},
    ]}
    # The other chat's group must be untouched.
    remaining_other = [t for t in tm.trades.values() if t.group_id == other_chat_group]
    assert len(remaining_other) == 2


@pytest.mark.asyncio
async def test_apply_mgmt_action_close_now_isolates_a_real_exception_in_one_group():
    """
    A real exception (not just a failed retcode) while processing one
    group must not abort the whole request -- the other group of the same
    chat_id must still be processed and reported (chat_id-scoping spec
    section 5, per-group isolation).
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    g1_ticket = next(t.ticket for t in tm.trades.values() if t.group_id == g1)
    real_partial_close = sim.partial_close

    def _boom(account, ticket, pct):
        if ticket == g1_ticket:
            raise RuntimeError("simulated RPyC network failure")
        return real_partial_close(account, ticket, pct)

    sim.partial_close = _boom

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="close both", correction=None)

    results_by_group = {r["group_id"]: r for r in result["results"]}
    assert results_by_group[g1] == {"group_id": g1, "status": "failed", "reason": "exception"}
    assert results_by_group[g2] == {"group_id": g2, "status": "closed"}
    # g2 must have actually been closed in spite of g1's exception.
    remaining_g2 = [t for t in tm.trades.values() if t.group_id == g2]
    assert remaining_g2 == []


@pytest.mark.asyncio
async def test_apply_mgmt_action_close_now_one_group_fails_partial_close_other_still_closes():
    """
    partial_close returns a plain bool (SimuladorMT5.partial_close and the real
    MT5Client.partial_close both do -- no .retcode involved). If the broker
    rejects the close for a group's leg (returns False, no exception raised),
    that leg must stay tracked in self.trades (still open, still needs
    mechanical management) and the group's result must be "failed" with
    reason "partial_close_rejected" instead of "closed" -- the group must
    NOT be closed in the store either. A sibling group of the same chat_id
    whose partial_close succeeds must still close normally and be reported
    "closed", independent of the other group's failure (chat_id-scoping spec
    section 5, per-group isolation -- this is a different failure mode than
    a raised exception, and both must coexist).
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g_fail = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    g_ok = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    fail_tickets = {t.ticket for t in tm.trades.values() if t.group_id == g_fail}
    ok_tickets = {t.ticket for t in tm.trades.values() if t.group_id == g_ok}
    real_partial_close = sim.partial_close

    def _maybe_reject(account, ticket, pct):
        if ticket in fail_tickets:
            return False
        return real_partial_close(account, ticket, pct)

    sim.partial_close = _maybe_reject

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="close both", correction=None)

    results_by_group = {r["group_id"]: r for r in result["results"]}
    assert results_by_group[g_fail] == {"group_id": g_fail, "status": "failed", "reason": "partial_close_rejected"}
    assert results_by_group[g_ok] == {"group_id": g_ok, "status": "closed"}

    remaining_tickets = set(tm.trades.keys())
    assert fail_tickets.issubset(remaining_tickets)
    assert remaining_tickets.isdisjoint(ok_tickets)


@pytest.mark.asyncio
async def test_close_partial_now_applies_default_50_percent_when_no_percent_given():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    legs_before = [t for t in tm.trades.values() if t.group_id == group_id]
    tickets_before = {t.ticket for t in legs_before}

    result = await tm.apply_mgmt_action(action="close_partial_now", chat_id=CHAT_ID, raw_text="cierra parte", correction=None)

    assert result["status"] == "completed"
    # Both legs still open (partial, not full close) -- tickets unchanged.
    remaining_tickets = {t.ticket for t in tm.trades.values() if t.group_id == group_id}
    assert remaining_tickets == tickets_before
    for ticket in tickets_before:
        pos = sim.positions[ticket]
        assert pos["volume"] == pytest.approx(0.005)  # 50% of the 0.01 default fixed_lot


@pytest.mark.asyncio
async def test_close_partial_now_applies_explicit_percent():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    legs = [t for t in tm.trades.values() if t.group_id == group_id]

    await tm.apply_mgmt_action(action="close_partial_now", chat_id=CHAT_ID, raw_text="cierra 30%", correction=None, percent=30.0)

    for t in legs:
        pos = sim.positions[t.ticket]
        assert pos["volume"] == pytest.approx(0.01 * 0.7)


@pytest.mark.asyncio
async def test_close_partial_now_notifies_success_event_with_both_channel():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    await tm.apply_mgmt_action(action="close_partial_now", chat_id=CHAT_ID, raw_text="cierra 30%", correction=None, percent=30.0)

    events = [(event, kwargs) for event, kwargs in tm.notifier.events if event == "mgmt_close_partial_now"]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_close_partial_now_reports_failure_when_broker_rejects_a_leg():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    original_partial_close = sim.partial_close
    def failing_partial_close(account, ticket, percent):
        return False
    sim.partial_close = failing_partial_close

    result = await tm.apply_mgmt_action(action="close_partial_now", chat_id=CHAT_ID, raw_text="cierra 30%", correction=None, percent=30.0)

    events = [event for event, kwargs in tm.notifier.events if event == "mgmt_close_partial_now_failure"]
    assert len(events) == 1
    assert result["results"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_apply_mgmt_action_move_sl_be_now_applies_to_all_groups_with_mixed_outcomes():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    # Force g2's runner to already be at/above BE so it reports already_satisfied.
    g2_tp1 = next(t for t in tm.trades.values() if t.group_id == g2 and t.leg == "tp1")
    g2_runner = next(t for t in tm.trades.values() if t.group_id == g2 and t.leg == "runner")
    del sim.positions[g2_tp1.ticket]
    await tm._tick_once_account(ACCOUNT)  # BE applied to g2's runner at 2500

    result = await tm.apply_mgmt_action(action="move_sl_be_now", chat_id=CHAT_ID, raw_text="be now", correction=None)

    results_by_group = {r["group_id"]: r for r in result["results"]}
    assert results_by_group[g1]["status"] == "applied"
    assert results_by_group[g2]["status"] == "already_satisfied"
    g1_runner = next(t for t in tm.trades.values() if t.group_id == g1 and t.leg == "runner")
    assert tm.trades[g1_runner.ticket].be_applied is True


@pytest.mark.asyncio
async def test_apply_mgmt_action_move_sl_be_now_retries_a_transient_mt5_rejection():
    """
    Real production bug found live via the e2e suite (2026-09-08): n8n
    correctly classified "Set BE for zero risk" and called /mgmt/action,
    but MT5 rejected the order_send on the first attempt (price too close
    to the candidate SL, per the broker's trade_stops_level) and
    move_sl_be_now gave up after a single try -- unlike _on_tp1_leg_closed's
    automatic BE, which already retried. _force_runner_sl now retries
    internally (shared by every caller), so a transient rejection that
    clears up a moment later (e.g. price ticks forward slightly) must
    still succeed instead of being reported as a hard failure.
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")

    real_order_send = sim.order_send
    call_count = {"n": 0}

    def flaky_order_send(req):
        if req.get("action") == 6 and req.get("position") == runner_leg.ticket:
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First attempt: broker rejects (too close to live price).
                return type('OrderSendResult', (), {'retcode': 10016, 'order': 0, 'deal': 0, 'comment': 'Invalid stops'})()
        return real_order_send(req)

    sim.order_send = flaky_order_send

    result = await tm.apply_mgmt_action(action="move_sl_be_now", chat_id=CHAT_ID, raw_text="Set BE for zero risk", correction=None)

    assert result == {"status": "completed", "results": [{"group_id": group_id, "status": "applied"}]}
    assert call_count["n"] == 2  # failed once, succeeded on retry
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2500.0) < 1e-6
    assert tm.trades[runner_leg.ticket].be_applied is True


@pytest.mark.asyncio
async def test_apply_mgmt_action_move_sl_be_now_reports_no_active_trade_for_a_group_without_runner_and_notifies():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del tm.trades[runner_leg.ticket]  # simulate the runner leg missing entirely

    result = await tm.apply_mgmt_action(action="move_sl_be_now", chat_id=CHAT_ID, raw_text="be now", correction=None)

    assert result == {"status": "completed", "results": [{"group_id": group_id, "status": "no_active_trade"}]}
    events = [kwargs for event, kwargs in tm.notifier.events if event == "mgmt_no_runner_leg"]
    assert len(events) == 1
    assert events[0]["group_id"] == group_id
    assert events[0]["chat_id"] == CHAT_ID


@pytest.mark.asyncio
async def test_apply_mgmt_action_move_sl_be_now_with_missing_entry_price_reports_failed_not_raise():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    runner_leg.entry_price = None

    result = await tm.apply_mgmt_action(action="move_sl_be_now", chat_id=CHAT_ID, raw_text="be now", correction=None)

    assert result == {"status": "completed", "results": [{"group_id": group_id, "status": "failed", "reason": "no_entry_price"}]}


@pytest.mark.asyncio
async def test_apply_mgmt_action_note_sl_hit_notifies_once_per_group_without_touching_mt5():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    legs_before = {t.ticket: sim.positions_get(ticket=t.ticket)[0].sl for t in tm.trades.values()}

    result = await tm.apply_mgmt_action(action="note_sl_hit", chat_id=CHAT_ID, raw_text="HIT SL", correction=None)

    assert result == {"status": "noted", "group_ids": [g1, g2]}
    for ticket, sl_before in legs_before.items():
        assert sim.positions_get(ticket=ticket)[0].sl == sl_before


@pytest.mark.asyncio
async def test_apply_mgmt_action_signal_correction_applies_only_to_the_most_recent_group():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(
        action="signal_correction", chat_id=CHAT_ID, raw_text="TP2 IS 4687",
        correction={"field": "tp2", "value": 4687.0},
    )

    assert result == {"status": "applied", "group_id": g2}
    g1_runner = next(t for t in tm.trades.values() if t.group_id == g1 and t.leg == "runner")
    g2_runner = next(t for t in tm.trades.values() if t.group_id == g2 and t.leg == "runner")
    assert g1_runner.tp2_price == 2530.0  # untouched
    assert g2_runner.tp2_price == 4687.0


@pytest.mark.asyncio
async def test_apply_mgmt_action_signal_correction_with_invalid_field_notifies_and_returns_invalid_correction():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(
        action="signal_correction", chat_id=CHAT_ID, raw_text="volume is 2 lots",
        correction={"field": "volume", "value": 2.0},
    )

    assert result == {"status": "invalid_correction"}
    events = [kwargs for event, kwargs in tm.notifier.events if event == "mgmt_invalid_correction"]
    assert len(events) == 1
    assert "volume" in events[0]["message"]
    assert "volume is 2 lots" in events[0]["message"]


@pytest.mark.asyncio
async def test_apply_mgmt_action_ignore_is_a_noop_and_does_not_notify():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(action="ignore", chat_id=CHAT_ID, raw_text="spam your feedbacks", correction=None)

    assert result == {"status": "ignored"}


@pytest.mark.asyncio
async def test_apply_mgmt_action_unknown_action_notifies_and_returns_unknown_action():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(action="frobnicate", chat_id=CHAT_ID, raw_text="do the thing", correction=None)

    assert result == {"status": "unknown_action"}
    events = [kwargs for event, kwargs in tm.notifier.events if event == "mgmt_unknown_action"]
    assert len(events) == 1
    assert "frobnicate" in events[0]["message"]
    assert "do the thing" in events[0]["message"]


@pytest.mark.asyncio
async def test_apply_mgmt_action_account_unresolved_notifies_per_group_and_reports_failed():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    for t in tm.trades.values():
        if t.group_id == group_id:
            t.account_name = "nonexistent-account"

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="close now", correction=None)

    assert result == {"status": "completed", "results": [{"group_id": group_id, "status": "failed", "reason": "account_unresolved"}]}
    events = [kwargs for event, kwargs in tm.notifier.events if event == "mgmt_account_unresolved"]
    assert len(events) == 1
    assert events[0]["group_id"] == group_id
    assert events[0]["chat_id"] == CHAT_ID


@pytest.mark.asyncio
async def test_signal_correction_after_trailing_does_not_regress_sl_and_trailing_still_progresses():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # BE applied at 2500

    # Trail forward: price at 150% of unit past tp1 = 2510 + 30 = 2540 -> multiple=1.5,
    # entry->tp1 dist=10 -> SL = entry(2500) + 1.5*10 = 2515
    sim.positions[runner_leg.ticket]["price_current"] = 2540.0
    sim.price = 2540.0
    await tm._tick_once_account(ACCOUNT)
    sl_after_trailing = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert abs(sl_after_trailing - 2515.0) < 1e-6

    # A signal_correction that only touches tp2 must NOT regress the live SL
    # back down to the original planned_sl (2490).
    result = await tm.apply_mgmt_action(
        action="signal_correction", chat_id=CHAT_ID, raw_text="TP2 correction",
        correction={"field": "tp2", "value": 4687.0},
    )
    assert result == {"status": "applied", "group_id": group_id}
    sl_after_correction = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert sl_after_correction == sl_after_trailing  # unchanged, never regressed
    assert sl_after_correction >= 2500.0  # still at/above BE, not stranded below entry

    # Trailing must still be able to progress afterward with a further price move.
    sim.positions[runner_leg.ticket]["price_current"] = 2600.0
    sim.price = 2600.0
    await tm._tick_once_account(ACCOUNT)
    sl_after_further_move = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert sl_after_further_move >= sl_after_correction  # trailing not dead/frozen


@pytest.mark.asyncio
async def test_trailing_never_sends_a_candidate_worse_than_the_live_sl_after_unit_grows_a_lot():
    """
    Real production-shaped bug (2026-09-09), found re-testing the
    signal_correction path after the offset was re-scaled by entry->tp1
    instead of unit (see _apply_trailing's docstring). update_group_signal's
    peak_multiple rescale correctly re-projects peak_multiple for the
    "never decreases" GATE (multiple > peak_multiple) when tp1/tp2 change —
    but that gate says nothing about the SL VALUE the next valid multiple
    maps to via entry_to_tp1 (which the rescale doesn't touch). A large
    enough correction to tp2 (growing unit a lot) can make a multiple that
    legitimately clears the gate compute a new_sl, in absolute points,
    BELOW the SL already sitting in MT5 — this test isolates that exact
    mechanism: the candidate must be provably worse than the live SL, and
    the guard must reject it without silently corrupting peak_multiple.
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # BE applied at 2500

    # Trail forward to multiple=1.5 -> SL = entry(2500) + 1.5*10 = 2515.
    sim.positions[runner_leg.ticket]["price_current"] = 2540.0
    sim.price = 2540.0
    await tm._tick_once_account(ACCOUNT)
    live_sl = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert abs(live_sl - 2515.0) < 1e-6
    peak_before = tm.trades[runner_leg.ticket].peak_multiple

    # A huge tp2 correction grows unit from 20 to 2177 — peak_multiple gets
    # rescaled way down (still correctly gates future multiples), but
    # entry_to_tp1 (10) is untouched.
    await tm.apply_mgmt_action(
        action="signal_correction", chat_id=CHAT_ID, raw_text="TP2 correction",
        correction={"field": "tp2", "value": 4687.0},
    )
    rescaled_peak = tm.trades[runner_leg.ticket].peak_multiple
    assert 0 < rescaled_peak < peak_before  # rescale did shrink it, as expected

    # Price ticks up only slightly (2600) -- barely past tp1 relative to the
    # new, much larger unit, but that's still enough multiple to clear the
    # rescaled (tiny) peak_multiple gate.
    sim.positions[runner_leg.ticket]["price_current"] = 2600.0
    sim.price = 2600.0
    new_tp1 = tm.trades[runner_leg.ticket].tp1_price
    new_unit = tm.trades[runner_leg.ticket].tp2_price - new_tp1
    new_multiple = (2600.0 - new_tp1) / new_unit
    assert new_multiple > rescaled_peak  # confirms the gate WOULD clear
    entry_to_tp1 = new_tp1 - 2500.0
    raw_candidate = 2500.0 + new_multiple * entry_to_tp1
    assert raw_candidate < live_sl  # confirms the candidate IS worse than live SL

    await tm._tick_once_account(ACCOUNT)

    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert runner_pos.sl == live_sl  # guard rejected the worse candidate, SL untouched
    assert tm.trades[runner_leg.ticket].peak_multiple == rescaled_peak  # not corrupted by a rejected attempt


@pytest.mark.asyncio
async def test_mgmt_close_now_closes_the_group_in_the_store():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="close it", correction=None)

    assert result == {"status": "completed", "results": [{"group_id": group_id, "status": "closed"}]}
    assert group_id in store.closed


@pytest.mark.asyncio
async def test_mgmt_close_now_message_includes_entry_and_close_price_per_leg():
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    result = await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="Close now", correction=None)

    assert result["status"] == "completed"
    close_events = [kwargs for event, kwargs in notifier.events if event == "mgmt_close_now"]
    assert len(close_events) == 1
    message = close_events[0]["message"]
    assert "tp1" in message
    assert "runner" in message
    assert "2500.0" in message or "2500.00000" in message  # entry price for both legs


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
async def test_tick_applies_be_when_tp1_leg_genuinely_closed_at_tp1_price():
    """The real-deal-verified path: tp1_leg closes with a real TP out-deal
    (deal.reason == DEAL_REASON_TP) -- must be treated as a genuine TP1 hit
    and trigger BE (2026-09-10: classification now uses deal.reason
    directly instead of a price-tolerance heuristic -- see
    _classify_leg_closure)."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")

    sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=20.0)  # closes exactly at tp1_price

    await tm._tick_once_account(ACCOUNT)

    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2500.0) < 1e-6  # moved to entry price (BE)
    assert tm.trades[runner_leg.ticket].be_applied is True


@pytest.mark.asyncio
async def test_tick_does_not_apply_be_when_tp1_leg_closed_externally_not_at_tp1():
    """
    Real production bug found live (2026-09-09): a tp1_leg closed via
    partial_close (e.g. an e2e test's own emergency cleanup, or any other
    out-of-band close) at a price BELOW entry -- a real loss, nowhere near
    tp1_price -- and the system still logged it as "TP1 alcanzado" and
    moved the runner to breakeven.

    2026-09-10: classification now uses deal.reason directly instead of a
    price-tolerance heuristic (see _classify_leg_closure) -- an out-of-band
    close like this records DEAL_REASON_CLIENT, which is neither TP nor SL,
    so it must be treated as an external closure (notify only via
    external_close_detected, no BE, no TP1_HITS increment) rather than a
    genuine TP1 hit.
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_sl_before = sim.positions_get(ticket=runner_leg.ticket)[0].sl

    # Closed at a small loss (entry=2500, close=2499.5) -- nowhere near tp1_price=2510.
    sim.close_position_directly(tp1_leg.ticket, close_price=2499.5)

    await tm._tick_once_account(ACCOUNT)

    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert runner_pos.sl == runner_sl_before  # unchanged -- no BE applied
    assert tm.trades[runner_leg.ticket].be_applied is False
    tp1_hit_events = [kwargs for event, kwargs in notifier.events if event == "tp1_hit"]
    assert tp1_hit_events == []
    external_events = [kwargs for event, kwargs in notifier.events if event == "external_close_detected"]
    assert len(external_events) == 1
    assert external_events[0]["close_price"] == 2499.5


@pytest.mark.asyncio
async def test_be_not_marked_applied_when_order_send_fails_after_all_retries():
    """
    Real production bug: be_applied was set True unconditionally after
    attempting the BE move, even when order_send failed. That left the
    runner in an inconsistent state where _apply_trailing's guard (which
    only checks be_applied) stopped blocking it, so trailing started
    computing a new SL relative to a runner that was never actually moved
    to breakeven in MT5. It must retry a few times, and only mark
    be_applied True if one of those attempts actually succeeds; otherwise
    the runner keeps its original SL and a failure notification fires.
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    original_sl = sim.positions[runner_leg.ticket]['sl']

    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    del sim.positions[tp1_leg.ticket]

    # Every order_send for this ticket (the BE move) fails; sim.order_send is
    # monkeypatched to reject action=6 requests targeting the runner specifically.
    real_order_send = sim.order_send

    def failing_order_send(req):
        if req.get("action") == 6 and req.get("position") == runner_leg.ticket:
            return type('OrderSendResult', (), {'retcode': 10016, 'order': 0, 'deal': 0, 'comment': 'Invalid stops'})()
        return real_order_send(req)

    sim.order_send = failing_order_send

    await tm._tick_once_account(ACCOUNT)

    assert tm.trades[runner_leg.ticket].be_applied is False
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert runner_pos.sl == original_sl  # untouched — BE never actually landed

    failed_events = [kwargs for event, kwargs in notifier.events if event == "tp1_hit_be_failed"]
    assert len(failed_events) == 1
    assert failed_events[0]["runner_ticket"] == runner_leg.ticket
    assert "revision manual" in failed_events[0]["message"]


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

    # peak_multiple = 0.6, entry->tp1 dist = 10, SL = entry + 0.6*10 = 2506
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2506.0) < 1e-6
    assert abs(tm.trades[runner_leg.ticket].peak_multiple - 0.6) < 1e-9


@pytest.mark.asyncio
async def test_trailing_at_peak_zero_equals_be_with_full_protective_margin():
    """
    Real production concern (group 60, user-reported): anchoring the
    formula on tp1_price left the trailing SL only 0-3 points from the
    live price right after crossing TP1 (peak near 0) — tighter than the
    BE's own protective margin, and a completely normal pullback right
    after TP1 was enough to stop the runner out almost simultaneously
    with tp1_leg closing. Anchoring on entry_price instead means the
    first trailing tick past TP1 (peak just above 0) computes an SL
    barely past the BE itself, not a fresh, much tighter level — so the
    runner keeps the same protective margin BE already earned instead of
    trading it away the instant price ticks past TP1.
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # BE applied: SL = entry = 2500

    # Price ticks just barely past tp1 (2510 -> 2511): the smallest advance
    # that still makes multiple > 0 and triggers a trailing attempt.
    sim.positions[runner_leg.ticket]["price_current"] = 2511.0
    sim.price = 2511.0
    await tm._tick_once_account(ACCOUNT)

    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    # multiple = 1/20 = 0.05, entry->tp1 dist = 10 -> SL = entry + 0.05*10 = 2500.5
    assert abs(runner_pos.sl - 2500.5) < 1e-6
    # The live price-to-SL distance is still close to the BE's own margin
    # (10 points), not collapsed to a couple of points like the old
    # tp1-anchored formula produced at this same price.
    assert (2511.0 - runner_pos.sl) > 9.5


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
async def test_trailing_never_regresses_below_be_after_a_signal_correction_moves_tp1():
    """
    _apply_trailing anchors its formula on entry_price (SL = entry_price +
    (peak*unit)/3), so new_sl is structurally always >= entry_price once BE
    has landed: peak_multiple can never be negative, and the "never
    decreases" guard already requires multiple > peak_multiple >= 0 before
    a candidate is even computed. This still must hold after a
    signal_correction (update_group_signal, dual-TP spec §5.2) drags
    tp1_price/tp2_price around post-BE without validating them against
    entry_price — the correction changes `unit`/`multiple`'s magnitude, but
    must never be able to push new_sl below the BE already in MT5.

    (Historical note: this test originally exercised an explicit
    "candidate worse than BE" guard, back when the formula was anchored on
    tp1_price instead — a stale correction could then drag tp1_price behind
    entry_price and produce a candidate genuinely worse than BE. Re-anchoring
    on entry_price fixed that at the source, making the guard unreachable;
    it was removed, and this test now documents the structural property
    that replaced it.)
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # applies BE: runner SL -> entry_price = 2500.0

    be_sl = sim.positions_get(ticket=runner_leg.ticket)[0].sl
    assert be_sl == 2500.0

    # A signal_correction drags tp1/tp2 back down, below the runner's entry —
    # an inverted/stale correction, but update_group_signal doesn't validate
    # tp1 against entry_price, so it lands as-is.
    await tm.update_group_signal(group_id, sl=None, tp1=2495.0, tp2=2505.0)

    # Price sits just past the new tp1 (2495), still well below the BE (2500)
    # already live. multiple > 0 so the "never decreases" guard doesn't
    # short-circuit before _force_runner_sl is even attempted.
    sim.positions[runner_leg.ticket]["price_current"] = 2496.0
    sim.price = 2496.0
    await tm._tick_once_account(ACCOUNT)

    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert runner_pos.sl >= be_sl  # must never regress below the BE already applied


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

    # multiple=1.5, entry->tp1 dist=10 -> SL = entry + 1.5*10 = 2515
    # (already past tp1=2510 at this point, unlike the old unit-scaled offset)
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.sl - 2515.0) < 1e-6


@pytest.mark.asyncio
async def test_trailing_peak_multiple_not_advanced_when_order_send_fails():
    """
    Real production bug (group 60, XAUUSD SELL, live): peak_multiple was
    advanced unconditionally before attempting the SL move, even when
    order_send failed after all retries (e.g. candidate SL inside the
    broker's trade_stops_level right after TP1). That left the runner's
    real MT5 SL frozen at the last successfully-applied level while the
    orchestrator's internal peak kept climbing — so the next tick's
    "never decreases" guard silently discarded price levels that MT5
    would have accepted. The runner then got stopped out by its stale
    real SL almost immediately after TP1, mirroring _apply_be's
    already-fixed failure mode. peak_multiple must only advance when the
    order_send attempt actually succeeds — and (trailing_updated is now
    logged only, not sent as a notify() event, to cut notification spam
    from the ~100+ trailing ticks a single live trade can produce) no
    "trailing_updated" event may reach the notifier for a failed attempt
    either, since it's still gated by the same `if ok:` block.
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # applies BE

    sl_after_be = sim.positions_get(ticket=runner_leg.ticket)[0].sl

    # Every order_send for this ticket's trailing move (action=6) fails from
    # here on, simulating MT5 rejecting the candidate SL (e.g. retcode=10016,
    # too close to trade_stops_level).
    real_order_send = sim.order_send

    def failing_order_send(req):
        if req.get("action") == 6 and req.get("position") == runner_leg.ticket:
            return type('OrderSendResult', (), {'retcode': 10016, 'order': 0, 'deal': 0, 'comment': 'Invalid stops'})()
        return real_order_send(req)

    sim.order_send = failing_order_send

    # unit = tp2 - tp1 = 20. Move price to 60% of unit past tp1 = 2510 + 12 = 2522
    sim.positions[runner_leg.ticket]["price_current"] = 2522.0
    sim.price = 2522.0
    await tm._tick_once_account(ACCOUNT)

    # The order_send failed, so peak_multiple must NOT have advanced and the
    # real SL in MT5 must remain untouched at its post-BE level.
    assert tm.trades[runner_leg.ticket].peak_multiple == 0.0
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert runner_pos.sl == sl_after_be

    trailing_events = [kwargs for event, kwargs in notifier.events if event == "trailing_updated"]
    assert len(trailing_events) == 0


# --- TP2 partial close: runner takes 50% off at tp2, remainder keeps trailing ---

@pytest.mark.asyncio
async def test_tp2_partial_close_takes_half_volume_and_keeps_trailing_on_remainder():
    """
    New mechanic (product decision 2026-09-08): the first time the runner's
    live price reaches tp2_price, half of its CURRENT volume is closed via
    partial_close (same helper/pattern already used elsewhere for full
    closes), once per group (tp2_partial_applied flag, same pattern as
    be_applied). The remaining half keeps trailing exactly as before —
    peak_multiple/SL are NOT reset and do not treat tp2 as a new anchor.
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    original_vol = sim.positions[runner_leg.ticket]["volume"]
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # applies BE

    # Price reaches tp2 exactly (multiple=1.0, unit=20).
    sim.positions[runner_leg.ticket]["price_current"] = 2530.0
    sim.price = 2530.0
    await tm._tick_once_account(ACCOUNT)

    assert tm.trades[runner_leg.ticket].tp2_partial_applied is True
    runner_pos = sim.positions_get(ticket=runner_leg.ticket)[0]
    assert abs(runner_pos.volume - original_vol / 2.0) < 1e-9

    # Trailing still applied in the SAME tick, on the same entry-anchored
    # formula — tp2 is not a new anchor, peak_multiple/SL are unaffected by
    # the partial close itself. At multiple=1.0 (price exactly at tp2), the
    # entry->tp1-scaled offset (2026-09-09 fix) makes SL land exactly on
    # tp1_price (2510) — by design, this is what "reaching tp2" now means
    # for the SL.
    assert abs(tm.trades[runner_leg.ticket].peak_multiple - 1.0) < 1e-9
    assert abs(runner_pos.sl - 2510.0) < 1e-6

    partial_events = [kwargs for event, kwargs in notifier.events if event == "tp2_partial_closed"]
    assert len(partial_events) == 1
    assert partial_events[0]["group_id"] == group_id
    assert partial_events[0]["ticket"] == runner_leg.ticket


@pytest.mark.asyncio
async def test_tp2_partial_close_fires_only_once_even_if_price_oscillates_around_tp2():
    """
    Simple trigger (price >= tp2, no confirmation threshold) fires once and
    latches via tp2_partial_applied — a whipsaw around tp2 must not close
    another 50% slice on a later re-cross, same pattern as be_applied.
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)

    sim.positions[runner_leg.ticket]["price_current"] = 2531.0  # crosses tp2
    sim.price = 2531.0
    await tm._tick_once_account(ACCOUNT)
    vol_after_first_cross = sim.positions_get(ticket=runner_leg.ticket)[0].volume

    # Retreats back below tp2, then crosses again — peak_multiple's own
    # "never decreases" guard means the second crossing at the same price
    # doesn't even attempt a new trailing move, but tp2_partial_applied must
    # independently prevent a second partial_close regardless.
    sim.positions[runner_leg.ticket]["price_current"] = 2525.0
    sim.price = 2525.0
    await tm._tick_once_account(ACCOUNT)
    sim.positions[runner_leg.ticket]["price_current"] = 2535.0
    sim.price = 2535.0
    await tm._tick_once_account(ACCOUNT)

    vol_final = sim.positions_get(ticket=runner_leg.ticket)[0].volume
    assert vol_final == vol_after_first_cross  # no second partial_close happened


@pytest.mark.asyncio
async def test_tp2_partial_close_never_fires_for_sell_until_price_reaches_tp2():
    """Direction-aware trigger: for SELL, tp2 sits below tp1/entry, so the
    condition is price <= tp2, not price >= tp2."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="SELL", sl=2510.0, tp1=2490.0, tp2=2470.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    original_vol = sim.positions[runner_leg.ticket]["volume"]
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # BE applied

    # Still above tp2 (2470) -- must not trigger yet.
    sim.positions[runner_leg.ticket]["price_current"] = 2480.0
    sim.price = 2480.0
    await tm._tick_once_account(ACCOUNT)
    assert tm.trades[runner_leg.ticket].tp2_partial_applied is False
    assert sim.positions_get(ticket=runner_leg.ticket)[0].volume == original_vol

    # Reaches tp2 -- triggers now.
    sim.positions[runner_leg.ticket]["price_current"] = 2470.0
    sim.price = 2470.0
    await tm._tick_once_account(ACCOUNT)
    assert tm.trades[runner_leg.ticket].tp2_partial_applied is True
    assert abs(sim.positions_get(ticket=runner_leg.ticket)[0].volume - original_vol/2.0) < 1e-9


@pytest.mark.asyncio
async def test_tp2_partial_close_persists_flag_and_reconciles_from_store():
    """tp2_partial_applied must survive a persist/reconcile round trip, same
    as be_applied and peak_multiple, so a restart doesn't re-fire the
    partial close for a group that already took it."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)

    sim.positions[runner_leg.ticket]["price_current"] = 2530.0
    sim.price = 2530.0
    await tm._tick_once_account(ACCOUNT)
    assert tm.trades[runner_leg.ticket].tp2_partial_applied is True

    saved_doc = store.saved[-1]
    assert saved_doc["legs"]["runner"]["tp2_partial_applied"] is True

    # Reconcile a fresh TradeManager from that persisted doc + live MT5 state
    # (same pattern as test_reconcile_recovers_full_state_from_store).
    store.docs[group_id] = saved_doc
    tm2 = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    summary = await tm2.reconcile_from_mt5([ACCOUNT])
    assert summary["recovered_from_redis"] == 1
    assert tm2.trades[runner_leg.ticket].tp2_partial_applied is True


# --- Review fix 1: update_group_signal must not regress a trailed/BE'd SL ---

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


# --- chat_id-scoping: find_active_groups_for_chat ---

@pytest.mark.asyncio
async def test_find_active_groups_for_chat_returns_all_groups_oldest_first():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="chatA")
    g2 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="chatA")
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="chatB")

    found = tm.find_active_groups_for_chat("chatA")

    assert found == [g1, g2]


@pytest.mark.asyncio
async def test_find_active_groups_for_chat_returns_empty_list_for_unknown_chat():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="chatA")

    found = tm.find_active_groups_for_chat("chatZ")

    assert found == []


@pytest.mark.asyncio
async def test_find_active_groups_for_chat_never_returns_orphaned_none_chat_id_groups():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    # Opened without chat_id (legacy / test default) -- an orphan.
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    found_for_none = tm.find_active_groups_for_chat(None)
    found_for_real_chat = tm.find_active_groups_for_chat("chatA")

    assert found_for_none == []  # querying with None must not match orphans either
    assert found_for_real_chat == []


@pytest.mark.asyncio
async def test_find_active_groups_for_chat_deduplicates_group_ids_across_both_legs():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    g1 = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="chatA")

    found = tm.find_active_groups_for_chat("chatA")

    assert found == [g1]  # not [g1, g1] -- one entry per group, not per leg


# --- Review fix 3: entry_price=None must not raise ---

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
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier, config_provider=FakeConfigProvider(ENTRY_WAIT_SECONDS=1))

    # target range is far from current price but not "already past" (below entry_lo, not
    # past entry_hi for a BUY) -- it should wait, time out, and abort with no positions opened.
    group_id = await tm.open_group(
        ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2440.0, tp1=2470.0, tp2=2480.0,
        entry_range=(2460.0, 2465.0),
    )

    assert group_id is None
    assert len(tm.trades) == 0

    # n8n must receive a human-readable message it can forward as-is (not just raw fields) —
    # this is what lets the user learn *why* a real signal wasn't executed.
    aborted_events = [kwargs for event, kwargs in notifier.events if event == "open_aborted"]
    assert len(aborted_events) == 1
    assert aborted_events[0]["reason"] == "entry_range_missed"
    assert "no entro en el rango de entrada 2460.0-2465.0" in aborted_events[0]["message"]


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


@pytest.mark.asyncio
async def test_call_times_out_instead_of_hanging_forever_on_a_stuck_mt5_socket(monkeypatch):
    """
    Real production incident: an MT5/RPyC call hung with no exception and no
    timeout for 4+ minutes (and counting), freezing run_forever entirely --
    no other account/group could be managed, and _tick_once_account's own
    try/except never even ran because the hang was inside the awaited call
    itself. RPyC's own sync_request_timeout (30s) did not reliably cut this
    off (it lives inside AsyncResult.wait()'s serve loop, not a hard
    deadline). TradeManager._call must impose its own asyncio.wait_for so a
    stuck call fails fast instead of blocking the entire mechanical loop
    (and, transitively, PooledMT5Client's threading.Lock) indefinitely.
    """
    monkeypatch.setenv("MT5_CALL_TIMEOUT_SECONDS", "0.05")

    def hangs_forever(*args, **kwargs):
        time.sleep(0.3)  # longer than the patched timeout, short enough to keep the suite fast
        return "should never get here"

    with pytest.raises(asyncio.TimeoutError):
        await TradeManager._call(hangs_forever)


@pytest.mark.asyncio
async def test_call_falls_back_to_default_timeout_when_env_var_invalid(monkeypatch, caplog):
    """An invalid MT5_CALL_TIMEOUT_SECONDS (unparseable) must not crash the
    call — it falls back to the default and logs a warning, since a typo in
    .env should never take down the whole service."""
    monkeypatch.setenv("MT5_CALL_TIMEOUT_SECONDS", "not-a-number")

    result = await TradeManager._call(lambda: "ok")

    assert result == "ok"
    assert "invalido" in caplog.text


# --- Task 3: TradeStateStore write-point wiring ---

class RecordingStore:
    """Test double for TradeStateStore — records every save/close call."""
    def __init__(self):
        self.saved: list[dict] = []
        self.closed: list[int] = []
        self.docs: dict[int, dict] = {}

    async def save_group(self, doc):
        self.saved.append(doc)

    async def close_group(self, group_id):
        self.closed.append(group_id)

    async def load_group(self, group_id):
        doc = self.docs.get(group_id)
        return (doc, "redis") if doc is not None else (None, "none")

    async def load_all_group_ids(self):
        return set(self.docs.keys())

    async def compact(self, active_group_ids):
        pass


@pytest.mark.asyncio
async def test_open_group_persists_the_new_group():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    assert len(store.saved) == 1
    doc = store.saved[-1]
    assert doc["group_id"] == group_id
    assert doc["symbol"] == "XAUUSD"
    assert doc["direction"] == "BUY"
    assert doc["tp1_price"] == 2510.0
    assert doc["tp2_price"] == 2530.0
    tp1_ticket = next(t.ticket for t in tm.trades.values() if t.leg == "tp1")
    runner_ticket = next(t.ticket for t in tm.trades.values() if t.leg == "runner")
    assert doc["legs"]["tp1"]["ticket"] == tp1_ticket
    assert doc["legs"]["runner"]["ticket"] == runner_ticket


@pytest.mark.asyncio
async def test_update_group_signal_persists_new_tp_values():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2470.0, tp1=None, tp2=None)
    store.saved.clear()

    await tm.update_group_signal(group_id, sl=2490.0, tp1=2510.0, tp2=2530.0)

    assert len(store.saved) == 1
    assert store.saved[-1]["tp1_price"] == 2510.0
    assert store.saved[-1]["tp2_price"] == 2530.0


@pytest.mark.asyncio
async def test_tp1_leg_closing_persists_be_applied():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    store.saved.clear()
    del sim.positions[tp1_leg.ticket]

    await tm._tick_once_account(ACCOUNT)

    runner_docs = [d for d in store.saved if d["group_id"] == group_id]
    assert len(runner_docs) == 1
    assert runner_docs[-1]["legs"]["runner"]["be_applied"] is True


@pytest.mark.asyncio
async def test_trailing_update_persists_peak_multiple():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # applies BE
    store.saved.clear()
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    sim.price = 2522.0  # 60% of unit=20 past tp1=2510
    sim.positions[runner_leg.ticket]['price_current'] = sim.price

    await tm._tick_once_account(ACCOUNT)

    assert len(store.saved) == 1
    assert store.saved[-1]["legs"]["runner"]["peak_multiple"] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_tp1_leg_closing_syncs_planned_sl_to_the_new_be_price():
    """
    Real production bug found live (2026-09-10, e2e D1 scenario): BE was
    applied to MT5 successfully (be_applied=True) but ManagedTrade.planned_sl
    was never updated to match -- it stayed at the old, pre-BE value.
    _group_doc persists planned_sl as-is, so reconcile_from_mt5 rebuilt the
    runner with a stale baseline after any restart. planned_sl must now
    track the real, just-applied BE price both in memory and in the
    persisted doc.
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]

    await tm._tick_once_account(ACCOUNT)

    assert tm.trades[runner_leg.ticket].be_applied is True
    assert tm.trades[runner_leg.ticket].planned_sl == pytest.approx(runner_leg.entry_price)
    runner_docs = [d for d in store.saved if d["group_id"] == group_id]
    assert runner_docs[-1]["legs"]["runner"]["planned_sl"] == pytest.approx(runner_leg.entry_price)


@pytest.mark.asyncio
async def test_mgmt_move_sl_be_now_syncs_planned_sl_to_the_new_be_price():
    """Same fix as above, for the manual /mgmt/action move_sl_be_now path."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")

    result = await tm.apply_mgmt_action(action="move_sl_be_now", chat_id=CHAT_ID, raw_text="be now", correction=None)

    results_by_group = {r["group_id"]: r for r in result["results"]}
    assert results_by_group[group_id]["status"] == "applied"
    assert tm.trades[runner_leg.ticket].planned_sl == pytest.approx(runner_leg.entry_price)
    runner_docs = [d for d in store.saved if d["group_id"] == group_id]
    assert runner_docs[-1]["legs"]["runner"]["planned_sl"] == pytest.approx(runner_leg.entry_price)


@pytest.mark.asyncio
async def test_trailing_update_syncs_planned_sl_to_the_new_trailing_sl():
    """Same fix as above, for the mechanical _apply_trailing path."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # applies BE
    store.saved.clear()
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    sim.price = 2522.0  # 60% of unit=20 past tp1=2510
    sim.positions[runner_leg.ticket]['price_current'] = sim.price

    await tm._tick_once_account(ACCOUNT)

    expected_sl = tm.trades[runner_leg.ticket].planned_sl
    assert store.saved[-1]["legs"]["runner"]["peak_multiple"] == pytest.approx(0.6)
    # The live SL that _apply_trailing actually sent to MT5 must equal the
    # persisted planned_sl -- not the pre-BE value or entry_price alone.
    assert sim.positions[runner_leg.ticket]["sl"] == pytest.approx(expected_sl)
    runner_docs = [d for d in store.saved if d["group_id"] == group_id]
    assert runner_docs[-1]["legs"]["runner"]["planned_sl"] == pytest.approx(expected_sl)


@pytest.mark.asyncio
async def test_both_legs_closing_closes_the_group_in_the_store():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # tp1 closes, BE applied to runner
    del sim.positions[runner_leg.ticket]

    await tm._tick_once_account(ACCOUNT)  # runner closes too

    assert group_id in store.closed


@pytest.mark.asyncio
async def test_no_state_store_is_a_safe_default():
    """TradeManager() without state_store (existing callers, all prior tests) must keep working unchanged."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())  # no state_store kwarg

    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    assert group_id is not None  # did not raise despite no store configured


@pytest.mark.asyncio
async def test_group_doc_includes_chat_id():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    group_id = await tm.open_group(
        ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0,
        chat_id="-1001234567890",
    )

    doc = tm._group_doc(group_id)
    assert doc["chat_id"] == "-1001234567890"


# --- Task 4: reconcile_from_mt5 startup recovery ---

def _open_raw_position(sim, *, ticket_price, sl, tp, comment, magic=MAGIC, direction_type=0):
    """Directly injects a position into SimuladorMT5, bypassing TradeManager —
    simulates a position that existed before this process started."""
    req = {
        "action": 1, "symbol": "XAUUSD", "volume": 0.04, "type": direction_type,
        "price": ticket_price, "sl": sl, "tp": tp, "comment": comment, "magic": magic,
    }
    res = sim.order_send(req)
    return res.order


@pytest.mark.asyncio
async def test_reconcile_recovers_full_state_from_store():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()  # its default load_group/load_all_group_ids read from store.docs directly

    tp1_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP1-tp1")
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP1-runner")
    store.docs[1] = {
        "group_id": 1, "account_name": "demo", "symbol": "XAUUSD", "direction": "BUY",
        "tp1_price": 2510.0, "tp2_price": 2530.0,
        "legs": {
            "tp1": {"ticket": tp1_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": False, "peak_multiple": 0.0},
            "runner": {"ticket": runner_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": True, "peak_multiple": 0.35},
        },
    }
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    summary = await tm.reconcile_from_mt5([ACCOUNT])

    assert summary["recovered_from_redis"] == 1
    assert len(tm.trades) == 2
    runner = tm.trades[runner_ticket]
    assert runner.tp1_price == 2510.0
    assert runner.tp2_price == 2530.0
    assert runner.be_applied is True
    assert runner.peak_multiple == 0.35


@pytest.mark.asyncio
async def test_reconcile_inherits_chat_id_from_store_doc():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()

    tp1_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP1-tp1")
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP1-runner")
    store.docs[1] = {
        "group_id": 1, "account_name": "demo", "symbol": "XAUUSD", "direction": "BUY",
        "chat_id": "-1001234567890",
        "tp1_price": 2510.0, "tp2_price": 2530.0,
        "legs": {
            "tp1": {"ticket": tp1_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": False, "peak_multiple": 0.0},
            "runner": {"ticket": runner_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": True, "peak_multiple": 0.35},
        },
    }
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    await tm.reconcile_from_mt5([ACCOUNT])

    assert tm.trades[tp1_ticket].chat_id == "-1001234567890"
    assert tm.trades[runner_ticket].chat_id == "-1001234567890"


@pytest.mark.asyncio
async def test_reconcile_leaves_chat_id_none_when_store_doc_predates_the_field():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()

    tp1_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP1-tp1")
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP1-runner")
    store.docs[1] = {
        # Legacy doc persisted before chat_id existed -- no "chat_id" key at all.
        "group_id": 1, "account_name": "demo", "symbol": "XAUUSD", "direction": "BUY",
        "tp1_price": 2510.0, "tp2_price": 2530.0,
        "legs": {
            "tp1": {"ticket": tp1_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": False, "peak_multiple": 0.0},
            "runner": {"ticket": runner_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": True, "peak_multiple": 0.35},
        },
    }
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    await tm.reconcile_from_mt5([ACCOUNT])

    assert tm.trades[tp1_ticket].chat_id is None
    assert tm.trades[runner_ticket].chat_id is None


@pytest.mark.asyncio
async def test_reconcile_degraded_mode_leaves_chat_id_none():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()  # empty docs -- every group_id misses, forcing degraded mode

    tp1_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP7-tp1")
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP7-runner")
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    summary = await tm.reconcile_from_mt5([ACCOUNT])

    assert summary["degraded"] == 1
    assert tm.trades[tp1_ticket].chat_id is None
    assert tm.trades[runner_ticket].chat_id is None


@pytest.mark.asyncio
async def test_reconcile_falls_back_to_degraded_when_store_has_nothing():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()  # empty store.docs -- every group_id misses

    tp1_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP7-tp1")
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP7-runner")
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    summary = await tm.reconcile_from_mt5([ACCOUNT])

    assert summary["degraded"] == 1
    assert len(tm.trades) == 2
    runner = tm.trades[runner_ticket]
    assert runner.tp1_price is None
    assert runner.tp2_price is None
    assert runner.be_applied is False
    assert runner.peak_multiple == 0.0
    assert runner.planned_sl == 2490.0  # recovered directly from the MT5 position
    assert runner.entry_price == 2500.0


@pytest.mark.asyncio
async def test_reconcile_reports_orphan_for_unparseable_comment():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()

    # Async, matching the real TradeStateStore.compact contract — a sync
    # override here would raise TypeError inside reconcile_from_mt5's
    # try/except and be masked as a mere warning, so compaction would
    # silently never run and this test would still pass.
    async def _compact(active_group_ids):
        return None
    store.compact = _compact

    ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="some-old-format")
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    summary = await tm.reconcile_from_mt5([ACCOUNT])

    assert len(summary["orphaned"]) == 1
    assert summary["orphaned"][0]["ticket"] == ticket
    assert summary["orphaned"][0]["comment"] == "some-old-format"
    assert ticket not in tm.trades  # never managed automatically


@pytest.mark.asyncio
async def test_reconcile_applies_be_synchronously_when_tp1_closed_during_downtime():
    """The gap this whole design exists to close: tp1_leg closed while the
    process was down, runner is still open. The normal close-detection loop
    would never see this (tp1 was never re-inserted into self.trades) —
    reconcile_from_mt5 must apply BE inline, during reconciliation itself."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()

    # Only the runner still exists in MT5 -- tp1_leg closed during downtime.
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP1-runner")
    store.docs[1] = {
        "group_id": 1, "account_name": "demo", "symbol": "XAUUSD", "direction": "BUY",
        "tp1_price": 2510.0, "tp2_price": 2530.0,
        "legs": {
            "tp1": {"ticket": 999999, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": False, "peak_multiple": 0.0},
            "runner": {"ticket": runner_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": False, "peak_multiple": 0.0},
        },
    }
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    await tm.reconcile_from_mt5([ACCOUNT])

    runner_pos = sim.positions_get(ticket=runner_ticket)[0]
    assert abs(runner_pos.sl - 2500.0) < 1e-6  # moved to entry price (BE)
    assert tm.trades[runner_ticket].be_applied is True


@pytest.mark.asyncio
async def test_reconcile_does_not_crash_when_doc_no_longer_has_tp1_leg():
    """
    Real production bug (found live on the VPS): a group whose tp1_leg
    closed in a PREVIOUS process lifetime (not during the current
    downtime) has its own persisted doc already missing "tp1" from
    "legs" -- _group_doc only ever includes legs still in self.trades,
    and _on_tp1_leg_closed already ran once, live, and re-persisted the
    group without it. reconcile_from_mt5 must not assume doc["legs"]["tp1"]
    exists just because mt5_tp1 is None -- that KeyError crash-looped
    trade_orchestrator on every restart for a group in this state,
    leaving it (and every other group on the account) with zero
    mechanical management until fixed.
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()

    # Only the runner exists in MT5 (as in the downtime case), but this
    # doc's "legs" never had "tp1" at all -- it closed a while ago, in an
    # earlier reconciliation/tick cycle, not during this restart's downtime.
    runner_ticket = _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP1-runner")
    store.docs[1] = {
        "group_id": 1, "account_name": "demo", "symbol": "XAUUSD", "direction": "BUY",
        "chat_id": None, "tp1_price": 2510.0, "tp2_price": 2530.0,
        "legs": {
            "runner": {"ticket": runner_ticket, "planned_sl": 2490.0, "entry_price": 2500.0, "be_applied": False, "peak_multiple": 0.0},
        },
    }
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    summary = await tm.reconcile_from_mt5([ACCOUNT])  # must not raise KeyError

    assert summary["recovered_from_redis"] == 1
    assert runner_ticket in tm.trades
    # No synchronous BE was attempted -- the runner's SL is untouched from
    # what it already was, since there is no evidence tp1 closed just now.
    runner_pos = sim.positions_get(ticket=runner_ticket)[0]
    assert runner_pos.sl == 2490.0
    assert tm.trades[runner_ticket].be_applied is False


@pytest.mark.asyncio
async def test_reconcile_sets_next_group_id_above_the_highest_seen():
    sim = SimuladorMT5()
    sim.price = 2500.0
    store = RecordingStore()  # empty docs -- load_group naturally returns (None, "none")

    # group 5 known only from a closed entry in the file. Async, matching the
    # real TradeStateStore.load_all_group_ids contract — a sync override here
    # would raise TypeError inside reconcile_from_mt5's try/except, silently
    # dropping group 5 and leaving _next_group_id at 4 instead of 6.
    async def _load_all_group_ids():
        return {5}
    store.load_all_group_ids = _load_all_group_ids

    _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP3-tp1")
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier(), state_store=store)

    await tm.reconcile_from_mt5([ACCOUNT])

    assert tm._next_group_id == 6  # max(3 from MT5, 5 from store) + 1


@pytest.mark.asyncio
async def test_reconcile_with_no_state_store_still_recovers_degraded():
    """No state_store configured at all -- must behave like every group is degraded, not crash."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=2510.0, comment="TM-GRP1-tp1")
    _open_raw_position(sim, ticket_price=2500.0, sl=2490.0, tp=0.0, comment="TM-GRP1-runner")
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())  # no state_store

    summary = await tm.reconcile_from_mt5([ACCOUNT])

    assert summary["degraded"] == 1
    assert len(tm.trades) == 2


@pytest.mark.asyncio
async def test_runner_closed_externally_message_includes_close_price():
    """
    2026-09-10: a runner leg disappearing from positions_get with a real
    deal recorded under DEAL_REASON_CLIENT (close_position_directly) is now
    classified as an external closure (deal.reason-based classification,
    see _classify_leg_closure) and reported via external_close_detected --
    not the old undifferentiated runner_closed event.
    """
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # tp1 closes, BE applied to runner

    sim.close_position_directly(runner_leg.ticket, close_price=2514.0)
    await tm._tick_once_account(ACCOUNT)  # runner closes

    runner_events = [kwargs for event, kwargs in notifier.events if event == "runner_closed"]
    assert runner_events == []
    external_events = [kwargs for event, kwargs in notifier.events if event == "external_close_detected"]
    assert len(external_events) == 1
    assert external_events[0]["leg"] == "runner"
    assert external_events[0]["close_price"] == 2514.0
    assert "2514.0" in external_events[0]["message"] or "2514.00000" in external_events[0]["message"]


@pytest.mark.asyncio
async def test_runner_closed_message_falls_back_when_close_price_unavailable():
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    runner_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")
    del sim.positions[tp1_leg.ticket]
    await tm._tick_once_account(ACCOUNT)  # tp1 closes, BE applied to runner

    # No deal recorded for the runner's exit at all (e.g. history not yet
    # propagated) -- _get_close_deal_info returns None, so the closure is
    # classified "unknown" (2026-09-10: deal.reason-based classification)
    # and reported via the audit-only fallback event with no close-price
    # detail to fall back to.
    del sim.positions[runner_leg.ticket]
    await tm._tick_once_account(ACCOUNT)

    runner_events = [kwargs for event, kwargs in notifier.events if event == "runner_closed"]
    assert len(runner_events) == 1
    assert "sin poder determinar la causa" in runner_events[0]["message"]


@pytest.mark.asyncio
async def test_tp1_hit_message_includes_entry_and_close_price():
    sim = SimuladorMT5()
    sim.price = 2500.0
    notifier = DummyNotifier()
    tm = TradeManager(DummyExecutor(sim), notifier=notifier)
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")
    sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=20.0)

    await tm._tick_once_account(ACCOUNT)

    tp1_events = [kwargs for event, kwargs in notifier.events if event == "tp1_hit"]
    assert len(tp1_events) == 1
    message = tp1_events[0]["message"]
    # build_tp1_hit_message (Task 6, locked-in signature) reports close price/
    # volume/P&L only -- entry price is not part of this message by design,
    # unlike the old ad-hoc f-string it replaces.
    assert "2510.0" in message or "2510.00000" in message


@pytest.mark.asyncio
async def test_tp1_leg_closed_by_real_tp_reason_triggers_tp1_hit():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")

    sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=20.0)
    await tm._tick_once_account(ACCOUNT)

    events = [event for event, kwargs in tm.notifier.events if event == "tp1_hit"]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_tp1_leg_closed_by_sl_reason_does_not_trigger_tp1_hit():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")

    sim.close_position_by_sl(tp1_leg.ticket, close_price=2490.0, profit=-20.0)
    await tm._tick_once_account(ACCOUNT)

    tp1_events = [event for event, kwargs in tm.notifier.events if event == "tp1_hit"]
    sl_events = [kwargs for event, kwargs in tm.notifier.events if event == "sl_hit_detected"]
    assert len(tp1_events) == 0
    assert len(sl_events) == 1
    assert sl_events[0]["pnl_money"] == -20.0


@pytest.mark.asyncio
async def test_runner_closed_by_sl_reason_triggers_sl_hit_detected():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")

    sim.close_position_by_sl(runner.ticket, close_price=2495.0, profit=-5.0)
    await tm._tick_once_account(ACCOUNT)

    sl_events = [kwargs for event, kwargs in tm.notifier.events if event == "sl_hit_detected"]
    assert len(sl_events) == 1
    assert sl_events[0]["leg"] == "runner"


@pytest.mark.asyncio
async def test_leg_closed_by_unknown_external_cause_triggers_external_close_detected():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
    runner = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "runner")

    # Closed directly in MT5 by the user, outside the system -- not via
    # close_now/close_partial_now (which would have removed the ticket from
    # tm.trades synchronously before this tick ever ran).
    sim.close_position_directly(runner.ticket, close_price=2505.0)
    await tm._tick_once_account(ACCOUNT)

    external_events = [kwargs for event, kwargs in tm.notifier.events if event == "external_close_detected"]
    assert len(external_events) == 1


@pytest.mark.asyncio
async def test_close_now_does_not_trigger_external_close_detected():
    """Real bug class this guards against: apply_mgmt_action's close_now
    removes the ticket from tm.trades synchronously (trade_manager.py:908)
    before any _tick_once_account runs, so the passive detection loop must
    never see that ticket as 'closed' -- confirming no spurious
    external_close_detected fires for a close the system itself ordered."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    await tm.apply_mgmt_action(action="close_now", chat_id=CHAT_ID, raw_text="cierra todo", correction=None)
    await tm._tick_once_account(ACCOUNT)

    external_events = [event for event, kwargs in tm.notifier.events if event == "external_close_detected"]
    assert len(external_events) == 0


@pytest.mark.asyncio
async def test_notify_emits_log_line_for_every_event(caplog):
    import logging
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    with caplog.at_level(logging.INFO, logger="trade_orchestrator.trade_manager"):
        await tm._notify("group_opened", group_id=42, symbol="XAUUSD")

    assert any(
        "[TM][EVENT]" in r.message and "group_opened" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_notify_uses_event_bus_when_configured():
    from services.trade_orchestrator.event_bus import EventBus

    with tempfile.TemporaryDirectory() as d:
        audit_path = os.path.join(d, "audit_log.jsonl")
        bus = EventBus(audit_path, redis_client=None)
        sim = SimuladorMT5()
        sim.price = 2500.0
        tm = TradeManager(DummyExecutor(sim), event_bus=bus)

        await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

        with open(audit_path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f.readlines()]
        opened = [l for l in lines if l["event_type"] == "group_opened"]
        assert len(opened) == 1
        assert opened[0]["channel"] == "both"
        assert "entry_price" in opened[0]["payload"]
        assert "volume" in opened[0]["payload"]


@pytest.mark.asyncio
async def test_group_opened_message_is_telegram_ready_text():
    from services.trade_orchestrator.event_bus import EventBus

    with tempfile.TemporaryDirectory() as d:
        audit_path = os.path.join(d, "audit_log.jsonl")
        bus = EventBus(audit_path, redis_client=None)
        sim = SimuladorMT5()
        sim.price = 2500.0
        tm = TradeManager(DummyExecutor(sim), event_bus=bus)

        await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

        with open(audit_path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f.readlines()]
        opened = next(l for l in lines if l["event_type"] == "group_opened")
        assert "APERTURA" in opened["message"]
        assert "XAUUSD" in opened["message"]


@pytest.mark.asyncio
async def test_tp1_hit_event_includes_money_pnl_in_payload():
    from services.trade_orchestrator.event_bus import EventBus

    with tempfile.TemporaryDirectory() as d:
        audit_path = os.path.join(d, "audit_log.jsonl")
        bus = EventBus(audit_path, redis_client=None)
        sim = SimuladorMT5()
        sim.price = 2500.0
        tm = TradeManager(DummyExecutor(sim), event_bus=bus)
        group_id = await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)
        tp1_leg = next(t for t in tm.trades.values() if t.group_id == group_id and t.leg == "tp1")

        sim.close_position_by_tp(tp1_leg.ticket, close_price=2510.0, profit=20.0)
        await tm._tick_once_account(ACCOUNT)

        with open(audit_path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f.readlines()]
        tp1_hit = next(l for l in lines if l["event_type"] == "tp1_hit")
        assert tp1_hit["payload"]["pnl_money"] == 20.0
        assert tp1_hit["channel"] == "both"


@pytest.mark.asyncio
async def test_notify_falls_back_to_old_notifier_when_no_event_bus_configured():
    """Backward-compat: existing tests across the suite construct TradeManager
    with only notifier=DummyNotifier() (no event_bus) and must keep working
    unmodified."""
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())

    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    events = [event for event, kwargs in tm.notifier.events if event == "group_opened"]
    assert len(events) == 1
