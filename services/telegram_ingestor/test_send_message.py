
import os
import asyncio
from telethon import TelegramClient
from dotenv import load_dotenv


# Ruta absoluta al .env en la raíz del proyecto
env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../.env'))
print(f"[DEBUG] Cargando .env desde: {env_path}")
load_dotenv(dotenv_path=env_path, override=True)

api_id_raw = os.getenv('TG_API_ID')
api_hash = os.getenv('TG_API_HASH')
phone = os.getenv('TG_PHONE')
chat_id_raw = os.getenv('TG_TEST_CHAT_ID', '0')
print(f"[DEBUG] TG_API_ID={api_id_raw}")
print(f"[DEBUG] TG_API_HASH={api_hash}")
print(f"[DEBUG] TG_PHONE={phone}")
print(f"[DEBUG] TG_TEST_CHAT_ID={chat_id_raw}")
api_id = int(api_id_raw)
chat_id = int(chat_id_raw)

async def main():
    if not chat_id:
        print('Falta TG_TEST_CHAT_ID')
        return
    async with TelegramClient('telegram_ingestor', api_id, api_hash) as client:
        await client.send_message(chat_id, '✅ Prueba directa desde telegram_ingestor')
        print(f'Mensaje enviado a chat_id={chat_id}')

if __name__ == '__main__':
    asyncio.run(main())
