import os, json, asyncio, logging, sys, uuid
import redis.asyncio as aioredis
from services.common.config_db import ConfigProvider
from services.common.redis_streams import redis_client, xread_loop, xadd, Streams
from services.common.timewindow import parse_windows, in_windows

from .mt5_executor import MT5Executor
from .trade_manager import TradeManager
# Ensure services folder is on sys.path so sibling packages (telegram_ingestor) can be imported
_svc_a = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_svc_b = os.path.abspath(os.path.join(os.path.dirname(__file__), 'services'))
if os.path.isdir(_svc_b):
    sys.path.insert(0, _svc_b)
elif os.path.isdir(_svc_a):
    sys.path.insert(0, _svc_a)
else:
    # fallback: project root's services directory
    _svc_c = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'services'))
    if os.path.isdir(_svc_c):
        sys.path.insert(0, _svc_c)
import importlib.util

from services.common.telegram_notifier import RemoteTelegramNotifier, NotificationConfig
from .notifications.telegram import TelegramNotifierAdapter
from prometheus_client import start_http_server


# Add container label to log format for Grafana filtering
container_label = os.getenv("CONTAINER_LABEL") or os.getenv("HOSTNAME") or "trade_orchestrator"
log_fmt = f"%(asctime)s %(levelname)s [{container_label}] %(name)s: %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"), format=log_fmt)
log = logging.getLogger("trade_orchestrator")

class NotifierAdapter:
    """
    Adaptador que expone métodos async para notificaciones de TP, cierres parciales y mensajes generales.
    Permite usar un notificador Telegram remoto de forma uniforme en toda la app.
    """

    def __init__(self, tg_notifier):
        self._tg = tg_notifier

    async def notify_tp_hit(self, account_name: str, ticket: int, symbol: str, tp_index: int, tp_price: float, current_price: float):
        """
        Notifica que se alcanzó un TP para una cuenta/ticket/símbolo.
        """
        return await self._tg.notify_tp_hit(
            account_name=account_name,
            ticket=ticket,
            symbol=symbol,
            tp_index=tp_index,
            tp_price=tp_price,
            current_price=current_price,
        )

    async def notify_partial_close(self, *args, **kwargs):
        """
        Notifica un cierre parcial si el notificador lo soporta.
        """
        if hasattr(self._tg, "notify_partial_close"):
            return await self._tg.notify_partial_close(*args, **kwargs)

    async def __call__(self, account_name: str, message: str):
        """
        Notificación genérica (llamada como función).
        """
        await self._tg.notify(account_name, message)

    async def notify(self, account_name: str, message: str):
        """
        Notificación genérica (método explícito).
        """
        await self._tg.notify(account_name, message)

async def main():
    """
    Función principal de arranque del servicio trade_orchestrator.
    - Inicializa settings, métricas, Redis, cuentas y notificador.
    - Lanza los loops de señales y gestión de trades.
    """
    from services.common.config import Settings
    config = ConfigProvider()
    s = Settings.load()
    # start Prometheus metrics server
    try:
        metrics_port = int(os.getenv("METRICS_PORT", "8000"))
        start_http_server(metrics_port)
        log.info(f"Prometheus metrics server started on :{metrics_port}")
    except Exception as e:
        log.error(f"Failed to start Prometheus metrics server: {e}")
    r = await redis_client(s["redis_url"])
    accounts = config.get_accounts()

    # Inicialización centralizada del notificador Telegram
    # Forzar habilitación de notificaciones
    notifier_adapter = None
    try:
        tg_notifier = RemoteTelegramNotifier(config.get("TELEGRAM_INGESTOR_URL", "http://telegram_ingestor:8000"))
        notifier_adapter = TelegramNotifierAdapter(tg_notifier)
        log.info("TelegramNotifierAdapter initialized (forced enable)")
    except Exception as e:
        log.error(f"Failed to initialize TelegramNotifierAdapter: {e}")

    # # Enviar mensaje de prueba al iniciar
    # try:
    #     cuentas = config.get_accounts()
    #     if cuentas and notifier_adapter:
    #         cuenta_prueba = cuentas[0].get("chat_id")
    #         if cuenta_prueba:
    #             log.info(f"Enviando mensaje de prueba de notificación a la cuenta: {cuenta_prueba}")
    #             await notifier_adapter.notify(cuenta_prueba, "✅ Notificación de prueba enviada desde orchestrator.")
    # except Exception as e:
    #     log.error(f"Error enviando notificación de prueba: {e}")

    tradeExecutor = MT5Executor(
        accounts,
        magic=987654,
        notifier=(notifier_adapter if notifier_adapter is not None else None),
        trading_windows=s["trading_windows"],
        entry_wait_seconds=int(s["entry_wait_seconds"]),
        entry_poll_ms=int(s["entry_poll_ms"]),
        entry_buffer_points=float(s["entry_buffer_points"]),
    )

    tradeManager = TradeManager(tradeExecutor, notifier=(notifier_adapter if notifier_adapter is not None else None), config_provider=config)  # attach notifier and config_provider

    async def handle_signal(fields: dict):
        """
        Procesa una señal de trading recibida, calcula SL/TP, filtra cuentas y ejecuta la apertura o actualización de trades.
        """
        trace_id = uuid.uuid4().hex[:8]
        orig_trace = fields.get("trace", "NO_TRACE")
        
        if not in_windows(parse_windows(s["trading_windows"])):
            log.info("[SKIP] signal outside windows (no connect). trace=%s", trace_id)
            await xadd(r, Streams.EVENTS, {"type": "skip", "reason": "outside_windows", "trace": trace_id})
            return

        symbol = fields.get("symbol")
        direction = fields.get("direction")
        provider_tag = fields.get("provider_tag", "GEN")
        entry_range = fields.get("entry_range", "")
        sl = fields.get("sl", "")
        tps = json.loads(fields.get("tps", "[]") or "[]")
        is_fast = fields.get("fast", "false").lower() == "true"
        # Nuevo: obtener el canal de origen de la señal
        try:
            source_channel = int(fields.get("source_chat_id") or fields.get("chat_id") or 0)
        except Exception:
            source_channel = 0
        # Si es FAST y no trae SL, calcularlo aquí usando la lógica de pips correcta (oro y otros)
        if (not sl or float(sl) == 0.0) and is_fast:
            account = next((a for a in accounts if a.get("active")), None)
            if account:
                client = tradeExecutor._client_for(account)
                price = client.tick_price(symbol, direction)
                # Obtener default_sl_pips desde config
                default_sl_pips = float(config.get("DEFAULT_SL_XAUUSD_PIPS", 300)) if symbol.upper().startswith("XAU") else float(config.get("DEFAULT_SL_PIPS", 100))
                point = 0.1 if symbol.upper().startswith("XAU") else 0.00001
                from .trade_utils import calcular_sl_default
                forced_sl = calcular_sl_default(symbol, direction, price, point, default_sl_pips)
                sl = str(forced_sl)
                log.info(f"[TRACE][SIGNAL][FAST] SL forzado en handle_signal (calcular_sl_default): {sl} (price={price}, default_sl_pips={default_sl_pips}, point={point})")
            else:
                log.error(f"[TRACE][SIGNAL][FAST] No se pudo calcular SL forzado: no hay cuenta activa. Abortando señal.")
                return
        log.info(f"[TRACE][SIGNAL] SL recibido en handle_signal: {sl} (type={type(sl)}) fields={fields}")
        if not sl or float(sl) == 0.0:
            log.error(f"[TRACE][SIGNAL] SL inválido detectado en handle_signal. SL={sl} is_fast={is_fast} fields={fields}. Abortando señal.")
            return

        entry_tuple = json.loads(entry_range) if entry_range else None

        # --- FAST update logic ---
        log.info(f"[TRACE][SIGNAL] SL propagado a lógica FAST/COMPLETE: {sl}")
        if not is_fast:
            # For each account, check for an existing trade with provider_tag 'GB_FAST' for this symbol/direction
            updated_any = False
            for acct_name, trade in list(tradeManager.trades.items()):
                t = trade
                if (
                    t.symbol == symbol
                    and t.direction == direction
                    and t.provider_tag == "GB_FAST"
                ):
                    # Update the trade with new SL, TPs, and provider_tag
                    log.info(f"[TRACE][FAST-UPDATE] SL recibido para update_trade_signal: {sl}")
                    tradeManager.update_trade_signal(
                        ticket=t.ticket,
                        tps=tps,
                        planned_sl=float(sl) if sl else None,
                        provider_tag=provider_tag,
                    )
                    # --- NEW: Update SL in MT5 as well ---
                    try:
                        account = next((a for a in accounts if a.get("name") == t.account_name), None)
                        if account and t.ticket and sl:
                            result = await tradeExecutor.modify_sl(account, t.ticket, float(sl), reason="full-signal")
                            if result:
                                log.info(f"[FAST-UPDATE] SL updated in MT5 for ticket={t.ticket} acct={t.account_name} to SL={sl}")
                            else:
                                log.warning(f"[FAST-UPDATE] SL update in MT5 failed for ticket={t.ticket} acct={t.account_name} to SL={sl}")
                    except Exception as e:
                        log.error(f"[FAST-UPDATE] Failed to update SL in MT5 for ticket={t.ticket}: {e}")
                    log.info(f"[FAST-UPDATE] Updated FAST trade ticket={t.ticket} acct={t.account_name} with new SL/TP/provider_tag from full signal.")
                    updated_any = True
            # If any trade was updated, skip opening a new trade
            if updated_any:
                return

            # --- TP1/fast close logic for complete signals ---
            # Only for complete signals (not fast), and only if TP1 is present
            if tps and len(tps) > 0:
                # Get current price from any active account (first one)
                account = next((a for a in accounts if a.get("active")), None)
                if account:
                    client = tradeExecutor._client_for(account)
                    # Use tick_price to get current price in the right direction
                    # For BUY, price must be >= TP1; for SELL, price <= TP1
                    tp1 = float(tps[0])
                    current_price = client.tick_price(symbol, direction)
                    price_past_tp1 = False
                    if direction.upper() == "BUY" and current_price >= tp1:
                        price_past_tp1 = True
                    elif direction.upper() == "SELL" and current_price <= tp1:
                        price_past_tp1 = True
                    if price_past_tp1:
                        log.warning(f"[COMPLETE-SIGNAL] Price is past TP1 (current={current_price}, TP1={tp1}) for {symbol} {direction}. Not registering signal.")
                        # Close any open fast trade for this symbol/direction
                        for acct_name, trade in list(tradeManager.trades.items()):
                            t = trade
                            if (
                                t.symbol == symbol
                                and t.direction == direction
                                and t.provider_tag == "GB_FAST"
                            ):
                                # Attempt to close the fast trade (full close)
                                try:
                                    client = tradeExecutor._client_for(account)
                                    # Use partial_close with 100% to close fully
                                    client.partial_close(account, t.ticket, 100)
                                    log.info(f"[COMPLETE-SIGNAL] Closed FAST trade ticket={t.ticket} acct={t.account_name} due to price past TP1.")
                                except Exception as e:
                                    log.error(f"[COMPLETE-SIGNAL] Failed to close FAST trade ticket={t.ticket}: {e}")
                        return

        log.info("[SIGNAL] calling open_complete_trade trace=%s provider=%s symbol=%s dir=%s", trace_id, provider_tag, symbol, direction)
        log.info(f"[TRACE][SIGNAL] SL propagado a open_complete_trade: {sl}")
        # Filtrar cuentas según allowed_channels
        filtered_accounts = []
        for acct in accounts:
            allowed_channels = acct.get("allowed_channels")
            if allowed_channels is None:
                filtered_accounts.append(acct)
            else:
                if source_channel and any(int(ch) == source_channel for ch in allowed_channels):
                    filtered_accounts.append(acct)
        if not filtered_accounts:
            log.info(f"[SKIP] Ninguna cuenta permite el canal {source_channel}. Signal ignorada.")
            return
        # Ejecutar solo para las cuentas filtradas
        res = await MT5Executor(filtered_accounts,
            magic=tradeExecutor.magic,
            notifier=tradeExecutor.notifier,
            trading_windows=tradeExecutor.windows,
            entry_wait_seconds=tradeExecutor.entry_wait_seconds,
            entry_poll_ms=tradeExecutor.entry_poll_ms,
            entry_buffer_points=tradeExecutor.entry_buffer_points
        ).open_complete_trade(
            provider_tag=provider_tag,
            symbol=symbol,
            direction=direction,
            entry_range=entry_tuple,
            sl=float(sl) if sl else 0.0,
            tps=tps,
        )

        log.info("[SIGNAL] open_complete_trade done trace=%s", trace_id)

        # register opened
        for acct_name, ticket in res.tickets_by_account.items():
            log.info(f"[TRACE][SIGNAL] SL propagado a register_trade: {sl}")
            tradeManager.register_trade(
                account_name=acct_name,
                ticket=ticket,
                symbol=symbol,
                direction=direction,
                provider_tag=provider_tag,
                tps=tps,
                planned_sl=float(sl) if sl else None,
            )

        # Notify trade opened (friendly message) if notifier available
        if notifier_adapter is not None:
            try:
                entry_price = None
                if entry_tuple:
                    entry_price = (float(entry_tuple[0]) + float(entry_tuple[1])) / 2.0
                hint_price = float(fields.get("hint_price")) if fields.get("hint_price") else None
                use_price = entry_price if entry_price is not None else hint_price
                if use_price is None or use_price == 0.0:
                    log.error(f"[APP][ERROR] No se pudo obtener el precio de entrada para notificar trade abierto en {symbol}. Abortando notificación.")
                    return
                for acct_name, ticket in res.tickets_by_account.items():
                    asyncio.create_task(
                        tg_notifier.notify_trade_opened(
                            account_name=acct_name,
                            ticket=ticket,
                            symbol=symbol,
                            direction=direction,
                            entry_price=use_price,
                            sl_price=float(sl) if sl else None,
                            tp_prices=tps,
                            lot=0.0,
                            provider=provider_tag,
                        )
                    )
            except Exception as e:
                log.exception("failed to send trade_opened notifications: %s", e)

        if res.errors_by_account:
            await xadd(r, Streams.EVENTS, {"type": "open_errors", "errors": json.dumps(res.errors_by_account)})

    async def handle_mgmt(fields: dict):
        """
        Procesa mensajes de gestión recibidos (ej: comandos Hannah, Torofx, etc).
        """
        text = fields.get("text","")
        hint = fields.get("provider_hint","")
        if hint == "TOROFX":
            tradeManager.handle_torofx_management_message(int(fields.get("chat_id","0")), text)
        elif hint == "HANNAH":
            tradeManager.handle_hannah_management_message(int(fields.get("chat_id","0")), text)
        elif hint == "GOLD_BROTHERS":
            # aquí puedes enrutar a handle_bg_* si quieres
            pass

    REDIS_OFFSET_KEY = "signals:last_id"

    async def get_last_id():
        """
        Obtiene el último ID procesado de señales desde Redis.
        """
        try:
            redis_url = config.get("REDIS_URL", 'redis://localhost:6379/0')
            redis = aioredis.from_url(redis_url, decode_responses=True)
            last_id = await redis.get(REDIS_OFFSET_KEY)
            await redis.aclose()
            return last_id or "$"
        except Exception as e:
            log.warning(f"[OFFSET] Could not get last_id from Redis: {e}")
            return "$"

    async def set_last_id(last_id):
        """
        Guarda el último ID procesado de señales en Redis.
        """
        try:
            redis_url = config.get("REDIS_URL", 'redis://redis:6379/0')
            redis = aioredis.from_url(redis_url, decode_responses=True)
            await redis.set(REDIS_OFFSET_KEY, last_id)
            await redis.aclose()
        except Exception as e:
            log.warning(f"[OFFSET] Could not set last_id in Redis: {e}")

    async def loop_signals():
        """
        Loop principal que consume señales de trading y las procesa.
        """
        last_id = await get_last_id()
        log.info(f"[DEBUG] Suscrito a stream {Streams.SIGNALS} desde offset {last_id}")
        async for msg_id, fields in xread_loop(r, Streams.SIGNALS, last_id=last_id):
            log.info(f"[DEBUG] Mensaje recibido en stream {Streams.SIGNALS}: id={msg_id} fields={fields}")
            await handle_signal(fields)
            await set_last_id(msg_id)

    async def loop_mgmt():
        """
        Loop principal que consume mensajes de gestión y los procesa.
        """
        async for _, fields in xread_loop(r, Streams.MGMT, last_id="$"):
            await handle_mgmt(fields)

    # Lanzar el loop de gestión de trades en background
    asyncio.create_task(tradeManager.run_forever())
    await asyncio.gather(loop_signals(), loop_mgmt())

if __name__ == "__main__":
    asyncio.run(main())
