"""
Reads the current XAUUSD tick directly from mt5_acct1 over RPyC — the same
source of truth trade_orchestrator uses (services/trade_orchestrator/mt5_pool.py),
not an external price API. Used to build realistic ENTRY PRICE values for
full-signal test messages and to sanity-check the opening price the bot
recorded.
"""
import asyncio
import rpyc


class PriceReader:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    async def read_price(self, symbol: str = "XAUUSD", attempts: int = 3, delay_seconds: float = 0.15) -> float:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                client = rpyc.connect(self.host, self.port)
                tick = client.root.symbol_info_tick(symbol)
                if tick is not None and tick.bid and tick.ask:
                    return (float(tick.bid) + float(tick.ask)) / 2.0
                last_error = RuntimeError(f"empty tick for {symbol}")
            except Exception as e:
                last_error = e
            if attempt < attempts - 1:
                await asyncio.sleep(delay_seconds)
        raise RuntimeError(f"could not read price for {symbol} after {attempts} attempts: {last_error}")
