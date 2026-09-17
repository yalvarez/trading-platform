"""
parsers_management.py
Reconocimiento sin LLM del patron "TRADE INVALID ... Close now" que llega
por Telegram como mensaje de gestion. A diferencia de parsers_tradepulse.py,
no produce una senal de apertura: produce una accion de gestion que
router_parser ejecuta directo contra /mgmt/action, sin pasar por n8n/Ollama
(ver docs/superpowers/specs/2026-09-17-direct-close-now-shortcut-design.md).
"""
import re
from typing import Optional

CLOSE_NOW_RE = re.compile(r'TRADE\s+INVALID.*?CLOSE\s+NOW', re.IGNORECASE | re.DOTALL)
DIRECTION_RE = re.compile(r'\b(BUY|SELL)\b', re.IGNORECASE)


def match_close_now(text: str) -> Optional[dict]:
    """
    Retorna {"action": "close_now", "direction_hint": "SELL"|"BUY"|None} si
    el texto es una orden de cierre total reconocible sin LLM (exige ambas
    frases, TRADE INVALID y CLOSE NOW, en ese orden), o None si no lo es --
    el texto sigue su curso normal hacia n8n.
    """
    if not CLOSE_NOW_RE.search(text):
        return None
    direction_m = DIRECTION_RE.search(text)
    direction_hint = direction_m.group(1).upper() if direction_m else None
    return {"action": "close_now", "direction_hint": direction_hint}
