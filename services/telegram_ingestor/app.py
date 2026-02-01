import os, asyncio, json, logging
from telethon import TelegramClient, events
from services.common.config import Settings
from services.common.redis_streams import redis_client, xadd, Streams


# Add container label to log format for Grafana filtering
container_label = os.getenv("CONTAINER_LABEL") or os.getenv("HOSTNAME") or "telegram_ingestor"
log_fmt = f"%(asctime)s %(levelname)s [{container_label}] %(name)s: %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"), format=log_fmt)
log = logging.getLogger("telegram_ingestor")

async def main():
    last_msg = {"ts": None}

    async def watchdog():
        while True:
            now = asyncio.get_event_loop().time()
            last = last_msg["ts"]
            if last is not None and now - last > 600:
                log.warning(f"[WATCHDOG] ⚠️ No se reciben mensajes desde hace {int(now-last)}s. Posible desconexión o bloqueo.")
            await asyncio.sleep(600)  # Solo cada 10 minutos

    import json
    from services.common.config import CHANNELS_CONFIG_JSON
    s = Settings.load()
    r = await redis_client(s["redis_url"])

    api_id = int(s["TG_API_ID"])
    api_hash = s["TG_API_HASH"]
    phone = s["TG_PHONE"]
    try:
        channels_config = json.loads(CHANNELS_CONFIG_JSON)
        chats = list(channels_config.keys())
    except Exception as e:
        log.warning(f"CHANNELS_CONFIG_JSON parse error: {e}")
        chats = []

    client = TelegramClient("telegram_ingestor", api_id, api_hash)

    @client.on(events.NewMessage)
    async def handler(event):
        try:
            last_msg["ts"] = asyncio.get_event_loop().time()
            chat_id = str(event.chat_id)
            text = (event.raw_text or "").strip()
            log.debug(f"[HANDLER][RAW] Recibido: chat_id={chat_id} id={event.id} tipo={type(event.message).__name__} texto='{text[:80]}...'")
            if chats and chat_id not in chats:
                log.warning(f"[CHAT_FILTER] Ignorado chat_id={chat_id} (no está en chats). Lista de chats permitidos: {chats}")
                return
            if not text:
                log.warning(f"[HANDLER] Mensaje vacío ignorado: chat_id={chat_id} id={event.id}")
                return
            payload = {
                "chat_id": chat_id,
                "message_id": str(event.id),
                "date": event.date.isoformat() if event.date else "",
                "text": text
            }
            log.info(f"[RECEIVED] Mensaje recibido: chat_id={chat_id} id={event.id} texto='{text[:80]}...'")
            try:
                await xadd(r, Streams.RAW, payload)
            except Exception as re:
                log.error(f"[REDIS][EXCEPTION] Error al escribir en Redis: {re}")
                log.exception(re)
        except Exception as e:
            log.error(f"[HANDLER][EXCEPTION] chat_id={getattr(event, 'chat_id', None)} id={getattr(event, 'id', None)} error={e}")
            log.exception(e)

    asyncio.create_task(watchdog())

    try:
        await client.start(phone=phone)
        log.info("[CONNECT] Conexión a Telegram exitosa.")
    except Exception as e:
        log.error(f"[CONNECT][ERROR] Fallo al conectar a Telegram: {e}")
        raise
    log.info("Telegram ingestor running...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())