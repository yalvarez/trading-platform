from services.trade_orchestrator.event_messages import (
    build_group_opened_message, build_tp1_hit_message, build_tp2_partial_closed_message,
    build_sl_hit_message, build_external_close_message, build_close_now_message,
    build_close_partial_now_message, build_move_sl_be_applied_message,
    build_partial_failure_message,
)


def test_group_opened_message_includes_all_key_fields():
    msg = build_group_opened_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction="BUY",
        entry_price=1.09345, sl=1.09100, tp1=1.09500, tp2=1.09800, volume=0.02,
    )
    assert "Oro Premium" in msg
    assert "61" in msg
    assert "EURUSD" in msg
    assert "BUY" in msg
    assert "1.09345" in msg
    assert "1.091" in msg  # sl
    assert "1.095" in msg  # tp1
    assert "1.098" in msg  # tp2
    assert "0.02" in msg


def test_tp1_hit_message_includes_pnl():
    msg = build_tp1_hit_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction="BUY",
        close_price=1.09500, close_volume=0.01, pnl_money=12.50, account_currency="USD",
    )
    assert "TP1" in msg
    assert "12.50" in msg
    assert "USD" in msg


def test_sl_hit_message_shows_negative_pnl_clearly():
    msg = build_sl_hit_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction="BUY",
        close_price=1.09100, close_volume=0.01, pnl_money=-24.50,
    )
    assert "-24.50" in msg
    assert "STOP" in msg.upper()


def test_external_close_message_flags_it_as_outside_the_system():
    msg = build_external_close_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction="BUY",
        leg="runner", close_price=1.09200, close_volume=0.01, pnl_money=-5.0,
    )
    assert "externo" in msg.lower() or "fuera" in msg.lower()


def test_close_now_message_includes_raw_text_and_total():
    msg = build_close_now_message(
        channel_name="Oro Premium", group_id=61, raw_text="cierren esa operacion",
        leg_results=[
            {"leg": "tp1", "close_price": 1.095, "close_volume": 0.01, "pnl_money": 5.0},
            {"leg": "runner", "close_price": 1.093, "close_volume": 0.01, "pnl_money": 3.2},
        ],
        total_pnl_money=8.2,
    )
    assert "cierren esa operacion" in msg
    assert "8.2" in msg


def test_close_partial_now_message_includes_percent_requested():
    msg = build_close_partial_now_message(
        channel_name="Oro Premium", group_id=61, raw_text="cierra 30%",
        percent_requested=30.0,
        leg_results=[{"leg": "runner", "close_price": 1.093, "close_volume": 0.006, "pnl_money": 1.9}],
    )
    assert "30" in msg


def test_move_sl_be_applied_message_includes_new_sl():
    msg = build_move_sl_be_applied_message(
        channel_name="Oro Premium", group_id=61, new_sl=1.09345, raw_text="pon en be",
    )
    assert "1.09345" in msg
    assert "breakeven" in msg.lower() or "BE" in msg


def test_partial_failure_message_flags_it_needs_review():
    msg = build_partial_failure_message(
        channel_name="Oro Premium", group_id=61,
        leg_summaries=["tp1 (ticket=1, rechazado)"],
    )
    assert "revisar" in msg.lower() or "revis" in msg.lower()


def test_direction_none_handled_gracefully_in_all_functions():
    """Test that direction=None is handled without crashing (degraded output with 'N/D')."""
    # build_group_opened_message
    msg = build_group_opened_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction=None,
        entry_price=1.09345, sl=1.09100, tp1=1.09500, tp2=1.09800, volume=0.02,
    )
    assert "N/D" in msg
    assert "EURUSD" in msg

    # build_tp1_hit_message
    msg = build_tp1_hit_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction=None,
        close_price=1.09500, close_volume=0.01, pnl_money=12.50, account_currency="USD",
    )
    assert "N/D" in msg
    assert "TP1" in msg

    # build_tp2_partial_closed_message
    msg = build_tp2_partial_closed_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction=None,
        close_price=1.09500, close_volume=0.01, pnl_money=5.0, remaining_volume=0.01,
    )
    assert "N/D" in msg
    assert "TP2" in msg

    # build_sl_hit_message
    msg = build_sl_hit_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction=None,
        close_price=1.09100, close_volume=0.01, pnl_money=-24.50,
    )
    assert "N/D" in msg
    assert "STOP" in msg.upper()

    # build_external_close_message
    msg = build_external_close_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD", direction=None,
        leg="runner", close_price=1.09200, close_volume=0.01, pnl_money=-5.0,
    )
    assert "N/D" in msg
    assert "EXTERNO" in msg.upper()


# --- Final fix wave (2026-09-11), Fix 4: tp1_hit_be_failed message ---

def test_tp1_hit_be_failed_message_conveys_the_runner_is_unprotected():
    from services.trade_orchestrator.event_messages import build_tp1_hit_be_failed_message

    msg = build_tp1_hit_be_failed_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD",
        direction="BUY", runner_ticket=123456,
    )
    assert "Oro Premium" in msg
    assert "61" in msg
    assert "EURUSD" in msg
    assert "BUY" in msg
    assert "123456" in msg
    assert "breakeven" in msg.lower()
    # This is the one event where the runner is MORE exposed than normal --
    # the text must say so and ask for manual review.
    assert "manual" in msg.lower()
    assert "NO" in msg


def test_tp1_hit_be_failed_message_handles_direction_none():
    from services.trade_orchestrator.event_messages import build_tp1_hit_be_failed_message

    msg = build_tp1_hit_be_failed_message(
        channel_name="Oro Premium", group_id=61, symbol="EURUSD",
        direction=None, runner_ticket=123456,
    )
    assert "N/D" in msg


# --- Task 2: Timeout message builders ---

def test_tp1_hit_be_timeout_message_conveys_uncertainty_and_urgency():
    from services.trade_orchestrator.event_messages import build_tp1_hit_be_timeout_message

    msg = build_tp1_hit_be_timeout_message(
        channel_name="Oro Premium", group_id=61, symbol="XAUUSD", direction="BUY", runner_ticket=12345,
    )
    assert "TP1" in msg.upper()
    assert "no se pudo confirmar" in msg.lower() or "no pudo confirmar" in msg.lower()
    assert "revisar" in msg.lower()


def test_tp1_hit_be_timeout_message_handles_direction_none():
    from services.trade_orchestrator.event_messages import build_tp1_hit_be_timeout_message

    # Must not crash even if direction is somehow missing, same guard as the
    # other builders in this file (Task 6 of the audit-log plan already
    # established this pattern for every direction-taking builder).
    msg = build_tp1_hit_be_timeout_message(
        channel_name="Oro Premium", group_id=61, symbol="XAUUSD", direction=None, runner_ticket=12345,
    )
    assert isinstance(msg, str) and len(msg) > 0


def test_tp2_partial_timeout_message_conveys_uncertainty():
    from services.trade_orchestrator.event_messages import build_tp2_partial_timeout_message

    msg = build_tp2_partial_timeout_message(
        channel_name="Oro Premium", group_id=61, symbol="XAUUSD", direction="SELL",
    )
    assert "TP2" in msg.upper()
    assert "revisar" in msg.lower()
    assert "%%" not in msg


def test_tp2_partial_timeout_message_handles_direction_none():
    from services.trade_orchestrator.event_messages import build_tp2_partial_timeout_message

    msg = build_tp2_partial_timeout_message(
        channel_name="Oro Premium", group_id=61, symbol="XAUUSD", direction=None,
    )
    assert isinstance(msg, str) and len(msg) > 0
