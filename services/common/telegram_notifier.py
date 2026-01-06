"""
Telegram-based notifier for trade events and system status.
Sends notifications to configured chat IDs for each account.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from telethon import TelegramClient
class RemoteTelegramNotifier:
    """
    Sends notifications to a remote HTTP API endpoint (e.g., FastAPI Telegram notification service).
    """
    def __init__(self, api_url: str):
        self.api_url = api_url.rstrip("/")

    async def notify(self, chat_id: str, message: str):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.api_url}/notify", json={"chat_id": chat_id, "message": message})
            resp.raise_for_status()
            return resp.json()

log = logging.getLogger("telegram_notifier")


@dataclass
class NotificationConfig:
    """Configuration for where to send notifications"""
    account_name: str
    chat_id: Optional[int]
    enabled: bool = True


class TelegramNotifier:
    """
    Sends notifications via Telegram for trade execution and status updates.
    """
    
    def __init__(self, telegram_client: TelegramClient, notify_configs: list[NotificationConfig]):
        """
        Args:
            telegram_client: Telethon TelegramClient instance
            notify_configs: List of notification configs per account
        """
        self.client = telegram_client
        self.config_by_account: dict[str, NotificationConfig] = {
            cfg.account_name: cfg for cfg in notify_configs
        }
    
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
    ):
        """Notify when a trade is opened"""
        message = f"""
🎯 **TRADE OPENED**
━━━━━━━━━━━━━━━━━
📊 Account: `{account_name}`
🏷️ Provider: `{provider}`
📈 Symbol: `{symbol}` {direction}
🎲 Ticket: `{ticket}`
📍 Entry: `{entry_price}`
"""
        
        if lot > 0:
            message += f"📦 Lot: `{lot:.2f}`\n"
        
        if sl_price is not None:
            message += f"🛑 SL: `{sl_price}`\n"
        
        if tp_prices:
            message += "🎁 TPs:\n"
            for i, tp in enumerate(tp_prices, 1):
                message += f"   TP{i}: `{tp}`\n"
        
        await self._send(account_name, message)
    
    async def notify_tp_hit(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
        tp_index: int,
        tp_price: float,
        current_price: float,
    ):
        """Notify when a take profit is hit"""
        message = f"""
🎉 **TP HIT**
━━━━━━━━━━━━━━━━━
📊 Account: `{account_name}`
📈 Symbol: `{symbol}`
🎯 TP{tp_index+1}: `{tp_price}`
💰 Current: `{current_price}`
🏷️ Ticket: `{ticket}`
"""
        await self._send(account_name, message)
    
    async def notify_partial_close(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
        close_percent: float,
        close_price: float,
        closed_volume: float,
    ):
        """Notify when a partial position is closed"""
        message = f"""
📉 **PARTIAL CLOSE**
━━━━━━━━━━━━━━━━━
📊 Account: `{account_name}`
📈 Symbol: `{symbol}`
📦 Closed: `{closed_volume:.2f}` ({close_percent:.0f}%)
💹 At: `{close_price}`
🏷️ Ticket: `{ticket}`
"""
        await self._send(account_name, message)
    
    async def notify_sl_hit(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
        sl_price: float,
        loss: float,
    ):
        """Notify when stop loss is hit"""
        message = f"""
❌ **STOP LOSS HIT**
━━━━━━━━━━━━━━━━━
📊 Account: `{account_name}`
📈 Symbol: `{symbol}`
🛑 SL: `{sl_price}`
💔 Loss: `-{loss:.2f}` USD
🏷️ Ticket: `{ticket}`
"""
        await self._send(account_name, message)
    
    async def notify_trailing_activated(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
    ):
        """Notify when trailing stop is activated"""
        message = f"""
🚀 **TRAILING ACTIVATED**
━━━━━━━━━━━━━━━━━
📊 Account: `{account_name}`
📈 Symbol: `{symbol}`
🎯 Now protecting profits with trailing stop
🏷️ Ticket: `{ticket}`
"""
        await self._send(account_name, message)
    
    async def notify_connection_status(
        self,
        account_name: str,
        status: str,
        balance: Optional[float] = None,
        equity: Optional[float] = None,
        free_margin: Optional[float] = None,
    ):
        """Notify connection status to MT5"""
        if status == "connected":
            message = f"""
✅ **MT5 CONNECTED**
━━━━━━━━━━━━━━━━━
📊 Account: `{account_name}`
"""
            if balance is not None:
                message += f"💰 Balance: `{balance:.2f}` USD\n"
            if equity is not None:
                message += f"📊 Equity: `{equity:.2f}` USD\n"
            if free_margin is not None:
                message += f"🆓 Free Margin: `{free_margin:.2f}` USD\n"
        else:
            message = f"""
❌ **MT5 DISCONNECTED**
━━━━━━━━━━━━━━━━━
📊 Account: `{account_name}`
⚠️ Connection lost - trading disabled
"""
        
        await self._send(account_name, message)
    
    async def notify_error(
        self,
        account_name: str,
        error_type: str,
        error_message: str,
    ):
        """Notify about errors"""
        message = f"""
🚨 **ERROR**
━━━━━━━━━━━━━━━━━
📊 Account: `{account_name}`
⚠️ Type: `{error_type}`
📝 Message: `{error_message}`
"""
        await self._send(account_name, message)
    
    async def notify_addon_entry(
        self,
        account_name: str,
        ticket: int,
        symbol: str,
        addon_price: float,
        addon_lot: float,
    ):
        """Notify when addon entry is executed"""
        message = f"""
➕ **ADDON ENTRY**
━━━━━━━━━━━━━━━━━
📊 Account: `{account_name}`
📈 Symbol: `{symbol}`
📍 Entry: `{addon_price}`
📦 Lot: `{addon_lot:.2f}`
🏷️ Main Ticket: `{ticket}`
"""
        await self._send(account_name, message)

    async def notify(self, account_name: str, message: str):
        """Generic notify entrypoint for compatibility with older notifier API."""
        await self._send(account_name, message)
    
    async def _send(self, account_name: str, message: str):
        """Internal method to send message"""
        config = self.config_by_account.get(account_name)
        if not config or not config.chat_id or not config.enabled:
            log.debug(f"[NOTIFY][SKIP] {account_name}: notification disabled or no chat_id")
            return

        if self.client is None:
            log.error(f"[NOTIFY][CRITICAL] {account_name}: Telegram client is None. Cannot send message. Check initialization.")
            return

        try:
            await self.client.send_message(config.chat_id, message)
            log.info(f"[NOTIFY] → {account_name} (chat_id={config.chat_id}): notification sent")
        except Exception as e:
            log.error(f"[NOTIFY][ERROR] {account_name}: {e}")
