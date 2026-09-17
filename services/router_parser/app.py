import os, re, json, logging, uuid
import asyncio
import datetime
from datetime import timezone
import httpx
from services.common.config import Settings
from services.common.redis_streams import redis_client, xadd, Streams, create_consumer_group, xreadgroup_loop, xack
from services.common.signal_dedup import SignalDeduplicator
from services.trade_orchestrator.n8n_retry_worker import enqueue as enqueue_n8n_event
from parsers_base import SignalParser, ParseResult
from parsers_tradepulse import TradePulseParser
from parsers_management import match_close_now


# Add container label to log format for Grafana filtering
container_label = os.getenv("CONTAINER_LABEL") or os.getenv("HOSTNAME") or "router_parser"
log_fmt = f"%(asctime)s %(levelname)s [{container_label}] %(name)s: %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"), format=log_fmt)
log = logging.getLogger("router_parser")



from services.common.config import FAST_UPDATE_WINDOW_SECONDS

# Sentinel distinto de None: el texto SI fue reconocido como señal valida,
# pero el deduplicador lo descarto por repetido. Distinguirlo de "no
# reconocido" evita reenviarlo a n8n como ruido (ver SignalRouter.process_raw_signal).
DUPLICATE_SIGNAL = object()


async def forward_to_n8n(text: str, chat_id: str, webhook_url: str) -> None:
    """
    Reenvia texto que el parser de senales no reconocio a un webhook n8n
    de entrada, para que un flujo n8n/Ollama externo decida si es una
    excepcion de gestion accionable (ver dual-TP spec seccion 5.1).
    Nunca levanta: un fallo de red no debe tumbar el loop principal.
    """
    payload = {
        "chat_id": chat_id,
        "message": text,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload, timeout=10.0)
        if not (200 <= resp.status_code < 300):
            log.warning("[N8N_FORWARD] webhook respondio status=%s chat_id=%s", resp.status_code, chat_id)
    except Exception as e:
        log.warning("[N8N_FORWARD] error reenviando a n8n: %s", e)


CLOSE_NOW_RETRY_BACKOFF_SECONDS = [1, 2, 4]


async def execute_close_now_directly(
    chat_id: str, text: str, direction_hint, mgmt_url: str, action_api_key: str, redis_client,
) -> bool:
    """
    Ejecuta close_now directo contra /mgmt/action de trade_orchestrator,
    sin pasar por n8n/Ollama -- el patron "TRADE INVALID/Close now" es
    literal y no requiere clasificacion. Reintenta ante error de red,
    timeout o 5xx; NO reintenta ante 4xx (error de configuracion). Si se
    agotan los reintentos o llega un 4xx, encola una notificacion en la
    misma cola de reintentos que usa EventBus, para que el worker que ya
    corre en trade_orchestrator la entregue -- nunca cae a n8n (ver
    docs/superpowers/specs/2026-09-17-direct-close-now-shortcut-design.md
    seccion 5).
    """
    payload = {"action": "close_now", "chat_id": chat_id, "raw_text": text}
    if direction_hint:
        payload["direction_hint"] = direction_hint
    headers = {"X-N8N-Action-Key": action_api_key}

    # 3 intentos totales, con backoff SOLO entre intentos (no antes del
    # primero): intento 1 inmediato, intento 2 tras 1s, intento 3 tras 2s.
    # CLOSE_NOW_RETRY_BACKOFF_SECONDS[attempt - 1] indexa el gap que
    # PRECEDE al intento actual -- por eso el loop nunca consume el 4s
    # final de la constante con solo 3 intentos; ese tercer valor queda
    # disponible si el numero de intentos crece en el futuro.
    last_error = None
    for attempt in range(3):
        if attempt > 0:
            await asyncio.sleep(CLOSE_NOW_RETRY_BACKOFF_SECONDS[attempt - 1])
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(mgmt_url, json=payload, headers=headers, timeout=10.0)
            if 200 <= resp.status_code < 300:
                return True
            last_error = f"HTTP {resp.status_code}"
            if 400 <= resp.status_code < 500:
                break  # config error, retrying won't help
        except Exception as e:
            last_error = str(e)

    await _enqueue_close_now_failure(redis_client, chat_id=chat_id, raw_text=text, direction_hint=direction_hint, error=last_error)
    return False


async def _enqueue_close_now_failure(redis_client, *, chat_id: str, raw_text: str, direction_hint, error: str) -> None:
    envelope = {
        "event_id": str(uuid.uuid4()),
        "event_type": "mgmt_direct_close_failed",
        "channel": "both",
        "timestamp": datetime.datetime.now(timezone.utc).isoformat(),
        "message": (
            f"\U0001F6A8 CIERRE AUTOMÁTICO FALLIDO — Canal: {chat_id}\n"
            f"Motivo: \"{raw_text}\"\n"
            f"No se pudo ejecutar el cierre tras 3 intentos: {error}\n"
            f"REVISAR LA CUENTA MANUALMENTE — las posiciones pueden seguir abiertas."
        ),
        "payload": {"chat_id": chat_id, "raw_text": raw_text, "direction_hint": direction_hint, "error": error},
    }
    try:
        await enqueue_n8n_event(redis_client, envelope)
    except Exception as e:
        log.error("[CLOSE_NOW_DIRECT] no se pudo encolar la notificacion de fallo: %s", e)


async def dispatch_raw_message(
    *, router: "SignalRouter", redis_client, chat_id: str, text: str,
    n8n_webhook_url: str, mgmt_url: str, action_api_key: str,
) -> None:
    """
    Un mensaje crudo de Streams.RAW, ya sea senal o gestion. Cuatro casos,
    mutuamente excluyentes:
      1. Señal reconocida pero duplicada -- ya se proceso, no reenviar a n8n.
      2. Señal reconocida -- publicar a Streams.SIGNALS.
      3. Patron "TRADE INVALID/Close now" -- ejecutar close_now directo,
         NUNCA reenviar a n8n (ver spec 2026-09-17).
      4. Cualquier otro texto no vacio -- reenviar a n8n/Ollama.
    """
    sig = await router.process_raw_signal(chat_id, text)
    if sig is DUPLICATE_SIGNAL:
        return
    if sig:
        trace_id = uuid.uuid4().hex[:8]
        sig["chat_id"] = chat_id
        sig["raw_text"] = text
        sig["trace"] = trace_id
        await xadd(redis_client, Streams.SIGNALS, sig)
        log.info(f"[SIGNAL] trace={trace_id} {sig['provider_tag']} {sig['direction']} {sig['symbol']}")
        return

    close_now = match_close_now(text)
    if close_now:
        if mgmt_url:
            await execute_close_now_directly(
                chat_id=chat_id, text=text, direction_hint=close_now["direction_hint"],
                mgmt_url=mgmt_url, action_api_key=action_api_key, redis_client=redis_client,
            )
        else:
            log.error("[CLOSE_NOW_DIRECT] TRADE_ORCHESTRATOR_MGMT_URL no configurada — reenviando a n8n como fallback: %r", text[:80])
            if n8n_webhook_url:
                await forward_to_n8n(text, chat_id, n8n_webhook_url)
        return

    if text.strip():
        if n8n_webhook_url:
            await forward_to_n8n(text, chat_id, n8n_webhook_url)
        else:
            log.warning("[N8N_FORWARD] N8N_INBOUND_WEBHOOK_URL no configurada — mensaje descartado: %r", text[:80])


class SignalRouter:
    def __init__(self, redis_client, dedup_ttl=120.0):
        from parsers_tradepulse import TradePulseParser
        self.parser_map = {
            'tradepulse': TradePulseParser(),
        }
        self.deduplicator = SignalDeduplicator(redis_client, ttl_seconds=dedup_ttl)
        self.fast_update_window = FAST_UPDATE_WINDOW_SECONDS
        self.redis = redis_client

    def parse_signal(self, text, chat_id=None):
        norm = text.strip()
        for parser in self.parser_map.values():
            try:
                result = parser.parse(norm)
                if result:
                    if hasattr(result, 'entry_range') and result.entry_range is not None:
                        try:
                            entry_range = list(map(float, result.entry_range))
                            result = result.__class__(**{**result.__dict__, 'entry_range': entry_range})
                        except Exception as e:
                            log.warning(f"[PARSE_ERROR] entry_range conversion: {e}")
                            result = result.__class__(**{**result.__dict__, 'entry_range': None})
                    # log.debug(f"[PARSE] {parser.format_tag} matched")  # Reduce log noise
                    return result
            except Exception as e:
                log.warning(f"[PARSE_ERROR] {parser.__class__.__name__}: {e}")
                continue
        # log.debug("[PARSE] no parser matched")  # Reduce log noise
        return None

    async def process_raw_signal(self, chat_id, text):
        """
        Retorna un dict de señal lista para publicar, None si el texto no
        coincidio con ningun parser (candidato a reenviarse a n8n/Ollama), o
        el sentinel DUPLICATE_SIGNAL si SI fue reconocido pero el
        deduplicador lo descarto por repetido dentro de DEDUP_TTL_SECONDS.
        Esa distincion importa: un duplicado ya fue procesado la primera vez,
        asi que no debe reenviarse a n8n como si fuera texto no reconocido
        (ver app.py: loop_signals trata cada caso distinto).
        """
        parse_result = self.parse_signal(text, chat_id=chat_id)
        if not parse_result:
            return None

        # Si es señal completa, busca una FAST previa para actualizar
        if not parse_result.is_fast:
            # Buscar señales FAST recientes para el mismo chat, símbolo y dirección
            key_prefix = f"fast_sig:{chat_id}:{parse_result.symbol}:{parse_result.direction}"
            fast_key = f"{key_prefix}"
            fast_data = await self.redis.get(fast_key)
            if fast_data:
                # Hay una señal FAST previa, actualizarla
                # log.info(f"[FAST-UPDATE] Actualizando señal FAST previa para {parse_result.symbol} {parse_result.direction}")  # Reduce log noise
                await self.redis.delete(fast_key)
                # No deduplicar, forzar update
            elif await self.deduplicator.is_duplicate(chat_id, parse_result):
                # log.info("[DEDUP] %s", parse_result.provider_tag)  # Reduce log noise
                return DUPLICATE_SIGNAL
        else:
            # Es señal FAST, guarda referencia para posible actualización
            key_prefix = f"fast_sig:{chat_id}:{parse_result.symbol}:{parse_result.direction}"
            await self.redis.setex(key_prefix, int(self.fast_update_window), "1")
            if await self.deduplicator.is_duplicate(chat_id, parse_result):
                # log.info("[DEDUP] %s", parse_result.provider_tag)  # Reduce log noise
                return DUPLICATE_SIGNAL

        entry_range = json.dumps(parse_result.entry_range) if parse_result.entry_range else ""
        tps = parse_result.tps or []

        # Always ensure entry_range is a valid JSON array (never a string tuple)
        if parse_result.entry_range is not None:
            try:
                entry_range = json.dumps(list(map(float, parse_result.entry_range)))
            except Exception:
                entry_range = json.dumps([])
        else:
            entry_range = json.dumps([])

        return {
            "symbol": parse_result.symbol,
            "direction": parse_result.direction,
            "entry_range": entry_range,
            "sl": str(parse_result.sl) if parse_result.sl is not None else "",
            "tps": json.dumps(tps),
            "provider_tag": parse_result.provider_tag,
            "format_tag": parse_result.format_tag,
            "fast": "true" if parse_result.is_fast else "false",
            "hint_price": str(parse_result.hint_price) if parse_result.hint_price else "",
        }

async def main():
    from services.common.env_validator import validate_router_parser
    validate_router_parser()

    s = Settings.load()
    r = await redis_client(s["redis_url"])
    router = SignalRouter(r, dedup_ttl=s["dedup_ttl_seconds"])
    group = "router_group"
    consumer = f"consumer_{os.getpid()}"

    from services.common.config import config as _config
    n8n_webhook_url = _config.get("N8N_INBOUND_WEBHOOK_URL", "")
    mgmt_url = _config.get("TRADE_ORCHESTRATOR_MGMT_URL", "")
    action_api_key = _config.get("N8N_ACTION_API_KEY", "")

    # Bucle robusto: reintenta creación de grupo si ocurre NOGROUP
    import asyncio
    while True:
        try:
            async for msg_id, fields in xreadgroup_loop(r, Streams.RAW, group, consumer):
                text = fields.get("text", "")
                chat_id = fields.get("chat_id", "")
                try:
                    await dispatch_raw_message(
                        router=router, redis_client=r, chat_id=chat_id, text=text,
                        n8n_webhook_url=n8n_webhook_url, mgmt_url=mgmt_url, action_api_key=action_api_key,
                    )
                finally:
                    await xack(r, Streams.RAW, group, msg_id)
        except Exception as e:
            if "NOGROUP" in str(e):
                log.warning("[REDIS] NOGROUP detectado, reintentando creación de grupo...")
                await create_consumer_group(r, Streams.RAW, group)
                await asyncio.sleep(1)
                continue
            else:
                log.error(f"[FATAL] Error inesperado en bucle de consumo: {e}")
                raise

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
