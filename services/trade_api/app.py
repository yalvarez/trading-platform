"""
trade_api/app.py
Servicio HTTP independiente para abrir, modificar, cerrar y consultar
trades en MT5 desde aplicaciones externas. No depende del estado en
memoria de trade_orchestrator: opera MT5 directamente via MT5Client.
Ver docs/superpowers/specs/2026-09-03-tradepulse-only-simplification-design.md
seccion 6.
"""
import hmac
import logging
import os

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from services.common.config import Settings
from services.common.mt5_client import MT5Client

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("trade_api")

app = FastAPI(title="trade_api")

TRADE_API_KEY = os.getenv("TRADE_API_KEY", "")
if not TRADE_API_KEY:
    # Fail closed: this service can open/close/modify real MT5 positions, so it
    # must never silently run unauthenticated because an env var was forgotten.
    raise RuntimeError(
        "TRADE_API_KEY debe estar configurada para iniciar trade_api "
        "(generar con: openssl rand -hex 32)."
    )

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
MAGIC = 987654


def _check_api_key(api_key: str | None = Depends(_api_key_header)) -> None:
    if not hmac.compare_digest(api_key or "", TRADE_API_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key invalida o ausente. Incluir header X-API-Key.")


_client_singleton: MT5Client | None = None


def get_mt5_client() -> MT5Client:
    global _client_singleton
    if _client_singleton is not None:
        return _client_singleton
    accounts = Settings.accounts()
    account = next((a for a in accounts if a.get("active")), None)
    if not account:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No hay cuenta MT5 activa configurada")
    _client_singleton = MT5Client(host=account["host"], port=int(account["port"]))
    return _client_singleton


class OpenTradeRequest(BaseModel):
    symbol: str
    direction: str
    volume: float
    sl: float
    tp: float | None = None


class ModifyTradeRequest(BaseModel):
    sl: float | None = None
    tp: float | None = None


class TradeResponse(BaseModel):
    ticket: int
    symbol: str
    direction: str
    volume: float
    sl: float
    tp: float


def _position_to_response(pos) -> TradeResponse:
    direction = "BUY" if getattr(pos, "type", 0) == 0 else "SELL"
    return TradeResponse(
        ticket=int(pos.ticket), symbol=pos.symbol, direction=direction,
        volume=float(pos.volume), sl=float(getattr(pos, "sl", 0.0)), tp=float(getattr(pos, "tp", 0.0)),
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/trades", status_code=status.HTTP_201_CREATED, dependencies=[Depends(_check_api_key)])
async def open_trade(req: OpenTradeRequest, client: MT5Client = Depends(get_mt5_client)) -> TradeResponse:
    order_type = 0 if req.direction.upper() == "BUY" else 1
    price = client.tick_price(req.symbol, req.direction.upper())
    if not price:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"No se pudo obtener precio para {req.symbol}")
    request_payload = {
        "action": 1, "symbol": req.symbol, "volume": float(req.volume), "type": order_type,
        "price": float(price), "sl": float(req.sl), "tp": float(req.tp) if req.tp is not None else 0.0,
        "deviation": 50, "magic": MAGIC, "comment": "trade_api", "type_time": 0, "type_filling": 1,
    }
    res = client.order_send(request_payload)
    if not res or getattr(res, "retcode", None) != 10009:
        detail = getattr(res, "comment", "order_send failed") if res else "no response from MT5"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    ticket = int(res.order)
    pos_list = client.positions_get(ticket=ticket)
    if not pos_list:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Orden ejecutada pero posicion no encontrada")
    return _position_to_response(pos_list[0])


@app.get("/trades", dependencies=[Depends(_check_api_key)])
async def list_trades(client: MT5Client = Depends(get_mt5_client)) -> list[TradeResponse]:
    positions = client.positions_get() or []
    return [_position_to_response(p) for p in positions]


@app.get("/trades/{ticket}", dependencies=[Depends(_check_api_key)])
async def get_trade(ticket: int, client: MT5Client = Depends(get_mt5_client)) -> TradeResponse:
    pos_list = client.positions_get(ticket=ticket)
    if not pos_list:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Ticket {ticket} no encontrado")
    return _position_to_response(pos_list[0])


@app.patch("/trades/{ticket}", dependencies=[Depends(_check_api_key)])
async def modify_trade(ticket: int, req: ModifyTradeRequest, client: MT5Client = Depends(get_mt5_client)) -> TradeResponse:
    pos_list = client.positions_get(ticket=ticket)
    if not pos_list:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Ticket {ticket} no encontrado")
    pos = pos_list[0]
    new_sl = req.sl if req.sl is not None else float(getattr(pos, "sl", 0.0))
    new_tp = req.tp if req.tp is not None else float(getattr(pos, "tp", 0.0))
    request_payload = {"action": 6, "position": ticket, "sl": float(new_sl), "tp": float(new_tp)}
    res = client.order_send(request_payload)
    if not res or getattr(res, "retcode", None) != 10009:
        detail = getattr(res, "comment", "order_send failed") if res else "no response from MT5"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    pos_list = client.positions_get(ticket=ticket)
    return _position_to_response(pos_list[0])


@app.delete("/trades/{ticket}", dependencies=[Depends(_check_api_key)])
async def close_trade(ticket: int, client: MT5Client = Depends(get_mt5_client)) -> dict:
    pos_list = client.positions_get(ticket=ticket)
    if not pos_list:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Ticket {ticket} no encontrado")
    ok = client.partial_close({}, ticket, 100)
    if not ok:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="No se pudo cerrar la posicion")
    return {"status": "closed", "ticket": ticket}
