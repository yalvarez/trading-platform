from telethon import TelegramClient

api_id = 21104104
api_hash = "7afb33549783f0315ae6538370c78ab9"

client = TelegramClient("telegram_ingestor_api", api_id, api_hash)
client.start()
print("Sesión creada correctamente")