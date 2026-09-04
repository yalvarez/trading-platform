from telethon import TelegramClient

api_id = 21104104
api_hash = "7afb33549783f0315ae6538370c78ab9"

client_name = "telegram_ingestor"
client = TelegramClient(client_name, api_id, api_hash)
client.start()
print("Sesión " + client_name + " creada correctamente")