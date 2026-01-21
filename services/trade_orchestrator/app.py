import os, json, asyncio, logging, sys, uuid
import aioredis
from common.config import Settings
from common.redis_streams import redis_client, xread_loop, xadd, Streams
from common.timewindow import parse_windows, in_windows

from mt5_executor import MT5Executor
from trade_manager import TradeManager
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

from common.telegram_notifier import RemoteTelegramNotifier, NotificationConfig
from prometheus_client import start_http_server


# Add container label to log format for Grafana filtering
container_label = os.getenv("CONTAINER_LABEL") or os.getenv("HOSTNAME") or "trade_orchestrator"
log_fmt = f"%(asctime)s %(levelname)s [{container_label}] %(name)s: %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"), format=log_fmt)
log = logging.getLogger("trade_orchestrator")

class NotifierAdapter:
    """Adapter exposing both async callable and notify() used across modules."""

    def __init__(self, tg_notifier):
        self._tg = tg_notifier

    async def notify_tp_hit(self, account_name: str, ticket: int, symbol: str, tp_index: int, tp_price: float, current_price: float):
        return await self._tg.notify_tp_hit(
            account_name=account_name,
            ticket=ticket,
            symbol=symbol,
            tp_index=tp_index,
            tp_price=tp_price,
            current_price=current_price,
        )

    async def notify_partial_close(self, *args, **kwargs):
        if hasattr(self._tg, "notify_partial_close"):
            return await self._tg.notify_partial_close(*args, **kwargs)

    async def __call__(self, account_name: str, message: str):
        await self._tg.notify(account_name, message)

    async def notify(self, account_name: str, message: str):
        await self._tg.notify(account_name, message)

async def main():
    s = Settings.load()
    # start Prometheus metrics server
    try:
        metrics_port = int(os.getenv("METRICS_PORT", "8000"))
        start_http_server(metrics_port)
        log.info(f"Prometheus metrics server started on :{metrics_port}")
    except Exception as e:
        log.error(f"Failed to start Prometheus metrics server: {e}")
    r = await redis_client(s.redis_url)
    accounts = s.accounts()

    # Initialize Telegram-based notifier (if configured)
    notifier_adapter = None
    if s.enable_notifications:
        chat_list = os.getenv("TG_NOTIFY_TARGET") or os.getenv("TG_SOURCE_CHATS") or ""
        first_chat = None
        if chat_list:
            try:
                first_chat = int(chat_list.split(",")[0].strip())
            except Exception:
                first_chat = None

        notify_configs = []
        for a in accounts:
            notify_configs.append(NotificationConfig(account_name=a.get("name"), chat_id=first_chat))

        try:
            tg_notifier = RemoteTelegramNotifier(os.getenv("TELEGRAM_INGESTOR_URL", "http://telegram_ingestor:8000"))
            notifier_adapter = NotifierAdapter(tg_notifier)
            log.info("RemoteTelegramNotifier initialized")
        except Exception as e:
            log.error(f"Failed to initialize RemoteTelegramNotifier: {e}")

    execu = MT5Executor(
        accounts,
        magic=987654,
        notifier=(notifier_adapter if notifier_adapter is not None else None),
        trading_windows=s.trading_windows,
        entry_wait_seconds=s.entry_wait_seconds,
        entry_poll_ms=s.entry_poll_ms,
        entry_buffer_points=s.entry_buffer_points,
    )

    tm = TradeManager(execu, notifier=(notifier_adapter if notifier_adapter is not None else None))  # attach notifier if available

    async def handle_signal(fields: dict):
        trace_id = uuid.uuid4().hex[:8]
        orig_trace = fields.get("trace", "NO_TRACE")
        if not in_windows(parse_windows(s.trading_windows)):
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
                client = execu._client_for(account)
                price = client.tick_price(symbol, direction)
                # Obtener default_sl_pips desde entorno o config
                default_sl_pips = float(os.getenv("DEFAULT_SL_XAUUSD_PIPS", 300)) if symbol.upper().startswith("XAU") else float(os.getenv("DEFAULT_SL_PIPS", 100))
                # Usar la función de TradeManager para convertir pips a precio
                point = 0.1 if symbol.upper().startswith("XAU") else 0.00001
                if hasattr(tm, "_pips_to_price"):
                    sl_offset = tm._pips_to_price(symbol, default_sl_pips, point)
                else:
                    sl_offset = default_sl_pips * point
                if direction.upper() == "BUY":
                    forced_sl = price - sl_offset
                else:
                    forced_sl = price + sl_offset
                sl = str(round(forced_sl, 2 if symbol.upper().startswith("XAU") else 5))
                log.info(f"[TRACE][SIGNAL][FAST] SL forzado en handle_signal (pips_to_price): {sl} (price={price}, offset={sl_offset})")
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
            for acct_name, trade in list(tm.trades.items()):
                t = trade
                if (
                    t.symbol == symbol
                    and t.direction == direction
                    and t.provider_tag == "GB_FAST"
                ):
                    # Update the trade with new SL, TPs, and provider_tag
                    log.info(f"[TRACE][FAST-UPDATE] SL recibido para update_trade_signal: {sl}")
                    tm.update_trade_signal(
                        ticket=t.ticket,
                        tps=tps,
                        planned_sl=float(sl) if sl else None,
                        provider_tag=provider_tag,
                    )
                    # --- NEW: Update SL in MT5 as well ---
                    try:
                        account = next((a for a in accounts if a.get("name") == t.account_name), None)
                        if account and t.ticket and sl:
                            result = await execu.modify_sl(account, t.ticket, float(sl), reason="full-signal")
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
                    client = execu._client_for(account)
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
                        for acct_name, trade in list(tm.trades.items()):
                            t = trade
                            if (
                                t.symbol == symbol
                                and t.direction == direction
                                and t.provider_tag == "GB_FAST"
                            ):
                                # Attempt to close the fast trade (full close)
                                try:
                                    client = execu._client_for(account)
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
                filtered_accounts.append(acct)  # Si no está definido, acepta todos los canales
            else:
                # Puede ser lista de int o str
                if source_channel and any(int(ch) == source_channel for ch in allowed_channels):
                    filtered_accounts.append(acct)
        if not filtered_accounts:
            log.info(f"[SKIP] Ninguna cuenta permite el canal {source_channel}. Signal ignorada.")
            return
        # Ejecutar solo para las cuentas filtradas
        res = await MT5Executor(filtered_accounts,
            magic=execu.magic,
            notifier=execu.notifier,
            trading_windows=execu.windows,
            entry_wait_seconds=execu.entry_wait_seconds,
            entry_poll_ms=execu.entry_poll_ms,
            entry_buffer_points=execu.entry_buffer_points
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
            tm.register_trade(
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
                use_price = entry_price if entry_price is not None else hint_price or 0.0
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
        text = fields.get("text","")
        hint = fields.get("provider_hint","")
        if hint == "TOROFX":
            tm.handle_torofx_management_message(int(fields.get("chat_id","0")), text)
        elif hint == "HANNAH":
            tm.handle_hannah_management_message(int(fields.get("chat_id","0")), text)
        elif hint == "GOLD_BROTHERS":
            # aquí puedes enrutar a handle_bg_* si quieres
            pass

    REDIS_OFFSET_KEY = "signals:last_id"

    async def get_last_id():
        try:
            redis_url = s.redis_url if hasattr(s, 'redis_url') else os.getenv('REDIS_URL', 'redis://localhost:6379/0')
            redis = await aioredis.from_url(redis_url, decode_responses=True)
            last_id = await redis.get(REDIS_OFFSET_KEY)
            await redis.close()
            return last_id or "$"
        except Exception as e:
            log.warning(f"[OFFSET] Could not get last_id from Redis: {e}")
            return "$"

    async def set_last_id(last_id):
        try:
            redis_url = s.redis_url if hasattr(s, 'redis_url') else os.getenv('REDIS_URL', 'redis://localhost:6379/0')
            redis = await aioredis.from_url(redis_url, decode_responses=True)
            await redis.set(REDIS_OFFSET_KEY, last_id)
            await redis.close()
        except Exception as e:
            log.warning(f"[OFFSET] Could not set last_id in Redis: {e}")

    async def loop_signals():
        last_id = await get_last_id()
        async for msg_id, fields in xread_loop(r, Streams.SIGNALS, last_id=last_id):
            await handle_signal(fields)
            await set_last_id(msg_id)

    async def loop_mgmt():
        async for _, fields in xread_loop(r, Streams.MGMT, last_id="$"):
            await handle_mgmt(fields)

    # Lanzar el loop de gestión de trades en background
    asyncio.create_task(tm.run_forever())
    await asyncio.gather(loop_signals(), loop_mgmt())

if __name__ == "__main__":
    asyncio.run(main())
