"""
n8n_notifier.py
Cliente HTTP minimo para enviar eventos de trading a un webhook n8n.
n8n es el unico destino de notificaciones/eventos y decide que hacer
con cada uno (reenviar a Telegram, loggear, alertar, etc).
"""
import json
import logging

import httpx

log = logging.getLogger("n8n_notifier")

# La tabla de n8n (y el webhook que la alimenta) solo acepta estas columnas.
# Cualquier POST debe ajustarse exactamente a esta forma.
N8N_SCHEMA_FIELDS = ("group_id", "leg", "symbol", "action", "message")


class N8nWebhookNotifier:
    """Envia eventos de trading como JSON a un webhook n8n via HTTP POST."""

    def __init__(self, webhook_url: str, token: str = ""):
        self.webhook_url = webhook_url
        self.token = token

    async def send_event(self, event: str, **fields) -> bool:
        payload = self._build_payload(event, fields)
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

    @staticmethod
    def _build_payload(event: str, fields: dict) -> dict:
        """
        Arma el payload con exactamente las columnas que espera la tabla de
        n8n: group_id, leg, symbol, action, message. `event` mapea a
        `action`. Cualquier campo que no encaje en el esquema (incluidos
        group_id/leg/symbol si vinieran con un tipo no serializable tal
        cual) se serializa y se anexa a `message`, para no perder
        informacion aunque el caller no se haya ajustado del todo al
        esquema.
        """
        remaining = dict(fields)
        message = remaining.pop("message", None)

        payload = {
            "group_id": remaining.pop("group_id", None),
            "leg": remaining.pop("leg", None),
            "symbol": remaining.pop("symbol", None),
            "action": event,
            "message": message or "",
        }

        if remaining:
            extra_json = json.dumps(remaining, default=str, ensure_ascii=False)
            if payload["message"]:
                payload["message"] = f"{payload['message']} | extra={extra_json}"
            else:
                payload["message"] = extra_json

        return payload
