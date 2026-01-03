import re

FOLLOWUP_KEYWORDS = [
    "GANANCIAS", "PROFITS", "BREAKEVEN", "BREAK EVEN", "PUNTO DE EQUILIBRIO",
    "CIERRA", "CERRAR", "CERRANDO", "ASEGURANDO", "RISK OFF", "QUITANDO EL RIESGO",
    "CORRIENDO", "PIPS DESDE", "RECOGER", "SCALPERS", "MANTENER", "CAPAS"
]

# Heurística: si NO hay “@rango” o “SL” o “TP” y contiene palabras de seguimiento -> gestión
def looks_like_followup(text: str) -> bool:
    up = (text or "").upper()
    if any(k in up for k in FOLLOWUP_KEYWORDS):
        has_entry = bool(re.search(r"@\s*\d+", text)) or ("@" in text)
        has_sl = "SL" in up or "STOP" in up
        has_tp = "TP" in up or "TAKE PROFIT" in up
        if not (has_entry and has_sl):
            # normalmente señales formales tienen @ + SL mínimo
            return True
    return False
