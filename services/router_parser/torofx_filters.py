def looks_like_torofx_management(text: str) -> bool:
    up = (text or "").upper()
    # Solo palabras/frases realmente únicas de gestión TOROFX
    keywords = [
        "ASEGURANDO", "QUITANDO EL RIESGO", "TOMAR PARCIAL", "TOMANDO PARCIAL", "CIERRO MI ENTRADA", "CERRANDO EL RIESGO"
    ]
    # Si contiene 'Target: open' también es TOROFX
    if "TARGET: OPEN" in up:
        return True
    return any(k in up for k in keywords)