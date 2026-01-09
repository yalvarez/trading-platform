import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from services.router_parser.parsers_base import ParseResult, SignalParser

import pytest
from services.router_parser.parsers_goldbro_fast import GoldBroFastParser
from services.router_parser.parsers_goldbro_long import GoldBroLongParser
from services.router_parser.parsers_goldbro_scalp import GoldBroScalpParser
from services.router_parser.parsers_torofx import ToroFxParser
from services.router_parser.parsers_daily_signal import DailySignalParser
from services.router_parser.parsers_limitless import LimitlessParser
from services.router_parser.parsers_hannah import HannahParser

@pytest.mark.parametrize("parser_cls,text,expected", [
    (GoldBroFastParser, "Compra ORO ahora @2500", "GB_FAST"),
    (GoldBroLongParser, "ORO BUY Entry: 2500-2505, SL: 2490, TP1: 2515, TP2: 2530", "GB_LONG"),
    (GoldBroScalpParser, "ORO SCALP BUY Entry: 2500, SL: 2495, TP1: 2505 (70%), TP2: 2510 (100%)", "GB_SCALP"),
    (ToroFxParser, "EURUSD BUY Entry: 1.2500-1.2510, SL: 1.2490, TP: 1.2550, 1.2600", "TOROFX"),
    (DailySignalParser, "GOLD MARKET BUY Entry: 2500-2505, SL: 2490, TP1: 2515, TP2: 2530, TP3: 2550", "DAILY_SIGNAL"),
    (LimitlessParser, "GOLD SELL NOW\nZone: 4473 - 4475\nTP 1: 4470\nTP 2: 4468\nRisk Price: 4478", "LIMITLESS"),
    (HannahParser, "GOLD BUY NOW\n@4460-4457\nSL 4454\nTP1 4463\nTP2 4466", "HANNAH"),
    # Casos límite y negativos
    (GoldBroLongParser, "GOLD SELL NOW\nZone: 4473 - 4475\nTP 1: 4470\nTP 2: 4468\nRisk Price: 4478", None),
    (GoldBroFastParser, "GOLD SELL NOW\nZone: 4473 - 4475\nTP 1: 4470\nTP 2: 4468\nRisk Price: 4478", None),
])
def test_parsers_cases(parser_cls, text, expected):
    parser = parser_cls()
    result = parser.parse(text)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert result.format_tag == expected
