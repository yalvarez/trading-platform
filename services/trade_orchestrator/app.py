import os, json, asyncio, logging, sys, uuid
import redis.asyncio as aioredis
from common.config import Settings
from common.redis_streams import redis_client, xread_loop, xadd, Streams
from common.timewindow import parse_windows, in_windows

from .mt5_executor import MT5Executor
from .trade_manager import TradeManager
from .bus import TradeBus
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
from .notifications.telegram import TelegramNotifierAdapter
# from prometheus_client import start_http_server


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
    s = Settings.load()
    r = await redis_client(s.redis_url)
    accounts = s.accounts()

    # Inicialización centralizada del notificador Telegram
    notifier_adapter = None
    if s.enable_notifications:
        try:
            tg_notifier = RemoteTelegramNotifier(os.getenv("TELEGRAM_INGESTOR_URL", "http://telegram_ingestor:8000"))
            notifier_adapter = TelegramNotifierAdapter(tg_notifier)
            log.info("TelegramNotifierAdapter initialized")
        except Exception as e:
            log.error(f"Failed to initialize TelegramNotifierAdapter: {e}")

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

    bus = TradeBus(s.redis_url)
    await bus.connect()
    log.info("TradeBus conectado. Esperando comandos centralizados...")

    async def handle_command(cmd: dict):
        """
        Procesa un comando recibido del bus centralizado y ejecuta la acción correspondiente.
        """
        # TODO: Mapear tipos de comando a métodos de MT5Executor/TradeManager
        # Ejemplo: open, move_sl, close, partial_close, trailing, be
        # Aquí solo se muestra el esqueleto para 'open'
        if cmd["type"] == "open":
            # Ejecutar la orden para cada cuenta dict recibido
            for account in cmd["accounts"]:
                res = await execu.open_runner_trade(
                    account=account,
                    symbol=cmd["symbol"],
                    direction=cmd["direction"],
                    volume=cmd["volume"],
                    sl=cmd["sl"],
                    tp=cmd["tp"][0] if cmd["tp"] else None,
                    provider_tag=cmd.get("provider_tag", "CENTRALIZED")
                )
                # Publicar evento de ejecución por cuenta
                await bus.publish_event({
                    "signal_id": cmd["signal_id"],
                    "account": account,
                    "type": "executed",
                    "ticket": getattr(res, "ticket", None),
                    "status": "success" if getattr(res, "ticket", None) else "error",
                    "details": str(res),
                    "timestamp": int(asyncio.get_event_loop().time())
                })
        # TODO: Implementar otros tipos de comando (move_sl, close, etc.)

    async def loop_commands():
        last_id = "$"
        async for msg_id, cmd in bus.listen_commands(last_id=last_id):
            await handle_command(cmd)
            last_id = msg_id

    # Lanzar el loop de gestión de trades en background
    asyncio.create_task(tm.run_forever())
    await asyncio.gather(loop_commands())

if __name__ == "__main__":
    asyncio.run(main())
