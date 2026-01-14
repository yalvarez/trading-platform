import httpx

class RemoteTelegramNotifier:

    async def notify_trade_opened(self, account_name: str, ticket: int, symbol: str, direction: str, entry_price: float, sl_price: float, tp_prices: list, lot: float, provider: str):
        # Simple implementation: send a formatted message to a default chat (could be improved)
        msg = f"[TRADE OPENED] {account_name} | {symbol} {direction} | Ticket: {ticket}\nEntry: {entry_price} SL: {sl_price} TP: {tp_prices} Lot: {lot} Provider: {provider}"
        # You may want to route to a specific chat_id per account/provider
        chat_id = None
        try:
            chat_id = int(os.getenv('TG_NOTIFY_TARGET', ''))
        except Exception:
            pass
        if not chat_id:
            # fallback: do nothing
            return
        await self.notify(chat_id, msg)

    def __init__(self, api_url: str):
        self.api_url = api_url.rstrip("/")

    async def notify(self, chat_id: str, message: str):
        import logging
        log = logging.getLogger("trade_orchestrator.telegram_notifier")
        log.info(f"[API][PAYLOAD] chat_id={chat_id} message={message}")
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.api_url}/notify", json={"chat_id": chat_id, "message": message})
            resp.raise_for_status()
            return resp.json()
