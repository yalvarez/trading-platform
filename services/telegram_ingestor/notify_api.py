from fastapi import FastAPI, Request
from pydantic import BaseModel
import os, asyncio, logging
from telethon import TelegramClient

app = FastAPI()

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log = logging.getLogger("telegram_ingestor_api")

# Configuración de Telegram
api_id = int(os.getenv("TG_API_ID", "21104104"))
api_hash = os.getenv("TG_API_HASH", "7afb33549783f0315ae6538370c78ab9")
phone = os.getenv("TG_PHONE", "")

# Usa un archivo de sesión separado para la API para evitar conflictos de SQLite
client = TelegramClient("telegram_ingestor_api", api_id, api_hash)

class NotifyRequest(BaseModel):
    chat_id: str
    message: str

@app.on_event("startup")
async def startup_event():
    await client.start(phone=phone)
    log.info("Telegram client started for API")

@app.post("/notify")
async def notify(req: NotifyRequest):
    print(f"[API][NOTIFY] Recibido: chat_id={req.chat_id}, message={req.message}")
    log.info(f"[API][NOTIFY] Recibido: chat_id={req.chat_id}, message={req.message}")
    # Validar que el chat_id sea numérico (int, puede ser negativo)
    if not str(req.chat_id).lstrip('-').isdigit():
        error_msg = f"chat_id inválido: '{req.chat_id}'. Debe ser el ID numérico, no el nombre."
        print(f"[API][NOTIFY][ERROR] {error_msg}")
        log.error(f"[API][NOTIFY][ERROR] {error_msg}")
        return {"status": "error", "detail": error_msg}
    try:
        chat_id = int(req.chat_id)
        await client.send_message(chat_id, req.message)
        print(f"[API][NOTIFY] Mensaje enviado correctamente a {chat_id}")
        log.info(f"[API][NOTIFY] Mensaje enviado correctamente a {chat_id}")
        return {"status": "ok"}
    except Exception as e:
        print(f"[API][NOTIFY][ERROR] {e}")
        log.error(f"[API][NOTIFY][ERROR] {e}")
        return {"status": "error", "detail": str(e)}

@app.get("/health")
async def health():
    return {"status": "ok"}
