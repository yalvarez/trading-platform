"""
telegram_notifier.py
Notificador Telegram unificado para eventos de trading.
RemoteTelegramNotifier envia notificaciones via HTTP al servicio telegram_ingestor.
TelegramNotifier envia directamente usando un TelegramClient (Telethon).
Ambas clases implementan la misma interfaz de metodos para ser intercambiables.
"""
import logging
import os
import json
from dataclasses import dataclass
from typing import Optional

import httpx
from telethon import TelegramClient

log = logging.getLogger("telegram_notifier")


@dataclass
class NotificationConfig:
    """Configuracion de destino de notificaciones por cuenta."""
    account_name: str
    chat_id: Optional[int]
    enabled: bool = True


class RemoteTelegramNotifier:
    """
    Envia notificaciones via HTTP al endpoint /notify del telegram_ingestor.
    Resuelve el chat_id de cada cuenta desde ACCOUNTS_JSON o config_provider.
    """

    def __init__(self, api_url: str, api_key: str = "", config_provider=None):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key or os.getenv("NOTIFY_API_KEY", "")
        self._config_provider = config_provider

    def _resolve_chat_id(self, account_name: str) -> Optional[int]:
        """Busca el chat_id de una cuenta por nombre."""
        # Intentar desde config_provider primero
        if self._config_provider:
            try:
                accounts = self._config_provider.get_accounts()
                for acct in accounts:
                    if acct.get("name") == account_name:
                        return acct.get("chat_id")
            except Exception:
                pass
        # Fallback: ACCOUNTS_JSON env var
        try:
            accounts = json.loads(os.getenv("ACCOUNTS_JSON", "[]"))
            for acct in accounts:
                if acct.get("name") == account_name:
                    return acct.get("chat_id")
        except Exception:
            pass
        return None

    async def notify(self, chat_id: str | int, message: str) -> None:
        try:
            chat_id_int = int(str(chat_id))
        except (ValueError, TypeError):
            log.error("[NOTIFY][SKIP] chat_id '%s' no es un entero valido.", chat_id)
            return
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.api_url}/notify",
                    json={"chat_id": str(chat_id_int), "message": message},
                    headers=headers,
                )
                resp.raise_for_status()
        except Exception as e:
            log.error("[NOTIFY][ERROR] chat_id=%s error=%s", chat_id_int, e)

    async def notify_trade_opened(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
        direction: str,
        entry_price: float,
        sl_price: Optional[float] = None,
        tp_prices: Optional[list] = None,
        lot: float = 0.0,
        provider: str = "UNKNOWN",
    ) -> None:
        chat_id = self._resolve_chat_id(account_name)
        if not chat_id:
            log.warning("[NOTIFY][SKIP] Sin chat_id para cuenta '%s'", account_name)
            return
        msg = f"🎯 TRADE OPENED\n━━━━━━━━━━━━━━━━\n📊 Account: `{account_name}`\n🏷️ Provider: `{provider}`\n📈 Symbol: `{symbol}` {direction}\n🎲 Ticket: `{ticket}`\n📍 Entry: `{entry_price}`\n"
        if lot > 0:
            msg += f"📦 Lot: `{lot:.2f}`\n"
        if sl_price is not None:
            msg += f"🛑 SL: `{sl_price}`\n"
        if tp_prices:
            msg += "🎁 TPs:\n"
            for i, tp in enumerate(tp_prices, 1):
                msg += f"   TP{i}: `{tp}`\n"
        await self.notify(chat_id, msg)

    async def notify_tp_hit(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
        tp_index: int,
        tp_price: float,
        current_price: float,
    ) -> None:
        chat_id = self._resolve_chat_id(account_name)
        if not chat_id:
            return
        msg = f"🎉 TP HIT\n━━━━━━━━━━━━━━━━\n📊 Account: `{account_name}`\n📈 Symbol: `{symbol}`\n🎯 TP{tp_index + 1}: `{tp_price}`\n💰 Precio actual: `{current_price}`\n🏷️ Ticket: `{ticket}`\n"
        await self.notify(chat_id, msg)

    async def notify_partial_close(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
        close_percent: float,
        close_price: float,
        closed_volume: float,
    ) -> None:
        chat_id = self._resolve_chat_id(account_name)
        if not chat_id:
            return
        msg = f"📉 PARTIAL CLOSE\n━━━━━━━━━━━━━━━━\n📊 Account: `{account_name}`\n📈 Symbol: `{symbol}`\n📦 Cerrado: `{closed_volume:.2f}` ({close_percent:.0f}%)\n💹 Precio: `{close_price}`\n🏷️ Ticket: `{ticket}`\n"
        await self.notify(chat_id, msg)

    async def notify_sl_hit(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
        sl_price: float,
        loss: float,
    ) -> None:
        chat_id = self._resolve_chat_id(account_name)
        if not chat_id:
            return
        msg = f"❌ STOP LOSS HIT\n━━━━━━━━━━━━━━━━\n📊 Account: `{account_name}`\n📈 Symbol: `{symbol}`\n🛑 SL: `{sl_price}`\n💔 Loss: `-{loss:.2f}` USD\n🏷️ Ticket: `{ticket}`\n"
        await self.notify(chat_id, msg)

    async def notify_error(self, account_name: str, error_type: str, error_message: str) -> None:
        chat_id = self._resolve_chat_id(account_name)
        if not chat_id:
            return
        msg = f"🚨 ERROR\n━━━━━━━━━━━━━━━━\n📊 Account: `{account_name}`\n⚠️ Tipo: `{error_type}`\n📝 Mensaje: `{error_message}`\n"
        await self.notify(chat_id, msg)


class TelegramNotifier:
    """
    Envia notificaciones directamente usando Telethon TelegramClient.
    Usar cuando el servicio tiene acceso directo al cliente Telegram.
    """

    def __init__(self, telegram_client: TelegramClient, notify_configs: list[NotificationConfig]):
        self.client = telegram_client
        self.config_by_account: dict[str, NotificationConfig] = {
            cfg.account_name: cfg for cfg in notify_configs
        }

    async def notify(self, account_name: str, message: str) -> None:
        await self._send(account_name, message)

    async def notify_trade_opened(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
        direction: str,
        entry_price: float,
        sl_price: Optional[float] = None,
        tp_prices: Optional[list[float]] = None,
        lot: float = 0.0,
        provider: str = "UNKNOWN",
    ) -> None:
        msg = f"🎯 **TRADE OPENED**\n━━━━━━━━━━━━━━━━━\n📊 Account: `{account_name}`\n🏷️ Provider: `{provider}`\n📈 Symbol: `{symbol}` {direction}\n🎲 Ticket: `{ticket}`\n📍 Entry: `{entry_price}`\n"
        if lot > 0:
            msg += f"📦 Lot: `{lot:.2f}`\n"
        if sl_price is not None:
            msg += f"🛑 SL: `{sl_price}`\n"
        if tp_prices:
            msg += "🎁 TPs:\n"
            for i, tp in enumerate(tp_prices, 1):
                msg += f"   TP{i}: `{tp}`\n"
        await self._send(account_name, msg)

    async def notify_tp_hit(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
        tp_index: int,
        tp_price: float,
        current_price: float,
    ) -> None:
        msg = f"🎉 **TP HIT**\n━━━━━━━━━━━━━━━━━\n📊 Account: `{account_name}`\n📈 Symbol: `{symbol}`\n🎯 TP{tp_index + 1}: `{tp_price}`\n💰 Precio actual: `{current_price}`\n🏷️ Ticket: `{ticket}`\n"
        await self._send(account_name, msg)

    async def notify_partial_close(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
        close_percent: float,
        close_price: float,
        closed_volume: float,
    ) -> None:
        msg = f"📉 **PARTIAL CLOSE**\n━━━━━━━━━━━━━━━━━\n📊 Account: `{account_name}`\n📈 Symbol: `{symbol}`\n📦 Cerrado: `{closed_volume:.2f}` ({close_percent:.0f}%)\n💹 Precio: `{close_price}`\n🏷️ Ticket: `{ticket}`\n"
        await self._send(account_name, msg)

    async def notify_sl_hit(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
        sl_price: float,
        loss: float,
    ) -> None:
        msg = f"❌ **STOP LOSS HIT**\n━━━━━━━━━━━━━━━━━\n📊 Account: `{account_name}`\n📈 Symbol: `{symbol}`\n🛑 SL: `{sl_price}`\n💔 Loss: `-{loss:.2f}` USD\n🏷️ Ticket: `{ticket}`\n"
        await self._send(account_name, msg)

    async def notify_error(self, account_name: str, error_type: str, error_message: str) -> None:
        msg = f"🚨 **ERROR**\n━━━━━━━━━━━━━━━━━\n📊 Account: `{account_name}`\n⚠️ Tipo: `{error_type}`\n📝 Mensaje: `{error_message}`\n"
        await self._send(account_name, msg)

    async def _send(self, account_name: str, message: str) -> None:
        config = self.config_by_account.get(account_name)
        if not config or not config.chat_id or not config.enabled:
            log.debug("[NOTIFY][SKIP] %s: notificacion deshabilitada o sin chat_id", account_name)
            return
        if self.client is None:
            log.error("[NOTIFY][ERROR] %s: TelegramClient es None.", account_name)
            return
        try:
            await self.client.send_message(config.chat_id, message)
            log.info("[NOTIFY] -> %s (chat_id=%s): enviado", account_name, config.chat_id)
        except Exception as e:
            log.error("[NOTIFY][ERROR] %s: %s", account_name, e)
