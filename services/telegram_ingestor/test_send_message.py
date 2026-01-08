import os
import asyncio
from telethon import TelegramClient

api_id = int(os.getenv('TG_API_ID'))
api_hash = os.getenv('TG_API_HASH')
phone = os.getenv('TG_PHONE')
chat_id = int(os.getenv('TG_TEST_CHAT_ID', '0'))  # Define TG_TEST_CHAT_ID en tu .env o pásalo por env

async def main():
    if not chat_id:
        print('Falta TG_TEST_CHAT_ID')
        return
    async with TelegramClient('telegram_ingestor', api_id, api_hash) as client:
        await client.send_message(chat_id, '✅ Prueba directa desde telegram_ingestor')
        print(f'Mensaje enviado a chat_id={chat_id}')

if __name__ == '__main__':
    asyncio.run(main())
