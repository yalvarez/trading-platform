"""
channel_names.py
Resuelve un chat_id de Telegram a un nombre legible de canal, usando un
mapeo simple mantenido en config (env var CHANNEL_NAMES_JSON, parseada
en app.py). No existe ningun mapeo asi en el sistema hoy -- ver spec
seccion "Nombre de canal".
"""
from typing import Optional


def resolve_channel_name(chat_id: Optional[str], channel_names: dict) -> str:
    if chat_id is None:
        return "N/D"
    return channel_names.get(str(chat_id), str(chat_id))
