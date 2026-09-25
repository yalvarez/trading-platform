import os
import json
import asyncio
import logging
import logging.handlers

from services.common.config import Settings
from services.common.redis_streams import redis_client, xread_loop, Streams
from services.common.timewindow import parse_windows, in_windows

from .trade_manager import TradeManager
from .mt5_executor import MT5Executor
from .trade_state_store import TradeStateStore

container_label = os.getenv("CONTAINER_LABEL") or os.getenv("HOSTNAME") or "trade_orchestrator"
log_fmt = f"%(asctime)s %(levelname)s [{container_label}] %(name)s: %(message)s"
formatter = logging.Formatter(log_fmt)

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler()
console_handler.setLevel(os.getenv("LOG_LEVEL", "INFO"))
console_handler.setFormatter(formatter)
root_logger.addHandler(console_handler)

# Archivo de debug persistente en disco (bind-mounted, mismo patron que
# audit_log.jsonl): docker logs se pierde por completo cada vez que el
# contenedor se recrea (rebuild/restart), lo que ya nos costo la evidencia
# cruda de un incidente real en produccion (grupo 129, 2026-09-14) -- para
# cuando se re-reviso el log, el contenedor ya se habia reiniciado con el
# fix desplegado y el log crudo del contenedor anterior ya no existia (solo
# sobrevivio el audit_log.jsonl, que no registra logs a nivel DEBUG ni cada
# tick del loop de gestion mecanica). Este handler queda SIEMPRE en DEBUG,
# independiente de LOG_LEVEL/consola, para no volver a perder ese detalle.
# Rotacion diaria con 15 backups (~15 dias de retencion) para no crecer sin
# limite.
debug_log_dir = os.getenv("DEBUG_LOG_DIR", "data")
os.makedirs(debug_log_dir, exist_ok=True)
debug_file_handler = logging.handlers.TimedRotatingFileHandler(
    os.path.join(debug_log_dir, "orchestrator_debug.log"),
    when="midnight", backupCount=15, encoding="utf-8",
)
debug_file_handler.setLevel(logging.DEBUG)
debug_file_handler.setFormatter(formatter)
root_logger.addHandler(debug_file_handler)

log = logging.getLogger("trade_orchestrator")


def parse_channel_names_json(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("CHANNEL_NAMES_JSON invalido, usando mapeo vacio: %s", e)
        return {}


async def handle_signal_fields(fields: dict, tradeManager: TradeManager, accounts: list[dict]) -> None:
    """
    Procesa un mensaje de Streams.SIGNALS (senal fast o completa de TradePulse)
    y lo traduce a open_group/update_group_signal en el TradeManager
    (dual-TP spec seccion 3).
    """
    symbol = fields.get("symbol")
    direction = fields.get("direction")
    chat_id = fields.get("chat_id")
    is_fast = fields.get("fast", "false").lower() == "true"
    sl_raw = fields.get("sl", "")
    tps = json.loads(fields.get("tps", "[]") or "[]")
    entry_range_raw = fields.get("entry_range", "")
    entry_range = tuple(json.loads(entry_range_raw)) if entry_range_raw and entry_range_raw != "[]" else None

    account = next((a for a in accounts if a.get("active")), None)
    if not account:
        log.error("[SIGNAL] No hay cuenta activa configurada. Abortando.")
        return

    # Señal contraria a un grupo abierto del mismo canal: cerrar primero los que
    # aun no llegaron a TP1, luego abrir (ver close_opposite_groups_before_tp1,
    # caso real 2026-09-25). CLOSE_ON_OPPOSITE_SIGNAL=off lo desactiva.
    from services.common.config import config as _config
    if str(_config.get("CLOSE_ON_OPPOSITE_SIGNAL", "before_tp1")).strip().lower() == "before_tp1":
        await tradeManager.close_opposite_groups_before_tp1(chat_id=chat_id, symbol=symbol, direction=direction)

    if is_fast:
        # Solo grupos del mismo canal Y la misma direccion: el grupo reciente de
        # OTRO canal no hace que esta señal sea un duplicado, y una fast en la
        # direccion contraria tampoco (antes cualquier fast dentro del cooldown
        # se ignoraba, asi que un giro rapido del canal no cerraba ni abria nada).
        existing_group_id = tradeManager.find_active_group_for_symbol(symbol, chat_id=chat_id, direction=direction)
        if existing_group_id is not None:
            # find_active_group_for_symbol no tiene nocion de tiempo: sin este
            # cooldown, CUALQUIER señal fast nueva del mismo simbolo se ignoraria
            # para siempre mientras el grupo anterior siga abierto, sin importar
            # si pasaron 17 segundos (duplicado real) o 17 minutos (reapertura
            # legitima del proveedor, BUY o SELL). REOPEN_COOLDOWN_SECONDS
            # distingue ambos casos: solo se ignora si el grupo activo es MAS
            # NUEVO que el cooldown.
            from services.common.config import config as _config
            reopen_cooldown = float(_config.get("REOPEN_COOLDOWN_SECONDS", 300))
            age = tradeManager.group_age_seconds(existing_group_id)
            if age is None or age < reopen_cooldown:
                log.info("[SIGNAL][FAST] Grupo activo reciente para %s (edad=%s s < cooldown=%s s), ignorando fast duplicado.",
                         symbol, f"{age:.1f}" if age is not None else "?", reopen_cooldown)
                return
            log.info("[SIGNAL][FAST] Grupo activo para %s tiene %.1fs (>= cooldown=%ss) — tratando como reapertura, abriendo grupo nuevo.",
                      symbol, age, reopen_cooldown)
        sl = float(sl_raw) if sl_raw else None
        # Senal fast: si la señal completa (con TP1/TP2 reales) nunca llega, el
        # grupo quedaria dependiendo solo del SL, sin ningun objetivo de salida,
        # Y el runner nunca haria trailing (_apply_trailing exige tp1_price y
        # tp2_price no-None). tp1_leg recibe un TP temporal de proteccion aqui;
        # runner_leg NO recibe TP real en MT5 (open_group solo pone tp en la
        # pierna tp1, ver mas abajo) — eso no cambia, sigue siendo la pierna
        # disenada para correr. Pero SI necesita tp1_price/tp2_price poblados
        # en memoria para que el trailing se active. tp2_temp es sintetico
        # (1 punto mas alla de tp1_temp, misma direccion) unicamente para
        # definir un "unit" > 0: en la formula SL = entry_price + (peak*unit)/3
        # con peak = avance/unit, el unit se cancela algebraicamente — cualquier
        # unit > 0 produce el mismo SL para el mismo avance de precio. No es
        # un techo, es solo la unidad de medida del trailing.
        # Si la señal completa llega despues, update_group_signal sobreescribe
        # ambos con los valores reales (ver trade_manager.py), y el trailing
        # sigue desde donde iba (peak_multiple se reescala, no se resetea).
        client = tradeManager.mt5._client_for(account)
        price = client.tick_price(symbol, direction)
        from services.common.config import config as _config
        point = 0.1 if symbol.upper().startswith("XAU") else 0.00001
        from .trade_utils import calcular_sl_default, calcular_tp_default
        if sl is None:
            default_sl_pips = float(_config.get("DEFAULT_SL_XAUUSD_PIPS", 100)) if symbol.upper().startswith("XAU") else float(_config.get("DEFAULT_SL_PIPS", 100))
            sl = calcular_sl_default(symbol, direction, price, point, default_sl_pips)
        default_tp_pips = float(_config.get("DEFAULT_TP_XAUUSD_PIPS", 100)) if symbol.upper().startswith("XAU") else float(_config.get("DEFAULT_TP_PIPS", 100))
        default_tp1 = calcular_tp_default(symbol, direction, price, point, default_tp_pips) if default_tp_pips > 0 else None
        default_tp2 = None
        if default_tp1 is not None:
            # tp2 = tp1 + N pips (not "+1 point"): a 1-point unit made
            # _apply_trailing's peak_multiple hypersensitive to tiny price
            # ticks, sending an SL order_send on nearly every tick (~15 in
            # 2 minutes observed live, group_id=23). DEFAULT_TP2_EXTRA_PIPS
            # gives tp1/tp2 a real, configurable distance — the same order
            # of magnitude a genuine signal's TP1/TP2 pair would have.
            tp2_extra_pips = float(_config.get("DEFAULT_TP2_EXTRA_PIPS", 40))
            default_tp2 = calcular_tp_default(symbol, direction, default_tp1, point, tp2_extra_pips)
        await tradeManager.open_group(account, symbol=symbol, direction=direction, sl=sl, tp1=default_tp1, tp2=default_tp2, chat_id=chat_id)
        return

    sl = float(sl_raw) if sl_raw else None
    tp1 = float(tps[0]) if len(tps) > 0 else None
    tp2 = float(tps[1]) if len(tps) > 1 else None

    # Mismo canal Y misma direccion: una señal completa solo completa/actualiza
    # un grupo propio en su direccion -- nunca el grupo de otro canal, ni
    # escribe niveles SELL en un grupo BUY (o viceversa).
    existing_group_id = tradeManager.find_active_group_for_symbol(symbol, chat_id=chat_id, direction=direction)
    if existing_group_id is not None:
        await tradeManager.update_group_signal(existing_group_id, sl=sl, tp1=tp1, tp2=tp2)
        return

    if sl is None or tp1 is None or tp2 is None:
        log.error("[SIGNAL] Senal completa incompleta (sl=%s tp1=%s tp2=%s), abortando.", sl, tp1, tp2)
        return
    await tradeManager.open_group(account, symbol=symbol, direction=direction, sl=sl, tp1=tp1, tp2=tp2, entry_range=entry_range, chat_id=chat_id)


async def main():
    from services.common.env_validator import validate_trade_orchestrator
    validate_trade_orchestrator()

    from services.common.config import config as _config
    s = Settings.load()
    r = await redis_client(s["redis_url"])
    accounts = Settings.accounts()

    from services.trade_orchestrator.event_bus import EventBus
    from services.trade_orchestrator.n8n_event_client import N8nEventClient
    from services.trade_orchestrator.n8n_retry_worker import run_retry_worker

    audit_log_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "audit_log.jsonl")
    event_webhook_url = _config.get("N8N_EVENT_WEBHOOK_URL", "")
    event_bus = None
    n8n_event_client = None
    if event_webhook_url:
        n8n_event_client = N8nEventClient(event_webhook_url, token=_config.get("N8N_EVENT_WEBHOOK_TOKEN", ""))
        event_bus = EventBus(audit_log_path, redis_client=r)
        log.info("EventBus initialized (event_webhook_url=%s)", event_webhook_url)
    else:
        event_bus = EventBus(audit_log_path, redis_client=None)
        log.warning("N8N_EVENT_WEBHOOK_URL not configured — audit log still writes locally, n8n delivery disabled")

    channel_names = parse_channel_names_json(_config.get("CHANNEL_NAMES_JSON", ""))

    tradeExecutor = MT5Executor(
        accounts,
        magic=987654,
        notifier=None,
        trading_windows=s["trading_windows"],
        entry_wait_seconds=int(s["entry_wait_seconds"]),
        entry_poll_ms=int(s["entry_poll_ms"]),
        entry_buffer_points=float(s["entry_buffer_points"]),
        config_provider=_config,
    )
    state_store = TradeStateStore(r, os.path.join(os.path.dirname(__file__), "..", "..", "data", "trade_state.jsonl"))
    tradeManager = TradeManager(
        tradeExecutor, event_bus=event_bus, config_provider=_config, state_store=state_store,
        channel_names=channel_names,
    )

    reconciliation_summary = await tradeManager.reconcile_from_mt5(accounts)
    log.info("[RECONCILE] al arranque: %s", reconciliation_summary)

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
    tasks = [loop_signals(), uvicorn_server.serve()]
    if n8n_event_client is not None:
        tasks.append(run_retry_worker(r, n8n_event_client, audit_log_path))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
