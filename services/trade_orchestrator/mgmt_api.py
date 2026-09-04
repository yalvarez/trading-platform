"""
mgmt_api.py
Endpoint HTTP /mgmt/action que recibe decisiones de gestion desde un
flujo n8n/Ollama externo, para mensajes del canal que el parser de
senales no reconoce (dual-TP spec seccion 5.2). Se monta junto al
consumer de Redis Streams de trade_orchestrator, en el mismo proceso,
porque necesita el TradeManager en memoria para resolver el grupo
activo por simbolo.
"""
import hmac
import os
import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

log = logging.getLogger("trade_orchestrator.mgmt_api")

_action_key_header = APIKeyHeader(name="X-N8N-Action-Key", auto_error=False)


class Correction(BaseModel):
    field: str
    value: float


class MgmtActionRequest(BaseModel):
    action: str
    symbol: str
    raw_text: str
    correction: Optional[Correction] = None


def create_mgmt_app(trade_manager) -> FastAPI:
    app = FastAPI(title="trade_orchestrator-mgmt")
    action_api_key = os.getenv("N8N_ACTION_API_KEY", "")
    if not action_api_key:
        # Fail closed: an unauthenticated management-action endpoint can open/close
        # real MT5 positions, so refuse to start rather than silently serving
        # unprotected. Set N8N_ACTION_API_KEY (openssl rand -hex 32) before deploying.
        raise RuntimeError(
            "N8N_ACTION_API_KEY debe estar configurada para iniciar trade_orchestrator "
            "(el endpoint /mgmt/action nunca debe correr sin autenticacion)."
        )

    def _check_key(api_key: str | None = Depends(_action_key_header)) -> None:
        if not hmac.compare_digest(api_key or "", action_api_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key invalida o ausente.")

    @app.post("/mgmt/action", dependencies=[Depends(_check_key)])
    async def mgmt_action(req: MgmtActionRequest) -> dict:
        correction = req.correction.model_dump() if req.correction else None
        try:
            result = await trade_manager.apply_mgmt_action(
                action=req.action, symbol=req.symbol, raw_text=req.raw_text, correction=correction,
            )
        except Exception as e:
            log.exception("[MGMT_API] apply_mgmt_action fallo inesperadamente: action=%s symbol=%s", req.action, req.symbol)
            return {"status": "failed", "reason": "internal_error", "detail": str(e)}
        return result

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app
