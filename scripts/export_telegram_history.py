"""
Exporta el historial de canales de Telegram a JSON para analisis/backtests.

Uso (desde la raiz del repo, con TG_API_ID/TG_API_HASH en .env):
    python scripts/export_telegram_history.py --session telegram_ingestor_api \
        --days 90 --out data/telegram_history

NO usar la sesion del ingestor en produccion (telegram_ingestor.session) mientras
el ingestor esta corriendo: dos clientes con la misma clave de autorizacion pueden
provocar AUTH_KEY_DUPLICATED y dejar al ingestor sin sesion.

Limitacion: Telegram solo devuelve el texto FINAL de cada mensaje y la fecha de su
ultima edicion (edit_date). El texto original de un mensaje editado no se puede
recuperar despues, asi que este export no permite saber si una senal se publico
primero sin niveles ("XAUUSD SELL NOW") y se completo editandola.
"""
import argparse
import asyncio
import datetime as dt
import json
import os

from telethon import TelegramClient

CHANNELS = {
    "tradepulse": -1003321565807,
    "ahmed": -1002841150806,
    "goldbrothers": -1003816701426,
    "veritas": -1003236994394,
    "torofx": -1001558718570,
    "dailysig": -1002025086488,
}


def _msg_to_dict(m) -> dict:
    return {
        "id": m.id,
        "date": m.date.isoformat() if m.date else None,
        "edit_date": m.edit_date.isoformat() if m.edit_date else None,
        "text": m.raw_text or "",
        "reply_to": m.reply_to_msg_id,
        "media": type(m.media).__name__ if m.media else None,
        "grouped_id": m.grouped_id,
        "views": m.views,
    }


def _read_env(path: str = ".env") -> dict:
    """Lee KEY=VALUE de .env sin depender de python-dotenv (no esta en todas las imagenes)."""
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


async def export(session: str, days: int, out_dir: str, names: list[str]) -> None:
    env = _read_env()
    client = TelegramClient(session, int(env["TG_API_ID"]), env["TG_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit(f"La sesion '{session}' no esta autorizada.")
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    os.makedirs(out_dir, exist_ok=True)
    for name in names:
        chat_id = CHANNELS[name]
        msgs = []
        async for m in client.iter_messages(chat_id):
            if m.date < since:
                break
            msgs.append(_msg_to_dict(m))
        msgs.reverse()
        entity = await client.get_entity(chat_id)
        doc = {
            "channel": name,
            "title": getattr(entity, "title", None),
            "chat_id": chat_id,
            "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "since": since.isoformat(),
            "note": "Texto FINAL de cada mensaje; Telegram no conserva el texto original de los editados.",
            "messages": msgs,
        }
        path = os.path.join(out_dir, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        print(f"{name}: {len(msgs)} mensajes -> {path}")
    await client.disconnect()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--session", required=True, help="ruta de la sesion Telethon (sin .session)")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--out", default="data/telegram_history")
    p.add_argument("--channels", nargs="*", default=list(CHANNELS), choices=list(CHANNELS))
    a = p.parse_args()
    asyncio.run(export(a.session, a.days, a.out, a.channels))


if __name__ == "__main__":
    main()
