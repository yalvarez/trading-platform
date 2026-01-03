import os, asyncio, json, time, logging
from common.config import Settings
from common.redis_streams import redis_client, xadd

from mt5linux import MetaTrader5

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log = logging.getLogger("market_data")

async def connect_mt5(host="mt5_acct1", port=8001, max_attempts=30):
    """Try to connect to MT5 with exponential backoff"""
    mt5 = None
    for attempt in range(max_attempts):
        try:
            log.info(f"Attempting to connect to {host}:{port} (attempt {attempt+1}/{max_attempts})...")
            mt5 = MetaTrader5(host=host, port=port)
            log.info(f"Connected to {host}:{port} successfully")
            return mt5
        except Exception as e:
            log.warning(f"Connection attempt {attempt+1} failed: {e}")
            if attempt < max_attempts - 1:
                wait_time = min(2 ** (attempt // 5), 16)  # cap at 16 seconds
                log.info(f"Waiting {wait_time}s before retry...")
                await asyncio.sleep(wait_time)
            else:
                log.error(f"Failed to connect to {host}:{port} after {max_attempts} attempts")
                raise
    return mt5

async def fetch_tick_data(mt5, symbol):
    """Fetch tick data from MT5 with error handling"""
    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick:
            return {
                "symbol": symbol,
                "bid": str(getattr(tick, "bid", 0.0)),
                "ask": str(getattr(tick, "ask", 0.0)),
                "time": str(getattr(tick, "time", 0)),
            }
        else:
            log.info(f"symbol_info_tick returned None for {symbol}")
            return None
    except Exception as e:
        log.warning(f"Error fetching tick for {symbol}: {e}")
        return None

async def main():
    s = Settings.load()
    r = await redis_client(s.redis_url)

    # Usa acct1 como "market feed" base
    symbols = ["XAUUSD"]
    
    # Retry logic for mt5_acct1 connection (may need significant startup time)
    mt5 = await connect_mt5(host="mt5_acct1", port=8001, max_attempts=30)
    
    # Try to get basic info about MT5 connection
    try:
        account_info = mt5.account_info()
        log.info(f"Connected MT5 account: {account_info}")
    except Exception as e:
        log.warning(f"Could not get account info: {e}")
    
    reconnect_attempts = 0
    max_reconnect_attempts = 5
    
    log.info(f"Starting main loop, will fetch from symbols: {symbols}")
    
    while True:
        try:
            # Attempt to fetch tick data from all symbols
            for sym in symbols:
                tick_data = await fetch_tick_data(mt5, sym)
                if tick_data:
                    await xadd(r, "market_ticks", tick_data)
                    log.info(f"Published tick for {sym}: bid={tick_data['bid']}, ask={tick_data['ask']}")
                    # Reset reconnection counter on successful data fetch
                    reconnect_attempts = 0
                else:
                    log.info(f"No tick data for {sym}")
            
            await asyncio.sleep(0.5)
            
        except Exception as e:
            # Stream may be closed - try to reconnect
            log.error(f"Error fetching tick data: {e}")
            reconnect_attempts += 1
            
            if reconnect_attempts >= max_reconnect_attempts:
                log.error(f"Max reconnection attempts ({max_reconnect_attempts}) reached, attempting full reconnect...")
                try:
                    mt5 = await connect_mt5(host="mt5_acct1", port=8001, max_attempts=10)
                    reconnect_attempts = 0
                except Exception as reconnect_e:
                    log.error(f"Failed to reconnect to MT5: {reconnect_e}")
                    log.info("Waiting 10s before retry...")
                    await asyncio.sleep(10)
            else:
                log.info(f"Stream connection lost, reconnect attempt {reconnect_attempts}/{max_reconnect_attempts}")
                await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())