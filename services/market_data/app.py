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
            # initialize the connection (required by mt5linux wrapper)
            try:
                init_res = mt5.initialize()
                log.debug(f"mt5.initialize() returned: {init_res}")
            except Exception as ie:
                log.debug(f"mt5.initialize() exception: {ie}")

            # Give the slave a moment and check for account/symbol availability
            await asyncio.sleep(0.2)
            try:
                account = mt5.account_info()
            except Exception:
                account = None

            symbols = None
            try:
                symbols = mt5.symbols_total()
            except Exception:
                try:
                    syml = mt5.symbols_get()
                    symbols = len(syml) if syml else 0
                except Exception:
                    symbols = 0

            if account is None and (not symbols):
                log.warning(f"Connected to {host}:{port} but no account/symbols available yet (account={account}, symbols={symbols}). Retrying...")
                raise RuntimeError('No account/symbols yet')

            log.info(f"Connected to {host}:{port} successfully (account present: {account is not None}, symbols: {symbols})")
            # Dump available symbol names for debugging
            try:
                syml = mt5.symbols_get()
                if syml:
                    names = [s.name for s in syml[:500]]
                    try:
                        with open('/tmp/mt5_symbols.txt','w') as f:
                            f.write('\n'.join(names))
                    except Exception as wf:
                        log.debug(f"Could not write symbols dump: {wf}")
                    log.info(f"Wrote {len(names)} symbol names to /tmp/mt5_symbols.txt (first 20: {names[:20]})")
            except Exception:
                pass

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

async def fetch_tick_data(mt5, symbol, max_retries=3):
    """Fetch tick data from MT5 with retry logic and subscription"""
    for attempt in range(max_retries):
        try:
            # Ensure symbol is subscribed - critical for some brokers like Vantage
            try:
                select_result = mt5.symbol_select(symbol, True)
                if not select_result:
                    log.debug(f"symbol_select returned False for {symbol} (attempt {attempt+1})")
            except Exception as e:
                log.debug(f"Error selecting symbol {symbol}: {e}")
            
            # Small delay to allow subscription to register
            await asyncio.sleep(0.1)
            
            tick = mt5.symbol_info_tick(symbol)
            if tick:
                return {
                    "symbol": symbol,
                    "bid": str(getattr(tick, "bid", 0.0)),
                    "ask": str(getattr(tick, "ask", 0.0)),
                    "time": str(getattr(tick, "time", 0)),
                }
            else:
                if attempt < max_retries - 1:
                    # Retry with small delay
                    await asyncio.sleep(0.2)
                else:
                    log.debug(f"symbol_info_tick returned None for {symbol} after {max_retries} attempts")
                    return None
        except Exception as e:
            log.debug(f"Error fetching tick for {symbol} (attempt {attempt+1}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(0.2)
    
    return None

async def get_available_symbols(mt5, fallback_symbols=None):
    """Get list of available symbols from MT5"""
    try:
        # Try to get all symbols
        symbols = mt5.symbols_get()
        if symbols:
            symbol_names = [s.name for s in symbols]
            log.info(f"Found {len(symbol_names)} symbols in MT5")
            return symbol_names
        else:
            log.warning("Could not get symbols list from MT5, using fallback")
            return fallback_symbols or []
    except Exception as e:
        log.warning(f"Error getting symbols: {e}, using fallback")
        return fallback_symbols or []


async def resolve_symbol_aliases(mt5, requested_symbols: list[str]) -> list[str]:
    """Resolve requested symbols to actual broker-exposed symbol names.
    Tries exact match, then looks for best contains-match, and prefers symbols that
    return a recent tick.
    """
    resolved = []
    try:
        all_syms = [s.name for s in mt5.symbols_get()]
    except Exception:
        all_syms = []

    for base in requested_symbols:
        if base in all_syms:
            resolved.append(base)
            continue

        # find candidates that contain the base string
        candidates = [s for s in all_syms if base in s]
        chosen = None
        for c in candidates:
            try:
                tick = mt5.symbol_info_tick(c)
                if tick and getattr(tick, 'time', 0):
                    chosen = c
                    break
            except Exception:
                continue

        if not chosen and candidates:
            chosen = candidates[0]

        if chosen:
            log.info(f"Resolved {base} -> {chosen}")
            resolved.append(chosen)
        else:
            log.warning(f"Could not resolve symbol for {base}, keeping as-is")
            resolved.append(base)

    return resolved

async def main():
    s = Settings.load()
    r = await redis_client(s.redis_url)

    # Symbols to monitor - Vantage symbols
    symbols = ["XAUUSD", "BTCUSD", "EURUSD", "GBPUSD", "USDJPY"]
    # preserve original requested list for resolution
    requested_symbols = symbols
    
    # Retry logic for mt5_acct1 connection (may need significant startup time)
    mt5 = await connect_mt5(host="mt5_acct1", port=8001, max_attempts=30)
    
    # Try to get basic info about MT5 connection
    try:
        account_info = mt5.account_info()
        log.info(f"Connected MT5 account: {account_info}")
    except Exception as e:
        log.warning(f"Could not get account info: {e}")
    
    # Pre-resolve and pre-subscribe to all symbols to ensure data availability
    log.info(f"Resolving requested symbols: {requested_symbols}")
    symbols = await resolve_symbol_aliases(mt5, requested_symbols)
    log.info(f"Resolved symbols: {symbols}")

    log.info(f"Pre-subscribing to symbols: {symbols}")
    for sym in symbols:
        try:
            select_result = mt5.symbol_select(sym, True)
            log.info(f"Symbol {sym} subscription: {select_result}")
            await asyncio.sleep(0.05)
        except Exception as e:
            log.warning(f"Failed to pre-subscribe to {sym}: {e}")
    
    # Wait for subscriptions to register
    await asyncio.sleep(0.5)
    
    reconnect_attempts = 0
    max_reconnect_attempts = 5
    failed_symbols = {}  # Track failed symbols to reduce log spam
    
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
                    # Clear failed count for this symbol
                    if sym in failed_symbols:
                        del failed_symbols[sym]
                else:
                    # Only log if this is a new failure or after many attempts
                    failed_symbols[sym] = failed_symbols.get(sym, 0) + 1
                    if failed_symbols[sym] == 1 or failed_symbols[sym] % 10 == 0:
                        log.warning(f"No tick data for {sym} (failure count: {failed_symbols[sym]})")
            
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
                    failed_symbols.clear()
                except Exception as reconnect_e:
                    log.error(f"Failed to reconnect to MT5: {reconnect_e}")
                    log.info("Waiting 10s before retry...")
                    await asyncio.sleep(10)
            else:
                log.info(f"Stream connection lost, reconnect attempt {reconnect_attempts}/{max_reconnect_attempts}")
                await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())