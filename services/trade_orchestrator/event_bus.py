"""
event_bus.py
Punto unico de emision de eventos de negocio de trade_orchestrator.
Reemplaza TradeManager._notify: siempre escribe al log de auditoria
local (sincrono, antes que cualquier llamada de red) y, si hay Redis
configurado, encola el evento para entrega (con reintento) al webhook
de n8n. Ver spec:
docs/superpowers/specs/2026-09-10-audit-log-and-telegram-notifications-design.md
"""
import logging
import uuid
from datetime import datetime, timezone

from .audit_log import append_event
from .n8n_retry_worker import enqueue

log = logging.getLogger("trade_orchestrator.event_bus")


class EventBus:
    def __init__(self, audit_log_path: str, redis_client=None):
        self.audit_log_path = audit_log_path
        self.redis_client = redis_client

    async def emit(self, event_type: str, channel: str, message: str, payload: dict) -> str:
        event_id = str(uuid.uuid4())
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "channel": channel,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": message,
            "payload": payload,
        }
        append_event(self.audit_log_path, envelope)

        if self.redis_client is None:
            log.warning("[EVENT_BUS] Redis no configurado, evento no se envia a n8n event_id=%s", event_id)
            return event_id

        try:
            await enqueue(self.redis_client, envelope)
        except Exception as e:
            log.warning("[EVENT_BUS] fallo encolando evento a Redis event_id=%s: %s", event_id, e)

        return event_id
