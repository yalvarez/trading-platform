"""
notifications/n8n.py
Adaptador que traduce las llamadas de notificacion del orchestrator
(notify, notify_trade_event) a eventos enviados al webhook n8n.
Reemplaza notifications/telegram.py.
"""
import logging

log = logging.getLogger("trade_orchestrator.notifications.n8n")


class N8nNotifierAdapter:
    """Adaptador desacoplado: todas las llamadas son seguras y no bloquean la gestion principal."""

    def __init__(self, notifier=None):
        self.notifier = notifier

    async def notify(self, target, message: str) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.send_event("message", target=str(target), message=message)
        except Exception as e:
            log.warning("[N8N_ADAPTER] notify failed: %s", e)

    async def notify_trade_event(self, event: str, **kwargs) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.send_event(event, **kwargs)
        except Exception as e:
            log.warning("[N8N_ADAPTER] notify_trade_event failed: %s", e)
