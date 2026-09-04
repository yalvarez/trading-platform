import os
import json
import asyncio
import logging
import uuid
from typing import Optional

from services.common.config import Settings
from services.common.redis_streams import redis_client, xread_loop, xadd, Streams
from services.common.timewindow import parse_windows, in_windows

from .trade_manager import TradeManager
from .mt5_executor import MT5Executor

container_label = os.getenv("CONTAINER_LABEL") or os.getenv("HOSTNAME") or "trade_orchestrator"
log_fmt = f"%(asctime)s %(levelname)s [{container_label}] %(name)s: %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format=log_fmt)
log = logging.getLogger("trade_orchestrator")


async def handle_signal_fields(fields: dict, tradeManager: TradeManager, accounts: list[dict]) -> None:
    """
    Procesa un mensaje de Streams.SIGNALS (senal fast o completa de TradePulse)
    y lo traduce a open_group/update_group_signal en el TradeManager
    (dual-TP spec seccion 3).
    """
    symbol = fields.get("symbol")
    direction = fields.get("direction")
    is_fast = fields.get("fast", "false").lower() == "true"
    sl_raw = fields.get("sl", "")
    tps = json.loads(fields.get("tps", "[]") or "[]")

    account = next((a for a in accounts if a.get("active")), None)
    if not account:
        log.error("[SIGNAL] No hay cuenta activa configurada. Abortando.")
        return

    existing_group_id = tradeManager.find_active_group_for_symbol(symbol)

    if is_fast:
        if existing_group_id is not None:
            log.info("[SIGNAL][FAST] Ya existe un grupo activo para %s, ignorando fast duplicado.", symbol)
            return
        sl = float(sl_raw) if sl_raw else None
        if sl is None:
            client = tradeManager.mt5._client_for(account)
            price = client.tick_price(symbol, direction)
            from services.common.config import config as _config
            default_sl_pips = float(_config.get("DEFAULT_SL_XAUUSD_PIPS", 100)) if symbol.upper().startswith("XAU") else float(_config.get("DEFAULT_SL_PIPS", 100))
            point = 0.1 if symbol.upper().startswith("XAU") else 0.00001
            from .trade_utils import calcular_sl_default
            sl = calcular_sl_default(symbol, direction, price, point, default_sl_pips)
        await tradeManager.open_group(account, symbol=symbol, direction=direction, sl=sl, tp1=None, tp2=None)
        return

    sl = float(sl_raw) if sl_raw else None
    tp1 = float(tps[0]) if len(tps) > 0 else None
    tp2 = float(tps[1]) if len(tps) > 1 else None

    if existing_group_id is not None:
        await tradeManager.update_group_signal(existing_group_id, sl=sl, tp1=tp1, tp2=tp2)
        return

    if sl is None or tp1 is None or tp2 is None:
        log.error("[SIGNAL] Senal completa incompleta (sl=%s tp1=%s tp2=%s), abortando.", sl, tp1, tp2)
        return
    await tradeManager.open_group(account, symbol=symbol, direction=direction, sl=sl, tp1=tp1, tp2=tp2)


async def main():
    from services.common.env_validator import validate_trade_orchestrator
    validate_trade_orchestrator()

    from services.common.config import config as _config
    s = Settings.load()
    r = await redis_client(s["redis_url"])
    accounts = Settings.accounts()

    from services.common.n8n_notifier import N8nWebhookNotifier
    from .notifications.n8n import N8nNotifierAdapter

    notifier_adapter = None
    webhook_url = _config.get("N8N_WEBHOOK_URL", "")
    if webhook_url:
        n8n_notifier = N8nWebhookNotifier(webhook_url, token=_config.get("N8N_WEBHOOK_TOKEN", ""))
        notifier_adapter = N8nNotifierAdapter(n8n_notifier)
        log.info("N8nNotifierAdapter initialized (webhook_url=%s)", webhook_url)
    else:
        log.warning("N8N_WEBHOOK_URL not configured — trade notifications disabled")

    tradeExecutor = MT5Executor(
        accounts,
        magic=987654,
        notifier=notifier_adapter,
        trading_windows=s["trading_windows"],
        entry_wait_seconds=int(s["entry_wait_seconds"]),
        entry_poll_ms=int(s["entry_poll_ms"]),
        entry_buffer_points=float(s["entry_buffer_points"]),
        config_provider=_config,
    )
    tradeManager = TradeManager(tradeExecutor, notifier=notifier_adapter, config_provider=_config)

    from .mgmt_api import create_mgmt_app
    import uvicorn

    mgmt_app = create_mgmt_app(tradeManager)
    mgmt_port = int(_config.get("MGMT_API_PORT", 8200))
    uvicorn_config = uvicorn.Config(mgmt_app, host="0.0.0.0", port=mgmt_port, log_level="warning")
    uvicorn_server = uvicorn.Server(uvicorn_config)

    async def loop_signals():
        async for msg_id, fields in xread_loop(r, Streams.SIGNALS, last_id="$"):
            if not in_windows(parse_windows(s["trading_windows"])):
                log.info("[SKIP] signal outside windows")
                continue
            try:
                await handle_signal_fields(fields, tradeManager, accounts)
            except Exception:
                log.exception("[SIGNAL] error procesando senal: %s", fields)

    asyncio.create_task(tradeManager.run_forever())
    await asyncio.gather(loop_signals(), uvicorn_server.serve())


if __name__ == "__main__":
    asyncio.run(main())
