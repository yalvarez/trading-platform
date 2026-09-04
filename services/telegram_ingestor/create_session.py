import os
from telethon import TelegramClient

api_id = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]

client_name = "telegram_ingestor"
client = TelegramClient(client_name, api_id, api_hash)
client.start()
print("Sesión " + client_name + " creada correctamente")