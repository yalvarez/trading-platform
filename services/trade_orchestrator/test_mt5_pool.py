import pytest
from unittest.mock import MagicMock, patch

from services.trade_orchestrator.mt5_pool import PooledMT5Client


@pytest.fixture
def pooled_client():
    with patch("services.common.mt5_client.MT5Client") as MockMT5Client:
        instance = MockMT5Client.return_value
        client = PooledMT5Client("mt5_acct1", 8001)
        yield client, instance


def test_history_deals_get_passes_through_to_underlying_client(pooled_client):
    """
    Real production bug found live (2026-09-09): PooledMT5Client -- the
    real client TradeManager uses in production, not the plain MT5Client
    tests exercise directly -- had no history_deals_get passthrough at
    all. Every TradeManager._get_close_price call against a real account
    silently failed with AttributeError (caught by _get_close_price's own
    try/except), degrading every close-price message to "N/D" and making
    it impossible to verify whether a tp1_leg closure actually reached
    tp1_price.
    """
    client, instance = pooled_client
    instance.history_deals_get.return_value = ["deal1", "deal2"]

    result = client.history_deals_get(position=12345)

    assert result == ["deal1", "deal2"]
    instance.history_deals_get.assert_called_once_with(position=12345)


# --- Stuck-lock recovery (2026-09-24). Production evidence: on 2026-09-23 a hung
# RPyC call held PooledMT5Client's lock and every following call to the account
# timed out one after another for up to 42 minutes (70 "colgada tras 10s" that
# day) -- trailing, BE-after-TP1, opens and closes all stalled. ---

import asyncio
import threading
import time


def _hang_until(event):
    def _fn(*args, **kwargs):
        event.wait(5)
        return "late"
    return _fn


@pytest.fixture
def fast_timeouts(monkeypatch):
    monkeypatch.setenv("MT5_LOCK_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setattr(PooledMT5Client, "REPLACE_COOLDOWN_SECONDS", 60.0)


def _start_hung_call(client, release):
    t = threading.Thread(target=lambda: client.positions_get(ticket=1), daemon=True)
    t.start()
    time.sleep(0.05)  # let it grab the lock
    return t


def test_stuck_lock_replaces_connection_and_next_call_succeeds(fast_timeouts):
    with patch("services.common.mt5_client.MT5Client") as MockMT5Client:
        stuck_instance, fresh_instance = MagicMock(name="stuck"), MagicMock(name="fresh")
        MockMT5Client.side_effect = [stuck_instance, fresh_instance]
        release = threading.Event()
        stuck_instance.positions_get.side_effect = _hang_until(release)
        fresh_instance.symbol_info_tick.return_value = "tick"

        client = PooledMT5Client("mt5_acct1", 8001)
        hung = _start_hung_call(client, release)

        result = client.symbol_info_tick("XAUUSD")

        assert result == "tick"
        assert MockMT5Client.call_count == 2  # connection replaced once
        fresh_instance.symbol_info_tick.assert_called_once_with("XAUUSD")
        stuck_instance.symbol_info_tick.assert_not_called()
        release.set(); hung.join(2)


def test_call_that_gave_up_waiting_never_executes_later(fast_timeouts):
    """A call whose caller already gave up must not run late on the old connection
    (a late order_send could leave an orphan position the caller already reverted)."""
    with patch("services.common.mt5_client.MT5Client") as MockMT5Client:
        stuck_instance = MagicMock(name="stuck")
        MockMT5Client.side_effect = [stuck_instance, RuntimeError("terminal down")]
        release = threading.Event()
        stuck_instance.positions_get.side_effect = _hang_until(release)

        client = PooledMT5Client("mt5_acct1", 8001)
        hung = _start_hung_call(client, release)

        with pytest.raises(Exception):
            client.order_send({"action": 1})
        release.set(); hung.join(2)
        time.sleep(0.1)
        stuck_instance.order_send.assert_not_called()


def test_replacement_is_rate_limited_and_fails_fast_as_timeout(fast_timeouts):
    """If the terminal itself is hung, the fresh connection hangs too. Do not keep
    opening connections: within the cooldown, fail fast with a timeout error that the
    existing asyncio.TimeoutError handling in TradeManager already notifies on."""
    from services.trade_orchestrator.mt5_pool import MT5ConnectionStuckError

    with patch("services.common.mt5_client.MT5Client") as MockMT5Client:
        first, second = MagicMock(name="first"), MagicMock(name="second")
        MockMT5Client.side_effect = [first, second, MagicMock(name="third")]
        release = threading.Event()
        first.positions_get.side_effect = _hang_until(release)
        second.positions_get.side_effect = _hang_until(release)

        client = PooledMT5Client("mt5_acct1", 8001)
        hung1 = _start_hung_call(client, release)
        hung2 = _start_hung_call(client, release)  # replaces conn, then hangs on the new one
        time.sleep(0.3)

        started = time.monotonic()
        with pytest.raises(MT5ConnectionStuckError) as exc:
            client.symbol_info_tick("XAUUSD")
        assert time.monotonic() - started < 1.0
        assert isinstance(exc.value, asyncio.TimeoutError)
        assert MockMT5Client.call_count == 2  # no third connection inside the cooldown
        release.set(); hung1.join(2); hung2.join(2)


def test_lock_timeout_defaults_below_call_timeout(monkeypatch):
    monkeypatch.delenv("MT5_LOCK_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("MT5_CALL_TIMEOUT_SECONDS", "10")
    assert PooledMT5Client._lock_timeout() == pytest.approx(8.0)


def test_symbol_info_goes_through_the_lock(pooled_client):
    """symbol_info used to call client.mt5.symbol_info directly, bypassing the lock
    every other MT5 call takes -- concurrent RPyC use against the same terminal."""
    from services.trade_orchestrator.mt5_pool import MT5ClientPool
    client, instance = pooled_client
    MT5ClientPool.invalidate_symbol("mt5_acct1", 8001, "XAUUSD")
    instance.symbol_info.return_value = "info"

    assert client.symbol_info("XAUUSD") == "info"
    instance.symbol_info.assert_called_once_with("XAUUSD")
    instance.mt5.symbol_info.assert_not_called()


# --- No resending non-idempotent calls (2026-10-05). Production evidence: in
# every log since 2026-09-14 the pool resent order_send after "result expired"
# 22 times -- 19 trailing SL moves (harmless to repeat) and 3 opens (groups
# 170, 171, 204). All 3 opens executed TWICE in MT5: "result expired" means the
# request DID reach the terminal and only the answer was lost, so the resend
# always duplicated the position and never rescued a missed one. ---

def _pool_with_failing_first_call(method_name, *, fail_with=Exception("result expired")):
    """PooledMT5Client whose first underlying `method_name` call raises, and a
    reconnected client that would succeed -- to observe whether it resends."""
    patcher = patch("services.common.mt5_client.MT5Client")
    MockMT5Client = patcher.start()
    first, reconnected = MagicMock(name="first"), MagicMock(name="reconnected")
    MockMT5Client.side_effect = [first, reconnected]
    getattr(first, method_name).side_effect = fail_with
    getattr(reconnected, method_name).return_value = "resent-result"
    client = PooledMT5Client("mt5_acct1", 8001)
    return client, first, reconnected, patcher


def test_order_send_opening_a_position_is_never_resent_after_an_error():
    client, first, reconnected, patcher = _pool_with_failing_first_call("order_send")
    try:
        with pytest.raises(Exception, match="result expired"):
            client.order_send({"action": 1, "symbol": "XAUUSD", "volume": 0.02})
        first.order_send.assert_called_once()
        reconnected.order_send.assert_not_called()
    finally:
        patcher.stop()


def test_order_send_still_reconnects_after_a_non_resent_failure():
    """Not resending must not leave the dead connection in place: the next
    call has to go through the reconnected client."""
    client, first, reconnected, patcher = _pool_with_failing_first_call("order_send")
    try:
        with pytest.raises(Exception):
            client.order_send({"action": 1})
        reconnected.positions_get.return_value = ["pos"]
        assert client.positions_get() == ["pos"]
    finally:
        patcher.stop()


def test_order_send_modifying_sl_tp_is_still_resent_after_reconnect():
    """action=6 (TRADE_ACTION_SLTP) sets absolute SL/TP values: sending it twice
    leaves the position exactly as sending it once -- safe to resend, and the 19
    real trailing resends since 2026-09-14 relied on it."""
    client, first, reconnected, patcher = _pool_with_failing_first_call("order_send")
    try:
        assert client.order_send({"action": 6, "position": 1, "sl": 1.0, "tp": 2.0}) == "resent-result"
        reconnected.order_send.assert_called_once()
    finally:
        patcher.stop()


def test_partial_close_is_never_resent_after_an_error():
    """A resent 50% partial would close 50% of the remainder too (~75% total)."""
    client, first, reconnected, patcher = _pool_with_failing_first_call("partial_close")
    try:
        with pytest.raises(Exception, match="result expired"):
            client.partial_close({}, 123, 50)
        first.partial_close.assert_called_once()
        reconnected.partial_close.assert_not_called()
    finally:
        patcher.stop()


def test_read_calls_are_still_resent_after_reconnect():
    client, first, reconnected, patcher = _pool_with_failing_first_call("positions_get")
    try:
        assert client.positions_get() == "resent-result"
    finally:
        patcher.stop()
