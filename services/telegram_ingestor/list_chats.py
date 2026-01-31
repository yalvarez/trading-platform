from telethon import TelegramClient
from telethon.sessions import StringSession
import os

# Configuración desde variables de entorno o .env
API_ID = int(os.getenv("TG_API_ID", ""))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION_FILE = os.getenv("TELEGRAM_SESSION", "telegram_ingestor_api.session")

# Si usas StringSession, carga el string, si no, usa el archivo
if os.path.exists(SESSION_FILE):
    session = SESSION_FILE
else:
    session = None

async def main():
    async with TelegramClient(session, API_ID, API_HASH) as client:
        print("Chats accesibles por la sesión:")
        async for dialog in client.iter_dialogs():
            print(f"Nombre: {dialog.name}")
            print(f"chat_id: {dialog.id}")
            print(f"Tipo: {type(dialog.entity).__name__}")
            print("---")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
