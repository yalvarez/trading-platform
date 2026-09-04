import os, re, json, logging, uuid
import datetime
import httpx
from services.common.config import Settings
from services.common.redis_streams import redis_client, xadd, Streams, create_consumer_group, xreadgroup_loop, xack
from services.common.signal_dedup import SignalDeduplicator
from parsers_base import SignalParser, ParseResult
from parsers_tradepulse import TradePulseParser


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
        "text": text,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload, timeout=10.0)
        if not (200 <= resp.status_code < 300):
            log.warning("[N8N_FORWARD] webhook respondio status=%s chat_id=%s", resp.status_code, chat_id)
    except Exception as e:
        log.warning("[N8N_FORWARD] error reenviando a n8n: %s", e)


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

    # Bucle robusto: reintenta creación de grupo si ocurre NOGROUP
    import asyncio
    while True:
        try:
            async for msg_id, fields in xreadgroup_loop(r, Streams.RAW, group, consumer):
                text = fields.get("text", "")
                chat_id = fields.get("chat_id", "")

                try:
                    sig = await router.process_raw_signal(chat_id, text)
                    if sig is DUPLICATE_SIGNAL:
                        # Reconocida pero descartada por dedup — ya se proceso la primera
                        # vez, no reenviar a n8n como si fuera texto no reconocido.
                        pass
                    elif sig:
                        trace_id = uuid.uuid4().hex[:8]
                        sig["chat_id"] = chat_id
                        sig["raw_text"] = text
                        sig["trace"] = trace_id
                        await xadd(r, Streams.SIGNALS, sig)
                        log.info(f"[SIGNAL] trace={trace_id} {sig['provider_tag']} {sig['direction']} {sig['symbol']}")
                    elif text.strip():
                        if n8n_webhook_url:
                            await forward_to_n8n(text, chat_id, n8n_webhook_url)
                        else:
                            log.warning("[N8N_FORWARD] N8N_INBOUND_WEBHOOK_URL no configurada — mensaje descartado: %r", text[:80])
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
