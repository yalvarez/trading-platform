import os
from telethon import TelegramClient

api_id = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]

client = TelegramClient("telegram_ingestor_api", api_id, api_hash)
client.start()
print("Sesión creada correctamente")