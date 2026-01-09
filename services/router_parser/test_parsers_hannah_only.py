import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from router_parser.parsers_hannah import HannahParser

# Ejemplo de señal Hannah
hannah_signal = '''GOLD BUY NOW

@4460-4457

SL 4454

TP1 4463
TP2 4466'''

parser = HannahParser()
result = parser.parse(hannah_signal)

print("--- Prueba señal Hannah ---")
if result:
    print(f"Parser HannahParser matched: {result}")
    assert result.format_tag == "HANNAH"
    assert result.provider_tag == "hannah"
    assert result.symbol == "XAUUSD"
    assert result.direction == "BUY"
    assert result.entry_range == (4457.0, 4460.0)
    assert result.sl == 4454.0
    assert result.tps == [4463.0, 4466.0]
    print("✔️ Test OK")
else:
    print("❌ Test FAILED: No match")
    assert False
