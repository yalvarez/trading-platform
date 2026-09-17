import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from parsers_management import match_close_now


def test_matches_the_real_incident_message():
    text = "XAUUSD SELL TRADE INVALID ❌\n\nClose now"
    result = match_close_now(text)
    assert result == {"action": "close_now", "direction_hint": "SELL"}


def test_matches_buy_variant():
    text = "XAUUSD BUY TRADE INVALID\nClose now all"
    result = match_close_now(text)
    assert result == {"action": "close_now", "direction_hint": "BUY"}


def test_direction_hint_is_none_when_text_names_no_direction():
    text = "TRADE INVALID\nClose now"
    result = match_close_now(text)
    assert result == {"action": "close_now", "direction_hint": None}


def test_direction_hint_is_uppercased_even_if_text_is_lowercase():
    text = "trade invalid please close now the buy position"
    result = match_close_now(text)
    assert result == {"action": "close_now", "direction_hint": "BUY"}


def test_does_not_match_close_now_alone():
    assert match_close_now("Close now") is None


def test_does_not_match_trade_invalid_alone():
    assert match_close_now("TRADE INVALID") is None


def test_does_not_match_reversed_order():
    assert match_close_now("Close now because TRADE INVALID") is None


def test_does_not_match_fast_signal():
    assert match_close_now("XAUUSD SELL NOW") is None


def test_takes_first_direction_when_two_are_present():
    text = "XAUUSD SELL TRADE INVALID, the BUY stays\nClose now"
    result = match_close_now(text)
    assert result == {"action": "close_now", "direction_hint": "SELL"}
