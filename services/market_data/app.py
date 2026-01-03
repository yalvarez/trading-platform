import os, asyncio, json, time, logging
from common.config import Settings
from common.redis_streams import redis_client, xadd

from mt5linux import MetaTrader5

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log = logging.getLogger("market_data")

async def main():
    s = Settings.load()
    r = await redis_client(s.redis_url)

    # Usa acct1 como “market feed” base
    mt5 = MetaTrader5(host="mt5_acct1", port=8001)

    symbols = ["XAUUSD"]
    while True:
        for sym in symbols:
            tick = mt5.symbol_info_tick(sym)
            if tick:
                await xadd(r, "market_ticks", {
                    "symbol": sym,
                    "bid": str(getattr(tick, "bid", 0.0)),
                    "ask": str(getattr(tick, "ask", 0.0)),
                    "time": str(getattr(tick, "time", 0)),
                })
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    asyncio.run(main())