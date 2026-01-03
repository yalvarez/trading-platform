import os, json, asyncio, logging
from common.config import Settings
from common.redis_streams import redis_client, xread_loop, xadd, Streams
from common.timewindow import parse_windows, in_windows

from mt5_executor import MT5Executor
from trade_manager import TradeManager

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log = logging.getLogger("trade_orchestrator")

async def notifier(account_name: str, message: str):
    # placeholder: aquí conectas Telegram notifier si quieres
    log.info(f"[NOTIFY] {account_name}: {message}")

async def main():
    s = Settings.load()
    r = await redis_client(s.redis_url)
    accounts = s.accounts()

    execu = MT5Executor(
        accounts,
        magic=987654,
        notifier=notifier,
        trading_windows=s.trading_windows,
        entry_wait_seconds=s.entry_wait_seconds,
        entry_poll_ms=s.entry_poll_ms,
        entry_buffer_points=s.entry_buffer_points,
    )

    tm = TradeManager(execu, notifier=None)  # si quieres, pásale notifier real

    async def handle_signal(fields: dict):
        if not in_windows(parse_windows(s.trading_windows)):
            log.info("[SKIP] signal outside windows (no connect).")
            await xadd(r, Streams.EVENTS, {"type":"skip", "reason":"outside_windows"})
            return

        symbol = fields.get("symbol")
        direction = fields.get("direction")
        provider_tag = fields.get("provider_tag","GEN")
        entry_range = fields.get("entry_range","")
        sl = fields.get("sl","")
        tps = json.loads(fields.get("tps","[]") or "[]")

        entry_tuple = json.loads(entry_range) if entry_range else None
        # ✅ wait 60s for price to enter range (if provided)
        # For scaffold simplicity, we skip the async price wait here; you can hook it in next iteration.

        res = execu.open_complete_trade(
            provider_tag=provider_tag,
            symbol=symbol,
            direction=direction,
            entry_range=entry_tuple,
            sl=float(sl) if sl else 0.0,
            tps=tps,
        )

        # register opened
        for acct_name, ticket in res.tickets_by_account.items():
            tm.register_trade(
                account_name=acct_name,
                ticket=ticket,
                symbol=symbol,
                direction=direction,
                provider_tag=provider_tag,
                tps=tps,
                planned_sl=float(sl) if sl else None,
            )

        if res.errors_by_account:
            await xadd(r, Streams.EVENTS, {"type":"open_errors", "errors": json.dumps(res.errors_by_account)})

    async def handle_mgmt(fields: dict):
        text = fields.get("text","")
        hint = fields.get("provider_hint","")
        if hint == "TOROFX":
            tm.handle_torofx_management_message(int(fields.get("chat_id","0")), text)
        elif hint == "GOLD_BROTHERS":
            # aquí puedes enrutar a handle_bg_* si quieres
            pass

    async def loop_signals():
        async for _, fields in xread_loop(r, Streams.SIGNALS, last_id="$"):
            await handle_signal(fields)

    async def loop_mgmt():
        async for _, fields in xread_loop(r, Streams.MGMT, last_id="$"):
            await handle_mgmt(fields)

    await asyncio.gather(loop_signals(), loop_mgmt())

if __name__ == "__main__":
    asyncio.run(main())
