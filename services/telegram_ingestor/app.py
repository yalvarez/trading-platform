import os, asyncio, json, logging
from telethon import TelegramClient, events
from services.common.config import Settings
from services.common.redis_streams import redis_client, xadd, Streams


# Add container label to log format for Grafana filtering
container_label = os.getenv("CONTAINER_LABEL") or os.getenv("HOSTNAME") or "telegram_ingestor"
log_fmt = f"%(asctime)s %(levelname)s [{container_label}] %(name)s: %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"), format=log_fmt)
log = logging.getLogger("telegram_ingestor")


def build_channel_filter(accounts: list[dict]) -> tuple[set[str], bool]:
    """
    Construye el filtro de canal: union de allowed_channels de TODAS las
    cuentas en ACCOUNTS_JSON (activas o no) -- telegram_ingestor no tiene
    nocion de "cuenta", solo escucha y publica a Redis, asi que el filtro
    opera a nivel de canal, no por cuenta.
    Retorna (allowed_channels, any_account_defines_filter). Si NINGUNA
    cuenta define allowed_channels, any_account_defines_filter es False y
    el llamador no debe filtrar nada (deja pasar todo).
    """
    allowed_channels: set[str] = set()
    any_account_defines_filter = False
    for acct in accounts:
        acct_channels = acct.get("allowed_channels")
        if acct_channels:
            any_account_defines_filter = True
            allowed_channels.update(str(ch) for ch in acct_channels)
    return allowed_channels, any_account_defines_filter


async def main():
    from services.common.env_validator import validate_telegram_ingestor
    validate_telegram_ingestor()

    last_msg = {"ts": None}

    async def watchdog():
        while True:
            now = asyncio.get_event_loop().time()
            last = last_msg["ts"]
            if last is not None and now - last > 600:
                log.warning(f"[WATCHDOG] ⚠️ No se reciben mensajes desde hace {int(now-last)}s. Posible desconexión o bloqueo.")
            await asyncio.sleep(600)  # Solo cada 10 minutos

    s = Settings.load()
    r = await redis_client(s["redis_url"])

    api_id = int(s["TG_API_ID"])
    api_hash = s["TG_API_HASH"]
    phone = s["TG_PHONE"]

    accounts = Settings.accounts()
    allowed_channels, any_account_defines_filter = build_channel_filter(accounts)
    if any_account_defines_filter:
        log.info("[FILTER] Canales permitidos (union de ACCOUNTS_JSON): %s", sorted(allowed_channels))
    else:
        log.info("[FILTER] Ninguna cuenta define allowed_channels -- sin filtro de canal, se procesan todos los mensajes.")

    client = TelegramClient("telegram_ingestor", api_id, api_hash)

    @client.on(events.NewMessage)
    async def handler(event):
        try:
            last_msg["ts"] = asyncio.get_event_loop().time()
            chat_id = str(event.chat_id)
            text = (event.raw_text or "").strip()
            log.debug(f"[HANDLER][RAW] Recibido: chat_id={chat_id} id={event.id} tipo={type(event.message).__name__} texto='{text[:80]}...'")
            if any_account_defines_filter and chat_id not in allowed_channels:
                log.debug(f"[FILTER] Ignorado chat_id={chat_id} (no esta en allowed_channels)")
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