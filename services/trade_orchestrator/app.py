import os, json, asyncio, logging, sys, uuid
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

# Try to dynamically load `services/telegram_ingestor/create_session.py` as a module
tg_client = None
try:
    _tg_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'telegram_ingestor', 'create_session.py'))
    if os.path.exists(_tg_path):
        spec = importlib.util.spec_from_file_location("telegram_create_session", _tg_path)
        _mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_mod)
        tg_client = getattr(_mod, 'client', None)
except Exception:
    tg_client = None
from common.telegram_notifier import TelegramNotifier, NotificationConfig
from prometheus_client import start_http_server

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log = logging.getLogger("trade_orchestrator")

class NotifierAdapter:
    """Adapter exposing both async callable and notify() used across modules."""
    def __init__(self, tg_notifier: TelegramNotifier):
        self._tg = tg_notifier

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
            tg_notifier = TelegramNotifier(tg_client, notify_configs)
            notifier_adapter = NotifierAdapter(tg_notifier)
            log.info("TelegramNotifier initialized")
            # mark readiness for healthchecks
            try:
                with open('/tmp/telegram_notifier.ready', 'w', encoding='utf-8') as fh:
                    fh.write('ready')
            except Exception:
                log.debug('Could not write readiness file for TelegramNotifier')
        except Exception as e:
            log.error(f"Failed to initialize TelegramNotifier: {e}")

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
        log.info("[SIGNAL] recv trace=%s fields=%s", trace_id, json.dumps(fields, ensure_ascii=False))
        if not in_windows(parse_windows(s.trading_windows)):
            log.info("[SKIP] signal outside windows (no connect). trace=%s", trace_id)
            await xadd(r, Streams.EVENTS, {"type":"skip", "reason":"outside_windows", "trace": trace_id})
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

        log.info("[SIGNAL] calling open_complete_trade trace=%s provider=%s symbol=%s dir=%s entry=%s sl=%s tps=%s", trace_id, provider_tag, symbol, direction, str(entry_tuple), str(sl), str(tps))
        res = execu.open_complete_trade(
            provider_tag=provider_tag,
            symbol=symbol,
            direction=direction,
            entry_range=entry_tuple,
            sl=float(sl) if sl else 0.0,
            tps=tps,
        )

        log.info("[SIGNAL] open_complete_trade done trace=%s tickets=%s errors=%s", trace_id, json.dumps(res.tickets_by_account), json.dumps(res.errors_by_account))

        # register opened
        for acct_name, ticket in res.tickets_by_account.items():
            log.info("[SIGNAL] registering trade trace=%s acct=%s ticket=%s symbol=%s", trace_id, acct_name, ticket, symbol)
            tm.register_trade(
                account_name=acct_name,
                ticket=ticket,
                symbol=symbol,
                direction=direction,
                provider_tag=provider_tag,
                tps=tps,
                planned_sl=float(sl) if sl else None,
            )
            log.info("[SIGNAL] registered trade trace=%s acct=%s ticket=%s", trace_id, acct_name, ticket)

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
