"""
n8n_event_client.py
Cliente HTTP para el unico webhook de eventos de n8n (auditoria +
notificaciones). A diferencia de services/common/n8n_notifier.py, este
cliente envia el envelope completo tal cual (event_id, event_type,
channel, timestamp, message, payload) sin transformarlo -- n8n decide
que hacer segun `channel`.
"""
import logging

import httpx

log = logging.getLogger("trade_orchestrator.n8n_event_client")


class N8nEventClient:
    def __init__(self, webhook_url: str, token: str = ""):
        self.webhook_url = webhook_url
        self.token = token

    async def post_event(self, envelope: dict) -> bool:
        headers = {"X-N8N-Token": self.token} if self.token else None
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(self.webhook_url, json=envelope, headers=headers, timeout=10.0)
            if 200 <= resp.status_code < 300:
                return True
            log.warning("[N8N_EVENT] webhook respondio status=%s event_id=%s", resp.status_code, envelope.get("event_id"))
            return False
        except Exception as e:
            log.warning("[N8N_EVENT] error enviando evento event_id=%s: %s", envelope.get("event_id"), e)
            return False
