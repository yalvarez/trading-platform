"""
Tests for TradePulseParser — full signal and fast signal formats.
"""
import pytest
from services.router_parser.parsers_tradepulse import TradePulseParser

FULL_SIGNAL_BUY = """‼️SIGNAL ALERT‼️

PAIR: XAUUSD
ORDER TYPE: BUY
ENTRY PRICE: 4999 -4992


❌STOP LOSS: 4982

✅TAKE PROFIT 1:5020
✅TAKE PROFIT 2:5040

‼️Follow Risk Management rules‼️"""

FULL_SIGNAL_SELL = """‼️SIGNAL ALERT‼️

PAIR: XAUUSD
ORDER TYPE: SELL
ENTRY PRICE: 3100 - 3110

❌STOP LOSS: 3120

✅TAKE PROFIT 1:3080
✅TAKE PROFIT 2:3060
✅TAKE PROFIT 3:3040

‼️Follow Risk Management rules‼️"""

FAST_BUY = "XAUUSD BUY NOW"
FAST_SELL = "XAUUSD SELL NOW"

parser = TradePulseParser()


# ── Full signal ──────────────────────────────────────────────────────────────

class TestFullSignal:
    def test_format_tag(self):
        r = parser.parse(FULL_SIGNAL_BUY)
        assert r is not None
        assert r.format_tag == "TRADEPULSE"

    def test_provider_tag(self):
        r = parser.parse(FULL_SIGNAL_BUY)
        assert r.provider_tag == "TRADE_PULSE"

    def test_symbol_normalized(self):
        r = parser.parse(FULL_SIGNAL_BUY)
        assert r.symbol == "XAUUSD"

    def test_direction_buy(self):
        r = parser.parse(FULL_SIGNAL_BUY)
        assert r.direction == "BUY"

    def test_direction_sell(self):
        r = parser.parse(FULL_SIGNAL_SELL)
        assert r.direction == "SELL"

    def test_entry_range_buy(self):
        r = parser.parse(FULL_SIGNAL_BUY)
        assert r.entry_range is not None
        lo, hi = r.entry_range
        assert lo == 4992.0
        assert hi == 4999.0

    def test_entry_range_sell(self):
        r = parser.parse(FULL_SIGNAL_SELL)
        assert r.entry_range is not None
        lo, hi = r.entry_range
        assert lo == 3100.0
        assert hi == 3110.0

    def test_sl(self):
        r = parser.parse(FULL_SIGNAL_BUY)
        assert r.sl == 4982.0

    def test_tps_buy(self):
        r = parser.parse(FULL_SIGNAL_BUY)
        assert r.tps == [5020.0, 5040.0]

    def test_tps_sell_three(self):
        r = parser.parse(FULL_SIGNAL_SELL)
        assert r.tps == [3080.0, 3060.0, 3040.0]

    def test_not_fast(self):
        r = parser.parse(FULL_SIGNAL_BUY)
        assert r.is_fast is False


# ── Fast signal ──────────────────────────────────────────────────────────────

class TestFastSignal:
    def test_fast_buy_parsed(self):
        r = parser.parse(FAST_BUY)
        assert r is not None

    def test_fast_sell_parsed(self):
        r = parser.parse(FAST_SELL)
        assert r is not None

    def test_fast_format_tag(self):
        r = parser.parse(FAST_BUY)
        assert r.format_tag == "TRADEPULSE"

    def test_fast_is_fast_true(self):
        r = parser.parse(FAST_BUY)
        assert r.is_fast is True

    def test_fast_direction_buy(self):
        r = parser.parse(FAST_BUY)
        assert r.direction == "BUY"

    def test_fast_direction_sell(self):
        r = parser.parse(FAST_SELL)
        assert r.direction == "SELL"

    def test_fast_symbol(self):
        r = parser.parse(FAST_BUY)
        assert r.symbol == "XAUUSD"

    def test_fast_no_sl(self):
        r = parser.parse(FAST_BUY)
        assert r.sl is None

    def test_fast_no_tps(self):
        r = parser.parse(FAST_BUY)
        assert r.tps is None

    def test_fast_with_leading_whitespace(self):
        r = parser.parse("  XAUUSD BUY NOW  ")
        assert r is not None
        assert r.is_fast is True


# ── Negative / disambiguation ────────────────────────────────────────────────

class TestNegative:
    def test_random_text_returns_none(self):
        assert parser.parse("Hello world") is None

    def test_empty_returns_none(self):
        assert parser.parse("") is None

    def test_full_signal_without_alert_returns_none(self):
        text = "PAIR: XAUUSD\nORDER TYPE: BUY\nSTOP LOSS: 100"
        assert parser.parse(text) is None

    def test_other_provider_signal_not_matched(self):
        # GoldBro style — no "SIGNAL ALERT" header
        text = "ORO BUY Entry: 2500-2505, SL: 2490, TP1: 2515, TP2: 2530"
        assert parser.parse(text) is None

    def test_fast_with_sl_not_matched_as_fast(self):
        # Has "NOW" but also has SL → should try full signal path
        text = "XAUUSD BUY NOW\nSTOP LOSS: 4980"
        # Without SIGNAL ALERT header the full path also fails → returns None
        assert parser.parse(text) is None

    def test_full_signal_missing_order_type_returns_none(self):
        text = "‼️SIGNAL ALERT‼️\nPAIR: XAUUSD\nSTOP LOSS: 4982"
        assert parser.parse(text) is None

    def test_full_signal_missing_pair_returns_none(self):
        text = "‼️SIGNAL ALERT‼️\nORDER TYPE: BUY\nSTOP LOSS: 4982"
        assert parser.parse(text) is None
