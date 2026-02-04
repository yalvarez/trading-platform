"""
notifications/telegram.py
Centraliza toda la lógica de notificaciones por Telegram para el trade orchestrator.
Incluye adaptadores y helpers para desacoplar la gestión de notificaciones del resto de la lógica de trading.
"""

import logging
from typing import Any

class TelegramNotifierAdapter:
    """
    Adaptador para notificaciones Telegram desacoplado de la lógica de gestión.
    Todas las llamadas aquí deben ser seguras y no bloquear la gestión principal.
    """
    def __init__(self, notifier=None):
        self.notifier = notifier
        self.log = logging.getLogger("trade_orchestrator.notifications.telegram")

    async def notify(self, target: str | int, message: str):
        """
        target: puede ser el nombre de la cuenta (str) o el chat_id (int o str numérico)
        Si es un nombre de cuenta, busca el chat_id en ACCOUNTS_JSON.
        Si es un chat_id numérico, lo usa directamente.
        """
        import os, json
        chat_id = None
        account_name = None
        # Si es numérico, usar como chat_id
        # if isinstance(target, int) or (isinstance(target, str) and target.lstrip('-').isdigit()):
        #     chat_id = int(target)
        # else:
        #     # Buscar chat_id por nombre de cuenta
        #     account_name = target
        #     accounts_env = os.getenv('ACCOUNTS_JSON', '[]')
        #     try:
        #         accounts_list = json.loads(accounts_env)
        #     except Exception:
        #         accounts_list = []
        #     for acct in accounts_list:
        #         if acct.get('name') == account_name:
        #             chat_id = acct.get('chat_id')
        #             break
        # if not chat_id:
        #     self.log.error(f"[NOTIFY][ERROR] No se encontró chat_id para '{target}'")
        #     return
        # if not self.notifier:
        #     self.log.info(f"[NOTIFY][{account_name or chat_id}] {message}")
        #     return
        # try:
        #     if hasattr(self.notifier, 'notify') and callable(getattr(self.notifier, 'notify')):
        #         await self.notifier.notify(str(chat_id), message)
        #     else:
        #         await self.notifier(str(chat_id), message)
        # except Exception as e:
        #     self.log.error(f"[NOTIFY][ERROR] {account_name or chat_id}: {e}")

    async def notify_trade_event(self, event: str, **kwargs: Any):
        account_name = kwargs.get('account_name')
        msg = self.format_event_message(event, **kwargs)
        await self.notify(account_name, msg)

    def format_event_message(self, event: str, **kwargs: Any) -> str:
        if event == 'opened':
            return f"🎯 TRADE OPENED | Cuenta: {kwargs.get('account_name')} | Ticket: {kwargs.get('ticket')} | {kwargs.get('symbol')} {kwargs.get('direction')} | Entry: {kwargs.get('entry_price')} | SL: {kwargs.get('sl_price')} | TP: {kwargs.get('tp_prices')} | Lote: {kwargs.get('lot')} | Provider: {kwargs.get('provider')}"
        elif event == 'tp':
            return f"🎯 TP HIT | Cuenta: {kwargs.get('account_name')} | Ticket: {kwargs.get('ticket')} | {kwargs.get('symbol')} | TP{kwargs.get('tp_index')}: {kwargs.get('tp_price')} | Precio actual: {kwargs.get('current_price')}"
        elif event == 'partial':
            return f"🎯 Partial Close | Cuenta: {kwargs.get('account_name')} | Ticket: {kwargs.get('ticket')} | {kwargs.get('symbol')} | {kwargs.get('close_percent')}% | Motivo: {kwargs.get('reason')}"
        elif event == 'sl':
            return f"❌ SL HIT | Cuenta: {kwargs.get('account_name')} | Ticket: {kwargs.get('ticket')} | {kwargs.get('symbol')} | SL: {kwargs.get('sl_price')} | Close: {kwargs.get('close_price')}"
        elif event == 'tramo':
            return f"🎯 TRAMO HIT | Cuenta: {kwargs.get('account_name')} | Ticket: {kwargs.get('ticket')} | {kwargs.get('symbol')} | Tramo: {kwargs.get('tramo')} | Precio actual: {kwargs.get('current_price')}"
        elif event == 'be':
            return kwargs.get('message')
        elif event == 'trailing':
            return kwargs.get('message')
        elif event == 'addon':
            return f"➕ Addon | Cuenta: {kwargs.get('account_name')} | Ticket: {kwargs.get('ticket')} | {kwargs.get('symbol')} | Precio: {kwargs.get('addon_price')} | Lote: {kwargs.get('addon_lot')}"
        elif event == 'close':
            return kwargs.get('message')
        else:
            return kwargs.get('message')
