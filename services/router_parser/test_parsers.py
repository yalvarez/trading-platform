import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from router_parser.parsers_goldbro_fast import GoldBroFastParser
from router_parser.parsers_goldbro_long import GoldBroLongParser
from router_parser.parsers_goldbro_scalp import GoldBroScalpParser
from router_parser.parsers_torofx import ToroFxParser
from router_parser.parsers_daily_signal import DailySignalParser

samples = {
    'gb_fast': 'Compra ORO ahora @2500',
    'gb_long': 'ORO BUY Entry: 2500-2505, SL: 2490, TP1: 2515, TP2: 2530',
    'gb_scalp': 'ORO SCALP BUY Entry: 2500, SL: 2495, TP1: 2505 (70%), TP2: 2510 (100%)',
    'torofx': 'EURUSD BUY Entry: 1.2500-1.2510, SL: 1.2490, TP: 1.2550, 1.2600',
    'daily': 'GOLD MARKET BUY Entry: 2500-2505, SL: 2490, TP1: 2515, TP2: 2530, TP3: 2550',
}

parsers = [
    GoldBroFastParser(),
    GoldBroLongParser(),
    GoldBroScalpParser(),
    ToroFxParser(),
    DailySignalParser(),
]

for name, text in samples.items():
    print(f"--- Sample: {name} ---")
    matched = False
    for p in parsers:
        try:
            res = p.parse(text)
        except Exception as e:
            res = f"ERROR: {e}"
        if res:
            print(f"Parser {p.__class__.__name__} matched: {res}")
            matched = True
            break
    if not matched:
        print("No parser matched")

print('\nDone')
