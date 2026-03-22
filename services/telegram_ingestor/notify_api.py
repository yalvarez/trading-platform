from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, status, Depends
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, field_validator
import os, logging, time
from collections import defaultdict
from telethon import TelegramClient

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("telegram_ingestor_api")

# Configuracion de Telegram
api_id = int(os.getenv("TG_API_ID", "0"))
api_hash = os.getenv("TG_API_HASH", "")
phone = os.getenv("TG_PHONE", "")

if not api_id or not api_hash:
    log.warning("[NOTIFY_API] TG_API_ID o TG_API_HASH no configurados")

# API Key para autenticacion del endpoint /notify
NOTIFY_API_KEY = os.getenv("NOTIFY_API_KEY", "")
if not NOTIFY_API_KEY:
    log.warning("[NOTIFY_API] NOTIFY_API_KEY no configurada - endpoint /notify desprotegido")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Rate limiting simple en memoria: max 30 req/min por IP
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW = 60  # segundos

client = TelegramClient("telegram_ingestor_api", api_id, api_hash)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await client.start(phone=phone)
    log.info("[NOTIFY_API] Telegram client iniciado")
    yield
    await client.disconnect()
    log.info("[NOTIFY_API] Telegram client desconectado")


app = FastAPI(lifespan=lifespan)


class NotifyRequest(BaseModel):
    chat_id: str
    message: str

    @field_validator("chat_id")
    @classmethod
    def chat_id_must_be_numeric(cls, v: str) -> str:
        if not str(v).lstrip("-").isdigit():
            raise ValueError(f"chat_id debe ser un ID numerico, recibido: '{v}'")
        return v

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message no puede estar vacio")
        if len(v) > 4096:
            raise ValueError("message excede el limite de 4096 caracteres de Telegram")
        return v


def _check_rate_limit(client_ip: str) -> None:
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    _rate_limit_store[client_ip] = [t for t in _rate_limit_store[client_ip] if t > window_start]
    if len(_rate_limit_store[client_ip]) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit excedido: max {RATE_LIMIT_REQUESTS} requests por {RATE_LIMIT_WINDOW}s",
        )
    _rate_limit_store[client_ip].append(now)


def _check_api_key(api_key: str | None = Depends(api_key_header)) -> None:
    if NOTIFY_API_KEY and api_key != NOTIFY_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key invalida o ausente. Incluir header X-API-Key.",
        )


@app.post("/notify", dependencies=[Depends(_check_api_key)])
async def notify(req: NotifyRequest, request: Request) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    log.info("[NOTIFY] chat_id=%s len_msg=%d ip=%s", req.chat_id, len(req.message), client_ip)
    try:
        chat_id = int(req.chat_id)
        await client.send_message(chat_id, req.message)
        log.info("[NOTIFY] Mensaje enviado a chat_id=%s", chat_id)
        return {"status": "ok"}
    except Exception as e:
        log.error("[NOTIFY][ERROR] chat_id=%s error=%s", req.chat_id, e)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "connected": client.is_connected()}
