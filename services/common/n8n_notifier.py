"""
n8n_notifier.py
Cliente HTTP minimo para enviar eventos de trading a un webhook n8n.
n8n es el unico destino de notificaciones/eventos y decide que hacer
con cada uno (reenviar a Telegram, loggear, alertar, etc).
"""
import logging

import httpx

log = logging.getLogger("n8n_notifier")


class N8nWebhookNotifier:
    """Envia eventos de trading como JSON a un webhook n8n via HTTP POST."""

    def __init__(self, webhook_url: str, token: str = ""):
        self.webhook_url = webhook_url
        self.token = token

    async def send_event(self, event: str, **fields) -> bool:
        payload = {"event": event, **fields}
        headers = {"X-N8N-Token": self.token} if self.token else None
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(self.webhook_url, json=payload, headers=headers, timeout=10.0)
            if 200 <= resp.status_code < 300:
                return True
            log.warning("[N8N] webhook respondio status=%s event=%s", resp.status_code, event)
            return False
        except Exception as e:
            log.warning("[N8N] error enviando evento '%s': %s", event, e)
            return False
