import os, asyncio, json, logging
from telethon import TelegramClient, events
from common.config import Settings, env
from common.redis_streams import redis_client, xadd, Streams

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log = logging.getLogger("telegram_ingestor")

async def main():
    s = Settings.load()
    r = await redis_client(s.redis_url)

    api_id = int(env("TG_API_ID"))
    api_hash = env("TG_API_HASH")
    phone = env("TG_PHONE")
    chats = [c.strip() for c in env("TG_SOURCE_CHATS","").split(",") if c.strip()]

    client = TelegramClient("telegram_ingestor", api_id, api_hash)

    @client.on(events.NewMessage)
    async def handler(event):
        try:
            chat_id = str(event.chat_id)
            log.debug(f"[CHAT_FILTER] chat_id={chat_id} chats={chats}")
            if chats and chat_id not in chats:
                log.debug(f"[CHAT_FILTER] Ignorado chat_id={chat_id} (no está en chats)")
                return
            text = (event.raw_text or "").strip()
            if not text:
                return
            payload = {
                "chat_id": chat_id,
                "message_id": str(event.id),
                "date": event.date.isoformat() if event.date else "",
                "text": text
            }
            await xadd(r, Streams.RAW, payload)
            log.info(f"[RAW] chat={chat_id} msg_id={event.id} len={len(text)} :: {text.replace(chr(10), ' | ')}")
        except Exception as e:
            log.exception(e)

    await client.start(phone=phone)
    log.info("Telegram ingestor running...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())