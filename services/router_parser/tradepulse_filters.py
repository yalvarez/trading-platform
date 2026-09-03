import re

FOLLOWUP_KEYWORDS = [
    "SET BE", "ZERO RISK", "BREAKEVEN", "BREAK EVEN", "ROAD TO TP ONE",
    "SECURE PARTIALS", "DONE", "CLOSE NOW", "DON'T HOLD", "RISK OFF"
]

# Heurística: si NO hay “@rango” o “SL” o “TP” y contiene palabras de seguimiento -> gestión
def looks_like_followup(text: str) -> bool:
    up = (text or "").upper()
    if any(k in up for k in FOLLOWUP_KEYWORDS):        
            return True
    return False
