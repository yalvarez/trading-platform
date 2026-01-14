from telethon import TelegramClient
import os

api_id = int(os.getenv("TG_API_ID", "21104104"))
api_hash = os.getenv("TG_API_HASH", "7afb33549783f0315ae6538370c78ab9")
phone = os.getenv("TG_PHONE", "")

# Usa un archivo de sesión separado para la API
client = TelegramClient("telegram_ingestor_api", api_id, api_hash)

async def main():
    await client.start(phone=phone)
    print("Sesión para telegram_ingestor_api creada correctamente.")
    await client.disconnect()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
