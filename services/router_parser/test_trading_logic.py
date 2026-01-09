import pytest
from router_parser.parsers_goldbro_fast import GoldBroFastParser
from router_parser.parsers_goldbro_long import GoldBroLongParser
from router_parser.parsers_goldbro_scalp import GoldBroScalpParser
from router_parser.parsers_torofx import ToroFxParser
from router_parser.parsers_daily_signal import DailySignalParser
from router_parser.parsers_limitless import LimitlessParser
from router_parser.parsers_hannah import HannahParser

@pytest.mark.parametrize("parser_cls,text,expected", [
    # GB_FAST should not match complete signals
    (GoldBroFastParser, "GOLD SELL NOW\nZone: 4473 - 4475\nTP 1: 4470\nTP 2: 4468\nRisk Price: 4478", None),
    # GB_LONG should not match signals with Risk Price
    (GoldBroLongParser, "GOLD SELL NOW\nZone: 4473 - 4475\nTP 1: 4470\nTP 2: 4468\nRisk Price: 4478", None),
    # LIMITLESS should match signals with Risk Price
    (LimitlessParser, "GOLD SELL NOW\nZone: 4473 - 4475\nTP 1: 4470\nTP 2: 4468\nRisk Price: 4478", "LIMITLESS"),
    # GB_LONG should match classic long
    (GoldBroLongParser, "ORO BUY Entry: 2500-2505, SL: 2490, TP1: 2515, TP2: 2530", "GB_LONG"),
    # GB_FAST should match fast signal
    (GoldBroFastParser, "Compra ORO ahora @2500", "GB_FAST"),
    # HANNAH should match Hannah format
    (HannahParser, "GOLD BUY NOW\n@4460-4457\nSL 4454\nTP1 4463\nTP2 4466", "HANNAH"),
])
def test_parsers(parser_cls, text, expected):
    parser = parser_cls()
    result = parser.parse(text)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert result.format_tag == expected

# Puedes agregar más tests para deduplicación, errores de conexión, autotrading, etc.
