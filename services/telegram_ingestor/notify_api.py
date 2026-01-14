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
    try:
        await client.send_message(req.chat_id, req.message)
        return {"status": "ok"}
    except Exception as e:
        log.error(f"[API][ERROR] {e}")
        return {"status": "error", "detail": str(e)}

@app.get("/health")
async def health():
    return {"status": "ok"}
