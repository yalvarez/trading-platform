def looks_like_torofx_management(text: str) -> bool:
    up = (text or "").upper()
    keywords = ["ASEGURANDO", "QUITANDO EL RIESGO", "CERRANDO", "PARCIAL", "PIPS", "+", "ENTRADA"]
    return any(k in up for k in keywords)